# -*- coding: utf-8 -*-
"""M1/S2: AutonomyDriver 驱动循环契约测试（Issue #13）。

与租约/执行器测试相同的注入式 SQLite 内存引擎方案；图用
MemorySaver 验证暂停/恢复语义（真实 Redis 8 上的 ShallowRedisSaver
由 WP0 门槛脚本验证），心跳用替身，不碰真实线程与连接。
"""
import datetime
import json
import threading

import pytest
from cryptography.fernet import Fernet
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.autonomy import drive as drive_mod
from app.ai.autonomy.drive import AutonomyDriver
from app.ai.autonomy.executor import RunnerResult
from app.ai.autonomy.lease import RunLeaseService
from app.ai.autonomy.policy import PolicyDecision
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


def test_checkpoint_saver_uses_bounded_socket_timeouts(monkeypatch):
    monkeypatch.setattr(config, "AI_AUTONOMY_ENABLED", True)
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_HOST", "192.0.2.10")
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_PORT", 6390)
    monkeypatch.setattr(config, "AI_AUTONOMY_REDIS_PASSWORD", "fake-pass")
    monkeypatch.setattr(config, "REDIS_CONF", {
        "socket_connect_timeout": 1.25,
        "socket_timeout": 2.5,
    })
    observed = {"setup": False, "closed": False}

    class FakeSaver:
        def setup(self):
            observed["setup"] = True

    class FakeManager:
        def __enter__(self):
            return FakeSaver()

        def __exit__(self, *_args):
            observed["closed"] = True

    def fake_from_conn_string(url, *, connection_args=None):
        observed["url"] = url
        observed["connection_args"] = connection_args
        return FakeManager()

    from langgraph.checkpoint.redis import ShallowRedisSaver

    monkeypatch.setattr(
        ShallowRedisSaver, "from_conn_string",
        staticmethod(fake_from_conn_string),
    )
    factory = drive_mod.make_autonomy_saver_factory()
    assert factory is not None

    _saver, close = factory()

    assert observed["url"].endswith("/0")
    assert observed["connection_args"] == {
        "socket_connect_timeout": 1.25,
        "socket_timeout": 2.5,
        "retry_on_timeout": False,
    }
    assert observed["setup"] is True
    close()
    assert observed["closed"] is True


def test_heartbeat_session_factory_works_in_background_thread():
    """Celery creates the factory in-app, then heartbeat calls it out-of-app."""
    from app.app_factory import app

    with app.app_context():
        factory = drive_mod.make_autonomy_heartbeat_session_factory()

    observed = {}

    def open_session():
        try:
            observed["session"] = factory()
        except Exception as exc:  # pragma: no branch - regression signal
            observed["error"] = exc

    thread = threading.Thread(target=open_session)
    thread.start()
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert "error" not in observed
    session = observed["session"]
    try:
        assert session.get_bind() is not None
    finally:
        session.close()


