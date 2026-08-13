# -*- coding: utf-8 -*-
"""Linux shell acceptance for server-owned bounded file actions.

The tests run in an unprivileged user/mount namespace with a private tmpfs on
``/opt``.  They exercise the generated command through POSIX ``dash`` rather
than reimplementing its checks in Python.
"""
import os
import shutil
import subprocess

import pytest

from app.ai.autonomy.actions import (
    build_file_patch_command,
    build_file_restore_command,
    build_probe_command,
    patch_backup_path,
)


pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("unshare") is None
    or shutil.which("dash") is None,
    reason="requires Linux unshare and dash",
)

RUN_ID = "run" + "2" * 29
STEP_ID = "step" + "3" * 28
TARGET = "/opt/ogs-boundary/app.conf"


def _namespace_available():
    result = subprocess.run(
        ["unshare", "-Urm", "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _run_in_private_opt(script, *commands, env=None):
    if not _namespace_available():
        pytest.skip("unprivileged user/mount namespaces are unavailable")
    setup = "\n".join((
        "set -eu",
        "mount -t tmpfs tmpfs /opt",
        script,
    ))
    return subprocess.run(
        ["unshare", "-Urm", "dash", "-c", setup, "ogs-test", *commands],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _commands(content="after\n"):
    backup = patch_backup_path(TARGET, RUN_ID, STEP_ID)
    return (
        build_probe_command(
            "file.read_bounded", {"lines": "5", "path": TARGET},
        ),
        build_file_patch_command(TARGET, content, backup),
        build_file_restore_command(TARGET, backup),
    )


def test_dash_executes_read_patch_and_restore_through_verified_fds():
    read, patch, restore = _commands()
    result = _run_in_private_opt(
        "\n".join((
            "mkdir -p /opt/ogs-boundary",
            "printf 'before\\n' > /opt/ogs-boundary/app.conf",
            "sh -c \"$1\"",
            "test \"$(cat /opt/ogs-boundary/app.conf)\" = after",
            "test \"$(sh -c \"$2\")\" = after",
            "printf 'changed\\n' > /opt/ogs-boundary/app.conf",
            "sh -c \"$3\"",
            "test \"$(cat /opt/ogs-boundary/app.conf)\" = before",
        )),
        patch, read, restore,
    )
    assert result.returncode == 0, result.stderr
    assert "--- %s.before" % TARGET in result.stdout
    assert "+++ %s.after" % TARGET in result.stdout
    assert "-before" in result.stdout
    assert "+after" in result.stdout


@pytest.mark.parametrize("escape", ["final", "intermediate", "backup"])
def test_dash_rejects_symlink_escape_without_touching_outside(
    tmp_path, escape,
):
    _, patch, _ = _commands()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.conf"
    outside_file.write_text("secret\n", encoding="utf-8")

    arrange = {
        "final": "\n".join((
            "mkdir -p /opt/ogs-boundary",
            "ln -s \"$OUTSIDE_FILE\" /opt/ogs-boundary/app.conf",
        )),
        "intermediate": "\n".join((
            "ln -s \"$OUTSIDE_DIR\" /opt/ogs-boundary",
            "printf 'victim\\n' > \"$OUTSIDE_DIR/app.conf\"",
        )),
        "backup": "\n".join((
            "mkdir -p /opt/ogs-boundary",
            "printf 'before\\n' > /opt/ogs-boundary/app.conf",
            "ln -s \"$OUTSIDE_DIR\" "
            "/opt/ogs-boundary/.ogs-autonomy-backup",
        )),
    }[escape]
    result = _run_in_private_opt(
        "\n".join((
            arrange,
            "if sh -c \"$1\"; then exit 99; fi",
        )),
        patch,
        env=dict(
            os.environ,
            OUTSIDE_DIR=str(outside_dir),
            OUTSIDE_FILE=str(outside_file),
        ),
    )
    assert result.returncode == 0, result.stderr
    assert outside_file.read_text(encoding="utf-8") == "secret\n"
    assert not (outside_dir / ".ogs-autonomy-backup").exists()


def test_atomic_replace_does_not_follow_target_swapped_after_fd_check(
    tmp_path,
):
    _, patch, _ = _commands("safe replacement\n")
    outside = tmp_path / "outside.conf"
    outside.write_text("outside\n", encoding="utf-8")
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "readlink"
    wrapper.write_text(
        """#!/bin/sh
set -eu
last=
for arg in "$@"; do last=$arg; done
result=$(/usr/bin/readlink "$@")
if [ "$last" = /proc/self/fd/3 ]; then
    ln -sfn "$OUTSIDE" "$TARGET"
fi
printf %s "$result"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = _run_in_private_opt(
        "\n".join((
            "mkdir -p /opt/ogs-boundary",
            "printf 'before\\n' > /opt/ogs-boundary/app.conf",
            "sh -c \"$1\"",
            "test \"$(cat /opt/ogs-boundary/app.conf)\" = 'safe replacement'",
        )),
        patch,
        env=dict(
            os.environ,
            PATH="%s:%s" % (wrapper_dir, os.environ.get("PATH", "")),
            OUTSIDE=str(outside),
            TARGET=TARGET,
        ),
    )
    assert result.returncode == 0, result.stderr
    assert outside.read_text(encoding="utf-8") == "outside\n"
