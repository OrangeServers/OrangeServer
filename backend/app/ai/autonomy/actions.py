# -*- coding: utf-8 -*-
"""M1/S1+S2: 结构化动作 schema、服务端探针与审批 digest。

锁定的安全契约：
- 自动的只读工作只接受服务端自有探针 ID + 校验过的结构化参数；
  模型或调用方不能把任意 Shell 标记为只读。
- S2 有界读取：文件/日志读取限制行数与根目录白名单，敏感路径
  在参数层即被永久拒绝。
- 动作快照在审批前不可变地落库；凭据只以 ID 引用存在，永不进入
  快照、digest、Event 或响应。
- digest 绑定动作版本、目标、凭据引用、工具(kind/probe)、规范化
  参数、工作目录、超时与 Step ID；任一字段变化都会使审批失效。
"""
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict


# 动作 schema 版本。digest 与快照都绑定该版本；升级动作结构时必须
# 递增，避免旧审批被复用到新语义。
ACTION_VERSION = 1

# 参数值里出现任何 Shell 元字符即拒绝。探针参数只允许纯标量，
# 管道/重定向/命令替换在参数层就被堵死。
_PARAM_FORBIDDEN_RE = re.compile(r"[|&;<>()`$\\\"'\n\r\t]")

# 服务端自有探针：命令模板完全由服务端持有，参数白名单校验。
# 每个探针都是只读探测；新增探针必须过安全评审。
#
# S2 有界读取与验证探针：
# - file.read_bounded / log.tail 的行数上限 999，路径只允许白名单
#   根目录，`..` 逐段拒绝，敏感路径复用 policy.is_sensitive_path；
# - verify.* 探针的退出码即验证结论（0 = 通过），模板不含任何
#   Shell 元字符。
_PROBES: Dict[str, Dict[str, Any]] = {
    'system.load': {
        'title': '系统负载',
        'command': 'uptime',
        'params': {},
    },
    'system.memory': {
        'title': '内存使用',
        'command': 'free -m',
        'params': {},
    },
    'system.disk_usage': {
        'title': '磁盘使用',
        'command': 'df -h',
        'params': {},
    },
    'service.status': {
        'title': '服务状态',
        'command': 'systemctl status {unit} --no-pager',
        'params': {
            'unit': re.compile(r'^[A-Za-z0-9@:._-]{1,128}$'),
        },
    },
    'file.read_bounded': {
        'title': '有界文件读取',
        'command': 'head -n {lines} -- {path}',
        'params': {
            'lines': re.compile(r'^[1-9][0-9]{0,2}$'),
            'path': re.compile(r'^/[A-Za-z0-9._/-]{1,255}$'),
        },
        'check': lambda params: _check_bounded_path(
            params, _FILE_READ_ROOTS,
        ),
    },
    'log.tail': {
        'title': '有界日志读取',
        'command': 'tail -n {lines} -- {path}',
        'params': {
            'lines': re.compile(r'^[1-9][0-9]{0,2}$'),
            'path': re.compile(r'^/[A-Za-z0-9._/-]{1,255}$'),
        },
        'check': lambda params: _check_bounded_path(
            params, _LOG_TAIL_ROOTS,
        ),
    },
    'verify.port_open': {
        'title': '端口连通性验证',
        'command': 'nc -z -w 5 {host} {port}',
        'params': {
            'host': re.compile(r'^[A-Za-z0-9.-]{1,253}$'),
            'port': re.compile(r'^[1-9][0-9]{0,4}$'),
        },
        'check': lambda params: _check_port_range(params),
    },
    'verify.http_status': {
        'title': 'HTTP 状态验证',
        'command': (
            'curl --silent --show-error --output /dev/null'
            ' --write-out %{{http_code}} --max-time 10 {url}'
        ),
        'params': {
            # 只允许 scheme://host[:port]/path；查询串、凭据与元字符
            # 在白名单字符集之外，直接拒绝。
            'url': re.compile(r'^https?://[A-Za-z0-9._:/-]{1,500}$'),
        },
    },
    'verify.process_running': {
        'title': '进程存在性验证',
        'command': 'pgrep -c -x {process}',
        'params': {
            'process': re.compile(r'^[A-Za-z0-9._-]{1,64}$'),
        },
    },
    'verify.journal_pattern': {
        'title': '服务日志关键字验证',
        'command': (
            'journalctl -u {unit} -n {lines} --no-pager -g {pattern}'
        ),
        'params': {
            'unit': re.compile(r'^[A-Za-z0-9@:._-]{1,128}$'),
            'lines': re.compile(r'^[1-9][0-9]{0,2}$'),
            'pattern': re.compile(r'^[A-Za-z0-9._:-]{1,128}$'),
        },
    },
}