def test_checkpoint_writes_require_the_exact_current_claim(env):
    run = env["create_queued_run"]()
    claim_a = env["claim"](run["id"])
    config_payload = {
        "configurable": {
            "thread_id": drive_mod.THREAD_ID_PREFIX + run["id"],
        },
    }
    calls = []

    class RecordingSaver(MemorySaver):
        def put(self, *args, **kwargs):
            calls.append(("put", args, kwargs))
            return "put-ok"

        def put_writes(self, *args, **kwargs):
            calls.append(("put_writes", args, kwargs))
            return "writes-ok"

    driver_a = env["make_driver"]()
    driver_a._active_lease_token = claim_a["lease_token"]
    saver_a = driver_a._fence_checkpoint_saver(
        run["id"], RecordingSaver(),
    )

    row = _run_row(env, run["id"])
    row.lease_owner = "driver-test-worker-b"
    row.lease_token = "claim-token-b"
    env["session"].commit()

    with pytest.raises(drive_mod.DriveAbort, match="claim fence lost"):
        saver_a.put(config_payload)
    with pytest.raises(drive_mod.DriveAbort, match="claim fence lost"):
        saver_a.put_writes(config_payload, [("channel", "value")], "task")
    assert calls == []

    driver_b = env["make_driver"](worker_id="driver-test-worker-b")
    driver_b._active_lease_token = "claim-token-b"
    saver_b = driver_b._fence_checkpoint_saver(
        run["id"], RecordingSaver(),
    )

    assert saver_b.put(config_payload) == "put-ok"
    assert saver_b.put_writes(
        config_payload, [("channel", "value")], "task",
    ) == "writes-ok"
    assert [call[0] for call in calls] == ["put", "put_writes"]
    assert calls[0][1][0] is config_payload
    assert calls[1][1][0] is config_payload


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
def env(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "OGS_FERNET_KEYS", Fernet.generate_key().decode("ascii"),
    )
    FakeHeartbeater.instances = []
    db_path = (tmp_path / "autonomy-drive.db").as_posix()
    engine = create_engine(
        "sqlite:///%s" % db_path,
        connect_args={"check_same_thread": False},
    )
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
        graph_version = kwargs.pop("graph_version", None)
        legacy_mode = kwargs.get("mode") if kwargs.get("mode") in {
            "read_only", "assisted", "lab_autonomous",
        } else None
        if legacy_mode is not None:
            kwargs["mode"] = "ask"
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
            mode="ask",
        )
        payload.update(kwargs)
        run = repo.create_run("admin", "admin", **payload)
        if graph_version is not None or legacy_mode is not None:
            row = session.get(t_ai_autonomous_run, run["id"])
            if graph_version is not None:
                row.graph_version = graph_version
            if legacy_mode is not None:
                row.mode = legacy_mode
            session.commit()
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
            heartbeat_session_factory=session_factory,
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

def test_v2_allowed_probe_executes_without_per_step_approval(env):
    run = env["create_queued_run"](mode="ask")
    assert _run_row(env, run["id"]).graph_version == "v2"
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_COMPLETED
    assert len(env["runner"].calls) == 1
    row = _run_row(env, run["id"])
    assert row.status == "completed"
    step = env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"], kind="action",
    ).one()
    assert step.status == "succeeded"
    assert _pending_step(env, run["id"]) is None
    event_types = [event.event_type for event in _events(env, run["id"])]
    assert "step_policy_decided" in event_types
    assert "steps_waiting_approval" not in event_types


def test_v2_ask_pauses_without_calling_the_runner(env, monkeypatch):
    monkeypatch.setattr(
        drive_mod, "classify_action",
        lambda *_args: (PolicyDecision.ASK, "test boundary needs approval"),
    )
    run = env["create_queued_run"](mode="ai_review")
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_PAUSED
    assert env["runner"].calls == []
    assert _run_row(env, run["id"]).status == "waiting_approval"
    assert _pending_step(env, run["id"]) is not None


def test_v2_tampered_action_is_denied_without_calling_the_runner(env):
    planner_calls = {"count": 0}

    def tampering_planner(context):
        planner_calls["count"] += 1
        if planner_calls["count"] > 1:
            return []
        step = context["repo"].propose_probe(
            context["owner"], context["role"],
            context["run_id"], "system.load",
        )
        row = _step_row(env, step["id"])
        snapshot = json.loads(row.action_json)
        snapshot["action_version"] += 1
        row.action_json = json.dumps(snapshot, sort_keys=True)
        env["session"].commit()
        return [step["id"]]

    run = env["create_queued_run"](mode="ask")
    driver = env["make_driver"](planner=tampering_planner)
    claimed = env["claim"](run["id"])

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_COMPLETED
    assert env["runner"].calls == []
    step = env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"], kind="action",
    ).one()
    assert step.status == "failed"
    assert step.note == "malformed action snapshot"


def test_first_drive_pauses_at_approval(env):
    run = env["create_queued_run"](mode="assisted", graph_version="v1")
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
    assert row.lease_token is None
    assert row.lease_expires_at is None
    types = [e.event_type for e in _events(env, run["id"])]
    assert "steps_waiting_approval" in types


