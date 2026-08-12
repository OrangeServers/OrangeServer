# -*- coding: utf-8 -*-
"""M1/S1: AutonomyRepository 持久层与原子审批决策契约测试（Issue #11）。

conftest 会把 db.session 的方法 patch 成 no-op，因此这里使用独立的
SQLite 内存引擎 + 注入式 session，绕开全局 patch 验证真实落库行为。
"""
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.ai.autonomy.actions import (
    StructuredAction,
    build_action_digest,
)
from app.ai.autonomy.repository import (
    AutonomyConflict,
    AutonomyNotFound,
    AutonomyPermissionError,
    AutonomyRepository,
    AutonomyValidationError,
    sanitize_payload,
)
from app.ai.autonomy.state import AutonomyStateError
from app.core.db.database import (
    db,
    t_ai_autonomous_artifact,
    t_ai_autonomous_event,
    t_ai_autonomous_run,
    t_ai_autonomous_step,
    t_group,
    t_host,
)

SECRET_KEY = "unit-test-secret-key-for-autonomy"


class FakePlatform:
    """可翻转授权结果的 PlatformQueryService 替身。"""

    def __init__(self, owner, role, state):
        self.owner = owner
        self.role = role
        self.state = state
        state["calls"].append((owner, role))

    def validate_asset_ids(self, asset_ids):
        return self.state["asset_ok"]

    def resolve_system_user(self, sys_user_id):
        if not self.state["credential_ok"]:
            return None
        return {"id": int(sys_user_id), "alias": "readonly"}


