import os
import re
import shutil
import subprocess
import time

import pytest

from app.ai.autonomy.ssh_runner import (
    TERMINATION_AUTHORIZATION_REVOKED,
    TERMINATION_CANCELLED,
    TERMINATION_EXITED,
    TERMINATION_STOP_UNCONFIRMED,
    TERMINATION_TIMED_OUT,
    _build_remote_command,
    _build_stop_command,
    _parse_handshake_line,
    _shell_quote,
    run_ssh_command,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeChannel:
    def __init__(
        self,
        *,
        stdout=(),
        stderr=(),
        exit_code=0,
        never_exit=False,
        recv_error=None,
        exec_error=None,
        handshake_pgid=4242,
    ):
        self.stdout = list(stdout)
        self.stderr = list(stderr)
        self.exit_code = exit_code
        self.never_exit = never_exit
        self.recv_error = recv_error
        self.exec_error = exec_error
        self.handshake_pgid = handshake_pgid
        self.commands = []
        self.closed = False
        self.stdout_reads = 0
        self.stderr_reads = 0

    def exec_command(self, command):
        self.commands.append(command)
        if self.exec_error is not None:
            raise self.exec_error
        match = re.search(r"OGS_AUTONOMY_PGID:([A-Za-z0-9_-]+):", command)
        if match and self.handshake_pgid is not None:
            marker = (
                b"\x1eOGS_AUTONOMY_PGID:"
                + match.group(1).encode("ascii")
                + b":"
                + str(self.handshake_pgid).encode("ascii")
                + b"\n"
            )
            self.stderr.insert(0, marker)

    def recv_ready(self):
        return bool(self.stdout)

    def recv(self, _size):
        if self.recv_error is not None:
            raise self.recv_error
        self.stdout_reads += 1
        return self.stdout.pop(0)

    def recv_stderr_ready(self):
        return bool(self.stderr)

    def recv_stderr(self, _size):
        self.stderr_reads += 1
        return self.stderr.pop(0)

    def exit_status_ready(self):
        return not self.never_exit and not self.stdout and not self.stderr

    def recv_exit_status(self):
        return self.exit_code

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, *channels):
        self.channels = list(channels)
        self.opened = []

    def is_active(self):
        return True

    def open_session(self):
        channel = self.channels.pop(0)
        self.opened.append(channel)
        return channel


class FakeSSHClient:
    def __init__(self, transport):
        self.transport = transport

    def get_transport(self):
        return self.transport


class FakeConnection:
    def __init__(self, transport):
        self.ssh = FakeSSHClient(transport)
        self.closed = False

    def close(self):
        self.closed = True


def make_factory(connection, calls):
    def factory(system_user_id, host, port):
        calls.append((system_user_id, host, port))
        return connection

    return factory


def run_with_fakes(primary, *controls, **kwargs):
    calls = []
    connection = FakeConnection(FakeTransport(primary, *controls))
    clock = kwargs.pop("clock", FakeClock())
    result = run_ssh_command(
        kwargs.pop("command", "printf test"),
        host=kwargs.pop("host", "192.0.2.10"),
        port=kwargs.pop("port", 22),
        system_user_id=kwargs.pop("system_user_id", 9),
        timeout_seconds=kwargs.pop("timeout_seconds", 5),
        max_output_bytes=kwargs.pop("max_output_bytes", 4096),
        connection_factory=make_factory(connection, calls),
        clock=clock.monotonic,
        sleeper=clock.sleep,
        poll_interval=kwargs.pop("poll_interval", 0.1),
        token_factory=kwargs.pop("token_factory", lambda _size: "fixedtoken123"),
        **kwargs,
    )
    return result, connection, calls


