"""Request-independent SSH batch command service shared by UI and AI Agent."""
from __future__ import annotations

import io
import shlex
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

import paramiko

from app.core.db.database import (
    t_acc_user,
    t_auth_host,
    t_auth_host_host_group,
    t_auth_host_sys_user,
    t_auth_host_user,
    t_auth_host_user_group,
    t_host,
    t_sys_user,
)
from app.tools.audlog import ComToolsLog
from app.tools.shellcmd import (
    DangerousCommandError,
    SshCommandTimeout,
    _check_dangerous_command,
    get_ssh_connection,
    get_ssh_connection_by_id,
)


MAX_BATCH_COUNT = 50
MAX_SCRIPT_SIZE = 1 * 1024 * 1024
ALLOWED_SCRIPT_INTERPRETERS = {
    ".sh": "bash",
    ".py": "python3",
}
DANGEROUS_SCRIPT_PATTERNS = (
    "rm -rf /",
    "rm -rf /*",
    ":(){:|:&};:",
    "mkfs",
    "dd if=",
    "shutdown",
    "reboot",
    "chmod -R 777 /",
    "chown -R",
)


class BatchOperationValidationError(RuntimeError):
    pass


BatchCommandValidationError = BatchOperationValidationError


def _normalize_host_ids(host_ids: Iterable[int]) -> List[int]:
    try:
        normalized_ids = [int(item) for item in host_ids]
    except (TypeError, ValueError) as exc:
        raise BatchOperationValidationError("invalid target hosts") from exc
    if not normalized_ids:
        raise BatchOperationValidationError("target hosts are required")
    if len(normalized_ids) > MAX_BATCH_COUNT:
        raise BatchOperationValidationError(
            f"too many hosts (max {MAX_BATCH_COUNT})"
        )
    if len(set(normalized_ids)) != len(normalized_ids):
        raise BatchOperationValidationError(
            "duplicate target hosts are not allowed"
        )
    return normalized_ids


def _query(model, session=None):
    """Use an injected control session when authorization is rechecked."""
    return session.query(model) if session is not None else model.query


def _active_authorization_ids(username: str, session=None) -> set[int]:
    user = _query(t_acc_user, session).filter_by(
        name=username,
        is_deleted=False,
    ).first()
    if user is None:
        return set()
    auth_ids = {
        int(row.auth_id)
        for row in _query(t_auth_host_user, session).filter_by(
            user_name=username,
        ).all()
    }
    if user.group:
        auth_ids.update(
            int(row.auth_id)
            for row in _query(t_auth_host_user_group, session).filter_by(
                group_name=user.group
            ).all()
        )
    if not auth_ids:
        return set()
    return {
        int(row.id)
        for row in _query(t_auth_host, session).filter(
            t_auth_host.id.in_(auth_ids),
            t_auth_host.is_deleted.is_(False),
        ).all()
    }


def validate_authorized_hosts(
    *,
    username: str,
    role: str,
    host_ids: Iterable[int],
    session=None,
) -> List[Any]:
    """Resolve active hosts and enforce the caller's current asset grants."""
    if str(role or "") not in {"admin", "user"}:
        raise BatchOperationValidationError(
            "batch operation permission denied"
        )
    normalized_ids = _normalize_host_ids(host_ids)
    hosts = _query(t_host, session).filter(
        t_host.id.in_(normalized_ids),
        t_host.is_deleted.is_(False),
    ).all()
    by_id = {int(host.id): host for host in hosts}
    if set(by_id) != set(normalized_ids):
        raise BatchOperationValidationError(
            "one or more hosts no longer exist"
        )
    ordered_hosts = [by_id[host_id] for host_id in normalized_ids]
    if str(role or "") == "admin":
        return ordered_hosts

    active_auth_ids = _active_authorization_ids(username, session)
    if not active_auth_ids:
        raise BatchOperationValidationError(
            "asset and system user permission denied"
        )
    covered_groups = {
        row.group_name
        for row in _query(t_auth_host_host_group, session).filter(
            t_auth_host_host_group.auth_id.in_(active_auth_ids)
        ).all()
        if int(row.auth_id) in active_auth_ids
    }
    if any(host.group not in covered_groups for host in ordered_hosts):
        raise BatchOperationValidationError(
            "asset and system user permission denied"
        )
    return ordered_hosts


