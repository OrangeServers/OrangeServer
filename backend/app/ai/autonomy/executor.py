# -*- coding: utf-8 -*-
"""M1/S2: 自治执行器——唯一的远程副作用入口。

锁定的安全契约（Issue #13）：
- 每次执行前都重新复核资产权限、凭据授权、资产环境与动作
  digest，全部通过才允许产生远程副作用。
- 写意图先落库再执行：写动作的意图在副作用之前持久化；结果不
  确定（写可能已生效但未落库结果）时 Step 落 outcome_unknown、
  Run 落 needs_attention，绝不自动重放。
- 只读动作传输失败可重试（Step failed，note 标记 retryable）；
  确认未执行的失败按普通 failed 处理。
- 预算硬约束：动作数耗尽拒绝执行；命令超时取动作超时与预算
  上限的较小值。
- 取消是请求：cancel_requested 后执行器拒绝开跑新 Step。
"""
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from app.ai.autonomy.actions import (
    ActionValidationError,
    action_from_dict,
    build_probe_command,
    verify_action_digest,
)
from app.ai.autonomy.policy import Budget
from app.ai.autonomy.repository import (
    AutonomyConflict,
    AutonomyPermissionError,
    AutonomyRepository,
    AutonomyValidationError,
    sanitize_text,
)
from app.ai.autonomy.state import (
    RunStatus,
    StepKind,
    StepStatus,
    assert_run_transition,
    assert_step_transition,
)

# 需要"写意图先落库"的动作类别。v1 探针全部只读；shell 是通用
# 写入口（永远精确审批）。后续 file_patch/systemd 类别在此追加。
WRITE_KINDS = frozenset({'shell'})

# 已计入动作预算的 Step 状态。
_EXECUTED_STEP_STATUSES = (
    StepStatus.RUNNING.value,
    StepStatus.SUCCEEDED.value,
    StepStatus.FAILED.value,
    StepStatus.OUTCOME_UNKNOWN.value,
)


@dataclass(frozen=True)
class RunnerResult:
    """远程执行结果。exit_code 为 None 表示结果未知（传输层
    中断/超时），写动作必须按 fail-closed 处理。"""

    exit_code: Optional[int]
    output: str = ''
    transport_error: Optional[str] = None

    @property
    def uncertain(self) -> bool:
        return self.exit_code is None


def default_runner(
    command, *, host_id, system_user_id, host_alias,
    system_user_alias, timeout_seconds, step_id,
) -> RunnerResult:
    """生产 runner：复用批量命令边界（审计、危险命令拦截、超时）。"""
    from app.assets.batch_service import execute_batch_command

    result = execute_batch_command(
        username='AI Autonomy',
        host_ids=[int(host_id)],
        sys_user=str(system_user_alias or ''),
        sys_user_id=int(system_user_id),
        command=command,
        audit_source='AI Autonomy',
        audit_ref=str(step_id or '')[:64],
        command_timeout=int(timeout_seconds),
    )
    items = result.get('items') or []
    item = items[0] if items else {'status': 'failed', 'error': 'no result'}
    if item.get('status') == 'success':
        return RunnerResult(exit_code=0, output=str(item.get('output') or ''))
    error = str(item.get('error') or 'command failed')
    if 'timeout' in error.lower():
        # 超时 = 结果未知：写动作可能已生效。
        return RunnerResult(exit_code=None, output='', transport_error=error)
    return RunnerResult(
        exit_code=1, output=str(item.get('output') or error),
    )


