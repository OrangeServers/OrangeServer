# -*- coding: utf-8 -*-
"""M1/S2: 自治 Run 的驱动循环——图 + 执行器 + 租约心跳的接线。

设计要点：
- LangGraph 只是流程游标：驱动循环的每一步都以 MySQL 的 Run/Step
  为权威。恢复（resume）值不是人读的决策文本，而是从 MySQL 的
  Step 状态重新推导（approved→approve / rejected→reject），
  checkpoint 里存的决策永不覆盖权威状态。
- 暂停即释放：图停在 approval_pause 时把 Run 置 waiting_approval
  并释放租约，等人审批期间不占租约、不产生过期 churn；决策接口
  把 Run 推回 queued 并显式重新投递 drive_run。
- checkpoint fail-closed：saver 不可用、图版本未知或规划器未接线
  时，Run 落明确的失败/待处理状态并释放租约，绝不在没有流程游标
  的情况下产生远程副作用。
- 租约丢失即中止：心跳续租失败后，驱动循环在下一个节点边界立刻
  中止，不再产生任何副作用，也不释放（已不属于自己）的租约。
- v1 兼容图仍让所有动作经过 approval_pause；v2 使用服务端
  allow/ask/deny 决策，仅 ask 暂停。role 固定 'admin'（自治入口在
  路由层强制管理员），多角色支持需先把 role 持久化到 Run 行。
"""
import datetime
import json
import logging
import threading
from contextlib import contextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from app.ai.autonomy.actions import (
    ActionValidationError,
    action_from_dict,
    verify_action_digest,
)
from app.ai.autonomy.executor import AutonomyExecutor
from app.ai.autonomy.graph import AutonomyGraphError, build_graph
from app.ai.autonomy.lease import RunLeaseService
from app.ai.autonomy.policy import Budget, PolicyDecision, classify_action
from app.ai.autonomy.recovery import (
    MODE_BOUNDARY,
    MODE_FRESH,
    MODE_HALT,
    MODE_PAUSE,
    MODE_RESUME,
    RecoveryService,
)
from app.ai.autonomy.repository import sanitize_text
from app.ai.autonomy.state import (
    RunStatus,
    StepKind,
    StepStatus,
    assert_run_transition,
    assert_step_transition,
)
from app.core import config

logger = logging.getLogger('autonomy_driver')

THREAD_ID_PREFIX = 'ogs-autonomy-run-'

RESULT_PAUSED = 'paused'
RESULT_COMPLETED = 'completed'
RESULT_FAILED = 'failed'
RESULT_CANCELLED = 'cancelled'
RESULT_NEEDS_ATTENTION = 'needs_attention'
RESULT_SKIPPED = 'skipped'
RESULT_CHECKPOINT_UNAVAILABLE = 'checkpoint_unavailable'
RESULT_LEASE_LOST = 'lease_lost'

# 无待审步骤时的自动恢复值：图仍需走完 execute→decide 收尾。
RESUME_CONTINUE = 'continue'

# 单次 drive 内自动恢复次数上限：防止 decide 不收敛时无限自恢复；
# 正常循环每轮最多一次自动恢复，上限已含充分余量。
MAX_RESUMES_PER_DRIVE = 64


class DriveAbort(Exception):
    """租约丢失等致命前置条件不满足：立即中止，不再产生副作用。"""


class DurationBudgetExhausted(Exception):
    """Run 的持久化墙钟预算已耗尽，不得再开始新的副作用。"""


class _ClaimFencedPlannerRepository:
    """Expose planner proposals only through the current locked claim."""

    def __init__(self, repo, lock_claim):
        self._repo = repo
        self._lock_claim = lock_claim

    def propose_probe(
        self, owner, role, run_id, probe_id, params=None,
    ):
        try:
            self._lock_claim(run_id)
            return self._repo.propose_probe(
                owner, role, run_id, probe_id, params,
            )
        except Exception:
            self._repo.session.rollback()
            raise


