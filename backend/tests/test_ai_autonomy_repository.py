# -*- coding: utf-8 -*-
"""M1/S1: AutonomyRepository 持久层与原子审批决策契约测试（Issue #11）。

conftest 会把 db.session 的方法 patch 成 no-op，因此这里使用独立的
SQLite 内存引擎 + 注入式 session，绕开全局 patch 验证真实落库行为。
"""
import datetime
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
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
from app.ai.autonomy.state import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    AutonomyStateError,
)
from app.core.db.database import (
    db,
    t_ai_autonomous_artifact,
    t_ai_autonomous_event,
    t_ai_autonomous_evidence,
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
            t_ai_autonomous_evidence.__table__,
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
            mode="ask",
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
        mode="ask",
    )
    assert run["status"] == "draft"
    assert run["revision"] == 0
    assert run["host_alias"] == "web-01"
    assert run["system_user_alias"] == "readonly"
    assert run["trigger_type"] == "manual"
    assert run["trigger_ref"] is None
    assert run["trigger_summary"] == ""
    assert run["budget"]["max_actions"] == 30
    assert run["graph_version"] == "v2"

    events = repo_env["session"].query(t_ai_autonomous_event).all()
    assert [event.event_type for event in events] == ["run_created"]
    assert events[0].sequence == 1
    # 凭据内容永不进入事件 payload，只允许 ID 引用。
    payload = json.loads(events[0].payload_json)
    assert payload["system_user_id"] == 19
    assert "password" not in payload
    assert "credential" not in json.dumps(payload)


def test_alert_trigger_roundtrip_and_unique_idempotency(repo_env):
    repo = repo_env["repo"]
    run = repo.create_run(
        "admin", "admin",
        goal="service alert",
        host_id=repo_env["host_id"],
        system_user_id=19,
        mode="ask",
        trigger_type="alertmanager",
        trigger_ref="a" * 64,
        trigger_summary="firing: nginx on asset #1",
    )
    assert repo.find_run_by_trigger(
        "admin", "alertmanager", "a" * 64,
    )["id"] == run["id"]

    row = repo_env["session"].get(t_ai_autonomous_run, run["id"])
    row.status = "completed"
    repo_env["session"].commit()
    with pytest.raises(AutonomyConflict, match="trigger"):
        repo.create_run(
            "admin", "admin",
            goal="duplicate service alert",
            host_id=repo_env["host_id"],
            system_user_id=19,
            mode="ask",
            trigger_type="alertmanager",
            trigger_ref="a" * 64,
        )


def test_external_evidence_appends_timeline_event(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    evidence = repo.record_evidence(
        "admin", run["id"],
        kind="alert_observation",
        summary="Alertmanager firing: nginx",
        event_type="alert_firing",
    )
    event = repo_env["session"].query(t_ai_autonomous_event).filter_by(
        run_id=run["id"], event_type="alert_firing",
    ).one()
    assert json.loads(event.payload_json)["evidence_id"] == evidence["id"]


@pytest.mark.parametrize("mode", ["ask", "ai_review", "auto", "custom"])
def test_product_permission_profiles_create_v2_runs(repo_env, mode):
    repo = repo_env["repo"]
    repo.set_host_environment(repo_env["host_id"], "lab")

    # custom 档案必须显式携带服务端固定类别集合（S3 切片 3）。
    profile_payload = (
        {"action_categories": ["systemd", "file_read"]}
        if mode == "custom" else None
    )
    run = repo.create_run(
        "admin", "admin",
        goal="investigate service health",
        host_id=repo_env["host_id"],
        system_user_id=19,
        mode=mode,
        profile_payload=profile_payload,
    )

    assert run["mode"] == mode
    assert run["graph_version"] == "v2"
    assert run["custom_profile"] == profile_payload


@pytest.mark.parametrize("legacy_mode", [
    "read_only", "assisted", "lab_autonomous",
])
def test_new_runs_reject_legacy_permission_modes(repo_env, legacy_mode):
    repo_env["repo"].set_host_environment(repo_env["host_id"], "lab")

    with pytest.raises(AutonomyValidationError):
        repo_env["repo"].create_run(
            "admin", "admin",
            goal="legacy mode must not create a new v1 run",
            host_id=repo_env["host_id"],
            system_user_id=19,
            mode=legacy_mode,
        )


# ---------------------------------------------------------------------------
# S3 切片 3：custom 权限档案——服务端固定类别 + 单一主机绑定
# ---------------------------------------------------------------------------

def test_custom_run_requires_an_action_categories_profile(repo_env):
    repo = repo_env["repo"]

    with pytest.raises(AutonomyValidationError, match="custom mode requires"):
        repo.create_run(
            "admin", "admin",
            goal="custom without profile",
            host_id=repo_env["host_id"],
            system_user_id=19,
            mode="custom",
        )


def test_custom_profile_is_rejected_for_other_modes(repo_env):
    repo = repo_env["repo"]

    with pytest.raises(AutonomyValidationError, match="mode=custom"):
        repo.create_run(
            "admin", "admin",
            goal="ask run with profile",
            host_id=repo_env["host_id"],
            system_user_id=19,
            mode="ask",
            profile_payload={"action_categories": ["systemd"]},
        )


@pytest.mark.parametrize("bad_payload", [
    {"action_categories": ["reboot_world"]},
    {"action_categories": []},
    {"action_categories": "systemd"},
    {"action_categories": ["systemd"], "script": "rm -rf /"},
    "systemd",
])
def test_custom_profile_rejects_unknown_or_malformed_input(
    repo_env, bad_payload,
):
    repo = repo_env["repo"]

    with pytest.raises(AutonomyValidationError):
        repo.create_run(
            "admin", "admin",
            goal="bad custom profile",
            host_id=repo_env["host_id"],
            system_user_id=19,
            mode="custom",
            profile_payload=bad_payload,
        )


def test_custom_profile_dedupes_and_persists_categories(repo_env):
    repo = repo_env["repo"]

    run = repo.create_run(
        "admin", "admin",
        goal="custom profile roundtrip",
        host_id=repo_env["host_id"],
        system_user_id=19,
        mode="custom",
        profile_payload={
            "action_categories": ["systemd", "systemd", "file_read"],
        },
    )

    assert run["custom_profile"] == {
        "action_categories": ["systemd", "file_read"],
    }
    row = repo_env["session"].get(t_ai_autonomous_run, run["id"])
    assert json.loads(row.custom_profile_json) == run["custom_profile"]


def test_ask_run_exposes_no_custom_profile(repo_env):
    run = repo_env["create_started_run"]()

    assert run["custom_profile"] is None


def test_custom_run_blocks_out_of_profile_plan_actions(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"](
        mode="custom",
        profile_payload={"action_categories": ["systemd"]},
    )

    # 档案外的类别在提案时拒绝，绝不落半张计划。
    with pytest.raises(AutonomyValidationError, match="custom profile"):
        repo.propose_plan(
            "admin", "admin", run["id"], "install updates",
            [{"kind": "package_install", "params": {
                "manager": "apt", "package": "curl",
            }}],
        )
    assert repo_env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"],
    ).count() == 0

    # 档案内的类别照常走服务端策略（写动作仍需人工审批）。
    step = repo.propose_plan(
        "admin", "admin", run["id"], "restart nginx", _systemd_actions(),
    )
    assert step["status"] == "waiting_approval"


