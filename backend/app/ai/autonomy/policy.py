# -*- coding: utf-8 -*-
"""M1/S1+S2: 服务端动作策略、预算、敏感路径与永久拒绝清单。

策略完全由服务端持有，采用常见 Harness 的 allow/ask/deny 契约：
- probe（服务端自有探针）是唯一可自动执行的只读动作；
- file_read 永不自动：敏感路径直接拒绝，其余等待精确审批；
- systemd/package_install 结构化写动作永不自动：read_only 拒绝，
  其余等待精确审批；参数在 actions.validate_write_action 白名单
  校验，构造结果再过永久拒绝清单；
- shell 永不自动：read_only 模式直接拒绝，其余等待精确审批；
  含管道/重定向/解释器/下载执行等特征时按高危处理。
- 永久拒绝清单（磁盘分区格式化、根目录宽删、主动读秘密、横向
  SSH、绕过审计）即使命中人工精确审批也不执行；黑名单只提供
  风险信号，永久拒绝清单才是硬规则。
- auto（以及兼容值 lab_autonomous）仅由管理员维护的
  t_host.ai_environment='lab' 授予；名为 lab 的普通资产组不带来
  任何自动执行能力。
"""
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from app.ai.autonomy.actions import StructuredAction
from app.ai.autonomy.state import AiEnvironment, RunMode, normalize_run_mode


class PolicyDecision(str, Enum):
    """Deterministic harness decision; the model never grants permission."""

    ALLOW = 'allow'
    ASK = 'ask'
    DENY = 'deny'

    # Compatibility names for existing internal callers. Enum aliases do not
    # add policy values or change the persisted allow/ask/deny vocabulary.
    AUTO = 'allow'
    APPROVAL_REQUIRED = 'ask'
    DENIED = 'deny'


ApprovalDecision = PolicyDecision


class PolicyViolation(Exception):
    """动作命中永久拒绝规则。"""


# ---------------------------------------------------------------------------
# 预算：服务端硬上限不可被调用方抬高
# ---------------------------------------------------------------------------

# 默认值与硬上限来自 docs/ai/ROADMAP.md 的执行预算章节。
BUDGET_LIMITS: Dict[str, Tuple[int, int]] = {
    # field: (default, hard_max)
    'duration_seconds': (3600, 3600),
    'max_loops': (20, 20),
    'max_actions': (30, 30),
    'command_timeout_seconds': (60, 600),
    'step_output_bytes': (65536, 65536),
    'run_artifact_bytes': (2097152, 2097152),
}


@dataclass(frozen=True)
class Budget:
    duration_seconds: int = BUDGET_LIMITS['duration_seconds'][0]
    max_loops: int = BUDGET_LIMITS['max_loops'][0]
    max_actions: int = BUDGET_LIMITS['max_actions'][0]
    command_timeout_seconds: int = BUDGET_LIMITS['command_timeout_seconds'][0]
    step_output_bytes: int = BUDGET_LIMITS['step_output_bytes'][0]
    run_artifact_bytes: int = BUDGET_LIMITS['run_artifact_bytes'][0]

    def to_dict(self) -> Dict[str, int]:
        return {
            'duration_seconds': self.duration_seconds,
            'max_loops': self.max_loops,
            'max_actions': self.max_actions,
            'command_timeout_seconds': self.command_timeout_seconds,
            'step_output_bytes': self.step_output_bytes,
            'run_artifact_bytes': self.run_artifact_bytes,
        }


def parse_budget(payload: Any) -> Budget:
    """从创建请求解析预算；缺省用默认值，越界拒绝而不是静默钳制。"""
    if payload is None:
        return Budget()
    if not isinstance(payload, dict):
        raise PolicyViolation('budget must be an object')
    values = {}
    for name, (default, hard_max) in BUDGET_LIMITS.items():
        if name not in payload:
            values[name] = default
            continue
        raw = payload[name]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise PolicyViolation('budget.%s must be an integer' % name) from None
        if value < 1 or value > hard_max:
            raise PolicyViolation(
                'budget.%s must be within 1..%d' % (name, hard_max)
            )
        values[name] = value
    unknown = set(payload) - set(BUDGET_LIMITS)
    if unknown:
        raise PolicyViolation(
            'unknown budget fields: %s' % ', '.join(sorted(unknown))
        )
    return Budget(**values)


# ---------------------------------------------------------------------------
# 敏感路径：服务端策略拒绝，提示词不是安全控制
# ---------------------------------------------------------------------------

