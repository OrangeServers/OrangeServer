"""AI Agent tool registration and read-only result-set behavior tests."""
from __future__ import annotations

from test_ai_agent_state import FakeRedis


class FakePlatform:
    def __init__(self, allowed_ids=None, sys_users=None):
        self.allowed_ids = list(allowed_ids or [1, 2])
        self.sys_users = list(sys_users or ["ops"])

    def get_platform_overview(self, _arguments):
        from app.ai.tools import ToolData
        return ToolData("overview", [{"host_count": 2}], [], {"host_count": 2})

    def search_assets(self, _arguments):
        from app.ai.tools import ToolData
        return ToolData(
            "assets",
            [{"id": item, "alias": f"host-{item}"} for item in self.allowed_ids],
            self.allowed_ids,
            {"total": len(self.allowed_ids)},
        )

    def search_cron_jobs(self, _arguments):
        from app.ai.tools import ToolData
        return ToolData("cron_jobs", [], [], {"total": 0})

    def list_authorized_system_users(self, _arguments=None):
        from app.ai.tools import ToolData
        return ToolData(
            "system_users",
            [{"alias": alias} for alias in self.sys_users],
            self.sys_users,
            {"total": len(self.sys_users)},
        )

    def search_accounts(self, _arguments):
        from app.ai.tools import ToolData
        return ToolData("accounts", [], [], {"total": 0})

    def search_audit_logs(self, _arguments):
        from app.ai.tools import ToolData
        return ToolData("audit_logs", [], [], {"total": 0})

    def validate_asset_ids(self, asset_ids):
        return sorted(asset_ids) == sorted(self.allowed_ids)

    def authorized_system_user_aliases(self):
        return set(self.sys_users)

    def validate_asset_sys_user_pair(self, asset_ids, sys_user):
        return (
            sorted(asset_ids) == sorted(self.allowed_ids)
            and sys_user in self.sys_users
        )


def _registry(role="user", allowed_ids=None):
    from app.ai.storage import AgentStore
    from app.ai.tools import ToolRegistry

    store = AgentStore(FakeRedis())
    conversation = store.create_conversation("alice", "minimax", "demo")
    registry = ToolRegistry(
        store=store,
        platform=FakePlatform(allowed_ids=allowed_ids),
        owner="alice",
        role=role,
        conversation_id=conversation["id"],
        command_checker=lambda command: None,
    )
    return store, conversation, registry


def test_normal_user_does_not_receive_admin_only_tools():
    _, _, registry = _registry(role="user")
    names = {item["function"]["name"] for item in registry.definitions()}

    assert "search_accounts" not in names
    assert "search_audit_logs" not in names
    assert "search_assets" in names
    assert "prepare_batch_command" not in names


def test_admin_receives_account_and_audit_tools():
    _, _, registry = _registry(role="admin")
    names = {item["function"]["name"] for item in registry.definitions()}

    assert "search_accounts" in names
    assert "search_audit_logs" in names
    assert "search_knowledge" in names


def test_admin_knowledge_tool_returns_bounded_references(monkeypatch):
    from app.ai import knowledge

    class FakeKnowledgeService:
        def __init__(self, _session):
            pass

        def search(self, query, *, limit):
            assert (query, limit) == ("disk full", 8)
            return [{"citation_id": "K1", "title": "Disk runbook"}]

    monkeypatch.setattr(knowledge, "KnowledgeService", FakeKnowledgeService)
    _, _, registry = _registry(role="admin")
    result = registry.execute("search_knowledge", {"query": "disk full"})
    assert result == {
        "knowledge_references": [{"citation_id": "K1", "title": "Disk runbook"}],
        "count": 1,
    }


def test_asset_query_returns_result_set_reference_instead_of_full_context():
    store, conversation, registry = _registry(allowed_ids=[1, 2, 3])

    response = registry.execute("search_assets", {"group": "web"})

    assert response["summary"] == {"total": 3}
    assert len(response["preview"]) == 3
    result = store.get_result_set("alice", response["result_set_id"])
    assert result["conversation_id"] == conversation["id"]
    assert result["resource_ids"] == [1, 2, 3]


