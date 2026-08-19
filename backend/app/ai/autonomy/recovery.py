# -*- coding: utf-8 -*-
"""M1/S2: 自治 Run 的恢复层——只从 MySQL 已确认的安全边界重建。

恢复契约（docs/ai/ROADMAP.md「恢复、取消和回退」）：
- 只读动作可以自动重试：执行中被强杀、无写意图落库的 Step 回到
  approved，由执行循环重跑（人工审批仍然有效，无需重新审批）。
- 已确认尚未执行的结构化动作可以继续：仍处 approved 的 Step 直接
  从 execute 边界重建，绝不回到 plan 重新提案。
- 写动作可能已经生效但结果未落库：存在 write_intent 事件而 Step
  仍停在 running 时，Step 落 outcome_unknown、Run 落
  needs_attention，绝不自动重放。
- Redis checkpoint 丢失时只能从 MySQL 重建：有待审 Step 就继续等
  人；有已批准 Step 就从 execute 边界恢复；已落库 proposal 从 policy
  边界继续；只有 checkpoint 与动作 Step 都不存在时才允许重新规划。
- 结果已落库但 post-action 游标无法从 MySQL 完整重建时进入
  needs_attention；绝不猜测完成、重置模型循环或回 plan 重放。
- 恢复层是驱动循环的预检：先解决 Step 层面的中断残留，再决定图的
  入场方式；所有状态写入都过白名单转换校验。
"""
import json
from dataclasses import dataclass
from typing import Dict, Optional

from app.ai.autonomy.repository import sanitize_text
from app.ai.autonomy.state import (
    RunStatus,
    StepStatus,
    assert_run_transition,
    assert_step_transition,
)

# 驱动循环入场方式。
MODE_HALT = 'halt'
MODE_PAUSE = 'pause'
MODE_RESUME = 'resume'      # checkpoint 健康：原样续跑
MODE_FRESH = 'fresh'        # 无动作 Step 落库：从 plan 重新入场
MODE_BOUNDARY = 'boundary'  # checkpoint 丢失：从 MySQL 边界重建

EVENT_WRITE_UNKNOWN = 'recovery_write_outcome_unknown'
EVENT_READONLY_RETRY = 'recovery_readonly_retry'
EVENT_BOUNDARY_REBUILD = 'recovery_boundary_rebuild'
EVENT_CURSOR_UNRESOLVED = 'recovery_cursor_unresolved'


@dataclass(frozen=True)
class RecoveryOutcome:
    """恢复预检结论：halt/pause 直接返回；resume/boundary 给出入场。"""

    mode: str
    result: Optional[str] = None
    entry: Optional[Dict] = None
    as_node: Optional[str] = None


