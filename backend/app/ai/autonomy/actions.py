# -*- coding: utf-8 -*-
"""M1/S1+S2: 结构化动作 schema、服务端探针/写模板与审批 digest。

锁定的安全契约：
- 自动的只读工作只接受服务端自有探针 ID + 校验过的结构化参数；
  模型或调用方不能把任意 Shell 标记为只读。
- S2 有界读取：文件/日志读取限制行数与根目录白名单，敏感路径
  在参数层即被永久拒绝。
- S2 结构化写动作（systemd 服务操作、包安装、文件补丁与恢复）
  同样只接受服务端模板 + 白名单参数；永远不会自动执行，执行前
  落写意图。文件补丁必须有备份并可恢复（v1 唯一回退承诺）。
- 动作快照在审批前不可变地落库；凭据只以 ID 引用存在，永不进入
  快照、digest、Event 或响应。
- digest 绑定动作版本、目标、凭据引用、工具(kind/probe)、规范化
  参数、工作目录、超时与 Step ID；任一字段变化都会使审批失效。
"""
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict
from urllib.parse import urlsplit


# 动作 schema 版本。digest 与快照都绑定该版本；升级动作结构时必须
# 递增，避免旧审批被复用到新语义。
ACTION_VERSION = 1

# 需要“写意图先落库”的动作类别。shell 是通用写入口（永远精确
# 审批）；systemd/package_install 是服务端模板的结构化写动作；
# file_patch/file_restore 是带备份与恢复承诺的文件写动作。
WRITE_KINDS = frozenset({
    'shell', 'systemd', 'package_install', 'file_patch', 'file_restore',
})

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
            'host': re.compile(r'^[A-Za-z0-9.:-]{1,253}$'),
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
            'url': re.compile(r'^https?://[A-Za-z0-9._:/\[\]-]{1,500}$'),
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
    'package.installed': {
        'title': '包安装状态查询（只读）',
        'selector': 'manager',
        'command': {
            'apt': 'dpkg -s {package}',
            'dnf': 'rpm -q {package}',
        },
        'params': {
            'manager': re.compile(r'^(apt|dnf)$'),
            'package': re.compile(r'^[A-Za-z0-9][A-Za-z0-9+._-]{0,127}$'),
        },
    },
}

# 有界读取的根目录白名单；log.tail 进一步收紧到 /var/log。
# 新增根目录必须过安全评审。
_FILE_READ_ROOTS = ('/var/log', '/etc', '/opt')
_LOG_TAIL_ROOTS = ('/var/log',)

# 文件补丁只开放配置目录（/etc、/opt）；补丁前自动备份到目标
# 文件同目录的受管备份目录，恢复只接受白名单后缀的备份名。
_PATCH_ROOTS = ('/etc', '/opt')
PATCH_BACKUP_DIR = '.ogs-autonomy-backup'
_PATCH_BACKUP_SUFFIX_RE = re.compile(
    r'\.ogs-bak-[0-9a-f]{12}$'
)

# 结构化文件补丁的内容上限（字节）：审批单元保持可审，大文件
# 变更不在 v1 承诺范围。远端备份是原文件的完整副本，不受此限。
PATCH_CONTENT_MAX_BYTES = 8192


