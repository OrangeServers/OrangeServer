"""Cancellable SSH command primitive for the autonomy executor.

This module deliberately does not reuse the legacy ``ssh_cmd`` read loop.  It
does reuse the existing credential-resolving connection factory, while owning
an independent Paramiko channel so stdout and stderr can be drained together
and a second channel can stop the remote process group.

The remote wrapper also treats SSH stdin EOF as loss of its supervising
Worker and reaps the process group before read-only recovery can retry it.

``control_probe`` is server-owned and must return ``None`` to continue or one
of the public control reasons below to stop.  A probe failure is fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import posixpath
import re
import secrets
import time
from typing import Callable, Optional


TERMINATION_EXITED = "exited"
TERMINATION_CANCELLED = "cancelled"
TERMINATION_TIMED_OUT = "timed_out"
TERMINATION_AUTHORIZATION_REVOKED = "authorization_revoked"
TERMINATION_LEASE_LOST = "lease_lost"
TERMINATION_CONTROL_ERROR = "control_error"
TERMINATION_TRANSPORT_ERROR = "transport_error"
TERMINATION_STOP_UNCONFIRMED = "stop_unconfirmed"

_CONTROL_REASONS = {
    TERMINATION_CANCELLED,
    TERMINATION_AUTHORIZATION_REVOKED,
    TERMINATION_LEASE_LOST,
}
_TOKEN_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
_READ_SIZE = 32 * 1024
_DRAIN_ROUNDS_PER_POLL = 16
_HANDSHAKE_PREFIX = b"\x1eOGS_AUTONOMY_PGID:"
_MAX_HANDSHAKE_BYTES = 160
_MAX_LINUX_PID = 2**31 - 1

ControlProbe = Callable[[], Optional[str]]
ConnectionFactory = Callable[[int, str, int], object]


@dataclass(frozen=True)
class SSHExecutionResult:
    """Exact lifecycle and bounded output from one remote command."""

    started: bool
    stop_confirmed: bool
    termination: str
    exit_code: Optional[int]
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes_seen: int = 0
    stderr_bytes_seen: int = 0
    control_reason: Optional[str] = None
    transport_error: Optional[str] = None


class _OutputCollector:
    def __init__(self, max_bytes: int) -> None:
        self._remaining = max(0, int(max_bytes))
        self._stdout = bytearray()
        self._stderr = bytearray()
        self.stdout_bytes_seen = 0
        self.stderr_bytes_seen = 0

    def add_stdout(self, chunk: bytes) -> None:
        self.stdout_bytes_seen += len(chunk)
        self._store(self._stdout, chunk)

    def add_stderr(self, chunk: bytes) -> None:
        self.stderr_bytes_seen += len(chunk)
        self._store(self._stderr, chunk)

    def _store(self, destination: bytearray, chunk: bytes) -> None:
        if self._remaining <= 0:
            return
        stored = chunk[: self._remaining]
        destination.extend(stored)
        self._remaining -= len(stored)

    @staticmethod
    def _decode(raw: bytearray) -> tuple[str, bool]:
        # Dropping only an incomplete final code point keeps the returned text
        # valid UTF-8 and guarantees its encoded size cannot exceed the cap.
        text = bytes(raw).decode("utf-8", errors="ignore")
        return text, len(text.encode("utf-8")) != len(raw)

    def result_fields(self) -> dict:
        stdout, stdout_decode_truncated = self._decode(self._stdout)
        stderr, stderr_decode_truncated = self._decode(self._stderr)
        return {
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": (
                self.stdout_bytes_seen > len(self._stdout)
                or stdout_decode_truncated
            ),
            "stderr_truncated": (
                self.stderr_bytes_seen > len(self._stderr)
                or stderr_decode_truncated
            ),
            "stdout_bytes_seen": self.stdout_bytes_seen,
            "stderr_bytes_seen": self.stderr_bytes_seen,
        }


def _parse_handshake_line(line: bytes, token: str) -> Optional[int]:
    expected = _HANDSHAKE_PREFIX + token.encode("ascii") + b":"
    if not line.startswith(expected) or not line.endswith(b"\n"):
        return None
    raw_pgid = line[len(expected):-1]
    if not raw_pgid.isdigit():
        return None
    pgid = int(raw_pgid)
    if pgid <= 1 or pgid > _MAX_LINUX_PID:
        return None
    return pgid


class _ProcessGroupHandshake:
    """Consume the launcher's first stderr line and retain its PGID locally."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.pgid: Optional[int] = None
        self._pending = bytearray()
        self._complete = False

    def feed(self, chunk: bytes, collector: _OutputCollector) -> None:
        if self._complete:
            collector.add_stderr(chunk)
            return
        self._pending.extend(chunk)
        newline = self._pending.find(b"\n")
        if newline < 0:
            if len(self._pending) > _MAX_HANDSHAKE_BYTES:
                collector.add_stderr(bytes(self._pending))
                self._pending.clear()
                self._complete = True
            return

        line = bytes(self._pending[:newline + 1])
        remainder = bytes(self._pending[newline + 1:])
        self._pending.clear()
        self.pgid = _parse_handshake_line(line, self.token)
        if self.pgid is None:
            collector.add_stderr(line)
        if remainder:
            collector.add_stderr(remainder)
        self._complete = True

    def finish(self, collector: _OutputCollector) -> None:
        if self._pending:
            collector.add_stderr(bytes(self._pending))
            self._pending.clear()
        self._complete = True