class RecoveryService:
    """session 注入式恢复预检；不依赖图与 saver。"""

    def __init__(self, session, repo):
        self.session = session
        self.repo = repo

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _steps(self, run_id):
        from app.core.db.database import t_ai_autonomous_step

        return (
            self.session.query(t_ai_autonomous_step)
            .filter_by(run_id=run_id)
            .order_by(t_ai_autonomous_step.seq.asc())
            .all()
        )

    def _has_write_intent(self, run_id, step_id):
        from app.core.db.database import t_ai_autonomous_event

        events = self.session.query(t_ai_autonomous_event).filter_by(
            run_id=run_id, event_type='write_intent',
        ).all()
        for event in events:
            payload = json.loads(event.payload_json or '{}')
            if str(payload.get('step_id') or '') == str(step_id):
                return True
        return False

    def _transition_running(self, run):
        """把 Run 推到可执行态；提交由 resolve 的边界事务统一完成。"""
        if run.status in (
            RunStatus.QUEUED.value, RunStatus.RUNNING.value,
        ):
            return
        assert_run_transition(run.status, RunStatus.RUNNING.value)
        run.status = RunStatus.RUNNING.value
        self.repo._bump(run)

    def _halt_for_cursor_review(self, run, steps, reason):
        """Fail closed when MySQL proves work but not a safe graph cursor."""
        if run.status != RunStatus.NEEDS_ATTENTION.value:
            assert_run_transition(
                run.status, RunStatus.NEEDS_ATTENTION.value,
            )
            run.status = RunStatus.NEEDS_ATTENTION.value
        self.repo._bump(run)
        self.repo.append_event(run, EVENT_CURSOR_UNRESOLVED, {
            'reason': reason,
            'step_ids': [step.id for step in steps],
        })
        self.repo._commit()
        return RecoveryOutcome(
            mode=MODE_HALT, result='needs_attention',
        )

    # ------------------------------------------------------------------
    # 预检入口
    # ------------------------------------------------------------------

    def resolve(self, run, *, checkpoint_present):
        """按 MySQL 权威状态决定恢复方式。

        checkpoint_present=False 且 Run 已有 Step 时视为 checkpoint
        丢失，只能从 MySQL 已确认的安全边界重建。
        """
        steps = self._steps(run.id)
        action_steps = [step for step in steps if step.kind == 'action']
        has_steps = bool(action_steps)
        needs_preflight = (
            run.status == RunStatus.RECOVERING.value
            or (not checkpoint_present and has_steps)
        )
        if not needs_preflight:
            if not checkpoint_present:
                # A brand-new Run has neither checkpoint nor durable action
                # Step.  This is the only legitimate fresh-plan entry.
                self._transition_running(run)
                self.repo._commit()
                return RecoveryOutcome(mode=MODE_FRESH)
            self.repo._commit()
            return RecoveryOutcome(mode=MODE_RESUME)

        # 1) 结果未知的写动作：保持 needs_attention，人工介入。
        if run.status == RunStatus.NEEDS_ATTENTION.value:
            self.repo._commit()
            return RecoveryOutcome(mode=MODE_HALT, result='needs_attention')

        unknown = [
            step for step in action_steps
            if step.status == StepStatus.OUTCOME_UNKNOWN.value
        ]
        if unknown:
            return self._halt_for_cursor_review(
                run, unknown, 'write_outcome_unknown',
            )

        if len(action_steps) > 1:
            # S2 does not persist a plan/DAG cursor that can prove the order
            # between multiple durable actions after a crash.  This includes
            # terminal + unresolved mixtures: continuing only the unresolved
            # action could skip observation of the terminal one, while going
            # back to plan could replay it.  S3 may replace this fail-closed
            # boundary once it owns a durable multi-action plan cursor.
            return self._halt_for_cursor_review(
                run, action_steps, 'multiple_action_steps_without_plan_cursor',
            )

        # 2) 中断残留：running Step 按写意图分流。
        for step in steps:
            if step.status != StepStatus.RUNNING.value:
                continue
            if self._has_write_intent(run.id, step.id):
                # 写可能已生效但结果未落库：绝不自动重放。
                assert_step_transition(
                    step.status, StepStatus.OUTCOME_UNKNOWN.value,
                )
                step.status = StepStatus.OUTCOME_UNKNOWN.value
                step.note = sanitize_text(
                    'interrupted after write intent; outcome unknown',
                )[:255]
                assert_run_transition(
                    run.status, RunStatus.NEEDS_ATTENTION.value,
                )
                run.status = RunStatus.NEEDS_ATTENTION.value
                self.repo._bump(run)
                self.repo.append_event(run, EVENT_WRITE_UNKNOWN, {
                    'step_id': step.id,
                })
                self.repo._commit()
                return RecoveryOutcome(
                    mode=MODE_HALT, result='needs_attention',
                )
            # 只读动作可自动重试：回到 approved 由执行循环重跑。
            assert_step_transition(step.status, StepStatus.APPROVED.value)
            step.status = StepStatus.APPROVED.value
            step.note = 'interrupted read-only step; auto retry'
            self.repo._bump(run)
            self.repo.append_event(run, EVENT_READONLY_RETRY, {
                'step_id': step.id,
            })

        # 3) 从 MySQL 安全边界重建入场点。
        waiting = [
            step for step in steps
            if step.status == StepStatus.WAITING_APPROVAL.value
        ]
        if waiting:
            # 审批未落定：把 checkpoint 明确重建到 policy 之后的
            # approval_pause。仅修改 MySQL 后暂停会留下较旧的
            # next=policy 游标；用户批准后旧 policy 看不到 approved
            # Step，可能跳过执行。
            if run.status in (
                RunStatus.QUEUED.value, RunStatus.RUNNING.value,
                RunStatus.RECOVERING.value,
            ):
                assert_run_transition(
                    run.status, RunStatus.WAITING_APPROVAL.value,
                )
                run.status = RunStatus.WAITING_APPROVAL.value
                self.repo._bump(run)
            step = waiting[0]
            self.repo.append_event(run, EVENT_BOUNDARY_REBUILD, {
                'entry': 'approval_pause', 'step_id': step.id,
            })
            self.repo._commit()
            entry = {
                'run_id': run.id,
                'graph_version': str(run.graph_version or ''),
                'owner': str(run.owner or ''),
                'phase': 'policy',
                'loops': 0,
                'proposed_steps': 0,
                'pending_step_id': step.id,
                'policy_decision': 'ask',
            }
            return RecoveryOutcome(
                mode=MODE_BOUNDARY, entry=entry, as_node='policy',
            )

        approved = [
            step for step in steps
            if step.kind == 'action'
            and step.status == StepStatus.APPROVED.value
        ]
        if approved:
            # 已确认尚未执行：从 execute 边界继续，绝不回到 plan。
            step = approved[0]
            self._transition_running(run)
            self.repo.append_event(run, EVENT_BOUNDARY_REBUILD, {
                'entry': 'execute', 'step_id': step.id,
            })
            self.repo._commit()
            entry = {
                'run_id': run.id,
                'graph_version': str(run.graph_version or ''),
                'owner': str(run.owner or ''),
                'phase': 'policy',
                'loops': 0,
                # 边界重建按单轮收敛：执行完已批准 Step 后 decide
                # 直接收尾，不自动重新规划；继续与否由人重新发起。
                'proposed_steps': 0,
                'pending_step_id': step.id,
            }
            return RecoveryOutcome(
                mode=MODE_BOUNDARY, entry=entry, as_node='policy',
            )

        # Planner proposals are durable MySQL facts.  If the Worker died
        # before LangGraph persisted the plan node, re-enter at policy rather
        # than calling the model again and duplicating the proposal.
        proposed = [
            step for step in steps
            if step.kind == 'action'
            and step.status == StepStatus.PROPOSED.value
        ]
        if proposed:
            self._transition_running(run)
            self.repo.append_event(run, EVENT_BOUNDARY_REBUILD, {
                'entry': 'policy',
                'step_ids': [step.id for step in proposed],
            })
            self.repo._commit()
            entry = {
                'run_id': run.id,
                'graph_version': str(run.graph_version or ''),
                'owner': str(run.owner or ''),
                'phase': 'plan',
                # One continuing loop must have produced at least one action
                # Step.  Counting durable actions is conservative when one
                # planner call proposed several actions and never understates
                # the recoverable model-loop budget.
                'loops': len([
                    step for step in steps if step.kind == 'action'
                ]),
                # Recovery executes/policies the already durable proposal as
                # one bounded closing round.  It must not automatically call
                # the planner again after that boundary.
                'proposed_steps': 0,
            }
            return RecoveryOutcome(
                mode=MODE_BOUNDARY, entry=entry, as_node='plan',
            )

        terminal = [
            step for step in steps
            if step.kind == 'action'
            and step.status in {
                StepStatus.SUCCEEDED.value,
                StepStatus.FAILED.value,
                StepStatus.SKIPPED.value,
                StepStatus.CANCELLED.value,
            }
        ]
        if terminal:
            # The action outcome is authoritative, but MySQL does not yet
            # persist the planner loop/cursor needed to decide whether a
            # terminal Step should observe, continue, or finish.  Guessing a
            # post-action cursor could skip planned work; returning to plan
            # could replay it.  Preserve the outcome and require review.
            return self._halt_for_cursor_review(
                run, terminal, 'post_action_cursor_unavailable',
            )

        if has_steps:
            # Every known action state is handled above.  Unknown or mixed
            # future states fail closed instead of silently becoming fresh.
            return self._halt_for_cursor_review(
                run,
                [step for step in steps if step.kind == 'action'],
                'unsupported_action_recovery_state',
            )

        if checkpoint_present:
            # A recovering Run with no durable action still has a usable
            # native cursor (for example after a zero-action planning node).
            # Resume it so model-loop accounting is not reset.
            self._transition_running(run)
            self.repo._commit()
            return RecoveryOutcome(mode=MODE_RESUME)

        # 首跑即丢（无 checkpoint 且确实无动作 Step）：允许重新规划。
        self._transition_running(run)
        self.repo._commit()
        return RecoveryOutcome(mode=MODE_FRESH)
