# -*- coding: utf-8 -*-
"""M1/S1: 自治 Run/Step/Event/Artifact 持久层与原子审批决策。

设计要点：
- session 由调用方注入（生产用 db.session，测试用独立引擎），
  本模块不自建连接。
- 每次权威状态变化都递增 run.revision；决策输入只有
  {operation, expected_revision}，且 operation 必须来自服务端
  当前返回的 allowed_operations。
- 决策在同一事务内原子复核：owner、Run/Step 从属、当前状态、
  revision、digest、当前资产权限、凭据授权和资产环境；任何一项
  失败都返回冲突且不发生任何状态转换。
- Event payload 永不包含凭据；动作快照中凭据仅为 ID 引用。
"""
import datetime
import json
import re
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.ai.autonomy.actions import (
    ACTION_VERSION,
    ActionValidationError,
    StructuredAction,
    action_from_dict,
    build_action_digest,
    build_probe_command,
    list_probe_ids,
    redacted_summary,
    validate_probe,
    verify_action_digest,
)
from app.ai.autonomy.policy import (
    ApprovalDecision,
    Budget,
    classify_action,
    parse_budget,
    validate_mode_for_environment,
)
from app.ai.autonomy.state import (
    ACTIVE_RUN_STATUSES,
    AiEnvironment,
    AutonomyStateError,
    DecisionOperation,
    RunMode,
    RunStatus,
    StepKind,
    StepStatus,
    TERMINAL_RUN_STATUSES,
    assert_run_transition,
    assert_step_transition,
)


class AutonomyError(Exception):
    """自治模块基础错误。"""


class AutonomyNotFound(AutonomyError):
    """Run/Step 不存在或对当前 owner 不可见。"""


class AutonomyConflict(AutonomyError):
    """revision 过期、状态不符、digest 被篡改或重复决策。"""


class AutonomyPermissionError(AutonomyError):
    """资产/凭据授权校验失败。"""


class AutonomyValidationError(AutonomyError):
    """创建参数不合法。"""


_CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_SECRET_KEY_MARKERS = ('password', 'secret', 'token', 'credential', 'private_key')
_ACTIVE_HOST_UNIQUE_KEY = 'uq_ai_auto_run_active_host'
_SQLITE_ACTIVE_HOST_UNIQUE_ERROR = (
    'UNIQUE constraint failed: t_ai_autonomous_run.active_host_id'
)


def _is_active_host_unique_violation(exc: IntegrityError) -> bool:
    """只识别活动 Run 的唯一键冲突；其他完整性错误保持原样。"""
    message = str(getattr(exc, 'orig', None) or exc)
    return (
        _ACTIVE_HOST_UNIQUE_KEY in message
        or _SQLITE_ACTIVE_HOST_UNIQUE_ERROR in message
    )


def sanitize_text(value: str) -> str:
    """清洗控制字符（保留换行/制表），防 ANSI 注入。"""
    return _CONTROL_CHARS_RE.sub('', str(value or ''))


def _truncate_utf8(text: str, max_bytes: int):
    """按 UTF-8 字节安全截断，绝不返回半个多字节字符。"""
    encoded = text.encode('utf-8', errors='replace')
    normalized = encoded.decode('utf-8')
    limit = max(0, int(max_bytes))
    if len(encoded) <= limit:
        return normalized, len(encoded), False
    clipped = encoded[:limit].decode('utf-8', errors='ignore')
    size_bytes = len(clipped.encode('utf-8'))
    return clipped, size_bytes, True


def sanitize_payload(payload: Any) -> Any:
    """递归清洗 Event payload：丢弃任何疑似凭据键。"""
    if isinstance(payload, dict):
        cleaned = {}
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
                continue
            cleaned[str(key)] = sanitize_payload(value)
        return cleaned
    if isinstance(payload, list):
        return [sanitize_payload(item) for item in payload]
    if isinstance(payload, str):
        return sanitize_text(payload)
    return payload


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _approval_ttl_seconds(ttl_seconds=None) -> int:
    """draft/待审批有效期；默认读配置（ROADMAP 约定 24 小时）。"""
    if ttl_seconds is not None:
        return max(1, int(ttl_seconds))
    from app.core import config
    return config.AI_AUTONOMY_APPROVAL_TTL_SECONDS


