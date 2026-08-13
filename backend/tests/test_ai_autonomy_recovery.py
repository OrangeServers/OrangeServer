# -*- coding: utf-8 -*-
"""M1/S2: 恢复语义契约测试（Issue #13）。

覆盖 worker 被强杀的五类现场：
- 只读动作执行中被杀 → 自动重试（回到 approved 重跑）；
- 写动作确认后、执行前被杀 → 从 execute 边界继续执行；
- 写动作执行中/后被杀、结果未落库 → outcome_unknown +
  needs_attention，绝不自动重放；
- Redis checkpoint 丢失 → 只从 MySQL 已确认的安全边界重建；
- recovering 再被接管保持 recovering，不无限自套娃。

"被强杀现场"用直接改库模拟（崩溃后的数据库残留本来就不经过
状态机）；驱动循环与恢复层走真实代码。
"""
import datetime
import json
import uuid

import pytest
from cryptography.fernet import Fernet
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.autonomy import drive as drive_mod
from app.ai.autonomy.actions import StructuredAction, build_action_digest
from app.ai.autonomy.drive import AutonomyDriver
from app.ai.autonomy.executor import RunnerResult
from app.ai.autonomy.lease import RunLeaseService
from app.ai.autonomy.repository import AutonomyRepository
from app.core.db.database import (
    db,
    t_ai_autonomous_artifact,
    t_ai_autonomous_event,
    t_ai_autonomous_run,
    t_ai_autonomous_step,
    t_group,
    t_host,
)

SECRET_KEY = "unit-test-secret-key-for-autonomy-recovery"
TTL = 300


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


