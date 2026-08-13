# -*- coding: utf-8 -*-
"""M1/S2: AutonomyDriver 驱动循环契约测试（Issue #13）。

与租约/执行器测试相同的注入式 SQLite 内存引擎方案；图用
MemorySaver 验证暂停/恢复语义（真实 Redis 8 上的 ShallowRedisSaver
由 WP0 门槛脚本验证），心跳用替身，不碰真实线程与连接。
"""
import datetime
import json

import pytest
from cryptography.fernet import Fernet
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.autonomy import drive as drive_mod
from app.ai.autonomy.drive import AutonomyDriver
from app.ai.autonomy.executor import RunnerResult
from app.ai.autonomy.lease import RunLeaseService
from app.ai.autonomy.repository import AutonomyRepository
from app.ai.autonomy.state import StepStatus
from app.core import config
from app.core.db.database import (
    db,
    t_ai_autonomous_artifact,
    t_ai_autonomous_event,
    t_ai_autonomous_run,
    t_ai_autonomous_step,
    t_group,
    t_host,
)

SECRET_KEY = "unit-test-secret-key-for-autonomy-drive"
TTL = 300


def test_checkpoint_url_uses_db0_and_encodes_password(monkeypatch):
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_HOST", "192.0.2.10")
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_PORT", 6390)
    monkeypatch.setattr(
        config, "AI_AUTONOMY_REDIS_PASSWORD", "fake@pass:/#% ?",
    )
    assert drive_mod.autonomy_checkpoint_url() == (
        "redis://:fake%40pass%3A%2F%23%25%20%3F@192.0.2.10:6390/0"
    )


class FakePlatform:
    def __init__(self, owner, role, state=None):
        pass

    def validate_asset_ids(self, asset_ids):
        return True

    def resolve_system_user(self, sys_user_id):
        return {"id": int(sys_user_id), "alias": "readonly"}


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.result = RunnerResult(exit_code=0, output="ok")

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        return self.result


class FakeExecutor:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def execute_step(self, owner, role, run_id, step_id, **kwargs):
        self.calls.append((owner, role, run_id, step_id, kwargs))
        return dict(self.result)


class FakeHeartbeater:
    """替身心跳：记录 renew 调用；lost 可直接置位模拟租约被抢。"""

    instances = []

    def __init__(self, renew_fn, interval_seconds):
        self.renew_fn = renew_fn
        self.interval = interval_seconds
        self.started = False
        self.stopped = False
        self.lost = False
        FakeHeartbeater.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(
        "OGS_FERNET_KEYS", Fernet.generate_key().decode("ascii"),
    )
    FakeHeartbeater.instances = []
    engine = create_engine("sqlite:///:memory:")
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    db.metadata.create_all(
        engine,
        tables=[t_group.__table__, t_host.__table__,
                t_ai_autonomous_run.__table__,
                t_ai_autonomous_step.__table__,
                t_ai_autonomous_event.__table__,
                t_ai_autonomous_artifact.__table__],
    )

    platform_state = {"asset_ok": True}
    repo = AutonomyRepository(
        session, SECRET_KEY,
        platform_factory=lambda owner, role: FakePlatform(owner, role),
    )
    runner = FakeRunner()

    host_seq = {"n": 0}

    def create_queued_run(**kwargs):
        host_seq["n"] += 1
        n = host_seq["n"]
        host = t_host(
            alias="web-%02d" % n, host_ip="203.0.113.%d" % (120 + n),
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

    planner_calls = {"n": 0}

    def fake_planner(context):
        """第一轮提案一个探针，之后不再提案（驱动循环应收敛）。"""
        planner_calls["n"] += 1
        if planner_calls["n"] > 1:
            return []
        step = context["repo"].propose_probe(
            context["owner"], context["role"],
            context["run_id"], "system.load",
        )
        return [step["id"]]

    saver = MemorySaver()

    def make_driver(**overrides):
        kwargs = dict(
            planner=fake_planner,
            runner=runner,
            platform_factory=lambda owner, role: FakePlatform(owner, role),
            saver_factory=lambda: (saver, lambda: None),
            heartbeater_factory=FakeHeartbeater,
            lease_ttl=TTL,
            worker_id="driver-test-worker",
        )
        kwargs.update(overrides)
        return AutonomyDriver(session, SECRET_KEY, **kwargs)

    lease = RunLeaseService(session)

    def claim(run_id):
        claimed = lease.claim_run(run_id, "driver-test-worker", TTL)
        assert claimed is not None
        return claimed

    env = {
        "session": session,
        "repo": repo,
        "runner": runner,
        "lease": lease,
        "saver": saver,
        "create_queued_run": create_queued_run,
        "make_driver": make_driver,
        "claim": claim,
    }
    yield env
    session.close()
    engine.dispose()


def _run_row(env, run_id):
    return env["session"].query(t_ai_autonomous_run).filter_by(
        id=run_id,
    ).one()


def _step_row(env, step_id):
    return env["session"].query(t_ai_autonomous_step).filter_by(
        id=step_id,
    ).one()


def _events(env, run_id):
    return env["session"].query(t_ai_autonomous_event).filter_by(
        run_id=run_id,
    ).order_by(t_ai_autonomous_event.sequence.asc()).all()


def _pending_step(env, run_id):
    return env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run_id, status=StepStatus.WAITING_APPROVAL.value,
    ).first()