def test_checkpoint_does_not_store_the_run_goal(env):
    secret_goal = "diagnose password=checkpoint-secret"
    run = env["create_queued_run"](
        goal=secret_goal, mode="assisted", graph_version="v1",
    )
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
    run = env["create_queued_run"](mode="assisted", graph_version="v1")
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
    run = env["create_queued_run"](mode="assisted", graph_version="v1")
    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED

    result = _approve_and_drive(env, driver, run["id"], "approve")

    assert result == drive_mod.RESULT_COMPLETED
    assert len(env["runner"].calls) == 1
    row = _run_row(env, run["id"])
    assert row.status == "completed"
    assert row.lease_owner is None
    assert row.lease_token is None
    step = env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"], kind="action",
    ).one()
    assert step.status == "succeeded"
    types = [e.event_type for e in _events(env, run["id"])]
    assert "run_completed" in types


def test_resume_rejected_skips_execution(env):
    run = env["create_queued_run"](mode="assisted", graph_version="v1")
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
    run = env["create_queued_run"](mode="assisted", graph_version="v1")
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
    assert row.lease_token is None
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


def test_saver_factory_failure_persists_attention_and_releases_lease(env):
    run = env["create_queued_run"]()

    def broken_factory():
        raise RuntimeError("redis unavailable")

    driver = env["make_driver"](saver_factory=broken_factory)
    result = driver.drive(run["id"], env["claim"](run["id"]))

    assert result == drive_mod.RESULT_CHECKPOINT_UNAVAILABLE
    row = _run_row(env, run["id"])
    assert row.status == "needs_attention"
    assert row.lease_owner is None
    assert row.lease_token is None
    assert [event.event_type for event in _events(env, run["id"])].count(
        "checkpoint_unavailable"
    ) == 1


def test_saver_access_failure_persists_attention_and_releases_lease(env):
    run = env["create_queued_run"]()

    class BrokenSaver(MemorySaver):
        def get_tuple(self, _config):
            from redis.exceptions import ConnectionError

            raise ConnectionError("checkpoint backend unavailable")

    driver = env["make_driver"](
        saver_factory=lambda: (BrokenSaver(), lambda: None),
    )
    result = driver.drive(run["id"], env["claim"](run["id"]))

    assert result == drive_mod.RESULT_CHECKPOINT_UNAVAILABLE
    row = _run_row(env, run["id"])
    assert row.status == "needs_attention"
    assert row.lease_owner is None
    assert row.lease_token is None
    assert [event.event_type for event in _events(env, run["id"])].count(
        "checkpoint_unavailable"
    ) == 1


def test_non_checkpoint_runtime_error_is_not_misclassified(env):
    run = env["create_queued_run"]()

    def broken_planner(_context):
        raise RuntimeError("redis policy issue")

    driver = env["make_driver"](planner=broken_planner)
    result = driver.drive(run["id"], env["claim"](run["id"]))

    assert result == drive_mod.RESULT_NEEDS_ATTENTION
    assert _run_row(env, run["id"]).status == "running"
    assert "checkpoint_unavailable" not in {
        event.event_type for event in _events(env, run["id"])
    }


def test_checkpoint_failure_cannot_overwrite_new_claim(env):
    run = env["create_queued_run"]()
    claimed = env["claim"](run["id"])
    row = _run_row(env, run["id"])
    row.lease_token = "new-claim-token"
    env["session"].commit()

    def broken_factory():
        raise RuntimeError("checkpoint setup failed")

    driver = env["make_driver"](saver_factory=broken_factory)
    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_LEASE_LOST
    row = _run_row(env, run["id"])
    assert row.status == "running"
    assert row.lease_token == "new-claim-token"
    assert "checkpoint_unavailable" not in {
        event.event_type for event in _events(env, run["id"])
    }


def test_finalize_cannot_overwrite_same_identity_new_claim(env):
    run = env["create_queued_run"]()
    claimed = env["claim"](run["id"])

    def takeover_then_finish(_context):
        row = _run_row(env, run["id"])
        row.lease_token = "new-claim-token"
        env["session"].commit()
        return []

    driver = env["make_driver"](planner=takeover_then_finish)
    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_LEASE_LOST
    row = _run_row(env, run["id"])
    assert row.status == "running"
    assert row.lease_token == "new-claim-token"
    assert "run_completed" not in {
        event.event_type for event in _events(env, run["id"])
    }


