# -*- coding: utf-8 -*-
"""M1/S3 切片 1：ToolCallingPlanner 契约测试（Issue #16）。

替身 adapter/repo 验证提议边界：模型输出只是不可信提议，白名单、
预算与 fail-closed 全部在服务端裁决；不碰真实网络与 Provider。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.autonomy import planner as planner_mod
from app.ai.autonomy.planner import (
    FINISH_TOOL_NAME,
    PLAN_TOOL_NAME,
    PROPOSAL_TOOL_NAME,
    VERIFICATION_TOOL_NAME,
    PlannerProposalError,
    ToolCallingPlanner,
    proposal_tool_schemas,
    summarize_step_history,
)
from app.ai.autonomy.plans import PLAN_ACTION_KINDS
from app.ai.autonomy.repository import (
    AutonomyConflict,
    AutonomyValidationError,
)
from app.core.db.database import db, t_ai_autonomous_step


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
            error, self.error = self.error, None
            if isinstance(error, Exception):
                raise error
            raise RuntimeError(error)
        return self.results.pop(0)


class FakeRepo:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def propose_probe(self, owner, role, run_id, probe_id, params=None):
        self.calls.append(
            {"owner": owner, "role": role, "run_id": run_id,
             "probe_id": probe_id, "params": params},
        )
        if self.error is not None:
            raise self.error
        return {"id": "step-%d" % len(self.calls)}

    def propose_plan(self, owner, role, run_id, summary, actions):
        self.calls.append(
            {"owner": owner, "role": role, "run_id": run_id,
             "summary": summary, "actions": actions},
        )
        if self.error is not None:
            raise self.error
        return {"id": "step-%d" % len(self.calls)}

    def propose_verification(
        self, owner, role, run_id, probe_id, params=None,
    ):
        self.calls.append(
            {"owner": owner, "role": role, "run_id": run_id,
             "probe_id": probe_id, "params": params},
        )
        if self.error is not None:
            raise self.error
        return {"id": "step-%d" % len(self.calls)}

    def conclude_run(self, owner, role, run_id, outcome, evidence_ids):
        self.calls.append(
            {"owner": owner, "role": role, "run_id": run_id,
             "outcome": outcome, "evidence_ids": evidence_ids},
        )
        if self.error is not None:
            raise self.error
        return {"outcome": outcome, "already_concluded": False}


class VerificationBeforeWriteRepo(FakeRepo):
    """Model-phase fence used to reproduce an early verification proposal."""

    def propose_verification(
        self, owner, role, run_id, probe_id, params=None,
    ):
        raise AutonomyValidationError(
            "verification requires a prior succeeded write action",
        )


class OneShotConclusionRepo(FakeRepo):
    """Make the first conclusion citation fail, then accept the repair."""

    def __init__(self, error):
        super().__init__()
        self._error = error

    def conclude_run(self, owner, role, run_id, outcome, evidence_ids):
        self.calls.append({
            "owner": owner, "role": role, "run_id": run_id,
            "outcome": outcome, "evidence_ids": evidence_ids,
        })
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        return {"outcome": outcome, "already_concluded": False}


class RepairFailsThenFallbackRepo(FakeRepo):
    """Reject both provider citations, then accept the safe fallback."""

    def __init__(self, error):
        super().__init__()
        self._remaining_errors = 2
        self._error = error

    def conclude_run(self, owner, role, run_id, outcome, evidence_ids):
        self.calls.append({
            "owner": owner, "role": role, "run_id": run_id,
            "outcome": outcome, "evidence_ids": evidence_ids,
        })
        if self._remaining_errors:
            self._remaining_errors -= 1
            raise self._error
        return {"outcome": outcome, "already_concluded": False}


def make_context(repo, **budget_overrides):
    budget = {"remaining_loops": 5, "remaining_actions": 5}
    budget.update(budget_overrides)
    return {
        "run_id": "run-1",
        "owner": "admin",
        "role": "admin",
        "goal": "diagnose latency",
        "loops": 0,
        "repo": repo,
        "budget": budget,
        "history": [],
    }


def probe_call(probe_id="system.load", params=None):
    return FakeToolCall(
        PROPOSAL_TOOL_NAME, {"probe_id": probe_id, "params": params or {}},
    )


def make_planner(adapter):
    return ToolCallingPlanner(lambda: adapter)


def test_single_probe_proposal_goes_through_the_fenced_repo():
    repo = FakeRepo()
    adapter = FakeAdapter(results=[FakeChatResult([probe_call()])])
    planner = make_planner(adapter)

    proposed = planner(make_context(repo))

    assert proposed == ["step-1"]
    assert repo.calls == [{
        "owner": "admin", "role": "admin", "run_id": "run-1",
        "probe_id": "system.load", "params": {},
    }]
    request = adapter.requests[0]
    assert request["tool_choice"] == "required"
    tool_names = {
        tool["function"]["name"] for tool in request["tools"]
    }
    assert tool_names == {
        PROPOSAL_TOOL_NAME, PLAN_TOOL_NAME, VERIFICATION_TOOL_NAME,
        FINISH_TOOL_NAME,
    }
    plan_tool = next(
        tool for tool in request["tools"]
        if tool["function"]["name"] == PLAN_TOOL_NAME
    )
    kind_enum = plan_tool["function"]["parameters"]["properties"][
        "actions"
    ]["items"]["properties"]["kind"]["enum"]
    assert tuple(kind_enum) == PLAN_ACTION_KINDS
    assert "shell" not in kind_enum


def test_finish_tool_ends_the_loop_without_a_proposal():
    repo = FakeRepo()
    adapter = FakeAdapter(
        results=[FakeChatResult([FakeToolCall(FINISH_TOOL_NAME, {})])],
    )

    assert make_planner(adapter)(make_context(repo)) == []
    assert repo.calls == []


def test_missing_provider_configuration_fails_closed():
    from app.ai.provider_config import ProviderConfigError

    def factory():
        raise ProviderConfigError("所选模型服务未启用或配置不完整")

    planner = ToolCallingPlanner(factory)

    with pytest.raises(PlannerProposalError) as excinfo:
        planner(make_context(FakeRepo()))
    assert excinfo.value.reason == "provider_not_configured"


def test_provider_exception_fails_closed_without_half_steps():
    repo = FakeRepo()
    adapter = FakeAdapter(error=TimeoutError("provider timed out"))

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))
    assert excinfo.value.reason == "provider_call_failed"
    assert repo.calls == []


@pytest.mark.parametrize("result", [
    FakeChatResult([probe_call()], truncated=True),
    FakeChatResult([probe_call()], finish_reason="length"),
])
def test_truncated_provider_output_fails_closed(result):
    repo = FakeRepo()
    adapter = FakeAdapter(results=[result])

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))
    assert excinfo.value.reason == "provider_output_truncated"
    assert repo.calls == []


@pytest.mark.parametrize("calls", [
    (),
    (probe_call(), probe_call()),
])
def test_ambiguous_tool_calls_fail_closed(calls):
    repo = FakeRepo()
    adapter = FakeAdapter(results=[FakeChatResult(list(calls))])

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))
    assert excinfo.value.reason == "ambiguous_proposal"
    assert repo.calls == []


def test_unknown_tool_name_is_unsupported():
    repo = FakeRepo()
    adapter = FakeAdapter(
        results=[FakeChatResult([FakeToolCall("rm_rf", {})])],
    )

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))
    assert excinfo.value.reason == "unsupported_proposal"


def test_server_side_probe_validation_is_authoritative():
    repo = FakeRepo(error=AutonomyValidationError("unknown probe"))
    adapter = FakeAdapter(
        results=[FakeChatResult([probe_call("not.a.real.probe")])],
    )

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))
    assert excinfo.value.reason == "unsupported_proposal"


def test_conflicting_run_state_fails_closed():
    repo = FakeRepo(error=AutonomyConflict("run not active"))
    adapter = FakeAdapter(results=[FakeChatResult([probe_call()])])

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))
    assert excinfo.value.reason == "run_not_active"


def test_non_object_params_are_malformed():
    repo = FakeRepo()
    adapter = FakeAdapter(results=[FakeChatResult([
        FakeToolCall(
            PROPOSAL_TOOL_NAME, {"probe_id": "system.load", "params": "x"},
        ),
    ])])

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))
    assert excinfo.value.reason == "malformed_proposal"
    assert repo.calls == []


@pytest.mark.parametrize("budget", [
    {"remaining_actions": 0},
    {"remaining_loops": 0},
])
def test_exhausted_budget_never_calls_the_provider(budget):
    adapter = FakeAdapter(results=[FakeChatResult([probe_call()])])
    planner = make_planner(adapter)

    assert planner(make_context(FakeRepo(), **budget)) == []
    assert adapter.requests == []


def test_unsupported_tool_choice_degrades_once_then_decides():
    repo = FakeRepo()
    adapter = FakeAdapter(
        results=[FakeChatResult([probe_call()])],
        error="tool_choice is not supported by this provider",
    )

    assert make_planner(adapter)(make_context(repo)) == ["step-1"]
    assert [request["tool_choice"] for request in adapter.requests] == [
        "required", None,
    ]


def test_provider_response_error_does_not_trigger_tool_choice_retry():
    from app.ai.provider import ProviderResponseError

    repo = FakeRepo()
    adapter = FakeAdapter(
        error=ProviderResponseError("模型返回了无效的 Tool Call JSON 参数"),
    )

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))
    assert excinfo.value.reason == "provider_call_failed"
    assert len(adapter.requests) == 1


def test_tool_schemas_pin_the_probe_registry_enum():
    from app.ai.autonomy.actions import list_probe_ids

    schemas = proposal_tool_schemas()
    proposal = next(
        tool for tool in schemas
        if tool["function"]["name"] == PROPOSAL_TOOL_NAME
    )
    enum = proposal["function"]["parameters"]["properties"]["probe_id"]["enum"]
    assert enum == list_probe_ids()


@pytest.fixture
def step_session(tmp_path):
    engine = create_engine(
        "sqlite:///%s" % (tmp_path / "planner-history.db").as_posix(),
    )
    db.metadata.create_all(engine, tables=[t_ai_autonomous_step.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_step(session, seq, status, summary, note=""):
    session.add(t_ai_autonomous_step(
        id="step-%d" % seq, run_id="run-1", kind="action",
        status=status, seq=seq, summary=summary, action_json="{}",
        action_digest="d", note=note,
    ))
    session.commit()


def test_history_is_bounded_ordered_and_sanitized(step_session):
    for seq in range(1, planner_mod.HISTORY_STEP_LIMIT + 4):
        _add_step(step_session, seq, "succeeded", "probe %d" % seq)
    _add_step(step_session, 99, "failed", "ignored: wrong run")
    step = step_session.query(t_ai_autonomous_step).filter_by(
        id="step-99",
    ).one()
    step.run_id = "run-2"
    step_session.commit()
    _add_step(step_session, 100, "failed", "red\x1b[31mteam", "bad")

    history = summarize_step_history(step_session, "run-1")

    assert len(history) == planner_mod.HISTORY_STEP_LIMIT
    # run-1 共 12 条（seq 1..11 + 100）；取最近 8 条后时序正排首条为 #5。
    assert history[0].startswith("#5 succeeded")
    assert history[-1].startswith("#100 failed")
    assert "\x1b" not in "".join(history)
    assert all(len(entry) <= planner_mod.HISTORY_ENTRY_CHARS
               for entry in history)
    assert all("run-2" not in entry for entry in history)


def test_history_entries_are_capped_per_line(step_session):
    _add_step(step_session, 1, "succeeded", "x" * 5000)

    history = summarize_step_history(step_session, "run-1")

    assert len(history) == 1
    assert len(history[0]) == planner_mod.HISTORY_ENTRY_CHARS


# ---------------------------------------------------------------------------
# S3 切片 2：propose_plan 工具分发——计划也只是不可信提议
# ---------------------------------------------------------------------------

def plan_call(summary="restart service", actions=None):
    return FakeToolCall(PLAN_TOOL_NAME, {
        "summary": summary,
        "actions": actions or [
            {"kind": "systemd", "params": {
                "operation": "restart", "unit": "nginx",
            }},
        ],
    })


def test_plan_proposal_goes_through_the_fenced_repo():
    repo = FakeRepo()
    adapter = FakeAdapter(results=[FakeChatResult([plan_call()])])

    proposed = make_planner(adapter)(make_context(repo))

    assert proposed == ["step-1"]
    assert repo.calls == [{
        "owner": "admin", "role": "admin", "run_id": "run-1",
        "summary": "restart service",
        "actions": [{"kind": "systemd", "params": {
            "operation": "restart", "unit": "nginx",
        }}],
    }]


@pytest.mark.parametrize("arguments", [
    {"summary": "", "actions": [{"kind": "systemd", "params": {}}]},
    {"summary": "fix", "actions": []},
    {"summary": "fix", "actions": "not-a-list"},
    {"summary": "fix", "actions": ["not-an-object"]},
    {"summary": "fix", "actions": [{"kind": "systemd", "params": "x"}]},
])
def test_malformed_plan_shapes_fail_closed_without_repo_calls(arguments):
    repo = FakeRepo()
    adapter = FakeAdapter(
        results=[FakeChatResult([FakeToolCall(PLAN_TOOL_NAME, arguments)])],
    )

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))

    assert excinfo.value.reason == "malformed_proposal"
    assert repo.calls == []


def test_plan_validation_error_maps_to_unsupported_proposal():
    repo = FakeRepo(error=AutonomyValidationError("denied by policy"))
    adapter = FakeAdapter(results=[FakeChatResult([plan_call()])])

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))

    assert excinfo.value.reason == "unsupported_proposal"


def test_active_plan_conflict_maps_to_plan_conflict():
    repo = FakeRepo(error=AutonomyConflict(
        "a plan already requires a decision or execution",
    ))
    adapter = FakeAdapter(results=[FakeChatResult([plan_call()])])

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))

    assert excinfo.value.reason == "plan_conflict"


def test_inactive_run_conflict_still_maps_to_run_not_active():
    repo = FakeRepo(error=AutonomyConflict(
        "steps can only be proposed while the run is active",
    ))
    adapter = FakeAdapter(results=[FakeChatResult([plan_call()])])

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))

    assert excinfo.value.reason == "run_not_active"


# ---------------------------------------------------------------------------
# S3 切片 4：验证提案工具 + finish 附带终局结论
# ---------------------------------------------------------------------------

def verification_call(probe_id="system.load", params=None):
    return FakeToolCall(
        VERIFICATION_TOOL_NAME,
        {"probe_id": probe_id, "params": params or {}},
    )


def finish_conclusion_call(outcome="resolved", evidence_ids=("ev-1",)):
    return FakeToolCall(FINISH_TOOL_NAME, {
        "outcome": outcome, "evidence_ids": list(evidence_ids),
    })


def test_verification_proposal_goes_through_the_fenced_repo():
    repo = FakeRepo()
    adapter = FakeAdapter(results=[FakeChatResult([verification_call()])])

    proposed = make_planner(adapter)(make_context(repo))

    assert proposed == ["step-1"]
    assert repo.calls == [{
        "owner": "admin", "role": "admin", "run_id": "run-1",
        "probe_id": "system.load", "params": {},
    }]


def test_verification_without_prior_write_maps_to_unsupported():
    repo = FakeRepo(error=AutonomyValidationError(
        "verification requires a prior succeeded write action",
    ))
    adapter = FakeAdapter(results=[FakeChatResult([verification_call()])])

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))

    assert excinfo.value.reason == "unsupported_proposal"


def test_deepseek_early_verification_is_repaired_to_plan():
    """A rejected phase choice gets one forced, side-effect-free repair."""
    repo = VerificationBeforeWriteRepo()
    adapter = FakeAdapter(results=[
        FakeChatResult([verification_call()]),
        FakeChatResult([plan_call("restart service")]),
    ])

    assert make_planner(adapter)(make_context(repo)) == ["step-1"]
    assert repo.calls == [{
        "owner": "admin", "role": "admin", "run_id": "run-1",
        "summary": "restart service",
        "actions": [{"kind": "systemd", "params": {
            "operation": "restart", "unit": "nginx",
        }}],
    }]
    assert len(adapter.requests) == 2
    repair = adapter.requests[1]
    assert repair["tool_choice"] == {
        "type": "function",
        "function": {"name": PLAN_TOOL_NAME},
    }
    assert "服务端合同" in repair["messages"][-1]["content"]


def test_investigation_handoff_forces_plan_after_probe_phase():
    """A provider cannot loop on probes after the server phase handoff."""
    repo = FakeRepo()
    adapter = FakeAdapter(results=[
        FakeChatResult([probe_call()]),
        FakeChatResult([plan_call("restart service")]),
    ])

    context = make_context(repo, remaining_actions=10)
    context["require_plan"] = True

    assert make_planner(adapter)(context) == ["step-1"]
    assert repo.calls == [{
        "owner": "admin", "role": "admin", "run_id": "run-1",
        "summary": "restart service",
        "actions": [{"kind": "systemd", "params": {
            "operation": "restart", "unit": "nginx",
        }}],
    }]
    assert adapter.requests[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": PLAN_TOOL_NAME},
    }
    assert adapter.requests[1]["tool_choice"] == {
        "type": "function",
        "function": {"name": PLAN_TOOL_NAME},
    }


def test_verification_schema_pins_the_probe_registry_enum():
    from app.ai.autonomy.actions import list_probe_ids

    schemas = proposal_tool_schemas()
    tool = next(
        item for item in schemas
        if item["function"]["name"] == VERIFICATION_TOOL_NAME
    )
    enum = tool["function"]["parameters"]["properties"]["probe_id"]["enum"]
    assert enum == list_probe_ids()


def test_finish_with_conclusion_calls_conclude_run():
    repo = FakeRepo()
    adapter = FakeAdapter(results=[FakeChatResult([
        finish_conclusion_call("resolved", ("ev-1", "ev-2")),
    ])])

    assert make_planner(adapter)(make_context(repo)) == []
    assert repo.calls == [{
        "owner": "admin", "role": "admin", "run_id": "run-1",
        "outcome": "resolved", "evidence_ids": ["ev-1", "ev-2"],
    }]


@pytest.mark.parametrize("arguments", [
    {"outcome": "success", "evidence_ids": ["ev-1"]},
    {"outcome": "resolved", "evidence_ids": []},
    {"outcome": "resolved", "evidence_ids": "ev-1"},
    {"outcome": "resolved"},
    {"evidence_ids": ["ev-1"]},
])
def test_malformed_conclusions_fail_closed_without_repo_calls(arguments):
    repo = FakeRepo()
    adapter = FakeAdapter(results=[FakeChatResult([
        FakeToolCall(FINISH_TOOL_NAME, arguments),
    ])])

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))

    assert excinfo.value.reason == "malformed_proposal"
    assert repo.calls == []


def test_conclude_validation_error_maps_to_malformed_proposal():
    repo = FakeRepo(error=AutonomyValidationError(
        "conclusion may only cite same-run evidence",
    ))
    adapter = FakeAdapter(results=[FakeChatResult([
        finish_conclusion_call("resolved", ("foreign-ev",)),
    ])])

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))

    assert excinfo.value.reason == "malformed_proposal"


def test_conclude_validation_error_gets_one_finish_repair():
    repo = OneShotConclusionRepo(
        AutonomyValidationError(
            "conclusion may only cite same-run evidence",
        ),
    )
    adapter = FakeAdapter(results=[
        FakeChatResult([
            finish_conclusion_call("resolved", ("artifact-not-evidence",)),
        ]),
        FakeChatResult([
            finish_conclusion_call("inconclusive", ("ev-1",)),
        ]),
    ])

    assert make_planner(adapter)(make_context(repo)) == []
    assert len(repo.calls) == 2
    assert adapter.requests[1]["tool_choice"] == {
        "type": "function",
        "function": {"name": FINISH_TOOL_NAME},
    }
    assert "artifact ID" in adapter.requests[1]["messages"][-1]["content"]


def test_finish_repair_falls_back_to_safe_inconclusive_evidence():
    repo = RepairFailsThenFallbackRepo(
        AutonomyValidationError(
            "conclusion may only cite same-run evidence",
        ),
    )
    adapter = FakeAdapter(results=[
        FakeChatResult([
            finish_conclusion_call("resolved", ("artifact-id",)),
        ]),
        FakeChatResult([
            finish_conclusion_call("resolved", ("still-artifact-id",)),
        ]),
    ])
    context = make_context(repo)
    context["evidence"] = [
        "id=action-1 | kind=action_observation | summary=write succeeded",
        "id=verify-1 | kind=verification_observation | summary=target verified",
    ]

    assert make_planner(adapter)(context) == []
    assert repo.calls[-1]["outcome"] == "inconclusive"
    assert repo.calls[-1]["evidence_ids"] == ["verify-1"]
    assert len(adapter.requests) == 2


def test_finish_schema_pins_same_run_evidence_ids():
    context = make_context(FakeRepo())
    context["evidence"] = [
        "id=verify-1 | kind=verification_observation | summary=verified",
    ]

    finish = next(
        item for item in proposal_tool_schemas(context)
        if item["function"]["name"] == FINISH_TOOL_NAME
    )
    assert finish["function"]["parameters"]["properties"][
        "evidence_ids"
    ]["items"]["enum"] == ["verify-1"]


def test_conclude_conflict_maps_to_run_not_active():
    repo = FakeRepo(error=AutonomyConflict(
        "outcome can only be concluded while the run is active",
    ))
    adapter = FakeAdapter(results=[FakeChatResult([
        finish_conclusion_call("inconclusive", ("ev-1",)),
    ])])

    with pytest.raises(PlannerProposalError) as excinfo:
        make_planner(adapter)(make_context(repo))

    assert excinfo.value.reason == "run_not_active"