def _expire_run_row(session, run, reason: str) -> None:
    """把活动 Run 落 expired 并追加事件（调用方负责提交）。"""
    assert_run_transition(run.status, RunStatus.EXPIRED.value)
    run.status = RunStatus.EXPIRED.value
    run.completed_at = run.completed_at or _utcnow()
    run.revision = int(run.revision or 0) + 1
    seq = int(run.latest_event_seq or 0) + 1
    from app.core.db.database import t_ai_autonomous_event
    session.add(t_ai_autonomous_event(
        run_id=run.id,
        sequence=seq,
        event_type='run_expired',
        payload_json=json.dumps(
            sanitize_payload({'reason': reason}), ensure_ascii=False,
        ),
    ))
    run.latest_event_seq = seq


def sweep_expired_runs(session, ttl_seconds=None, now=None) -> Dict[str, int]:
    """清扫超期的 draft 与待审批 Run（Worker 启动扫描兼周期兜底）。

    - draft 超期未启动 → expired；
    - 待审批 Step 超期未决策 → Step 落 cancelled，Run 落 expired；
    绝不自动越过审批，也不碰租约被持有的活动 Run。
    """
    from app.core.db.database import (
        t_ai_autonomous_run, t_ai_autonomous_step,
    )

    now = now or _utcnow()
    cutoff = now - datetime.timedelta(seconds=_approval_ttl_seconds(ttl_seconds))
    expired = 0

    drafts = session.query(t_ai_autonomous_run).filter(
        t_ai_autonomous_run.status == RunStatus.DRAFT.value,
        t_ai_autonomous_run.updated_at < cutoff,
    ).all()
    for run in drafts:
        _expire_run_row(session, run, 'draft_expired')
        expired += 1

    stale_steps = session.query(t_ai_autonomous_step).filter(
        t_ai_autonomous_step.status == StepStatus.WAITING_APPROVAL.value,
        t_ai_autonomous_step.updated_at < cutoff,
    ).all()
    stale_run_ids = sorted({step.run_id for step in stale_steps})
    for step in stale_steps:
        assert_step_transition(step.status, StepStatus.CANCELLED.value)
        step.status = StepStatus.CANCELLED.value
        step.note = 'approval expired'
    for run_id in stale_run_ids:
        run = session.query(t_ai_autonomous_run).filter_by(id=run_id).first()
        if run is not None and run.status == RunStatus.WAITING_APPROVAL.value:
            _expire_run_row(session, run, 'approval_expired')
            expired += 1
    if expired:
        session.commit()
    return {'expired_runs': expired, 'cancelled_steps': len(stale_steps)}