def _check_bounded_path(params: Dict[str, str], roots) -> None:
    """有界读取路径复核：逐段拒绝 `..`，限定根目录，敏感路径拒绝。

    这里完成纯参数校验；执行命令还会在远端做 canonical/no-follow
    校验并通过已验证的文件描述符读取，拒绝符号链接逃逸。
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


_REMOTE_LOOPBACK_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})


def _check_network_probe_target(
    probe_id: str,
    params: Dict[str, str],
    target_host: str,
) -> None:
    """Bind network verification to the authoritative Run target.

    The command executes on the target over SSH, so its own loopback is also
    in scope.  Arbitrary model-supplied hosts are never resolved or accepted.
    """
    authoritative = str(target_host or '').strip().strip('[]').lower()
    if not authoritative:
        raise ActionValidationError(
            'network verification requires the current run target'
        )

    if probe_id == 'verify.port_open':
        requested = str(params.get('host') or '').strip().strip('[]').lower()
    else:
        try:
            parsed = urlsplit(str(params.get('url') or ''))
            requested = str(parsed.hostname or '').strip().lower()
            # Force urlsplit to validate a numeric port when one is present.
            parsed.port
        except ValueError:
            raise ActionValidationError(
                'HTTP verification URL is malformed'
            ) from None
        if (
            parsed.scheme not in ('http', 'https')
            or not requested
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ActionValidationError(
                'HTTP verification URL is malformed'
            )

    if requested not in _REMOTE_LOOPBACK_HOSTS and requested != authoritative:
        raise ActionValidationError(
            'network verification host must match the current run target'
        )


# ---------------------------------------------------------------------------
# 结构化写动作：服务端模板 + 白名单参数，永不自动
# ---------------------------------------------------------------------------

# 与探针同构的写模板注册表：命令模板完全由服务端持有。新增写
# 模板必须过安全评审。约束：
# - systemd 只开放 start/stop/restart；status 走只读探针
#   service.status，enable/disable/mask 属高影响变更，v1 不开放；
# - 包安装只能从已配置源安装（模板不携带任何新增源的能力）；
#   新增源属“永远精确审批”类，v1 不做。
_WRITE_TEMPLATES: Dict[str, Dict[str, Any]] = {
    'systemd': {
        'title': 'systemd 服务操作',
        'command': 'systemctl {operation} {unit}',
        'params': {
            'operation': re.compile(r'^(start|stop|restart)$'),
            'unit': re.compile(r'^[A-Za-z0-9@:._-]{1,128}$'),
        },
    },
    'package_install': {
        'title': '包安装（仅限已配置源）',
        'selector': 'manager',
        'command': {
            'apt': (
                'apt-get install --assume-yes'
                ' --no-install-recommends {package}'
            ),
            'dnf': 'dnf install --assumeyes {package}',
        },
        'params': {
            'manager': re.compile(r'^(apt|dnf)$'),
            'package': re.compile(r'^[A-Za-z0-9][A-Za-z0-9+._-]{0,127}$'),
        },
    },
}


def list_write_kinds():
    """结构化写动作 kind 列表（供测试与后续提案入口）。"""
    return sorted(_WRITE_TEMPLATES)


def _write_spec(kind: str) -> Dict[str, Any]:
    spec = _WRITE_TEMPLATES.get(str(kind or ''))
    if spec is None:
        raise ActionValidationError(
            'unknown write action kind: %r' % (kind,)
        )
    return spec


def validate_write_action(kind: str, params: Dict[str, Any]) -> Dict[str, str]:
    """校验结构化写动作参数，返回规范化参数（与探针同构）。"""
    spec = _write_spec(kind)
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
                'parameter %r does not match the whitelist' % (key,)
            )
        normalized[key] = value
    return normalized


def build_write_command(kind: str, params: Dict[str, Any]) -> str:
    """由服务端写模板构造最终命令。

    除探针同构的白名单/元字符自检外，构造结果再过一次永久拒绝
    清单（如 stop auditd 这种绕过审计的服务操作）。
    """
    # 延迟导入：policy 依赖本模块。
    from app.ai.autonomy.policy import permanent_deny_reason

    spec = _write_spec(kind)
    normalized = validate_write_action(kind, params)
    template = spec['command']
    if isinstance(template, dict):
        template = template[normalized[spec['selector']]]
    command = template.format(**normalized)
    if _PARAM_FORBIDDEN_RE.search(command):
        raise ActionValidationError('constructed command failed safety guard')
    deny_reason = permanent_deny_reason(command)
    if deny_reason is not None:
        raise ActionValidationError(
            'constructed command is permanently denied: %s' % (deny_reason,)
        )
    return command


# ---------------------------------------------------------------------------
# 结构化文件补丁：备份 + 写入 + 恢复（v1 唯一回退承诺）
# ---------------------------------------------------------------------------

# 补丁内容的控制字符清洗：保留换行/回车/制表符（文本补丁必需），
# 其余控制字符全部剔除，防止 ANSI/终端注入。
_PATCH_CONTROL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def _shell_single_quote(text: str) -> str:
    """POSIX 单引号安全转义：'→'\\''。"""
    return "'" + str(text).replace("'", "'\\''") + "'"


def _matching_root(path: str, roots) -> str:
    matches = [
        root for root in roots
        if path == root or path.startswith(root + '/')
    ]
    if not matches:
        raise ActionValidationError('path is outside the allowed roots')
    return max(matches, key=len)


def _build_bounded_read_command(
    path: str,
    lines: str,
    roots,
    reader: str,
) -> str:
    """Read via verified descriptors on the remote Linux host."""
    root = _matching_root(path, roots)
    script = '\n'.join((
        'set -eu',
        'path=$1',
        'root=$2',
        'lines=$3',
        'parent=${path%/*}',
        'name=${path##*/}',
        '[ -n "$parent" ] || parent=/',
        'canonical_root=$(realpath -e -- "$root")',
        '[ "$canonical_root" = "$root" ] || exit 64',
        'canonical_parent=$(realpath -e -- "$parent")',
        '[ "$canonical_parent" = "$parent" ] || exit 64',
        'case "$canonical_parent" in',
        '  "$root"|"$root"/*) ;;',
        '  *) exit 64 ;;',
        'esac',
        'canonical_path=$(realpath -e -- "$path")',
        '[ "$canonical_path" = "$path" ] || exit 64',
        '[ -f "$path" ] && [ ! -L "$path" ] || exit 64',
        'exec 4<"$parent"',
        'opened_parent=$(readlink -f -- "/proc/self/fd/4")',
        '[ "$opened_parent" = "$parent" ] || exit 64',
        'exec 3<"/proc/self/fd/4/$name"',
        'opened=$(readlink -f -- "/proc/self/fd/3")',
        '[ "$opened" = "$path" ] || exit 64',
        '%s -n "$lines" <&3' % reader,
    ))
    return 'sh -c %s ogs-bounded-read %s %s %s' % (
        _shell_single_quote(script),
        _shell_single_quote(path),
        _shell_single_quote(root),
        _shell_single_quote(lines),
    )


def patch_backup_path(path: str, run_id: str, step_id: str) -> str:
    """确定性备份路径：同目录受管备份目录 + 原名 + 派生 token。

    token 由 path/run/step 确定性派生，不含时钟：提案时即可写入
    digest，执行期复算一致，恢复动作无需依赖执行期产物。
    """
    token = uuid.uuid5(
        uuid.NAMESPACE_OID, '%s|%s|%s' % (path, run_id, step_id),
    ).hex[:12]
    name = path.rsplit('/', 1)[-1]
    return '%s/%s/%s.ogs-bak-%s' % (
        path.rsplit('/', 1)[0], PATCH_BACKUP_DIR, name, token,
    )


def build_file_patch_command(
    path: str, content: str, backup_path: str,
) -> str:
    """构造补丁命令：备份、生成 unified diff、原子写入新内容。

    备份或 diff 失败都绝不替换目标。本构造器不走通用模板白名单
    （内容天然含元字符），安全性由路径白名单 + 单引号转义 + 永久
    拒绝清单承担。
    """
    from app.ai.autonomy.policy import permanent_deny_reason

    _check_patch_path(path)
    text = _PATCH_CONTROL_RE.sub('', str(content or ''))
    if len(text.encode('utf-8')) > PATCH_CONTENT_MAX_BYTES:
        raise ActionValidationError(
            'patch content exceeds %d bytes' % PATCH_CONTENT_MAX_BYTES
        )
    if not text:
        raise ActionValidationError('patch content is empty')
    backup_dir, backup_name = _check_managed_backup_path(path, backup_path)
    root = _matching_root(path, _PATCH_ROOTS)
    script = '\n'.join((
        'set -eu',
        'path=$1',
        'root=$2',
        'backup_dir=$3',
        'backup_name=$4',
        'content=$5',
        'parent=${path%/*}',
        'name=${path##*/}',
        '[ -n "$parent" ] || parent=/',
        'canonical_root=$(realpath -e -- "$root")',
        '[ "$canonical_root" = "$root" ] || exit 64',
        'canonical_parent=$(realpath -e -- "$parent")',
        '[ "$canonical_parent" = "$parent" ] || exit 64',
        'case "$canonical_parent" in',
        '  "$root"|"$root"/*) ;;',
        '  *) exit 64 ;;',
        'esac',
        'canonical_path=$(realpath -e -- "$path")',
        '[ "$canonical_path" = "$path" ] || exit 64',
        '[ -f "$path" ] && [ ! -L "$path" ] || exit 64',
        'exec 4<"$parent"',
        'opened_parent=$(readlink -f -- "/proc/self/fd/4")',
        '[ "$opened_parent" = "$parent" ] || exit 64',
        'exec 3<"/proc/self/fd/4/$name"',
        'opened=$(readlink -f -- "/proc/self/fd/3")',
        '[ "$opened" = "$path" ] || exit 64',
        '[ ! -L "/proc/self/fd/4/%s" ] || exit 64' % PATCH_BACKUP_DIR,
        'mkdir -p -- "/proc/self/fd/4/%s"' % PATCH_BACKUP_DIR,
        'canonical_backup=$(realpath -e -- "/proc/self/fd/4/%s")'
        % PATCH_BACKUP_DIR,
        '[ "$canonical_backup" = "$backup_dir" ] || exit 64',
        'exec 5<"/proc/self/fd/4/%s"' % PATCH_BACKUP_DIR,
        'opened_backup=$(readlink -f -- "/proc/self/fd/5")',
        '[ "$opened_backup" = "$backup_dir" ] || exit 64',
        'backup_tmp=$(mktemp -- "/proc/self/fd/5/.ogs-backup.XXXXXX")',
        'target_tmp=$(mktemp -- "/proc/self/fd/4/.ogs-patch.XXXXXX")',
        "trap 'rm -f -- \"$backup_tmp\" \"$target_tmp\"' EXIT HUP INT TERM",
        'cp -p -- "/proc/self/fd/3" "$backup_tmp"',
        'mv -fT -- "$backup_tmp" "/proc/self/fd/5/$backup_name"',
        'cp -p -- "/proc/self/fd/3" "$target_tmp"',
        'printf %s "$content" >"$target_tmp"',
        'diff_rc=0',
        'diff -u --label "$path.before" --label "$path.after" '
        '"/proc/self/fd/3" "$target_tmp" || diff_rc=$?',
        '[ "$diff_rc" -le 1 ] || exit 65',
        'mv -fT -- "$target_tmp" "/proc/self/fd/4/$name"',
        'trap - EXIT HUP INT TERM',
    ))
    command = 'sh -c %s ogs-file-patch %s %s %s %s %s' % (
        _shell_single_quote(script),
        _shell_single_quote(path),
        _shell_single_quote(root),
        _shell_single_quote(backup_dir),
        _shell_single_quote(backup_name),
        _shell_single_quote(text),
    )
    deny_reason = permanent_deny_reason(command)
    if deny_reason is not None:
        raise ActionValidationError(
            'constructed command is permanently denied: %s' % (deny_reason,)
        )
    return command


def build_file_restore_command(path: str, backup_path: str) -> str:
    """构造恢复命令：把受管备份复制回目标路径（保留权限）。"""
    from app.ai.autonomy.policy import permanent_deny_reason

    _check_patch_path(path)
    backup_dir, backup_name = _check_managed_backup_path(path, backup_path)
    root = _matching_root(path, _PATCH_ROOTS)
    script = '\n'.join((
        'set -eu',
        'path=$1',
        'root=$2',
        'backup_dir=$3',
        'backup_name=$4',
        'parent=${path%/*}',
        'name=${path##*/}',
        '[ -n "$parent" ] || parent=/',
        'canonical_root=$(realpath -e -- "$root")',
        '[ "$canonical_root" = "$root" ] || exit 64',
        'canonical_parent=$(realpath -e -- "$parent")',
        '[ "$canonical_parent" = "$parent" ] || exit 64',
        'case "$canonical_parent" in',
        '  "$root"|"$root"/*) ;;',
        '  *) exit 64 ;;',
        'esac',
        'canonical_path=$(realpath -e -- "$path")',
        '[ "$canonical_path" = "$path" ] || exit 64',
        '[ -f "$path" ] && [ ! -L "$path" ] || exit 64',
        'exec 4<"$parent"',
        'opened_parent=$(readlink -f -- "/proc/self/fd/4")',
        '[ "$opened_parent" = "$parent" ] || exit 64',
        'exec 3<"/proc/self/fd/4/$name"',
        'opened=$(readlink -f -- "/proc/self/fd/3")',
        '[ "$opened" = "$path" ] || exit 64',
        '[ -d "/proc/self/fd/4/%s" ]' % PATCH_BACKUP_DIR,
        '[ ! -L "/proc/self/fd/4/%s" ]' % PATCH_BACKUP_DIR,
        'exec 5<"/proc/self/fd/4/%s"' % PATCH_BACKUP_DIR,
        'opened_backup=$(readlink -f -- "/proc/self/fd/5")',
        '[ "$opened_backup" = "$backup_dir" ] || exit 64',
        'exec 6<"/proc/self/fd/5/$backup_name"',
        'opened_backup_file=$(readlink -f -- "/proc/self/fd/6")',
        '[ "$opened_backup_file" = "$backup_dir/$backup_name" ] || exit 64',
        'target_tmp=$(mktemp -- "/proc/self/fd/4/.ogs-restore.XXXXXX")',
        "trap 'rm -f -- \"$target_tmp\"' EXIT HUP INT TERM",
        'cp -p -- "/proc/self/fd/6" "$target_tmp"',
        'mv -fT -- "$target_tmp" "/proc/self/fd/4/$name"',
        'trap - EXIT HUP INT TERM',
    ))
    command = 'sh -c %s ogs-file-restore %s %s %s %s' % (
        _shell_single_quote(script),
        _shell_single_quote(path),
        _shell_single_quote(root),
        _shell_single_quote(backup_dir),
        _shell_single_quote(backup_name),
    )
    deny_reason = permanent_deny_reason(command)
    if deny_reason is not None:
        raise ActionValidationError(
            'constructed command is permanently denied: %s' % (deny_reason,)
        )
    return command


def _check_managed_backup_path(path: str, backup_path: str):
    expected_dir = '%s/%s' % (path.rsplit('/', 1)[0], PATCH_BACKUP_DIR)
    backup_dir = backup_path.rsplit('/', 1)[0]
    if backup_dir != expected_dir:
        raise ActionValidationError(
            'backup must live in the managed backup directory'
        )
    name = backup_path.rsplit('/', 1)[-1]
    if '/' in name or not _PATCH_BACKUP_SUFFIX_RE.search(name):
        raise ActionValidationError(
            'backup name does not match the managed suffix whitelist'
        )
    return backup_dir, name


def _check_patch_path(path: str) -> None:
    """补丁路径复核：白名单根 + 逐段拒绝 `..` + 敏感路径拒绝。"""
    from app.ai.autonomy.policy import is_sensitive_path

    normalized = str(path or '')
    if not re.match(r'^/[A-Za-z0-9._/-]{1,255}$', normalized):
        raise ActionValidationError('patch path is malformed')
    parts = [part for part in normalized.split('/') if part]
    if '..' in parts or '.' in parts:
        raise ActionValidationError('path traversal is not allowed')
    if not any(
        normalized == root or normalized.startswith(root + '/')
        for root in _PATCH_ROOTS
    ):
        raise ActionValidationError(
            'patch path is outside the allowed roots'
        )
    if is_sensitive_path(normalized):
        raise ActionValidationError(
            'sensitive path is denied by server policy'
        )


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


def build_probe_command(
    probe_id: str,
    params: Dict[str, Any],
    *,
    target_host: str = '',
) -> str:
    """由服务端模板构造最终命令。

    模板与参数都来自服务端白名单；构造结果再做一次元字符自检，
    双重防御模板维护失误。模板中的字面花括号（如 curl 的
    %{http_code}）必须写成双花括号转义，避免被 format 误解析。
    """
    spec = probe_spec(probe_id)
    normalized = validate_probe(probe_id, params)
    if probe_id in ('verify.port_open', 'verify.http_status'):
        _check_network_probe_target(probe_id, normalized, target_host)
    if probe_id in ('file.read_bounded', 'log.tail'):
        return _build_bounded_read_command(
            normalized['path'],
            normalized['lines'],
            _FILE_READ_ROOTS if probe_id == 'file.read_bounded'
            else _LOG_TAIL_ROOTS,
            'head' if probe_id == 'file.read_bounded' else 'tail',
        )
    template = spec['command']
    if isinstance(template, dict):
        # 与写模板同构的 selector 分发（如包查询按管理器选命令）。
        template = template[normalized[spec['selector']]]
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
    return hmac.new(
        _digest_key(secret_key), payload, hashlib.sha256,
    ).hexdigest()


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
        if int(data['action_version']) != ACTION_VERSION:
            raise ActionValidationError('unsupported action snapshot version')
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