def test_exact_exit_code_and_both_streams_are_preserved():
    primary = FakeChannel(
        stdout=(b"out-1", b"-out-2"),
        stderr=(b"err-1", b"-err-2"),
        exit_code=37,
    )

    result, connection, calls = run_with_fakes(primary)

    assert result.started is True
    assert result.stop_confirmed is True
    assert result.termination == TERMINATION_EXITED
    assert result.exit_code == 37
    assert result.stdout == "out-1-out-2"
    assert result.stderr == "err-1-err-2"
    assert calls == [(9, "192.0.2.10", 22)]
    assert primary.stdout_reads == 2
    assert primary.stderr_reads == 3
    assert connection.closed is True


def test_shared_utf8_byte_cap_keeps_draining_both_streams():
    primary = FakeChannel(
        stdout=("你好🙂".encode("utf-8"), b"tail"),
        stderr=(b"stderr",),
    )

    result, _, _ = run_with_fakes(primary, max_output_bytes=7)

    assert result.termination == TERMINATION_EXITED
    assert len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) <= 7
    assert result.stdout == "你好"
    assert result.stderr == ""
    assert result.stdout_bytes_seen == len("你好🙂".encode("utf-8")) + 4
    assert result.stderr_bytes_seen == 6
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert primary.stdout == []
    assert primary.stderr == []


def test_working_directory_is_posix_quoted_inside_setsid_wrapper():
    primary = FakeChannel()

    result, _, _ = run_with_fakes(
        primary,
        command="printf '%s' safe",
        working_directory="/opt/app dir/it's",
    )

    assert result.termination == TERMINATION_EXITED
    wrapper = primary.commands[0]
    assert "if setsid --wait true" in wrapper
    assert "setsid --wait sh -c" in wrapper
    assert "setsid sh -c" in wrapper
    assert "OGS_AUTONOMY_PGID:fixedtoken123:%s" in wrapper
    assert "/tmp/.ogs-autonomy-" not in wrapper
    quoted_directory = "'/opt/app dir/it'\"'\"'s'"
    assert _shell_quote("/opt/app dir/it's") == quoted_directory
    assert "opt/app dir/it" in wrapper
    assert "printf" in wrapper
    assert "exec 3<&0" in wrapper
    assert "while IFS= read -r _ogs_guard_line <&3" in wrapper
    assert "exec 3<&-" in wrapper
    assert "/bin/kill -TERM" in wrapper
    assert "/bin/kill -KILL" in wrapper


def test_confirmed_control_stop_reports_cancelled():
    primary = FakeChannel(never_exit=True)
    controller = FakeChannel(exit_code=0)
    probe_calls = {"count": 0}

    def probe():
        probe_calls["count"] += 1
        if probe_calls["count"] >= 3:
            return TERMINATION_CANCELLED
        return None

    result, _, _ = run_with_fakes(
        primary,
        controller,
        control_probe=probe,
    )

    assert result.started is True
    assert result.stop_confirmed is True
    assert result.termination == TERMINATION_CANCELLED
    assert result.control_reason == TERMINATION_CANCELLED
    assert result.exit_code is None
    assert "/bin/kill -TERM" in controller.commands[0]
    assert "/bin/kill -KILL" in controller.commands[0]
    assert "/bin/kill -0" in controller.commands[0]
    assert "_ogs_pgid=4242" in controller.commands[0]
    assert "pidfile" not in controller.commands[0]


def test_unconfirmed_remote_stop_never_reports_cancelled():
    primary = FakeChannel(never_exit=True)
    controller = FakeChannel(exit_code=4)
    probe_calls = {"count": 0}

    def probe():
        probe_calls["count"] += 1
        return TERMINATION_CANCELLED if probe_calls["count"] >= 3 else None

    result, _, _ = run_with_fakes(
        primary,
        controller,
        control_probe=probe,
    )

    assert result.started is True
    assert result.stop_confirmed is False
    assert result.termination == TERMINATION_STOP_UNCONFIRMED
    assert result.termination != TERMINATION_CANCELLED
    assert result.control_reason == TERMINATION_CANCELLED


