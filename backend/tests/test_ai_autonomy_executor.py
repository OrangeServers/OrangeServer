# -*- coding: utf-8 -*-
"""M1/S2: AutonomyExecutor 执行器契约测试（Issue #13）。

覆盖：执行前 digest/权限/凭据/环境复核、写意图先落库、结果未知
fail-closed（outcome_unknown + needs_attention，绝不重放）、只读
传输失败可重试、预算耗尽停止、取消请求拒绝开跑。
runner 用替身注入，不触网。
"""
import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.autonomy.actions import (
    StructuredAction,
    build_action_digest,
)
from app.ai.autonomy.executor import (
    AutonomyExecutor,
    RunnerResult,
)
from app.ai.autonomy.repository import (
    AutonomyConflict,
    AutonomyPermissionError,
    AutonomyRepository,
)
from app.core.db.database import (
    db,
    t_ai_autonomous_event,
    t_ai_autonomous_run,
    t_ai_autonomous_step,
    t_group,
    t_host,
)

SECRET_KEY = "unit-test-secret-key-for-autonomy-executor"


class FakePlatform:
    def __init__(self, owner, role, state):
        self.state = state

    def validate_asset_ids(self, asset_ids):
        return self.state["asset_ok"]

    def resolve_system_user(self, sys_user_id):
        if not self.state["credential_ok"]:
            return None
        return {"id": int(sys_user_id), "alias": "readonly"}


class FakeRunner:
    """记录调用现场的可编程远程执行替身。"""

    def __init__(self, result=None):
        self.result = result or RunnerResult(exit_code=0, output="ok")
        self.calls = []
        self.on_call = None

    def __call__(self, command, **kwargs):
        snapshot = self.on_call() if self.on_call else None
        self.calls.append({"command": command, "kwargs": kwargs,
                           "at_call": snapshot})
        return self.result