# 有界读取的根目录白名单；log.tail 进一步收紧到 /var/log。
# 新增根目录必须过安全评审。
_FILE_READ_ROOTS = ('/var/log', '/etc', '/opt')
_LOG_TAIL_ROOTS = ('/var/log',)


def _check_bounded_path(params: Dict[str, str], roots) -> None:
    """有界读取路径复核：逐段拒绝 `..`，限定根目录，敏感路径拒绝。

    符号链接逃逸不在 v1 承诺范围内；白名单根目录已经把攻击面收到
    最小，剩余风险由审批与审计兜底。
    """
    # 延迟导入：policy 依赖本模块的 StructuredAction。
    from app.ai.autonomy.policy import is_sensitive_path

    path = str(params.get('path') or '')
    parts = [part for part in path.split('/') if part]
    if '..' in parts or '.' in parts:
        raise ActionValidationError('path traversal is not allowed')
    if not any(
        path == root or path.startswith(root + '/') for root in roots
    ):
        raise ActionValidationError(
            'path is outside the bounded read roots'
        )
    if is_sensitive_path(path):
        raise ActionValidationError(
            'sensitive path is denied by server policy'
        )


def _check_port_range(params: Dict[str, str]) -> None:
    port = int(params['port'])
    if port < 1 or port > 65535:
        raise ActionValidationError('port must be within 1..65535')


class ActionValidationError(Exception):
    """探针/参数/动作构造不合法。"""


@dataclass(frozen=True)
class StructuredAction:
    """参与 digest 的不可变动作快照。

    target_id / system_user_id 只是引用；凭据内容永不进入本对象。
    """

    kind: str
    target_id: int
    system_user_id: int
    parameters: Dict[str, Any] = field(default_factory=dict)
    working_directory: str = ''
    timeout_seconds: int = 60
    step_id: str = ''

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            'action_version': ACTION_VERSION,
            'kind': self.kind,
            'target_id': int(self.target_id),
            'system_user_id': int(self.system_user_id),
            'parameters': _normalize_params(self.parameters),
            'working_directory': str(self.working_directory or ''),
            'timeout_seconds': int(self.timeout_seconds),
            'step_id': str(self.step_id),
        }


def list_probe_ids():
    """服务端自有探针 ID 列表（只读，供 API 展示与测试）。"""
    return sorted(_PROBES)


def probe_spec(probe_id: str) -> Dict[str, Any]:
    spec = _PROBES.get(str(probe_id or ''))
    if spec is None:
        raise ActionValidationError('unknown probe: %r' % (probe_id,))
    return spec


def _normalize_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """规范化参数：字符串化 + 排序，保证 digest 输入稳定。"""
    normalized = {}
    for key in sorted(params or {}):
        value = params[key]
        normalized[str(key)] = str(value)
    return normalized


