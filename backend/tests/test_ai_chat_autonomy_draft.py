"""M1/S3 切片 7：聊天侧仅能创建自治任务草稿/引用卡的安全边界测试。

契约：Existing chat may create an autonomy draft and return an "open task"
reference card. Chat must never start, approve, cancel, or mutate a Run.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from test_ai_agent_state import FakeRedis
from test_ai_agent_tools import _registry


# =============================================================================
# 工具定义边界：聊天工具面不存在任何 Run 生命周期能力
# =============================================================================


def test_chat_tool_surface_cannot_start_or_manage_autonomy_runs():
    from app.ai.tools import TOOL_DEFINITIONS

    names = set(TOOL_DEFINITIONS)
    assert not names & {
        "start_autonomy_run",
        "approve_autonomy_run",
        "approve_autonomy_step",
        "cancel_autonomy_run",
        "update_autonomy_run",
    }


def test_admin_definitions_include_create_autonomy_draft():
    _, _, registry = _registry(role="admin")
    names = {item["function"]["name"] for item in registry.definitions()}

    assert "create_autonomy_draft" in names


def test_normal_user_receives_create_autonomy_draft():
    from app.ai.tools import ADMIN_ONLY_TOOLS

    _, _, registry = _registry(role="user")
    names = {item["function"]["name"] for item in registry.definitions()}

    assert "create_autonomy_draft" in names
    assert "create_autonomy_draft" not in ADMIN_ONLY_TOOLS


def test_create_autonomy_draft_schema_does_not_let_model_choose_mode():
    from app.ai.tools import TOOL_DEFINITIONS

    properties = TOOL_DEFINITIONS["create_autonomy_draft"]["function"][
        "parameters"
    ]["properties"]
    assert "mode" not in properties


# =============================================================================
# 草稿创建行为：只落 draft，绝不执行
# =============================================================================


class FakeAutonomyRepository:
    instances = []

    def __init__(self, session, secret_key):
        self.session = session
        self.secret_key = secret_key
        self.create_run_calls = []
        FakeAutonomyRepository.instances.append(self)

    def create_run(self, owner, role, **kwargs):
        self.create_run_calls.append((owner, role, kwargs))
        return {
            "id": "run-draft-1",
            "goal": kwargs["goal"],
            "status": "draft",
            "mode": kwargs["mode"],
            "host_alias": "web-01",
            "custom_profile": kwargs["profile_payload"],
        }


@pytest.fixture
def autonomy_env(monkeypatch):
    from app.ai.autonomy import repository as repo_module
    from app.core import config as config_module
    from app.core.db import database as database_module

    FakeAutonomyRepository.instances = []
    monkeypatch.setattr(repo_module, "AutonomyRepository", FakeAutonomyRepository)
    monkeypatch.setattr(config_module, "AI_AUTONOMY_ENABLED", True)
    monkeypatch.setattr(
        database_module,
        "db",
        SimpleNamespace(session=SimpleNamespace(rollback=lambda: None)),
    )
    return FakeAutonomyRepository.instances


def test_create_autonomy_draft_creates_draft_only(autonomy_env):
    _, _, registry = _registry(role="admin", autonomy_mode="ai_review")

    response = registry.execute(
        "create_autonomy_draft",
        {
            "goal": "清理 web 组磁盘",
            "host_id": 1,
            "system_user_id": 2,
        },
    )

    assert response["autonomy_draft"] == {
        "run_id": "run-draft-1",
        "goal": "清理 web 组磁盘",
        "status": "draft",
        "mode": "ai_review",
        "host_alias": "web-01",
    }
    (repo,) = autonomy_env
    (owner, role, kwargs), = repo.create_run_calls
    assert owner == "alice"
    assert role == "admin"
    assert kwargs["budget_payload"] is None
    assert kwargs["profile_payload"] is None
    assert kwargs["trigger_type"] == "chat"
    assert kwargs["trigger_summary"] == "AI chat draft"


def test_user_create_autonomy_draft_still_uses_existing_run_path(autonomy_env):
    _, _, registry = _registry(role="user")

    response = registry.execute(
        "create_autonomy_draft",
        {
            "goal": "检查授权主机",
            "host_id": 1,
            "system_user_id": 2,
        },
    )

    assert response["autonomy_draft"]["status"] == "draft"
    (repo,) = autonomy_env
    (owner, role, kwargs), = repo.create_run_calls
    assert owner == "alice"
    assert role == "user"
    assert kwargs["trigger_type"] == "chat"


def test_create_autonomy_draft_disabled_flag_blocks_execution(monkeypatch):
    from app.ai.autonomy import repository as repo_module
    from app.core import config as config_module
    from app.core.db import database as database_module
    from app.ai.tools import ToolNotAllowed

    monkeypatch.setattr(repo_module, "AutonomyRepository", FakeAutonomyRepository)
    monkeypatch.setattr(config_module, "AI_AUTONOMY_ENABLED", False)
    monkeypatch.setattr(
        database_module,
        "db",
        SimpleNamespace(session=SimpleNamespace(rollback=lambda: None)),
    )
    _, _, registry = _registry(role="admin")

    with pytest.raises(ToolNotAllowed):
        registry.execute(
            "create_autonomy_draft",
            {"goal": "巡检", "host_id": 1, "system_user_id": 1},
        )


def test_create_autonomy_draft_requires_goal(autonomy_env):
    from app.ai.tools import ToolValidationError

    _, _, registry = _registry(role="admin")

    with pytest.raises(ToolValidationError):
        registry.execute(
            "create_autonomy_draft",
            {"goal": "  ", "host_id": 1, "system_user_id": 1},
        )
    assert all(not repo.create_run_calls for repo in autonomy_env)


def test_create_autonomy_draft_uses_server_selected_custom_profile(autonomy_env):
    _, _, registry = _registry(
        role="admin",
        autonomy_mode="custom",
        autonomy_profile={"action_categories": ["systemd", "file_read"]},
    )

    result = registry.execute(
        "create_autonomy_draft",
        {"goal": "巡检", "host_id": 1, "system_user_id": 1},
    )

    assert result["autonomy_draft"]["mode"] == "custom"
    assert result["autonomy_draft"]["action_categories"] == [
        "systemd", "file_read",
    ]
    (repo,) = autonomy_env
    assert repo.create_run_calls[0][2]["profile_payload"] == {
        "action_categories": ["systemd", "file_read"],
    }


def test_create_autonomy_draft_rejects_model_mode_override(autonomy_env):
    from app.ai.tools import ToolValidationError

    _, _, registry = _registry(role="admin", autonomy_mode="ask")

    with pytest.raises(ToolValidationError):
        registry.execute(
            "create_autonomy_draft",
            {
                "goal": "巡检",
                "host_id": 1,
                "system_user_id": 1,
                "mode": "auto",
            },
        )
    assert all(not repo.create_run_calls for repo in autonomy_env)


def test_create_autonomy_draft_conflict_becomes_readable_validation_error(
    monkeypatch, autonomy_env
):
    """单活唯一冲突（手测复现）必须给模型可读文案，而不是笼统
    ToolError 导致盲目重试撞穿工具步数上限。"""
    from app.ai.autonomy.repository import AutonomyConflict
    from app.ai.tools import ToolValidationError

    def conflict(self, owner, role, **kwargs):
        raise AutonomyConflict(
            "an active autonomous run already exists for this host"
        )

    monkeypatch.setattr(FakeAutonomyRepository, "create_run", conflict)
    _, _, registry = _registry(role="admin")

    with pytest.raises(ToolValidationError) as excinfo:
        registry.execute(
            "create_autonomy_draft",
            {"goal": "巡检", "host_id": 2, "system_user_id": 6},
        )
    assert "已有活动自治任务" in str(excinfo.value)


def test_create_autonomy_draft_permission_error_becomes_validation_error(
    monkeypatch, autonomy_env
):
    from app.ai.autonomy.repository import AutonomyPermissionError
    from app.ai.tools import ToolValidationError

    def denied(self, owner, role, **kwargs):
        raise AutonomyPermissionError("host is not authorized")

    monkeypatch.setattr(FakeAutonomyRepository, "create_run", denied)
    _, _, registry = _registry(role="admin")

    with pytest.raises(ToolValidationError) as excinfo:
        registry.execute(
            "create_autonomy_draft",
            {"goal": "巡检", "host_id": 2, "system_user_id": 6},
        )
    assert "授权校验失败" in str(excinfo.value)


# =============================================================================
# Runner：引用卡事件持久化 + SSE 广播；绝不启动 Run
# =============================================================================


def test_runner_emits_and_persists_autonomy_draft_reference_card(
    monkeypatch, autonomy_env
):
    import json

    from app.ai import runner as runner_module
    from app.ai.provider import ChatResult, ProviderToolCall
    from app.ai.storage import AgentStore

    class DraftAdapter:
        def __init__(self):
            self.rounds = 0

        def complete(self, **_kwargs):
            self.rounds += 1
            if self.rounds == 1:
                return ChatResult(
                    content="",
                    tool_calls=(ProviderToolCall(
                        id="call-draft",
                        name="create_autonomy_draft",
                        arguments={
                            "goal": "清理磁盘",
                            "host_id": 1,
                            "system_user_id": 2,
                        },
                    ),),
                    used_stream=False,
                )
            return ChatResult(
                content="已创建自治任务草稿", tool_calls=(), used_stream=False,
            )

    class EmptyPlatform:
        pass

    store = AgentStore(FakeRedis())
    conversation = store.create_conversation(
        "alice",
        "minimax",
        "demo",
        autonomy_mode="custom",
        autonomy_profile={"action_categories": ["systemd"]},
    )
    monkeypatch.setattr(
        runner_module,
        "PlatformQueryService",
        lambda _owner, _role: EmptyPlatform(),
    )

    adapter = DraftAdapter()

    class SharedProviderService:
        def adapter(self, _code):
            return adapter

        def runtime(self, _code, *, context_mode):
            from app.ai.context import (
                STANDARD_CONTEXT_TOKENS,
                normalize_context_mode,
            )

            return SimpleNamespace(
                adapter=adapter,
                context_mode=normalize_context_mode(context_mode),
                context_window_tokens=STANDARD_CONTEXT_TOKENS,
            )

    runner = runner_module.AgentRunner(
        store=store,
        provider_service=SharedProviderService(),
    )

    output = "".join(runner.run(
        owner="alice",
        role="admin",
        conversation_id=conversation["id"],
        message="帮我建一个清理磁盘的自治任务",
    ))
    events = [
        json.loads(line[6:])
        for line in output.splitlines()
        if line.startswith("data: ")
    ]
    draft_events = [
        event for event in events if event["type"] == "autonomy.draft_created"
    ]

    assert len(draft_events) == 1
    card = draft_events[0]
    assert card["run_id"] == "run-draft-1"
    assert card["goal"] == "清理磁盘"
    assert card["status"] == "draft"
    assert card["mode"] == "custom"
    assert card["action_categories"] == ["systemd"]

    # 持久化：刷新页面后仍能恢复引用卡
    saved = store.get_conversation("alice", conversation["id"])
    persisted = [
        event for event in saved["events"]
        if event.get("type") == "autonomy.draft_created"
    ]
    assert len(persisted) == 1
    assert persisted[0]["run_id"] == "run-draft-1"

    # 边界：聊天侧只调用过 create_run，没有任何 Run 生命周期操作
    (repo,) = autonomy_env
    assert len(repo.create_run_calls) == 1
    assert repo.create_run_calls[0][2]["mode"] == "custom"
    assert repo.create_run_calls[0][2]["profile_payload"] == {
        "action_categories": ["systemd"],
    }


# =============================================================================
# 会话详情：引用卡历史投影
# =============================================================================


def test_conversation_detail_exposes_autonomy_drafts(monkeypatch):
    from flask import Flask

    from app.ai import views
    from app.ai.storage import AgentStore

    store = AgentStore(FakeRedis())
    conversation = store.create_conversation("alice", "minimax", "demo")
    store.append_event(
        "alice",
        conversation["id"],
        {
            "id": "draft-1",
            "type": "autonomy.draft_created",
            "run_id": "run-1",
            "goal": "清理日志",
            "status": "draft",
            "mode": "custom",
            "action_categories": ["file_read", "systemd"],
            "host_alias": "web-01",
            "created_at": "2026-08-13T02:00:00+00:00",
        },
    )

    monkeypatch.setattr(views, "_identity", lambda: (None, "alice", "admin"))
    monkeypatch.setattr(views, "_store", lambda: store)
    app = Flask(__name__)
    with app.test_request_context():
        detail = views.conversation_detail(
            conversation["id"]
        ).get_json()["conversation"]

    assert detail["autonomy_drafts"] == [{
        "id": "draft-1",
        "run_id": "run-1",
        "goal": "清理日志",
        "status": "draft",
        "mode": "custom",
        "action_categories": ["file_read", "systemd"],
        "host_alias": "web-01",
        "created_at": "2026-08-13T02:00:00+00:00",
    }]


def test_conversation_api_persists_and_updates_authoritative_autonomy_profile(
    monkeypatch,
):
    from flask import Flask

    from app.ai import views
    from app.ai.storage import AgentStore

    class FakeProviders:
        def configured_row(self, _code):
            return SimpleNamespace(provider_code="minimax", model="demo")

        def context_mode(self, _row, _value):
            return "standard_256k"

    store = AgentStore(FakeRedis())
    monkeypatch.setattr(views, "_identity", lambda: (None, "alice", "admin"))
    monkeypatch.setattr(views, "_store", lambda: store)
    monkeypatch.setattr(views, "ProviderConfigService", FakeProviders)
    app = Flask(__name__)

    with app.test_request_context(json={
        "provider_code": "minimax",
        "autonomy_mode": "custom",
        "autonomy_profile": {"action_categories": ["systemd"]},
    }):
        created = views.create_conversation().get_json()["conversation"]

    assert created["autonomy_mode"] == "custom"
    assert created["autonomy_profile"] == {"action_categories": ["systemd"]}

    with app.test_request_context(json={"autonomy_mode": "ai_review"}):
        updated = views.update_conversation(created["id"]).get_json()["conversation"]

    assert updated["autonomy_mode"] == "ai_review"
    assert updated["autonomy_profile"] is None
    restored = store.get_conversation("alice", created["id"])
    assert restored["autonomy_mode"] == "ai_review"
    assert restored["autonomy_profile"] is None


@pytest.mark.parametrize("payload", [
    {"autonomy_mode": "unknown"},
    {"autonomy_mode": None},
    {"autonomy_mode": ""},
    {"autonomy_mode": False},
    {"autonomy_mode": 0},
    {"autonomy_mode": "custom"},
    {
        "autonomy_mode": "custom",
        "autonomy_profile": {"action_categories": ["unknown"]},
    },
    {
        "autonomy_mode": "ask",
        "autonomy_profile": {"action_categories": ["systemd"]},
    },
])
def test_conversation_api_rejects_invalid_autonomy_profiles(monkeypatch, payload):
    from flask import Flask

    from app.ai import views
    from app.ai.storage import AgentStore

    class FakeProviders:
        def configured_row(self, _code):
            return SimpleNamespace(provider_code="minimax", model="demo")

        def context_mode(self, _row, _value):
            return "standard_256k"

    store = AgentStore(FakeRedis())
    monkeypatch.setattr(views, "_identity", lambda: (None, "alice", "admin"))
    monkeypatch.setattr(views, "_store", lambda: store)
    monkeypatch.setattr(views, "ProviderConfigService", FakeProviders)
    app = Flask(__name__)

    with app.test_request_context(json={"provider_code": "minimax", **payload}):
        response = views.create_conversation()

    assert response[1] == 400
    assert store.list_conversations("alice") == []


def test_conversation_autonomy_update_is_owner_scoped(monkeypatch):
    from flask import Flask

    from app.ai import views
    from app.ai.storage import AgentStore

    store = AgentStore(FakeRedis())
    conversation = store.create_conversation("alice", "minimax", "demo")
    monkeypatch.setattr(views, "_identity", lambda: (None, "bob", "user"))
    monkeypatch.setattr(views, "_store", lambda: store)
    app = Flask(__name__)

    with app.test_request_context(json={"autonomy_mode": "auto"}):
        response = views.update_conversation(conversation["id"])

    assert response[1] == 404
    assert store.get_conversation("alice", conversation["id"])["autonomy_mode"] == "ask"


def test_conversation_autonomy_update_rejects_an_active_run(monkeypatch):
    from flask import Flask

    from app.ai import views
    from app.ai.storage import AgentStore

    store = AgentStore(FakeRedis())
    conversation = store.create_conversation("alice", "minimax", "demo")
    lock_token = store.acquire_run_lock("alice", conversation["id"])
    monkeypatch.setattr(views, "_identity", lambda: (None, "alice", "user"))
    monkeypatch.setattr(views, "_store", lambda: store)
    app = Flask(__name__)

    try:
        with app.test_request_context(json={"autonomy_mode": "ai_review"}):
            response = views.update_conversation(conversation["id"])

        assert response[1] == 409
        assert store.get_conversation(
            "alice", conversation["id"]
        )["autonomy_mode"] == "ask"
    finally:
        store.release_run_lock("alice", conversation["id"], lock_token)


def test_old_conversation_without_autonomy_fields_defaults_to_ask(monkeypatch):
    from flask import Flask

    from app.ai import views
    from app.ai.storage import AgentStore

    store = AgentStore(FakeRedis())
    conversation = store.create_conversation("alice", "minimax", "demo")
    legacy = store.get_conversation("alice", conversation["id"])
    legacy.pop("autonomy_mode")
    legacy.pop("autonomy_profile")
    store.save_conversation("alice", legacy)
    monkeypatch.setattr(views, "_identity", lambda: (None, "alice", "user"))
    monkeypatch.setattr(views, "_store", lambda: store)
    app = Flask(__name__)

    with app.test_request_context():
        detail = views.conversation_detail(conversation["id"]).get_json()["conversation"]

    assert detail["autonomy_mode"] == "ask"
    assert detail["autonomy_profile"] is None