class FakeHeartbeater:
    def __init__(self, renew_fn, interval_seconds):
        self.renew_fn = renew_fn
        self.interval = interval_seconds
        self.lost = False

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(
        "OGS_FERNET_KEYS", Fernet.generate_key().decode("ascii"),
    )
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
            alias="web-%02d" % n, host_ip="203.0.113.%d" % (140 + n),
            host_port=22, ai_environment="lab",
        )
        session.add(host)
        session.commit()
        payload = dict(
            goal="recover safely",
            host_id=int(host.id),
            system_user_id=21,
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

    def make_driver(saver=None, **overrides):
        saver = saver if saver is not None else MemorySaver()
        kwargs = dict(
            planner=fake_planner,
            runner=runner,
            platform_factory=lambda owner, role: FakePlatform(owner, role),
            saver_factory=lambda: (saver, lambda: None),
            heartbeater_factory=FakeHeartbeater,
            lease_ttl=TTL,
            worker_id="recovery-test-worker",
        )
        kwargs.update(overrides)
        return AutonomyDriver(session, SECRET_KEY, **kwargs)

    lease = RunLeaseService(session)

    def propose_approved_probe(run_id):
        """提案一个探针并推到 approved（合法转换逐步走）。"""
        step = repo.propose_probe(
            "admin", "admin", run_id, "system.load",
        )
        row = session.query(t_ai_autonomous_step).filter_by(
            id=step["id"],
        ).one()
        row.status = "waiting_approval"
        row.status = "approved"
        session.commit()
        return step["id"]

    def add_approved_shell_step(run_id, command="systemctl restart nginx"):
        run = session.query(t_ai_autonomous_run).filter_by(
            id=run_id,
        ).one()
        step_id = uuid.uuid4().hex
        action = StructuredAction(
            kind="shell",
            target_id=int(run.host_id),
            system_user_id=int(run.system_user_id),
            parameters={"command": command},
            timeout_seconds=30,
            step_id=step_id,
        )
        step = t_ai_autonomous_step(
            id=step_id, run_id=run_id, kind="action", status="approved",
            seq=90, summary="shell restart",
            action_json=json.dumps(
                action.to_canonical_dict(), sort_keys=True,
            ),
            action_digest=build_action_digest(action, SECRET_KEY),
            note="",
        )
        session.add(step)
        session.commit()
        return step_id

    def simulate_kill(run_id, *, lease_expired=True):
        """模拟 worker 被强杀：Run 落 recovering + 租约过期。"""
        row = session.query(t_ai_autonomous_run).filter_by(
            id=run_id,
        ).one()
        row.status = "recovering"
        if lease_expired:
            row.lease_owner = "dead-worker"
            row.lease_expires_at = (
                datetime.datetime.utcnow() - datetime.timedelta(seconds=10)
            )
        session.commit()

    def claim(run_id):
        claimed = lease.claim_run(run_id, "recovery-test-worker", TTL)
        assert claimed is not None
        return claimed

    env = {
        "session": session,
        "repo": repo,
        "runner": runner,
        "lease": lease,
        "create_queued_run": create_queued_run,
        "make_driver": make_driver,
        "propose_approved_probe": propose_approved_probe,
        "add_approved_shell_step": add_approved_shell_step,
        "simulate_kill": simulate_kill,
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


def _event_types(env, run_id):
    return [e.event_type for e in _events(env, run_id)]


def _append_write_intent(env, run_id, step_id):
    """模拟执行器在副作用前落库的写意图。"""
    run = _run_row(env, run_id)
    seq = int(run.latest_event_seq or 0) + 1
    env["session"].add(t_ai_autonomous_event(
        run_id=run_id, sequence=seq, event_type="write_intent",
        payload_json=json.dumps({"step_id": step_id, "kind": "shell"}),
    ))
    run.latest_event_seq = seq
    env["session"].commit()


# ---------------------------------------------------------------------------
# 只读动作执行中被杀：自动重试
# ---------------------------------------------------------------------------

def test_kill_during_read_only_step_retries_automatically(env):
    run = env["create_queued_run"]()
    step_id = env["propose_approved_probe"](run["id"])
    # 崩溃残留：只读 Step 停在 running，无写意图。
    step = _step_row(env, step_id)
    step.status = "running"
    env["session"].commit()
    env["simulate_kill"](run["id"])

    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_COMPLETED
    assert len(env["runner"].calls) == 1
    assert _step_row(env, step_id).status == "succeeded"
    row = _run_row(env, run["id"])
    assert row.status == "completed"
    types = _event_types(env, run["id"])
    assert "recovery_readonly_retry" in types
    assert "recovery_write_outcome_unknown" not in types


# ---------------------------------------------------------------------------
# 写动作确认后、执行前被杀：继续执行
# ---------------------------------------------------------------------------

def test_kill_before_write_execution_continues_from_boundary(env):
    run = env["create_queued_run"]()
    step_id = env["add_approved_shell_step"](run["id"])
    # 崩溃残留：已批准未执行（无 write_intent），无 checkpoint。
    env["simulate_kill"](run["id"])

    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_COMPLETED
    assert len(env["runner"].calls) == 1
    assert env["runner"].calls[0]["command"] == "systemctl restart nginx"
    assert _step_row(env, step_id).status == "succeeded"
    assert _run_row(env, run["id"]).status == "completed"
    assert "recovery_boundary_rebuild" in _event_types(env, run["id"])


# ---------------------------------------------------------------------------
# 写动作执行中/后被杀、结果未落库：outcome_unknown，绝不重放
# ---------------------------------------------------------------------------

def test_kill_during_write_lands_outcome_unknown_never_replays(env):
    run = env["create_queued_run"]()
    step_id = env["add_approved_shell_step"](run["id"])
    # 崩溃残留：写意图已落库、Step 停在 running（结果未知）。
    step = _step_row(env, step_id)
    step.status = "running"
    env["session"].commit()
    _append_write_intent(env, run["id"], step_id)
    env["simulate_kill"](run["id"])

    driver = env["make_driver"]()
    claimed = env["claim"](run["id"])
    result = driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_NEEDS_ATTENTION
    assert env["runner"].calls == []
    assert _step_row(env, step_id).status == "outcome_unknown"
    row = _run_row(env, run["id"])
    assert row.status == "needs_attention"
    assert row.lease_owner is None
    types = _event_types(env, run["id"])
    assert "recovery_write_outcome_unknown" in types

    # 绝不自动重放：重复投递直接拒绝，不再产生任何远程调用。
    again = driver.drive(run["id"], {"revision": int(row.revision)})
    assert again == drive_mod.RESULT_NEEDS_ATTENTION
    assert env["runner"].calls == []
    # needs_attention 也不再是可认领状态，扫描不会唤醒它。
    assert env["lease"].claim_run(
        run["id"], "recovery-test-worker", TTL,
    ) is None


# ---------------------------------------------------------------------------
# checkpoint 丢失：只从 MySQL 已确认的安全边界重建
# ---------------------------------------------------------------------------

def test_checkpoint_lost_with_waiting_step_stays_paused(env):
    run = env["create_queued_run"]()
    saver = MemorySaver()
    driver = env["make_driver"](saver=saver)
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED

    # checkpoint 被删（Redis 数据丢失），决策接口又把 Run 推回 queued。
    row = _run_row(env, run["id"])
    row.status = "queued"
    env["session"].commit()

    fresh_driver = env["make_driver"](saver=MemorySaver())
    claimed = env["claim"](run["id"])
    result = fresh_driver.drive(run["id"], claimed)

    # 有待审 Step：绝不越过审批自动执行，回到等人状态。
    assert result == drive_mod.RESULT_PAUSED
    assert env["runner"].calls == []
    row = _run_row(env, run["id"])
    assert row.status == "waiting_approval"
    assert row.lease_owner is None


def test_checkpoint_lost_with_approved_step_rebuilds_execute(env):
    run = env["create_queued_run"]()
    saver = MemorySaver()
    driver = env["make_driver"](saver=saver)
    claimed = env["claim"](run["id"])
    assert driver.drive(run["id"], claimed) == drive_mod.RESULT_PAUSED

    # 人工批准后 checkpoint 丢失：只能从 MySQL 的已批准边界重建。
    row = _run_row(env, run["id"])
    step = env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"], status="waiting_approval",
    ).one()
    env["repo"].decide(
        "admin", "admin", run["id"], step.id,
        operation="approve", expected_revision=int(row.revision),
    )

    fresh_driver = env["make_driver"](saver=MemorySaver())
    claimed = env["claim"](run["id"])
    result = fresh_driver.drive(run["id"], claimed)

    assert result == drive_mod.RESULT_COMPLETED
    assert len(env["runner"].calls) == 1
    assert _step_row(env, step.id).status == "succeeded"
    assert _run_row(env, run["id"]).status == "completed"
    # 绝不回到 plan 重新提案：规划器只在边界收尾时被再问一次。
    assert "recovery_boundary_rebuild" in _event_types(env, run["id"])


# ---------------------------------------------------------------------------
# recovering 接管契约
# ---------------------------------------------------------------------------

def test_recovering_claim_preserves_status(env):
    run = env["create_queued_run"]()
    env["simulate_kill"](run["id"])

    claimed = env["claim"](run["id"])
    assert claimed is not None
    # recovering 再被接管时保持 recovering，避免恢复中无限自套娃。
    assert _run_row(env, run["id"]).status == "recovering"


def test_recovering_scan_excludes_healthy_paused_and_attention(env):
    run = env["create_queued_run"]()
    env["simulate_kill"](run["id"])

    candidates = env["lease"].scan_recoverable()
    assert [c["run_id"] for c in candidates] == [run["id"]]

    # needs_attention 落定后不再是恢复候选：只有人工能唤醒。
    row = _run_row(env, run["id"])
    row.status = "needs_attention"
    row.lease_owner = None
    row.lease_expires_at = None
    env["session"].commit()
    assert env["lease"].scan_recoverable() == []
