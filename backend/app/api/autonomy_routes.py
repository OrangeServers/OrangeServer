"""M1/S1 autonomy routes with owner-scoped admin/user access."""
from typing import Any, Callable, cast

from app.ai.autonomy import views
from app.tools.at import ogs_auth_token, require_role
from app.tools.csrf import csrf_protect


def _secure(view: Callable[..., Any], *roles: str) -> Callable[..., Any]:
    wrapped = require_role(*roles)(view)
    wrapped = ogs_auth_token(wrapped)
    return cast(Callable[..., Any], csrf_protect(wrapped))


def register_autonomy_routes(app: Any) -> None:
    """注册自治任务最小 API。

    Run 生命周期与只读数据按当前 owner 对 admin/user 开放；服务端
    自有探针提议、主机环境和知识库管理仍仅管理员。自治功能本身还
    受 OGS_AI_AUTONOMY_ENABLED（默认关闭）二次门控。
    """
    admins = ("admin",)
    run_users = ("admin", "user")
    app.add_url_rule(
        "/ai/autonomy/status", "ai_autonomy_status",
        _secure(views.autonomy_status, "admin", "user"), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/ops/status", "ai_ops_status",
        _secure(views.ops_status, *run_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomy/system-users", "ai_autonomy_system_users",
        _secure(views.system_user_options, *run_users), methods=["GET"],
    )
    # Machine-to-machine endpoint: its own constant-time Bearer check replaces
    # session auth and CSRF; never wrap it with the browser security chain.
    app.add_url_rule(
        "/ai/ops/alertmanager/webhook", "ai_ops_alertmanager_webhook",
        views.alertmanager_webhook, methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs", "ai_autonomy_create_run",
        _secure(views.create_run, *run_users), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs", "ai_autonomy_list_runs",
        _secure(views.list_runs, *run_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>", "ai_autonomy_run_detail",
        _secure(views.run_detail, *run_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/start", "ai_autonomy_run_start",
        _secure(views.start_run, *run_users), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/cancel",
        "ai_autonomy_run_cancel",
        _secure(views.cancel_run, *run_users), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/steps", "ai_autonomy_propose_step",
        _secure(views.propose_step, *admins), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/steps/<string:step_id>/decision",
        "ai_autonomy_step_decision",
        _secure(views.decide_step, *run_users), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/artifacts",
        "ai_autonomy_artifact_list",
        _secure(views.list_artifacts, *run_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/artifacts/"
        "<string:artifact_id>",
        "ai_autonomy_artifact_detail",
        _secure(views.artifact_content, *run_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/evidence",
        "ai_autonomy_evidence_list",
        _secure(views.list_evidence, *run_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/knowledge/config", "ai_knowledge_config",
        _secure(views.knowledge_config, *admins), methods=["GET", "PATCH"],
    )
    app.add_url_rule(
        "/ai/knowledge/documents", "ai_knowledge_documents",
        _secure(views.knowledge_documents, *run_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/knowledge/documents", "ai_knowledge_documents_create",
        _secure(views.knowledge_documents, *admins), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/knowledge/documents/preview", "ai_knowledge_document_preview",
        _secure(views.knowledge_document_preview, *admins), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/knowledge/search", "ai_knowledge_search",
        _secure(views.knowledge_search, *run_users), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/knowledge/documents/<string:document_id>",
        "ai_knowledge_document",
        _secure(views.knowledge_document, *admins),
        methods=["GET", "PATCH", "DELETE"],
    )
    app.add_url_rule(
        "/ai/knowledge/reindex", "ai_knowledge_reindex",
        _secure(views.knowledge_reindex, *admins), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/knowledge",
        "ai_autonomy_knowledge_capture",
        _secure(views.knowledge_capture_run, *admins), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/stream",
        "ai_autonomy_run_stream",
        _secure(views.stream_run, *run_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomy/hosts/<int:host_id>/environment",
        "ai_autonomy_host_environment",
        _secure(views.set_host_environment, *admins), methods=["POST"],
    )
