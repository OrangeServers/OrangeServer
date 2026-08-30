# -*- coding: utf-8 -*-
"""M1/S2: Celery Worker 接线与任务契约测试（Issue #13）。

与租约测试相同的注入式 SQLite 内存引擎方案；Celery 应用只做构造
与配置断言，不连接真实 broker（投递用替身记录）。
"""
import datetime
import pickle
import sys
from types import SimpleNamespace

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

    def validate_asset_sys_user_id_pair(self, asset_ids, sys_user_id):
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
            mode="ask",
        )
        payload.update(kwargs)
        run = repo.create_run("admin", "admin", **payload)
        return repo.start_run("admin", "admin", run["id"])

    monkeypatch.setattr(config, "AI_AUTONOMY_ENABLED", True)
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_HOST", "192.0.2.10")
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_PORT", 6390)
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_PASSWORD", "fake-pass")
    monkeypatch.setattr(config, "AI_AUTONOMY_WORKER_CONCURRENCY", 2)
    monkeypatch.setattr(config, "AI_AUTONOMY_LEASE_TTL_SECONDS", 120)
    worker.reset_celery_app()

    env = {
        "session": session,
        "repo": repo,
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
    assert app.conf.worker_pool == "prefork"
    assert app.conf.worker_concurrency == 2
    assert app.conf.result_backend is None
    assert worker.DRIVE_RUN_TASK in app.tasks
    assert worker.REINDEX_KNOWLEDGE_TASK in app.tasks
    assert app.tasks[worker.DRIVE_RUN_TASK].max_retries is None
    # 单例：重复获取是同一实例。
    assert worker.get_celery_app() is app


@pytest.mark.parametrize("concurrency", [1, 4])
def test_celery_app_uses_configured_prefork_concurrency(
    worker_env, monkeypatch, concurrency,
):
    monkeypatch.setattr(
        config, "AI_AUTONOMY_WORKER_CONCURRENCY", concurrency,
    )

    app = worker.get_celery_app()

    assert app.conf.worker_pool == "prefork"
    assert app.conf.worker_concurrency == concurrency


def test_worker_process_init_resets_inherited_resources(monkeypatch):
    import app.app_factory as app_factory
    import app.core.db.database as database_mod

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    class _FakeFlaskApp:
        def app_context(self):
            return _Ctx()

    class _Session:
        def __init__(self):
            self.removed = 0

        def remove(self):
            self.removed += 1

    class _Engine:
        def __init__(self):
            self.dispose_kwargs = None

        def dispose(self, **kwargs):
            self.dispose_kwargs = kwargs

    class _Pool:
        def __init__(self):
            self.disconnect_kwargs = None

        def disconnect(self, **kwargs):
            self.disconnect_kwargs = kwargs

    session = _Session()
    engine = _Engine()
    pool = _Pool()
    monkeypatch.setattr(app_factory, "app", _FakeFlaskApp())
    monkeypatch.setattr(
        database_mod, "db", SimpleNamespace(session=session, engine=engine),
    )
    monkeypatch.setitem(
        sys.modules, "app.tools.redisdb",
        SimpleNamespace(_shared_pool=pool),
    )

    worker._on_worker_process_init()

    assert session.removed == 1
    assert engine.dispose_kwargs == {"close": False}
    assert pool.disconnect_kwargs == {"inuse_connections": True}


def test_broker_url_without_password(worker_env, monkeypatch):
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_PASSWORD", "")
    assert worker.autonomy_broker_url() == "redis://192.0.2.10:6390/1"


def test_broker_url_percent_encodes_reserved_password_characters(
    worker_env, monkeypatch,
):
    monkeypatch.setattr(
        config, "AI_AUTONOMY_REDIS_PASSWORD", "fake@pass:/#% ?",
    )
    assert worker.autonomy_broker_url() == (
        "redis://:fake%40pass%3A%2F%23%25%20%3F@192.0.2.10:6390/1"
    )


def test_worker_identity_fits_lease_owner_column():
    assert 0 < len(worker.worker_identity()) <= 64


def test_lease_retry_signal_survives_prefork_serialization():
    original = worker.LeaseRetryRequired("run-x", 17)

    restored = pickle.loads(pickle.dumps(original))

    assert restored.run_id == "run-x"
    assert restored.retry_after_seconds == 17


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


def test_duplicate_delivery_waits_without_executing_twice(worker_env):
    """同 identity 的重复投递也延迟到租约边界，绝不二次执行。"""
    run = worker_env["create_queued_run"]()
    calls = []

    def executor(run_id, claim):
        calls.append(run_id)

    first = worker.drive_run_once(
        worker_env["session"], run["id"],
        executor=executor, worker_id="worker-a",
    )
    with pytest.raises(worker.LeaseRetryRequired):
        worker.drive_run_once(
            worker_env["session"], run["id"],
            executor=executor, worker_id="worker-a",
        )
    assert first == worker.RESULT_CLAIMED
    assert calls == [run["id"]]


def test_delivery_to_other_workers_live_run_is_retried(worker_env):
    run = worker_env["create_queued_run"]()
    calls = []
    worker.drive_run_once(
        worker_env["session"], run["id"],
        executor=lambda run_id, claim: calls.append(run_id),
        worker_id="worker-a",
    )
    with pytest.raises(worker.LeaseRetryRequired):
        worker.drive_run_once(
            worker_env["session"], run["id"],
            executor=lambda run_id, claim: calls.append(run_id),
            worker_id="worker-b",
        )
    assert calls == [run["id"]]


def test_redelivery_retries_until_old_lease_expires_then_recovers(worker_env):
    """Worker 丢失后的 redelivery 不能 ACK 掉唯一的恢复机会。"""
    run = worker_env["create_queued_run"]()
    calls = []

    worker.drive_run_once(
        worker_env["session"], run["id"],
        executor=lambda run_id, claim: calls.append((run_id, claim)),
        worker_id="worker-a", lease_ttl=60,
    )

    with pytest.raises(worker.LeaseRetryRequired) as caught:
        worker.drive_run_once(
            worker_env["session"], run["id"],
            executor=lambda run_id, claim: calls.append((run_id, claim)),
            worker_id="worker-b", lease_ttl=60,
        )
    assert caught.value.retry_after_seconds >= 1
    assert len(calls) == 1
    assert calls[0][0] == run["id"]

    row = worker_env["session"].get(t_ai_autonomous_run, run["id"])
    row.lease_expires_at = (
        datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
    )
    worker_env["session"].commit()

    recovered = worker.drive_run_once(
        worker_env["session"], run["id"],
        executor=lambda run_id, claim: calls.append((run_id, claim)),
        worker_id="worker-b", lease_ttl=60,
    )
    assert recovered == worker.RESULT_CLAIMED
    assert [run_id for run_id, _claim in calls] == [run["id"], run["id"]]
    worker_env["session"].expire_all()
    row = worker_env["session"].get(t_ai_autonomous_run, run["id"])
    assert row.status == "recovering"
    assert row.lease_owner == "worker-b"


def test_same_identity_expired_lease_is_reclaimed(worker_env):
    """PID/hostname 被复用也不能让已过期租约永久跳过恢复。"""
    run = worker_env["create_queued_run"]()
    calls = []
    worker.drive_run_once(
        worker_env["session"], run["id"],
        executor=lambda run_id, claim: calls.append(run_id),
        worker_id="worker-a", lease_ttl=60,
    )
    row = worker_env["session"].get(t_ai_autonomous_run, run["id"])
    row.lease_expires_at = (
        datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
    )
    worker_env["session"].commit()

    result = worker.drive_run_once(
        worker_env["session"], run["id"],
        executor=lambda run_id, claim: calls.append(run_id),
        worker_id="worker-a", lease_ttl=60,
    )

    assert result == worker.RESULT_CLAIMED
    assert calls == [run["id"], run["id"]]


def test_drive_run_without_executor_fails_closed(worker_env):
    """执行器未接线：认领后立即释放租约，绝不伪造执行结果。

    释放后的 running + 无租约不是启动扫描候选（健康空闲不
    churn）；接线修复后由新的投递/扫描唤醒。
    """
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
    candidates = RunLeaseService(worker_env["session"]).scan_recoverable()
    assert run["id"] not in {c["run_id"] for c in candidates}


def test_drive_run_unknown_run_is_skipped(worker_env):
    result = worker.drive_run_once(
        worker_env["session"], "missing-run", worker_id="worker-a",
    )
    assert result == worker.RESULT_SKIPPED


def test_drive_run_task_runs_inside_flask_app_context(
    worker_env, monkeypatch,
):
    """Celery 任务内核：先推应用上下文再用 db.session（Flask-
    SQLAlchemy 3.x 下无上下文直接拿 session 会 RuntimeError）。"""
    import app.app_factory as app_factory
    import app.core.db.database as database_mod

    entered = {"count": 0}

    class _Ctx:
        def __enter__(self):
            entered["count"] += 1
            return self

        def __exit__(self, *exc_info):
            return False

    class _FakeFlaskApp:
        def app_context(self):
            return _Ctx()

    class _Session:
        def __init__(self):
            self.removed = 0

        def remove(self):
            self.removed += 1

    sentinel_session = _Session()

    class _FakeDb:
        session = sentinel_session

    monkeypatch.setattr(app_factory, "app", _FakeFlaskApp())
    monkeypatch.setattr(database_mod, "db", _FakeDb())

    captured = {}

    def fake_once(session, run_id, *, executor=None, worker_id=None,
                  lease_ttl=None):
        captured["session"] = session
        captured["inside_context"] = entered["count"] == 1
        return worker.RESULT_SKIPPED

    monkeypatch.setattr(worker, "drive_run_once", fake_once)
    monkeypatch.setattr(
        worker, "build_default_executor", lambda session: None,
    )

    celery_app = worker.get_celery_app()
    result = celery_app.tasks[worker.DRIVE_RUN_TASK].apply(args=["run-x"])
    assert result.result == worker.RESULT_SKIPPED
    assert captured["session"] is sentinel_session
    assert captured["inside_context"] is True
    assert sentinel_session.removed == 1


def test_drive_run_task_uses_delayed_celery_retry_for_live_lease(
    worker_env, monkeypatch,
):
    """租约未到期时必须延迟重投，不能 ACK，也不能立即忙循环。"""
    import app.app_factory as app_factory
    import app.core.db.database as database_mod

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    class _FakeFlaskApp:
        def app_context(self):
            return _Ctx()

    class _Session:
        def __init__(self):
            self.removed = 0

        def remove(self):
            self.removed += 1

    sentinel_session = _Session()

    class _FakeDb:
        session = sentinel_session

    monkeypatch.setattr(app_factory, "app", _FakeFlaskApp())
    monkeypatch.setattr(database_mod, "db", _FakeDb())
    monkeypatch.setattr(
        worker, "build_default_executor", lambda session: object(),
    )
    monkeypatch.setattr(
        worker,
        "drive_run_once",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            worker.LeaseRetryRequired("run-x", 17)
        ),
    )

    task = worker.get_celery_app().tasks[worker.DRIVE_RUN_TASK]
    captured = {}

    class _RetrySignal(Exception):
        pass

    def fake_retry(*, exc, countdown):
        captured.update(exc=exc, countdown=countdown)
        raise _RetrySignal()

    monkeypatch.setattr(task, "retry", fake_retry)
    with pytest.raises(_RetrySignal):
        task.run("run-x")

    assert isinstance(captured["exc"], worker.LeaseRetryRequired)
    assert captured["countdown"] == 17
    assert sentinel_session.removed == 1


