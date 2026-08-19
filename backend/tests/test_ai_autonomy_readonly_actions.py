# -*- coding: utf-8 -*-
"""M1/S2: 只读动作族契约测试（Issue #13，切片 8a）。

覆盖三块契约：
- 有界文件/日志读取：行数上限、根目录白名单、`..` 逐段拒绝、
  敏感路径在参数层永久拒绝；
- 验证探针（端口 / HTTP / 进程 / 服务日志）：命令完全来自服务端
  模板，退出码即验证结论；
- v1 永久拒绝清单：磁盘分区格式化、根目录宽删、主动读秘密、
  横向 SSH、绕过审计——即使命中人工精确审批也不执行。
"""
import pytest

from app.ai.autonomy.actions import (
    ActionValidationError,
    StructuredAction,
    build_probe_command,
    validate_probe,
)
from app.ai.autonomy.policy import (
    ApprovalDecision,
    classify_action,
    classify_shell_command,
    permanent_deny_reason,
)


# ---------------------------------------------------------------------------
# 有界读取：行数上限 + 根目录白名单 + 敏感路径拒绝
# ---------------------------------------------------------------------------

def test_bounded_read_probes_build_server_templates():
    read_command = build_probe_command(
        "file.read_bounded",
        {"lines": "50", "path": "/etc/nginx/nginx.conf"},
    )
    tail_command = build_probe_command(
        "log.tail", {"lines": "100", "path": "/var/log/syslog"},
    )

    for command, reader in (
        (read_command, 'head -n "$lines" <&3'),
        (tail_command, 'tail -n "$lines" <&3'),
    ):
        assert "realpath -e" in command
        assert 'exec 3<"/proc/self/fd/4/$name"' in command
        assert 'readlink -f -- "/proc/self/fd/3"' in command
        assert reader in command
    assert "head -n 50 -- /etc/nginx/nginx.conf" not in read_command
    assert "tail -n 100 -- /var/log/syslog" not in tail_command


@pytest.mark.parametrize("probe_id,path", [
    ("file.read_bounded", "/etc/shadow"),           # 敏感路径
    ("file.read_bounded", "/opt/app/.env"),         # 秘密载体
    ("file.read_bounded", "/home/deploy/.ssh/id_rsa"),
    ("file.read_bounded", "/var/log/../../etc/shadow"),  # 逐段逃逸
    ("file.read_bounded", "/etc/../etc/passwd"),
    ("file.read_bounded", "/home/deploy/data.txt"),  # 根白名单之外
    ("file.read_bounded", "/root/secret.txt"),
    ("log.tail", "/etc/nginx/nginx.conf"),          # 日志只读 /var/log
    ("log.tail", "/opt/app/app.log"),
])
def test_bounded_read_path_escapes_are_rejected(probe_id, path):
    with pytest.raises(ActionValidationError):
        validate_probe(probe_id, {"lines": "100", "path": path})


@pytest.mark.parametrize("lines", ["0", "1000", "-5", "1e2", ""])
def test_bounded_read_line_cap_is_enforced(lines):
    with pytest.raises(ActionValidationError):
        validate_probe(
            "file.read_bounded", {"lines": lines, "path": "/var/log/app.log"},
        )


# ---------------------------------------------------------------------------
# 验证探针：服务端模板 + 白名单参数
# ---------------------------------------------------------------------------

def test_verify_probes_build_server_templates():
    assert build_probe_command(
        "verify.port_open", {"host": "127.0.0.1", "port": "8080"},
        target_host="192.0.2.40",
    ) == "nc -z -w 5 127.0.0.1 8080"
    assert build_probe_command(
        "verify.http_status", {"url": "http://localhost:8080/health"},
        target_host="192.0.2.40",
    ) == (
        "curl --silent --show-error --output /dev/null"
        " --write-out %{http_code} --max-time 10"
        " http://localhost:8080/health"
    )
    assert build_probe_command(
        "verify.process_running", {"process": "nginx"},
    ) == "pgrep -c -x nginx"
    assert build_probe_command(
        "verify.journal_pattern",
        {"unit": "nginx", "lines": "200", "pattern": "error"},
    ) == "journalctl -u nginx -n 200 --no-pager -g error"


def test_network_verify_probe_is_bound_to_run_target_or_remote_loopback():
    target = "192.0.2.40"

    assert build_probe_command(
        "verify.port_open",
        {"host": target, "port": "443"},
        target_host=target,
    ) == "nc -z -w 5 192.0.2.40 443"
    assert build_probe_command(
        "verify.http_status",
        {"url": "http://localhost:8080/health"},
        target_host=target,
    ).endswith(" http://localhost:8080/health")

    with pytest.raises(ActionValidationError, match="run target"):
        build_probe_command(
            "verify.port_open",
            {"host": "198.51.100.8", "port": "443"},
            target_host=target,
        )
    with pytest.raises(ActionValidationError, match="run target"):
        build_probe_command(
            "verify.http_status",
            {"url": "https://metadata.example.com/latest"},
            target_host=target,
        )


