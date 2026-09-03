"""Role-aware deterministic tools exposed to the LLM."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from app.ai.storage import AgentStore


MAX_QUERY_ROWS = 200
MAX_PREVIEW_ROWS = 10


class ToolError(RuntimeError):
    pass


class ToolNotAllowed(ToolError):
    pass


class ToolValidationError(ToolError):
    pass


@dataclass
class ToolData:
    kind: str
    rows: List[Dict[str, Any]]
    resource_ids: List[Any]
    summary: Dict[str, Any]
    filters: Optional[Dict[str, Any]] = None


def _tool(name: str, description: str, properties: Dict[str, Any], required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required or []),
                "additionalProperties": False,
            },
        },
    }


TOOL_DEFINITIONS = {
    "get_platform_overview": _tool(
        "get_platform_overview",
        "获取当前用户有权查看的 OrangeServer 平台资产与任务概览。",
        {},
    ),
    "search_assets": _tool(
        "search_assets",
        "按资产组、主机名、IP、在线状态或 SSH 配置状态查询当前用户有权限的主机。",
        {
            "group": {"type": "string"},
            "alias": {"type": "string"},
            "ip": {"type": "string"},
            "online": {"type": "boolean"},
            "configured": {"type": "boolean"},
        },
    ),
    "search_cron_jobs": _tool(
        "search_cron_jobs",
        "查询当前用户可见的定时任务。",
        {
            "name": {"type": "string"},
            "status": {"type": "string"},
        },
    ),
    "list_authorized_system_users": _tool(
        "list_authorized_system_users",
        "列出当前用户有权用于远程命令的系统用户 ID 与别名，不返回任何密码或密钥；"
        "只读诊断必须传回其中的 ID。",
        {},
    ),
    "search_accounts": _tool(
        "search_accounts",
        "管理员查询账号与用户组元数据。",
        {
            "name": {"type": "string"},
            "role": {"type": "string"},
            "group": {"type": "string"},
        },
    ),
    "search_audit_logs": _tool(
        "search_audit_logs",
        "管理员查询最近的登录、命令或操作审计。",
        {
            "log_type": {
                "type": "string",
                "enum": ["login", "command", "operation"],
            },
            "username": {"type": "string"},
            "status": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        required=["log_type"],
    ),
    "run_diagnostic": _tool(
        "run_diagnostic",
        "对最多 10 台已有资产查询结果运行服务端固定的只读诊断档案。"
        "不能提交 Shell 命令；返回结论必须引用 evidence_ids。",
        {
            "profile_id": {
                "type": "string",
                "enum": [
                    "system_baseline", "cpu_load", "memory_pressure",
                    "disk_usage", "process_snapshot", "port_status",
                    "service_status", "system_logs", "docker_health",
                    "docker_logs",
                ],
            },
            "result_set_id": {"type": "string"},
            "system_user_id": {"type": "integer", "minimum": 1},
            "parameters": {
                "type": "object",
                "properties": {
                    "log_lines": {
                        "type": "integer",
                        "enum": [50, 100, 200],
                    },
                    "container_name": {
                        "type": "string",
                        "pattern": "^[A-Za-z0-9_.@-]{1,64}$",
                    },
                    "port": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 65535,
                    },
                },
                "additionalProperties": False,
            },
        },
        required=["profile_id", "result_set_id", "system_user_id"],
    ),
    "create_autonomy_draft": _tool(
        "create_autonomy_draft",
        "创建 AI 自治任务草稿（仅落库，绝不执行）。创建后由用户在自治任务"
        "工作台打开并自行决定是否启动；聊天侧不能启动、批准或取消。"
        "host_id 与 system_user_id 必须来自已有查询工具返回的授权 ID。"
        "审批档案由用户在当前对话中选择，模型不能设置或更改。",
        {
            "goal": {"type": "string", "minLength": 1, "maxLength": 512},
            "host_id": {"type": "integer", "minimum": 1},
            "system_user_id": {"type": "integer", "minimum": 1},
        },
        required=["goal", "host_id", "system_user_id"],
    ),
    "search_knowledge": _tool(
        "search_knowledge",
        "检索管理员审核的 Runbook 与已独立验证历史任务；结果仅作诊断参考，"
        "不能授权动作或证明当前主机状态。检索主机限定知识时传入授权"
        "host_id 或精确 host_alias；服务端会重新校验资产权限。",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 512},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
            "host_id": {"type": "integer", "minimum": 1},
            "host_alias": {"type": "string", "minLength": 1, "maxLength": 25},
        },
        required=["query"],
    ),
}


ADMIN_ONLY_TOOLS = frozenset({
    "search_accounts", "search_audit_logs",
})


class ToolRegistry:
    def __init__(
        self,
        *,
        store: AgentStore,
        platform: "PlatformQueryService",
        owner: str,
        role: str,
        conversation_id: str,
        autonomy_mode: str = "ask",
        autonomy_profile: Optional[Dict[str, Any]] = None,
        command_checker: Optional[Callable[[str], Optional[str]]] = None,
        diagnostic_executor: Optional[
            Callable[[Dict[str, Any]], Dict[str, Any]]
        ] = None,
    ):
        self.store = store
        self.platform = platform
        self.owner = owner
        self.role = role
        self.conversation_id = conversation_id
        from app.ai.autonomy.repository import (
            AutonomyValidationError,
            parse_custom_profile,
        )
        from app.ai.autonomy.state import (
            AutonomyStateError,
            CANONICAL_RUN_MODES,
            RunMode,
            normalize_run_mode,
        )
        try:
            mode = normalize_run_mode(autonomy_mode or RunMode.ASK.value)
            if mode not in CANONICAL_RUN_MODES:
                raise AutonomyStateError("non-canonical conversation mode")
            profile = (
                parse_custom_profile(autonomy_profile)
                if mode == RunMode.CUSTOM else None
            )
        except (AutonomyStateError, AutonomyValidationError):
            mode, profile = RunMode.ASK, None
        self.autonomy_mode = mode.value
        self.autonomy_profile = profile
        self.command_checker = command_checker or self._default_command_checker
        self.diagnostic_executor = diagnostic_executor

    @staticmethod
    def _default_command_checker(command: str) -> Optional[str]:
        from app.tools.shellcmd import _check_dangerous_command
        return _check_dangerous_command(command)

    def definitions(self) -> List[Dict[str, Any]]:
        return [
            definition
            for name, definition in TOOL_DEFINITIONS.items()
            if self.role == "admin" or name not in ADMIN_ONLY_TOOLS
        ]

    def execute(self, name: str, arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        arguments = dict(arguments or {})
        if name not in TOOL_DEFINITIONS:
            raise ToolNotAllowed(f"unknown tool: {name}")
        if name in ADMIN_ONLY_TOOLS and self.role != "admin":
            raise ToolNotAllowed(f"tool requires admin role: {name}")
        if name == "run_diagnostic":
            return self._run_diagnostic(arguments)
        if name == "create_autonomy_draft":
            return self._create_autonomy_draft(arguments)
        if name == "search_knowledge":
            return self._search_knowledge(arguments)

        method = getattr(self.platform, name, None)
        if method is None:
            raise ToolNotAllowed(f"tool is not implemented: {name}")
        data = method(arguments)
        if not isinstance(data, ToolData):
            raise ToolError(f"tool returned invalid data: {name}")
        result_set = self.store.create_result_set(
            self.owner,
            self.conversation_id,
            data.kind,
            rows=data.rows,
            resource_ids=data.resource_ids,
            filters=data.filters or arguments,
            summary=data.summary,
        )
        conversation = self.store.get_conversation(self.owner, self.conversation_id)
        conversation.setdefault("state", {})["last_result_set_id"] = result_set["id"]
        conversation["state"]["last_result_kind"] = data.kind
        self.store.save_conversation(self.owner, conversation)
        return {
            "result_set_id": result_set["id"],
            "kind": data.kind,
            "summary": data.summary,
            "preview": data.rows[:MAX_PREVIEW_ROWS],
            "truncated": len(data.rows) > MAX_PREVIEW_ROWS,
        }

    def _search_knowledge(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from app.ai.knowledge import KnowledgeError, KnowledgeService
        from app.core.db.database import db

        scopes = ("global",)
        if arguments.get("host_id") is not None and arguments.get("host_alias"):
            raise ToolValidationError("use either host_id or host_alias")
        if arguments.get("host_alias"):
            alias = str(arguments["host_alias"]).strip()
            data = self.platform.search_assets({"alias": alias})
            matches = [row for row in data.rows if row.get("alias") == alias]
            if len(matches) != 1:
                raise ToolValidationError("host_alias is not authorized or unique")
            arguments["host_id"] = matches[0]["id"]
        if arguments.get("host_id") is not None:
            try:
                host_id = int(arguments["host_id"])
            except (TypeError, ValueError) as exc:
                raise ToolValidationError("host_id must be an integer") from exc
            if host_id <= 0 or not self.platform.validate_asset_ids([host_id]):
                raise ToolValidationError("host_id is not authorized")
            scopes = ("global", f"host:{host_id}")
        try:
            items = KnowledgeService(db.session).search(
                arguments.get("query"), limit=arguments.get("limit", 8),
                scopes=scopes,
            )
        except (KnowledgeError, TypeError, ValueError) as exc:
            raise ToolValidationError(str(exc)) from exc
        return {"knowledge_references": items, "count": len(items)}

    def _create_autonomy_draft(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """M1/S3 切片 7：聊天侧仅创建自治草稿。

        安全边界：只调用 repository.create_run（落 draft），
        聊天工具面不存在任何 start/approve/cancel 自治 Run 的能力。
        """
        from app.ai.autonomy.repository import (
            AutonomyConflict,
            AutonomyRepository,
            AutonomyValidationError,
            AutonomyPermissionError,
        )
        from app.core.config import AI_AUTONOMY_ENABLED, FLASK_SECRET_KEY
        from app.core.db.database import db

        if not AI_AUTONOMY_ENABLED:
            raise ToolNotAllowed("autonomy feature is disabled")
        if {"mode", "autonomy_mode", "autonomy_profile", "profile"} & set(arguments):
            raise ToolValidationError(
                "autonomy permission is selected by the user for this conversation"
            )
        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            raise ToolValidationError("missing required fields: goal")
        try:
            host_id = int(arguments.get("host_id"))
            system_user_id = int(arguments.get("system_user_id"))
        except (TypeError, ValueError):
            raise ToolValidationError(
                "host_id and system_user_id must be integers"
            ) from None
        try:
            run = AutonomyRepository(db.session, FLASK_SECRET_KEY).create_run(
                self.owner,
                self.role,
                goal=goal,
                host_id=host_id,
                system_user_id=system_user_id,
                mode=self.autonomy_mode,
                budget_payload=None,
                profile_payload=self.autonomy_profile,
                trigger_type="chat",
                trigger_summary="AI chat draft",
            )
        except AutonomyValidationError as exc:
            raise ToolValidationError(str(exc)) from exc
        except AutonomyConflict as exc:
            # 单活唯一冲突（含草稿）等领域冲突不能重试；给模型可读
            # 的明确文案，避免盲目重试撞穿工具步数上限。
            raise ToolValidationError(
                "无法创建自治任务草稿：该主机已有活动自治任务（含草稿），"
                "请先在自治任务工作台处理已有任务。detail: %s" % exc
            ) from exc
        except AutonomyPermissionError as exc:
            raise ToolValidationError(
                "无法创建自治任务草稿：主机或系统账号授权校验失败。"
                "detail: %s" % exc
            ) from exc
        except Exception as exc:
            db.session.rollback()
            raise ToolError("autonomy draft creation failed") from exc
        draft = {
            "run_id": run["id"],
            "goal": run["goal"],
            "status": run["status"],
            "mode": run["mode"],
            "host_alias": run.get("host_alias") or "",
        }
        if run["mode"] == "custom":
            profile = run.get("custom_profile") or self.autonomy_profile or {}
            draft["action_categories"] = list(
                profile.get("action_categories") or []
            )
        return {"autonomy_draft": draft}

    def _run_diagnostic(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        required = ("profile_id", "result_set_id", "system_user_id")
        missing = [
            name for name in required
            if not str(arguments.get(name) or "").strip()
        ]
        if missing:
            raise ToolValidationError(
                "missing required fields: " + ", ".join(missing)
            )
        if self.diagnostic_executor is None:
            raise ToolNotAllowed("diagnostic service is unavailable")
        try:
            return self.diagnostic_executor(arguments)
        except ToolError:
            raise
        except Exception as exc:
            from app.ai.diagnostics import DiagnosticError

            if isinstance(exc, DiagnosticError):
                raise ToolValidationError(str(exc)) from exc
            raise


class PlatformQueryService:
    """Thin, request-independent queries over existing OrangeServer models."""

    def __init__(self, owner: str, role: str, session=None):
        self.owner = owner
        self.role = role
        self.session = session

    def _query(self, model):
        return self.session.query(model) if self.session is not None else model.query

    def _active_auth_ids(self, auth_ids: Iterable[int]) -> Set[int]:
        from app.core.db.database import t_auth_host

        if not auth_ids:
            return set()
        return {
            int(row.id)
            for row in self._query(t_auth_host).filter(
                t_auth_host.id.in_(auth_ids),
                t_auth_host.is_deleted.is_(False),
            ).all()
        }

    def _allowed_groups(self) -> Set[str]:
        from app.core.db.database import (
            t_acc_user,
            t_auth_host_host_group,
            t_auth_host_user,
            t_auth_host_user_group,
            t_group,
        )

        if self.role == "admin":
            return {
                row.name
                for row in self._query(t_group).filter_by(
                    is_deleted=False,
                ).all()
            }
        auth_ids = {
            row.auth_id
            for row in self._query(t_auth_host_user).filter_by(
                user_name=self.owner,
            ).all()
        }
        user = self._query(t_acc_user).filter_by(
            name=self.owner, is_deleted=False,
        ).first()
        if user and user.group:
            auth_ids.update(
                row.auth_id
                for row in self._query(t_auth_host_user_group).filter_by(
                    group_name=user.group,
                ).all()
            )
        auth_ids = self._active_auth_ids(auth_ids)
        if not auth_ids:
            return set()
        return {
            row.group_name
            for row in self._query(t_auth_host_host_group).filter(
                t_auth_host_host_group.auth_id.in_(auth_ids)
            ).all()
        }

    def _authorized_auth_ids(self) -> Set[int]:
        from app.core.db.database import (
            t_acc_user,
            t_auth_host_user,
            t_auth_host_user_group,
        )

        auth_ids = {
            int(row.auth_id)
            for row in self._query(t_auth_host_user).filter_by(
                user_name=self.owner,
            ).all()
        }
        user = self._query(t_acc_user).filter_by(
            name=self.owner, is_deleted=False,
        ).first()
        if user and user.group:
            auth_ids.update(
                int(row.auth_id)
                for row in self._query(t_auth_host_user_group).filter_by(
                    group_name=user.group
                ).all()
            )
        return self._active_auth_ids(auth_ids)

    def authorized_system_user_aliases(self) -> Set[str]:
        from app.core.db.database import (
            t_acc_user,
            t_auth_host_sys_user,
            t_auth_host_user,
            t_auth_host_user_group,
            t_sys_user,
        )

        if self.role == "admin":
            return {
                row.alias
                for row in self._query(t_sys_user).filter_by(
                    is_deleted=False,
                ).all()
            }
        auth_ids = {
            row.auth_id
            for row in self._query(t_auth_host_user).filter_by(
                user_name=self.owner,
            ).all()
        }
        user = self._query(t_acc_user).filter_by(
            name=self.owner, is_deleted=False,
        ).first()
        if user and user.group:
            auth_ids.update(
                row.auth_id
                for row in self._query(t_auth_host_user_group).filter_by(
                    group_name=user.group,
                ).all()
            )
        auth_ids = self._active_auth_ids(auth_ids)
        if not auth_ids:
            return set()
        return {
            row.sys_user_alias
            for row in self._query(t_auth_host_sys_user).filter(
                t_auth_host_sys_user.auth_id.in_(auth_ids)
            ).all()
        }

    def resolve_system_user(self, sys_user_id: int) -> Optional[Dict[str, Any]]:
        from app.core.db.database import t_sys_user

        try:
            credential_id = int(sys_user_id)
        except (TypeError, ValueError):
            return None
        row = self._query(t_sys_user).filter_by(
            id=credential_id,
            is_deleted=False,
        ).first()
        if row is None:
            return None
        if (
            self.role != "admin"
            and row.alias not in self.authorized_system_user_aliases()
        ):
            return None
        return {
            "id": int(row.id),
            "alias": str(row.alias),
            "host_user": str(row.host_user),
        }

    def validate_asset_ids(self, asset_ids: Sequence[int]) -> bool:
        from app.assets.batch_service import (
            BatchOperationValidationError,
            validate_authorized_hosts,
        )

        try:
            validate_authorized_hosts(
                username=self.owner,
                role=self.role,
                host_ids=asset_ids,
                session=self.session,
            )
            return True
        except BatchOperationValidationError:
            return False

    def validate_asset_sys_user_pair(
        self,
        asset_ids: Sequence[int],
        sys_user: str,
    ) -> bool:
        from app.assets.batch_service import (
            BatchOperationValidationError,
            validate_batch_targets,
        )

        try:
            validate_batch_targets(
                username=self.owner,
                role=self.role,
                host_ids=asset_ids,
                sys_user=sys_user,
                session=self.session,
            )
            return True
        except BatchOperationValidationError:
            return False

    def validate_asset_sys_user_id_pair(
        self,
        asset_ids: Sequence[int],
        sys_user_id: int,
    ) -> bool:
        credential = self.resolve_system_user(sys_user_id)
        return bool(
            credential
            and self.validate_asset_sys_user_pair(
                asset_ids,
                str(credential["alias"]),
            )
        )

    def authorized_knowledge_scopes(self) -> tuple[str, ...]:
        """Return server-derived global and currently visible host scopes."""
        from app.core.db.database import t_host

        query = self._query(t_host).filter(t_host.is_deleted.is_(False))
        if self.role != "admin":
            allowed_groups = self._allowed_groups()
            if not allowed_groups:
                return ("global",)
            query = query.filter(t_host.group.in_(allowed_groups))
        host_ids = sorted(int(row.id) for row in query.all())
        return ("global",) + tuple(
            f"host:{host_id}" for host_id in host_ids
        )

    def get_platform_overview(self, _arguments: Dict[str, Any]) -> ToolData:
        from app.core.db.database import t_cron, t_group, t_host
        from app.assets.ServerManagement import get_hosts_online_status

        allowed_groups = self._allowed_groups()
        hosts = (
            t_host.query.filter(
                t_host.group.in_(allowed_groups),
                t_host.is_deleted.is_(False),
            ).all()
            if allowed_groups else []
        )
        cron_query = t_cron.query.filter_by(is_deleted=False)
        if self.role != "admin":
            cron_query = cron_query.filter_by(job_owner=self.owner)
        cron_count = cron_query.count()
        online_statuses = get_hosts_online_status([host.id for host in hosts])
        online_count = sum(
            bool(online_statuses.get(host.id, False))
            for host in hosts
        )
        summary = {
            "host_count": len(hosts),
            "online_count": online_count,
            "offline_count": len(hosts) - online_count,
            "group_count": len(allowed_groups),
            "cron_count": cron_count,
        }
        return ToolData("overview", [summary], [], summary)

    def search_assets(self, arguments: Dict[str, Any]) -> ToolData:
        from app.assets.ServerManagement import (
            _get_configured_groups,
            get_hosts_online_status,
        )
        from app.core.db.database import t_host

        allowed_groups = self._allowed_groups()
        requested_group = str(arguments.get("group") or "").strip()
        if requested_group and requested_group not in allowed_groups:
            raise ToolValidationError("asset group is not authorized")
        query = t_host.query.filter(t_host.is_deleted.is_(False))
        query = query.filter(
            t_host.group.in_([requested_group] if requested_group else allowed_groups)
        )
        alias = str(arguments.get("alias") or "").strip()
        ip = str(arguments.get("ip") or "").strip()
        if alias:
            query = query.filter(t_host.alias.contains(alias))
        if ip:
            query = query.filter(t_host.host_ip.contains(ip))
        configured_groups = _get_configured_groups()
        rows = []
        offset = 0
        while len(rows) < MAX_QUERY_ROWS:
            hosts = (
                query.order_by(t_host.id.asc())
                .offset(offset)
                .limit(MAX_QUERY_ROWS)
                .all()
            )
            if not hosts:
                break
            offset += len(hosts)
            online_statuses = get_hosts_online_status(
                [host.id for host in hosts]
            )
            for host in hosts:
                online = online_statuses.get(host.id, False)
                configured = host.group in configured_groups
                if (
                    "online" in arguments
                    and bool(arguments["online"]) != online
                ):
                    continue
                if (
                    "configured" in arguments
                    and bool(arguments["configured"]) != configured
                ):
                    continue
                rows.append({
                    "id": host.id,
                    "alias": host.alias,
                    "host_ip": host.host_ip,
                    "host_port": host.host_port,
                    "group": host.group,
                    "is_online": online,
                    "configured": configured,
                })
                if len(rows) >= MAX_QUERY_ROWS:
                    break
            if len(hosts) < MAX_QUERY_ROWS:
                break
        summary = {
            "total": len(rows),
            "online": sum(int(row["is_online"]) for row in rows),
            "offline": sum(int(not row["is_online"]) for row in rows),
            "configured": sum(int(row["configured"]) for row in rows),
            "groups": sorted({str(row["group"]) for row in rows}),
        }
        return ToolData(
            "assets", rows, [row["id"] for row in rows], summary, arguments
        )

    def search_cron_jobs(self, arguments: Dict[str, Any]) -> ToolData:
        from app.core.db.database import t_cron, t_cron_group, t_cron_host

        query = t_cron.query.filter_by(is_deleted=False)
        if self.role != "admin":
            query = query.filter_by(job_owner=self.owner)
        name = str(arguments.get("name") or "").strip()
        status = str(arguments.get("status") or "").strip()
        if name:
            query = query.filter(t_cron.job_name.contains(name))
        if status:
            query = query.filter_by(job_status=status)
        jobs = query.order_by(t_cron.id.desc()).limit(MAX_QUERY_ROWS).all()
        rows = []
        for job in jobs:
            rows.append({
                "id": job.id,
                "job_name": job.job_name,
                "job_status": job.job_status,
                "job_owner": job.job_owner,
                "job_command": job.job_command,
                "job_sys_user": job.job_sys_user,
                "job_hosts": [
                    row.host_alias for row in t_cron_host.query.filter_by(cron_id=job.id).all()
                ],
                "job_groups": [
                    row.group_name for row in t_cron_group.query.filter_by(cron_id=job.id).all()
                ],
            })
        return ToolData(
            "cron_jobs",
            rows,
            [row["id"] for row in rows],
            {
                "total": len(rows),
                "enabled": sum(
                    int(str(row["job_status"]).lower() in ("on", "running", "enabled"))
                    for row in rows
                ),
            },
            arguments,
        )

    def list_authorized_system_users(self, _arguments=None) -> ToolData:
        from app.core.db.database import t_sys_user

        aliases = self.authorized_system_user_aliases()
        rows = [
            {
                "id": row.id,
                "alias": row.alias,
                "host_user": row.host_user,
                "agreement": row.agreement,
                "remarks": row.remarks,
            }
            for row in self._query(t_sys_user).filter(
                t_sys_user.alias.in_(aliases),
                t_sys_user.is_deleted.is_(False),
            ).order_by(t_sys_user.alias.asc()).all()
        ] if aliases else []
        return ToolData(
            "system_users", rows, [row["id"] for row in rows], {"total": len(rows)}
        )

    def search_accounts(self, arguments: Dict[str, Any]) -> ToolData:
        if self.role != "admin":
            raise ToolNotAllowed("admin required")
        from app.core.db.database import t_acc_user

        query = t_acc_user.query.filter_by(is_deleted=False)
        name = str(arguments.get("name") or "").strip()
        role = str(arguments.get("role") or "").strip()
        group = str(arguments.get("group") or "").strip()
        if name:
            query = query.filter(t_acc_user.name.contains(name))
        if role:
            query = query.filter_by(usrole=role)
        if group:
            query = query.filter_by(group=group)
        rows = [
            {
                "id": row.id,
                "name": row.name,
                "alias": row.alias,
                "role": row.usrole,
                "group": row.group,
                "remarks": row.remarks,
            }
            for row in query.order_by(t_acc_user.id.asc()).limit(MAX_QUERY_ROWS).all()
        ]
        return ToolData(
            "accounts", rows, [row["id"] for row in rows], {"total": len(rows)}, arguments
        )

    def search_audit_logs(self, arguments: Dict[str, Any]) -> ToolData:
        if self.role != "admin":
            raise ToolNotAllowed("admin required")
        from app.core.db.database import t_command_log, t_cz_log, t_login_log

        log_type = str(arguments.get("log_type") or "")
        models = {
            "login": t_login_log,
            "command": t_command_log,
            "operation": t_cz_log,
        }
        model = models.get(log_type)
        if model is None:
            raise ToolValidationError("invalid log_type")
        query = model.query
        username = str(arguments.get("username") or "").strip()
        status = str(arguments.get("status") or "").strip()
        if username:
            query = query.filter_by(log_name=username)
        if status:
            query = query.filter_by(log_status=status)
        limit = min(50, max(1, int(arguments.get("limit") or 20)))
        rows = []
        for row in query.order_by(model.log_time.desc()).limit(limit).all():
            item = {
                "id": row.id,
                "username": row.log_name,
                "status": row.log_status,
                "time": row.log_time.isoformat() if row.log_time else None,
            }
            if log_type == "login":
                item.update({"ip": row.log_nw_ip, "reason": row.log_reason})
            elif log_type == "command":
                item.update({
                    "command": row.log_info,
                    "host": row.log_host,
                    "reason": row.log_reason,
                })
            else:
                item.update({
                    "operation": row.log_info,
                    "details": row.log_details,
                    "reason": row.log_reason,
                })
            rows.append(item)
        return ToolData(
            "audit_logs", rows, [row["id"] for row in rows],
            {"total": len(rows), "log_type": log_type}, arguments,
        )