def test_worker_ready_scan_runs_inside_flask_app_context(
    worker_env, monkeypatch,
):
    """启动扫描信号同样必须先推应用上下文再用 db.session。"""
    import app.app_factory as app_factory
    import app.core.db.database as database_mod

    entered = {"count": 0}

    class _Ctx:
        def __enter__(self):
            entered["count"] += 1
            return self

        def __exit__(self, *exc_info):
            return False

    class _FakeFlaskApp:
        def app_context(self):
            return _Ctx()

    sentinel_session = object()

    class _FakeDb:
        session = sentinel_session

    monkeypatch.setattr(app_factory, "app", _FakeFlaskApp())
    monkeypatch.setattr(database_mod, "db", _FakeDb())

    captured = {}

    def fake_dispatch(session, celery_app=None):
        captured["session"] = session
        captured["inside_context"] = entered["count"] == 1
        return []

    monkeypatch.setattr(worker, "dispatch_recoverable", fake_dispatch)

    worker._on_worker_ready()
    assert captured["session"] is sentinel_session
    assert captured["inside_context"] is True


def test_worker_ready_installs_periodic_recovery_after_publish_failure(
    worker_env, monkeypatch,
):
    """初次 publish 失败后，Celery worker timer 必须持续扫描补投。"""
    import app.app_factory as app_factory
    import app.core.db.database as database_mod

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    class _FakeFlaskApp:
        def app_context(self):
            return _Ctx()

    class _FakeDb:
        session = object()

    class _Timer:
        def __init__(self):
            self.callback = None
            self.interval = None

        def call_repeatedly(self, interval, callback):
            self.interval = interval
            self.callback = callback

    class _Sender:
        timer = _Timer()

    attempts = []

    def flaky_dispatch(session, celery_app=None):
        attempts.append(session)
        if len(attempts) == 1:
            raise OSError("broker publish failed")
        return []

    monkeypatch.setattr(app_factory, "app", _FakeFlaskApp())
    monkeypatch.setattr(database_mod, "db", _FakeDb())
    monkeypatch.setattr(worker, "dispatch_recoverable", flaky_dispatch)

    sender = _Sender()
    worker._on_worker_ready(sender=sender)
    assert len(attempts) == 1
    assert sender.timer.interval == worker.RECOVERY_SCAN_INTERVAL_SECONDS
    assert callable(sender.timer.callback)

    sender.timer.callback()
    assert len(attempts) == 2