_SENSITIVE_PATH_PATTERNS = (
    re.compile(r'^/etc/(shadow|gshadow|sudoers|sudoers\.d(/.*)?)$'),
    re.compile(r'^/etc/(ssl|pki|ca-certificates)/.*\.key$'),
    re.compile(r'^/root/\.ssh(/.*)?$'),
    re.compile(r'^/home/[^/]+/\.ssh(/.*)?$'),
    re.compile(r'.*\.pem$'),
    re.compile(r'.*id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$'),
    re.compile(r'.*(^|/)\.env(\..+)?$'),
    re.compile(r'^/proc/[^/]+/(mem|environ)$'),
    re.compile(r'.*(secret|credential|password|token).*\.(key|pem|json|ya?ml|ini|conf)$'),
)


def is_sensitive_path(path: str) -> bool:
    """通用文件读取的敏感路径判定（命中即永久拒绝）。"""
    normalized = str(path or '').strip()
    if not normalized:
        return True
    return any(p.match(normalized) for p in _SENSITIVE_PATH_PATTERNS)


# ---------------------------------------------------------------------------
# 永久拒绝清单（ROADMAP「模式和动作」）：即使有精确人工审批也拒绝
# ---------------------------------------------------------------------------

_PERMANENT_DENY_PATTERNS = (
    (re.compile(r'\bmkfs(\.[a-z0-9]+)?\b'),
     'disk formatting is permanently denied'),
    (re.compile(r'\b(fdisk|parted|sgdisk|wipefs)\b'),
     'disk partitioning is permanently denied'),
    (re.compile(r'\bdd\b[^|;&]*\bof=/dev/'),
     'raw writes to block devices are permanently denied'),
    (re.compile(r'\brm\b[^\n]*--no-preserve-root'),
     'root filesystem deletion is permanently denied'),
    # rm 带任意旗标且目标是 / 或 /*：根目录宽范围删除。
    (re.compile(
        r'\brm\s+(?:-[a-zA-Z]+\s+)+(?:--\s+)?'
        r'[\'\"]?/\*?[\'\"]?(?=\s|[;&|)]|$)'
    ),
     'root filesystem deletion is permanently denied'),
    (re.compile(r'\bssh\s'), 'lateral SSH is permanently denied'),
    (re.compile(r'\b(scp|sftp)\s'),
     'lateral SSH transfer is permanently denied'),
    (re.compile(r'\bsetenforce\s+0\b'),
     'disabling SELinux is permanently denied'),
    (re.compile(r'\bauditctl\s+(-D|-e\s+0)'),
     'disabling audit is permanently denied'),
    (re.compile(r'\bsystemctl\s+(stop|disable|mask)\s+auditd\b'),
     'disabling the audit service is permanently denied'),
)

# 命令文本中出现秘密载体即视为主动读取密钥（v1 永久拒绝）。
# 宁严勿漏：合法维护走精确审批的结构化动作，不走任意 Shell。
_SECRET_TARGET_RE = re.compile(
    r'/etc/shadow\b|/etc/gshadow\b|/etc/sudoers\b|/etc/ssl/private/'
    r'|\.ssh/|id_(rsa|dsa|ecdsa|ed25519)\b|\.pem\b|(^|\s)/[^ ]*\.env(\.|\b)'
)


def permanent_deny_reason(command: str) -> Optional[str]:
    """永久拒绝清单复核：命中返回拒绝原因，否则返回 None。

    在执行器构造命令时与提案分类时都会调用；清单是服务端硬
    规则，审批不能推翻。
    """
    text = str(command or '')
    if not text.strip():
        return None
    for pattern, reason in _PERMANENT_DENY_PATTERNS:
        if pattern.search(text):
            return reason
    if _SECRET_TARGET_RE.search(text):
        return 'reading secrets is permanently denied'
    return None


# ---------------------------------------------------------------------------
# Shell 命令风险特征：只用于提高审批风险提示，绝不是安全证明
# ---------------------------------------------------------------------------

_SHELL_RISK_PATTERNS = (
    (re.compile(r'[|;`]'), 'pipeline or command chaining'),
    (re.compile(r'&&|\|\|'), 'command chaining'),
    (re.compile(r'[<>]'), 'redirection'),
    (re.compile(r'\$\('), 'command substitution'),
    (re.compile(r'(^|\s)(bash|sh|zsh|python[23]?|perl|ruby|node)\s+-c\b'), 'inline interpreter'),
    (re.compile(r'(^|\s)(wget|curl)\b.*\|\s*(bash|sh)\b'), 'download and execute'),
    (re.compile(r'(^|\s)(wget|curl)\b'), 'download command'),
    (re.compile(r'\n|\r'), 'embedded newline'),
)


