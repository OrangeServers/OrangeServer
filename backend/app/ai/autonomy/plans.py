# -*- coding: utf-8 -*-
"""M1/S3 切片 2：不可变计划快照与计划级授权 digest。

设计要点：
- 一次授权一个稳定计划：plan Step 的 action_json 承载不可变计划
  快照，action_digest 绑定该快照。授权 digest 覆盖有序动作 digest、
  目标、凭据引用、策略版本、预算、图版本与过期时间；任一字段被
  篡改、漂移或过期，授权即失效并回 ask（由调用方落事件）。
- 计划只能由服务端构造：目标绑定（host/system_user）、预算、图
  版本与凭据引用全部取自权威 Run 行，模型只提交 kind+params。
- 计划不授权任意未来动作：kind 锁定在结构化动作族，参数白名单在
  actions 层逐条校验；自由 Shell 文本不进计划。
- 本模块是纯函数契约，不依赖数据库；repository/drive 落库前后
  都必须经过这里复核。
"""
import hashlib
import hmac
import json
import time
from typing import Any, Dict, List

from app.ai.autonomy.actions import (
    ActionValidationError,
    StructuredAction,
    action_from_dict,
    build_action_digest,
    build_file_patch_command,
    build_file_restore_command,
    build_probe_command,
    build_write_command,
    patch_backup_path,
    validate_probe,
    validate_write_action,
    verify_action_digest,
)
from app.ai.autonomy.policy import (
    PolicyDecision,
    classify_action,
)
from app.ai.autonomy.state import normalize_run_mode

# 快照格式版本：升级必须换值，旧快照按原版本复核或拒绝。
PLAN_VERSION = 1

# 服务端策略版本：策略语义变化时递增，旧计划授权随之失效。
POLICY_VERSION = '1'

# 单个计划最多 10 个动作；超出的工作必须拆成新计划重新决策。
PLAN_MAX_ACTIONS = 10

PLAN_SUMMARY_CHARS = 200

# 计划允许的动作族：全部有服务端模板/白名单。自由 Shell 不进
# 计划——计划级授权绝不覆盖任意命令文本。
PLAN_ACTION_KINDS = (
    'probe', 'systemd', 'package_install', 'file_patch', 'file_restore',
)

REASON_MALFORMED_PLAN = 'malformed_plan'
REASON_DIGEST_MISMATCH = 'digest_mismatch'
REASON_EXPIRED = 'expired'
REASON_TARGET_CHANGED = 'target_changed'
REASON_CREDENTIAL_CHANGED = 'credential_changed'
REASON_POLICY_CHANGED = 'policy_changed'
REASON_MODE_CHANGED = 'mode_changed'
REASON_BUDGET_CHANGED = 'budget_changed'
REASON_GRAPH_VERSION_CHANGED = 'graph_version_changed'
REASON_ACTION_DIGEST_MISMATCH = 'action_digest_mismatch'


class PlanAuthorizationError(Exception):
    """计划授权复核失败：可预期、fail-closed、原因短 token。"""

    def __init__(self, reason):
        self.reason = str(reason)[:64]
        super().__init__(self.reason)


def credential_ref_for(system_user_id: int) -> str:
    """凭据按引用绑定：只进 digest，永不展开成明文凭据。"""
    return 'system_user:%d' % int(system_user_id)


def canonical_plan_json(snapshot: Dict[str, Any]) -> str:
    return json.dumps(
        snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
    )


def _plan_digest_key(secret_key: str) -> bytes:
    base = str(secret_key or '').encode('utf-8')
    return hashlib.sha256(b'ogs.ai.autonomy.plan.digest.v1:' + base).digest()


def build_plan_digest(snapshot: Dict[str, Any], secret_key: str) -> str:
    """对整个计划快照做 HMAC-SHA256：任一字段变化都会破坏 digest。"""
    payload = canonical_plan_json(snapshot).encode('utf-8')
    return hmac.new(
        _plan_digest_key(secret_key), payload, hashlib.sha256,
    ).hexdigest()