def validate_probe(probe_id: str, params: Dict[str, Any]) -> Dict[str, str]:
    """校验探针 ID 与结构化参数，返回规范化参数。

    - 未知探针直接拒绝；
    - 参数集合必须与探针声明完全一致（不允许多余参数，防止注入
      伪装成合法探针的额外字段）；
    - 每个参数值必须匹配探针的白名单正则，且不含任何 Shell 元字符；
    - 探针可选声明 check 钩子，做正则表达不了的语义复核（路径根
      白名单、端口范围等）。
    """
    spec = probe_spec(probe_id)
    declared = spec['params']
    params = params or {}
    unknown = set(params) - set(declared)
    if unknown:
        raise ActionValidationError(
            'unexpected parameters: %s' % ', '.join(sorted(unknown))
        )
    missing = set(declared) - set(params)
    if missing:
        raise ActionValidationError(
            'missing parameters: %s' % ', '.join(sorted(missing))
        )
    normalized = {}
    for key, pattern in declared.items():
        value = str(params[key])
        if _PARAM_FORBIDDEN_RE.search(value):
            raise ActionValidationError(
                'parameter %r contains shell metacharacters' % (key,)
            )
        if not pattern.match(value):
            raise ActionValidationError(
                'parameter %r does not match the probe whitelist' % (key,)
            )
        normalized[key] = value
    check = spec.get('check')
    if check is not None:
        check(normalized)
    return normalized


def build_probe_command(probe_id: str, params: Dict[str, Any]) -> str:
    """由服务端模板构造最终命令。

    模板与参数都来自服务端白名单；构造结果再做一次元字符自检，
    双重防御模板维护失误。模板中的字面花括号（如 curl 的
    %{http_code}）必须写成双花括号转义，避免被 format 误解析。
    """
    spec = probe_spec(probe_id)
    normalized = validate_probe(probe_id, params)
    template = spec['command']
    if normalized:
        # format 会把双花括号转义还原为字面花括号。
        command = template.format(**normalized)
    else:
        command = template.replace('{{', '{').replace('}}', '}')
    if _PARAM_FORBIDDEN_RE.search(command):
        raise ActionValidationError('constructed command failed safety guard')
    return command


def redacted_summary(action: StructuredAction) -> str:
    """生成不含凭据的动作摘要（供快照与列表展示）。"""
    params = _normalize_params(action.parameters)
    param_text = ' '.join('%s=%s' % (k, v) for k, v in params.items())
    summary = '%s %s' % (action.kind, param_text)
    # 控制字符清洗，防止 ANSI/换行注入 UI 与日志。
    return re.sub(r'[\x00-\x1f\x7f]', '', summary).strip()[:255]


def _digest_key(secret_key: str) -> bytes:
    base = str(secret_key or '').encode('utf-8')
    return hashlib.sha256(b'ogs.ai.autonomy.digest.v1:' + base).digest()


def build_action_digest(action: StructuredAction, secret_key: str) -> str:
    """对规范化动作做 HMAC-SHA256 签名。"""
    payload = json.dumps(
        action.to_canonical_dict(), sort_keys=True,
        separators=(',', ':'), ensure_ascii=True,
    ).encode('utf-8')
    return hmac.new(_digest_key(secret_key), payload, hashlib.sha256).hexdigest()


def verify_action_digest(
    action: StructuredAction, digest: str, secret_key: str,
) -> bool:
    if not digest:
        return False
    expected = build_action_digest(action, secret_key)
    return hmac.compare_digest(expected, str(digest))


def action_from_dict(data: Dict[str, Any]) -> StructuredAction:
    """从落库快照重建动作（审批复核用）。缺字段即视为篡改。"""
    try:
        return StructuredAction(
            kind=str(data['kind']),
            target_id=int(data['target_id']),
            system_user_id=int(data['system_user_id']),
            parameters=dict(data.get('parameters') or {}),
            working_directory=str(data.get('working_directory') or ''),
            timeout_seconds=int(data.get('timeout_seconds') or 60),
            step_id=str(data.get('step_id') or ''),
        )
    except (KeyError, TypeError, ValueError):
        raise ActionValidationError('malformed action snapshot') from None
