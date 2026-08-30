# -*- coding: utf-8 -*-
"""M1/S2: 结构化文件补丁与恢复契约测试（Issue #13，切片 8c）。

覆盖 v1 唯一回退承诺的三道防线：
- 备份路径由 path/run/step 确定性派生（不含时钟）：提案期即可写
  入审批 digest，执行期复算一致，恢复动作无需依赖执行期产物；
- 补丁命令结构：整文件备份、可审 unified diff、原子替换——备份或
  diff 失败绝不写入；内容上限、控制字符清洗、单引号安全转义；
- 恢复只接受受管备份目录 + 后缀白名单的备份名；
- 路径白名单（/etc、/opt）、`..` 逐段拒绝、敏感路径永久拒绝；
- 策略层：file_patch/file_restore 永不自动。
"""
import pytest

from app.ai.autonomy.actions import (
    ActionValidationError,
    PATCH_CONTENT_MAX_BYTES,
    StructuredAction,
    build_file_patch_command,
    build_file_restore_command,
    patch_backup_path,
    redacted_summary,
)
from app.ai.autonomy.policy import ApprovalDecision, classify_action

RUN_ID = "run" + "0" * 29
STEP_ID = "step" + "1" * 28


def _action(kind, parameters):
    return StructuredAction(
        kind=kind, target_id=7, system_user_id=19,
        parameters=parameters, step_id=STEP_ID,
    )


# ---------------------------------------------------------------------------
# 确定性备份路径
# ---------------------------------------------------------------------------

def test_backup_path_is_deterministic_and_managed():
    first = patch_backup_path("/etc/app.conf", RUN_ID, STEP_ID)
    second = patch_backup_path("/etc/app.conf", RUN_ID, STEP_ID)
    assert first == second
    assert first.startswith("/etc/.ogs-autonomy-backup/app.conf.ogs-bak-")
    # 不含时钟：同一三元组永远派生同一路径。
    assert len(first.rsplit("-", 1)[-1]) == 12

    # run/step 变化必须派生不同备份，避免跨 Step 覆盖。
    other = patch_backup_path("/etc/app.conf", RUN_ID, "x" * 32)
    assert other != first


# ---------------------------------------------------------------------------
# 补丁命令构造
# ---------------------------------------------------------------------------

def test_patch_command_backs_up_before_writing():
    backup = patch_backup_path("/etc/app.conf", RUN_ID, STEP_ID)
    command = build_file_patch_command(
        "/etc/app.conf", "workers=4\n", backup,
    )
    # 目标和父目录先 canonical/no-follow 校验并打开 FD；后续备份、
    # 临时文件和原子替换均锚定 FD，不再按可被换成 symlink 的原路径写。
    assert "realpath -e" in command
    assert 'exec 3<"/proc/self/fd/4/$name"' in command
    assert 'exec 4<"$parent"' in command
    assert 'readlink -f -- "/proc/self/fd/3"' in command
    assert 'readlink -f -- "/proc/self/fd/4"' in command
    assert 'mktemp -- "/proc/self/fd/4/' in command
    assert 'diff -u --label "$path.before" --label "$path.after"' in command
    assert command.index("diff -u") < command.index(
        'mv -fT -- "$target_tmp" "/proc/self/fd/4/$name"'
    )
    assert 'mv -fT -- "$target_tmp" "/proc/self/fd/4/$name"' in command
    assert "'workers=4\n'" in command
    assert "> '/etc/app.conf'" not in command


def test_patch_content_is_bounded_and_cleaned():
    backup = patch_backup_path("/etc/app.conf", RUN_ID, STEP_ID)
    oversized = "x" * (PATCH_CONTENT_MAX_BYTES + 1)
    with pytest.raises(ActionValidationError):
        build_file_patch_command("/etc/app.conf", oversized, backup)
    with pytest.raises(ActionValidationError):
        build_file_patch_command("/etc/app.conf", "", backup)

    # ANSI/终端控制字符被剔除（残留的可见文本无害），换行等
    # 文本必需字符保留。
    command = build_file_patch_command(
        "/etc/app.conf", "a\x1b[31mb\nc\t", backup,
    )
    assert "\x1b" not in command
    assert "'a[31mb\nc\t'" in command


