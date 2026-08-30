"""OpenAI-compatible model provider primitives.

The supported vendors expose the same Chat Completions shape, so the Agent
uses one adapter and keeps vendor differences in data-only presets. The
adapter intentionally has no Flask or database dependency and accepts a
client factory, which keeps provider behavior testable without network calls.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class ProviderPreset:
    """Display metadata and the default OpenAI-compatible endpoint."""

    code: str
    name: str
    base_url: str
    note: str = ""


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        code="openai", name="OpenAI", base_url="https://api.openai.com/v1"
    ),
    "anthropic": ProviderPreset(
        code="anthropic", name="Anthropic", base_url="https://api.anthropic.com/v1",
        note="原生 API 非 OpenAI 兼容，当前仅支持通过中转代理（如 OpenRouter）接入",
    ),
    "xai": ProviderPreset(
        code="xai", name="xAI (Grok)", base_url="https://api.x.ai/v1"
    ),
    "deepseek": ProviderPreset(
        code="deepseek", name="DeepSeek", base_url="https://api.deepseek.com"
    ),
    "minimax": ProviderPreset(
        code="minimax", name="MiniMax", base_url="https://api.minimaxi.com/v1"
    ),
    "kimi": ProviderPreset(
        code="kimi", name="Kimi", base_url="https://api.moonshot.cn/v1"
    ),
    "qwen": ProviderPreset(
        code="qwen",
        name="Qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "glm": ProviderPreset(
        code="glm",
        name="GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4/",
    ),
    "siliconflow": ProviderPreset(
        code="siliconflow",
        name="硅基流动",
        base_url="https://api.siliconflow.cn/v1",
    ),
}


@dataclass(frozen=True)
class ProviderToolCall:
    """A normalized tool call returned by any compatible provider."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatResult:
    """Normalized result consumed by the Agent runner."""

    content: str
    tool_calls: tuple[ProviderToolCall, ...]
    used_stream: bool
    content_deltas: tuple[str, ...] = ()
    usage: Mapping[str, int] | None = None
    finish_reason: Optional[str] = None
    latency_ms: int = 0
    truncated: bool = False
    reasoning_content: str = ""


class ProviderResponseError(RuntimeError):
    """The provider returned a response that cannot be executed safely."""


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _merge_fragment(current: str, fragment: Optional[str]) -> str:
    """Join stream fragments and tolerate providers that repeat values."""

    if not fragment:
        return current
    if not current:
        return fragment
    if fragment.startswith(current):
        return fragment
    if current.endswith(fragment):
        return current
    return current + fragment


def _decode_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProviderResponseError(
            "模型返回了无效的 Tool Call JSON 参数"
        ) from exc
    if not isinstance(decoded, dict):
        raise ProviderResponseError("模型 Tool Call 参数必须是 JSON object")
    return decoded