def verify_plan_digest(
    snapshot: Dict[str, Any], digest: str, secret_key: str,
) -> bool:
    if not digest:
        return False
    expected = build_plan_digest(snapshot, secret_key)
    return hmac.compare_digest(expected, str(digest))


def build_plan_snapshot(
    *,
    graph_version: str,
    mode: str,
    target_id: int,
    system_user_id: int,
    budget: Dict[str, int],
    expires_at: int,
    summary: str,
    actions_canonical: List[Dict[str, Any]],
    ordered_action_digests: List[str],
) -> Dict[str, Any]:
    """服务端构造的不可变计划快照；调用方不得再增删字段。"""
    return {
        'plan_version': PLAN_VERSION,
        'graph_version': str(graph_version),
        'mode': str(normalize_run_mode(mode).value),
        'target_id': int(target_id),
        'system_user_id': int(system_user_id),
        'credential_ref': credential_ref_for(system_user_id),
        'policy_version': POLICY_VERSION,
        'budget': {str(k): int(v) for k, v in dict(budget).items()},
        'expires_at': int(expires_at),
        'summary': str(summary),
        'actions': list(actions_canonical),
        'ordered_action_digests': [str(d) for d in ordered_action_digests],
    }


def parse_plan_snapshot(raw: Any) -> Dict[str, Any]:
    """解析并结构化校验计划快照；缺字段/错类型一律 malformed。"""
    try:
        snapshot = json.loads(raw or '')
    except (TypeError, ValueError):
        raise PlanAuthorizationError(REASON_MALFORMED_PLAN) from None
    if not isinstance(snapshot, dict):
        raise PlanAuthorizationError(REASON_MALFORMED_PLAN)
    try:
        if int(snapshot['plan_version']) != PLAN_VERSION:
            raise PlanAuthorizationError(REASON_MALFORMED_PLAN)
        actions = snapshot['actions']
        digests = snapshot['ordered_action_digests']
        if (
            not isinstance(actions, list)
            or not actions
            or len(actions) > PLAN_MAX_ACTIONS
            or not isinstance(digests, list)
            or len(digests) != len(actions)
            or not isinstance(snapshot['budget'], dict)
            or not isinstance(snapshot['summary'], str)
        ):
            raise PlanAuthorizationError(REASON_MALFORMED_PLAN)
        int(snapshot['target_id'])
        int(snapshot['system_user_id'])
        int(snapshot['expires_at'])
        str(snapshot['mode'])
        str(snapshot['graph_version'])
        str(snapshot['credential_ref'])
        str(snapshot['policy_version'])
        for item in actions:
            if not isinstance(item, dict):
                raise PlanAuthorizationError(REASON_MALFORMED_PLAN)
        for digest in digests:
            str(digest)
    except (KeyError, TypeError, ValueError):
        raise PlanAuthorizationError(REASON_MALFORMED_PLAN) from None
    return snapshot


