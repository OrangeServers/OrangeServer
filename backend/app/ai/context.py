"""Deterministic context-window policy with model-assisted summarization."""
from __future__ import annotations

import copy
import json
import math
from typing import Any, Callable, Dict, List


STANDARD_CONTEXT_MODE = "standard_256k"
DEEP_CONTEXT_MODE = "deep_diagnostic_1m"
STANDARD_CONTEXT_TOKENS = 256 * 1024
DEEP_CONTEXT_TOKENS = 1024 * 1024
CONTEXT_WINDOWS = frozenset({
    STANDARD_CONTEXT_TOKENS,
    DEEP_CONTEXT_TOKENS,
})


def normalize_context_mode(value: Any) -> str:
    mode = str(value or STANDARD_CONTEXT_MODE).strip()
    if mode not in (STANDARD_CONTEXT_MODE, DEEP_CONTEXT_MODE):
        raise ValueError("不支持的上下文档位")
    return mode


def resolve_context_window(context_mode: Any, capability_tokens: Any) -> int:
    """Resolve a conversation mode against the configured model capability."""
    mode = normalize_context_mode(context_mode)
    if isinstance(capability_tokens, bool):
        raise ValueError("模型上下文能力无效")
    try:
        capability = int(capability_tokens)
    except (TypeError, ValueError) as exc:
        raise ValueError("模型上下文能力无效") from exc
    if capability not in CONTEXT_WINDOWS:
        raise ValueError("模型上下文能力无效")
    if mode == DEEP_CONTEXT_MODE:
        if capability < DEEP_CONTEXT_TOKENS:
            raise ValueError("当前模型未配置 1M 深度诊断能力")
        return DEEP_CONTEXT_TOKENS
    return STANDARD_CONTEXT_TOKENS


class ContextManager:
    def __init__(
        self,
        *,
        context_window: int = STANDARD_CONTEXT_TOKENS,
        threshold_ratio: float = 0.80,
        keep_rounds: int = 4,
        output_reserve_tokens: int | None = None,
        safety_reserve_tokens: int | None = None,
    ):
        self.context_window = max(1024, int(context_window))
        self.threshold_ratio = min(0.95, max(0.10, float(threshold_ratio)))
        self.keep_rounds = max(1, int(keep_rounds))
        self.output_reserve_tokens = min(
            self.context_window - 256,
            max(
                256,
                int(output_reserve_tokens)
                if output_reserve_tokens is not None
                else min(16384, self.context_window // 16),
            ),
        )
        remaining = max(256, self.context_window - self.output_reserve_tokens)
        self.safety_reserve_tokens = min(
            remaining - 128,
            max(
                128,
                int(safety_reserve_tokens)
                if safety_reserve_tokens is not None
                else max(512, self.context_window // 20),
            ),
        )
        self.runtime_reserve_tokens = 0

    @staticmethod
    def estimate_value_tokens(value: Any) -> int:
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        )
        return max(len(payload), math.ceil(len(payload.encode("utf-8")) / 4))

    def set_runtime_reservations(
        self,
        *,
        system_prompt: str,
        tools: Any,
        state: Any,
    ) -> None:
        self.runtime_reserve_tokens = self.estimate_value_tokens({
            "system": system_prompt,
            "tools": tools,
            "state": state,
        })

    @property
    def effective_input_tokens(self) -> int:
        return max(
            128,
            self.context_window
            - self.output_reserve_tokens
            - self.safety_reserve_tokens
            - self.runtime_reserve_tokens,
        )

    def budget_snapshot(self) -> Dict[str, int]:
        return {
            "context_window_tokens": self.context_window,
            "output_reserve_tokens": self.output_reserve_tokens,
            "safety_reserve_tokens": self.safety_reserve_tokens,
            "runtime_reserve_tokens": self.runtime_reserve_tokens,
            "effective_input_tokens": self.effective_input_tokens,
        }

    @staticmethod
    def estimate_tokens(messages: List[Dict[str, Any]], summary: str = "") -> int:
        """Conservative cross-provider estimate without a vendor tokenizer."""
        payload = json.dumps(
            {"summary": summary, "messages": messages},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return max(len(payload), math.ceil(len(payload.encode("utf-8")) / 4))

    def should_compress(self, conversation: Dict[str, Any]) -> bool:
        return self.estimate_tokens(
            conversation.get("messages", []),
            str(conversation.get("summary") or ""),
        ) >= int(self.effective_input_tokens * self.threshold_ratio)

    def compress(
        self,
        conversation: Dict[str, Any],
        summarize: Callable[[List[Dict[str, Any]], str], str],
    ) -> Dict[str, Any]:
        if not self.should_compress(conversation):
            return copy.deepcopy(conversation)

        messages = list(conversation.get("messages") or [])
        user_indexes = [
            index for index, message in enumerate(messages)
            if message.get("role") == "user"
        ]
        if len(user_indexes) <= self.keep_rounds:
            return copy.deepcopy(conversation)

        keep_from = user_indexes[-self.keep_rounds]
        old_messages = messages[:keep_from]
        recent_messages = messages[keep_from:]
        if not old_messages:
            return copy.deepcopy(conversation)

        result = copy.deepcopy(conversation)
        previous_summary = str(conversation.get("summary") or "")
        try:
            summary = str(summarize(copy.deepcopy(old_messages), previous_summary) or "").strip()
        except Exception:
            return copy.deepcopy(conversation)
        if not summary:
            return copy.deepcopy(conversation)
        result["summary"] = summary
        result["messages"] = recent_messages
        # state / result-set / pending actions are deliberately untouched.
        return result
