# -*- coding: utf-8 -*-
"""M1/S3 切片 3：PlanGuardian 契约测试（Issue #16 交付物 3）。

替身 adapter 验证独立复核边界：Guardian 只能 approve/reject/escalate
一个 ask；未配置、失败、截断、歧义、畸形输出一律 escalate 回人工。
同一 Provider 账号复用，但每次复核都是全新、独立的请求上下文：
消息里绝不出现凭据、原始日志、完整提示词或历史观察。
"""
import pytest

from app.ai.autonomy import guardian as guardian_mod
from app.ai.autonomy.guardian import (
    GUARDIAN_APPROVE,
    GUARDIAN_ESCALATE,
    GUARDIAN_REJECT,
    GUARDIAN_TOOL_NAME,
    PlanGuardian,
    guardian_tool_schemas,
    make_default_guardian,
)
from app.core import config


class FakeToolCall:
    def __init__(self, name, arguments, call_id="call-1"):
        self.id = call_id
        self.name = name
        self.arguments = arguments


class FakeChatResult:
    def __init__(self, tool_calls=(), truncated=False, finish_reason="stop"):
        self.tool_calls = tuple(tool_calls)
        self.truncated = truncated
        self.finish_reason = finish_reason


class FakeAdapter:
    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.requests = []

    def complete(self, *, messages, tools=None, tool_choice=None, **kwargs):
        self.requests.append(
            {"messages": messages, "tools": tools, "tool_choice": tool_choice},
        )
        if self.error is not None:
            raise self.error
        return self.results.pop(0)


class ConfigErrorFactory:
    """调用即抛 ProviderConfigError，模拟 Provider 未配置。"""

    def __call__(self):
        from app.ai.provider_config import ProviderConfigError

        raise ProviderConfigError("provider not configured")


def decision_call(decision, reason="bounded and aligned"):
    return FakeToolCall(
        GUARDIAN_TOOL_NAME, {"decision": decision, "reason": reason},
    )


def make_context(**overrides):
    context = {
        "goal": "diagnose latency",
        "host_alias": "web-01",
        "environment": "production",
        "summary": "restart nginx",
        "snapshot": {
            "summary": "restart nginx",
            "actions": [
                {
                    "kind": "systemd",
                    "parameters": {
                        "operation": "restart", "unit": "nginx",
                    },
                },
            ],
        },
    }
    context.update(overrides)
    return context


def make_guardian(adapter):
    return PlanGuardian(lambda: adapter)


def test_approve_and_reject_pass_through_with_reason():
    adapter = FakeAdapter(results=[
        FakeChatResult([decision_call(GUARDIAN_APPROVE, "aligned")]),
    ])

    result = make_guardian(adapter)(make_context())

    assert result == {"decision": "approve", "reason": "aligned"}

    adapter = FakeAdapter(results=[
        FakeChatResult([decision_call(GUARDIAN_REJECT, "scope drift")]),
    ])
    result = make_guardian(adapter)(make_context())
    assert result == {"decision": "reject", "reason": "scope drift"}


def test_review_uses_a_separate_single_tool_request_context():
    """同一 Provider 账号、独立请求上下文：全新消息、单工具强制调用。"""
    adapter = FakeAdapter(results=[
        FakeChatResult([decision_call(GUARDIAN_APPROVE)]),
    ])

    make_guardian(adapter)(make_context())

    request = adapter.requests[0]
    assert request["tool_choice"] == "required"
    tool_names = {tool["function"]["name"] for tool in request["tools"]}
    assert tool_names == {GUARDIAN_TOOL_NAME}
    # 独立上下文：只有 system+user 两条消息，没有任何历史观察。
    assert [m["role"] for m in request["messages"]] == ["system", "user"]
    text = " ".join(m["content"] for m in request["messages"])
    lowered = text.lower()
    # 凭据、原始日志与调查历史永不进入 Guardian 消息。
    for marker in (
        "password", "secret", "token", "credential", "private_key",
    ):
        assert marker not in lowered
    assert "观察" not in text
    assert "history" not in lowered
    assert "diagnose latency" in text
    assert "web-01" in text
    assert "restart nginx" in text
    # 动作白名单参数可见；凭据引用与主机地址不可见。
    assert "unit=nginx" in text
    assert "system_user" not in text
    assert "203.0.113" not in text


