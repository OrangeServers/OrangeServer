# -*- coding: utf-8 -*-
"""M1/S2: 自治执行器——唯一的远程副作用入口。

锁定的安全契约（Issue #13）：
- 每次执行前都重新复核资产权限、凭据授权、资产环境与动作
  digest，全部通过才允许产生远程副作用。
- 写意图先落库再执行：写动作的意图在副作用之前持久化；结果不
  确定（写可能已生效但未落库结果）时 Step 落 outcome_unknown、
  Run 落 needs_attention，绝不自动重放。
- 永久拒绝清单在构造命令时硬拦截：即使旧审批残留，命中清单
  的命令也不产生任何远程副作用。
- 只读动作传输失败可重试（Step failed，note 标记 retryable）；
  确认未执行的失败按普通 failed 处理。
- 预算硬约束：动作数耗尽拒绝执行；命令超时取动作超时与预算
  上限的较小值。
- 取消是请求：cancel_requested 后执行器拒绝开跑新 Step。
"""
import datetime
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from app.ai.autonomy.actions import (
    ActionValidationError,
    action_from_dict,
    build_file_patch_command,
    build_file_restore_command,
    build_probe_command,
    build_write_command,
    patch_backup_path,
    verify_action_digest,
)
from app.ai.diagnostic_adapters import sanitize_evidence
from app.ai.autonomy.policy import (
    Budget,
    permanent_deny_reason,
    validate_mode_for_environment,
)
from app.ai.autonomy.repository import (
    AutonomyConflict,
    AutonomyRepository,
    AutonomyValidationError,
)
from app.ai.autonomy.ssh_runner import (
    TERMINATION_AUTHORIZATION_REVOKED,
    TERMINATION_CANCELLED,
    TERMINATION_EXITED,
    TERMINATION_LEASE_LOST,
    run_ssh_command,
    validate_working_directory,
)
from app.ai.autonomy.state import (
    RunStatus,
    StepKind,
    StepStatus,
    assert_run_transition,
    assert_step_transition,
)

# 需要“写意图先落库”的动作类别。shell 是通用写入口（永远精确
# 审批）；systemd/package_install 是服务端模板的结构化写动作；
# file_patch/file_restore 是带备份与恢复承诺的文件写动作。
WRITE_KINDS = frozenset({
    'shell', 'systemd', 'package_install', 'file_patch', 'file_restore',
})

# 已计入动作预算的 Step 状态。
_EXECUTED_STEP_STATUSES = (
    StepStatus.RUNNING.value,
    StepStatus.SUCCEEDED.value,
    StepStatus.FAILED.value,
    StepStatus.OUTCOME_UNKNOWN.value,
)

logger = logging.getLogger('autonomy_executor')


class _ExecutionLeaseLost(RuntimeError):
    """The worker no longer has authority to persist a remote outcome."""


@dataclass(frozen=True)
class RunnerResult:
    """远程执行结果。exit_code 为 None 表示结果未知（传输层
    中断/超时），写动作必须按 fail-closed 处理。"""

    exit_code: Optional[int]
    output: str = ''
    transport_error: Optional[str] = None
    stderr: str = ''
    started: Optional[bool] = None
    stop_confirmed: bool = False
    termination: str = ''
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    control_reason: Optional[str] = None

    @property
    def uncertain(self) -> bool:
        return self.exit_code is None


