"""Manual AI routes: supports REST verbs, URL params and POST SSE."""
from typing import Any, Callable, cast

from app.ai import views
from app.tools.at import ogs_auth_token, require_role
from app.tools.csrf import csrf_protect


def _secure(view: Callable[..., Any], *roles: str) -> Callable[..., Any]:
    wrapped = require_role(*roles)(view)
    wrapped = ogs_auth_token(wrapped)
    return cast(Callable[..., Any], csrf_protect(wrapped))


def register_ai_routes(app: Any) -> None:
    all_users = ("admin", "user")
    app.add_url_rule(
        "/ai/providers", "ai_providers",
        _secure(views.public_providers, *all_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/stats", "ai_stats",
        _secure(views.ai_stats, "admin", "user", "audit"), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/admin/providers", "ai_admin_providers",
        _secure(views.admin_providers, "admin"), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/admin/providers/<string:code>", "ai_save_provider",
        _secure(views.save_provider, "admin"), methods=["PUT"],
    )
    app.add_url_rule(
        "/ai/admin/providers/<string:code>/test", "ai_test_provider",
        _secure(views.test_provider, "admin"), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/admin/providers/<string:code>/models", "ai_provider_models",
        _secure(views.provider_models, "admin"), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/admin/providers/<string:code>/clear-key", "ai_clear_provider_key",
        _secure(views.clear_provider_key, "admin"), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/conversations", "ai_conversations",
        _secure(views.conversations, *all_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/conversations", "ai_create_conversation",
        _secure(views.create_conversation, *all_users), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/conversations/<string:conversation_id>", "ai_conversation_detail",
        _secure(views.conversation_detail, *all_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/conversations/<string:conversation_id>", "ai_update_conversation",
        _secure(views.update_conversation, *all_users), methods=["PATCH"],
    )
    app.add_url_rule(
        "/ai/conversations/<string:conversation_id>", "ai_delete_conversation",
        _secure(views.delete_conversation, *all_users), methods=["DELETE"],
    )
    app.add_url_rule(
        "/ai/chat", "ai_chat",
        _secure(views.chat, *all_users), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/results/<string:result_set_id>", "ai_result_set_detail",
        _secure(views.result_set_detail, *all_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/diagnostic-profiles", "ai_diagnostic_profiles",
        _secure(views.diagnostic_profiles, *all_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/diagnostics", "ai_create_diagnostic",
        _secure(views.create_diagnostic, *all_users), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/diagnostics/<string:run_id>", "ai_diagnostic_detail",
        _secure(views.diagnostic_detail, *all_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/diagnostics/<string:run_id>/cancel", "ai_cancel_diagnostic",
        _secure(views.cancel_diagnostic, *all_users), methods=["POST"],
    )
    app.add_url_rule(
        "/ai/diagnostics/<string:run_id>/evidence", "ai_diagnostic_evidence",
        _secure(views.diagnostic_evidence, *all_users), methods=["GET"],
    )
    app.add_url_rule(
        "/ai/diagnostics/<string:run_id>/report", "ai_diagnostic_report",
        _secure(views.diagnostic_report, *all_users), methods=["GET"],
    )
