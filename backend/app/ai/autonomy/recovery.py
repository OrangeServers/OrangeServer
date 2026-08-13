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
  人；有已批准 Step 就从 execute 边界恢复；两者皆无（首跑即丢）才
  允许重新规划。
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


@dataclass(frozen=True)
class RecoveryOutcome:
    """恢复预检结论：halt/pause 直接返回；resume/boundary 给出入场。"""

    mode: str
    result: Optional[str] = None
    entry: Optional[Dict] = None


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
        """把 Run 推到可执行态：执行器只认 queued/running。"""
        if run.status in (
            RunStatus.QUEUED.value, RunStatus.RUNNING.value,
        ):
            return
        assert_run_transition(run.status, RunStatus.RUNNING.value)
        run.status = RunStatus.RUNNING.value
        self.repo._bump(run)
        self.repo._commit()

    # ------------------------------------------------------------------
    # 预检入口
    # ------------------------------------------------------------------

    def resolve(self, run, *, checkpoint_present):
        """按 MySQL 权威状态决定恢复方式。

        checkpoint_present=False 且 Run 已有 Step 时视为 checkpoint
        丢失，只能从 MySQL 已确认的安全边界重建。
        """
        steps = self._steps(run.id)
        has_steps = any(step.kind == 'action' for step in steps)
        needs_preflight = (
            run.status == RunStatus.RECOVERING.value
            or (not checkpoint_present and has_steps)
        )
        if not needs_preflight:
            return RecoveryOutcome(mode=MODE_RESUME)

        # 1) 结果未知的写动作：保持 needs_attention，人工介入。
        if run.status == RunStatus.NEEDS_ATTENTION.value:
            return RecoveryOutcome(mode=MODE_HALT, result='needs_attention')

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
            self.repo._commit()
            steps = self._steps(run.id)

        # 3) 从 MySQL 安全边界重建入场点。
        waiting = [
            step for step in steps
            if step.status == StepStatus.WAITING_APPROVAL.value
        ]
        if waiting:
            # 审批未落定：继续等人，绝不自动越过审批。
            if run.status in (
                RunStatus.QUEUED.value, RunStatus.RUNNING.value,
                RunStatus.RECOVERING.value,
            ):
                assert_run_transition(
                    run.status, RunStatus.WAITING_APPROVAL.value,
                )
                run.status = RunStatus.WAITING_APPROVAL.value
                self.repo._bump(run)
                self.repo._commit()
            return RecoveryOutcome(mode=MODE_PAUSE)

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
            return RecoveryOutcome(mode=MODE_BOUNDARY, entry=entry)

        # 首跑即丢（无动作 Step 落库）：允许重新规划。
        self._transition_running(run)
        return RecoveryOutcome(mode=MODE_FRESH)