def _shell_quote(value: str) -> str:
    """Quote one value for a POSIX shell without interpolation."""

    return "'" + value.replace("'", "'\"'\"'") + "'"


def validate_working_directory(working_directory: str) -> None:
    """Validate the server-approved remote working directory."""

    if not working_directory:
        return
    if (
        "\x00" in working_directory
        or "\n" in working_directory
        or "\r" in working_directory
    ):
        raise ValueError("working_directory contains an invalid control character")
    if not posixpath.isabs(working_directory):
        raise ValueError("working_directory must be an absolute POSIX path")


def _build_remote_command(command: str, working_directory: str, token: str) -> str:
    payload = "exec sh -c " + _shell_quote(command)
    if working_directory:
        # Absolute paths cannot be parsed as shell options, so quoting is
        # sufficient and avoids depending on non-portable ``cd --`` support.
        payload = "cd " + _shell_quote(working_directory) + " && " + payload

    # The guardian owns the SSH channel's stdin while the non-interactive
    # payload receives /dev/null.  A hard Worker/transport loss closes stdin;
    # the guardian then terminates the entire dedicated process group before a
    # read-only recovery may retry it.  It ignores TERM itself so it can still
    # escalate a TERM-resistant payload to KILL.
    guardian = " ".join(
        [
            "(",
            "trap '' HUP INT TERM;",
            "while IFS= read -r _ogs_guard_line <&3; do :; done;",
            (
                '/bin/kill -TERM -- "-$_ogs_pgid" 2>/dev/null '
                "|| exit 0;"
            ),
            "sleep 1;",
            '/bin/kill -KILL -- "-$_ogs_pgid" 2>/dev/null || :',
            ") &",
        ]
    )
    session_payload = "\n".join(
        [
            "set -u",
            "[ -x /bin/kill ] || exit 126",
            "exec 3<&0",
            "_ogs_pgid=$$",
            guardian,
            "_ogs_guard_pid=$!",
            "exec 3<&-",
            (
                "printf '\\036OGS_AUTONOMY_PGID:"
                + token
                + ":%s\\n' \"$_ogs_pgid\" >&2"
            ),
            "(" + payload + ") </dev/null",
            "_ogs_rc=$?",
            '/bin/kill -KILL -- "$_ogs_guard_pid" 2>/dev/null || :',
            'wait "$_ogs_guard_pid" 2>/dev/null || :',
            'exit "$_ogs_rc"',
        ]
    )

    # setsid makes the approved payload the leader of a dedicated process
    # group. --wait preserves the payload's exact exit status even if setsid
    # must fork. The process group identity comes from the in-memory handshake.
    return "; ".join(
        [
            "setsid --wait sh -c " + _shell_quote(session_payload),
            "_ogs_rc=$?",
            'exit "$_ogs_rc"',
        ]
    )