class _ClaimFencedCheckpointSaver(BaseCheckpointSaver):
    """Fence each checkpoint write with the exact authoritative Run claim."""

    def __init__(self, saver, write_fence):
        super().__init__(serde=saver.serde)
        self._saver = saver
        self._write_fence = write_fence

    def __getattr__(self, name):
        return getattr(self._saver, name)

    @property
    def config_specs(self):
        return self._saver.config_specs

    def get_tuple(self, *args, **kwargs):
        return self._saver.get_tuple(*args, **kwargs)

    def list(self, *args, **kwargs):
        return self._saver.list(*args, **kwargs)

    def get_next_version(self, *args, **kwargs):
        return self._saver.get_next_version(*args, **kwargs)

    def get_delta_channel_history(self, *args, **kwargs):
        return self._saver.get_delta_channel_history(*args, **kwargs)

    def put(self, *args, **kwargs):
        with self._write_fence():
            return self._saver.put(*args, **kwargs)

    def put_writes(self, *args, **kwargs):
        with self._write_fence():
            return self._saver.put_writes(*args, **kwargs)


class LeaseHeartbeat:
    """后台续租线程：renew_fn 返回假值或抛错即标记 lost 并退出。

    生产实现用独立 session 做条件 UPDATE；测试注入替身 renew_fn，
    不碰真实连接。
    """

    def __init__(self, renew_fn, interval_seconds):
        self._renew = renew_fn
        self._interval = max(0.01, float(interval_seconds))
        self._stop = threading.Event()
        self._lost = False
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name='autonomy-lease-heartbeat', daemon=True,
        )
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                ok = self._renew()
            except Exception:
                logger.exception('lease heartbeat renew failed')
                ok = None
            if not ok:
                self._lost = True
                break

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 3)

    @property
    def lost(self):
        return self._lost


