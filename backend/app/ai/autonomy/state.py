# -*- coding: utf-8 -*-
"""M1/S1: AI 自治领域模型的状态枚举与合法转换契约。

状态集合来自 docs/ai/ROADMAP.md 的最小领域模型。本模块只定义纯函数
契约，不依赖数据库；repository 层落库前必须通过这里校验。

安全边界：
- 转换表是白名单；未列出的组合一律非法。
- 终态（completed/failed/cancelled/expired 与 Step 终态）没有任何
  出边，审批或恢复都不能把终态对象拉回活动状态。
"""
from enum import Enum


class AutonomyStateError(Exception):
    """非法状态转换或非法状态值。"""


class RunStatus(str, Enum):
    DRAFT = 'draft'
    QUEUED = 'queued'
    RUNNING = 'running'
    WAITING_APPROVAL = 'waiting_approval'
    RECOVERING = 'recovering'
    NEEDS_ATTENTION = 'needs_attention'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'


class RunOutcome(str, Enum):
    RESOLVED = 'resolved'
    NOT_RESOLVED = 'not_resolved'
    INCONCLUSIVE = 'inconclusive'


class StepStatus(str, Enum):
    PROPOSED = 'proposed'
    WAITING_APPROVAL = 'waiting_approval'
    APPROVED = 'approved'
    RUNNING = 'running'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    SKIPPED = 'skipped'
    OUTCOME_UNKNOWN = 'outcome_unknown'
    CANCELLED = 'cancelled'


class StepKind(str, Enum):
    PLAN = 'plan'
    ACTION = 'action'
    VERIFICATION = 'verification'


class RunMode(str, Enum):
    ASK = 'ask'
    AI_REVIEW = 'ai_review'
    AUTO = 'auto'
    CUSTOM = 'custom'

    # Persisted compatibility values from the unpublished v1 graph.
    READ_ONLY = 'read_only'
    ASSISTED = 'assisted'
    LAB_AUTONOMOUS = 'lab_autonomous'


CANONICAL_RUN_MODES = (
    RunMode.ASK,
    RunMode.AI_REVIEW,
    RunMode.AUTO,
    RunMode.CUSTOM,
)

_LEGACY_RUN_MODE_MAP = {
    RunMode.ASSISTED: RunMode.ASK,
    RunMode.LAB_AUTONOMOUS: RunMode.AUTO,
    RunMode.READ_ONLY: RunMode.READ_ONLY,
}


def normalize_run_mode(value):
    """Return the canonical permission profile for a persisted Run mode."""
    try:
        mode = RunMode(value)
    except ValueError:
        raise AutonomyStateError('unknown run mode: %r' % (value,)) from None
    return _LEGACY_RUN_MODE_MAP.get(mode, mode)


class AiEnvironment(str, Enum):
    PRODUCTION = 'production'
    STAGING = 'staging'
    LAB = 'lab'


class DecisionOperation(str, Enum):
    APPROVE = 'approve'
    REJECT = 'reject'


# Run 合法转换白名单。queued 允许回到 waiting_approval 之外的活动态；
# waiting_approval 审批通过后回到 queued 等待执行器认领（S1 尚无执行器，
# 语义上是"已解锁"）。
RUN_TRANSITIONS = {
    RunStatus.DRAFT: {RunStatus.QUEUED, RunStatus.CANCELLED, RunStatus.EXPIRED},
    RunStatus.QUEUED: {
        RunStatus.RUNNING, RunStatus.WAITING_APPROVAL,
        RunStatus.CANCELLED, RunStatus.EXPIRED, RunStatus.FAILED,
    },
    RunStatus.RUNNING: {
        RunStatus.WAITING_APPROVAL, RunStatus.RECOVERING,
        RunStatus.NEEDS_ATTENTION, RunStatus.COMPLETED,
        RunStatus.FAILED, RunStatus.CANCELLED,
    },
    RunStatus.WAITING_APPROVAL: {
        RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.NEEDS_ATTENTION,
        RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.EXPIRED,
    },
    RunStatus.RECOVERING: {
        RunStatus.RUNNING, RunStatus.WAITING_APPROVAL,
        RunStatus.NEEDS_ATTENTION,
        RunStatus.FAILED, RunStatus.CANCELLED,
    },
    RunStatus.NEEDS_ATTENTION: {
        RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED,
    },
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
    RunStatus.EXPIRED: set(),
}