class OpenAICompatibleAdapter:
    """Small Chat Completions adapter shared by all supported vendors.

    Streaming is attempted first so fragmented tool calls are handled in the
    normal path. If a compatible vendor rejects or corrupts its streaming
    response, the same request is retried once with ``stream=False``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        extra_body: Optional[Mapping[str, Any]] = None,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not base_url:
            raise ValueError("base_url is required")
        if not model:
            raise ValueError("model is required")

        self.model = model
        self.extra_body = dict(extra_body or {})
        self._client_factory = client_factory or self._default_client_factory
        self._client = self._client_factory(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=timeout,
        )

    @staticmethod
    def _default_client_factory(**kwargs: Any) -> Any:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "缺少 openai 依赖，请安装 backend/requirements.txt"
            ) from exc
        return OpenAI(**kwargs)

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> ChatResult:
        request = self._request_payload(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )
        started_at = time.perf_counter()
        self._stream_emitted = False
        try:
            stream = self._client.chat.completions.create(
                **request,
                stream=True,
            )
            result = self._consume_stream(stream, on_delta=on_delta)
        except Exception:
            if getattr(self, "_stream_emitted", False):
                raise
            response = self._client.chat.completions.create(
                **request,
                stream=False,
            )
            result = self._consume_non_stream(response)
            if on_delta and result.content:
                on_delta(result.content)
        latency_ms = max(0, round((time.perf_counter() - started_at) * 1000))
        return replace(
            result,
            latency_ms=latency_ms,
            truncated=result.finish_reason == "length",
        )

    @staticmethod
    def _usage(value: Any) -> dict[str, int]:
        result = {}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            raw = _value(value, name)
            if raw is None:
                continue
            try:
                result[name] = max(0, int(raw))
            except (TypeError, ValueError):
                continue
        return result

    def list_models(self) -> Any:
        """Return the provider's OpenAI-compatible model listing response."""
        return self._client.models.list()

    def _request_payload(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Optional[Sequence[Mapping[str, Any]]],
        tool_choice: Optional[Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
        }
        if tools:
            payload["tools"] = list(tools)
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if self.extra_body:
            payload["extra_body"] = dict(self.extra_body)
        return payload

    def _consume_stream(
        self,
        chunks: Iterable[Any],
        *,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> ChatResult:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        saw_choice = False
        finish_reason = None
        usage: dict[str, int] = {}
        for chunk in chunks:
            chunk_usage = self._usage(_value(chunk, "usage"))
            if chunk_usage:
                usage = chunk_usage
            choices = _value(chunk, "choices", ()) or ()
            if not choices:
                continue
            saw_choice = True
            choice = choices[0]
            finish_reason = _value(choice, "finish_reason") or finish_reason
            delta = _value(choice, "delta")
            if delta is None:
                continue

            content = _value(delta, "content")
            if isinstance(content, str) and content:
                text_parts.append(content)
                self._stream_emitted = True
                if on_delta:
                    on_delta(content)

            reasoning_content = _value(delta, "reasoning_content")
            if isinstance(reasoning_content, str) and reasoning_content:
                reasoning_parts.append(reasoning_content)
                # A streamed reasoning token means the provider has started
                # producing this response. Retrying the request as non-stream
                # could duplicate a billed/tool-producing model turn.
                self._stream_emitted = True

            for position, tool_chunk in enumerate(
                _value(delta, "tool_calls", ()) or ()
            ):
                index = _value(tool_chunk, "index", position)
                try:
                    index = int(index)
                except (TypeError, ValueError):
                    index = position
                part = tool_parts.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )
                function = _value(tool_chunk, "function")
                part["id"] = _merge_fragment(
                    part["id"], _value(tool_chunk, "id")
                )
                if function is not None:
                    part["name"] = _merge_fragment(
                        part["name"], _value(function, "name")
                    )
                    part["arguments"] = _merge_fragment(
                        part["arguments"], _value(function, "arguments")
                    )

        if not saw_choice:
            raise ProviderResponseError("模型流式响应未返回 choices")

        normalized_calls: list[ProviderToolCall] = []
        for index, part in sorted(tool_parts.items()):
            if not part["name"]:
                raise ProviderResponseError("模型 Tool Call 缺少函数名称")
            normalized_calls.append(
                ProviderToolCall(
                    id=part["id"] or f"call_{index}",
                    name=part["name"],
                    arguments=_decode_arguments(part["arguments"]),
                )
            )
        return ChatResult(
            content="".join(text_parts),
            content_deltas=tuple(text_parts),
            tool_calls=tuple(normalized_calls),
            used_stream=True,
            usage=usage,
            finish_reason=finish_reason,
            reasoning_content="".join(reasoning_parts),
        )

    def _consume_non_stream(self, response: Any) -> ChatResult:
        choices = _value(response, "choices", ()) or ()
        if not choices:
            raise ProviderResponseError("模型未返回 choices")
        choice = choices[0]
        message = _value(choice, "message")
        if message is None:
            raise ProviderResponseError("模型未返回 message")

        content = _value(message, "content") or ""
        if not isinstance(content, str):
            content = str(content)
        reasoning_content = _value(message, "reasoning_content") or ""
        if not isinstance(reasoning_content, str):
            reasoning_content = str(reasoning_content)
        tool_calls: list[ProviderToolCall] = []
        for position, tool_call in enumerate(
            _value(message, "tool_calls", ()) or ()
        ):
            function = _value(tool_call, "function")
            if function is None:
                continue
            name = _value(function, "name") or ""
            if not name:
                raise ProviderResponseError("模型 Tool Call 缺少函数名称")
            tool_calls.append(
                ProviderToolCall(
                    id=_value(tool_call, "id") or f"call_{position}",
                    name=name,
                    arguments=_decode_arguments(
                        _value(function, "arguments")
                    ),
                )
            )
        return ChatResult(
            content=content,
            content_deltas=(content,) if content else (),
            tool_calls=tuple(tool_calls),
            used_stream=False,
            usage=self._usage(_value(response, "usage")),
            finish_reason=_value(choice, "finish_reason"),
            reasoning_content=reasoning_content,
        )