class AutonomyExecutor:
    """session 注入式执行器；runner 可替换（测试/未来传输层）。"""

    def __init__(
        self, session, secret_key: str,
        runner: Optional[Callable[..., RunnerResult]] = None,
        platform_factory=None,
    ):
        self.session = session
        self.repo = AutonomyRepository(
            session, secret_key, platform_factory=platform_factory,
        )
        self.runner = runner or default_runner

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _get_step_row(self, run_id: str, step_id: str):
        from app.core.db.database import t_ai_autonomous_step

        step = self.session.query(t_ai_autonomous_step).filter_by(
            id=step_id, run_id=run_id,
        ).first()
        if step is None:
            raise AutonomyConflict('step does not belong to this run')
        return step

    def _load_action(self, step):
        try:
            action = action_from_dict(json.loads(step.action_json or '{}'))
        except (ActionValidationError, ValueError):
            raise AutonomyConflict('action snapshot is corrupted') from None
        if not verify_action_digest(
            action, step.action_digest, self.repo.secret_key,
        ):
            raise AutonomyConflict('action digest mismatch')
        return action

    def _build_command(self, action) -> str:
        kind = str(action.kind)
        if kind == 'probe':
            probe_id = str(action.parameters.get('probe_id') or '')
            params = {
                key: value for key, value in action.parameters.items()
                if key != 'probe_id'
            }
            return build_probe_command(probe_id, params)
        if kind == 'shell':
            command = str(action.parameters.get('command') or '')
            if not command.strip():
                raise AutonomyValidationError('shell action has no command')
            return command
        raise AutonomyValidationError(
            'executor does not support action kind %r yet' % (kind,)
        )

    # ------------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------------

    def execute_step(
        self, owner: str, role: str, run_id: str, step_id: str,
    ) -> Dict[str, Any]:
        """执行已审批的 Step；任何前置复核失败都不产生副作用。"""
        run = self.repo._get_run_row(owner, run_id)
        step = self._get_step_row(run_id, step_id)

        if bool(run.cancel_requested):
            raise AutonomyConflict('run cancellation was requested')
        if step.kind != StepKind.ACTION.value:
            raise AutonomyConflict('only action steps can be executed')
        if step.status != StepStatus.APPROVED.value:
            raise AutonomyConflict(
                'step is not approved (status=%s)' % (step.status,)
            )
        if run.status not in (
            RunStatus.QUEUED.value, RunStatus.RUNNING.value,
        ):
            raise AutonomyConflict(
                'run is not in an executable state (status=%s)'
                % (run.status,)
            )

        # 副作用前的最后一道复核：digest + 权限/凭据/环境。
        action = self._load_action(step)
        self.repo._revalidate_boundaries(owner, role, run)

        budget = Budget(**json.loads(run.budget_json or '{}'))
        from app.core.db.database import t_ai_autonomous_step
        executed = self.session.query(t_ai_autonomous_step).filter(
            t_ai_autonomous_step.run_id == run_id,
            t_ai_autonomous_step.kind == StepKind.ACTION.value,
            t_ai_autonomous_step.status.in_(_EXECUTED_STEP_STATUSES),
        ).count()
        if executed >= budget.max_actions:
            raise AutonomyConflict('action budget exhausted')

        command = self._build_command(action)
        timeout = max(
            1, min(budget.command_timeout_seconds, action.timeout_seconds),
        )
        is_write = action.kind in WRITE_KINDS

        # 状态推进 + 写意图落库（必须在任何远程副作用之前提交）。
        assert_step_transition(step.status, StepStatus.RUNNING.value)
        step.status = StepStatus.RUNNING.value
        if run.status == RunStatus.QUEUED.value:
            assert_run_transition(run.status, RunStatus.RUNNING.value)
            run.status = RunStatus.RUNNING.value
        self.repo._bump(run)
        if is_write:
            self.repo.append_event(run, 'write_intent', {
                'step_id': step_id,
                'kind': str(action.kind),
                'target_id': int(action.target_id),
            })
        self.repo._commit()

        result = self.runner(
            command,
            host_id=int(run.host_id),
            system_user_id=int(run.system_user_id),
            host_alias=str(run.host_alias or ''),
            system_user_alias=str(run.system_user_alias or ''),
            timeout_seconds=timeout,
            step_id=step_id,
        )

        self._apply_result(run, step, action, result, is_write)
        return {
            'step_id': step_id,
            'step_status': step.status,
            'run_status': run.status,
            'revision': int(run.revision or 0),
            'uncertain': bool(result.uncertain and is_write),
        }

    # ------------------------------------------------------------------
    # 结果落库
    # ------------------------------------------------------------------

    def _apply_result(self, run, step, action, result, is_write):
        budget = Budget(**json.loads(run.budget_json or '{}'))
        cap = budget.step_output_bytes

        if result.uncertain:
            if is_write:
                # 写可能已生效但结果未落库：fail-closed，人工介入，
                # 绝不自动重放。
                assert_step_transition(
                    step.status, StepStatus.OUTCOME_UNKNOWN.value,
                )
                step.status = StepStatus.OUTCOME_UNKNOWN.value
                step.note = sanitize_text(
                    result.transport_error or 'transport interrupted',
                )[:255]
                assert_run_transition(
                    run.status, RunStatus.NEEDS_ATTENTION.value,
                )
                run.status = RunStatus.NEEDS_ATTENTION.value
                self.repo._bump(run)
                self.repo.append_event(run, 'step_outcome_unknown', {
                    'step_id': step.id, 'kind': str(action.kind),
                })
            else:
                # 只读动作传输失败：确认未生效，可安全重试。
                assert_step_transition(step.status, StepStatus.FAILED.value)
                step.status = StepStatus.FAILED.value
                step.note = 'transport_error_retryable: ' + sanitize_text(
                    result.transport_error or 'transport interrupted',
                )[:200]
                self.repo._bump(run)
                self.repo.append_event(run, 'step_executed', {
                    'step_id': step.id, 'outcome': 'transport_error',
                })
            self.repo._commit()
            return

        text = sanitize_text(result.output)[:cap]
        if result.exit_code == 0:
            assert_step_transition(step.status, StepStatus.SUCCEEDED.value)
            step.status = StepStatus.SUCCEEDED.value
            step.note = text[:255]
            outcome = 'succeeded'
        else:
            assert_step_transition(step.status, StepStatus.FAILED.value)
            step.status = StepStatus.FAILED.value
            step.note = (
                'exit_code=%s: %s' % (result.exit_code, text)
            )[:255]
            outcome = 'failed'
        self.repo._bump(run)
        self.repo.append_event(run, 'step_executed', {
            'step_id': step.id,
            'outcome': outcome,
            'exit_code': int(result.exit_code),
        })
        self.repo._commit()
