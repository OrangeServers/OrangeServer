"""AI Agent 会话状态与上下文档位策略的行为测试。"""
from __future__ import annotations

import fnmatch


class FakeRedis:
    """覆盖 AgentStore 使用到的最小 redis-py 行为。"""

    def __init__(self):
        self.values = {}
        self.zsets = {}
        self.sets = {}
        self.ttls = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.ttls[key] = ex
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            deleted += int(key in self.values or key in self.zsets or key in self.sets)
            self.values.pop(key, None)
            self.zsets.pop(key, None)
            self.sets.pop(key, None)
            self.ttls.pop(key, None)
        return deleted

    def expire(self, key, ttl):
        exists = key in self.values or key in self.zsets or key in self.sets
        if exists:
            self.ttls[key] = ttl
        return exists

    def eval(self, script, numkeys, key, *args):
        assert numkeys == 1
        if self.get(key) != args[0]:
            return 0
        command = script.lower()
        if "expire" in command:
            return int(self.expire(key, int(args[1])))
        if "del" in command:
            return self.delete(key)
        raise AssertionError("unsupported FakeRedis script")

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zrange(self, key, start, end):
        rows = sorted(self.zsets.get(key, {}).items(), key=lambda row: row[1])
        if end == -1:
            end = len(rows) - 1
        return [item[0] for item in rows[start:end + 1]]

    def zrevrange(self, key, start, end):
        rows = sorted(self.zsets.get(key, {}).items(), key=lambda row: row[1], reverse=True)
        if end == -1:
            end = len(rows) - 1
        return [item[0] for item in rows[start:end + 1]]

    def zrem(self, key, *members):
        target = self.zsets.get(key, {})
        for member in members:
            target.pop(member, None)

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def srem(self, key, *members):
        target = self.sets.get(key, set())
        for member in members:
            target.discard(member)

    def scan_iter(self, match):
        for key in list(self.values) + list(self.zsets) + list(self.sets):
            if fnmatch.fnmatch(key, match):
                yield key


def test_recent_conversations_keep_latest_twenty():
    from app.ai.storage import AgentStore

    clock = iter(range(100, 200))
    store = AgentStore(FakeRedis(), now=lambda: float(next(clock)))

    created = [
        store.create_conversation("alice", provider_code="minimax", model="MiniMax-Test")
        for _ in range(21)
    ]

    recent = store.list_conversations("alice")
    assert len(recent) == 20
    assert recent[0]["id"] == created[-1]["id"]
    assert all(item["id"] != created[0]["id"] for item in recent)


def test_conversation_context_mode_defaults_to_standard_and_persists_deep_mode():
    from app.ai.context import DEEP_CONTEXT_MODE, STANDARD_CONTEXT_MODE
    from app.ai.storage import AgentStore

    store = AgentStore(FakeRedis())
    standard = store.create_conversation("alice", "minimax", "demo")
    deep = store.create_conversation(
        "alice",
        "siliconflow",
        "demo",
        context_mode=DEEP_CONTEXT_MODE,
    )

    assert standard["context_mode"] == STANDARD_CONTEXT_MODE
    assert store.get_conversation("alice", deep["id"])["context_mode"] == DEEP_CONTEXT_MODE
    listed = {row["id"]: row for row in store.list_conversations("alice")}
    assert listed[deep["id"]]["context_mode"] == DEEP_CONTEXT_MODE


def test_conversation_autonomy_permission_defaults_to_ask_and_persists_custom():
    from app.ai.storage import AgentStore

    store = AgentStore(FakeRedis())
    standard = store.create_conversation("alice", "minimax", "demo")
    custom = store.create_conversation(
        "alice",
        "minimax",
        "demo",
        autonomy_mode="custom",
        autonomy_profile={"action_categories": ["file_patch", "systemd"]},
    )

    assert standard["autonomy_mode"] == "ask"
    assert standard["autonomy_profile"] is None
    restored = store.get_conversation("alice", custom["id"])
    assert restored["autonomy_mode"] == "custom"
    assert restored["autonomy_profile"] == {
        "action_categories": ["file_patch", "systemd"],
    }
    listed = {row["id"]: row for row in store.list_conversations("alice")}
    assert listed[custom["id"]]["autonomy_mode"] == "custom"
    assert listed[custom["id"]]["autonomy_profile"] == {
        "action_categories": ["file_patch", "systemd"],
    }


