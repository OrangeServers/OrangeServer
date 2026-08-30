"""Redis-backed ephemeral state for the OrangeServer Web AI Agent."""
from __future__ import annotations

import copy
import json
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

from app.ai.context import normalize_context_mode


CONVERSATION_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_CONVERSATIONS_PER_USER = 20


class AgentStoreError(RuntimeError):
    pass


class AgentStoreNotFound(AgentStoreError):
    pass


class AgentStoreConflict(AgentStoreError):
    pass


class AgentStore:
    """Owns all Agent Redis keys and enforces owner-scoped access."""

    def __init__(
        self,
        redis_client: Any,
        *,
        now: Callable[[], float] = time.time,
        conversation_ttl: int = CONVERSATION_TTL_SECONDS,
        max_conversations: int = MAX_CONVERSATIONS_PER_USER,
    ):
        self.redis = redis_client
        self.now = now
        self.conversation_ttl = conversation_ttl
        self.max_conversations = max_conversations

    @staticmethod
    def _json(value: Dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _loads(value: Any) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return json.loads(value)

    @staticmethod
    def _conversation_key(owner: str, conversation_id: str) -> str:
        return f"ai:conversation:{owner}:{conversation_id}"

    @staticmethod
    def _conversation_index(owner: str) -> str:
        return f"ai:conversation-index:{owner}"

    @staticmethod
    def _result_key(owner: str, result_id: str) -> str:
        return f"ai:result:{owner}:{result_id}"

    @staticmethod
    def _conversation_results(owner: str, conversation_id: str) -> str:
        return f"ai:conversation-results:{owner}:{conversation_id}"

    @staticmethod
    def _run_lock_key(owner: str, conversation_id: str) -> str:
        return f"ai:run-lock:{owner}:{conversation_id}"

    def create_conversation(
        self,
        owner: str,
        provider_code: str,
        model: str,
        *,
        title: str = "新会话",
        context_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        conversation_id = uuid.uuid4().hex
        now = self.now()
        conversation = {
            "id": conversation_id,
            "owner": owner,
            "title": title[:60] or "新会话",
            "provider_code": provider_code,
            "model": model,
            "context_mode": normalize_context_mode(context_mode),
            "summary": "",
            "messages": [],
            "events": [],
            "state": {},
            "created_at": now,
            "updated_at": now,
        }
        self._save_conversation(conversation)
        self._cleanup_old_conversations(owner)
        return copy.deepcopy(conversation)

    def _save_conversation(self, conversation: Dict[str, Any]) -> None:
        owner = conversation["owner"]
        conversation_id = conversation["id"]
        key = self._conversation_key(owner, conversation_id)
        score = float(conversation.get("updated_at") or self.now())
        self.redis.set(key, self._json(conversation), ex=self.conversation_ttl)
        index = self._conversation_index(owner)
        self.redis.zadd(index, {conversation_id: score})
        self.redis.expire(index, self.conversation_ttl)

    def save_conversation(self, owner: str, conversation: Dict[str, Any]) -> Dict[str, Any]:
        if conversation.get("owner") != owner:
            raise AgentStoreNotFound("conversation not found")
        conversation = copy.deepcopy(conversation)
        conversation["updated_at"] = self.now()
        self._save_conversation(conversation)
        return conversation

    def get_conversation(self, owner: str, conversation_id: str) -> Dict[str, Any]:
        value = self._loads(self.redis.get(self._conversation_key(owner, conversation_id)))
        if not value or value.get("owner") != owner:
            raise AgentStoreNotFound("conversation not found")
        return value

    def list_conversations(self, owner: str) -> List[Dict[str, Any]]:
        ids = self.redis.zrevrange(self._conversation_index(owner), 0, self.max_conversations - 1)
        rows = []
        for conversation_id in ids:
            if isinstance(conversation_id, bytes):
                conversation_id = conversation_id.decode("utf-8")
            try:
                conversation = self.get_conversation(owner, conversation_id)
            except AgentStoreNotFound:
                self.redis.zrem(self._conversation_index(owner), conversation_id)
                continue
            rows.append({
                key: conversation.get(key)
                for key in (
                    "id", "title", "provider_code", "model", "context_mode",
                    "created_at", "updated_at",
                )
            })
        return rows

    def append_message(
        self,
        owner: str,
        conversation_id: str,
        message: Dict[str, Any],
    ) -> Dict[str, Any]:
        conversation = self.get_conversation(owner, conversation_id)
        conversation["messages"].append(copy.deepcopy(message))
        if conversation["title"] == "新会话" and message.get("role") == "user":
            content = str(message.get("content") or "").strip()
            if content:
                conversation["title"] = content[:30]
        return self.save_conversation(owner, conversation)

    def append_event(
        self,
        owner: str,
        conversation_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        conversation = self.get_conversation(owner, conversation_id)
        conversation["events"].append(copy.deepcopy(event))
        conversation["events"] = conversation["events"][-200:]
        return self.save_conversation(owner, conversation)

    def acquire_run_lock(
        self,
        owner: str,
        conversation_id: str,
        *,
        ttl: int = 10 * 60,
    ) -> str:
        self.get_conversation(owner, conversation_id)
        token = uuid.uuid4().hex
        if not self.redis.set(
            self._run_lock_key(owner, conversation_id),
            token,
            ex=max(30, int(ttl)),
            nx=True,
        ):
            raise AgentStoreConflict("conversation is already running")
        return token

    def release_run_lock(
        self,
        owner: str,
        conversation_id: str,
        token: str,
    ) -> None:
        key = self._run_lock_key(owner, conversation_id)
        current = self.redis.get(key)
        if isinstance(current, bytes):
            current = current.decode("utf-8")
        if current == token:
            self.redis.delete(key)

    def delete_conversation(self, owner: str, conversation_id: str) -> None:
        self.get_conversation(owner, conversation_id)

        result_index = self._conversation_results(owner, conversation_id)
        result_keys = [
            self._result_key(owner, rid.decode("utf-8") if isinstance(rid, bytes) else rid)
            for rid in self.redis.smembers(result_index)
        ]
        keys = result_keys + [
            result_index,
            self._conversation_key(owner, conversation_id),
        ]
        if keys:
            self.redis.delete(*keys)
        self.redis.zrem(self._conversation_index(owner), conversation_id)

    def _cleanup_old_conversations(self, owner: str) -> None:
        index = self._conversation_index(owner)
        while self.redis.zcard(index) > self.max_conversations:
            oldest = self.redis.zrange(index, 0, 0)
            if not oldest:
                break
            conversation_id = oldest[0]
            if isinstance(conversation_id, bytes):
                conversation_id = conversation_id.decode("utf-8")
            try:
                self.get_conversation(owner, conversation_id)
            except AgentStoreNotFound:
                self.redis.zrem(index, conversation_id)
                continue
            self.delete_conversation(owner, conversation_id)

    def create_result_set(
        self,
        owner: str,
        conversation_id: str,
        kind: str,
        *,
        rows: List[Dict[str, Any]],
        resource_ids: Iterable[Any],
        filters: Optional[Dict[str, Any]] = None,
        summary: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.get_conversation(owner, conversation_id)
        result_id = uuid.uuid4().hex
        result = {
            "id": result_id,
            "owner": owner,
            "conversation_id": conversation_id,
            "kind": kind,
            "rows": copy.deepcopy(rows),
            "resource_ids": list(resource_ids),
            "filters": copy.deepcopy(filters or {}),
            "summary": copy.deepcopy(summary or {}),
            "created_at": self.now(),
        }
        key = self._result_key(owner, result_id)
        self.redis.set(key, self._json(result), ex=self.conversation_ttl)
        index = self._conversation_results(owner, conversation_id)
        self.redis.sadd(index, result_id)
        self.redis.expire(index, self.conversation_ttl)
        return copy.deepcopy(result)

    def get_result_set(self, owner: str, result_id: str) -> Dict[str, Any]:
        result = self._loads(self.redis.get(self._result_key(owner, result_id)))
        if not result or result.get("owner") != owner:
            raise AgentStoreNotFound("result set not found")
        return result