def default_runner(
    command, *, host_id, system_user_id, host_alias, audit_name,
    system_user_alias, timeout_seconds, step_id, host, port,
    working_directory='', max_output_bytes=65536, control_probe=None,
    audit_callback=None, action_kind='action', action_digest='',
) -> RunnerResult:
    """Production runner using the dedicated cancellable SSH primitive."""
    del host_id, system_user_alias  # kept in the injected-runner contract

    try:
        remote = run_ssh_command(
            command,
            host=str(host),
            port=int(port),
            system_user_id=int(system_user_id),
            timeout_seconds=float(timeout_seconds),
            max_output_bytes=int(max_output_bytes),
            working_directory=str(working_directory or ''),
            control_probe=control_probe,
        )
        # A failure that is authoritatively known not to have started is a
        # normal failed attempt, not an unknown write outcome.
        exit_code = remote.exit_code
        if exit_code is None and not remote.started and remote.stop_confirmed:
            exit_code = 1
        result = RunnerResult(
            exit_code=exit_code,
            output=remote.stdout,
            stderr=remote.stderr,
            transport_error=(
                remote.transport_error
                or (
                    remote.termination
                    if remote.termination != TERMINATION_EXITED
                    else None
                )
            ),
            started=remote.started,
            stop_confirmed=remote.stop_confirmed,
            termination=remote.termination,
            stdout_truncated=remote.stdout_truncated,
            stderr_truncated=remote.stderr_truncated,
            control_reason=remote.control_reason,
        )
    except Exception as exc:
        # Invalid local inputs are checked before write_intent. Remaining
        # adapter failures are conservatively unknown.
        result = RunnerResult(
            exit_code=None,
            transport_error=type(exc).__name__,
            started=None,
            stop_confirmed=False,
            termination='transport_error',
            control_reason='transport_error',
        )

    # Preserve the existing append-only command audit without reusing the
    # legacy SSH read loop. Audit failure is already non-blocking by contract.
    reason = 'source=AI Autonomy; ref=%s; digest=%s; termination=%s' % (
        str(step_id or '')[:64], str(action_digest or '')[:12],
        str(result.termination or 'legacy'),
    )
    if result.exit_code is not None:
        reason += '; exit_code=%s' % int(result.exit_code)
    try:
        if audit_callback is None:
            from app.tools.audlog import log_ssh_audit
            audit_callback = log_ssh_audit
        audit_callback(
            str(audit_name or '')[:24], 'AI 自治命令',
            ('action=%s' % str(action_kind or 'action'))[:255],
            str(host_alias or '')[:30],
            '成功' if result.exit_code == 0 else '失败', reason[:255],
        )
    except Exception:
        # The authoritative Run/Step Events are committed separately. Preserve
        # execution semantics if the legacy auxiliary audit sink is
        # unavailable.
        logger.exception('AI autonomy command audit failed')
    return result


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

    def _build_command(
        self, action, run_id: str, *, target_host: str = '',
    ) -> str:
        kind = str(action.kind)
        if kind == 'probe':
            probe_id = str(action.parameters.get('probe_id') or '')
            params = {
                key: value for key, value in action.parameters.items()
                if key != 'probe_id'
            }
            try:
                return build_probe_command(
                    probe_id, params, target_host=target_host,
                )
            except ActionValidationError as exc:
                raise AutonomyValidationError(str(exc)) from exc
        if kind == 'shell':
            command = str(action.parameters.get('command') or '')
            if not command.strip():
                raise AutonomyValidationError('shell action has no command')
            # 永久拒绝清单是服务端硬规则：旧审批不能推翻。
            deny_reason = permanent_deny_reason(command)
            if deny_reason is not None:
                raise AutonomyValidationError(
                    'shell command is permanently denied: %s'
                    % (deny_reason,)
                )
            return command
        if kind in ('systemd', 'package_install'):
            # 服务端模板构造；白名单与永久拒绝复核在 actions 层。
            try:
                return build_write_command(kind, action.parameters)
            except ActionValidationError as exc:
                raise AutonomyValidationError(str(exc)) from exc
        if kind == 'file_patch':
            path = str(action.parameters.get('path') or '')
            content = str(action.parameters.get('content') or '')
            backup = patch_backup_path(path, run_id, action.step_id)
            try:
                return build_file_patch_command(path, content, backup)
            except ActionValidationError as exc:
                raise AutonomyValidationError(str(exc)) from exc
        if kind == 'file_restore':
            path = str(action.parameters.get('path') or '')
            backup = str(action.parameters.get('backup_path') or '')
            try:
                return build_file_restore_command(path, backup)
            except ActionValidationError as exc:
                raise AutonomyValidationError(str(exc)) from exc
        raise AutonomyValidationError(
            'executor does not support action kind %r yet' % (kind,)
        )

    def _validate_restore_source(self, action, run_id: str) -> None:
        """Require a restore to name this Run's successful patch backup."""
        from app.core.db.database import t_ai_autonomous_step

        path = str(action.parameters.get('path') or '')
        requested = str(action.parameters.get('backup_path') or '')
        candidates = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id,
            kind=StepKind.ACTION.value,
            status=StepStatus.SUCCEEDED.value,
        ).all()
        for source in candidates:
            try:
                source_action = self._load_action(source)
            except AutonomyConflict:
                continue
            if (
                str(source_action.kind) != 'file_patch'
                or str(source_action.step_id) != str(source.id)
                or int(source_action.target_id) != int(action.target_id)
                or int(source_action.system_user_id)
                != int(action.system_user_id)
                or str(source_action.parameters.get('path') or '') != path
            ):
                continue
            expected = patch_backup_path(path, run_id, str(source.id))
            if hmac.compare_digest(requested, expected):
                return
        raise AutonomyValidationError(
            'restore backup is not owned by a successful file patch'
        )

    def _runtime_control_probe(
        self, owner: str, role: str, run_id: str,
        *, action, action_digest: str,
        host_address: str,
        host_port: int,
        host_environment: str,
        control_session_factory=None,
        lease_owner: Optional[str] = None,
        lease_token: Optional[str] = None,
        external_probe=None,
    ):
        """Compose worker control with short-lived authoritative DB checks."""
        check_state = {'at': 0.0, 'forced': 2}

        def probe():
            if external_probe is not None:
                reason = external_probe()
                if reason is not None:
                    return reason

            now = time.monotonic()
            force_check = check_state['forced'] > 0
            if force_check:
                check_state['forced'] -= 1
            if not force_check and now - check_state['at'] < 0.5:
                return None
            check_state['at'] = now

            if control_session_factory is None:
                # Production execution must never reuse the executor's long
                # transaction for mid-command authority checks.
                return TERMINATION_AUTHORIZATION_REVOKED
            try:
                session = control_session_factory()
                from app.core.db.database import (
                    t_acc_user, t_ai_autonomous_run,
                    t_ai_autonomous_step, t_host, t_sys_user,
                )

                user = session.query(t_acc_user).filter_by(
                    name=owner, usrole='admin', is_deleted=False,
                ).first()
                if user is None or role != 'admin':
                    return TERMINATION_AUTHORIZATION_REVOKED
                current = session.query(t_ai_autonomous_run).filter_by(
                    id=run_id, owner=owner,
                ).first()
                if current is None:
                    return TERMINATION_AUTHORIZATION_REVOKED
                if bool(current.cancel_requested):
                    return TERMINATION_CANCELLED
                if (
                    lease_owner is not None
                    and (
                        not lease_token
                        or str(current.lease_owner or '') != str(lease_owner)
                        or str(current.lease_token or '') != str(lease_token)
                        or current.lease_expires_at is None
                        or current.lease_expires_at
                        < datetime.datetime.utcnow()
                    )
                ):
                    return TERMINATION_LEASE_LOST

                step = session.query(t_ai_autonomous_step).filter_by(
                    id=action.step_id, run_id=run_id,
                ).first()
                host = session.query(t_host).filter_by(
                    id=int(action.target_id), is_deleted=False,
                ).first()
                credential = session.query(t_sys_user).filter_by(
                    id=int(action.system_user_id), is_deleted=False,
                ).first()
                try:
                    persisted_action = action_from_dict(
                        json.loads(step.action_json or '{}')
                        if step is not None else {}
                    )
                except (ActionValidationError, ValueError):
                    persisted_action = None
                if (
                    step is None
                    or step.status != StepStatus.RUNNING.value
                    or step.action_digest != action_digest
                    or persisted_action != action
                    or not verify_action_digest(
                        action, action_digest, self.repo.secret_key,
                    )
                    or int(current.host_id) != int(action.target_id)
                    or int(current.system_user_id)
                    != int(action.system_user_id)
                    or host is None
                    or credential is None
                    or str(host.host_ip) != host_address
                    or int(host.host_port) != int(host_port)
                    or str(host.ai_environment) != host_environment
                ):
                    return TERMINATION_AUTHORIZATION_REVOKED
                try:
                    validate_mode_for_environment(
                        current.mode, host.ai_environment,
                    )
                except Exception:
                    return TERMINATION_AUTHORIZATION_REVOKED

            except Exception:
                return TERMINATION_AUTHORIZATION_REVOKED
            finally:
                if 'session' in locals():
                    session.close()
            return None

        return probe

    def _reload_execution_rows(
        self, owner: str, run_id: str, step_id: str,
        *, lease_owner: Optional[str] = None,
        lease_token: Optional[str] = None,
    ):
        """Lock and fence fresh authority rows before execution persistence.

        Locking the Run row serializes intent and outcome commits with a
        competing expired-lease claim.  Once the lease is verified under the
        lock, the current transaction commits before a new owner can take
        over.
        """
        from app.core.db.database import (
            t_ai_autonomous_run, t_ai_autonomous_step,
        )

        self.session.rollback()
        run = self.session.query(t_ai_autonomous_run).filter_by(
            id=run_id, owner=owner,
        ).with_for_update().one()
        step = self.session.query(t_ai_autonomous_step).filter_by(
            id=step_id, run_id=run_id,
        ).with_for_update().one()
        if lease_owner is not None and (
            not lease_token
            or str(run.lease_owner or '') != str(lease_owner)
            or str(run.lease_token or '') != str(lease_token)
            or run.lease_expires_at is None
            or run.lease_expires_at < datetime.datetime.utcnow()
        ):
            self.session.rollback()
            raise _ExecutionLeaseLost()
        return run, step

    def _lease_lost_response(
        self, owner: str, run_id: str, step_id: str,
    ) -> Dict[str, Any]:
        self.session.rollback()
        self.session.expire_all()
        current_run = self.repo._get_run_row(owner, run_id)
        current_step = self._get_step_row(run_id, step_id)
        return {
            'step_id': step_id,
            'step_status': current_step.status,
            'run_status': current_run.status,
            'revision': int(current_run.revision or 0),
            'uncertain': True,
            'termination': TERMINATION_LEASE_LOST,
        }

    def _stage_remote_output_artifacts(
        self, owner: str, run_id: str, step_id: str, result: RunnerResult,
        *, action_kind: str = '',
    ) -> Dict[str, str]:
        """Redact and encrypt untrusted SSH streams in the outcome txn."""
        artifacts = {}
        streams = (
            ('stdout', result.output, bool(result.stdout_truncated)),
            ('stderr', result.stderr, bool(result.stderr_truncated)),
        )
        for stream, content, truncated in streams:
            required_empty_diff = (
                stream == 'stdout'
                and action_kind == 'file_patch'
                and result.exit_code == 0
                and not result.uncertain
            )
            if not content and not truncated and not required_empty_diff:
                continue
            kind = 'step_%s' % stream
            title = 'remote %s for step %s' % (stream, step_id[:32])
            if stream == 'stdout' and action_kind == 'file_patch':
                kind = 'patch_diff'
                title = 'file patch diff for step %s' % step_id[:32]
            artifact = self.repo.create_artifact(
                owner, run_id,
                kind=kind,
                title=title,
                content=sanitize_evidence(content),
                step_id=step_id,
                force_truncated=truncated,
                commit=False,
            )
            artifacts[stream] = artifact['id']
        return artifacts

    def _apply_confirmed_cancel(self, run, step):
        """Persist cancellation only after the SSH process group is gone."""
        assert_step_transition(step.status, StepStatus.CANCELLED.value)
        step.status = StepStatus.CANCELLED.value
        step.note = 'cancelled: remote stop confirmed'
        self.repo._bump(run)
        self.repo.append_event(run, 'step_cancelled', {
            'step_id': step.id,
            'stop_confirmed': True,
        })
        self.repo._commit()

    # ------------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------------

    def execute_step(
        self, owner: str, role: str, run_id: str, step_id: str,
        *, timeout_seconds: Optional[float] = None,
        control_probe=None,
        control_session_factory=None,
        lease_owner: Optional[str] = None,
        lease_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行已审批的 Step；任何前置复核失败都不产生副作用。"""
        if lease_owner is not None:
            try:
                run, step = self._reload_execution_rows(
                    owner, run_id, step_id,
                    lease_owner=lease_owner, lease_token=lease_token,
                )
            except _ExecutionLeaseLost:
                return self._lease_lost_response(owner, run_id, step_id)
        else:
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

        if str(action.kind) == 'file_restore':
            self._validate_restore_source(action, run_id)
        host = self.repo._get_host_row(run.host_id)
        host_address = str(host.host_ip)
        command = self._build_command(
            action, run_id, target_host=host_address,
        )
        try:
            validate_working_directory(str(action.working_directory or ''))
        except ValueError as exc:
            raise AutonomyValidationError(str(exc)) from exc

        timeout = float(min(
            budget.command_timeout_seconds, action.timeout_seconds,
        ))
        if timeout_seconds is not None:
            try:
                remaining_timeout = float(timeout_seconds)
            except (TypeError, ValueError):
                raise AutonomyConflict(
                    'remaining run timeout is invalid',
                ) from None
            if remaining_timeout <= 0:
                raise AutonomyConflict('run duration budget exhausted')
            timeout = min(timeout, remaining_timeout)
        is_write = action.kind in WRITE_KINDS
        host_port = int(host.host_port)
        host_alias = str(host.alias or run.host_alias or '')
        host_environment = str(host.ai_environment or '')
        # 状态推进 + 持久审计（必须在任何远程副作用之前提交）。
        assert_step_transition(step.status, StepStatus.RUNNING.value)
        step.status = StepStatus.RUNNING.value
        if run.status == RunStatus.QUEUED.value:
            assert_run_transition(run.status, RunStatus.RUNNING.value)
            run.status = RunStatus.RUNNING.value
        self.repo._bump(run)
        self.repo.append_event(run, 'step_execution_started', {
            'step_id': step_id,
            'kind': str(action.kind),
            'target_id': int(action.target_id),
            'action_digest': str(step.action_digest or '')[:12],
        })
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
            host_alias=host_alias,
            system_user_alias=str(run.system_user_alias or ''),
            timeout_seconds=timeout,
            step_id=step_id,
            host=host_address,
            port=host_port,
            working_directory=str(action.working_directory or ''),
            max_output_bytes=int(budget.step_output_bytes),
            control_probe=self._runtime_control_probe(
                owner, role, run_id,
                action=action,
                action_digest=str(step.action_digest or ''),
                host_address=host_address,
                host_port=host_port,
                host_environment=host_environment,
                control_session_factory=control_session_factory,
                lease_owner=lease_owner,
                lease_token=lease_token,
                external_probe=control_probe,
            ),
            audit_name=owner,
            action_kind=str(action.kind),
            action_digest=str(step.action_digest or ''),
        )

        if (
            result.termination == TERMINATION_LEASE_LOST
            or result.control_reason == TERMINATION_LEASE_LOST
        ):
            # This worker no longer owns persistence authority. The next owner
            # recovers from the committed write_intent and running Step.
            return self._lease_lost_response(owner, run_id, step_id)

        try:
            run, step = self._reload_execution_rows(
                owner, run_id, step_id, lease_owner=lease_owner,
                lease_token=lease_token,
            )
        except _ExecutionLeaseLost:
            return self._lease_lost_response(owner, run_id, step_id)

        # A successful file patch cannot be reported as succeeded unless the
        # complete deterministic rollback reference can be committed in the
        # same outcome transaction.  Stage this tiny required artifact before
        # the bounded diff so output cannot consume its remaining budget.
        if (
            str(action.kind) == 'file_patch'
            and result.exit_code == 0
            and not result.uncertain
        ):
            backup = patch_backup_path(
                str(action.parameters.get('path') or ''),
                run_id,
                str(action.step_id),
            )
            self.repo.create_artifact(
                owner, run_id,
                kind='backup_ref',
                title='file_patch backup for %s'
                % str(action.parameters.get('path') or '')[:96],
                content=backup,
                step_id=step_id,
                require_full_content=True,
                commit=False,
            )
        output_artifacts = self._stage_remote_output_artifacts(
            owner, run_id, step_id, result,
            action_kind=str(action.kind),
        )
        if result.control_reason == TERMINATION_AUTHORIZATION_REVOKED:
            if is_write and result.started is not False:
                # Revocation stops further execution, but cannot erase a
                # partially-applied write.
                self._apply_result(
                    run, step, action, result, is_write,
                    output_artifacts=output_artifacts,
                )
                return {
                    'step_id': step_id,
                    'step_status': step.status,
                    'run_status': run.status,
                    'revision': int(run.revision or 0),
                    'uncertain': True,
                    'termination': TERMINATION_AUTHORIZATION_REVOKED,
                }
            if not result.stop_confirmed and result.started is not False:
                assert_step_transition(step.status, StepStatus.FAILED.value)
                step.status = StepStatus.FAILED.value
                step.note = 'authorization revoked; remote stop unconfirmed'
                assert_run_transition(
                    run.status, RunStatus.NEEDS_ATTENTION.value,
                )
                run.status = RunStatus.NEEDS_ATTENTION.value
                self.repo._bump(run)
                self.repo.append_event(
                    run, 'execution_authorization_revoked', {
                        'step_id': step.id,
                        'stop_confirmed': False,
                    },
                )
                self.repo._commit()
                return {
                    'step_id': step_id,
                    'step_status': step.status,
                    'run_status': run.status,
                    'revision': int(run.revision or 0),
                    'uncertain': True,
                    'termination': TERMINATION_AUTHORIZATION_REVOKED,
                }
            assert_step_transition(step.status, StepStatus.FAILED.value)
            step.status = StepStatus.FAILED.value
            step.note = 'authorization revoked during remote execution'
            assert_run_transition(run.status, RunStatus.FAILED.value)
            run.status = RunStatus.FAILED.value
            self.repo._bump(run)
            self.repo.append_event(run, 'execution_authorization_revoked', {
                'step_id': step.id,
                'stop_confirmed': bool(result.stop_confirmed),
            })
            self.repo._commit()
            return {
                'step_id': step_id,
                'step_status': step.status,
                'run_status': run.status,
                'revision': int(run.revision or 0),
                'uncertain': bool(is_write and result.started is not False),
                'termination': TERMINATION_AUTHORIZATION_REVOKED,
            }

        if (
            result.termination == TERMINATION_CANCELLED
            and result.stop_confirmed
        ):
            if is_write and result.started is not False:
                # Stopping a process group does not prove an already-started
                # write had no partial effect. Preserve the uncertain outcome.
                self._apply_result(
                    run, step, action, result, is_write,
                    output_artifacts=output_artifacts,
                )
                return {
                    'step_id': step_id,
                    'step_status': step.status,
                    'run_status': run.status,
                    'revision': int(run.revision or 0),
                    'uncertain': True,
                    'termination': TERMINATION_CANCELLED,
                }
            self._apply_confirmed_cancel(run, step)
            return {
                'step_id': step_id,
                'step_status': step.status,
                'run_status': run.status,
                'revision': int(run.revision or 0),
                'uncertain': False,
                'termination': TERMINATION_CANCELLED,
            }

        self._apply_result(
            run, step, action, result, is_write,
            output_artifacts=output_artifacts,
        )
        return {
            'step_id': step_id,
            'step_status': step.status,
            'run_status': run.status,
            'revision': int(run.revision or 0),
            'uncertain': bool(result.uncertain and is_write),
            'termination': str(result.termination or ''),
        }

    # ------------------------------------------------------------------
    # 结果落库
    # ------------------------------------------------------------------

    def _apply_result(
        self, run, step, action, result, is_write,
        *, output_artifacts=None,
    ):
        output_artifacts = output_artifacts or {}

        if result.uncertain:
            if is_write:
                # 写可能已生效但结果未落库：fail-closed，人工介入，
                # 绝不自动重放。
                assert_step_transition(
                    step.status, StepStatus.OUTCOME_UNKNOWN.value,
                )
                step.status = StepStatus.OUTCOME_UNKNOWN.value
                step.note = sanitize_evidence(
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
                # A normal read transport error remains retryable. If remote
                # stop could not be confirmed, this Run requires attention so
                # the graph cannot later finalize it as completed.
                assert_step_transition(step.status, StepStatus.FAILED.value)
                step.status = StepStatus.FAILED.value
                step.note = 'transport_error_retryable: ' + sanitize_evidence(
                    result.transport_error or 'transport interrupted',
                )[:200]
                if result.termination == 'stop_unconfirmed':
                    assert_run_transition(
                        run.status, RunStatus.NEEDS_ATTENTION.value,
                    )
                    run.status = RunStatus.NEEDS_ATTENTION.value
                self.repo._bump(run)
                self.repo.append_event(run, 'step_executed', {
                    'step_id': step.id, 'outcome': 'transport_error',
                })
            self.repo._commit()
            return

        note = (
            'exit_code=%s; stdout_artifact=%s; stderr_artifact=%s; '
            'output_truncated=%s'
        ) % (
            result.exit_code,
            output_artifacts.get('stdout', 'none'),
            output_artifacts.get('stderr', 'none'),
            str(bool(
                result.stdout_truncated or result.stderr_truncated
            )).lower(),
        )
        if result.exit_code == 0:
            assert_step_transition(step.status, StepStatus.SUCCEEDED.value)
            step.status = StepStatus.SUCCEEDED.value
            step.note = note[:255]
            outcome = 'succeeded'
        else:
            assert_step_transition(step.status, StepStatus.FAILED.value)
            step.status = StepStatus.FAILED.value
            step.note = note[:255]
            outcome = 'failed'
        self.repo._bump(run)
        self.repo.append_event(run, 'step_executed', {
            'step_id': step.id,
            'outcome': outcome,
            'exit_code': int(result.exit_code),
        })
        self.repo._commit()