def test_custom_run_probes_stay_allowed_outside_profile(repo_env):
    """探针是服务端自有只读能力，不属于可配置类别，永不受限。"""
    repo = repo_env["repo"]
    run = repo_env["create_started_run"](
        mode="custom",
        profile_payload={"action_categories": ["systemd"]},
    )

    step = repo.propose_probe(
        "admin", "admin", run["id"], "system.load",
    )
    assert step["status"] == "proposed"

    plan = repo.propose_plan(
        "admin", "admin", run["id"], "recheck load",
        [{"kind": "probe", "params": {"probe_id": "system.load"}}],
    )
    assert plan["status"] == "approved"


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
            mode="ask",
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
        mode="ask",
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
            system_user_id=19, mode="ask",
        )


def test_create_run_requires_credential_authorization(repo_env):
    repo_env["platform_state"]["credential_ok"] = False
    with pytest.raises(AutonomyPermissionError):
        repo_env["repo"].create_run(
            "admin", "admin",
            goal="diagnose", host_id=repo_env["host_id"],
            system_user_id=19, mode="ask",
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
            system_user_id=19, mode="auto",
        )

    repo.set_host_environment(repo_env["host_id"], "lab")
    run = repo.create_run(
        "admin", "admin",
        goal="lab experiment", host_id=repo_env["host_id"],
        system_user_id=19, mode="auto",
    )
    assert run["mode"] == "auto"


def test_only_one_active_run_per_host(repo_env):
    repo_env["create_started_run"]()
    with pytest.raises(AutonomyConflict):
        repo_env["repo"].create_run(
            "admin", "admin",
            goal="second run", host_id=repo_env["host_id"],
            system_user_id=19, mode="ask",
        )


def test_database_unique_key_blocks_a_second_active_run(repo_env):
    """绕过 Repository 预查时，SQLite 也必须复现 MySQL 单活约束。"""
    repo = repo_env["repo"]
    session = repo_env["session"]
    host_id = repo_env["host_id"]
    first = repo.create_run(
        "admin", "admin",
        goal="first run", host_id=host_id,
        system_user_id=19, mode="ask",
    )
    first_row = session.get(t_ai_autonomous_run, first["id"])
    first_row.status = "needs_attention"
    session.commit()
    assert first_row.active_host_id == host_id

    duplicate = t_ai_autonomous_run(
        id="database-race-duplicate",
        owner="admin",
        goal="duplicate active run",
        host_id=host_id,
        host_alias="web-01",
        system_user_id=19,
        system_user_alias="readonly",
        mode="ask",
        status="draft",
        revision=0,
        budget_json="{}",
        latest_event_seq=0,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError, match="active_host_id"):
        session.commit()
    session.rollback()

    # 终态映射为 NULL：多个历史 Run 可共存，并释放资产槽位。
    first_row = session.get(t_ai_autonomous_run, first["id"])
    first_row.status = "failed"
    terminal = t_ai_autonomous_run(
        id="database-terminal-history",
        owner="admin",
        goal="completed history",
        host_id=host_id,
        host_alias="web-01",
        system_user_id=19,
        system_user_alias="readonly",
        mode="ask",
        status="completed",
        revision=1,
        budget_json="{}",
        latest_event_seq=0,
    )
    session.add(terminal)
    session.commit()
    assert first_row.active_host_id is None
    assert terminal.active_host_id is None

    replacement = repo.create_run(
        "admin", "admin",
        goal="replacement run", host_id=host_id,
        system_user_id=19, mode="ask",
    )
    replacement_row = session.get(t_ai_autonomous_run, replacement["id"])
    assert replacement_row.active_host_id == host_id