def test_total_timeout_uses_second_channel_and_requires_confirmation():
    primary = FakeChannel(never_exit=True)
    controller = FakeChannel(exit_code=0)

    result, _, _ = run_with_fakes(
        primary,
        controller,
        timeout_seconds=0.3,
        poll_interval=0.1,
    )

    assert result.started is True
    assert result.stop_confirmed is True
    assert result.termination == TERMINATION_TIMED_OUT
    assert result.control_reason == TERMINATION_TIMED_OUT
    assert result.exit_code is None


def test_control_revocation_before_connect_never_starts_remote_command():
    calls = []

    def forbidden_factory(*args):
        calls.append(args)
        raise AssertionError("connection must not be opened")

    result = run_ssh_command(
        "hostname",
        host="192.0.2.10",
        port=22,
        system_user_id=9,
        timeout_seconds=5,
        max_output_bytes=100,
        control_probe=lambda: TERMINATION_AUTHORIZATION_REVOKED,
        connection_factory=forbidden_factory,
    )

    assert result.started is False
    assert result.stop_confirmed is True
    assert result.termination == TERMINATION_AUTHORIZATION_REVOKED
    assert result.exit_code is None
    assert calls == []


def test_relative_working_directory_is_rejected_before_connect():
    calls = []

    try:
        run_ssh_command(
            "pwd",
            host="192.0.2.10",
            port=22,
            system_user_id=9,
            timeout_seconds=5,
            max_output_bytes=100,
            working_directory="relative/path",
            connection_factory=lambda *args: calls.append(args),
        )
    except ValueError as exc:
        assert str(exc) == "working_directory must be an absolute POSIX path"
    else:
        raise AssertionError("relative working directory should be rejected")

    assert calls == []


def test_connection_time_counts_toward_total_timeout_and_never_starts():
    clock = FakeClock()
    primary = FakeChannel()
    connection = FakeConnection(FakeTransport(primary))

    def slow_factory(_system_user_id, _host, _port):
        clock.sleep(2)
        return connection

    result = run_ssh_command(
        "hostname",
        host="192.0.2.10",
        port=22,
        system_user_id=9,
        timeout_seconds=1,
        max_output_bytes=100,
        connection_factory=slow_factory,
        clock=clock.monotonic,
        sleeper=clock.sleep,
        token_factory=lambda _size: "fixedtoken123",
    )

    assert result.started is False
    assert result.stop_confirmed is True
    assert result.termination == TERMINATION_TIMED_OUT
    assert primary.commands == []


def test_ambiguous_exec_transport_error_requires_stop_confirmation():
    primary = FakeChannel(exec_error=OSError("request interrupted"))
    controller = FakeChannel(exit_code=4)

    result, _, _ = run_with_fakes(primary, controller)

    assert result.started is True
    assert result.stop_confirmed is False
    assert result.termination == TERMINATION_STOP_UNCONFIRMED
    assert result.transport_error == "OSError"


def test_missing_process_group_handshake_cannot_confirm_cancel():
    primary = FakeChannel(never_exit=True, handshake_pgid=None)
    controller = FakeChannel(exit_code=0)
    probe_calls = {"count": 0}

    def probe():
        probe_calls["count"] += 1
        return TERMINATION_CANCELLED if probe_calls["count"] >= 3 else None

    result, _, _ = run_with_fakes(
        primary,
        controller,
        control_probe=probe,
    )

    assert result.stop_confirmed is False
    assert result.termination == TERMINATION_STOP_UNCONFIRMED
    assert controller.commands == []


def test_payload_cannot_replace_in_memory_process_group_handshake():
    spoof = b"\x1eOGS_AUTONOMY_PGID:fixedtoken123:1\n"
    primary = FakeChannel(
        stderr=(spoof,),
        never_exit=True,
        handshake_pgid=4242,
    )
    controller = FakeChannel(exit_code=0)
    probe_calls = {"count": 0}

    def probe():
        probe_calls["count"] += 1
        return TERMINATION_CANCELLED if probe_calls["count"] >= 3 else None

    result, _, _ = run_with_fakes(
        primary,
        controller,
        control_probe=probe,
    )

    assert result.stop_confirmed is True
    assert "_ogs_pgid=4242" in controller.commands[0]
    assert "_ogs_pgid=1" not in controller.commands[0]
    assert "OGS_AUTONOMY_PGID:fixedtoken123:1" in result.stderr