def classify_shell_command(command: str) -> Tuple[PolicyDecision, str]:
    """对任意 Shell 文本做风险分级。

    shell 动作永远不可能自动执行；永久拒绝清单命中直接拒绝。
    管道、重定向、解释器、下载等只提供风险信号，仍必须绑定完整
    动作摘要进行精确人工审批，不能把黑名单当成安全判定器。
    """
    text = str(command or '')
    if not text.strip():
        return PolicyDecision.DENY, 'empty command'
    deny_reason = permanent_deny_reason(text)
    if deny_reason is not None:
        return PolicyDecision.DENY, deny_reason
    for pattern, reason in _SHELL_RISK_PATTERNS:
        if pattern.search(text):
            return PolicyDecision.ASK, (
                'high-risk shell requires exact approval: %s' % reason
            )
    return PolicyDecision.ASK, 'arbitrary shell requires exact approval'


# ---------------------------------------------------------------------------
# 动作分类：模式 × kind × 环境 → 决策
# ---------------------------------------------------------------------------

def validate_mode_for_environment(mode: str, environment: str) -> None:
    """自动档只授予管理员标记为 lab 的资产。"""
    parsed_mode = RunMode(mode)
    parsed_environment = AiEnvironment(environment)
    canonical_mode = normalize_run_mode(parsed_mode.value)
    if (
        canonical_mode == RunMode.AUTO
        and parsed_environment != AiEnvironment.LAB
    ):
        raise PolicyViolation(
            'auto mode requires ai_environment=lab on the target host'
        )


def classify_action(
    mode: str,
    action: StructuredAction,
    environment: str,
) -> Tuple[PolicyDecision, str]:
    """服务端策略：返回 (决策, 原因)。

    - probe 是服务端自有只读探针，任何模式下都可自动（参数在
      actions.validate_probe 已白名单校验）；
    - file_read 敏感路径拒绝，其余任何模式都要求审批；
    - shell 在 read_only 拒绝，其余要求审批，永不自动；
    - systemd/package_install 结构化写动作在 read_only 拒绝，其余
      要求精确审批，永不自动；
    - file_patch/file_restore 带备份承诺的文件写动作同样永不自动：
      read_only 拒绝，敏感路径拒绝，其余要求精确审批。
    """
    canonical_mode = normalize_run_mode(mode)
    AiEnvironment(environment)
    kind = str(action.kind)
    if kind == 'probe':
        return PolicyDecision.ALLOW, 'server-owned read-only probe'
    if kind == 'file_read':
        path = str(action.parameters.get('path') or '')
        if is_sensitive_path(path):
            return PolicyDecision.DENY, 'sensitive path is denied by server policy'
        return PolicyDecision.ASK, 'general file reads are never automatic'
    if kind == 'shell':
        command = str(action.parameters.get('command') or '')
        decision, reason = classify_shell_command(command)
        if decision == PolicyDecision.DENY:
            return PolicyDecision.DENY, reason
        if canonical_mode == RunMode.READ_ONLY:
            return PolicyDecision.DENY, 'shell actions are denied in read_only mode'
        return PolicyDecision.ASK, reason
    if kind in ('systemd', 'package_install'):
        # 结构化写动作：模板与参数白名单在 actions 层校验；这里
        # 只决定审批策略——永不自动。
        if canonical_mode == RunMode.READ_ONLY:
            return PolicyDecision.DENY, (
                '%s actions are denied in read_only mode' % kind
            )
        return PolicyDecision.ASK, (
            'structured write requires exact approval'
        )
    if kind in ('file_patch', 'file_restore'):
        # 文件写动作：路径白名单与敏感路径复核在 actions 层；
        # 敏感路径在策略层先拒绝，其余永不自动。
        path = str(action.parameters.get('path') or '')
        if is_sensitive_path(path):
            return PolicyDecision.DENY, 'sensitive path is denied by server policy'
        if canonical_mode == RunMode.READ_ONLY:
            return PolicyDecision.DENY, (
                '%s actions are denied in read_only mode' % kind
            )
        return PolicyDecision.ASK, (
            'file write requires exact approval'
        )
    return PolicyDecision.DENY, 'unknown action kind'