@pytest.mark.parametrize('content', [
    'password=hunter2\n',
    'Authorization: Bearer top-secret\n',
    '-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n',
])
def test_patch_content_rejects_credential_like_material(content):
    backup = patch_backup_path("/etc/app.conf", RUN_ID, STEP_ID)
    with pytest.raises(ActionValidationError, match='credential-like'):
        build_file_patch_command("/etc/app.conf", content, backup)


def test_redacted_summary_sanitizes_legacy_patch_content():
    summary = redacted_summary(_action('file_patch', {
        'path': '/etc/app.conf',
        'content': 'password=legacy-secret\nworkers=4',
    }), max_chars=None)

    assert 'legacy-secret' not in summary
    assert 'password=[REDACTED]' in summary
    assert 'workers=4' in summary


def test_patch_content_cannot_break_shell_quoting():
    backup = patch_backup_path("/etc/app.conf", RUN_ID, STEP_ID)
    # 字面安全的引号逃逸内容照常作为文本写入。
    tame = "msg='it''s fine'\n"
    command = build_file_patch_command("/etc/app.conf", tame, backup)
    assert "'\''" in command
    # 命中永久拒绝清单的内容（如根目录宽删）在构造层直接拒绝，
    # 绝不落到远端。
    hostile = "line'; rm -rf / #\n"
    with pytest.raises(ActionValidationError):
        build_file_patch_command("/etc/app.conf", hostile, backup)


def test_patch_path_is_whitelisted():
    backup = patch_backup_path("/etc/app.conf", RUN_ID, STEP_ID)
    for bad_path in (
        "/home/user/app.conf",       # 白名单根之外
        "/etc/../home/x.conf",       # 逐段拒绝 `..`
        "/etc/shadow",               # 敏感路径
        "etc/app.conf",              # 非绝对路径
        "/etc/app.conf$(reboot)",    # 元字符
    ):
        with pytest.raises(ActionValidationError):
            build_file_patch_command(bad_path, "content", backup)


# ---------------------------------------------------------------------------
# 恢复命令构造：只接受受管备份
# ---------------------------------------------------------------------------

def test_restore_command_copies_managed_backup_back():
    backup = patch_backup_path("/etc/app.conf", RUN_ID, STEP_ID)
    command = build_file_restore_command("/etc/app.conf", backup)
    assert "realpath -e" in command
    assert 'exec 4<"$parent"' in command
    assert 'exec 6<"/proc/self/fd/5/$backup_name"' in command
    assert 'readlink -f -- "/proc/self/fd/6"' in command
    assert 'mv -fT -- "$target_tmp" "/proc/self/fd/4/$name"' in command
    assert "cp -p -- '%s' '/etc/app.conf'" % backup not in command


def test_restore_rejects_unmanaged_backup_names():
    for bad_backup in (
        "/tmp/app.conf.ogs-bak-aaaaaaaaaaaa",        # 目录不受管
        "/etc/.ogs-autonomy-backup/../shadow.bak",   # 目录逃逸
        "/etc/.ogs-autonomy-backup/plain.txt",       # 后缀不合法
        "/etc/.ogs-autonomy-backup/x.ogs-bak-ZZZ",   # token 非十六进制
    ):
        with pytest.raises(ActionValidationError):
            build_file_restore_command("/etc/app.conf", bad_backup)


# ---------------------------------------------------------------------------
# 策略层：永不自动
# ---------------------------------------------------------------------------

def test_policy_file_patch_never_automatic():
    decision, reason = classify_action(
        "assisted", _action("file_patch", {
            "path": "/etc/app.conf", "content": "workers=4",
        }), "lab",
    )
    assert decision == ApprovalDecision.APPROVAL_REQUIRED

    decision, reason = classify_action(
        "read_only", _action("file_patch", {
            "path": "/etc/app.conf", "content": "workers=4",
        }), "lab",
    )
    assert decision == ApprovalDecision.DENIED


def test_policy_file_restore_never_automatic_and_sensitive_denied():
    decision, reason = classify_action(
        "assisted", _action("file_restore", {
            "path": "/etc/app.conf", "backup_path": "/etc/x",
        }), "lab",
    )
    assert decision == ApprovalDecision.APPROVAL_REQUIRED

    decision, reason = classify_action(
        "assisted", _action("file_restore", {
            "path": "/etc/shadow", "backup_path": "/etc/x",
        }), "lab",
    )
    assert decision == ApprovalDecision.DENIED
    assert "sensitive" in reason