@pytest.mark.parametrize("pgid", [True, 0, 1, -1, 2**31])
def test_stop_command_rejects_unsafe_process_group_ids(pgid):
    with pytest.raises(ValueError):
        _build_stop_command(pgid, 0.1)


def _local_linux_shell(script):
    if os.path.exists("/bin/sh"):
        return ["/bin/sh", "-c", script]
    wsl = shutil.which("wsl.exe")
    if wsl:
        return [wsl, "--exec", "sh", "-c", script]
    pytest.skip("a local Linux /bin/sh is required for process-group smoke")


def test_remote_payload_is_reaped_when_ssh_stdin_closes():
    token = "stdinclosetoken"
    wrapper = _build_remote_command(
        "trap '' TERM; exec sleep 10",
        "/tmp",
        token,
    )
    primary = subprocess.Popen(
        _local_linux_shell(wrapper),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pgid = None
    try:
        marker = primary.stderr.readline()
        pgid = _parse_handshake_line(marker, token)
        assert pgid is not None

        primary.stdin.close()
        primary.stdin = None
        primary.wait(timeout=5)
        primary.communicate(timeout=5)

        deadline = time.monotonic() + 3
        while True:
            absent = subprocess.run(
                _local_linux_shell(
                    "/bin/kill -0 -- -%d >/dev/null 2>&1" % pgid
                ),
                capture_output=True,
                timeout=5,
            )
            if absent.returncode != 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        assert absent.returncode != 0
    finally:
        if primary.poll() is None:
            if pgid is not None:
                subprocess.run(
                    _local_linux_shell(_build_stop_command(pgid, 0.1)),
                    capture_output=True,
                    timeout=8,
                )
            if primary.poll() is None:
                primary.kill()
        primary.communicate(timeout=5)


def test_real_bin_sh_normal_completion_preserves_exact_exit_code():
    token = "normalexittoken"
    wrapper = _build_remote_command(
        "printf normal-output; printf normal-error >&2; exit 37",
        "/tmp",
        token,
    )
    primary = subprocess.Popen(
        _local_linux_shell(wrapper),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    marker = primary.stderr.readline()
    pgid = _parse_handshake_line(marker, token)
    assert pgid is not None

    primary.wait(timeout=5)
    primary.stdin.close()
    primary.stdin = None
    stdout, stderr = primary.communicate(timeout=5)

    assert primary.returncode == 37
    assert stdout == b"normal-output"
    assert stderr == b"normal-error"


def test_real_bin_sh_kill_escalation_confirms_group_is_gone():
    token = "realshelltoken"
    wrapper = _build_remote_command(
        "trap '' TERM; exec sleep 10",
        "/tmp",
        token,
    )
    primary = subprocess.Popen(
        _local_linux_shell(wrapper),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pgid = None
    try:
        marker = primary.stderr.readline()
        pgid = _parse_handshake_line(marker, token)
        assert pgid is not None

        started = time.monotonic()
        controller = subprocess.run(
            _local_linux_shell(_build_stop_command(pgid, 0.2)),
            capture_output=True,
            timeout=8,
        )
        primary.communicate(timeout=8)
        elapsed = time.monotonic() - started

        assert controller.returncode == 0, controller.stderr
        assert primary.returncode != 0
        assert elapsed < 5
        absent = subprocess.run(
            _local_linux_shell(
                "/bin/kill -0 -- -%d >/dev/null 2>&1" % pgid
            ),
            capture_output=True,
            timeout=5,
        )
        assert absent.returncode != 0
    finally:
        if primary.poll() is None:
            if pgid is not None:
                subprocess.run(
                    _local_linux_shell(_build_stop_command(pgid, 0.1)),
                    capture_output=True,
                    timeout=8,
                )
            if primary.poll() is None:
                primary.kill()
            primary.communicate(timeout=5)