def test_failure_path_cannot_overwrite_same_identity_new_claim(env):
    run = env["create_queued_run"]()
    claimed = env["claim"](run["id"])

    def takeover_then_fail(_context):
        row = _run_row(env, run["id"])
        row.lease_token = "new-claim-token"
        env["session"].commit()
        raise drive_mod.PlannerUnavailable("planner unavailable")

    driver = env["make_driver"](planner=takeover_then_fail)
    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_LEASE_LOST
    row = _run_row(env, run["id"])
    assert row.status == "running"
    assert row.lease_token == "new-claim-token"
    assert "planner_unavailable" not in {
        event.event_type for event in _events(env, run["id"])
    }


def test_planner_proposal_cannot_write_after_same_identity_takeover(env):
    run = env["create_queued_run"]()
    claimed = env["claim"](run["id"])

    def takeover_before_proposal(context):
        row = _run_row(env, run["id"])
        row.lease_token = "new-claim-token"
        env["session"].commit()
        step = context["repo"].propose_probe(
            context["owner"], context["role"],
            context["run_id"], "system.load",
        )
        return [step["id"]]

    driver = env["make_driver"](planner=takeover_before_proposal)
    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_LEASE_LOST
    assert env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"], kind="action",
    ).count() == 0
    assert _run_row(env, run["id"]).lease_token == "new-claim-token"
    assert "step_proposed" not in {
        event.event_type for event in _events(env, run["id"])
    }


def test_drive_does_not_checkpoint_after_exact_claim_takeover(env):
    run = env["create_queued_run"]()
    claimed = env["claim"](run["id"])
    takeover = {"done": False}
    stale_writes = []

    class RecordingSaver(MemorySaver):
        def put(self, *args, **kwargs):
            if takeover["done"]:
                stale_writes.append("put")
            return super().put(*args, **kwargs)

        def put_writes(self, *args, **kwargs):
            if takeover["done"]:
                stale_writes.append("put_writes")
            return super().put_writes(*args, **kwargs)

    def takeover_after_plan(_context):
        row = _run_row(env, run["id"])
        row.lease_token = "new-claim-token"
        env["session"].commit()
        takeover["done"] = True
        return []

    driver = env["make_driver"](
        planner=takeover_after_plan,
        saver_factory=lambda: (RecordingSaver(), lambda: None),
    )

    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_LEASE_LOST
    assert stale_writes == []
    assert _run_row(env, run["id"]).lease_token == "new-claim-token"
    assert "checkpoint_unavailable" not in {
        event.event_type for event in _events(env, run["id"])
    }


def test_policy_cannot_persist_after_same_identity_takeover(env):
    run = env["create_queued_run"]()
    claimed = env["claim"](run["id"])

    def propose_then_takeover(context):
        step = context["repo"].propose_probe(
            context["owner"], context["role"],
            context["run_id"], "system.load",
        )
        row = _run_row(env, run["id"])
        row.lease_token = "new-claim-token"
        env["session"].commit()
        return [step["id"]]

    driver = env["make_driver"](planner=propose_then_takeover)
    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_LEASE_LOST
    step = env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"], kind="action",
    ).one()
    assert step.status == StepStatus.PROPOSED.value
    assert _run_row(env, run["id"]).lease_token == "new-claim-token"
    event_types = {
        event.event_type for event in _events(env, run["id"])
    }
    assert "step_proposed" in event_types
    assert "step_policy_decided" not in event_types


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
    row = _run_row(env, run["id"])
    assert row.status == "cancelled"
    assert row.lease_token is None
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
    claimed = env["claim"](run["id"])
    row = _run_row(env, run["id"])
    row.status = "needs_attention"
    row.cancel_requested = True
    env["session"].commit()
    driver = env["make_driver"]()
    driver._active_lease_token = claimed["lease_token"]

    result = driver._finalize(run["id"], {"decision": "cancelled"})

    assert result == drive_mod.RESULT_NEEDS_ATTENTION
    row = _run_row(env, run["id"])
    assert row.status == "needs_attention"
    assert row.completed_at is None


def test_terminal_run_is_skipped(env):
    run = env["create_queued_run"](mode="assisted", graph_version="v1")
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
    run = env["create_queued_run"](mode="assisted", graph_version="v1")
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
    run = env["create_queued_run"](mode="assisted", graph_version="v1")
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
    run = env["create_queued_run"](mode="assisted", graph_version="v1")
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
    run = env["create_queued_run"](mode="assisted", graph_version="v1")
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