def test_asset_query_filters_before_applying_result_limit(monkeypatch):
    import sys
    from types import ModuleType

    from app.ai.tools import PlatformQueryService
    from app.core.db import database

    class FakeColumn:
        def is_(self, _value):
            return self

        def in_(self, _values):
            return self

        def asc(self):
            return self

    class FakeQuery:
        def __init__(self, rows):
            self.rows = list(rows)

        def filter(self, *_conditions):
            return self

        def order_by(self, *_columns):
            return self

        def offset(self, count):
            return FakeQuery(self.rows[count:])

        def limit(self, count):
            return FakeQuery(self.rows[:count])

        def all(self):
            return list(self.rows)

    hosts = [
        type("Host", (), {
            "id": host_id,
            "alias": f"host-{host_id}",
            "host_ip": f"192.0.2.{host_id % 255}",
            "host_port": 22,
            "group": "web",
        })()
        for host_id in range(1, 202)
    ]
    fake_host_model = type("FakeHostModel", (), {
        "query": FakeQuery(hosts),
        "id": FakeColumn(),
        "group": FakeColumn(),
        "is_deleted": FakeColumn(),
    })
    monkeypatch.setattr(database, "t_host", fake_host_model)
    fake_server_management = ModuleType("app.assets.ServerManagement")
    fake_server_management._get_configured_groups = lambda: {"web"}
    fake_server_management.get_hosts_online_status = lambda host_ids: {
        host_id: host_id == 201 for host_id in host_ids
    }
    monkeypatch.setitem(
        sys.modules,
        "app.assets.ServerManagement",
        fake_server_management,
    )
    service = PlatformQueryService(owner="alice", role="admin")
    monkeypatch.setattr(service, "_allowed_groups", lambda: {"web"})

    result = service.search_assets({"online": True})

    assert result.resource_ids == [201]
    assert result.summary["total"] == 1

    fake_server_management.get_hosts_online_status = lambda host_ids: {
        host_id: True for host_id in host_ids
    }
    capped = service.search_assets({"online": True})

    assert capped.resource_ids == list(range(1, 201))
    assert capped.summary["total"] == 200


def test_bulk_host_online_status_uses_one_redis_read(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.assets.ServerManagement import get_hosts_online_status
    from app.tools import redisdb

    redis_conn = SimpleNamespace(
        mget=MagicMock(return_value=["1", "0", None])
    )
    monkeypatch.setattr(
        redisdb,
        "ConnRedis",
        lambda: SimpleNamespace(conn=redis_conn),
    )

    statuses = get_hosts_online_status([11, 12, 13])

    redis_conn.mget.assert_called_once_with([
        "host:online:11",
        "host:online:12",
        "host:online:13",
    ])
    assert statuses == {11: True, 12: False, 13: False}


def test_platform_overview_reads_online_status_in_one_batch(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    from app.ai.tools import PlatformQueryService
    from app.core.db import database

    class FakeColumn:
        def in_(self, _values):
            return True

        def is_(self, _value):
            return True

    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *_args):
            return self

        def filter_by(self, **_kwargs):
            return self

        def all(self):
            return list(self.rows)

        def count(self):
            return len(self.rows)

    hosts = [SimpleNamespace(id=11), SimpleNamespace(id=12)]
    monkeypatch.setattr(
        database,
        "t_host",
        SimpleNamespace(
            query=FakeQuery(hosts),
            group=FakeColumn(),
            is_deleted=FakeColumn(),
        ),
    )
    monkeypatch.setattr(
        database,
        "t_cron",
        SimpleNamespace(query=FakeQuery([object()])),
    )
    monkeypatch.setattr(database, "t_group", SimpleNamespace())

    requested = []
    fake_server_management = ModuleType("app.assets.ServerManagement")
    fake_server_management.get_hosts_online_status = lambda host_ids: (
        requested.append(list(host_ids)) or {11: True, 12: False}
    )

    def fail_single_read(_host_id):
        raise AssertionError("single Redis read used")

    fake_server_management.get_host_online_status = fail_single_read
    monkeypatch.setitem(
        sys.modules,
        "app.assets.ServerManagement",
        fake_server_management,
    )
    service = PlatformQueryService(owner="alice", role="admin")
    monkeypatch.setattr(service, "_allowed_groups", lambda: {"web"})

    result = service.get_platform_overview({})

    assert requested == [[11, 12]]
    assert result.summary["online_count"] == 1
    assert result.summary["offline_count"] == 1
