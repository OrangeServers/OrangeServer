# -*- coding: utf-8 -*-
"""M1/S1: Run/Step 合法状态转换契约测试（Issue #11）。"""
import pytest

from app.ai.autonomy.state import (
    ACTIVE_RUN_STATUSES,
    CANONICAL_RUN_MODES,
    RUN_TRANSITIONS,
    STEP_TRANSITIONS,
    TERMINAL_RUN_STATUSES,
    TERMINAL_STEP_STATUSES,
    AutonomyStateError,
    RunMode,
    RunStatus,
    StepStatus,
    assert_run_transition,
    assert_step_transition,
    normalize_run_mode,
)


LEGAL_RUN_TRANSITIONS = [
    ("draft", "queued"),
    ("draft", "cancelled"),
    ("draft", "expired"),
    ("queued", "running"),
    ("queued", "waiting_approval"),
    ("queued", "cancelled"),
    ("queued", "expired"),
    ("queued", "failed"),
    ("running", "waiting_approval"),
    ("running", "recovering"),
    ("running", "needs_attention"),
    ("running", "completed"),
    ("running", "failed"),
    ("running", "cancelled"),
    ("waiting_approval", "queued"),
    ("waiting_approval", "running"),
    ("waiting_approval", "needs_attention"),
    ("waiting_approval", "cancelled"),
    ("waiting_approval", "failed"),
    ("waiting_approval", "expired"),
    ("recovering", "running"),
    ("recovering", "waiting_approval"),
    ("recovering", "needs_attention"),
    ("recovering", "failed"),
    ("recovering", "cancelled"),
    ("needs_attention", "running"),
    ("needs_attention", "cancelled"),
    ("needs_attention", "failed"),
]


ILLEGAL_RUN_TRANSITIONS = [
    # 终态不能回到任何活动态。
    ("completed", "running"),
    ("completed", "queued"),
    ("failed", "queued"),
    ("failed", "running"),
    ("cancelled", "queued"),
    ("expired", "queued"),
    # 活动态也不能跳跃到未声明的组合。
    ("draft", "running"),
    ("draft", "completed"),
    ("queued", "completed"),
    ("queued", "needs_attention"),
    ("queued", "recovering"),
    ("running", "queued"),
    ("running", "expired"),
    ("waiting_approval", "waiting_approval"),
    ("recovering", "queued"),
    ("needs_attention", "queued"),
    ("needs_attention", "completed"),
]


LEGAL_STEP_TRANSITIONS = [
    ("proposed", "waiting_approval"),
    ("proposed", "approved"),
    ("proposed", "running"),
    ("proposed", "skipped"),
    ("proposed", "failed"),
    ("proposed", "cancelled"),
    ("waiting_approval", "approved"),
    ("waiting_approval", "failed"),
    ("waiting_approval", "skipped"),
    ("waiting_approval", "cancelled"),
    # Guardian 复核中的计划与复核中断的回退（S3 切片 3）。
    ("waiting_approval", "running"),
    ("running", "waiting_approval"),
    ("approved", "running"),
    ("approved", "failed"),
    ("approved", "cancelled"),
    ("running", "succeeded"),
    ("running", "failed"),
    ("running", "outcome_unknown"),
    ("running", "cancelled"),
    ("outcome_unknown", "succeeded"),
    ("outcome_unknown", "failed"),
]


ILLEGAL_STEP_TRANSITIONS = [
    # 拒绝不是独立状态：waiting_approval 只能落 failed 并附 note。
    ("waiting_approval", "rejected"),
    ("waiting_approval", "succeeded"),
    # 终态不可复活。
    ("succeeded", "running"),
    ("failed", "running"),
    ("skipped", "running"),
    ("cancelled", "running"),
    # outcome_unknown 绝不自动重放。
    ("outcome_unknown", "running"),
    ("outcome_unknown", "cancelled"),
    # proposed 不能直接成功。
    ("proposed", "succeeded"),
    ("approved", "succeeded"),
]


@pytest.mark.parametrize("current,target", LEGAL_RUN_TRANSITIONS)
def test_legal_run_transitions_pass(current, target):
    assert assert_run_transition(current, target) == RunStatus(target)


@pytest.mark.parametrize("current,target", ILLEGAL_RUN_TRANSITIONS)
def test_illegal_run_transitions_raise(current, target):
    if target not in {status.value for status in RunStatus}:
        with pytest.raises(AutonomyStateError):
            assert_run_transition(current, target)
        return
    with pytest.raises(AutonomyStateError):
        assert_run_transition(current, target)


@pytest.mark.parametrize("current,target", LEGAL_STEP_TRANSITIONS)
def test_legal_step_transitions_pass(current, target):
    assert assert_step_transition(current, target) == StepStatus(target)


@pytest.mark.parametrize("current,target", ILLEGAL_STEP_TRANSITIONS)
def test_illegal_step_transitions_raise(current, target):
    with pytest.raises(AutonomyStateError):
        assert_step_transition(current, target)


def test_unknown_status_values_are_rejected():
    with pytest.raises(AutonomyStateError):
        assert_run_transition("draft", "exploded")
    with pytest.raises(AutonomyStateError):
        assert_run_transition("exploded", "queued")
    with pytest.raises(AutonomyStateError):
        assert_step_transition("proposed", "rejected")
    with pytest.raises(AutonomyStateError):
        assert_step_transition("teleported", "running")


def test_terminal_statuses_have_no_outgoing_edges():
    for status in TERMINAL_RUN_STATUSES:
        assert RUN_TRANSITIONS[status] == set()
    for status in TERMINAL_STEP_STATUSES:
        assert STEP_TRANSITIONS[status] == set()


def test_active_run_statuses_exclude_terminal_and_match_transition_table():
    assert ACTIVE_RUN_STATUSES.isdisjoint(TERMINAL_RUN_STATUSES)
    assert ACTIVE_RUN_STATUSES | TERMINAL_RUN_STATUSES == set(RunStatus)


def test_every_run_status_appears_in_exactly_one_transition_bucket():
    assert set(RUN_TRANSITIONS) == set(RunStatus)
    assert set(STEP_TRANSITIONS) == set(StepStatus)


def test_permission_profiles_use_the_four_product_modes():
    assert CANONICAL_RUN_MODES == (
        RunMode.ASK,
        RunMode.AI_REVIEW,
        RunMode.AUTO,
        RunMode.CUSTOM,
    )
    assert [mode.value for mode in CANONICAL_RUN_MODES] == [
        "ask", "ai_review", "auto", "custom",
    ]


@pytest.mark.parametrize(("legacy", "canonical"), [
    ("assisted", RunMode.ASK),
    ("lab_autonomous", RunMode.AUTO),
    ("read_only", RunMode.READ_ONLY),
])
def test_legacy_run_modes_have_explicit_runtime_mapping(legacy, canonical):
    assert normalize_run_mode(legacy) is canonical