def test_deleting_conversation_invalidates_result_sets():
    from app.ai.storage import AgentStore, AgentStoreNotFound

    store = AgentStore(FakeRedis())
    conversation = store.create_conversation("alice", "siliconflow", "demo-model")
    result = store.create_result_set(
        "alice", conversation["id"], "assets",
        rows=[{"id": 1, "alias": "web-01"}],
        resource_ids=[1],
    )

    store.delete_conversation("alice", conversation["id"])

    for getter, args in (
        (store.get_conversation, ("alice", conversation["id"])),
        (store.get_result_set, ("alice", result["id"])),
    ):
        try:
            getter(*args)
        except AgentStoreNotFound:
            pass
        else:
            raise AssertionError("deleted conversation descendants must be invalidated")


def test_conversation_run_lock_blocks_parallel_runs_and_releases_by_token():
    from app.ai.storage import AgentStore, AgentStoreConflict

    store = AgentStore(FakeRedis())
    conversation = store.create_conversation("alice", "minimax", "demo")
    token = store.acquire_run_lock("alice", conversation["id"])

    try:
        store.acquire_run_lock("alice", conversation["id"])
    except AgentStoreConflict:
        pass
    else:
        raise AssertionError("parallel runs in one conversation must be rejected")

    store.release_run_lock("alice", conversation["id"], "wrong-token")
    try:
        store.acquire_run_lock("alice", conversation["id"])
    except AgentStoreConflict:
        pass
    else:
        raise AssertionError("another token must not release the run lock")

    store.release_run_lock("alice", conversation["id"], token)
    assert store.acquire_run_lock("alice", conversation["id"])


def test_conversation_run_lock_refreshes_and_never_deletes_a_successor():
    from app.ai.storage import AgentStore, AgentStoreConflict

    redis = FakeRedis()
    store = AgentStore(redis)
    conversation = store.create_conversation("alice", "minimax", "demo")
    key = store._run_lock_key("alice", conversation["id"])
    old_token = store.acquire_run_lock("alice", conversation["id"], ttl=30)

    store.refresh_run_lock("alice", conversation["id"], old_token, ttl=90)
    assert redis.ttls[key] == 90

    redis.values[key] = "successor-token"
    try:
        store.refresh_run_lock("alice", conversation["id"], old_token)
    except AgentStoreConflict:
        pass
    else:
        raise AssertionError("a stale owner must not renew its successor's lock")
    store.release_run_lock("alice", conversation["id"], old_token)
    assert redis.get(key) == "successor-token"


def test_context_policy_defaults_to_256k_and_only_enables_deep_with_1m_capability():
    from app.ai.context import (
        DEEP_CONTEXT_MODE,
        DEEP_CONTEXT_TOKENS,
        STANDARD_CONTEXT_MODE,
        STANDARD_CONTEXT_TOKENS,
        resolve_context_window,
    )

    assert resolve_context_window(None, STANDARD_CONTEXT_TOKENS) == STANDARD_CONTEXT_TOKENS
    assert (
        resolve_context_window(STANDARD_CONTEXT_MODE, DEEP_CONTEXT_TOKENS)
        == STANDARD_CONTEXT_TOKENS
    )
    assert (
        resolve_context_window(DEEP_CONTEXT_MODE, DEEP_CONTEXT_TOKENS)
        == DEEP_CONTEXT_TOKENS
    )

    for mode, capability in (
        (DEEP_CONTEXT_MODE, STANDARD_CONTEXT_TOKENS),
        ("unsupported", DEEP_CONTEXT_TOKENS),
    ):
        try:
            resolve_context_window(mode, capability)
        except ValueError:
            pass
        else:
            raise AssertionError("unsupported context mode/capability must fail closed")