def test_worker_ready_registers_one_timer_per_worker_lifecycle(
    worker_env, monkeypatch,
):
    scans = []

    class _Timer:
        def __init__(self):
            self.registrations = []

        def call_repeatedly(self, interval, callback):
            self.registrations.append((interval, callback))

    class _Sender:
        def __init__(self):
            self.timer = _Timer()

    monkeypatch.setattr(
        worker, "_run_recovery_scan", lambda: scans.append("scan"),
    )
    first = _Sender()
    second = _Sender()

    worker._on_worker_ready(sender=first)
    worker._on_worker_ready(sender=first)
    worker._on_worker_ready(sender=second)

    assert scans == ["scan", "scan", "scan"]
    assert len(first.timer.registrations) == 1
    assert second.timer.registrations == []


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


def test_dispatch_knowledge_reindex_uses_existing_worker(worker_env):
    fake_app = FakeCeleryApp()
    assert worker.dispatch_knowledge_reindex(celery_app=fake_app) is True
    assert fake_app.sent == [(worker.REINDEX_KNOWLEDGE_TASK, ())]


def test_dispatch_recoverable_sweeps_expired_before_dispatch(worker_env):
    """启动扫描先落掉超期 draft/待审批，再投递活动 Run。"""
    session = worker_env["session"]
    host = t_host(
        alias="web-stale", host_ip="203.0.113.60",
        host_port=22, ai_environment="lab",
    )
    session.add(host)
    session.commit()
    # 先建超期 draft 再建活动 Run：同 host 活动 Run 互斥。
    draft = worker_env["repo"].create_run(
        "admin", "admin", goal="stale draft",
        host_id=int(host.id), system_user_id=19, mode="ask",
    )
    row = session.get(t_ai_autonomous_run, draft["id"])
    row.updated_at = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=25)
    )
    session.commit()
    queued = worker_env["create_queued_run"]()

    fake_app = FakeCeleryApp()
    candidates = worker.dispatch_recoverable(
        session, celery_app=fake_app,
    )
    # 超期 draft 被清扫为 expired，绝不进入投递候选。
    assert [c["run_id"] for c in candidates] == [queued["id"]]
    session.expire_all()
    assert session.get(
        t_ai_autonomous_run, draft["id"],
    ).status == "expired"


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


def test_build_default_executor_wires_the_tool_calling_planner(monkeypatch):
    """S3：默认执行器必须携带规划器；不再出现 planner=None 的
    planner_unavailable 默认路径。"""
    from app.ai.autonomy import drive as drive_mod
    from app.ai.autonomy.planner import ToolCallingPlanner

    captured = {}

    class FakeDriver:
        def __init__(self, session, secret_key, **kwargs):
            captured["session"] = session
            captured.update(kwargs)

        def drive(self, run_id, claimed):
            captured["drive"] = (run_id, claimed)
            return drive_mod.RESULT_COMPLETED

    monkeypatch.setattr(drive_mod, "AutonomyDriver", FakeDriver)

    from app.app_factory import app as flask_app

    with flask_app.app_context():
        executor = worker.build_default_executor(object())
        result = executor("run-x", {"lease_token": "token-x"})

    assert result == drive_mod.RESULT_COMPLETED
    assert isinstance(captured["planner"], ToolCallingPlanner)
    assert captured.get("role") is None
    assert captured["drive"] == ("run-x", {"lease_token": "token-x"})