def _approve_and_drive(env, driver, run_id, operation="approve"):
    """按权威快照的 revision 决策后再次驱动（模拟决策接口投递）。"""
    run = _run_row(env, run_id)
    step = _pending_step(env, run_id)
    env["repo"].decide(
        "admin", "admin", run_id, step.id,
        operation=operation, expected_revision=int(run.revision),
    )
    claimed = env["claim"](run_id)
    return driver.drive(run_id, claimed)


# ---------------------------------------------------------------------------
# 首轮驱动：规划 → 策略 → 审批暂停
# ---------------------------------------------------------------------------

def test_first_drive_pauses_at_approval(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_PAUSED
    row = _run_row(env, run["id"])
    assert row.status == "waiting_approval"
    step = _pending_step(env, run["id"])
    assert step is not None
    # 暂停即释放租约：等人审批期间不占租约。
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    types = [e.event_type for e in _events(env, run["id"])]
    assert "steps_waiting_approval" in types


def test_checkpoint_does_not_store_the_run_goal(env):
    secret_goal = "diagnose password=checkpoint-secret"
    run = env["create_queued_run"](goal=secret_goal)
    driver = env["make_driver"]()

    assert driver.drive(run["id"], env["claim"](run["id"])) == (
        drive_mod.RESULT_PAUSED
    )

    checkpoint = env["saver"].get_tuple({
        "configurable": {
            "thread_id": drive_mod.THREAD_ID_PREFIX + run["id"],
        },
    })
    assert checkpoint is not None
    serialized = json.dumps(checkpoint.checkpoint, sort_keys=True)
    assert secret_goal not in serialized
    assert "checkpoint-secret" not in serialized


def test_second_drive_without_decision_stays_paused(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED

    # 决策未到又被投递（重复投递场景）：健康暂停的租约已释放，
    # 再认领直接失败，任务跳过，绝不产生副作用。
    assert env["lease"].claim_run(
        run["id"], "driver-test-worker", TTL,
    ) is None
    assert len(env["runner"].calls) == 0
    assert _run_row(env, run["id"]).status == "waiting_approval"


# ---------------------------------------------------------------------------
# 恢复：approve 执行 / reject 跳过
# ---------------------------------------------------------------------------

def test_resume_approved_executes_and_completes(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED

    result = _approve_and_drive(env, driver, run["id"], "approve")

    assert result == drive_mod.RESULT_COMPLETED
    assert len(env["runner"].calls) == 1
    row = _run_row(env, run["id"])
    assert row.status == "completed"
    assert row.lease_owner is None
    step = env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"], kind="action",
    ).one()
    assert step.status == "succeeded"
    types = [e.event_type for e in _events(env, run["id"])]
    assert "run_completed" in types


def test_resume_rejected_skips_execution(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED

    result = _approve_and_drive(env, driver, run["id"], "reject")

    assert result == drive_mod.RESULT_COMPLETED
    assert env["runner"].calls == []
    step = env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"], kind="action",
    ).one()
    assert step.status == "failed"
    assert step.note == "rejected"


def test_cancel_between_pause_and_resume_skips_execution(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED

    row = _run_row(env, run["id"])
    revision = int(row.revision)
    env["repo"].decide(
        "admin", "admin", run["id"], _pending_step(env, run["id"]).id,
        operation="approve", expected_revision=revision,
    )
    # 审批后、恢复前请求取消：已批准步骤也不得开跑。
    row = _run_row(env, run["id"])
    row.cancel_requested = True
    env["session"].commit()

    claimed = env["claim"](run["id"])
    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_CANCELLED
    assert env["runner"].calls == []
    assert _run_row(env, run["id"]).status == "cancelled"


# ---------------------------------------------------------------------------
# fail-closed：前置缺失绝不产生副作用
# ---------------------------------------------------------------------------

def test_no_planner_fails_closed(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"](planner=None)
    claimed = env["claim"](run["id"])

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_FAILED
    row = _run_row(env, run["id"])
    assert row.status == "failed"
    assert row.lease_owner is None
    types = [e.event_type for e in _events(env, run["id"])]
    assert "planner_unavailable" in types
    assert env["runner"].calls == []


def test_no_saver_fails_closed(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"](saver_factory=None)
    claimed = env["claim"](run["id"])

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_CHECKPOINT_UNAVAILABLE
    row = _run_row(env, run["id"])
    assert row.status == "needs_attention"
    assert row.lease_owner is None
    types = [e.event_type for e in _events(env, run["id"])]
    assert "checkpoint_unavailable" in types


def test_unknown_graph_version_fails_closed(env):
    run = env["create_queued_run"]()
    row = _run_row(env, run["id"])
    row.graph_version = "v9-does-not-exist"
    env["session"].commit()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_FAILED
    row = _run_row(env, run["id"])
    assert row.status == "failed"
    types = [e.event_type for e in _events(env, run["id"])]
    assert "unknown_graph_version" in types


def test_cancel_before_start_confirms_cancelled(env):
    run = env["create_queued_run"]()
    row = _run_row(env, run["id"])
    row.cancel_requested = True
    env["session"].commit()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_CANCELLED
    assert _run_row(env, run["id"]).status == "cancelled"
    assert env["runner"].calls == []


def test_cancel_request_cannot_overwrite_needs_attention(env):
    run = env["create_queued_run"]()
    row = _run_row(env, run["id"])
    row.status = "needs_attention"
    row.cancel_requested = True
    env["session"].commit()
    driver = env["make_driver"]()

    result = driver.drive(run["id"], {"revision": int(row.revision)})

    assert result == drive_mod.RESULT_NEEDS_ATTENTION
    assert _run_row(env, run["id"]).status == "needs_attention"
    assert env["runner"].calls == []


def test_finalize_preserves_unknown_outcome_over_cancel(env):
    run = env["create_queued_run"]()
    row = _run_row(env, run["id"])
    row.status = "needs_attention"
    row.cancel_requested = True
    env["session"].commit()
    driver = env["make_driver"]()

    result = driver._finalize(run["id"], {"decision": "cancelled"})

    assert result == drive_mod.RESULT_NEEDS_ATTENTION
    row = _run_row(env, run["id"])
    assert row.status == "needs_attention"
    assert row.completed_at is None


def test_terminal_run_is_skipped(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED
    result = _approve_and_drive(env, driver, run["id"], "approve")
    assert result == drive_mod.RESULT_COMPLETED

    # 终态 Run 再被投递：认领直接失败，任务跳过，不改状态。
    assert env["lease"].claim_run(
        run["id"], "driver-test-worker", TTL,
    ) is None
    assert _run_row(env, run["id"]).status == "completed"


# ---------------------------------------------------------------------------
# 租约丢失与预算
# ---------------------------------------------------------------------------

def test_lease_lost_aborts_without_side_effects(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])

    # 首个节点边界前租约就被抢走：立即中止。
    def factory(renew_fn, interval):
        heartbeater = FakeHeartbeater(renew_fn, interval)
        heartbeater.lost = True
        return heartbeater

    driver._heartbeater_factory = factory
    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_LEASE_LOST
    assert env["runner"].calls == []
    row = _run_row(env, run["id"])
    # 租约已不属于自己：不写状态、不释放，留给新持有者。
    assert row.status == "running"
    assert row.lease_owner == "driver-test-worker"


def test_loop_budget_exhausted_marks_failed(env):
    run = env["create_queued_run"](budget_payload={"max_loops": 1})
    # 规划器不提案：首轮 decide 即触发循环预算上限。
    driver = env["make_driver"](planner=lambda context: [])
    claimed = env["claim"](run["id"])

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_FAILED
    row = _run_row(env, run["id"])
    assert row.status == "failed"
    types = [e.event_type for e in _events(env, run["id"])]
    assert "budget_exhausted" in types


def test_duration_budget_exhausted_before_side_effects(env):
    run = env["create_queued_run"]()
    row = _run_row(env, run["id"])
    row.started_at = (
        drive_mod._utcnow() - datetime.timedelta(seconds=3601)
    )
    env["session"].commit()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_FAILED
    assert _run_row(env, run["id"]).status == "failed"
    assert env["runner"].calls == []
    types = [e.event_type for e in _events(env, run["id"])]
    assert "budget_exhausted" in types


def test_remaining_duration_caps_command_timeout(env):
    run = env["create_queued_run"]()
    row = _run_row(env, run["id"])
    row.started_at = (
        drive_mod._utcnow() - datetime.timedelta(seconds=3590)
    )
    env["session"].commit()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED

    result = _approve_and_drive(env, driver, run["id"], "approve")

    assert result == drive_mod.RESULT_COMPLETED
    assert len(env["runner"].calls) == 1
    timeout = env["runner"].calls[0]["timeout_seconds"]
    assert 1 <= timeout <= 10


def test_lease_loss_during_remote_execution_aborts_graph(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED
    row = _run_row(env, run["id"])
    step = _pending_step(env, run["id"])
    env["repo"].decide(
        "admin", "admin", run["id"], step.id,
        operation="approve", expected_revision=int(row.revision),
    )
    fake_executor = FakeExecutor({
        "step_status": "running",
        "run_status": "running",
        "revision": int(_run_row(env, run["id"]).revision),
        "termination": "lease_lost",
    })
    driver.executor = fake_executor
    claimed = env["claim"](run["id"])

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_LEASE_LOST
    assert len(fake_executor.calls) == 1
    call_kwargs = fake_executor.calls[0][4]
    assert call_kwargs["lease_owner"] == "driver-test-worker"
    assert callable(call_kwargs["control_probe"])
    assert _run_row(env, run["id"]).status == "running"


def test_heartbeat_started_and_stopped(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED

    assert len(FakeHeartbeater.instances) == 1
    heartbeater = FakeHeartbeater.instances[0]
    assert heartbeater.started is True
    assert heartbeater.stopped is True
    # 心跳间隔必须显著短于租约 TTL。
    assert heartbeater.interval == TTL // 4


# ---------------------------------------------------------------------------
# 恢复扫描与驱动循环的分工
# ---------------------------------------------------------------------------

def test_scan_excludes_healthy_paused_runs(env):
    run = env["create_queued_run"]()
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED

    # 健康暂停（租约已释放）不是恢复候选，由决策接口显式唤醒。
    candidates = env["lease"].scan_recoverable(
        now=datetime.datetime.utcnow() + datetime.timedelta(hours=1),
    )
    assert candidates == []

    # 决策后回到 queued，重新进入扫描候选。
    row = _run_row(env, run["id"])
    env["repo"].decide(
        "admin", "admin", run["id"], _pending_step(env, run["id"]).id,
        operation="approve", expected_revision=int(row.revision),
    )
    candidates = env["lease"].scan_recoverable()
    assert [c["run_id"] for c in candidates] == [run["id"]]