@pytest.mark.parametrize("probe_id,params", [
    ("verify.port_open", {"host": "127.0.0.1", "port": "0"}),
    ("verify.port_open", {"host": "127.0.0.1", "port": "65536"}),
    ("verify.port_open", {"host": "evil host", "port": "80"}),
    ("verify.port_open", {"host": "127.0.0.1; rm", "port": "80"}),
    ("verify.http_status", {"url": "ftp://example.com/file"}),
    ("verify.http_status", {"url": "https://example.com/?q=1"}),
    ("verify.http_status", {"url": "https://user:pw@example.com/"}),
    ("verify.process_running", {"process": "nginx worker"}),
    ("verify.process_running", {"process": "x" * 65}),
    ("verify.journal_pattern", {
        "unit": "nginx", "lines": "100", "pattern": "has space",
    }),
    ("verify.journal_pattern", {"unit": "nginx", "lines": "1000",
                                "pattern": "error"}),
])
def test_verify_probe_parameters_outside_whitelist_are_rejected(
    probe_id, params,
):
    with pytest.raises(ActionValidationError):
        validate_probe(probe_id, params)


def test_probe_check_hooks_run_before_digestable_output():
    # check 钩子失败时绝不返回规范化参数。
    with pytest.raises(ActionValidationError):
        validate_probe(
            "verify.port_open", {"host": "db.internal", "port": "70000"},
        )


# ---------------------------------------------------------------------------
# 包查询探针：按包管理器分发，只读
# ---------------------------------------------------------------------------

def test_package_probe_dispatches_by_manager():
    assert build_probe_command(
        "package.installed", {"manager": "apt", "package": "nginx"},
    ) == "dpkg -s nginx"
    assert build_probe_command(
        "package.installed", {"manager": "dnf", "package": "nginx"},
    ) == "rpm -q nginx"


@pytest.mark.parametrize("params", [
    {"manager": "yum", "package": "nginx"},       # 管理器白名单外
    {"manager": "apt", "package": "nginx;rm -rf /"},
    {"manager": "apt", "package": ""},
    {"manager": "apt"},                            # 缺参数
    {"manager": "apt", "package": "nginx", "repo": "evil"},
])
def test_package_probe_parameters_outside_whitelist_are_rejected(params):
    with pytest.raises(ActionValidationError):
        validate_probe("package.installed", params)


# ---------------------------------------------------------------------------
# v1 永久拒绝清单：审批不能推翻
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "mkfs.ext4 /dev/sda1",
    "mkfs /dev/sdb",
    "fdisk /dev/sdb",
    "parted /dev/sda print",
    "wipefs -a /dev/sdb",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    "rm -rf /",
    "rm -rf /*",
    "rm -fr /",
    "rm -r -f /",
    "rm -rf --no-preserve-root /",
    "cat /etc/shadow",
    "head -n 5 /home/deploy/.ssh/id_rsa",
    "cp /etc/ssl/private/server.key /tmp/leak",
    "less /opt/app/.env.production",
    "ssh root@203.0.113.9 reboot",
    "scp /tmp/x root@203.0.113.9:/tmp/",
    "setenforce 0",
    "auditctl -D",
    "systemctl stop auditd",
])
def test_permanent_deny_list_blocks_even_approved_shell(command):
    assert permanent_deny_reason(command) is not None
    decision, reason = classify_shell_command(command)
    assert decision == ApprovalDecision.DENIED
    assert reason


@pytest.mark.parametrize("command", [
    "systemctl restart nginx",
    "ls -la /var/log",
    "tail -n 100 /var/log/app.log",
    "journalctl -u nginx -n 50 --no-pager",
    "rm /tmp/stale-file.txt",
])
def test_benign_commands_are_not_permanently_denied(command):
    assert permanent_deny_reason(command) is None
    decision, _ = classify_shell_command(command)
    assert decision == ApprovalDecision.APPROVAL_REQUIRED


def test_permanent_deny_shell_is_denied_in_every_mode():
    shell = StructuredAction(
        kind="shell", target_id=7, system_user_id=19,
        parameters={"command": "mkfs.ext4 /dev/sda1"},
        timeout_seconds=30, step_id="step-x",
    )
    for mode in ("read_only", "assisted", "lab_autonomous"):
        decision, _ = classify_action(mode, shell, "lab")
        assert decision == ApprovalDecision.DENIED


def test_constructed_probe_commands_stay_off_the_deny_list():
    # 服务端模板构造出的只读命令不得误伤永久拒绝清单。
    commands = [
        build_probe_command("system.load", {}),
        build_probe_command(
            "file.read_bounded", {"lines": "50", "path": "/var/log/app.log"},
        ),
        build_probe_command(
            "log.tail", {"lines": "100", "path": "/var/log/syslog"},
        ),
        build_probe_command(
            "verify.port_open", {"host": "db.internal", "port": "3306"},
            target_host="db.internal",
        ),
        build_probe_command(
            "verify.http_status", {"url": "http://127.0.0.1:8080/health"},
            target_host="192.0.2.40",
        ),
        build_probe_command(
            "verify.process_running", {"process": "nginx"},
        ),
        build_probe_command(
            "verify.journal_pattern",
            {"unit": "nginx", "lines": "200", "pattern": "error"},
        ),
        build_probe_command(
            "package.installed", {"manager": "apt", "package": "nginx"},
        ),
        build_probe_command(
            "package.installed", {"manager": "dnf", "package": "nginx"},
        ),
    ]
    for command in commands:
        assert permanent_deny_reason(command) is None
