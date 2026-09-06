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
import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError

from app.ai.autonomy.actions import (
    ActionValidationError,
    StructuredAction,
    WRITE_KINDS,
    action_from_dict,
    build_action_digest,
    build_probe_command,
    list_probe_ids,
    redacted_summary,
    validate_probe,
    verify_action_digest,
)
from app.ai.autonomy.policy import (
    Budget,
    PolicyDecision,
    classify_action,
    parse_budget,
    validate_mode_for_environment,
)
from app.ai.autonomy.graph import DEFAULT_GRAPH_VERSION
from app.ai.autonomy.plans import (
    PLAN_MAX_ACTIONS,
    PLAN_SUMMARY_CHARS,
    PlanAuthorizationError,
    build_plan_digest,
    build_plan_snapshot,
    canonical_plan_json,
    parse_plan_snapshot,
    validate_plan_action,
    verify_plan_authorization,
)
from app.ai.autonomy.state import (
    ACTIVE_RUN_STATUSES,
    CANONICAL_RUN_MODES,
    AiEnvironment,
    DecisionOperation,
    RunMode,
    RunOutcome,
    RunStatus,
    StepKind,
    StepStatus,
    TERMINAL_RUN_STATUSES,
    TERMINAL_STEP_STATUSES,
    assert_run_transition,
    assert_step_transition,
)
from app.ai.diagnostic_adapters import sanitize_evidence


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
_SECRET_KEY_MARKERS = (
    'password', 'secret', 'token', 'credential', 'private_key',
)
# M1/S3 切片 3：custom 档案可选的动作类别是服务端固定集合（与
# classify_action 支持的 kind 一致）；不引入策略表达式语言，目标
# 范围就是 Run 创建时绑定的单一主机。probe 是服务端自有只读探针，
# 不在可选集合里，任何档案下都不受 custom 限制。
CUSTOM_ACTION_CATEGORIES = frozenset({
    'file_read', 'file_patch', 'file_restore', 'package_install',
    'shell', 'systemd',
})

# M1/S3 切片 4：Evidence 是不可信观察的有界索引。类别固定；摘要
# 限长 500；结论至多引用 16 条同一 Run 的 Evidence。
EVIDENCE_KINDS = frozenset({
    'action_observation', 'verification_observation',
    'alert_observation', 'prometheus_observation',
})
EVIDENCE_SUMMARY_CHARS = 500
MAX_EVIDENCE_CITATIONS = 16
CONCLUSION_ITEM_LIMIT = 8
CONCLUSION_ITEM_CHARS = 240
CONCLUSION_TEXT_CHARS = 512
CONCLUSION_CONFIDENCE = frozenset({'low', 'medium', 'high'})
RUN_TRIGGER_TYPES = frozenset({'manual', 'chat', 'alertmanager'})
AUTONOMY_ROLES = frozenset({'admin', 'user'})
RUN_FAILURE_EVENT_TYPES = frozenset({
    'authorization_revoked',
    'budget_exhausted',
    'planner_failed',
    'planner_unavailable',
    'unknown_graph_version',
})


def resolve_current_autonomy_role(session, owner: str) -> Optional[str]:
    """Resolve the owner's current usable role without persisting it on Run."""
    from app.core.db.database import t_acc_user

    row = session.query(t_acc_user).filter_by(
        name=str(owner),
        is_deleted=False,
    ).first()
    role = str(getattr(row, 'usrole', '') or '').lower() if row else ''
    return role if role in AUTONOMY_ROLES else None


def parse_custom_profile(payload):
    """解析 custom 权限档案；只接受 {action_categories: [...]}。

    类别必须来自服务端固定集合：非空、去重、未知拒绝。非法输入
    直接拒绝而不是静默钳制。
    """
    if not isinstance(payload, dict):
        raise AutonomyValidationError('custom profile must be an object')
    unknown = set(payload) - {'action_categories'}
    if unknown:
        raise AutonomyValidationError(
            'unknown custom profile fields: %s' % ', '.join(sorted(unknown))
        )
    raw = payload.get('action_categories')
    if not isinstance(raw, list) or not raw:
        raise AutonomyValidationError(
            'custom profile requires a non-empty action_categories list'
        )
    categories = []
    for item in raw:
        name = str(item or '')
        if name not in CUSTOM_ACTION_CATEGORIES:
            raise AutonomyValidationError(
                'unknown action category: %r' % (name,)
            )
        if name not in categories:
            categories.append(name)
    return {'action_categories': categories}


_ACTIVE_HOST_UNIQUE_KEY = 'uq_ai_auto_run_active_host'
_TRIGGER_UNIQUE_KEY = 'uq_ai_auto_run_trigger'
_SQLITE_ACTIVE_HOST_UNIQUE_ERROR = (
    'UNIQUE constraint failed: t_ai_autonomous_run.active_host_id'
)
_SQLITE_TRIGGER_UNIQUE_ERROR = (
    'UNIQUE constraint failed: '
    't_ai_autonomous_run.trigger_type, t_ai_autonomous_run.trigger_ref'
)


def _is_active_host_unique_violation(exc: IntegrityError) -> bool:
    """只识别活动 Run 的唯一键冲突；其他完整性错误保持原样。"""
    message = str(getattr(exc, 'orig', None) or exc)
    return (
        _ACTIVE_HOST_UNIQUE_KEY in message
        or _SQLITE_ACTIVE_HOST_UNIQUE_ERROR in message
    )