class AutonomyRepository:
    """owner 隔离的自治任务持久层。"""

    def __init__(self, session, secret_key: str, platform_factory=None):
        self.session = session
        self.secret_key = secret_key
        if platform_factory is None:
            from app.ai.tools import PlatformQueryService
            platform_factory = PlatformQueryService
        self.platform_factory = platform_factory

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _platform(self, owner: str, role: str):
        return self.platform_factory(owner, role)

    def _commit(self):
        self.session.commit()

    def _get_run_row(self, owner: str, run_id: str):
        from app.core.db.database import t_ai_autonomous_run

        row = self.session.query(t_ai_autonomous_run).filter_by(
            id=run_id, owner=owner,
        ).first()
        if row is None:
            raise AutonomyNotFound('autonomous run not found')
        return row

    def _get_host_row(self, host_id: int):
        from app.core.db.database import t_host

        row = self.session.query(t_host).filter_by(
            id=int(host_id), is_deleted=False,
        ).first()
        if row is None:
            raise AutonomyPermissionError('target host no longer exists')
        return row

    def _revalidate_boundaries(self, owner: str, role: str, run_row) -> None:
        """创建/启动/决策边界统一复核资产与凭据授权。"""
        platform = self._platform(owner, role)
        if not platform.validate_asset_ids([int(run_row.host_id)]):
            raise AutonomyPermissionError('asset authorization revoked')
        if platform.resolve_system_user(int(run_row.system_user_id)) is None:
            raise AutonomyPermissionError('credential authorization revoked')
        host = self._get_host_row(run_row.host_id)
        try:
            validate_mode_for_environment(run_row.mode, host.ai_environment)
        except Exception as exc:
            raise AutonomyPermissionError(str(exc)) from exc

    def append_event(
        self, run_row, event_type: str, payload: Optional[Dict[str, Any]],
    ) -> int:
        from app.core.db.database import t_ai_autonomous_event

        seq = int(run_row.latest_event_seq or 0) + 1
        event = t_ai_autonomous_event(
            run_id=run_row.id,
            sequence=seq,
            event_type=str(event_type)[:32],
            payload_json=json.dumps(
                sanitize_payload(payload or {}), ensure_ascii=False,
            ),
        )
        self.session.add(event)
        run_row.latest_event_seq = seq
        return seq

    def _bump(self, run_row) -> int:
        run_row.revision = int(run_row.revision or 0) + 1
        return run_row.revision

    @staticmethod
    def _is_stale(stamp) -> bool:
        """时间戳早于有效期切点即视为过期（draft/待审批）。"""
        if stamp is None:
            return False
        cutoff = _utcnow() - datetime.timedelta(
            seconds=_approval_ttl_seconds(),
        )
        return stamp < cutoff

    def _run_to_dict(self, row) -> Dict[str, Any]:
        return {
            'id': row.id,
            'owner': row.owner,
            'goal': row.goal,
            'host_id': int(row.host_id),
            'host_alias': row.host_alias,
            'system_user_id': int(row.system_user_id),
            'system_user_alias': row.system_user_alias,
            'mode': row.mode,
            'status': row.status,
            'outcome': row.outcome,
            'revision': int(row.revision or 0),
            'budget': json.loads(row.budget_json or '{}'),
            'latest_event_seq': int(row.latest_event_seq or 0),
            'cancel_requested': bool(row.cancel_requested),
            'started_at': row.started_at,
            'completed_at': row.completed_at,
            'created_at': getattr(row, 'created_at', None),
        }

    def _step_to_dict(self, row) -> Dict[str, Any]:
        return {
            'id': row.id,
            'run_id': row.run_id,
            'kind': row.kind,
            'status': row.status,
            'seq': int(row.seq),
            'summary': row.summary,
            'action_digest': row.action_digest or '',
            'note': row.note or '',
            'created_at': getattr(row, 'created_at', None),
        }

    # ------------------------------------------------------------------
    # Run 生命周期
    # ------------------------------------------------------------------

    def create_run(
        self,
        owner: str,
        role: str,
        *,
        goal: str,
        host_id: int,
        system_user_id: int,
        mode: str,
        budget_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from app.core.db.database import t_ai_autonomous_run

        goal = sanitize_text(goal).strip()
        if not goal or len(goal) > 512:
            raise AutonomyValidationError('goal must be 1..512 characters')
        if mode not in {m.value for m in RunMode}:
            raise AutonomyValidationError('unknown mode: %r' % (mode,))
        try:
            budget = parse_budget(budget_payload)
        except Exception as exc:
            raise AutonomyValidationError(str(exc)) from exc

        try:
            host_id = int(host_id)
            system_user_id = int(system_user_id)
        except (TypeError, ValueError):
            raise AutonomyValidationError('host_id/system_user_id must be integers') from None
        if host_id <= 0 or system_user_id <= 0:
            raise AutonomyValidationError('host_id/system_user_id must be positive')

        platform = self._platform(owner, role)
        if not platform.validate_asset_ids([host_id]):
            raise AutonomyPermissionError('asset authorization failed')
        credential = platform.resolve_system_user(system_user_id)
        if credential is None:
            raise AutonomyPermissionError('credential authorization failed')
        host = self._get_host_row(host_id)
        try:
            validate_mode_for_environment(mode, host.ai_environment)
        except Exception as exc:
            raise AutonomyValidationError(str(exc)) from exc

        active = self.session.query(t_ai_autonomous_run).filter(
            t_ai_autonomous_run.host_id == host_id,
            t_ai_autonomous_run.status.in_(
                [s.value for s in ACTIVE_RUN_STATUSES]
            ),
        ).first()
        if active is not None:
            raise AutonomyConflict(
                'an active autonomous run already exists for this host'
            )

        run = t_ai_autonomous_run(
            id=uuid.uuid4().hex,
            owner=owner,
            goal=goal[:512],
            host_id=host_id,
            host_alias=str(host.alias),
            system_user_id=system_user_id,
            system_user_alias=str(credential.get('alias') or ''),
            mode=mode,
            status=RunStatus.DRAFT.value,
            revision=0,
            budget_json=json.dumps(budget.to_dict(), sort_keys=True),
            latest_event_seq=0,
        )
        self.session.add(run)
        # run 与首个事件同事务落库：ORM 无 relationship 时 flush 不保证
        # 父子插入顺序，真实 MySQL 的外键会拒绝先插的事件行，必须先
        # flush 出 run 主键。
        try:
            self.session.flush()
        except IntegrityError as exc:
            # flush 失败后 Session 必须先 rollback 才能继续使用。只把
            # 精确的单活唯一键冲突转换为领域 409；FK/NOT NULL 等其他
            # 完整性错误继续上抛，不能被误报成“已有活动 Run”。
            self.session.rollback()
            if _is_active_host_unique_violation(exc):
                raise AutonomyConflict(
                    'an active autonomous run already exists for this host'
                ) from None
            raise
        self.append_event(run, 'run_created', {
            'mode': mode, 'host_id': host_id,
            'system_user_id': system_user_id,
        })
        self._commit()
        return self._run_to_dict(run)

    def start_run(self, owner: str, role: str, run_id: str) -> Dict[str, Any]:
        run = self._get_run_row(owner, run_id)
        self._revalidate_boundaries(owner, role, run)
        # 惰性过期：draft 超过有效期未启动即落 expired，绝不带着
        # 陈旧草稿开跑。
        if (
            run.status == RunStatus.DRAFT.value
            and self._is_stale(run.updated_at)
        ):
            _expire_run_row(self.session, run, 'draft_expired')
            self._commit()
            raise AutonomyConflict('draft expired; start denied')
        assert_run_transition(run.status, RunStatus.QUEUED.value)
        run.status = RunStatus.QUEUED.value
        run.started_at = run.started_at or _utcnow()
        self._bump(run)
        self.append_event(run, 'run_started', {'revision': run.revision})
        self._commit()
        return self._run_to_dict(run)

    def request_cancel(
        self, owner: str, role: str, run_id: str,
    ) -> Dict[str, Any]:
        """Atomically cancel idle states; request stop for in-flight work.

        Cancellation is an owner/admin control-plane operation.  It must stay
        available after target or credential access is revoked.  The locked
        Run row serializes queued cancellation against Worker lease claims.
        """
        from app.core.db.database import (
            t_ai_autonomous_run, t_ai_autonomous_step,
        )

        if role != 'admin':
            raise AutonomyPermissionError('admin role required')
        run = self.session.query(t_ai_autonomous_run).filter_by(
            id=run_id, owner=owner,
        ).with_for_update().first()
        if run is None:
            raise AutonomyNotFound('autonomous run not found')
        if run.status in {s.value for s in TERMINAL_RUN_STATUSES}:
            return self._run_to_dict(run)

        cancel_without_remote_stop = run.status in {
            RunStatus.DRAFT.value,
            RunStatus.QUEUED.value,
            RunStatus.WAITING_APPROVAL.value,
        }
        newly_requested = not bool(run.cancel_requested)
        run.cancel_requested = True

        if cancel_without_remote_stop:
            pending_steps = self.session.query(
                t_ai_autonomous_step,
            ).filter(
                t_ai_autonomous_step.run_id == run.id,
                t_ai_autonomous_step.status.in_([
                    StepStatus.PROPOSED.value,
                    StepStatus.WAITING_APPROVAL.value,
                    StepStatus.APPROVED.value,
                ]),
            ).with_for_update().all()
            for step in pending_steps:
                assert_step_transition(
                    step.status, StepStatus.CANCELLED.value,
                )
                step.status = StepStatus.CANCELLED.value
                step.note = 'cancelled before execution'

            assert_run_transition(run.status, RunStatus.CANCELLED.value)
            run.status = RunStatus.CANCELLED.value
            run.completed_at = run.completed_at or _utcnow()
            run.lease_owner = None
            run.lease_expires_at = None

        if not newly_requested and not cancel_without_remote_stop:
            return self._run_to_dict(run)

        self._bump(run)
        if newly_requested:
            self.append_event(run, 'run_cancel_requested', {
                'revision': int(run.revision),
            })
        if cancel_without_remote_stop:
            self.append_event(run, 'run_cancelled', {
                'revision': int(run.revision),
                'reason': 'cancelled_before_execution',
            })
        self._commit()
        return self._run_to_dict(run)

    def get_run(self, owner: str, run_id: str) -> Dict[str, Any]:
        return self._run_to_dict(self._get_run_row(owner, run_id))

    def list_runs(self, owner: str, limit: int = 50) -> List[Dict[str, Any]]:
        from app.core.db.database import t_ai_autonomous_run

        rows = self.session.query(t_ai_autonomous_run).filter_by(
            owner=owner,
        ).order_by(t_ai_autonomous_run.created_at.desc()).limit(
            max(1, min(int(limit), 200))
        ).all()
        return [self._run_to_dict(row) for row in rows]

    def list_steps(self, owner: str, run_id: str) -> List[Dict[str, Any]]:
        from app.core.db.database import t_ai_autonomous_step

        self._get_run_row(owner, run_id)
        rows = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id,
        ).order_by(t_ai_autonomous_step.seq.asc()).all()
        return [self._step_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 权威快照与 allowed_operations
    # ------------------------------------------------------------------

    def allowed_operations(self, owner: str, run_id: str) -> List[str]:
        """服务端权威操作集合：决策只能来自这里。"""
        from app.core.db.database import t_ai_autonomous_step

        run = self._get_run_row(owner, run_id)
        if run.status != RunStatus.WAITING_APPROVAL.value:
            return []
        waiting = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id, status=StepStatus.WAITING_APPROVAL.value,
        ).first()
        if waiting is None:
            return []
        return [DecisionOperation.APPROVE.value, DecisionOperation.REJECT.value]

    def snapshot(self, owner: str, run_id: str) -> Dict[str, Any]:
        run = self.get_run(owner, run_id)
        run['steps'] = self.list_steps(owner, run_id)
        run['allowed_operations'] = self.allowed_operations(owner, run_id)
        return run

    # ------------------------------------------------------------------
    # Step 提议（服务端自有探针）
    # ------------------------------------------------------------------

    def propose_probe(
        self,
        owner: str,
        role: str,
        run_id: str,
        probe_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from app.core.db.database import t_ai_autonomous_step

        run = self._get_run_row(owner, run_id)
        if run.status not in {
            RunStatus.QUEUED.value, RunStatus.RUNNING.value,
            RunStatus.WAITING_APPROVAL.value,
        }:
            raise AutonomyConflict(
                'steps can only be proposed while the run is active'
            )
        if str(probe_id or '') not in list_probe_ids():
            raise AutonomyValidationError('unknown probe: %r' % (probe_id,))
        try:
            normalized = validate_probe(probe_id, params or {})
        except ActionValidationError as exc:
            raise AutonomyValidationError(str(exc)) from exc

        self._revalidate_boundaries(owner, role, run)
        budget = Budget(**json.loads(run.budget_json or '{}'))
        action_count = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id, kind=StepKind.ACTION.value,
        ).count()
        if action_count >= budget.max_actions:
            raise AutonomyConflict('action budget exhausted')

        # 预构造命令：命令本身不落快照（S1 无执行），仅用于构造期校验。
        build_probe_command(probe_id, normalized)

        step_id = uuid.uuid4().hex
        parameters = dict(normalized, probe_id=str(probe_id))
        action = StructuredAction(
            kind='probe',
            target_id=int(run.host_id),
            system_user_id=int(run.system_user_id),
            parameters=parameters,
            timeout_seconds=min(budget.command_timeout_seconds, 600),
            step_id=step_id,
        )
        host = self._get_host_row(run.host_id)
        decision, reason = classify_action(run.mode, action, host.ai_environment)

        seq = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id,
        ).count() + 1
        summary = redacted_summary(action)

        if decision == ApprovalDecision.DENIED:
            initial_status = StepStatus.FAILED.value
        elif decision == ApprovalDecision.APPROVAL_REQUIRED:
            initial_status = StepStatus.WAITING_APPROVAL.value
        else:
            # 自动通过的探针留给执行器（S2）；S1 不执行任何远程命令。
            initial_status = StepStatus.PROPOSED.value

        step = t_ai_autonomous_step(
            id=step_id,
            run_id=run_id,
            kind=StepKind.ACTION.value,
            status=initial_status,
            seq=seq,
            summary=summary,
            action_json=json.dumps(
                action.to_canonical_dict(), sort_keys=True, ensure_ascii=True,
            ),
            action_digest=build_action_digest(action, self.secret_key),
            note='' if decision != ApprovalDecision.DENIED else reason[:255],
        )
        self.session.add(step)
        self._bump(run)
        self.append_event(run, 'step_proposed', {
            'step_id': step_id, 'seq': seq, 'probe_id': str(probe_id),
            'decision': decision.value, 'reason': reason,
        })
        if decision == ApprovalDecision.APPROVAL_REQUIRED:
            if run.status != RunStatus.WAITING_APPROVAL.value:
                assert_run_transition(run.status, RunStatus.WAITING_APPROVAL.value)
                run.status = RunStatus.WAITING_APPROVAL.value
                self._bump(run)
        self._commit()
        return self._step_to_dict(step)

    # ------------------------------------------------------------------
    # 原子审批决策
    # ------------------------------------------------------------------

    def decide(
        self,
        owner: str,
        role: str,
        run_id: str,
        step_id: str,
        operation: str,
        expected_revision: Any,
    ) -> Dict[str, Any]:
        from app.core.db.database import t_ai_autonomous_step

        run = self._get_run_row(owner, run_id)
        step = self.session.query(t_ai_autonomous_step).filter_by(
            id=step_id, run_id=run_id,
        ).first()
        if step is None:
            # 跨 Run 的 step_id 与不存在的 step 一律冲突，不泄露存在性。
            raise AutonomyConflict('step does not belong to this run')

        # 惰性过期：待审批超过有效期即落 cancelled，Run 落 expired，
        # 绝不接受陈旧审批。
        if (
            step.status == StepStatus.WAITING_APPROVAL.value
            and self._is_stale(step.updated_at)
        ):
            assert_step_transition(step.status, StepStatus.CANCELLED.value)
            step.status = StepStatus.CANCELLED.value
            step.note = 'approval expired'
            if run.status == RunStatus.WAITING_APPROVAL.value:
                _expire_run_row(self.session, run, 'approval_expired')
            self._commit()
            raise AutonomyConflict('approval window expired')

        allowed = self.allowed_operations(owner, run_id)
        if str(operation or '') not in allowed:
            raise AutonomyConflict(
                'operation %r is not currently allowed' % (operation,)
            )
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError):
            raise AutonomyConflict('expected_revision is missing or invalid') from None
        if expected != int(run.revision or 0):
            raise AutonomyConflict('stale revision')
        if step.status != StepStatus.WAITING_APPROVAL.value:
            raise AutonomyConflict('step is not awaiting approval')

        # digest 复核：快照被篡改则审批无效。
        try:
            action = action_from_dict(json.loads(step.action_json or '{}'))
        except (ActionValidationError, ValueError):
            raise AutonomyConflict('action snapshot is corrupted') from None
        if not verify_action_digest(action, step.action_digest, self.secret_key):
            raise AutonomyConflict('action digest mismatch')

        # 决策边界重新校验当前权限、凭据授权与资产环境。
        self._revalidate_boundaries(owner, role, run)

        op = DecisionOperation(str(operation))
        if op == DecisionOperation.APPROVE:
            assert_step_transition(step.status, StepStatus.APPROVED.value)
            step.status = StepStatus.APPROVED.value
            step.note = 'approved'
        else:
            assert_step_transition(step.status, StepStatus.FAILED.value)
            step.status = StepStatus.FAILED.value
            step.note = 'rejected'

        # 解锁后回到 queued 等待执行器认领（S2）。
        assert_run_transition(run.status, RunStatus.QUEUED.value)
        run.status = RunStatus.QUEUED.value
        self._bump(run)
        self.append_event(run, 'step_decision', {
            'step_id': step_id, 'operation': op.value,
            'revision': run.revision,
        })
        self._commit()
        return self._step_to_dict(step)

    # ------------------------------------------------------------------
    # 资产环境（仅管理员入口在路由层强制）
    # ------------------------------------------------------------------

    def set_host_environment(self, host_id: int, environment: str) -> Dict[str, Any]:
        if environment not in {e.value for e in AiEnvironment}:
            raise AutonomyValidationError('unknown ai_environment value')
        host = self._get_host_row(int(host_id))
        previous = host.ai_environment
        host.ai_environment = environment
        self._commit()
        return {
            'host_id': int(host.id),
            'alias': str(host.alias),
            'previous': str(previous),
            'ai_environment': str(environment),
        }

    # ------------------------------------------------------------------
    # Artifact（S1 仅提供落库能力，无远程执行产物）
    # ------------------------------------------------------------------

    def create_artifact(
        self,
        owner: str,
        run_id: str,
        *,
        kind: str,
        title: str,
        content: str,
        step_id: Optional[str] = None,
        retention_days: int = 7,
        force_truncated: bool = False,
        commit: bool = True,
    ) -> Dict[str, Any]:
        from app.core.db.database import (
            t_ai_autonomous_artifact, t_ai_autonomous_run,
        )
        from app.tools.basesec import encrypt_secret

        run = self._get_run_row(owner, run_id)
        # 生产 MySQL 通过 Run 行锁串行化同一 Run 的 Artifact 预算核算，
        # 避免两个并发步骤都读到相同 remaining 后合计越过硬上限。
        self.session.query(t_ai_autonomous_run.id).filter_by(
            id=run.id,
        ).with_for_update().one()
        budget = Budget(**json.loads(run.budget_json or '{}'))
        text = sanitize_text(content)
        used_bytes = self.session.query(
            func.coalesce(func.sum(t_ai_autonomous_artifact.size_bytes), 0),
        ).filter_by(run_id=run_id).scalar()
        remaining_bytes = max(
            0, int(budget.run_artifact_bytes) - int(used_bytes or 0),
        )
        content_limit = min(int(budget.step_output_bytes), remaining_bytes)
        text, size_bytes, truncated = _truncate_utf8(text, content_limit)
        truncated = bool(truncated or force_truncated)
        # encrypt_secret intentionally rejects empty plaintext.  A zero-byte
        # Artifact is still useful as durable evidence that output was
        # omitted by the hard Run budget, so encrypt an explicit marker while
        # keeping size_bytes authoritative for budget accounting.
        storage_text = text
        if not storage_text:
            storage_text = (
                '[CONTENT OMITTED: ARTIFACT BUDGET EXHAUSTED]'
                if truncated else '[EMPTY ARTIFACT]'
            )
        artifact = t_ai_autonomous_artifact(
            id=uuid.uuid4().hex,
            run_id=run_id,
            step_id=step_id,
            kind=str(kind)[:32],
            title=sanitize_text(title)[:128],
            content_ciphertext=encrypt_secret(storage_text),
            size_bytes=size_bytes,
            truncated=truncated,
            expires_at=_utcnow() + datetime.timedelta(days=retention_days),
        )
        self.session.add(artifact)
        self.append_event(run, 'artifact_created', {
            'artifact_id': artifact.id, 'kind': artifact.kind,
            'size_bytes': size_bytes, 'truncated': truncated,
        })
        if commit:
            self._commit()
        return {
            'id': artifact.id,
            'run_id': run_id,
            'kind': artifact.kind,
            'size_bytes': size_bytes,
            'truncated': truncated,
        }
