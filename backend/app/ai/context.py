"""Conversation context-window modes and provider capability validation."""
from __future__ import annotations

from typing import Any


STANDARD_CONTEXT_MODE = "standard_256k"
DEEP_CONTEXT_MODE = "deep_diagnostic_1m"
STANDARD_CONTEXT_TOKENS = 256 * 1024
DEEP_CONTEXT_TOKENS = 1024 * 1024
CONTEXT_WINDOWS = frozenset({STANDARD_CONTEXT_TOKENS, DEEP_CONTEXT_TOKENS})


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