@pytest.fixture()
def env():
    engine = create_engine("sqlite:///:memory:")
    db.metadata.create_all(
        engine,
        tables=[t_group.__table__, t_host.__table__,
                t_ai_autonomous_run.__table__,
                t_ai_autonomous_step.__table__,
                t_ai_autonomous_event.__table__],
    )
    session = sessionmaker(bind=engine)()
    platform_state = {"asset_ok": True, "credential_ok": True}
    repo = AutonomyRepository(
        session, SECRET_KEY,
        platform_factory=lambda owner, role: FakePlatform(
            owner, role, platform_state,
        ),
    )
    runner = FakeRunner()
    executor = AutonomyExecutor(
        session, SECRET_KEY, runner=runner,
        platform_factory=lambda owner, role: FakePlatform(
            owner, role, platform_state,
        ),
    )

    host_seq = {"n": 0}

    def create_queued_run(**kwargs):
        host_seq["n"] += 1
        n = host_seq["n"]
        host = t_host(
            alias="web-%02d" % n, host_ip="203.0.113.%d" % (70 + n),
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

    def approve_probe_step(run_id, probe_id="system.load"):
        step = repo.propose_probe("admin", "admin", run_id, probe_id)
        row = session.query(t_ai_autonomous_step).filter_by(
            id=step["id"],
        ).one()
        row.status = "approved"
        session.commit()
        return step["id"]

    def add_approved_shell_step(run_id, command="systemctl restart nginx"):
        run = session.query(t_ai_autonomous_run).filter_by(id=run_id).one()
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

    env = {
        "session": session,
        "repo": repo,
        "executor": executor,
        "runner": runner,
        "platform_state": platform_state,
        "create_queued_run": create_queued_run,
        "approve_probe_step": approve_probe_step,
        "add_approved_shell_step": add_approved_shell_step,
    }
    yield env
    session.close()
    engine.dispose()


def _step_row(env, step_id):
    return env["session"].query(t_ai_autonomous_step).filter_by(
        id=step_id,
    ).one()


def _run_row(env, run_id):
    return env["session"].query(t_ai_autonomous_run).filter_by(
        id=run_id,
    ).one()


def _events(env, run_id):
    return env["session"].query(t_ai_autonomous_event).filter_by(
        run_id=run_id,
    ).order_by(t_ai_autonomous_event.sequence.asc()).all()


# ---------------------------------------------------------------------------
# 成功路径与副作用前复核
# ---------------------------------------------------------------------------

def test_execute_approved_probe_success(env):
    run = env["create_queued_run"]()
    step_id = env["approve_probe_step"](run["id"])
    before_revision = run["revision"]

    result = env["executor"].execute_step(
        "admin", "admin", run["id"], step_id,
    )
    row = _step_row(env, step_id)
    assert result["step_status"] == "succeeded"
    assert row.status == "succeeded"
    assert "ok" in row.note
    # Run 由 queued 推进到 running，revision 递增。
    assert result["run_status"] == "running"
    assert result["revision"] > before_revision
    # 服务端探针命令来自服务端模板，超时受预算约束。
    call = env["runner"].calls[0]
    assert call["command"] == "uptime"
    assert call["kwargs"]["timeout_seconds"] <= 600
    types = [e.event_type for e in _events(env, run["id"])]
    assert "step_executed" in types


def test_execute_rejects_unapproved_or_cancelled(env):
    run = env["create_queued_run"]()
    proposed = env["repo"].propose_probe(
        "admin", "admin", run["id"], "system.load",
    )
    with pytest.raises(AutonomyConflict):
        env["executor"].execute_step(
            "admin", "admin", run["id"], proposed["id"],
        )

    step_id = env["approve_probe_step"](run["id"])
    run_row = _run_row(env, run["id"])
    run_row.cancel_requested = True
    env["session"].commit()
    with pytest.raises(AutonomyConflict):
        env["executor"].execute_step(
            "admin", "admin", run["id"], step_id,
        )
    assert env["runner"].calls == []


def test_digest_tamper_rejected_before_side_effects(env):
    run = env["create_queued_run"]()
    step_id = env["approve_probe_step"](run["id"])
    row = _step_row(env, step_id)
    tampered = json.loads(row.action_json)
    tampered["timeout_seconds"] = 9999
    row.action_json = json.dumps(tampered, sort_keys=True)
    env["session"].commit()

    with pytest.raises(AutonomyConflict):
        env["executor"].execute_step(
            "admin", "admin", run["id"], step_id,
        )
    assert env["runner"].calls == []


def test_permission_revoked_blocks_execution(env):
    run = env["create_queued_run"]()
    step_id = env["approve_probe_step"](run["id"])
    env["platform_state"]["asset_ok"] = False

    with pytest.raises(AutonomyPermissionError):
        env["executor"].execute_step(
            "admin", "admin", run["id"], step_id,
        )
    assert env["runner"].calls == []
    # Step 保持 approved，等权限恢复后可再执行。
    assert _step_row(env, step_id).status == "approved"


# ---------------------------------------------------------------------------
# 写意图 / outcome_unknown / 绝不重放
# ---------------------------------------------------------------------------

def test_write_intent_persisted_before_side_effect(env):
    run = env["create_queued_run"]()
    step_id = env["add_approved_shell_step"](run["id"])
    session = env["session"]

    def snapshot():
        return [e.event_type for e in _events(env, run["id"])]

    env["runner"].on_call = snapshot
    env["executor"].execute_step("admin", "admin", run["id"], step_id)

    call = env["runner"].calls[0]
    assert call["command"] == "systemctl restart nginx"
    # 远程副作用发生时，write_intent 必须已经落库。
    assert "write_intent" in call["at_call"]
    assert session.query(t_ai_autonomous_step).filter_by(
        id=step_id,
    ).one().status == "succeeded"


def test_write_uncertain_lands_outcome_unknown_and_never_replays(env):
    run = env["create_queued_run"]()
    step_id = env["add_approved_shell_step"](run["id"])
    env["runner"].result = RunnerResult(
        exit_code=None, output="", transport_error="ssh timeout after 60s",
    )

    result = env["executor"].execute_step(
        "admin", "admin", run["id"], step_id,
    )
    assert result["uncertain"] is True
    step = _step_row(env, step_id)
    run_row = _run_row(env, run["id"])
    assert step.status == "outcome_unknown"
    assert run_row.status == "needs_attention"
    types = [e.event_type for e in _events(env, run["id"])]
    assert "step_outcome_unknown" in types

    # 绝不自动重放：再次执行被拒（step 不再是 approved）。
    with pytest.raises(AutonomyConflict):
        env["executor"].execute_step(
            "admin", "admin", run["id"], step_id,
        )
    assert len(env["runner"].calls) == 1


def test_write_confirmed_failure_is_not_unknown(env):
    run = env["create_queued_run"]()
    step_id = env["add_approved_shell_step"](run["id"])
    env["runner"].result = RunnerResult(exit_code=1, output="denied")

    result = env["executor"].execute_step(
        "admin", "admin", run["id"], step_id,
    )
    assert result["uncertain"] is False
    assert _step_row(env, step_id).status == "failed"
    assert _run_row(env, run["id"]).status == "running"


def test_read_transport_error_is_retryable_not_unknown(env):
    run = env["create_queued_run"]()
    step_id = env["approve_probe_step"](run["id"])
    env["runner"].result = RunnerResult(
        exit_code=None, output="", transport_error="connection reset",
    )

    env["executor"].execute_step("admin", "admin", run["id"], step_id)
    step = _step_row(env, step_id)
    run_row = _run_row(env, run["id"])
    assert step.status == "failed"
    assert "transport_error_retryable" in step.note
    # 只读失败不进 needs_attention，也不产生 outcome_unknown。
    assert run_row.status == "running"
    types = [e.event_type for e in _events(env, run["id"])]
    assert "step_outcome_unknown" not in types


# ---------------------------------------------------------------------------
# 预算与输出上限
# ---------------------------------------------------------------------------

def test_action_budget_exhausted_stops_execution(env):
    run = env["create_queued_run"](budget_payload={"max_actions": 1})
    step_one = env["approve_probe_step"](run["id"])
    env["executor"].execute_step("admin", "admin", run["id"], step_one)

    # 提案门：S1 预算闸门在提案阶段就拦住第二个动作。
    with pytest.raises(AutonomyConflict):
        env["repo"].propose_probe(
            "admin", "admin", run["id"], "system.memory",
        )

    # 执行前兜底复核：绕过提案门直插已批准 Step，执行器仍拒绝。
    step_two = env["add_approved_shell_step"](run["id"])
    with pytest.raises(AutonomyConflict):
        env["executor"].execute_step(
            "admin", "admin", run["id"], step_two,
        )
    assert len(env["runner"].calls) == 1
    assert _step_row(env, step_two).status == "approved"


def test_step_note_capped_by_budget(env):
    run = env["create_queued_run"]()
    step_id = env["approve_probe_step"](run["id"])
    env["runner"].result = RunnerResult(
        exit_code=0, output="x" * 70000,
    )
    env["executor"].execute_step("admin", "admin", run["id"], step_id)
    assert len(_step_row(env, step_id).note) <= 255
