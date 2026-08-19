"""M1/S1 autonomy routes: REST verbs, admin-only, disabled by default."""
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

    v1 除状态探测外全部仅管理员；功能本身还受
    OGS_AI_AUTONOMY_ENABLED（默认关闭）二次门控。
    """
    admins = ("admin",)
    app.add_url_rule(
        "/ai/autonomy/status", "ai_autonomy_status",
        _secure(views.autonomy_status, "admin", "user"), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs", "ai_autonomy_create_run",
        _secure(views.create_run, *admins), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs", "ai_autonomy_list_runs",
        _secure(views.list_runs, *admins), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>", "ai_autonomy_run_detail",
        _secure(views.run_detail, *admins), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/start", "ai_autonomy_run_start",
        _secure(views.start_run, *admins), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/cancel",
        "ai_autonomy_run_cancel",
        _secure(views.cancel_run, *admins), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/steps", "ai_autonomy_propose_step",
        _secure(views.propose_step, *admins), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/steps/<string:step_id>/decision",
        "ai_autonomy_step_decision",
        _secure(views.decide_step, *admins), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/artifacts",
        "ai_autonomy_artifact_list",
        _secure(views.list_artifacts, *admins), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/artifacts/"
        "<string:artifact_id>",
        "ai_autonomy_artifact_detail",
        _secure(views.artifact_content, *admins), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/evidence",
        "ai_autonomy_evidence_list",
        _secure(views.list_evidence, *admins), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomous-runs/<string:run_id>/stream",
        "ai_autonomy_run_stream",
        _secure(views.stream_run, *admins), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/autonomy/hosts/<int:host_id>/environment",
        "ai_autonomy_host_environment",
        _secure(views.set_host_environment, *admins), methods=["POST"],
    )