def test_active_host_generated_expression_tracks_state_contract():
    expression = str(
        t_ai_autonomous_run.__table__.c.active_host_id.computed.sqltext
    ).lower()
    for status in TERMINAL_RUN_STATUSES:
        assert status.value in expression
    for status in ACTIVE_RUN_STATUSES:
        assert status.value not in expression
    assert "else host_id" in expression


@pytest.mark.parametrize("db_message", [
    (
        "(1062, Duplicate entry '1' for key "
        "'t_ai_autonomous_run.uq_ai_auto_run_active_host')"
    ),
    "UNIQUE constraint failed: t_ai_autonomous_run.active_host_id",
])
def test_create_run_maps_active_host_unique_violation(
    repo_env, monkeypatch, db_message,
):
    """并发插入命中指定唯一键时稳定映射为领域冲突。"""
    error = IntegrityError("INSERT", {}, RuntimeError(db_message))
    original_flush = repo_env["session"].flush

    def fail_new_run_flush(*args, **kwargs):
        if any(
            isinstance(row, t_ai_autonomous_run)
            for row in repo_env["session"].new
        ):
            raise error
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(
        repo_env["session"], "flush", fail_new_run_flush,
    )
    with pytest.raises(AutonomyConflict, match="active autonomous run"):
        repo_env["repo"].create_run(
            "admin", "admin",
            goal="concurrent run", host_id=repo_env["host_id"],
            system_user_id=19, mode="ask",
        )


def test_create_run_does_not_remap_unrelated_integrity_error(
    repo_env, monkeypatch,
):
    """其他唯一键、外键或非空错误必须保留为数据库完整性错误。"""
    error = IntegrityError(
        "INSERT", {}, RuntimeError(
            "Duplicate entry 'x' for key 'uq_unrelated_constraint'"
        ),
    )
    original_flush = repo_env["session"].flush

    def fail_new_run_flush(*args, **kwargs):
        if any(
            isinstance(row, t_ai_autonomous_run)
            for row in repo_env["session"].new
        ):
            raise error
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(
        repo_env["session"], "flush", fail_new_run_flush,
    )
    with pytest.raises(IntegrityError) as caught:
        repo_env["repo"].create_run(
            "admin", "admin",
            goal="bad insert", host_id=repo_env["host_id"],
            system_user_id=19, mode="ask",
        )
    assert caught.value is error


# ---------------------------------------------------------------------------
# 启动边界：状态转换 + 重新校验授权
# ---------------------------------------------------------------------------

def test_start_run_moves_draft_to_queued_and_bumps_revision(repo_env):
    repo = repo_env["repo"]
    run = repo.create_run(
        "admin", "admin",
        goal="diagnose", host_id=repo_env["host_id"],
        system_user_id=19, mode="ask",
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
        system_user_id=19, mode="ask",
    )
    repo_env["platform_state"]["asset_ok"] = False
    with pytest.raises(AutonomyPermissionError):
        repo.start_run("admin", "admin", run["id"])
    repo_env["platform_state"]["asset_ok"] = True
    repo_env["platform_state"]["credential_ok"] = False
    with pytest.raises(AutonomyPermissionError):
        repo.start_run("admin", "admin", run["id"])