def _build_stop_command(pgid: int, grace_seconds: float) -> str:
    if isinstance(pgid, bool) or not isinstance(pgid, int):
        raise ValueError("pgid must be an integer")
    if pgid <= 1 or pgid > _MAX_LINUX_PID:
        raise ValueError("pgid is outside the safe Linux PID range")
    interval = 0.1
    grace_checks = max(1, int(max(0.0, grace_seconds) / interval))
    return "; ".join(
        [
            "set -u",
            "[ -x /bin/kill ] || exit 10",
            # Validate GNU /bin/kill option parsing before absence can be
            # interpreted as successful termination. Ubuntu's dash builtin is
            # intentionally bypassed because it rejects `kill -- -PGID`.
            '/bin/kill -0 -- "$$" 2>/dev/null || exit 10',
            f"_ogs_pgid={pgid}",
            '[ "$_ogs_pgid" -gt 1 ] || exit 3',
            (
                'if /bin/kill -0 -- "-$_ogs_pgid" 2>/dev/null; then '
                '/bin/kill -TERM -- "-$_ogs_pgid" 2>/dev/null || exit 11; fi'
            ),
            "_ogs_i=0",
            (
                'while /bin/kill -0 -- "-$_ogs_pgid" 2>/dev/null && '
                f'[ "$_ogs_i" -lt {grace_checks} ]; do '
                "sleep 0.1; _ogs_i=$((_ogs_i + 1)); done"
            ),
            (
                'if /bin/kill -0 -- "-$_ogs_pgid" 2>/dev/null; then '
                '/bin/kill -KILL -- "-$_ogs_pgid" 2>/dev/null || exit 12; fi'
            ),
            "_ogs_i=0",
            (
                'while /bin/kill -0 -- "-$_ogs_pgid" 2>/dev/null && '
                '[ "$_ogs_i" -lt 20 ]; do '
                "sleep 0.1; _ogs_i=$((_ogs_i + 1)); done"
            ),
            (
                'if /bin/kill -0 -- "-$_ogs_pgid" 2>/dev/null; '
                'then exit 4; fi'
            ),
            "exit 0",
        ]
    )


def _drain_available(
    channel: object,
    collector: _OutputCollector,
    handshake: Optional[_ProcessGroupHandshake] = None,
) -> bool:
    drained = False
    # Alternate the streams and bound work per poll. A permanently-readable
    # stdout must not starve stderr, timeout checks, or control revocation.
    for _ in range(_DRAIN_ROUNDS_PER_POLL):
        progressed = False
        if channel.recv_ready():
            chunk = channel.recv(_READ_SIZE)
            if chunk:
                collector.add_stdout(chunk)
                drained = True
                progressed = True
        if channel.recv_stderr_ready():
            chunk = channel.recv_stderr(_READ_SIZE)
            if chunk:
                if handshake is None:
                    collector.add_stderr(chunk)
                else:
                    handshake.feed(chunk, collector)
                drained = True
                progressed = True
        if not progressed:
            break
    return drained


def _probe(control_probe: Optional[ControlProbe]) -> Optional[str]:
    if control_probe is None:
        return None
    try:
        reason = control_probe()
    except Exception:
        return TERMINATION_CONTROL_ERROR
    if reason is None:
        return None
    if reason in _CONTROL_REASONS:
        return reason
    return TERMINATION_CONTROL_ERROR


def _stop_remote(
    transport: object,
    pgid: Optional[int],
    *,
    grace_seconds: float,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    poll_interval: float,
) -> bool:
    if pgid is None or pgid <= 1 or pgid > _MAX_LINUX_PID:
        return False
    control_channel = None
    try:
        control_channel = transport.open_session()
        control_channel.exec_command(_build_stop_command(pgid, grace_seconds))
        deadline = clock() + max(3.0, grace_seconds + 3.0)
        discard = _OutputCollector(0)
        while True:
            _drain_available(control_channel, discard)
            if control_channel.exit_status_ready():
                _drain_available(control_channel, discard)
                return control_channel.recv_exit_status() == 0
            now = clock()
            if now >= deadline:
                return False
            sleeper(min(poll_interval, deadline - now))
    except Exception:
        return False
    finally:
        if control_channel is not None:
            try:
                control_channel.close()
            except Exception:
                pass


def _result(
    collector: _OutputCollector,
    *,
    started: bool,
    stop_confirmed: bool,
    termination: str,
    exit_code: Optional[int],
    control_reason: Optional[str] = None,
    transport_error: Optional[str] = None,
    handshake: Optional[_ProcessGroupHandshake] = None,
) -> SSHExecutionResult:
    if handshake is not None:
        handshake.finish(collector)
    return SSHExecutionResult(
        started=started,
        stop_confirmed=stop_confirmed,
        termination=termination,
        exit_code=exit_code,
        control_reason=control_reason,
        transport_error=transport_error,
        **collector.result_fields(),
    )