class AutonomyDriver:
    """session 注入式驱动循环；planner/saver/心跳均可替身注入。"""

    def __init__(
        self, session, secret_key, *,
        planner=None,
        runner=None,
        platform_factory=None,
        saver_factory=None,
        heartbeater_factory=None,
        heartbeat_session_factory=None,
        worker_id=None,
        lease_ttl=None,
        role='admin',
    ):
        self.session = session
        self.role = role
        self.planner = planner
        self.saver_factory = saver_factory
        self.executor = AutonomyExecutor(
            session, secret_key, runner=runner,
            platform_factory=platform_factory,
        )
        self.repo = self.executor.repo
        self.lease = RunLeaseService(session)
        self.recovery = RecoveryService(session, self.repo)
        self.worker_id = worker_id
        self.lease_ttl = lease_ttl or config.AI_AUTONOMY_LEASE_TTL_SECONDS
        self._heartbeat_session_factory = heartbeat_session_factory
        self._heartbeater_factory = heartbeater_factory or LeaseHeartbeat
        self._heartbeater = None
        self._revision_state = {'revision': 0}
        self._duration_deadline = None
        self._active_lease_token = ''

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _run_row(self, run_id):
        from app.core.db.database import t_ai_autonomous_run

        return self.session.query(t_ai_autonomous_run).filter_by(
            id=run_id,
        ).first()

    def _step_row(self, run_id, step_id):
        from app.core.db.database import t_ai_autonomous_step

        return self.session.query(t_ai_autonomous_step).filter_by(
            id=step_id, run_id=run_id,
        ).first()

    def _guard(self):
        """节点边界守卫：租约或墙钟预算失效即停止推进。"""
        if self._heartbeater is not None and self._heartbeater.lost:
            raise DriveAbort('lease lost during drive')
        self._remaining_duration_seconds()

    def _execution_control_probe(self):
        """SSH 热路径快速检查；完整权限复核由独立控制 session 执行。"""
        if self._heartbeater is not None and self._heartbeater.lost:
            return 'lease_lost'
        return None

    def _configure_duration_budget(self, run):
        """用持久化 started_at 建立跨投递/重启一致的 Run 截止时间。"""
        try:
            budget = Budget(**json.loads(run.budget_json or '{}'))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DurationBudgetExhausted('invalid duration budget') from exc
        started_at = run.started_at or getattr(run, 'created_at', None)
        if started_at is None:
            raise DurationBudgetExhausted('missing run start time')
        self._duration_deadline = started_at + datetime.timedelta(
            seconds=int(budget.duration_seconds),
        )
        return budget

    def _remaining_duration_seconds(self):
        """返回可交给整数秒命令超时的剩余额度；不足 1 秒即耗尽。"""
        if self._duration_deadline is None:
            return int(Budget().duration_seconds)
        remaining = int(
            (self._duration_deadline - _utcnow()).total_seconds()
        )
        if remaining < 1:
            raise DurationBudgetExhausted('run duration budget exhausted')
        return remaining

    def _refresh_revision(self):
        row = self._run_row(getattr(self, '_active_run_id', ''))
        if row is not None:
            self._revision_state['revision'] = int(row.revision or 0)

    def _release_lease(self, run_id):
        self.lease.release_lease(
            run_id, self._identity(), self._active_lease_token,
        )

    def _lock_claim_in_session(self, session, run_id):
        """Lock the Run and prove this exact claim still owns persistence."""
        from app.core.db.database import t_ai_autonomous_run

        session.rollback()
        session.expire_all()
        current = session.query(t_ai_autonomous_run).filter_by(
            id=run_id,
        ).with_for_update().first()
        now = _utcnow()
        if (
            current is None
            or str(current.lease_owner or '') != self._identity()
            or not self._active_lease_token
            or str(current.lease_token or '') != self._active_lease_token
            or current.lease_expires_at is None
            or current.lease_expires_at < now
        ):
            session.rollback()
            raise DriveAbort('lease claim fence lost')
        return current

    def _lock_current_claim(self, run_id):
        return self._lock_claim_in_session(self.session, run_id)

    @contextmanager
    def _checkpoint_write_fence(self, run_id):
        """Hold the Run row lock across one Redis checkpoint write."""
        session = self.session
        owns_session = False
        if self._heartbeat_session_factory is not None:
            session = self._heartbeat_session_factory()
            owns_session = True
        try:
            self._lock_claim_in_session(session, run_id)
            yield
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()

    def _fence_checkpoint_saver(self, run_id, saver):
        return _ClaimFencedCheckpointSaver(
            saver, lambda: self._checkpoint_write_fence(run_id),
        )

    @staticmethod
    def _clear_claim(run):
        run.lease_owner = None
        run.lease_token = None
        run.lease_expires_at = None

    def _identity(self):
        from app.ai.autonomy.worker import worker_identity

        return self.worker_id or worker_identity()

    def _fail_run(self, run, event_type, note):
        """前置条件失败：Run 落终态 failed + 事件，释放租约。"""
        run = self._lock_current_claim(run.id)
        assert_run_transition(run.status, RunStatus.FAILED.value)
        run.status = RunStatus.FAILED.value
        run.completed_at = run.completed_at or _utcnow()
        self.repo._bump(run)
        self.repo.append_event(run, event_type, {'note': note[:128]})
        self._clear_claim(run)
        self.repo._commit()

    def _checkpoint_unavailable(self, run, note):
        """Persist a stable fail-closed checkpoint failure and release claim."""
        current = self._lock_current_claim(run.id)
        if current.status != RunStatus.NEEDS_ATTENTION.value:
            assert_run_transition(
                current.status, RunStatus.NEEDS_ATTENTION.value,
            )
            current.status = RunStatus.NEEDS_ATTENTION.value
        self.repo._bump(current)
        self.repo.append_event(current, 'checkpoint_unavailable', {
            'reason': sanitize_text(note)[:64],
        })
        self._clear_claim(current)
        self.repo._commit()
        return RESULT_CHECKPOINT_UNAVAILABLE

    # ------------------------------------------------------------------
    # 节点 handlers
    # ------------------------------------------------------------------

    def _build_handlers(self, run_id):
        planner_repo = _ClaimFencedPlannerRepository(
            self.repo, self._lock_current_claim,
        )

        def plan(state):
            self._guard()
            if self.planner is None:
                raise PlannerUnavailable('planner not wired')
            # Goal is authoritative MySQL input. Never copy it into Graph
            # State/checkpoints, where user-supplied secrets would persist in
            # Redis AOF. The planner receives it only at the call boundary.
            current_run = self._run_row(run_id)
            context = {
                'run_id': run_id,
                'owner': str(state.get('owner') or ''),
                'role': self.role,
                'goal': str(current_run.goal or ''),
                'loops': int(state.get('loops', 0)),
                'repo': planner_repo,
            }
            proposed = list(self.planner(context) or [])
            return {
                'proposed_steps': len(proposed),
                'summary': 'planned %d step(s)' % len(proposed),
            }

        def policy(state):
            self._guard()
            from app.core.db.database import t_ai_autonomous_step

            def proposed_steps():
                return (
                    self.session.query(t_ai_autonomous_step)
                    .filter_by(
                        run_id=run_id, kind=StepKind.ACTION.value,
                        status=StepStatus.PROPOSED.value,
                    )
                    .order_by(t_ai_autonomous_step.seq.asc())
                    .all()
                )

            steps = proposed_steps()
            if not steps:
                return {
                    'pending_step_id': '',
                    'policy_decision': PolicyDecision.ALLOW.value,
                    'decision': '',
                }
            run = self._lock_current_claim(run_id)
            steps = proposed_steps()
            if not steps:
                self.repo._commit()
                return {
                    'pending_step_id': '',
                    'policy_decision': PolicyDecision.ALLOW.value,
                    'decision': '',
                }
            if str(run.graph_version or '') != 'v2':
                for step in steps:
                    assert_step_transition(
                        step.status, StepStatus.WAITING_APPROVAL.value,
                    )
                    step.status = StepStatus.WAITING_APPROVAL.value
                assert_run_transition(
                    run.status, RunStatus.WAITING_APPROVAL.value,
                )
                run.status = RunStatus.WAITING_APPROVAL.value
                self.repo._bump(run)
                self.repo.append_event(run, 'steps_waiting_approval', {
                    'step_ids': [step.id for step in steps],
                })
                self.repo._commit()
                return {'pending_step_id': steps[0].id}

            # v2 evaluates one immutable action snapshot per graph loop. The
            # executor will independently revalidate digest and permissions
            # immediately before any side effect.
            step = steps[0]
            try:
                snapshot = json.loads(step.action_json or '')
                action = action_from_dict(snapshot)
                if not verify_action_digest(
                    action, step.action_digest, self.repo.secret_key,
                ):
                    decision = PolicyDecision.DENY
                    reason = 'action digest mismatch'
                else:
                    host = self.repo._get_host_row(run.host_id)
                    decision, reason = classify_action(
                        run.mode, action, host.ai_environment,
                    )
            except (ActionValidationError, TypeError, ValueError):
                decision = PolicyDecision.DENY
                reason = 'malformed action snapshot'

            if decision == PolicyDecision.ALLOW:
                assert_step_transition(step.status, StepStatus.APPROVED.value)
                step.status = StepStatus.APPROVED.value
                step.note = 'allowed by server policy'
            elif decision == PolicyDecision.ASK:
                assert_step_transition(
                    step.status, StepStatus.WAITING_APPROVAL.value,
                )
                step.status = StepStatus.WAITING_APPROVAL.value
                if run.status != RunStatus.WAITING_APPROVAL.value:
                    assert_run_transition(
                        run.status, RunStatus.WAITING_APPROVAL.value,
                    )
                    run.status = RunStatus.WAITING_APPROVAL.value
            else:
                assert_step_transition(step.status, StepStatus.FAILED.value)
                step.status = StepStatus.FAILED.value
                step.note = reason[:255]

            self.repo._bump(run)
            self.repo.append_event(run, 'step_policy_decided', {
                'step_id': step.id,
                'decision': decision.value,
                'reason': reason,
            })
            if decision == PolicyDecision.ASK:
                self.repo.append_event(run, 'steps_waiting_approval', {
                    'step_ids': [step.id],
                })
            self.repo._commit()
            return {
                'pending_step_id': step.id,
                'policy_decision': decision.value,
                'decision': '',
            }

        def execute(state):
            self._guard()
            step_id = str(state.get('pending_step_id') or '')
            decision = str(state.get('decision') or '')
            if not step_id or decision == RESUME_CONTINUE:
                return {}
            run = self._run_row(run_id)
            if bool(run.cancel_requested):
                # 取消是请求：收到后不再开跑新 Step。
                return {'summary': 'step skipped: cancel requested'}
            step = self._step_row(run_id, step_id)
            if step is None:
                return {'summary': 'step vanished'}
            if step.status != StepStatus.APPROVED.value:
                # 被拒绝（或已被其他路径处理）：绝不执行。
                return {
                    'summary': 'step skipped: %s' % (step.status,),
                }
            result = self.executor.execute_step(
                str(run.owner), self.role, run_id, step_id,
                timeout_seconds=self._remaining_duration_seconds(),
                control_probe=self._execution_control_probe,
                control_session_factory=self._heartbeat_session_factory,
                lease_owner=self._identity(),
                lease_token=self._active_lease_token,
            )
            self._revision_state['revision'] = int(result['revision'])
            if result.get('termination') == 'lease_lost':
                raise DriveAbort('lease lost during remote execution')
            return {'summary': 'step %s' % (result['step_status'],)}

        def observe(state):
            self._guard()
            step_id = str(state.get('pending_step_id') or '')
            if not step_id:
                return {}
            step = self._step_row(run_id, step_id)
            if step is None:
                return {}
            return {
                'summary': sanitize_text(
                    '%s: %s' % (step.status, step.note or ''),
                )[:120],
            }

        def verify(state):
            # v1 无独立验证节点（S3）；游标原样通过。
            self._guard()
            return {}

        def decide(state):
            self._guard()
            run = self._run_row(run_id)
            if bool(run.cancel_requested):
                return {'done': True, 'decision': 'cancelled'}
            budget = Budget(**json.loads(run.budget_json or '{}'))
            loops = int(state.get('loops', 0)) + 1
            if loops >= int(budget.max_loops):
                return {'done': True, 'decision': 'budget_exhausted'}
            if int(state.get('proposed_steps', 0)) == 0:
                return {'done': True, 'decision': 'exhausted'}
            return {'done': False}

        return {
            'plan': plan,
            'policy': policy,
            'execute': execute,
            'observe': observe,
            'verify': verify,
            'decide': decide,
        }

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------

    def _start_heartbeat(self, run_id, claimed):
        self._revision_state['revision'] = int(claimed.get('revision') or 0)
        interval = max(1, self.lease_ttl // 4)

        def renew():
            session = self.session
            own_session = False
            if self._heartbeat_session_factory is not None:
                session = self._heartbeat_session_factory()
                own_session = True
            try:
                result = RunLeaseService(session).heartbeat(
                    run_id, self._identity(), self._active_lease_token,
                    self.lease_ttl,
                )
                if result is None:
                    return None
                self._revision_state['revision'] = int(result['revision'])
                return result
            finally:
                if own_session:
                    session.close()

        self._heartbeater = self._heartbeater_factory(renew, interval)
        self._heartbeater.start()
        return self._heartbeater

    # ------------------------------------------------------------------
    # 恢复（resume）决策推导：只认 MySQL，不认 checkpoint 里的值
    # ------------------------------------------------------------------

    def _pending_decision(self, run_id, state):
        """从 MySQL 推导恢复值；仍在等人审批时返回 None。

        只认 Step 的权威状态：approved→approve，failed→reject，
        waiting_approval→等人，其余（已执行完或步骤丢失）一律
        自动恢复走完收尾。
        """
        step_id = str((state.values or {}).get('pending_step_id') or '')
        if not step_id:
            return RESUME_CONTINUE
        step = self._step_row(run_id, step_id)
        if step is None:
            return RESUME_CONTINUE
        if step.status == StepStatus.APPROVED.value:
            return 'approve'
        if step.status == StepStatus.FAILED.value:
            return 'reject'
        if step.status == StepStatus.WAITING_APPROVAL.value:
            return None
        return RESUME_CONTINUE

    # ------------------------------------------------------------------
    # 驱动入口
    # ------------------------------------------------------------------

    def drive(self, run_id, claimed):
        """驱动一个已认领的 Run 直到暂停、终态或中止。

        返回 RESULT_* 之一。绝不抛异常给任务层：所有可预期失败都
        落库为明确的 Run 状态。
        """
        self._active_run_id = run_id
        self._active_lease_token = str(claimed.get('lease_token') or '')
        try:
            return self._drive_inner(run_id, claimed)
        except DriveAbort:
            return RESULT_LEASE_LOST
        except Exception:
            logger.exception('drive_run unexpected error for %s', run_id)
            # 未知异常：保留现场（不释放租约），让租约过期后由
            # 恢复扫描接管，绝不静默吞掉。
            return RESULT_NEEDS_ATTENTION

    def _drive_inner(self, run_id, claimed):
        run = self._run_row(run_id)
        if run is None or run.status in {
            s.value for s in (
                RunStatus.COMPLETED, RunStatus.FAILED,
                RunStatus.CANCELLED, RunStatus.EXPIRED,
            )
        }:
            return RESULT_SKIPPED

        # 写结果未知的重放守卫：needs_attention 只能人工处置，
        # 再被投递或随后请求取消也绝不改写为 cancelled。
        if run.status == RunStatus.NEEDS_ATTENTION.value:
            self._release_lease(run_id)
            return RESULT_NEEDS_ATTENTION

        # 取消是请求：开跑前无进行中副作用，可直接确认。
        if bool(run.cancel_requested):
            return self._confirm_cancel(run)

        try:
            self._configure_duration_budget(run)
            self._remaining_duration_seconds()
        except DurationBudgetExhausted:
            self._fail_run(
                run, 'budget_exhausted', 'run duration budget exhausted',
            )
            return RESULT_FAILED

        if self.saver_factory is None:
            return self._checkpoint_unavailable(run, 'saver_not_configured')

        try:
            saver, close_saver = self.saver_factory()
        except Exception:
            logger.exception('checkpoint saver creation failed for %s', run_id)
            return self._checkpoint_unavailable(run, 'saver_creation_failed')
        saver = self._fence_checkpoint_saver(run_id, saver)
        try:
            try:
                return self._drive_graph(run, claimed, saver)
            except Exception as exc:
                if _is_checkpoint_error(exc):
                    logger.exception(
                        'checkpoint saver access failed for %s', run_id,
                    )
                    return self._checkpoint_unavailable(
                        run, 'saver_access_failed',
                    )
                raise
        finally:
            try:
                close_saver()
            except Exception:
                logger.exception('checkpoint saver close failed for %s', run_id)

    def _drive_graph(self, run, claimed, saver):
        run_id = run.id
        try:
            builder = build_graph(
                str(run.graph_version or ''), self._build_handlers(run_id),
            )
        except AutonomyGraphError as exc:
            self._fail_run(
                run, 'unknown_graph_version', 'graph error: %s' % (exc,),
            )
            return RESULT_FAILED
        compiled = builder.compile(checkpointer=saver)
        cfg = {'configurable': {'thread_id': THREAD_ID_PREFIX + run_id}}

        snapshot = compiled.get_state(cfg)
        # checkpoint 丢失检测：空线程的快照没有 metadata；有 Step
        # 落库却没有 checkpoint 即为丢失，只能从 MySQL 边界重建。
        checkpoint_present = snapshot.metadata is not None
        paused = 'approval_pause' in (snapshot.next or ())
        if (
            paused
            and checkpoint_present
            and run.status != RunStatus.RECOVERING.value
        ):
            decision = self._pending_decision(run_id, snapshot)
            if decision is None:
                # 决策未到：继续等人，释放租约保持暂停。
                self._settle_paused(run)
                return RESULT_PAUSED
            entry = Command(resume=decision)
        else:
            run = self._lock_current_claim(run_id)
            outcome = self.recovery.resolve(
                run, checkpoint_present=checkpoint_present,
            )
            if outcome.mode == MODE_HALT:
                self._release_lease(run_id)
                return outcome.result
            if outcome.mode == MODE_PAUSE:
                # 有待审 Step 但 checkpoint 丢失：继续等人审批。
                self._settle_paused(run)
                return RESULT_PAUSED
            if outcome.mode == MODE_BOUNDARY:
                # 从 MySQL 权威边界重建 checkpoint。Saver wrapper 会用
                # 独立 session 在 Redis 写期间持有 exact-claim Run 行
                # 锁；这里不能先用主 session 持同一锁，否则真实
                # MySQL 会与 wrapper 自锁。
                try:
                    compiled.update_state(
                        cfg, outcome.entry, as_node=outcome.as_node,
                    )
                except Exception:
                    self.session.rollback()
                    raise
                # Redis 写后再次同步 fencing；若接管恰好发生在两者
                # 之间，旧 Worker 立即退出，不解释或执行该游标。
                self._lock_current_claim(run_id)
                self.repo._commit()
                snapshot = compiled.get_state(cfg)
                if outcome.as_node == 'policy':
                    decision = self._pending_decision(run_id, snapshot)
                    if decision is None:
                        self._settle_paused(run)
                        return RESULT_PAUSED
                    entry = Command(resume=decision)
                else:
                    # as_node=plan 的下一节点是 policy；沿刚重建的
                    # checkpoint 继续，绝不再次调用 planner。
                    entry = None
            elif outcome.mode == MODE_RESUME:
                # 健康 checkpoint 的原生续跑输入是 None。重新传初始
                # dict 会错误回到 plan 并重置循环预算。
                entry = None
            elif outcome.mode == MODE_FRESH:
                entry = {
                    'run_id': run_id,
                    'graph_version': str(run.graph_version or ''),
                    'owner': str(run.owner or ''),
                    'loops': 0,
                    'proposed_steps': 0,
                }
            else:  # pragma: no cover - RecoveryService mode set is closed.
                raise DriveAbort('unsupported recovery mode')

        heartbeater = self._start_heartbeat(run_id, claimed)
        final_state = None
        resumes = 0
        try:
            try:
                while True:
                    final_state = compiled.invoke(entry, cfg)
                    state = compiled.get_state(cfg)
                    if 'approval_pause' not in (state.next or ()):
                        break
                    # 循环回到审批点的新一轮中断：有决策就继续，
                    # 无待审步骤就自动恢复走完收尾。
                    decision = self._pending_decision(run_id, state)
                    if decision is None:
                        # policy 已把 Run 置 waiting_approval；
                        # 释放租约等人审批。
                        self._settle_paused(run)
                        return RESULT_PAUSED
                    resumes += 1
                    if resumes > MAX_RESUMES_PER_DRIVE:
                        raise DriveAbort(
                            'resume loop did not converge',
                        )
                    entry = Command(resume=decision)
            except PlannerUnavailable:
                self._fail_run(run, 'planner_unavailable', 'planner not wired')
                return RESULT_FAILED
            except DurationBudgetExhausted:
                self.session.expire_all()
                current = self._run_row(run_id)
                if current.status == RunStatus.NEEDS_ATTENTION.value:
                    self._release_lease(run_id)
                    return RESULT_NEEDS_ATTENTION
                self._fail_run(
                    current, 'budget_exhausted',
                    'run duration budget exhausted',
                )
                return RESULT_FAILED
            except DriveAbort:
                # 租约已不属于自己或循环不收敛：不释放租约。
                return RESULT_LEASE_LOST
        finally:
            heartbeater.stop()
        if heartbeater.lost:
            return RESULT_LEASE_LOST
        return self._finalize(run_id, dict(final_state or {}))

    def _settle_paused(self, run):
        """暂停落定：recovering 接管后重新暂停要回 waiting_approval，
        避免留在 recovering 被恢复扫描反复认领。"""
        row = self._lock_current_claim(run.id)
        if row is not None and row.status == RunStatus.RECOVERING.value:
            assert_run_transition(
                row.status, RunStatus.WAITING_APPROVAL.value,
            )
            row.status = RunStatus.WAITING_APPROVAL.value
            self.repo._bump(row)
        self._clear_claim(row)
        self.repo._commit()
    # Claim-fenced terminal transitions keep status and lease atomic.

    def _confirm_cancel(self, run):
        run = self._lock_current_claim(run.id)
        assert_run_transition(run.status, RunStatus.CANCELLED.value)
        run.status = RunStatus.CANCELLED.value
        run.completed_at = run.completed_at or _utcnow()
        self.repo._bump(run)
        self.repo.append_event(run, 'run_cancelled', {
            'note': 'confirmed before side effects',
        })
        self._clear_claim(run)
        self.repo._commit()
        return RESULT_CANCELLED

    def _finalize(self, run_id, final_state):
        run = self._lock_current_claim(run_id)
        decision = str(final_state.get('decision') or '')

        if run.status == RunStatus.NEEDS_ATTENTION.value:
            # 执行器已因未知写结果或停止未确认落 needs_attention：
            # 即使取消请求同时到达也必须保留，等待人工核对。
            result = RESULT_NEEDS_ATTENTION
            event = None
        elif decision == 'cancelled' or bool(run.cancel_requested):
            result = RESULT_CANCELLED
            assert_run_transition(run.status, RunStatus.CANCELLED.value)
            run.status = RunStatus.CANCELLED.value
            event = 'run_cancelled'
        elif decision == 'budget_exhausted':
            result = RESULT_FAILED
            assert_run_transition(run.status, RunStatus.FAILED.value)
            run.status = RunStatus.FAILED.value
            event = 'budget_exhausted'
        else:
            result = RESULT_COMPLETED
            assert_run_transition(run.status, RunStatus.COMPLETED.value)
            run.status = RunStatus.COMPLETED.value
            event = 'run_completed'

        if result in (RESULT_COMPLETED, RESULT_FAILED, RESULT_CANCELLED):
            run.completed_at = run.completed_at or _utcnow()
        run.outcome = run.outcome or _default_outcome(result)
        self.repo._bump(run)
        if event is not None:
            self.repo.append_event(run, event, {
                'decision': decision[:32],
            })
        self._clear_claim(run)
        self.repo._commit()
        return result


class PlannerUnavailable(Exception):
    """规划器未接线：fail-closed，Run 落 failed。"""


def _default_outcome(result):
    from app.ai.autonomy.state import RunOutcome

    if result == RESULT_COMPLETED:
        return RunOutcome.INCONCLUSIVE.value
    if result == RESULT_FAILED:
        return RunOutcome.NOT_RESOLVED.value
    return None


def _utcnow():
    import datetime

    return datetime.datetime.utcnow()


def _is_checkpoint_error(exc):
    """Recognize saver/Redis failures without swallowing graph/domain bugs."""
    try:
        from redis.exceptions import RedisError
    except ImportError:  # pragma: no cover - Redis is a locked dependency.
        RedisError = ()
    module = type(exc).__module__.lower()
    return (
        isinstance(exc, RedisError)
        or module.startswith('langgraph.checkpoint.')
    )


# ---------------------------------------------------------------------------
# 生产接线
# ---------------------------------------------------------------------------

def autonomy_checkpoint_url() -> str:
    """自治专用 checkpoint：专用 Redis 8 的 DB 0。"""
    from app.ai.autonomy.readiness import autonomy_redis_url

    return autonomy_redis_url(0)


def make_autonomy_saver_factory():
    """ShallowRedisSaver 工厂；专用 Redis 未接线时返回 None。

    WP0 门槛已在真实 Redis 8 上验证 shallow saver 的中断/恢复/
    重启存活；这里按同一用法（from_conn_string + setup）构建。
    """
    if not config.AI_AUTONOMY_ENABLED:
        return None
    if not config.AI_AUTONOMY_REDIS_HOST:
        return None
    url = autonomy_checkpoint_url()

    def factory():
        from langgraph.checkpoint.redis import ShallowRedisSaver

        manager = ShallowRedisSaver.from_conn_string(
            url,
            connection_args={
                'socket_connect_timeout': float(
                    config.REDIS_CONF['socket_connect_timeout']
                ),
                'socket_timeout': float(
                    config.REDIS_CONF['socket_timeout']
                ),
                'retry_on_timeout': False,
            },
        )
        saver = manager.__enter__()
        saver.setup()

        def close():
            manager.__exit__(None, None, None)

        return saver, close

    return factory


def make_autonomy_heartbeat_session_factory():
    """心跳线程的独立 session 工厂（不与驱动主 session 跨线程共享）。"""
    def factory():
        from sqlalchemy.orm import Session

        from app.core.db.database import db

        return Session(bind=db.engine)

    return factory