def test_cancel_queued_run_is_atomic_terminal_and_releases_host(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    repo_env["platform_state"]["asset_ok"] = False
    repo_env["platform_state"]["credential_ok"] = False
    platform_calls = len(repo_env["platform_state"]["calls"])

    row = repo_env["session"].get(t_ai_autonomous_run, run["id"])
    row.lease_owner = "stale-worker"
    row.lease_token = "stale-claim"
    row.lease_expires_at = datetime.datetime.utcnow() + (
        datetime.timedelta(minutes=2)
    )
    repo_env["session"].commit()

    requested = repo.request_cancel("admin", "admin", run["id"])
    repeated = repo.request_cancel("admin", "admin", run["id"])

    assert requested["status"] == "cancelled"
    assert requested["cancel_requested"] is True
    assert repeated["revision"] == requested["revision"]
    row = repo_env["session"].get(t_ai_autonomous_run, run["id"])
    assert row.status == "cancelled"
    assert row.completed_at is not None
    assert row.active_host_id is None
    assert row.lease_owner is None
    assert row.lease_token is None
    assert row.lease_expires_at is None
    assert len(repo_env["platform_state"]["calls"]) == platform_calls
    events = repo_env["session"].query(t_ai_autonomous_event).filter_by(
        run_id=run["id"],
    ).order_by(t_ai_autonomous_event.sequence).all()
    assert [event.event_type for event in events] == [
        "run_created", "run_started", "run_cancel_requested", "run_cancelled",
    ]


def test_cancel_requires_owner_and_admin_role(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    with pytest.raises(AutonomyNotFound):
        repo.request_cancel("someone-else", "admin", run["id"])
    with pytest.raises(AutonomyPermissionError):
        repo.request_cancel("admin", "user", run["id"])


def test_cancel_draft_is_terminal_without_permission_revalidation(repo_env):
    repo = repo_env["repo"]
    run = repo.create_run(
        "admin", "admin", goal="cancel draft",
        host_id=repo_env["host_id"], system_user_id=19, mode="ask",
    )
    repo_env["platform_state"]["asset_ok"] = False
    repo_env["platform_state"]["credential_ok"] = False
    platform_calls = len(repo_env["platform_state"]["calls"])

    cancelled = repo.request_cancel("admin", "admin", run["id"])

    assert cancelled["status"] == "cancelled"
    assert len(repo_env["platform_state"]["calls"]) == platform_calls


def test_cancel_running_run_stays_request_only(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    row = repo_env["session"].get(t_ai_autonomous_run, run["id"])
    row.status = "running"
    row.lease_owner = "worker-a"
    row.lease_token = "claim-a"
    row.lease_expires_at = (
        row.updated_at + datetime.timedelta(minutes=2)
    )
    repo_env["session"].commit()
    repo_env["platform_state"]["asset_ok"] = False
    repo_env["platform_state"]["credential_ok"] = False

    requested = repo.request_cancel("admin", "admin", run["id"])

    assert requested["status"] == "running"
    assert requested["cancel_requested"] is True
    repo_env["session"].expire_all()
    row = repo_env["session"].get(t_ai_autonomous_run, run["id"])
    assert row.lease_owner == "worker-a"
    assert row.lease_token == "claim-a"
    assert row.lease_expires_at is not None


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


def test_propose_network_probe_is_bound_to_authoritative_run_host(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()

    accepted = repo.propose_probe(
        "admin", "admin", run["id"], "verify.port_open",
        params={"host": "203.0.113.10", "port": "443"},
    )
    assert accepted["status"] == "proposed"

    with pytest.raises(AutonomyValidationError, match="run target"):
        repo.propose_probe(
            "admin", "admin", run["id"], "verify.http_status",
            params={"url": "http://198.51.100.8/metadata"},
        )


def test_propose_probe_requires_active_run(repo_env):
    repo = repo_env["repo"]
    run = repo.create_run(
        "admin", "admin",
        goal="diagnose", host_id=repo_env["host_id"],
        system_user_id=19, mode="ask",
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


def test_cancel_waiting_approval_atomically_cancels_pending_step(waiting_env):
    before = waiting_env["session"].get(
        t_ai_autonomous_run, waiting_env["run_id"],
    )
    assert before.lease_owner is None
    assert before.lease_expires_at is None
    waiting_env["platform_state"]["asset_ok"] = False
    waiting_env["platform_state"]["credential_ok"] = False
    platform_calls = len(waiting_env["platform_state"]["calls"])

    cancelled = waiting_env["repo"].request_cancel(
        "admin", "admin", waiting_env["run_id"],
    )

    assert cancelled["status"] == "cancelled"
    assert len(waiting_env["platform_state"]["calls"]) == platform_calls
    waiting_env["session"].expire_all()
    run = waiting_env["session"].get(
        t_ai_autonomous_run, waiting_env["run_id"],
    )
    step = waiting_env["session"].get(
        t_ai_autonomous_step, waiting_env["step_id"],
    )
    assert run.status == "cancelled"
    assert run.active_host_id is None
    assert step.status == "cancelled"
    assert step.note == "cancelled before execution"


def test_allowed_operations_is_server_authoritative(waiting_env):
    repo = waiting_env["repo"]
    assert repo.allowed_operations("admin", waiting_env["run_id"]) == [
        "approve", "reject",
    ]
    snapshot = repo.snapshot("admin", waiting_env["run_id"])
    assert snapshot["status"] == "waiting_approval"
    assert snapshot["allowed_operations"] == ["approve", "reject"]


def test_decision_input_is_exactly_operation_and_expected_revision(
    waiting_env,
):
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
    other_host = t_host(
        alias="web-02", host_ip="203.0.113.12", host_port=22,
        ai_environment="production",
    )
    session.add(other_host)
    session.flush()
    other_run = t_ai_autonomous_run(
        id="other-run", owner="admin", goal="second",
        host_id=int(other_host.id), host_alias="web-02",
        system_user_id=19, system_user_alias="readonly",
        mode="ask", status="queued", revision=0, graph_version="v2",
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
    run_row.mode = "auto"
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


def test_create_artifact_truncates_on_utf8_byte_boundary(
    repo_env, monkeypatch,
):
    import app.tools.basesec as basesec

    monkeypatch.setattr(
        basesec, "encrypt_secret", lambda text: "enc:%s" % text,
    )
    repo = repo_env["repo"]
    run = repo_env["create_started_run"](
        budget_payload={"step_output_bytes": 5},
    )

    created = repo.create_artifact(
        "admin", run["id"],
        kind="step_output", title="utf8", content="你a好",
    )

    row = repo_env["session"].get(
        t_ai_autonomous_artifact, created["id"],
    )
    assert row.content_ciphertext == "enc:你a"
    assert row.size_bytes == 4
    assert row.size_bytes <= 5
    assert row.truncated is True
    assert created["size_bytes"] == 4
    assert created["truncated"] is True


def test_create_artifact_enforces_run_total_and_audits_exhaustion(
    repo_env, monkeypatch,
):
    import app.tools.basesec as basesec

    monkeypatch.setattr(
        basesec, "encrypt_secret", lambda text: "enc:%s" % text,
    )
    repo = repo_env["repo"]
    run = repo_env["create_started_run"](
        budget_payload={
            "step_output_bytes": 8,
            "run_artifact_bytes": 10,
        },
    )

    first = repo.create_artifact(
        "admin", run["id"],
        kind="step_output", title="first", content="abcdefgh",
    )
    second = repo.create_artifact(
        "admin", run["id"],
        kind="step_output", title="second", content="WXYZ",
    )
    exhausted = repo.create_artifact(
        "admin", run["id"],
        kind="step_output", title="exhausted", content="z",
    )

    rows = repo_env["session"].query(t_ai_autonomous_artifact).filter_by(
        run_id=run["id"],
    ).order_by(t_ai_autonomous_artifact.created_at).all()
    assert [row.size_bytes for row in rows] == [8, 2, 0]
    assert sum(row.size_bytes for row in rows) == 10
    assert rows[1].content_ciphertext == "enc:WX"
    assert rows[1].truncated is True
    assert rows[2].content_ciphertext == (
        "enc:[CONTENT OMITTED: ARTIFACT BUDGET EXHAUSTED]"
    )
    assert rows[2].truncated is True
    assert first["truncated"] is False
    assert second["truncated"] is True
    assert exhausted["size_bytes"] == 0
    assert exhausted["truncated"] is True


def test_required_artifact_fails_instead_of_storing_a_truncated_reference(
    repo_env, monkeypatch,
):
    import app.tools.basesec as basesec

    monkeypatch.setattr(
        basesec, "encrypt_secret", lambda text: "enc:%s" % text,
    )
    repo = repo_env["repo"]
    run = repo_env["create_started_run"](
        budget_payload={
            "step_output_bytes": 4,
            "run_artifact_bytes": 4,
        },
    )

    with pytest.raises(AutonomyConflict, match="artifact capacity"):
        repo.create_artifact(
            "admin", run["id"],
            kind="backup_ref", title="required",
            content="/etc/.ogs-autonomy-backup/app.conf.ogs-bak-0123456789ab",
            require_full_content=True,
        )

    assert repo_env["session"].query(t_ai_autonomous_artifact).filter_by(
        run_id=run["id"],
    ).count() == 0


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


# ---------------------------------------------------------------------------
# S3 切片 2：计划提案与计划级授权
# ---------------------------------------------------------------------------

def _systemd_actions():
    return [
        {"kind": "systemd", "params": {"operation": "restart", "unit": "nginx"}},
    ]


def test_propose_plan_persists_immutable_snapshot_and_pauses_run(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()

    step = repo.propose_plan(
        "admin", "admin", run["id"], "restart nginx", _systemd_actions(),
    )

    assert step["kind"] == "plan"
    assert step["status"] == "waiting_approval"
    assert step["plan_actions"] == [
        "systemd operation=restart unit=nginx",
    ]
    row = repo_env["session"].get(t_ai_autonomous_step, step["id"])
    snapshot = json.loads(row.action_json)
    assert snapshot["target_id"] == repo_env["host_id"]
    assert snapshot["credential_ref"] == "system_user:19"
    assert snapshot["policy_version"]
    assert snapshot["mode"] == "ask"
    assert snapshot["graph_version"] == "v2"
    assert snapshot["budget"]["max_actions"] == 30
    assert snapshot["expires_at"] > 0
    assert len(snapshot["actions"]) == 1
    assert len(snapshot["ordered_action_digests"]) == 1
    # 目标绑定来自权威 Run 行：模型参数里的主机/用户无处可进。
    action = snapshot["actions"][0]
    assert action["target_id"] == repo_env["host_id"]
    assert action["system_user_id"] == 19
    run_row = repo_env["session"].get(t_ai_autonomous_run, run["id"])
    assert run_row.status == "waiting_approval"


def test_plan_actions_do_not_hide_long_patch_details(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    content = "setting=true\n" * 40

    step = repo.propose_plan(
        "admin", "admin", run["id"], "update config",
        [{"kind": "file_patch", "params": {
            "path": "/opt/example.conf", "content": content,
        }}],
    )

    assert len(step["plan_actions"][0]) > 255
    assert content.replace("\n", "") in step["plan_actions"][0]
    assert "path=/opt/example.conf" in step["plan_actions"][0]


def test_probe_only_plan_is_approved_without_pausing(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()

    step = repo.propose_plan(
        "admin", "admin", run["id"], "recheck load",
        [{"kind": "probe", "params": {"probe_id": "system.load"}}],
    )

    assert step["status"] == "approved"
    run_row = repo_env["session"].get(t_ai_autonomous_run, run["id"])
    # 探针-only 计划直接放行，不为审批暂停。
    assert run_row.status != "waiting_approval"


def test_propose_plan_rejects_unsupported_kinds(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    for kind in ("shell", "file_read"):
        with pytest.raises(AutonomyValidationError):
            repo.propose_plan(
                "admin", "admin", run["id"], "bad plan",
                [{"kind": kind, "params": {"command": "true"}}],
            )
    assert repo_env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"],
    ).count() == 0


def test_propose_plan_rejects_denied_actions_whole(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()

    with pytest.raises(AutonomyValidationError):
        repo.propose_plan(
            "admin", "admin", run["id"], "bad plan",
            [{"kind": "file_patch", "params": {
                "path": "/etc/shadow", "content": "x",
            }}],
        )
    # 半张计划绝不落库。
    assert repo_env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"],
    ).count() == 0


def test_only_one_pending_plan_at_a_time(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    repo.propose_plan(
        "admin", "admin", run["id"], "first", _systemd_actions(),
    )

    with pytest.raises(AutonomyConflict, match="plan already"):
        repo.propose_plan(
            "admin", "admin", run["id"], "second", _systemd_actions(),
        )


def test_propose_plan_enforces_action_budget(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"](budget_payload={"max_actions": 1})

    with pytest.raises(AutonomyConflict, match="budget"):
        repo.propose_plan(
            "admin", "admin", run["id"], "too big",
            _systemd_actions() + [
                {"kind": "systemd", "params": {
                    "operation": "start", "unit": "nginx",
                }},
            ],
        )


def test_decide_approves_or_rejects_a_plan_step(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    step = repo.propose_plan(
        "admin", "admin", run["id"], "restart nginx", _systemd_actions(),
    )
    run_row = repo_env["session"].get(t_ai_autonomous_run, run["id"])

    approved = repo.decide(
        "admin", "admin", run["id"], step["id"],
        operation="approve", expected_revision=int(run_row.revision),
    )
    assert approved["status"] == "approved"
    run_row = repo_env["session"].get(t_ai_autonomous_run, run["id"])
    assert run_row.status == "queued"

    # 模拟计划已执行完毕（终态）后，新计划才可再次提案。
    plan_row = repo_env["session"].get(t_ai_autonomous_step, step["id"])
    plan_row.status = "succeeded"
    repo_env["session"].commit()

    second = repo.propose_plan(
        "admin", "admin", run["id"], "again", _systemd_actions(),
    )
    run_row = repo_env["session"].get(t_ai_autonomous_run, run["id"])
    rejected = repo.decide(
        "admin", "admin", run["id"], second["id"],
        operation="reject", expected_revision=int(run_row.revision),
    )
    assert rejected["status"] == "failed"


def test_tampered_plan_snapshot_invalidates_approval(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    step = repo.propose_plan(
        "admin", "admin", run["id"], "restart nginx", _systemd_actions(),
    )
    row = repo_env["session"].get(t_ai_autonomous_step, step["id"])
    snapshot = json.loads(row.action_json)
    snapshot["actions"][0]["parameters"]["unit"] = "attacker"
    row.action_json = json.dumps(snapshot)
    repo_env["session"].commit()
    run_row = repo_env["session"].get(t_ai_autonomous_run, run["id"])

    with pytest.raises(AutonomyConflict, match="plan authorization invalid"):
        repo.decide(
            "admin", "admin", run["id"], step["id"],
            operation="approve", expected_revision=int(run_row.revision),
        )


def test_expired_plan_authorization_invalidates_approval(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    step = repo.propose_plan(
        "admin", "admin", run["id"], "restart nginx", _systemd_actions(),
    )
    row = repo_env["session"].get(t_ai_autonomous_step, step["id"])
    snapshot = json.loads(row.action_json)
    snapshot["expires_at"] = 1
    from app.ai.autonomy.plans import build_plan_digest, canonical_plan_json

    # 即使重签，过期复核仍不放行。
    row.action_json = canonical_plan_json(snapshot)
    row.action_digest = build_plan_digest(snapshot, repo.secret_key)
    repo_env["session"].commit()
    run_row = repo_env["session"].get(t_ai_autonomous_run, run["id"])

    with pytest.raises(AutonomyConflict, match="expired"):
        repo.decide(
            "admin", "admin", run["id"], step["id"],
            operation="approve", expected_revision=int(run_row.revision),
        )


# ---------------------------------------------------------------------------
# S3 切片 4：Evidence 归一 / Verification 提案 / 三态 Outcome
# ---------------------------------------------------------------------------

def _succeeded_write_step(repo_env, run_id, seq):
    """直接持久化一个已成功的写动作 step，模拟执行器落库结果。"""
    import uuid as _uuid

    step_id = _uuid.uuid4().hex
    action = StructuredAction(
        kind="systemd",
        target_id=repo_env["host_id"],
        system_user_id=19,
        parameters={"operation": "restart", "unit": "nginx"},
        timeout_seconds=60,
        step_id=step_id,
    )
    step = t_ai_autonomous_step(
        id=step_id,
        run_id=run_id,
        kind="action",
        status="succeeded",
        seq=seq,
        summary="restart nginx",
        action_json=json.dumps(
            action.to_canonical_dict(), sort_keys=True, ensure_ascii=True,
        ),
        action_digest=build_action_digest(action, SECRET_KEY),
        note="",
    )
    repo_env["session"].add(step)
    repo_env["session"].commit()
    return step_id


def _insert_artifact(repo_env, run_id, step_id=None):
    """绕过加密直接插一条 Artifact 行（仅用于 Evidence 引用归属校验）。"""
    import uuid as _uuid

    artifact = t_ai_autonomous_artifact(
        id=_uuid.uuid4().hex,
        run_id=run_id,
        step_id=step_id,
        kind="command_output",
        title="observation",
        content_ciphertext="ciphertext-placeholder",
        size_bytes=22,
        truncated=False,
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=7),
    )
    repo_env["session"].add(artifact)
    repo_env["session"].commit()
    return artifact.id


def test_record_evidence_normalizes_and_marks_untrusted(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    step_id = _succeeded_write_step(repo_env, run["id"], seq=1)
    artifact_id = _insert_artifact(repo_env, run["id"], step_id=step_id)

    evidence = repo.record_evidence(
        "admin", run["id"],
        kind="action_observation",
        summary="succeeded: restart nginx | " + "x" * 600,
        step_id=step_id,
        artifact_ids=[artifact_id, artifact_id, ""],
    )

    assert evidence["kind"] == "action_observation"
    assert evidence["step_id"] == step_id
    # 摘要有界截 500；Evidence 永远不可信；Artifact 引用去重。
    assert len(evidence["summary"]) == 500
    assert evidence["trusted"] is False
    assert evidence["artifact_ids"] == [artifact_id]

    listed = repo.list_evidence("admin", run["id"])
    assert [item["id"] for item in listed] == [evidence["id"]]


def test_record_evidence_rejects_unknown_kind(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()

    with pytest.raises(AutonomyValidationError, match="unknown evidence kind"):
        repo.record_evidence(
            "admin", run["id"], kind="trusted_fact", summary="nope",
        )


def test_record_evidence_rejects_cross_run_artifacts(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    import uuid as _uuid

    # 不属于本 Run 的 Artifact 引用一律拒绝（owner 隔离的引用层）。
    with pytest.raises(
        AutonomyValidationError, match="same-run artifacts",
    ):
        repo.record_evidence(
            "admin", run["id"],
            kind="action_observation", summary="steal foreign artifact",
            artifact_ids=[_uuid.uuid4().hex],
        )


def test_record_evidence_owner_isolation(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()

    with pytest.raises(AutonomyNotFound):
        repo.record_evidence(
            "intruder", run["id"],
            kind="action_observation", summary="cross-owner write",
        )
    with pytest.raises(AutonomyNotFound):
        repo.list_evidence("intruder", run["id"])


def test_propose_verification_requires_prior_succeeded_write(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()

    # 没有任何写副作用就提议验证属于模型幻觉，fail-closed 拒绝。
    with pytest.raises(
        AutonomyValidationError, match="prior succeeded write",
    ):
        repo.propose_verification(
            "admin", "admin", run["id"], "system.load",
        )


def test_propose_verification_creates_readonly_probe_step(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    _succeeded_write_step(repo_env, run["id"], seq=1)

    step = repo.propose_verification(
        "admin", "admin", run["id"], "system.load",
    )

    assert step["kind"] == "verification"
    assert step["status"] == "proposed"
    row = repo_env["session"].get(t_ai_autonomous_step, step["id"])
    snapshot = json.loads(row.action_json)
    assert snapshot["kind"] == "probe"
    assert snapshot["parameters"]["probe_id"] == "system.load"

    events = repo_env["session"].query(t_ai_autonomous_event).filter_by(
        run_id=run["id"], event_type="step_proposed",
    ).all()
    payload = json.loads(events[-1].payload_json)
    assert payload["step_kind"] == "verification"


def test_propose_verification_rejects_unknown_probe(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    _succeeded_write_step(repo_env, run["id"], seq=1)

    with pytest.raises(AutonomyValidationError, match="unknown probe"):
        repo.propose_verification(
            "admin", "admin", run["id"], "rm.rootfs",
        )


def test_conclude_run_requires_same_run_evidence(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()

    with pytest.raises(
        AutonomyValidationError, match="evidence citations",
    ):
        repo.conclude_run("admin", "admin", run["id"], "resolved", [])
    with pytest.raises(
        AutonomyValidationError, match="same-run evidence",
    ):
        repo.conclude_run(
            "admin", "admin", run["id"], "resolved", ["deadbeef" * 4],
        )


def test_conclude_run_rejects_unknown_outcome(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    evidence = repo.record_evidence(
        "admin", run["id"],
        kind="action_observation", summary="observation",
    )

    with pytest.raises(AutonomyValidationError, match="unknown outcome"):
        repo.conclude_run(
            "admin", "admin", run["id"], "success", [evidence["id"]],
        )


def test_conclude_run_resolved_without_verification_is_downgraded(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    evidence = repo.record_evidence(
        "admin", run["id"],
        kind="action_observation", summary="write succeeded",
    )

    # 动作成功不是目标达成的证明：缺验证观察绝不允许 resolved。
    result = repo.conclude_run(
        "admin", "admin", run["id"], "resolved", [evidence["id"]],
    )
    assert result["outcome"] == "inconclusive"
    assert result["requested"] == "resolved"
    assert result["forced"] == "verification_missing"


def test_conclude_run_resolved_with_verification_observation(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    action_ev = repo.record_evidence(
        "admin", run["id"],
        kind="action_observation", summary="write succeeded",
    )
    verify_ev = repo.record_evidence(
        "admin", run["id"],
        kind="verification_observation", summary="probe exit 0",
    )

    result = repo.conclude_run(
        "admin", "admin", run["id"], "resolved",
        [action_ev["id"], verify_ev["id"]],
        {
            "confirmed_facts": ["cron is active"],
            "impact_scope": "target host",
            "root_cause_hypothesis": "configuration drift",
            "confidence": "high",
            "unknowns": [],
            "recommended_actions": ["monitor cron"],
        },
    )
    assert result["outcome"] == "resolved"
    assert result["forced"] == ""

    events = repo_env["session"].query(t_ai_autonomous_event).filter_by(
        run_id=run["id"], event_type="run_concluded",
    ).all()
    payload = json.loads(events[-1].payload_json)
    assert payload["outcome"] == "resolved"
    assert payload["requested"] == "resolved"
    assert payload["forced"] == ""
    conclusion = repo.snapshot("admin", run["id"])["conclusion"]
    assert conclusion["confirmed_facts"] == ["cron is active"]
    assert conclusion["final_status"] == "resolved"
    assert conclusion["evidence_ids"] == [action_ev["id"], verify_ev["id"]]


def test_conclusion_details_redact_common_secret_material(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    evidence = repo.record_evidence(
        "admin", run["id"],
        kind="action_observation", summary="observation",
    )

    repo.conclude_run(
        "admin", "admin", run["id"], "not_resolved", [evidence["id"]],
        {
            "confirmed_facts": ["Authorization: Bearer top-secret"],
            "impact_scope": "password=hunter2",
            "root_cause_hypothesis": "token: abc123",
            "confidence": "low",
            "unknowns": [],
            "recommended_actions": [],
        },
    )

    serialized = json.dumps(
        repo.snapshot("admin", run["id"])["conclusion"],
        ensure_ascii=False,
    )
    assert "top-secret" not in serialized
    assert "hunter2" not in serialized
    assert "abc123" not in serialized
    assert "[REDACTED]" in serialized


def test_conclude_run_uncertain_write_forces_inconclusive(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    _succeeded_write_step(repo_env, run["id"], seq=1)
    # 结果不确定的写动作仍在库里：S2 语义保留，绝不自动收口成功。
    repo_env["session"].query(t_ai_autonomous_step).filter_by(
        run_id=run["id"],
    ).update({"status": "outcome_unknown"})
    repo_env["session"].commit()
    verify_ev = repo.record_evidence(
        "admin", run["id"],
        kind="verification_observation", summary="probe exit 0",
    )

    result = repo.conclude_run(
        "admin", "admin", run["id"], "resolved", [verify_ev["id"]],
    )
    assert result["outcome"] == "inconclusive"
    assert result["forced"] == "uncertain_write"


def test_conclude_run_first_conclusion_wins(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    evidence = repo.record_evidence(
        "admin", run["id"],
        kind="action_observation", summary="observation",
    )

    first = repo.conclude_run(
        "admin", "admin", run["id"], "not_resolved", [evidence["id"]],
    )
    assert first["outcome"] == "not_resolved"
    assert first["already_concluded"] is False

    # 终局 Outcome 恰好一个：重复结论不改写也不报错。
    second = repo.conclude_run(
        "admin", "admin", run["id"], "resolved", [evidence["id"]],
    )
    assert second["outcome"] == "not_resolved"
    assert second["already_concluded"] is True

    run_row = repo_env["session"].get(t_ai_autonomous_run, run["id"])
    assert run_row.outcome == "not_resolved"


# ---------------------------------------------------------------------------
# S3 切片 5：权威读取器（Event 回放 / Artifact owner 隔离）
# ---------------------------------------------------------------------------

def test_list_events_replays_monotonic_sequence_from_cursor(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    # create/start/probe 自然产生至少三条单调递增事件。
    repo.propose_probe("admin", "admin", run["id"], "system.load")

    full = repo.list_events("admin", run["id"])
    sequences = [item["sequence"] for item in full]
    assert sequences == sorted(sequences)
    assert sequences[0] == 1
    assert [item["event_type"] for item in full][:2] == [
        "run_created", "run_started",
    ]
    assert all("password" not in json.dumps(item["payload"]) for item in full)

    # after_seq 是严格游标：不含自身，之后的事件按序回放。
    cursor = sequences[0]
    tail = repo.list_events("admin", run["id"], after_seq=cursor)
    assert [item["sequence"] for item in tail] == sequences[1:]

    # 超出最新游标回放为空，重连不重复业务转换。
    assert repo.list_events(
        "admin", run["id"], after_seq=sequences[-1],
    ) == []


def test_list_events_enforces_owner_isolation(repo_env):
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()

    with pytest.raises(AutonomyNotFound):
        repo.list_events("intruder", run["id"])


def test_list_artifacts_returns_metadata_without_content(
    repo_env, monkeypatch,
):
    from app.tools import basesec

    monkeypatch.setattr(
        basesec, "encrypt_secret", lambda text: "enc:%s" % text,
    )
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    created = repo.create_artifact(
        "admin", run["id"],
        kind="step_output", title="probe output", content="load 0.1",
    )

    items = repo.list_artifacts("admin", run["id"])
    assert [item["id"] for item in items] == [created["id"]]
    item = items[0]
    assert item["kind"] == "step_output"
    assert item["title"] == "probe output"
    assert item["expired"] is False
    # 列表只给元数据，正文必须走单条读取。
    assert "content" not in item

    with pytest.raises(AutonomyNotFound):
        repo.list_artifacts("intruder", run["id"])


def test_get_artifact_decrypts_content_and_enforces_boundaries(
    repo_env, monkeypatch,
):
    from app.tools import basesec

    monkeypatch.setattr(
        basesec, "encrypt_secret", lambda text: "enc:%s" % text,
    )
    monkeypatch.setattr(
        basesec, "decrypt_secret", lambda stored: str(stored)[4:],
    )
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    created = repo.create_artifact(
        "admin", run["id"],
        kind="step_output", title="probe output", content="load 0.1",
    )

    artifact = repo.get_artifact("admin", run["id"], created["id"])
    assert artifact["content"] == "load 0.1"
    assert artifact["truncated"] is False

    # 虚构 ID、跨 owner 一律 Not Found，不泄露存在性。
    with pytest.raises(AutonomyNotFound, match="artifact"):
        repo.get_artifact("admin", run["id"], "deadbeef" * 4)
    with pytest.raises(AutonomyNotFound):
        repo.get_artifact("intruder", run["id"], created["id"])


def test_get_artifact_refuses_expired_content(repo_env, monkeypatch):
    from app.tools import basesec

    monkeypatch.setattr(
        basesec, "encrypt_secret", lambda text: "enc:%s" % text,
    )
    repo = repo_env["repo"]
    run = repo_env["create_started_run"]()
    created = repo.create_artifact(
        "admin", run["id"],
        kind="step_output", title="probe output", content="load 0.1",
    )
    # 模拟保留期已过：过期正文绝不再对外可读。
    repo_env["session"].query(t_ai_autonomous_artifact).filter_by(
        id=created["id"],
    ).update(
        {"expires_at": datetime.datetime.utcnow()
         - datetime.timedelta(days=1)},
    )
    repo_env["session"].commit()

    listed = repo.list_artifacts("admin", run["id"])
    assert listed[0]["expired"] is True
    with pytest.raises(AutonomyNotFound, match="expired"):
        repo.get_artifact("admin", run["id"], created["id"])