@pytest.fixture()
def repo_env(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    db.metadata.create_all(
        engine,
        tables=[
            t_group.__table__,
            t_host.__table__,
            t_ai_autonomous_run.__table__,
            t_ai_autonomous_step.__table__,
            t_ai_autonomous_event.__table__,
            t_ai_autonomous_artifact.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()

    platform_state = {
        "asset_ok": True,
        "credential_ok": True,
        "calls": [],
    }

    def factory(owner, role):
        return FakePlatform(owner, role, platform_state)

    repo = AutonomyRepository(
        session, SECRET_KEY, platform_factory=factory,
    )

    host = t_host(
        alias="web-01", host_ip="203.0.113.10", host_port=22,
        ai_environment="production",
    )
    session.add(host)
    session.commit()

    env = {
        "repo": repo,
        "session": session,
        "platform_state": platform_state,
        "host_id": int(host.id),
    }

    def create_started_run(**kwargs):
        payload = dict(
            goal="diagnose latency",
            host_id=env["host_id"],
            system_user_id=19,
            mode="assisted",
        )
        payload.update(kwargs)
        run = repo.create_run("admin", "admin", **payload)
        return repo.start_run("admin", "admin", run["id"])

    env["create_started_run"] = create_started_run
    yield env
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 创建边界：参数校验 + 资产/凭据授权 + 环境分级
# ---------------------------------------------------------------------------

def test_create_run_defaults_and_event_trail(repo_env):
    repo = repo_env["repo"]
    run = repo.create_run(
        "admin", "admin",
        goal="diagnose latency",
        host_id=repo_env["host_id"],
        system_user_id=19,
        mode="assisted",
    )
    assert run["status"] == "draft"
    assert run["revision"] == 0
    assert run["host_alias"] == "web-01"
    assert run["system_user_alias"] == "readonly"
    assert run["budget"]["max_actions"] == 30

    events = repo_env["session"].query(t_ai_autonomous_event).all()
    assert [event.event_type for event in events] == ["run_created"]
    assert events[0].sequence == 1
    # 凭据内容永不进入事件 payload，只允许 ID 引用。
    payload = json.loads(events[0].payload_json)
    assert payload["system_user_id"] == 19
    assert "password" not in payload
    assert "credential" not in json.dumps(payload)


def test_create_run_inserts_run_before_first_event_under_fk():
    """强制外键下 create_run 必须先落 run 再落首个事件。

    完成门（真实 MySQL）发现：ORM 无 relationship 时 flush 不保证
    父子插入顺序，SQLite 默认不强制外键所以历史测试全绿，真实
    MySQL 会拒绝先插入的事件行。这里用 PRAGMA 强制外键复现。
    """
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enforce_fk(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    db.metadata.create_all(
        engine,
        tables=[
            t_group.__table__,
            t_host.__table__,
            t_ai_autonomous_run.__table__,
            t_ai_autonomous_step.__table__,
            t_ai_autonomous_event.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()
    try:
        platform_state = {"asset_ok": True, "credential_ok": True,
                          "calls": []}
        repo = AutonomyRepository(
            session, SECRET_KEY,
            platform_factory=lambda o, r: FakePlatform(o, r, platform_state),
        )
        host = t_host(
            alias="web-fk", host_ip="203.0.113.99", host_port=22,
            ai_environment="lab",
        )
        session.add(host)
        session.commit()

        run = repo.create_run(
            "admin", "admin",
            goal="fk ordering regression",
            host_id=int(host.id),
            system_user_id=19,
            mode="assisted",
        )
        assert run["status"] == "draft"
        rows = session.query(t_ai_autonomous_event).filter_by(
            run_id=run["id"],
        ).all()
        assert [row.event_type for row in rows] == ["run_created"]
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize("overrides", [
    {"goal": ""},
    {"goal": "x" * 513},
    {"mode": "full_auto"},
    {"host_id": 0},
    {"host_id": "abc"},
    {"system_user_id": -1},
    {"budget_payload": {"max_actions": 999}},
])
def test_create_run_validation_failures(repo_env, overrides):
    payload = dict(
        goal="diagnose latency",
        host_id=repo_env["host_id"],
        system_user_id=19,
        mode="assisted",
    )
    payload.update(overrides)
    with pytest.raises(AutonomyValidationError):
        repo_env["repo"].create_run("admin", "admin", **payload)


def test_create_run_requires_asset_authorization(repo_env):
    repo_env["platform_state"]["asset_ok"] = False
    with pytest.raises(AutonomyPermissionError):
        repo_env["repo"].create_run(
            "admin", "admin",
            goal="diagnose", host_id=repo_env["host_id"],
            system_user_id=19, mode="assisted",
        )


def test_create_run_requires_credential_authorization(repo_env):
    repo_env["platform_state"]["credential_ok"] = False
    with pytest.raises(AutonomyPermissionError):
        repo_env["repo"].create_run(
            "admin", "admin",
            goal="diagnose", host_id=repo_env["host_id"],
            system_user_id=19, mode="assisted",
        )


def test_lab_mode_needs_admin_maintained_lab_environment(repo_env):
    """名为 lab 的资产组不授予自治能力，只有 ai_environment=lab 授予。"""
    repo, session = repo_env["repo"], repo_env["session"]
    session.add(t_group(name="lab"))
    host = session.get(t_host, repo_env["host_id"])
    host.group = "lab"
    session.commit()

    with pytest.raises(AutonomyValidationError):
        repo.create_run(
            "admin", "admin",
            goal="lab experiment", host_id=repo_env["host_id"],
            system_user_id=19, mode="lab_autonomous",
        )

    repo.set_host_environment(repo_env["host_id"], "lab")
    run = repo.create_run(
        "admin", "admin",
        goal="lab experiment", host_id=repo_env["host_id"],
        system_user_id=19, mode="lab_autonomous",
    )
    assert run["mode"] == "lab_autonomous"


def test_only_one_active_run_per_host(repo_env):
    repo_env["create_started_run"]()
    with pytest.raises(AutonomyConflict):
        repo_env["repo"].create_run(
            "admin", "admin",
            goal="second run", host_id=repo_env["host_id"],
            system_user_id=19, mode="assisted",
        )


# ---------------------------------------------------------------------------
# 启动边界：状态转换 + 重新校验授权
# ---------------------------------------------------------------------------

def test_start_run_moves_draft_to_queued_and_bumps_revision(repo_env):
    repo = repo_env["repo"]
    run = repo.create_run(
        "admin", "admin",
        goal="diagnose", host_id=repo_env["host_id"],
        system_user_id=19, mode="assisted",
    )
    started = repo.start_run("admin", "admin", run["id"])
    assert started["status"] == "queued"
    assert started["revision"] == 1
    assert started["started_at"] is not None
    with pytest.raises(AutonomyStateError):
        repo.start_run("admin", "admin", run["id"])


def test_start_run_rechecks_asset_and_credential_authorization(repo_env):
    repo = repo_env["repo"]
    run = repo.create_run(
        "admin", "admin",
        goal="diagnose", host_id=repo_env["host_id"],
        system_user_id=19, mode="assisted",
    )
    repo_env["platform_state"]["asset_ok"] = False
    with pytest.raises(AutonomyPermissionError):
        repo.start_run("admin", "admin", run["id"])
    repo_env["platform_state"]["asset_ok"] = True
    repo_env["platform_state"]["credential_ok"] = False
    with pytest.raises(AutonomyPermissionError):
        repo.start_run("admin", "admin", run["id"])


def test_run_is_owner_scoped(repo_env):
    run = repo_env["create_started_run"]()
    with pytest.raises(AutonomyNotFound):
        repo_env["repo"].get_run("someone-else", run["id"])
    with pytest.raises(AutonomyNotFound):
        repo_env["repo"].start_run("someone-else", "admin", run["id"])


# ---------------------------------------------------------------------------
# 探针提议：白名单 + 预算 + digest 落库
# ---------------------------------------------------------------------------

def test_propose_probe_persists_immutable_snapshot_and_digest(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    step = repo.propose_probe("admin", "admin", run["id"], "system.load")
    assert step["status"] == "proposed"
    assert step["action_digest"]

    row = repo_env["session"].get(t_ai_autonomous_step, step["id"])
    action = json.loads(row.action_json)
    assert action["kind"] == "probe"
    assert action["target_id"] == repo_env["host_id"]
    assert action["system_user_id"] == 19
    assert action["parameters"]["probe_id"] == "system.load"
    rebuilt = StructuredAction(
        kind=action["kind"],
        target_id=action["target_id"],
        system_user_id=action["system_user_id"],
        parameters=action["parameters"],
        working_directory=action["working_directory"],
        timeout_seconds=action["timeout_seconds"],
        step_id=action["step_id"],
    )
    assert build_action_digest(rebuilt, SECRET_KEY) == row.action_digest
    # 探针是服务端自有只读动作：无需审批，但 S1 不执行。
    snapshot = repo.snapshot("admin", run["id"])
    assert snapshot["allowed_operations"] == []


def test_propose_probe_rejects_unknown_or_injected_parameters(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    with pytest.raises(AutonomyValidationError):
        repo.propose_probe("admin", "admin", run["id"], "system.rm_rf")
    with pytest.raises(AutonomyValidationError):
        repo.propose_probe(
            "admin", "admin", run["id"], "service.status",
            params={"unit": "nginx; reboot"},
        )
    with pytest.raises(AutonomyValidationError):
        repo.propose_probe(
            "admin", "admin", run["id"], "system.load",
            params={"command": "rm -rf /"},
        )


def test_propose_probe_requires_active_run(repo_env):
    repo = repo_env["repo"]
    run = repo.create_run(
        "admin", "admin",
        goal="diagnose", host_id=repo_env["host_id"],
        system_user_id=19, mode="assisted",
    )
    with pytest.raises(AutonomyConflict):
        repo.propose_probe("admin", "admin", run["id"], "system.load")


def test_propose_probe_enforces_action_budget(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"](
        budget_payload={"max_actions": 1},
    )
    repo.propose_probe("admin", "admin", run["id"], "system.load")
    with pytest.raises(AutonomyConflict):
        repo.propose_probe("admin", "admin", run["id"], "system.memory")


def test_propose_probe_rechecks_authorization(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    repo_env["platform_state"]["asset_ok"] = False
    with pytest.raises(AutonomyPermissionError):
        repo.propose_probe("admin", "admin", run["id"], "system.load")


# ---------------------------------------------------------------------------
# 原子审批决策
# ---------------------------------------------------------------------------

def _force_waiting_approval(env, action=None):
    """直接构造一个等待审批的动作 Step（模拟 APPROVAL_REQUIRED 分支）。"""
    session = env["session"]
    run_row = env["session"].get(t_ai_autonomous_run, env["run_id"])
    step_id = "step-waiting-%d" % (
        session.query(t_ai_autonomous_step).count() + 1
    )
    action = action or StructuredAction(
        kind="shell",
        target_id=int(run_row.host_id),
        system_user_id=int(run_row.system_user_id),
        parameters={"command": "systemctl restart nginx"},
        timeout_seconds=60,
        step_id=step_id,
    )
    seq = session.query(t_ai_autonomous_step).filter_by(
        run_id=run_row.id,
    ).count() + 1
    session.add(t_ai_autonomous_step(
        id=step_id,
        run_id=run_row.id,
        kind="action",
        status="waiting_approval",
        seq=seq,
        summary="shell command=systemctl restart nginx",
        action_json=json.dumps(
            action.to_canonical_dict(), sort_keys=True, ensure_ascii=True,
        ),
        action_digest=build_action_digest(action, SECRET_KEY),
        note="",
    ))
    run_row.status = "waiting_approval"
    session.commit()
    return step_id


@pytest.fixture()
def waiting_env(repo_env):
    run = repo_env["create_started_run"]()
    repo_env["run_id"] = run["id"]
    repo_env["step_id"] = _force_waiting_approval(repo_env)
    return repo_env


def _revision(env):
    row = env["session"].get(t_ai_autonomous_run, env["run_id"])
    return int(row.revision)


def test_allowed_operations_is_server_authoritative(waiting_env):
    repo = waiting_env["repo"]
    assert repo.allowed_operations("admin", waiting_env["run_id"]) == [
        "approve", "reject",
    ]
    snapshot = repo.snapshot("admin", waiting_env["run_id"])
    assert snapshot["status"] == "waiting_approval"
    assert snapshot["allowed_operations"] == ["approve", "reject"]


def test_decision_input_is_exactly_operation_and_expected_revision(waiting_env):
    repo = waiting_env["repo"]
    step = repo.decide(
        "admin", "admin", waiting_env["run_id"], waiting_env["step_id"],
        operation="approve", expected_revision=_revision(waiting_env),
    )
    assert step["status"] == "approved"
    assert step["note"] == "approved"
    run_row = waiting_env["session"].get(
        t_ai_autonomous_run, waiting_env["run_id"],
    )
    assert run_row.status == "queued"
    # 解锁后不再有可执行操作。
    assert repo.allowed_operations("admin", waiting_env["run_id"]) == []


def test_reject_lands_step_in_failed_with_note(waiting_env):
    repo = waiting_env["repo"]
    step = repo.decide(
        "admin", "admin", waiting_env["run_id"], waiting_env["step_id"],
        operation="reject", expected_revision=_revision(waiting_env),
    )
    assert step["status"] == "failed"
    assert step["note"] == "rejected"


@pytest.mark.parametrize("operation,revision_delta", [
    ("approve", 1),    # stale revision
    ("approve", -99),
])
def test_stale_or_invalid_revision_is_a_conflict(
    waiting_env, operation, revision_delta,
):
    with pytest.raises(AutonomyConflict):
        waiting_env["repo"].decide(
            "admin", "admin", waiting_env["run_id"], waiting_env["step_id"],
            operation=operation,
            expected_revision=_revision(waiting_env) + revision_delta,
        )


def test_missing_expected_revision_is_a_conflict(waiting_env):
    with pytest.raises(AutonomyConflict):
        waiting_env["repo"].decide(
            "admin", "admin", waiting_env["run_id"], waiting_env["step_id"],
            operation="approve", expected_revision=None,
        )


def test_operation_must_come_from_allowed_operations(waiting_env):
    for operation in ("execute", "retry", "", "APPROVE"):
        with pytest.raises(AutonomyConflict):
            waiting_env["repo"].decide(
                "admin", "admin", waiting_env["run_id"],
                waiting_env["step_id"],
                operation=operation,
                expected_revision=_revision(waiting_env),
            )


def test_duplicate_decision_is_rejected(waiting_env):
    repo = waiting_env["repo"]
    repo.decide(
        "admin", "admin", waiting_env["run_id"], waiting_env["step_id"],
        operation="approve", expected_revision=_revision(waiting_env),
    )
    with pytest.raises(AutonomyConflict):
        repo.decide(
            "admin", "admin", waiting_env["run_id"], waiting_env["step_id"],
            operation="approve", expected_revision=_revision(waiting_env),
        )


def test_cross_run_step_id_is_a_conflict_not_a_leak(waiting_env):
    session = waiting_env["session"]
    other_run = t_ai_autonomous_run(
        id="other-run", owner="admin", goal="second",
        host_id=waiting_env["host_id"], host_alias="web-01",
        system_user_id=19, system_user_alias="readonly",
        mode="assisted", status="queued", revision=0,
        budget_json="{}", latest_event_seq=0,
    )
    session.add(other_run)
    session.commit()
    with pytest.raises(AutonomyConflict):
        waiting_env["repo"].decide(
            "admin", "admin", "other-run", waiting_env["step_id"],
            operation="approve", expected_revision=0,
        )


def test_tampered_action_snapshot_breaks_approval(waiting_env):
    session = waiting_env["session"]
    row = session.get(t_ai_autonomous_step, waiting_env["step_id"])
    tampered = json.loads(row.action_json)
    tampered["parameters"]["command"] = "rm -rf /"
    row.action_json = json.dumps(tampered, sort_keys=True)
    session.commit()
    with pytest.raises(AutonomyConflict):
        waiting_env["repo"].decide(
            "admin", "admin", waiting_env["run_id"], waiting_env["step_id"],
            operation="approve", expected_revision=_revision(waiting_env),
        )


def test_decision_rechecks_asset_credential_and_environment(waiting_env):
    repo = waiting_env["repo"]
    revision = _revision(waiting_env)

    waiting_env["platform_state"]["asset_ok"] = False
    with pytest.raises(AutonomyPermissionError):
        repo.decide(
            "admin", "admin", waiting_env["run_id"], waiting_env["step_id"],
            operation="approve", expected_revision=revision,
        )
    waiting_env["platform_state"]["asset_ok"] = True

    waiting_env["platform_state"]["credential_ok"] = False
    with pytest.raises(AutonomyPermissionError):
        repo.decide(
            "admin", "admin", waiting_env["run_id"], waiting_env["step_id"],
            operation="approve", expected_revision=revision,
        )
    waiting_env["platform_state"]["credential_ok"] = True

    # 环境在等待审批期间被改回 production 时，lab 模式的 Run 必须被拦下。
    session = waiting_env["session"]
    run_row = session.get(t_ai_autonomous_run, waiting_env["run_id"])
    run_row.mode = "lab_autonomous"
    session.commit()
    with pytest.raises(AutonomyPermissionError):
        repo.decide(
            "admin", "admin", waiting_env["run_id"], waiting_env["step_id"],
            operation="approve", expected_revision=revision,
        )


def test_decision_is_owner_scoped(waiting_env):
    with pytest.raises(AutonomyNotFound):
        waiting_env["repo"].decide(
            "someone-else", "admin", waiting_env["run_id"],
            waiting_env["step_id"],
            operation="approve", expected_revision=_revision(waiting_env),
        )


def test_failed_decisions_leave_state_untouched(waiting_env):
    before = _revision(waiting_env)
    with pytest.raises(AutonomyConflict):
        waiting_env["repo"].decide(
            "admin", "admin", waiting_env["run_id"], waiting_env["step_id"],
            operation="execute", expected_revision=before,
        )
    assert _revision(waiting_env) == before
    step = waiting_env["session"].get(
        t_ai_autonomous_step, waiting_env["step_id"],
    )
    assert step.status == "waiting_approval"


# ---------------------------------------------------------------------------
# ai_environment 维护与 Artifact
# ---------------------------------------------------------------------------

def test_set_host_environment_validates_value_and_host(repo_env):
    repo = repo_env["repo"]
    result = repo.set_host_environment(repo_env["host_id"], "staging")
    assert result == {
        "host_id": repo_env["host_id"],
        "alias": "web-01",
        "previous": "production",
        "ai_environment": "staging",
    }
    with pytest.raises(AutonomyValidationError):
        repo.set_host_environment(repo_env["host_id"], "dmz")
    with pytest.raises(AutonomyPermissionError):
        repo.set_host_environment(99999, "lab")


def test_create_artifact_encrypts_truncates_and_expires(
    repo_env, monkeypatch,
):
    import app.tools.basesec as basesec

    monkeypatch.setattr(
        basesec, "encrypt_secret", lambda text: "enc:%s" % text,
    )
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    artifact = repo.create_artifact(
        "admin", run["id"],
        kind="step_output", title="probe output", content="load 0.1",
    )
    assert artifact["kind"] == "step_output"

    row = repo_env["session"].get(t_ai_autonomous_artifact, artifact["id"])
    assert row.content_ciphertext == "enc:load 0.1"
    assert row.truncated is False
    assert row.expires_at > row.created_at

    huge = "x" * (row.size_bytes + 70000)
    truncated = repo.create_artifact(
        "admin", run["id"],
        kind="step_output", title="huge", content=huge,
    )
    huge_row = repo_env["session"].get(
        t_ai_autonomous_artifact, truncated["id"],
    )
    assert huge_row.truncated is True
    assert huge_row.size_bytes == 65536


def test_event_payload_is_sanitized_of_credentials(repo_env):
    cleaned = sanitize_payload({
        "step_id": "s1",
        "password": "hunter2",
        "api_token": "tok",
        "nested": {"client_secret": "x", "ok": "y"},
        "list": [{"private_key": "k"}, {"safe": 1}],
    })
    assert "password" not in cleaned
    assert "api_token" not in cleaned
    assert cleaned["nested"] == {"ok": "y"}
    assert cleaned["list"] == [{}, {"safe": 1}]
    assert cleaned["step_id"] == "s1"