def test_message_bounds_goal_and_plan_text():
    adapter = FakeAdapter(results=[
        FakeChatResult([decision_call(GUARDIAN_ESCALATE, "unsure")]),
    ])

    make_guardian(adapter)(make_context(
        goal="g" * 5000, summary="s" * 5000,
        snapshot={"summary": "s" * 5000, "actions": [
            {"kind": "shell", "parameters": {"command": "x" * 5000}},
        ] * 50},
    ))

    text = " ".join(
        m["content"] for m in adapter.requests[0]["messages"]
    )
    assert len(text) < 8000


def test_unconfigured_provider_escalates_to_human():
    result = PlanGuardian(ConfigErrorFactory())(make_context())

    assert result["decision"] == GUARDIAN_ESCALATE
    assert result["reason"] == "provider_not_configured"


def test_provider_failure_escalates_to_human():
    adapter = FakeAdapter(error=RuntimeError("boom"))

    result = make_guardian(adapter)(make_context())

    assert result["decision"] == GUARDIAN_ESCALATE
    assert result["reason"] == "provider_call_failed"


def test_truncated_output_escalates_to_human():
    adapter = FakeAdapter(results=[
        FakeChatResult([decision_call(GUARDIAN_APPROVE)], truncated=True),
    ])

    result = make_guardian(adapter)(make_context())

    assert result["decision"] == GUARDIAN_ESCALATE
    assert result["reason"] == "provider_output_truncated"


def test_length_finish_reason_escalates_to_human():
    adapter = FakeAdapter(results=[
        FakeChatResult(
            [decision_call(GUARDIAN_APPROVE)], finish_reason="length",
        ),
    ])

    result = make_guardian(adapter)(make_context())

    assert result["decision"] == GUARDIAN_ESCALATE
    assert result["reason"] == "provider_output_truncated"


@pytest.mark.parametrize("tool_calls", [
    (),
    (decision_call(GUARDIAN_APPROVE), decision_call(GUARDIAN_REJECT)),
    (FakeToolCall("propose_probe", {}),),
])
def test_ambiguous_output_escalates_to_human(tool_calls):
    adapter = FakeAdapter(results=[FakeChatResult(tool_calls)])

    result = make_guardian(adapter)(make_context())

    assert result["decision"] == GUARDIAN_ESCALATE
    assert result["reason"] == "ambiguous_output"


@pytest.mark.parametrize("decision", ["", "allow", "deny", "approve_all"])
def test_invalid_decision_value_escalates_to_human(decision):
    """Guardian 绝不能输出 allow/deny：它的词汇表只有三种意见。"""
    adapter = FakeAdapter(results=[
        FakeChatResult([decision_call(decision)]),
    ])

    result = make_guardian(adapter)(make_context())

    assert result["decision"] == GUARDIAN_ESCALATE
    assert result["reason"] == "malformed_output"


def test_tool_schemas_lock_the_decision_vocabulary():
    schemas = guardian_tool_schemas()

    assert len(schemas) == 1
    tool = schemas[0]["function"]
    assert tool["name"] == GUARDIAN_TOOL_NAME
    enum = tool["parameters"]["properties"]["decision"]["enum"]
    assert enum == [GUARDIAN_APPROVE, GUARDIAN_REJECT, GUARDIAN_ESCALATE]


def test_make_default_guardian_is_off_by_default(monkeypatch):
    monkeypatch.setattr(config, "AI_AUTONOMY_GUARDIAN_ENABLED", False)

    assert make_default_guardian() is None


def test_make_default_guardian_returns_guardian_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "AI_AUTONOMY_GUARDIAN_ENABLED", True)

    guardian = make_default_guardian()

    assert isinstance(guardian, PlanGuardian)
