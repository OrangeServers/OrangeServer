# -*- coding: utf-8 -*-
"""M1/S2: Celery Worker 接线与任务契约测试（Issue #13）。

与租约测试相同的注入式 SQLite 内存引擎方案；Celery 应用只做构造
与配置断言，不连接真实 broker（投递用替身记录）。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.autonomy import worker
from app.ai.autonomy.repository import AutonomyRepository
from app.core import config
from app.core.db.database import (
    db,
    t_ai_autonomous_event,
    t_ai_autonomous_run,
    t_ai_autonomous_step,
    t_group,
    t_host,
)

SECRET_KEY = "unit-test-secret-key-for-autonomy-worker"


class FakePlatform:
    def __init__(self, owner, role):
        pass

    def validate_asset_ids(self, asset_ids):
        return True

    def resolve_system_user(self, sys_user_id):
        return {"id": int(sys_user_id), "alias": "readonly"}


class FakeCeleryApp:
    """记录 send_task 调用的投递替身。"""

    def __init__(self):
        self.sent = []

    def send_task(self, name, args=None, **kwargs):
        self.sent.append((name, tuple(args or ())))


@pytest.fixture()
def worker_env(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    db.metadata.create_all(
        engine,
        tables=[t_group.__table__, t_host.__table__,
                t_ai_autonomous_run.__table__,
                t_ai_autonomous_step.__table__,
                t_ai_autonomous_event.__table__],
    )
    session = sessionmaker(bind=engine)()
    repo = AutonomyRepository(
        session, SECRET_KEY,
        platform_factory=lambda owner, role: FakePlatform(owner, role),
    )

    host_seq = {"n": 0}

    def create_queued_run(**kwargs):
        host_seq["n"] += 1
        n = host_seq["n"]
        host = t_host(
            alias="web-%02d" % n, host_ip="203.0.113.%d" % (40 + n),
            host_port=22, ai_environment="lab",
        )
        session.add(host)
        session.commit()
        payload = dict(
            goal="diagnose latency",
            host_id=int(host.id),
            system_user_id=19,
            mode="assisted",
        )
        payload.update(kwargs)
        run = repo.create_run("admin", "admin", **payload)
        return repo.start_run("admin", "admin", run["id"])

    monkeypatch.setattr(config, "AI_AUTONOMY_ENABLED", True)
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_HOST", "192.0.2.10")
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_PORT", 6390)
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_PASSWORD", "fake-pass")
    monkeypatch.setattr(config, "AI_AUTONOMY_LEASE_TTL_SECONDS", 120)
    worker.reset_celery_app()

    env = {
        "session": session,
        "create_queued_run": create_queued_run,
    }
    yield env
    worker.reset_celery_app()
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Celery 应用：功能门与接线契约
# ---------------------------------------------------------------------------

def test_celery_app_requires_feature_flag(worker_env, monkeypatch):
    monkeypatch.setattr(config, "AI_AUTONOMY_ENABLED", False)
    assert worker.get_celery_app() is None


def test_celery_app_requires_redis_wiring(worker_env, monkeypatch):
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_HOST", "")
    assert worker.get_celery_app() is None


def test_celery_app_uses_dedicated_broker_db1(worker_env):
    app = worker.get_celery_app()
    assert app is not None
    assert app.conf.broker_url == "redis://:fake-pass@192.0.2.10:6390/1"
    # 至少一次投递契约：晚确认 + worker 丢失重投 + 单预取、无 result backend。
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.result_backend is None
    assert worker.DRIVE_RUN_TASK in app.tasks
    # 单例：重复获取是同一实例。
    assert worker.get_celery_app() is app


def test_broker_url_without_password(worker_env, monkeypatch):
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_PASSWORD", "")
    assert worker.autonomy_broker_url() == "redis://192.0.2.10:6390/1"


def test_worker_identity_fits_lease_owner_column():
    assert 0 < len(worker.worker_identity()) <= 64


# ---------------------------------------------------------------------------
# drive_run 任务内核：只带 run_id，重复投递绝不二次执行
# ---------------------------------------------------------------------------

def test_drive_run_claims_and_executes_once(worker_env):
    run = worker_env["create_queued_run"]()
    calls = []

    result = worker.drive_run_once(
        worker_env["session"], run["id"],
        executor=lambda run_id, claim: calls.append((run_id, claim)),
        worker_id="worker-a",
    )
    assert result == worker.RESULT_CLAIMED
    assert len(calls) == 1
    assert calls[0][0] == run["id"]


def test_duplicate_delivery_never_executes_twice(worker_env):
    """同一 Worker 的重复投递：执行中直接跳过，执行器只被调用一次。"""
    run = worker_env["create_queued_run"]()
    calls = []

    def executor(run_id, claim):
        calls.append(run_id)

    first = worker.drive_run_once(
        worker_env["session"], run["id"],
        executor=executor, worker_id="worker-a",
    )
    second = worker.drive_run_once(
        worker_env["session"], run["id"],
        executor=executor, worker_id="worker-a",
    )
    assert first == worker.RESULT_CLAIMED
    assert second == worker.RESULT_SKIPPED
    assert calls == [run["id"]]


def test_delivery_to_other_workers_run_is_skipped(worker_env):
    run = worker_env["create_queued_run"]()
    calls = []
    worker.drive_run_once(
        worker_env["session"], run["id"],
        executor=lambda run_id, claim: calls.append(run_id),
        worker_id="worker-a",
    )
    result = worker.drive_run_once(
        worker_env["session"], run["id"],
        executor=lambda run_id, claim: calls.append(run_id),
        worker_id="worker-b",
    )
    assert result == worker.RESULT_SKIPPED
    assert calls == [run["id"]]


def test_drive_run_without_executor_fails_closed(worker_env):
    """执行器未接线：认领后立即释放租约，绝不伪造执行结果。"""
    from app.core.db.database import t_ai_autonomous_run
    from app.ai.autonomy.lease import RunLeaseService

    run = worker_env["create_queued_run"]()
    result = worker.drive_run_once(
        worker_env["session"], run["id"], worker_id="worker-a",
    )
    assert result == worker.RESULT_EXECUTOR_UNAVAILABLE
    row = worker_env["session"].query(t_ai_autonomous_run).filter_by(
        id=run["id"],
    ).one()
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    # 释放后仍会被启动扫描重新发现（running + 无租约）。
    candidates = RunLeaseService(worker_env["session"]).scan_recoverable()
    assert run["id"] in {c["run_id"] for c in candidates}


def test_drive_run_unknown_run_is_skipped(worker_env):
    result = worker.drive_run_once(
        worker_env["session"], "missing-run", worker_id="worker-a",
    )
    assert result == worker.RESULT_SKIPPED


# ---------------------------------------------------------------------------
# 启动扫描投递
# ---------------------------------------------------------------------------

def test_dispatch_recoverable_enqueues_candidates(worker_env):
    queued = worker_env["create_queued_run"]()
    fake_app = FakeCeleryApp()
    candidates = worker.dispatch_recoverable(
        worker_env["session"], celery_app=fake_app,
    )
    assert [c["run_id"] for c in candidates] == [queued["id"]]
    assert fake_app.sent == [(worker.DRIVE_RUN_TASK, (queued["id"],))]


def test_dispatch_recoverable_disabled_sends_nothing(worker_env):
    worker_env["create_queued_run"]()
    fake_app = FakeCeleryApp()
    # celery_app=None 且功能关闭 → 不投递。
    worker.reset_celery_app()
    config.AI_AUTONOMY_ENABLED = False
    try:
        candidates = worker.dispatch_recoverable(
            worker_env["session"], celery_app=None,
        )
    finally:
        config.AI_AUTONOMY_ENABLED = True
    assert candidates == []
    assert fake_app.sent == []