def validate_batch_targets(
    *,
    username: str,
    role: str,
    host_ids: Iterable[int],
    sys_user: str,
    session=None,
) -> List[Any]:
    """Resolve active targets and revalidate their credential authorization."""
    if not sys_user:
        raise BatchOperationValidationError("system user is required")
    ordered_hosts = validate_authorized_hosts(
        username=username,
        role=role,
        host_ids=host_ids,
        session=session,
    )
    credential = _query(t_sys_user, session).filter_by(
        alias=sys_user,
        is_deleted=False,
    ).first()
    if credential is None:
        raise BatchOperationValidationError("system user no longer exists")
    if str(role or "") == "admin":
        return ordered_hosts

    active_auth_ids = _active_authorization_ids(username, session)
    credential_auth_ids = {
        int(row.auth_id)
        for row in _query(t_auth_host_sys_user, session).filter(
            t_auth_host_sys_user.auth_id.in_(active_auth_ids),
            t_auth_host_sys_user.sys_user_alias == sys_user,
        ).all()
        if int(row.auth_id) in active_auth_ids
        and row.sys_user_alias == sys_user
    }
    covered_groups = {
        row.group_name
        for row in _query(t_auth_host_host_group, session).filter(
            t_auth_host_host_group.auth_id.in_(credential_auth_ids)
        ).all()
        if int(row.auth_id) in credential_auth_ids
    }
    if not credential_auth_ids or any(
        host.group not in covered_groups for host in ordered_hosts
    ):
        raise BatchOperationValidationError(
            "asset and system user permission denied"
        )
    return ordered_hosts


def validate_script_payload(
    *,
    filename: str,
    script_bytes: bytes,
) -> tuple[str, bytes]:
    """Validate and normalize a user-provided script without remote access."""
    if not script_bytes:
        raise BatchCommandValidationError("script file is empty")
    lower_name = str(filename or "").lower()
    extension = next(
        (
            candidate
            for candidate in ALLOWED_SCRIPT_INTERPRETERS
            if lower_name.endswith(candidate)
        ),
        "",
    )
    if not extension:
        raise BatchCommandValidationError(
            "unsupported script type (allowed: .sh, .py)"
        )
    if len(script_bytes) > MAX_SCRIPT_SIZE:
        raise BatchCommandValidationError(
            f"script too large (max {MAX_SCRIPT_SIZE} bytes)"
        )
    try:
        script_text = script_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BatchCommandValidationError("script must be UTF-8 text") from exc
    danger = _check_dangerous_command(script_text)
    if not danger:
        lowered = script_text.lower()
        danger = next(
            (
                pattern
                for pattern in DANGEROUS_SCRIPT_PATTERNS
                if pattern.lower() in lowered
            ),
            None,
        )
    if danger:
        raise BatchCommandValidationError(
            f"script contains dangerous pattern: {danger}"
        )
    normalized = script_text.replace("\r\n", "\n").replace("\r", "\n")
    return extension, normalized.encode("utf-8")


def _write_target_command_audits(
    *,
    username: str,
    log_type: str,
    log_info: str,
    items: List[Dict[str, Any]],
    reason_parts: Iterable[str],
) -> None:
    """Write one execution-log row per target with a valid host reference.

    Production installations can still have the legacy
    ``t_command_log.log_host NOT NULL`` schema, while current schemas use a
    nullable FK to ``t_host.alias``. A NULL aggregate row therefore silently
    disappears on legacy databases. Per-target rows are accurate and valid on
    both schemas.
    """
    base_reason = [str(part) for part in reason_parts if part]
    command_log = ComToolsLog.__new__(ComToolsLog)
    for item in items:
        row_reason = list(base_reason)
        if item.get("status") != "success" and item.get("error"):
            row_reason.append("失败原因: %s" % str(item["error"])[:160])
        command_log.host_log(
            username,
            log_type,
            log_info,
            str(item.get("alias") or "")[:30],
            "成功" if item.get("status") == "success" else "失败",
            "; ".join(row_reason) or None,
        )