def run_ssh_command(
    command: str,
    *,
    host: str,
    port: int,
    system_user_id: int,
    timeout_seconds: float,
    max_output_bytes: int,
    working_directory: str = "",
    control_probe: Optional[ControlProbe] = None,
    connection_factory: Optional[ConnectionFactory] = None,
    poll_interval: float = 0.05,
    stop_grace_seconds: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    token_factory: Callable[[int], str] = secrets.token_hex,
) -> SSHExecutionResult:
    """Execute one approved command over a cancellable SSH channel.

    The output budget is shared by stdout and stderr and measured in source
    bytes.  Both streams keep draining after that budget is exhausted.
    ``cancelled`` (and other control terminations) is returned only after the
    second channel confirms that the process group no longer exists.
    """

    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_output_bytes < 0:
        raise ValueError("max_output_bytes cannot be negative")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    validate_working_directory(working_directory)

    collector = _OutputCollector(max_output_bytes)
    initial_reason = _probe(control_probe)
    if initial_reason is not None:
        return _result(
            collector,
            started=False,
            stop_confirmed=True,
            termination=initial_reason,
            exit_code=None,
            control_reason=initial_reason,
        )

    token = token_factory(12)
    if not _TOKEN_PATTERN.fullmatch(token):
        raise ValueError("token_factory returned an unsafe token")
    handshake = _ProcessGroupHandshake(token)

    if connection_factory is None:
        # Lazy import keeps the primitive independently unit-testable while
        # preserving the existing credential-by-reference lookup in production.
        from app.tools.shellcmd import get_ssh_connection_by_id

        connection_factory = get_ssh_connection_by_id

    connection = None
    primary_channel = None
    transport = None
    started = False
    deadline = clock() + timeout_seconds
    try:
        connection = connection_factory(system_user_id, host, int(port))
        ssh_client = getattr(connection, "ssh", None)
        if ssh_client is None:
            raise RuntimeError("connection has no SSH client")
        transport = ssh_client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport is unavailable")
        is_active = getattr(transport, "is_active", None)
        if callable(is_active) and not is_active():
            raise RuntimeError("SSH transport is inactive")

        primary_channel = transport.open_session()
        pre_start_reason = _probe(control_probe)
        if pre_start_reason is not None:
            return _result(
                collector,
                started=False,
                stop_confirmed=True,
                termination=pre_start_reason,
                exit_code=None,
                control_reason=pre_start_reason,
            )
        if clock() >= deadline:
            return _result(
                collector,
                started=False,
                stop_confirmed=True,
                termination=TERMINATION_TIMED_OUT,
                exit_code=None,
                control_reason=TERMINATION_TIMED_OUT,
            )

        # From this point the exec request may reach the server even if the
        # transport raises before acknowledging it. Treat that ambiguity as
        # potentially started and require explicit remote-stop confirmation.
        started = True
        primary_channel.exec_command(
            _build_remote_command(command, working_directory, token)
        )

        while True:
            _drain_available(primary_channel, collector, handshake)
            if primary_channel.exit_status_ready():
                _drain_available(primary_channel, collector, handshake)
                return _result(
                    collector,
                    started=True,
                    stop_confirmed=True,
                    termination=TERMINATION_EXITED,
                    exit_code=primary_channel.recv_exit_status(),
                    handshake=handshake,
                )

            reason = _probe(control_probe)
            now = clock()
            if reason is None and now >= deadline:
                reason = TERMINATION_TIMED_OUT
            if reason is not None:
                confirmed = _stop_remote(
                    transport,
                    handshake.pgid,
                    grace_seconds=stop_grace_seconds,
                    clock=clock,
                    sleeper=sleeper,
                    poll_interval=poll_interval,
                )
                try:
                    _drain_available(primary_channel, collector, handshake)
                except Exception:
                    pass
                return _result(
                    collector,
                    started=True,
                    stop_confirmed=confirmed,
                    termination=(
                        reason if confirmed else TERMINATION_STOP_UNCONFIRMED
                    ),
                    exit_code=None,
                    control_reason=reason,
                    handshake=handshake,
                )

            sleeper(min(poll_interval, max(0.0, deadline - now)))
    except Exception as exc:
        error_label = type(exc).__name__
        if not started:
            return _result(
                collector,
                started=False,
                stop_confirmed=True,
                termination=TERMINATION_TRANSPORT_ERROR,
                exit_code=None,
                transport_error=error_label,
            )

        confirmed = False
        if transport is not None:
            confirmed = _stop_remote(
                transport,
                handshake.pgid,
                grace_seconds=stop_grace_seconds,
                clock=clock,
                sleeper=sleeper,
                poll_interval=poll_interval,
            )
        return _result(
            collector,
            started=True,
            stop_confirmed=confirmed,
            termination=(
                TERMINATION_TRANSPORT_ERROR
                if confirmed
                else TERMINATION_STOP_UNCONFIRMED
            ),
            exit_code=None,
            control_reason=TERMINATION_TRANSPORT_ERROR,
            transport_error=error_label,
            handshake=handshake,
        )
    finally:
        if primary_channel is not None:
            try:
                primary_channel.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