# Step 合法转换白名单。waiting_approval 被拒绝时进入 failed（note 记录
# rejected），不引入 roadmap 之外的独立 rejected 状态。
STEP_TRANSITIONS = {
    StepStatus.PROPOSED: {
        StepStatus.WAITING_APPROVAL, StepStatus.APPROVED, StepStatus.RUNNING,
        StepStatus.SKIPPED, StepStatus.FAILED, StepStatus.CANCELLED,
    },
    StepStatus.WAITING_APPROVAL: {
        StepStatus.APPROVED, StepStatus.FAILED,
        StepStatus.SKIPPED, StepStatus.CANCELLED,
        # Guardian 复核中的计划：唯一允许进入 running 的路径，复核
        # 结果只会落 approved/failed 或回退 waiting，绝不自动放行。
        StepStatus.RUNNING,
    },
    StepStatus.APPROVED: {
        StepStatus.RUNNING, StepStatus.FAILED, StepStatus.CANCELLED,
    },
    StepStatus.RUNNING: {
        StepStatus.SUCCEEDED, StepStatus.FAILED,
        StepStatus.OUTCOME_UNKNOWN, StepStatus.CANCELLED,
        # 只读动作执行中被强杀：恢复层把它回退到 approved 自动重试
        # （人工审批仍然有效）；是否允许由恢复层按写意图判定。
        StepStatus.APPROVED,
        # 计划被 Guardian 复核中进程崩溃：唯一允许回 waiting 的路径，
        # 回退后由人工决策，绝不自动放行。
        StepStatus.WAITING_APPROVAL,
    },
    # 写动作结果未知：绝不自动重放，只能由人工决策落到终态。
    StepStatus.OUTCOME_UNKNOWN: {StepStatus.SUCCEEDED, StepStatus.FAILED},
    StepStatus.SUCCEEDED: set(),
    StepStatus.FAILED: set(),
    StepStatus.SKIPPED: set(),
    StepStatus.CANCELLED: set(),
}

# 仍占用资产的活动 Run 状态（用于"同一资产最多一个活动自治 Run"约束）。
ACTIVE_RUN_STATUSES = frozenset({
    RunStatus.DRAFT, RunStatus.QUEUED, RunStatus.RUNNING,
    RunStatus.WAITING_APPROVAL, RunStatus.RECOVERING,
    RunStatus.NEEDS_ATTENTION,
})

TERMINAL_RUN_STATUSES = frozenset({
    RunStatus.COMPLETED, RunStatus.FAILED,
    RunStatus.CANCELLED, RunStatus.EXPIRED,
})

TERMINAL_STEP_STATUSES = frozenset({
    StepStatus.SUCCEEDED, StepStatus.FAILED,
    StepStatus.SKIPPED, StepStatus.CANCELLED,
})


def _parse(enum_cls, value, label):
    try:
        return enum_cls(value)
    except ValueError:
        raise AutonomyStateError(
            'unknown %s: %r' % (label, value)
        ) from None


def assert_run_transition(current, target):
    """校验 Run 状态转换合法，非法时抛 AutonomyStateError。"""
    src = _parse(RunStatus, current, 'run status')
    dst = _parse(RunStatus, target, 'run status')
    if dst not in RUN_TRANSITIONS[src]:
        raise AutonomyStateError(
            "illegal run status transition: '%s' -> '%s'" % (src.value, dst.value)
        )
    return dst


def assert_step_transition(current, target):
    """校验 Step 状态转换合法，非法时抛 AutonomyStateError。"""
    src = _parse(StepStatus, current, 'step status')
    dst = _parse(StepStatus, target, 'step status')
    if dst not in STEP_TRANSITIONS[src]:
        raise AutonomyStateError(
            "illegal step status transition: '%s' -> '%s'" % (src.value, dst.value)
        )
    return dst