def _is_trigger_unique_violation(exc: IntegrityError) -> bool:
    message = str(getattr(exc, 'orig', None) or exc)
    return (
        _TRIGGER_UNIQUE_KEY in message
        or _SQLITE_TRIGGER_UNIQUE_ERROR in message
    )


def sanitize_text(value: str) -> str:
    """清洗控制字符（保留换行/制表），防 ANSI 注入。"""
    return _CONTROL_CHARS_RE.sub('', str(value or ''))


def _conclusion_text(value: Any, field: str) -> str:
    text = sanitize_evidence(value).strip()
    if not text:
        raise AutonomyValidationError('%s is required' % field)
    return text[:CONCLUSION_TEXT_CHARS]


def _conclusion_items(value: Any, field: str) -> List[str]:
    if not isinstance(value, list) or len(value) > CONCLUSION_ITEM_LIMIT:
        raise AutonomyValidationError('%s must be a bounded list' % field)
    return [
        _conclusion_text(item, field)[:CONCLUSION_ITEM_CHARS]
        for item in value
    ]


def normalize_conclusion_details(value: Any) -> Dict[str, Any]:
    """Validate the model-authored, operator-facing conclusion fields."""
    if not isinstance(value, dict):
        raise AutonomyValidationError('conclusion details must be an object')
    required = {
        'confirmed_facts', 'impact_scope', 'root_cause_hypothesis',
        'confidence', 'unknowns', 'recommended_actions',
    }
    if set(value) != required:
        raise AutonomyValidationError('conclusion details fields mismatch')
    confidence = str(value.get('confidence') or '')
    if confidence not in CONCLUSION_CONFIDENCE:
        raise AutonomyValidationError('unknown conclusion confidence')
    return {
        'confirmed_facts': _conclusion_items(
            value.get('confirmed_facts'), 'confirmed_facts',
        ),
        'impact_scope': _conclusion_text(
            value.get('impact_scope'), 'impact_scope',
        ),
        'root_cause_hypothesis': _conclusion_text(
            value.get('root_cause_hypothesis'), 'root_cause_hypothesis',
        ),
        'confidence': confidence,
        'unknowns': _conclusion_items(value.get('unknowns'), 'unknowns'),
        'recommended_actions': _conclusion_items(
            value.get('recommended_actions'), 'recommended_actions',
        ),
    }