def execute_batch_script(
    *,
    username: str,
    role: str,
    host_ids: Iterable[int],
    sys_user: str,
    filename: str,
    script_bytes: bytes,
    connection_factory: Optional[Callable[..., Any]] = None,
    audit_callback: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Execute a validated UTF-8 shell or Python script on authorized hosts."""
    if str(role or "") != "admin":
        raise BatchOperationValidationError(
            "script execution requires admin role"
        )
    hosts = validate_batch_targets(
        username=username,
        role=role,
        host_ids=host_ids,
        sys_user=sys_user,
    )
    extension, normalized_bytes = validate_script_payload(
        filename=filename,
        script_bytes=script_bytes,
    )
    interpreter = ALLOWED_SCRIPT_INTERPRETERS[extension]
    remote_path = "/tmp/orangeserver-script-%s%s" % (
        uuid.uuid4().hex,
        extension,
    )
    safe_path = shlex.quote(remote_path)
    connection_factory = connection_factory or get_ssh_connection
    items: List[Dict[str, Any]] = []

    for host in hosts:
        item = {
            "alias": host.alias,
            "status": "failed",
            "output": "",
            "error": "",
        }
        connection = None
        try:
            connection = connection_factory(
                sys_user,
                host.host_ip,
                int(host.host_port),
            )
            connection.put_fileobj(io.BytesIO(normalized_bytes), remote_path)
            output = connection.ssh_cmd(
                "%s %s" % (interpreter, safe_path),
                audit_callback=audit_callback,
            )
            if output is None:
                item["error"] = str(
                    getattr(connection, "last_command_error", None)
                    or "script failed"
                )[:2048]
            else:
                item["status"] = "success"
                item["output"] = str(output)
        except DangerousCommandError as exc:
            item["error"] = str(exc)
        except SshCommandTimeout:
            item["error"] = "command timeout"
        except paramiko.AuthenticationException:
            item["error"] = "authentication failed"
        except (paramiko.SSHException, OSError, IOError, ValueError) as exc:
            item["error"] = type(exc).__name__
        except Exception as exc:
            item["error"] = type(exc).__name__
        finally:
            if connection is not None:
                try:
                    connection.ssh_cmd(
                        "rm -f -- %s" % safe_path,
                        audit_callback=None,
                    )
                except Exception:
                    pass
                try:
                    connection.close()
                except Exception:
                    pass
        items.append(item)

    success = sum(int(item["status"] == "success") for item in items)
    failed = len(items) - success
    outcome = "success" if failed == 0 else ("failed" if success == 0 else "partial")
    status = (
        "成功"
        if outcome == "success"
        else ("失败" if outcome == "failed" else "部分失败")
    )
    _write_target_command_audits(
        username=username,
        log_type="批量脚本",
        log_info=str(filename or "")[:255],
        items=items,
        reason_parts=["targets=%d" % len(items)],
    )
    return {
        "total": len(items),
        "success": success,
        "failed": failed,
        "outcome": outcome,
        "status": status,
        "items": items,
    }


def execute_batch_upload(
    *,
    username: str,
    role: str,
    host_ids: Iterable[int],
    sys_user: str,
    filename: str,
    file_bytes: bytes,
    connection_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Preserve the legacy put_type=send upload without executing the file."""
    hosts = validate_batch_targets(
        username=username,
        role=role,
        host_ids=host_ids,
        sys_user=sys_user,
    )
    if not filename:
        raise BatchOperationValidationError("file name is required")
    if len(file_bytes) > MAX_SCRIPT_SIZE:
        raise BatchOperationValidationError(
            f"file too large (max {MAX_SCRIPT_SIZE} bytes)"
        )

    remote_path = "/tmp/%s" % filename
    connection_factory = connection_factory or get_ssh_connection
    items: List[Dict[str, Any]] = []
    for host in hosts:
        item = {
            "alias": host.alias,
            "status": "failed",
            "output": "",
            "error": "",
        }
        connection = None
        try:
            connection = connection_factory(
                sys_user,
                host.host_ip,
                int(host.host_port),
            )
            connection.put_fileobj(io.BytesIO(file_bytes), remote_path)
            item["status"] = "success"
            item["output"] = "上传成功"
        except paramiko.AuthenticationException:
            item["error"] = "authentication failed"
        except (paramiko.SSHException, OSError, IOError, ValueError) as exc:
            item["error"] = type(exc).__name__
        except Exception as exc:
            item["error"] = type(exc).__name__
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        items.append(item)

    success = sum(int(item["status"] == "success") for item in items)
    failed = len(items) - success
    status = "成功" if failed == 0 else ("失败" if success == 0 else "部分失败")
    failed_aliases = [
        item["alias"] for item in items if item["status"] != "success"
    ]
    upload_log = ComToolsLog.__new__(ComToolsLog)
    upload_log.host_log(
        username,
        "批量上传",
        filename[:255],
        items[0]["alias"] if len(items) == 1 else None,
        status,
        (
            "targets=%d; 失败主机: %s" % (len(items), ",".join(failed_aliases))
            if failed_aliases
            else "targets=%d" % len(items)
        ),
    )
    return {
        "total": len(items),
        "success": success,
        "failed": failed,
        "outcome": (
            "success" if failed == 0 else ("failed" if success == 0 else "partial")
        ),
        "status": status,
        "items": items,
    }


def execute_batch_command(
    *,
    username: str,
    host_ids: Iterable[int],
    sys_user: str,
    role: Optional[str] = None,
    sys_user_id: Optional[int] = None,
    command: str,
    audit_source: str = "批量命令页面",
    audit_ref: str = "",
    max_output_chars: Optional[int] = None,
    command_timeout: Optional[int] = None,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    connection_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    if not str(command or "").strip():
        raise BatchCommandValidationError("command is required")
    host_ids = [int(item) for item in host_ids]
    danger = _check_dangerous_command(command)
    if danger:
        raise BatchCommandValidationError(f"dangerous command blocked: {danger}")
    if role is not None:
        hosts = validate_batch_targets(
            username=username,
            role=role,
            host_ids=host_ids,
            sys_user=sys_user,
        )
    else:
        if not host_ids:
            raise BatchCommandValidationError("target hosts are required")
        if len(host_ids) > MAX_BATCH_COUNT:
            raise BatchCommandValidationError(
                f"too many hosts (max {MAX_BATCH_COUNT})"
            )
        if len(set(host_ids)) != len(host_ids):
            raise BatchCommandValidationError(
                "duplicate target hosts are not allowed"
            )
        hosts = t_host.query.filter(
            t_host.id.in_(host_ids),
            t_host.is_deleted.is_(False),
        ).all()
    by_id = {host.id: host for host in hosts}
    if set(by_id) != set(host_ids):
        raise BatchCommandValidationError("one or more hosts no longer exist")

    items: List[Dict[str, Any]] = []
    if connection_factory is None:
        if sys_user_id is None:
            connection_factory = get_ssh_connection
        else:
            credential_id = int(sys_user_id)

            def connection_factory(_alias, host_ip, host_port):
                return get_ssh_connection_by_id(
                    credential_id,
                    host_ip,
                    host_port,
                )

    for host_id in host_ids:
        host = by_id[host_id]
        item = {
            "host_id": host.id,
            "alias": host.alias,
            "host_ip": host.host_ip,
            "status": "failed",
            "output": "",
            "error": "",
        }
        connection = None
        try:
            connection = connection_factory(sys_user, host.host_ip, int(host.host_port))
            # A batch is one user operation. Per-host SSH callbacks would create
            # N unrelated top-level command logs and make the aggregate audit
            # impossible to read, so only the aggregate record below is stored.
            if command_timeout is None:
                output = connection.ssh_cmd(command, audit_callback=None)
            else:
                output = connection.ssh_cmd(
                    command,
                    audit_callback=None,
                    command_timeout=command_timeout,
                )
            if output is None:
                item["error"] = str(
                    getattr(connection, "last_command_error", None)
                    or "command failed"
                )[:2048]
            else:
                item["status"] = "success"
                text = str(output)
                limit = max_output_chars if max_output_chars is not None else len(text)
                item["output"] = text[:max(0, int(limit))]
                item["truncated"] = len(text) > max(0, int(limit))
        except DangerousCommandError as exc:
            item["error"] = str(exc)
        except SshCommandTimeout:
            item["error"] = "command timeout"
        except paramiko.AuthenticationException:
            item["error"] = "authentication failed"
        except (paramiko.SSHException, OSError, IOError, ValueError) as exc:
            item["error"] = type(exc).__name__
        except Exception as exc:
            item["error"] = type(exc).__name__
        finally:
            if connection is not None:
                connection.close()
        items.append(item)
        if on_progress is not None:
            on_progress(dict(item))

    success = sum(int(item["status"] == "success") for item in items)
    failed = len(items) - success
    outcome = "success" if failed == 0 else ("failed" if success == 0 else "partial")
    status = "成功" if outcome == "success" else ("失败" if outcome == "failed" else "部分失败")
    audit_reason = []
    audit_reason.append("targets=%d" % len(items))
    if audit_source:
        audit_reason.append("source=%s" % str(audit_source)[:32])
    if audit_ref:
        audit_reason.append("ref=%s" % str(audit_ref)[:96])
    _write_target_command_audits(
        username=username,
        log_type=(
            "AI 批量命令"
            if audit_source.startswith("AI ")
            else "批量命令"
        ),
        log_info=command,
        items=items,
        reason_parts=audit_reason,
    )
    return {
        "total": len(items),
        "success": success,
        "failed": failed,
        "outcome": outcome,
        "status": status,
        "items": items,
    }