def verify_plan_authorization(
    snapshot: Dict[str, Any],
    stored_digest: str,
    binding: Dict[str, Any],
    secret_key: str,
    now: int = None,
) -> List[StructuredAction]:
    """执行前权威复核：digest、过期与当前绑定全部一致才放行。

    binding 由调用方从权威 Run/Host 行现取：
    target_id / credential_ref / mode / budget / graph_version /
    environment。任一项与快照不一致即授权失效（抛
    PlanAuthorizationError），绝不放行部分动作。返回按序重建的
    StructuredAction 列表供执行层使用。
    """
    if now is None:
        now = int(time.time())
    if not verify_plan_digest(snapshot, stored_digest, secret_key):
        raise PlanAuthorizationError(REASON_DIGEST_MISMATCH)
    if int(snapshot['expires_at']) <= int(now):
        raise PlanAuthorizationError(REASON_EXPIRED)
    if int(snapshot['target_id']) != int(binding['target_id']):
        raise PlanAuthorizationError(REASON_TARGET_CHANGED)
    if str(snapshot['credential_ref']) != str(binding['credential_ref']):
        raise PlanAuthorizationError(REASON_CREDENTIAL_CHANGED)
    if str(snapshot['policy_version']) != POLICY_VERSION:
        raise PlanAuthorizationError(REASON_POLICY_CHANGED)
    current_mode = str(normalize_run_mode(binding['mode']).value)
    if str(snapshot['mode']) != current_mode:
        raise PlanAuthorizationError(REASON_MODE_CHANGED)
    current_budget = {
        str(k): int(v) for k, v in dict(binding['budget']).items()
    }
    if dict(snapshot['budget']) != current_budget:
        raise PlanAuthorizationError(REASON_BUDGET_CHANGED)
    if str(snapshot['graph_version']) != str(binding['graph_version']):
        raise PlanAuthorizationError(REASON_GRAPH_VERSION_CHANGED)

    actions = []
    digests = list(snapshot['ordered_action_digests'])
    for index, canonical in enumerate(snapshot['actions']):
        action = action_from_dict(canonical)
        if not verify_action_digest(action, digests[index], secret_key):
            raise PlanAuthorizationError(REASON_ACTION_DIGEST_MISMATCH)
        # 策略在授权之后也可能变化（模式/环境/永久拒绝清单）；
        # 执行前重分类，任何动作落到 deny 即整个计划失效回 ask。
        decision, _reason = classify_action(
            current_mode, action, str(binding['environment']),
        )
        if decision == PolicyDecision.DENY:
            raise PlanAuthorizationError(REASON_POLICY_CHANGED)
        actions.append(action)
    return actions


# ---------------------------------------------------------------------------
# 计划动作校验：kind 锁定结构化动作族，参数白名单在 actions 层
# ---------------------------------------------------------------------------

def validate_plan_action(
    kind: str,
    params: Dict[str, Any],
    *,
    run_id: str,
    step_id: str,
    target_host: str,
) -> Dict[str, str]:
    """校验单个计划动作并返回规范化参数。

    构造期即过白名单与永久拒绝清单（与 propose_probe 同构），
    落进 digest 的动作与执行期构造的命令完全一致。
    """
    kind = str(kind or '')
    if kind not in PLAN_ACTION_KINDS:
        raise ActionValidationError(
            'plans do not support action kind %r' % (kind,)
        )
    params = params or {}
    if not isinstance(params, dict):
        raise ActionValidationError('action params must be an object')
    if kind == 'probe':
        probe_id = str(params.get('probe_id') or '')
        rest = {k: v for k, v in params.items() if k != 'probe_id'}
        normalized = dict(validate_probe(probe_id, rest), probe_id=probe_id)
        build_probe_command(probe_id, rest, target_host=target_host)
        return normalized
    if kind in ('systemd', 'package_install'):
        normalized = validate_write_action(kind, params)
        build_write_command(kind, normalized)
        return normalized
    if kind == 'file_patch':
        path = str(params.get('path') or '')
        content = str(params.get('content') or '')
        unknown = set(params) - {'path', 'content'}
        if unknown:
            raise ActionValidationError(
                'unexpected parameters: %s' % ', '.join(sorted(unknown))
            )
        backup = patch_backup_path(path, run_id, step_id)
        build_file_patch_command(path, content, backup)
        return {'path': path, 'content': content}
    # file_restore：备份路径必须来自本 Run 的成功补丁，执行期由
    # executor._validate_restore_source 权威复核。
    path = str(params.get('path') or '')
    backup_path = str(params.get('backup_path') or '')
    unknown = set(params) - {'path', 'backup_path'}
    if unknown:
        raise ActionValidationError(
            'unexpected parameters: %s' % ', '.join(sorted(unknown))
        )
    build_file_restore_command(path, backup_path)
    return {'path': path, 'backup_path': backup_path}