def fallback_conclusion_details() -> Dict[str, Any]:
    """Return the bounded fail-closed conclusion used without model details."""
    return {
        'confirmed_facts': ['结论由服务端根据当前终态收口'],
        'impact_scope': '未确认影响范围',
        'root_cause_hypothesis': '未形成根因假设',
        'confidence': 'low',
        'unknowns': ['未生成有效的结构化结论详情'],
        'recommended_actions': ['查看 Evidence 与任务事件后人工复核'],
    }


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
    run.lease_owner = None
    run.lease_token = None
    run.lease_expires_at = None
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
    cutoff = now - datetime.timedelta(
        seconds=_approval_ttl_seconds(ttl_seconds),
    )
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

            def platform_factory(owner, role):
                return PlatformQueryService(owner, role, session=session)
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
        if not platform.validate_asset_sys_user_id_pair(
            [int(run_row.host_id)], int(run_row.system_user_id),
        ):
            raise AutonomyPermissionError(
                'asset and credential authorization revoked'
            )
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
            'trigger_type': getattr(row, 'trigger_type', 'manual'),
            'trigger_ref': getattr(row, 'trigger_ref', None),
            'trigger_summary': getattr(row, 'trigger_summary', '') or '',
            'custom_profile': (
                json.loads(row.custom_profile_json)
                if getattr(row, 'custom_profile_json', None) else None
            ),
            'status': row.status,
            'outcome': row.outcome,
            'conclusion': (
                json.loads(row.conclusion_json)
                if getattr(row, 'conclusion_json', None) else None
            ),
            'revision': int(row.revision or 0),
            'graph_version': row.graph_version,
            'budget': json.loads(row.budget_json or '{}'),
            'latest_event_seq': int(row.latest_event_seq or 0),
            'cancel_requested': bool(row.cancel_requested),
            'started_at': row.started_at,
            'completed_at': row.completed_at,
            'created_at': getattr(row, 'created_at', None),
        }

    def _step_to_dict(self, row) -> Dict[str, Any]:
        result = {
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
        if row.kind == StepKind.PLAN.value:
            try:
                snapshot = parse_plan_snapshot(row.action_json or '')
                result['plan_actions'] = [
                    redacted_summary(action_from_dict(item), max_chars=None)
                    for item in snapshot['actions']
                ]
            except (ActionValidationError, PlanAuthorizationError):
                result['plan_actions'] = []
        return result

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
        profile_payload: Optional[Dict[str, Any]] = None,
        trigger_type: str = 'manual',
        trigger_ref: Optional[str] = None,
        trigger_summary: str = '',
    ) -> Dict[str, Any]:
        from app.core.db.database import t_ai_autonomous_run

        goal = sanitize_text(goal).strip()
        if not goal or len(goal) > 512:
            raise AutonomyValidationError('goal must be 1..512 characters')
        if mode not in {m.value for m in CANONICAL_RUN_MODES}:
            raise AutonomyValidationError('unknown mode: %r' % (mode,))
        trigger_type = sanitize_text(trigger_type).strip().lower()
        if trigger_type not in RUN_TRIGGER_TYPES:
            raise AutonomyValidationError(
                'unknown trigger type: %r' % (trigger_type,)
            )
        trigger_ref = sanitize_text(trigger_ref or '').strip() or None
        if trigger_ref is not None and len(trigger_ref) > 64:
            raise AutonomyValidationError('trigger_ref is too long')
        if trigger_type == 'alertmanager' and not trigger_ref:
            raise AutonomyValidationError(
                'alertmanager trigger requires trigger_ref'
            )
        trigger_summary = sanitize_text(trigger_summary).strip()[:512]
        if mode == RunMode.CUSTOM.value:
            if profile_payload is None:
                raise AutonomyValidationError(
                    'custom mode requires an action_categories profile'
                )
            custom_profile = parse_custom_profile(profile_payload)
        else:
            if profile_payload is not None:
                raise AutonomyValidationError(
                    'custom profile is only valid with mode=custom'
                )
            custom_profile = None
        try:
            budget = parse_budget(budget_payload)
        except Exception as exc:
            raise AutonomyValidationError(str(exc)) from exc

        try:
            host_id = int(host_id)
            system_user_id = int(system_user_id)
        except (TypeError, ValueError):
            raise AutonomyValidationError(
                'host_id/system_user_id must be integers'
            ) from None
        if host_id <= 0 or system_user_id <= 0:
            raise AutonomyValidationError(
                'host_id/system_user_id must be positive'
            )

        platform = self._platform(owner, role)
        credential = platform.resolve_system_user(system_user_id)
        if credential is None:
            raise AutonomyPermissionError('credential authorization failed')
        if not platform.validate_asset_sys_user_id_pair(
            [host_id], system_user_id,
        ):
            raise AutonomyPermissionError(
                'asset and credential authorization failed'
            )
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
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            trigger_summary=trigger_summary,
            custom_profile_json=(
                json.dumps(custom_profile, sort_keys=True)
                if custom_profile else None
            ),
            graph_version=DEFAULT_GRAPH_VERSION,
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
            if _is_trigger_unique_violation(exc):
                raise AutonomyConflict(
                    'an autonomous run already exists for this trigger'
                ) from None
            raise
        self.append_event(run, 'run_created', {
            'mode': mode, 'host_id': host_id,
            'system_user_id': system_user_id,
            'custom_profile': custom_profile,
            'trigger_type': trigger_type,
            'trigger_ref': trigger_ref,
            'trigger_summary': trigger_summary,
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

        if str(role or '') not in AUTONOMY_ROLES:
            raise AutonomyPermissionError('unsupported autonomy role')
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
            RunStatus.NEEDS_ATTENTION.value,
        }
        # 租约已过期 = Worker 已死/失联，取消可直接落终态，不再等确认。
        lease_expired = (
            run.lease_expires_at is not None
            and run.lease_expires_at < _utcnow()
        )
        force_cancel = cancel_without_remote_stop or (
            run.status in {
                RunStatus.RUNNING.value,
                RunStatus.RECOVERING.value,
            } and lease_expired
        )
        newly_requested = not bool(run.cancel_requested)
        run.cancel_requested = True

        if force_cancel:
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
            run.lease_token = None
            run.lease_expires_at = None

        if not newly_requested and not force_cancel:
            return self._run_to_dict(run)

        self._bump(run)
        if newly_requested:
            self.append_event(run, 'run_cancel_requested', {
                'revision': int(run.revision),
            })
        if force_cancel:
            reason = (
                'cancelled_before_execution'
                if cancel_without_remote_stop
                else 'lease_expired_force_cancel'
            )
            self.append_event(run, 'run_cancelled', {
                'revision': int(run.revision),
                'reason': reason,
            })
        self._commit()
        return self._run_to_dict(run)

    def find_run_by_trigger(
        self, owner: str, trigger_type: str, trigger_ref: str,
    ) -> Optional[Dict[str, Any]]:
        """Find an idempotent external trigger without exposing other owners."""
        from app.core.db.database import t_ai_autonomous_run

        row = self.session.query(t_ai_autonomous_run).filter_by(
            owner=owner,
            trigger_type=str(trigger_type),
            trigger_ref=str(trigger_ref),
        ).first()
        return self._run_to_dict(row) if row is not None else None

    def get_run(self, owner: str, run_id: str) -> Dict[str, Any]:
        return self._run_to_dict(self._get_run_row(owner, run_id))

    def list_runs(self, owner: str, limit: int = 50) -> List[Dict[str, Any]]:
        from app.core.db.database import (
            t_ai_autonomous_event, t_ai_autonomous_run,
        )

        active_values = [status.value for status in ACTIVE_RUN_STATUSES]
        rows = self.session.query(t_ai_autonomous_run).filter_by(
            owner=owner,
        ).order_by(
            case(
                (t_ai_autonomous_run.status.in_(active_values), 0),
                else_=1,
            ).asc(),
            t_ai_autonomous_run.created_at.desc(),
            t_ai_autonomous_run.id.desc(),
        ).limit(
            max(1, min(int(limit), 200))
        ).all()
        runs = [self._run_to_dict(row) for row in rows]
        alert_ids = [
            row.id for row in rows if row.trigger_type == 'alertmanager'
        ]
        if not alert_ids:
            return runs

        latest_sequence = self.session.query(
            t_ai_autonomous_event.run_id.label('run_id'),
            func.max(t_ai_autonomous_event.sequence).label('sequence'),
        ).filter(
            t_ai_autonomous_event.run_id.in_(alert_ids),
            t_ai_autonomous_event.event_type.in_([
                'alert_firing', 'alert_resolved',
            ]),
        ).group_by(t_ai_autonomous_event.run_id).subquery()
        events = self.session.query(t_ai_autonomous_event).join(
            latest_sequence,
            (t_ai_autonomous_event.run_id == latest_sequence.c.run_id)
            & (t_ai_autonomous_event.sequence == latest_sequence.c.sequence),
        ).all()
        latest = {event.run_id: event for event in events}
        for run in runs:
            if run['trigger_type'] != 'alertmanager':
                continue
            event = latest.get(run['id'])
            run['alert_state'] = (
                event.event_type.removeprefix('alert_') if event else None
            )
            run['alert_updated_at'] = event.created_at if event else None
        return runs

    def ops_summary(self, owner: str, limit: int = 8) -> Dict[str, Any]:
        """Bounded data for the AIOps landing page; Run stays authoritative."""
        from app.core.db.database import t_ai_autonomous_run

        limit = max(1, min(int(limit), 20))
        active_values = [status.value for status in ACTIVE_RUN_STATUSES]
        active_query = self.session.query(t_ai_autonomous_run).filter(
            t_ai_autonomous_run.owner == owner,
            t_ai_autonomous_run.status.in_(active_values),
        )
        queued = active_query.filter(
            t_ai_autonomous_run.status == RunStatus.QUEUED.value,
        ).count()
        running = active_query.order_by(
            t_ai_autonomous_run.updated_at.desc(),
        ).limit(limit).all()
        alerts = active_query.filter(
            t_ai_autonomous_run.trigger_type == 'alertmanager',
        ).order_by(t_ai_autonomous_run.updated_at.desc()).limit(limit).all()
        conclusions = self.session.query(t_ai_autonomous_run).filter(
            t_ai_autonomous_run.owner == owner,
            t_ai_autonomous_run.outcome.isnot(None),
        ).order_by(t_ai_autonomous_run.completed_at.desc()).limit(limit).all()
        return {
            'active_runs': active_query.count(),
            'queued_runs': int(queued),
            'pending_alerts': [self._run_to_dict(row) for row in alerts],
            'running_runs': [self._run_to_dict(row) for row in running],
            'recent_conclusions': [
                self._run_to_dict(row) for row in conclusions
            ],
        }

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
        return [
            DecisionOperation.APPROVE.value,
            DecisionOperation.REJECT.value,
        ]

    def snapshot(self, owner: str, run_id: str) -> Dict[str, Any]:
        run = self.get_run(owner, run_id)
        run['steps'] = self.list_steps(owner, run_id)
        run['allowed_operations'] = self.allowed_operations(owner, run_id)
        if (
            run['status'] == RunStatus.FAILED.value
            and not any(step['status'] in {
                StepStatus.FAILED.value, StepStatus.OUTCOME_UNKNOWN.value,
            } for step in run['steps'])
        ):
            from app.core.db.database import t_ai_autonomous_event

            event = self.session.query(t_ai_autonomous_event).filter(
                t_ai_autonomous_event.run_id == run_id,
                t_ai_autonomous_event.event_type.in_(RUN_FAILURE_EVENT_TYPES),
            ).order_by(t_ai_autonomous_event.sequence.desc()).first()
            if event is not None:
                note = sanitize_evidence(
                    self._event_to_dict(event)['payload'].get('note') or ''
                ).strip()
                if note:
                    run['failure_reason'] = note[:128]
        return run

    # ------------------------------------------------------------------
    # 权威读取器：Event / Artifact（S3 切片 5）
    #
    # MySQL 快照是唯一权威来源；这些读取器只做 owner 隔离与脱敏
    # 边界复核，绝不改写任何状态（SSE 轮询依赖其纯读语义）。
    # ------------------------------------------------------------------

    MAX_EVENT_BATCH = 500

    @staticmethod
    def _event_to_dict(row) -> Dict[str, Any]:
        try:
            payload = json.loads(row.payload_json or '{}')
        except ValueError:
            payload = {}
        return {
            'sequence': int(row.sequence),
            'event_type': row.event_type,
            'payload': payload,
            'created_at': row.created_at,
        }

    def list_events(
        self,
        owner: str,
        run_id: str,
        after_seq: int = 0,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """按单调递增 sequence 回放 after_seq 之后的事件（不含 after_seq）。"""
        from app.core.db.database import t_ai_autonomous_event

        self._get_run_row(owner, run_id)
        batch = max(1, min(int(limit or self.MAX_EVENT_BATCH), self.MAX_EVENT_BATCH))
        rows = self.session.query(t_ai_autonomous_event).filter(
            t_ai_autonomous_event.run_id == run_id,
            t_ai_autonomous_event.sequence > int(after_seq),
        ).order_by(
            t_ai_autonomous_event.sequence.asc(),
        ).limit(batch).all()
        return [self._event_to_dict(row) for row in rows]

    @staticmethod
    def _artifact_meta(row) -> Dict[str, Any]:
        return {
            'id': row.id,
            'run_id': row.run_id,
            'step_id': row.step_id,
            'kind': row.kind,
            'title': row.title,
            'size_bytes': int(row.size_bytes or 0),
            'truncated': bool(row.truncated),
            'expired': bool(
                row.expires_at is not None and row.expires_at < _utcnow()
            ),
            'created_at': row.created_at,
        }

    def list_artifacts(self, owner: str, run_id: str) -> List[Dict[str, Any]]:
        """只返回 Artifact 元数据；正文必须走 get_artifact 单条读取。"""
        from app.core.db.database import t_ai_autonomous_artifact

        self._get_run_row(owner, run_id)
        rows = self.session.query(t_ai_autonomous_artifact).filter_by(
            run_id=run_id,
        ).order_by(t_ai_autonomous_artifact.created_at.asc()).all()
        return [self._artifact_meta(row) for row in rows]

    def get_artifact(
        self, owner: str, run_id: str, artifact_id: str,
    ) -> Dict[str, Any]:
        """解密读取单个 Artifact 正文。

        跨 Run 的 artifact_id 与已过保留期的 Artifact 一律 Not Found，
        不泄露其他 Run 内 Artifact 的存在性。
        """
        from app.core.db.database import t_ai_autonomous_artifact
        from app.tools.basesec import decrypt_secret

        self._get_run_row(owner, run_id)
        row = self.session.query(t_ai_autonomous_artifact).filter_by(
            id=artifact_id, run_id=run_id,
        ).first()
        if row is None:
            raise AutonomyNotFound('artifact not found')
        if row.expires_at is not None and row.expires_at < _utcnow():
            raise AutonomyNotFound('artifact expired')
        meta = self._artifact_meta(row)
        meta['content'] = decrypt_secret(row.content_ciphertext)
        return meta

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
        self._enforce_custom_profile(run, 'probe')
        budget = Budget(**json.loads(run.budget_json or '{}'))
        action_count = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id, kind=StepKind.ACTION.value,
        ).count()
        if action_count >= budget.max_actions:
            raise AutonomyConflict('action budget exhausted')

        host = self._get_host_row(run.host_id)
        # 预构造命令：命令本身不落快照（S1 无执行），仅用于构造期校验。
        # 网络验证由当前 Run 的权威 Host 地址限域，不能被模型参数
        # 扩成任意目标探测。
        try:
            build_probe_command(
                probe_id, normalized, target_host=str(host.host_ip),
            )
        except ActionValidationError as exc:
            raise AutonomyValidationError(str(exc)) from exc

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
        decision, reason = classify_action(
            run.mode, action, host.ai_environment,
        )

        seq = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id,
        ).count() + 1
        summary = redacted_summary(action)

        if decision == PolicyDecision.DENY:
            initial_status = StepStatus.FAILED.value
        elif decision == PolicyDecision.ASK:
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
            note='' if decision != PolicyDecision.DENY else reason[:255],
        )
        self.session.add(step)
        self._bump(run)
        self.append_event(run, 'step_proposed', {
            'step_id': step_id, 'seq': seq, 'probe_id': str(probe_id),
            'decision': decision.value, 'reason': reason,
        })
        if decision == PolicyDecision.ASK:
            if run.status != RunStatus.WAITING_APPROVAL.value:
                assert_run_transition(
                    run.status, RunStatus.WAITING_APPROVAL.value,
                )
                run.status = RunStatus.WAITING_APPROVAL.value
                self._bump(run)
        self._commit()
        return self._step_to_dict(step)

    # ------------------------------------------------------------------
    # 验证提案：副作用后的全新只读观察（S3 切片 4）
    # ------------------------------------------------------------------

    def propose_verification(
        self,
        owner: str,
        role: str,
        run_id: str,
        probe_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """提议一个 verification Step：只读探针，且必须已有写副作用。

        动作成功不等于目标达成：验证必须是副作用之后的全新只读
        观察。还没有任何写动作成功过就提议验证属于模型幻觉，
        fail-closed 拒绝。
        """
        from app.core.db.database import t_ai_autonomous_step

        run = self._get_run_row(owner, run_id)
        if run.status not in {
            RunStatus.QUEUED.value, RunStatus.RUNNING.value,
            RunStatus.WAITING_APPROVAL.value,
        }:
            raise AutonomyConflict(
                'steps can only be proposed while the run is active'
            )
        if not self._has_succeeded_write(run_id):
            raise AutonomyValidationError(
                'verification requires a prior succeeded write action'
            )
        if str(probe_id or '') not in list_probe_ids():
            raise AutonomyValidationError('unknown probe: %r' % (probe_id,))
        try:
            normalized = validate_probe(probe_id, params or {})
        except ActionValidationError as exc:
            raise AutonomyValidationError(str(exc)) from exc

        self._revalidate_boundaries(owner, role, run)
        budget = Budget(**json.loads(run.budget_json or '{}'))
        host = self._get_host_row(run.host_id)
        try:
            build_probe_command(
                probe_id, normalized, target_host=str(host.host_ip),
            )
        except ActionValidationError as exc:
            raise AutonomyValidationError(str(exc)) from exc

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
        # 探针在策略下永远 ALLOW；这里仍复核一遍，绝不信任假定。
        decision, reason = classify_action(
            run.mode, action, host.ai_environment,
        )
        if decision != PolicyDecision.ALLOW:
            raise AutonomyValidationError(
                'verification probe unexpectedly not allowed'
            )

        seq = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id,
        ).count() + 1
        step = t_ai_autonomous_step(
            id=step_id,
            run_id=run_id,
            kind=StepKind.VERIFICATION.value,
            status=StepStatus.PROPOSED.value,
            seq=seq,
            summary=redacted_summary(action),
            action_json=json.dumps(
                action.to_canonical_dict(), sort_keys=True, ensure_ascii=True,
            ),
            action_digest=build_action_digest(action, self.secret_key),
            note='',
        )
        self.session.add(step)
        self._bump(run)
        self.append_event(run, 'step_proposed', {
            'step_id': step_id, 'seq': seq, 'probe_id': str(probe_id),
            'step_kind': StepKind.VERIFICATION.value,
            'decision': decision.value, 'reason': reason,
        })
        self._commit()
        return self._step_to_dict(step)

    def _has_succeeded_write(self, run_id: str) -> bool:
        """本 Run 是否已有写动作成功落库（验证的前置条件）。"""
        from app.core.db.database import t_ai_autonomous_step

        rows = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id,
            kind=StepKind.ACTION.value,
            status=StepStatus.SUCCEEDED.value,
        ).all()
        for row in rows:
            try:
                action = action_from_dict(json.loads(row.action_json or ''))
            except (ActionValidationError, TypeError, ValueError):
                continue
            if str(action.kind) in WRITE_KINDS:
                return True
        return False

    # ------------------------------------------------------------------
    # 权限档案：custom 类别在提案时强制（S3 切片 3）
    # ------------------------------------------------------------------

    @staticmethod
    def _custom_profile(run):
        raw = getattr(run, 'custom_profile_json', None)
        if not raw:
            return None
        try:
            profile = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return profile if isinstance(profile, dict) else None

    def _enforce_custom_profile(self, run, kind) -> None:
        """custom 档案只放行选定的动作类别；probe 永不受限。

        档案缺失/损坏时 fail-closed：写动作拒绝，探针放行（探针是
        服务端自有只读能力，不属于管理员可配置的动作类别）。
        """
        if str(run.mode or '') != RunMode.CUSTOM.value:
            return
        if str(kind) == 'probe':
            return
        profile = self._custom_profile(run)
        allowed = set((profile or {}).get('action_categories') or [])
        if str(kind) not in allowed:
            raise AutonomyValidationError(
                'action category %r is not in the custom profile'
                % (str(kind),)
            )

    # ------------------------------------------------------------------
    # 计划提案：一次授权一个稳定计划（S3 切片 2）
    # ------------------------------------------------------------------

    def _plan_binding(self, run) -> Dict[str, Any]:
        """执行/决策边界从权威 Run/Host 行现取的当前绑定。

        与计划快照比较：目标、凭据引用、模式、预算、图版本或
        资产环境任一漂移，计划授权即失效回 ask。
        """
        host = self._get_host_row(run.host_id)
        return {
            'target_id': int(run.host_id),
            'credential_ref': 'system_user:%d' % int(run.system_user_id),
            'mode': str(run.mode or ''),
            'budget': json.loads(run.budget_json or '{}'),
            'graph_version': str(run.graph_version or ''),
            'environment': str(host.ai_environment),
        }

    def propose_plan(
        self,
        owner: str,
        role: str,
        run_id: str,
        summary: str,
        actions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """把模型提议的有序动作列表固化成不可变 plan Step。

        目标绑定、预算、图版本与凭据引用全部取自权威 Run 行；
        每个动作预先分配 step_id 并进 digest，展开执行时绝不重新
        生成。任一动作被服务端策略拒绝则整体拒绝，绝不落半张计划。
        """
        from app.core.db.database import t_ai_autonomous_step

        run = self._get_run_row(owner, run_id)
        if run.status not in {
            RunStatus.QUEUED.value, RunStatus.RUNNING.value,
            RunStatus.WAITING_APPROVAL.value,
        }:
            raise AutonomyConflict(
                'steps can only be proposed while the run is active'
            )
        items = list(actions or [])
        if not items:
            raise AutonomyValidationError('plan requires at least one action')
        if len(items) > PLAN_MAX_ACTIONS:
            raise AutonomyValidationError(
                'plan exceeds %d actions' % PLAN_MAX_ACTIONS
            )
        # 同一 Run 同时只允许一个未决计划：旧计划必须先被决策或
        # 执行完毕，避免两张计划争抢同一份授权。
        active_plan = (
            self.session.query(t_ai_autonomous_step)
            .filter_by(run_id=run_id, kind=StepKind.PLAN.value)
            .filter(
                t_ai_autonomous_step.status.notin_(
                    [s.value for s in TERMINAL_STEP_STATUSES],
                ),
            )
            .first()
        )
        if active_plan is not None:
            raise AutonomyConflict(
                'a plan already requires a decision or execution'
            )

        self._revalidate_boundaries(owner, role, run)
        budget = Budget(**json.loads(run.budget_json or '{}'))
        action_count = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id, kind=StepKind.ACTION.value,
        ).count()
        if action_count + len(items) > int(budget.max_actions):
            raise AutonomyConflict('action budget exhausted')

        host = self._get_host_row(run.host_id)
        constructed = []
        needs_ask = False
        for item in items:
            if not isinstance(item, dict):
                raise AutonomyValidationError(
                    'plan actions must be objects'
                )
            kind = str(item.get('kind') or '')
            params = item.get('params')
            if params is None:
                params = item.get('parameters') or {}
            step_id = uuid.uuid4().hex
            try:
                normalized = validate_plan_action(
                    kind, params,
                    run_id=run_id, step_id=step_id,
                    target_host=str(host.host_ip),
                )
            except ActionValidationError as exc:
                raise AutonomyValidationError(str(exc)) from exc
            self._enforce_custom_profile(run, kind)
            action = StructuredAction(
                kind=kind,
                target_id=int(run.host_id),
                system_user_id=int(run.system_user_id),
                parameters=normalized,
                timeout_seconds=min(budget.command_timeout_seconds, 600),
                step_id=step_id,
            )
            decision, reason = classify_action(
                run.mode, action, host.ai_environment,
            )
            if decision == PolicyDecision.DENY:
                raise AutonomyValidationError(
                    'plan action denied by server policy: %s' % reason
                )
            if decision == PolicyDecision.ASK:
                needs_ask = True
            constructed.append(action)

        ordered_digests = [
            build_action_digest(action, self.secret_key)
            for action in constructed
        ]
        expires_at = int(time.time()) + _approval_ttl_seconds()
        snapshot = build_plan_snapshot(
            graph_version=str(run.graph_version or ''),
            mode=str(run.mode or ''),
            target_id=int(run.host_id),
            system_user_id=int(run.system_user_id),
            budget=budget.to_dict(),
            expires_at=expires_at,
            summary=sanitize_text(summary)[:PLAN_SUMMARY_CHARS],
            actions_canonical=[
                action.to_canonical_dict() for action in constructed
            ],
            ordered_action_digests=ordered_digests,
        )

        plan_step_id = uuid.uuid4().hex
        initial_status = (
            StepStatus.WAITING_APPROVAL.value
            if needs_ask else StepStatus.APPROVED.value
        )
        seq = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id,
        ).count() + 1
        step = t_ai_autonomous_step(
            id=plan_step_id,
            run_id=run_id,
            kind=StepKind.PLAN.value,
            status=initial_status,
            seq=seq,
            summary=sanitize_text(summary)[:255] or 'plan',
            action_json=canonical_plan_json(snapshot),
            action_digest=build_plan_digest(snapshot, self.secret_key),
            note='',
        )
        self.session.add(step)
        self._bump(run)
        self.append_event(run, 'plan_proposed', {
            'step_id': plan_step_id,
            'seq': seq,
            'action_count': len(constructed),
            'decision': 'ask' if needs_ask else 'allow',
        })
        if needs_ask:
            if run.status != RunStatus.WAITING_APPROVAL.value:
                assert_run_transition(
                    run.status, RunStatus.WAITING_APPROVAL.value,
                )
                run.status = RunStatus.WAITING_APPROVAL.value
                self._bump(run)
            self.append_event(run, 'steps_waiting_approval', {
                'step_ids': [plan_step_id],
            })
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
            raise AutonomyConflict(
                'expected_revision is missing or invalid'
            ) from None
        if expected != int(run.revision or 0):
            raise AutonomyConflict('stale revision')
        if step.status != StepStatus.WAITING_APPROVAL.value:
            raise AutonomyConflict('step is not awaiting approval')

        # digest 复核：快照被篡改则审批无效。plan Step 走计划级
        # 授权复核（digest + 过期 + 当前绑定），动作 Step 走单动作
        # digest 复核。
        if step.kind == StepKind.PLAN.value:
            try:
                snapshot = parse_plan_snapshot(step.action_json or '')
                verify_plan_authorization(
                    snapshot, step.action_digest,
                    self._plan_binding(run), self.secret_key,
                )
            except PlanAuthorizationError as exc:
                raise AutonomyConflict(
                    'plan authorization invalid: %s' % exc.reason,
                ) from None
        else:
            try:
                action = action_from_dict(json.loads(step.action_json or '{}'))
            except (ActionValidationError, ValueError):
                raise AutonomyConflict('action snapshot is corrupted') from None
            if not verify_action_digest(
                action, step.action_digest, self.secret_key,
            ):
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

    def set_host_environment(
        self, host_id: int, environment: str,
    ) -> Dict[str, Any]:
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
        require_full_content: bool = False,
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
        if require_full_content and truncated:
            # Machine-required evidence (for example a rollback reference)
            # must never silently become an omission marker.  The caller can
            # then fail closed instead of reporting a write as safely
            # recoverable without the complete reference.
            raise AutonomyConflict(
                'required artifact capacity is unavailable'
            )
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

    # ------------------------------------------------------------------
    # Evidence 与三态 Outcome（S3 切片 4）
    # ------------------------------------------------------------------

    @staticmethod
    def _evidence_to_dict(row) -> Dict[str, Any]:
        try:
            artifact_ids = json.loads(row.artifact_ids_json or '[]')
        except (TypeError, ValueError):
            artifact_ids = []
        return {
            'id': row.id,
            'run_id': row.run_id,
            'step_id': row.step_id,
            'kind': row.kind,
            'summary': row.summary,
            'artifact_ids': list(artifact_ids),
            # M1 的 Evidence 永远不可信：只作索引，不作结论凭据。
            'trusted': bool(row.trusted),
            'created_at': getattr(row, 'created_at', None),
        }

    def record_evidence(
        self,
        owner: str,
        run_id: str,
        *,
        kind: str,
        summary: str,
        step_id: Optional[str] = None,
        artifact_ids: Optional[List[str]] = None,
        event_type: Optional[str] = None,
        commit: bool = True,
    ) -> Dict[str, Any]:
        """把一次执行观察归一化成有界脱敏的 Evidence 引用。

        大输出本体留在加密 Artifact；Evidence 只是索引，永远标记
        不可信。引用的 Artifact 必须属于同一 Run。
        """
        from app.core.db.database import (
            t_ai_autonomous_artifact, t_ai_autonomous_evidence,
        )

        run = self._get_run_row(owner, run_id)
        if str(kind) not in EVIDENCE_KINDS:
            raise AutonomyValidationError('unknown evidence kind: %r' % (kind,))
        text = sanitize_text(summary or '')[:EVIDENCE_SUMMARY_CHARS]
        ids: List[str] = []
        for artifact_id in (artifact_ids or []):
            artifact_id = str(artifact_id or '')
            if artifact_id and artifact_id not in ids:
                ids.append(artifact_id)
        if ids:
            found = self.session.query(t_ai_autonomous_artifact.id).filter(
                t_ai_autonomous_artifact.run_id == run_id,
                t_ai_autonomous_artifact.id.in_(ids),
            ).count()
            if found != len(ids):
                raise AutonomyValidationError(
                    'evidence may only reference same-run artifacts'
                )
        evidence = t_ai_autonomous_evidence(
            id=uuid.uuid4().hex,
            run_id=run_id,
            step_id=step_id,
            kind=str(kind),
            summary=text,
            artifact_ids_json=json.dumps(ids),
            trusted=False,
        )
        self.session.add(evidence)
        if event_type:
            self.append_event(run, str(event_type), {
                'evidence_id': evidence.id,
                'kind': evidence.kind,
                'summary': evidence.summary,
            })
        if commit:
            self._commit()
        return self._evidence_to_dict(evidence)

    def list_evidence(self, owner: str, run_id: str) -> List[Dict[str, Any]]:
        from app.core.db.database import t_ai_autonomous_evidence

        self._get_run_row(owner, run_id)
        rows = self.session.query(t_ai_autonomous_evidence).filter_by(
            run_id=run_id,
        ).order_by(t_ai_autonomous_evidence.created_at.asc()).all()
        return [self._evidence_to_dict(row) for row in rows]

    def conclude_run(
        self,
        owner: str,
        role: str,
        run_id: str,
        outcome: str,
        evidence_ids: List[str],
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """落库唯一终局 Outcome：必须引用同一 Run 的 Evidence。

        fail-closed 降级：存在结果不确定的写动作时绝不 resolved；
        resolved 必须引用至少一条验证观察。缺失证据的结论只能
        inconclusive，绝不虚构成功。首个结论获胜，后续不改写。
        """
        from app.core.db.database import (
            t_ai_autonomous_evidence, t_ai_autonomous_step,
        )

        run = self._get_run_row(owner, run_id)
        if str(run.outcome or ''):
            # 终局 Outcome 恰好一个：重复结论不改写也不报错。
            return {'outcome': run.outcome, 'already_concluded': True}
        if run.status not in {
            RunStatus.QUEUED.value, RunStatus.RUNNING.value,
            RunStatus.WAITING_APPROVAL.value,
        }:
            raise AutonomyConflict(
                'outcome can only be concluded while the run is active'
            )
        if str(outcome) not in {o.value for o in RunOutcome}:
            raise AutonomyValidationError('unknown outcome: %r' % (outcome,))
        ids: List[str] = []
        for evidence_id in (evidence_ids or []):
            evidence_id = str(evidence_id or '').strip()
            if evidence_id and evidence_id not in ids:
                ids.append(evidence_id)
        if not ids:
            raise AutonomyValidationError(
                'conclusion requires same-run evidence citations'
            )
        if len(ids) > MAX_EVIDENCE_CITATIONS:
            raise AutonomyValidationError('too many evidence citations')
        rows = self.session.query(t_ai_autonomous_evidence).filter(
            t_ai_autonomous_evidence.run_id == run_id,
            t_ai_autonomous_evidence.id.in_(ids),
        ).all()
        if len(rows) != len(ids):
            raise AutonomyValidationError(
                'conclusion may only cite same-run evidence'
            )
        normalized_details = normalize_conclusion_details(
            details or fallback_conclusion_details(),
        )

        requested = str(outcome)
        forced = ''
        uncertain = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run_id, status=StepStatus.OUTCOME_UNKNOWN.value,
        ).count()
        if uncertain:
            # S2 语义保留：写结果未确认绝不自动收口成 resolved。
            outcome = RunOutcome.INCONCLUSIVE.value
            forced = 'uncertain_write'
        elif requested == RunOutcome.RESOLVED.value and not any(
            row.kind == 'verification_observation' for row in rows
        ):
            # 动作成功不是目标达成的证明：缺验证观察不能 resolved。
            outcome = RunOutcome.INCONCLUSIVE.value
            forced = 'verification_missing'

        run.outcome = outcome
        run.conclusion_json = json.dumps({
            **normalized_details,
            'final_status': outcome,
            'evidence_ids': ids,
        }, ensure_ascii=False, separators=(',', ':'))
        self._bump(run)
        self.append_event(run, 'run_concluded', {
            'outcome': outcome,
            'requested': requested,
            'forced': forced,
            'evidence_ids': ids,
        })
        self._commit()
        return {
            'outcome': outcome,
            'requested': requested,
            'forced': forced,
            'already_concluded': False,
        }
