"""HTTP views for provider settings, conversations and Agent SSE."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from flask import Response, current_app, request, stream_with_context
from sqlalchemy import func

from app.ai.provider_config import ProviderConfigError, ProviderConfigService
from app.ai.runner import AgentRunner, sse_event
from app.ai.storage import (
    AgentStore,
    AgentStoreConflict,
    AgentStoreError,
    AgentStoreNotFound,
)
from app.core.db.database import db, t_command_log
from app.tools.apierr import ApiCode, api_error, api_response
from app.tools.at import get_current_user, get_current_user_role


logger = logging.getLogger(__name__)


def _identity():
    redis_holder, owner = get_current_user()
    return redis_holder, owner, str(get_current_user_role() or "")


def _payload() -> Dict[str, Any]:
    value = request.get_json(silent=True)
    if isinstance(value, dict):
        return value
    return request.form.to_dict(flat=True)


def _ok(**data):
    response, _status = api_response(**data)
    return response


def _error(message: str, status: int = 400):
    code = ApiCode.INTERNAL_ERROR if status >= 500 else ApiCode.TYPE_ERROR
    return api_error(code, message, status=status)


def _iso(value):
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _conversation_summary(row):
    return {
        **row,
        "autonomy_mode": str(row.get("autonomy_mode") or "ask"),
        "autonomy_profile": row.get("autonomy_profile"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _conversation_autonomy_permission(payload):
    from app.ai.autonomy.repository import (
        AutonomyValidationError,
        parse_custom_profile,
    )
    from app.ai.autonomy.state import (
        AutonomyStateError,
        RunMode,
        normalize_run_mode,
    )

    if "autonomy_mode" not in payload:
        raw_mode = "ask"
    else:
        raw_value = payload.get("autonomy_mode")
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise AutonomyValidationError(
                "autonomy_mode must be ask, ai_review, auto, or custom"
            )
        raw_mode = raw_value.strip()
    try:
        mode = normalize_run_mode(raw_mode)
    except AutonomyStateError as exc:
        raise AutonomyValidationError(str(exc)) from exc
    allowed = {RunMode.ASK, RunMode.AI_REVIEW, RunMode.AUTO, RunMode.CUSTOM}
    if mode not in allowed or mode.value != raw_mode:
        raise AutonomyValidationError("unsupported conversation autonomy mode")
    profile_payload = payload.get("autonomy_profile")
    if mode == RunMode.CUSTOM:
        profile = parse_custom_profile(profile_payload)
    else:
        if profile_payload is not None:
            raise AutonomyValidationError(
                "autonomy profile is only valid with mode=custom"
            )
        profile = None
    return mode.value, profile


def _project_tool_events(events):
    projected: Dict[str, Dict[str, Any]] = {}
    order = []
    for index, event in enumerate(events or []):
        if event.get("type") not in ("tool.started", "tool.completed"):
            continue
        event_id = str(event.get("tool_call_id") or event.get("id") or "")
        key = event_id or f"event:{index}"
        current = {
            **event,
            "created_at": _iso(event.get("created_at")),
        }
        existing = projected.get(key)
        if existing is None:
            projected[key] = current
            order.append(key)
            continue
        if (
            existing.get("type") == "tool.completed"
            and current.get("type") == "tool.started"
        ):
            continue
        started_at = existing.get("created_at")
        projected[key] = {
            **existing,
            **current,
            "created_at": started_at or current.get("created_at"),
        }
    return [projected[key] for key in order]


def _project_autonomy_drafts(events):
    """M1/S3 切片 7：投影聊天侧创建的自治任务草稿引用卡（历史恢复用）。"""
    drafts = []
    for event in events or []:
        if event.get("type") != "autonomy.draft_created":
            continue
        draft = {
            "id": str(event.get("id") or ""),
            "run_id": str(event.get("run_id") or ""),
            "goal": str(event.get("goal") or ""),
            "status": str(event.get("status") or "draft"),
            "mode": str(event.get("mode") or ""),
            "host_alias": str(event.get("host_alias") or ""),
            "created_at": _iso(event.get("created_at")),
        }
        if isinstance(event.get("action_categories"), list):
            draft["action_categories"] = [
                str(category)
                for category in event["action_categories"]
                if category
            ]
        drafts.append(draft)
    return drafts


def _project_provider_observability(state):
    source = (state or {}).get("provider_observability") or {}
    usage_source = source.get("usage") or {}
    usage = {
        key: max(0, int(usage_source.get(key) or 0))
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if usage_source.get(key) is not None
    }
    budget_source = source.get("context_budget") or {}
    budget = {
        key: max(0, int(budget_source.get(key) or 0))
        for key in (
            "context_window_tokens",
            "output_reserve_tokens",
            "safety_reserve_tokens",
            "runtime_reserve_tokens",
            "effective_input_tokens",
            "estimated_input_tokens",
        )
        if budget_source.get(key) is not None
    }
    result = {
        "usage": usage,
        "last_finish_reason": str(
            source.get("last_finish_reason") or "unknown"
        )[:32],
        "last_latency_ms": max(
            0, int(source.get("last_latency_ms") or 0)
        ),
        "compression_count": max(
            0, int(source.get("compression_count") or 0)
        ),
        "context_budget": budget,
    }
    if source.get("truncation_reason"):
        result["truncation_reason"] = str(
            source["truncation_reason"]
        )[:32]
    compression = source.get("last_compression")
    if isinstance(compression, dict):
        compression_usage = compression.get("usage") or {}
        result["last_compression"] = {
            "usage": {
                key: max(0, int(compression_usage.get(key) or 0))
                for key in (
                    "prompt_tokens", "completion_tokens", "total_tokens"
                )
                if compression_usage.get(key) is not None
            },
            "finish_reason": str(
                compression.get("finish_reason") or "unknown"
            )[:32],
            "latency_ms": max(
                0, int(compression.get("latency_ms") or 0)
            ),
            "truncated": bool(compression.get("truncated")),
        }
    return result


def _store() -> AgentStore:
    redis_holder, _owner, _role = _identity()
    return AgentStore(redis_holder.conn)


def _diagnostic_service():
    from app.ai.diagnostics import DiagnosticService

    return DiagnosticService(agent_store=_store())


def diagnostic_profiles():
    from app.ai.diagnostic_profiles import list_profiles

    profiles = list_profiles()
    return _ok(profiles=profiles, data=profiles)


def create_diagnostic():
    from app.ai.diagnostics import (
        DiagnosticError,
        DiagnosticValidationError,
    )

    _holder, owner, role = _identity()
    try:
        run = _diagnostic_service().start(
            owner=owner, role=role, payload=_payload()
        )
        return _ok(run=run, data=run)
    except DiagnosticValidationError as exc:
        db.session.rollback()
        return _error(str(exc))
    except DiagnosticError as exc:
        db.session.rollback()
        return _error(str(exc), 409)
    except Exception:
        db.session.rollback()
        logger.exception("AI diagnostic failed")
        return _error("诊断执行失败，请查看服务端日志", 500)


def diagnostic_detail(run_id: str):
    from app.ai.diagnostics import DiagnosticNotFound

    _holder, owner, role = _identity()
    try:
        service = _diagnostic_service()
        run = service.get_run(owner, run_id, role)
        try:
            after_seq = max(0, int(request.args.get("after_seq", 0)))
        except (TypeError, ValueError):
            return _error("after_seq 参数无效")
        events = service.events(owner, run_id, after_seq, role)
        return _ok(run=run, events=events, data=run)
    except DiagnosticNotFound as exc:
        return _error(str(exc), 404)


def cancel_diagnostic(run_id: str):
    from app.ai.diagnostics import DiagnosticNotFound

    _holder, owner, role = _identity()
    try:
        run = _diagnostic_service().cancel(owner, run_id, role)
        return _ok(run=run, data=run)
    except DiagnosticNotFound as exc:
        return _error(str(exc), 404)


def diagnostic_evidence(run_id: str):
    from app.ai.diagnostics import DiagnosticNotFound

    _holder, owner, role = _identity()
    try:
        items = _diagnostic_service().evidence(owner, run_id, role)
        data = {"items": items, "total": len(items)}
        return _ok(**data, data=data)
    except DiagnosticNotFound as exc:
        return _error(str(exc), 404)


def diagnostic_report(run_id: str):
    from app.ai.diagnostics import DiagnosticNotFound

    _holder, owner, role = _identity()
    try:
        report = _diagnostic_service().report(owner, run_id, role)
        return _ok(report=report, data=report)
    except DiagnosticNotFound as exc:
        return _error(str(exc), 404)


def ai_stats():
    """仪表盘用 AI 运维统计：近 N 天 AI 发起的批量执行按天台次（成功/失败）。

    数据源为 t_command_log 中 log_type='AI 批量命令' 的逐台审计行
    （AI Agent 的受控批量命令与只读诊断都会以该类型落审计）。
    """
    try:
        days = min(max(int(request.args.get("days", 7)), 1), 30)
    except (TypeError, ValueError):
        days = 7
    start = (datetime.now() - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    try:
        rows = (
            t_command_log.query
            .filter(t_command_log.log_type == "AI 批量命令")
            .filter(t_command_log.log_time >= start)
            .with_entities(
                func.date(t_command_log.log_time).label("day"),
                t_command_log.log_status,
                func.count(t_command_log.id).label("cnt"),
            )
            .group_by(func.date(t_command_log.log_time), t_command_log.log_status)
            .all()
        )
    except Exception:
        logger.exception("AI stats query failed")
        return _error("统计查询失败", 500)
    full_keys = [
        (start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)
    ]
    success = {key: 0 for key in full_keys}
    failed = {key: 0 for key in full_keys}
    for day, status, cnt in rows:
        key = str(day)[:10]
        if key not in success:
            continue
        if str(status) == "成功":
            success[key] += int(cnt)
        else:
            failed[key] += int(cnt)
    total_success = sum(success.values())
    total_failed = sum(failed.values())
    return _ok(
        days=[key[5:] for key in full_keys],
        success=[success[key] for key in full_keys],
        failed=[failed[key] for key in full_keys],
        total=total_success + total_failed,
        total_success=total_success,
        total_failed=total_failed,
    )


def public_providers():
    data = ProviderConfigService().public_rows()
    return _ok(**data, data=data)


def admin_providers():
    rows = ProviderConfigService().admin_rows()
    return _ok(providers=rows, data=rows)


def save_provider(code: str):
    try:
        row = ProviderConfigService().save(code, _payload())
        return _ok(provider=row, data=row)
    except ProviderConfigError as exc:
        db.session.rollback()
        return _error(str(exc))


def test_provider(code: str):
    try:
        result = ProviderConfigService().test(code)
        return _ok(result=result, data=result)
    except ProviderConfigError as exc:
        db.session.rollback()
        return _error(str(exc))
    except Exception:
        logger.exception("AI provider connection test failed: code=%s", code)
        return _error("连接测试失败，请查看服务端日志", 502)


def provider_models(code: str):
    try:
        result = ProviderConfigService().discover_models(code)
        return _ok(result=result, data=result)
    except ProviderConfigError as exc:
        db.session.rollback()
        return _error(str(exc))
    except Exception:
        logger.exception("AI provider model discovery failed: code=%s", code)
        return _error("模型列表获取失败，请查看服务端日志", 502)


def clear_provider_key(code: str):
    try:
        row = ProviderConfigService().clear_key(code)
        return _ok(provider=row, data=row)
    except ProviderConfigError as exc:
        return _error(str(exc), 404)


def conversations():
    _holder, owner, _role = _identity()
    rows = [
        _conversation_summary(row)
        for row in _store().list_conversations(owner)
    ]
    return _ok(conversations=rows, data=rows)


def create_conversation():
    from app.ai.autonomy.repository import AutonomyValidationError

    _holder, owner, _role = _identity()
    payload = _payload()
    providers = ProviderConfigService()
    try:
        row = providers.configured_row(str(payload.get("provider_code") or "") or None)
        context_mode = providers.context_mode(row, payload.get("context_mode"))
        autonomy_mode, autonomy_profile = _conversation_autonomy_permission(payload)
        conversation = _store().create_conversation(
            owner,
            row.provider_code,
            row.model,
            context_mode=context_mode,
            autonomy_mode=autonomy_mode,
            autonomy_profile=autonomy_profile,
        )
        conversation = _conversation_summary(conversation)
        return _ok(conversation=conversation, data=conversation)
    except (ProviderConfigError, AutonomyValidationError, ValueError) as exc:
        return _error(str(exc))


def update_conversation(conversation_id: str):
    from app.ai.autonomy.repository import AutonomyValidationError

    _holder, owner, _role = _identity()
    payload = _payload()
    store = _store()
    lock_token = None
    try:
        if "autonomy_mode" not in payload:
            raise AutonomyValidationError("autonomy_mode is required")
        autonomy_mode, autonomy_profile = _conversation_autonomy_permission(payload)
        lock_token = store.acquire_run_lock(owner, conversation_id, ttl=30)
        conversation = store.get_conversation(owner, conversation_id)
        conversation["autonomy_mode"] = autonomy_mode
        conversation["autonomy_profile"] = autonomy_profile
        conversation = store.save_conversation(owner, conversation)
        conversation = _conversation_summary(conversation)
        return _ok(conversation=conversation, data=conversation)
    except AutonomyValidationError as exc:
        return _error(str(exc))
    except AgentStoreNotFound as exc:
        return _error(str(exc), 404)
    except AgentStoreConflict as exc:
        return _error(str(exc), 409)
    finally:
        if lock_token:
            store.release_run_lock(owner, conversation_id, lock_token)


def conversation_detail(conversation_id: str):
    _holder, owner, role = _identity()
    store = _store()
    try:
        conversation = store.get_conversation(owner, conversation_id)
        try:
            diagnostic_runs = _diagnostic_service().conversation_runs(
                owner, conversation_id, limit=5, role=role
            )
        except Exception:
            # Deployments must apply rev50 before diagnostics become available;
            # existing conversation history remains readable during rollout.
            diagnostic_runs = []
        active_diagnostic = next(
            (
                run for run in diagnostic_runs
                if run.get("status") in ("queued", "running")
            ),
            None,
        )
        latest_diagnostic = diagnostic_runs[0] if diagnostic_runs else None
        result_scope = None
        result_id = (conversation.get("state") or {}).get("last_result_set_id")
        if result_id:
            try:
                result = store.get_result_set(owner, result_id)
                result_scope = {
                    "result_set_id": result["id"],
                    "kind": result["kind"],
                    "total": len(result.get("rows") or []),
                    **(result.get("summary") or {}),
                    "sample": (result.get("rows") or [])[:10],
                }
            except AgentStoreNotFound:
                pass
        display_messages = []
        for message in conversation.get("messages") or []:
            if message.get("role") not in ("user", "assistant"):
                continue
            if not str(message.get("content") or "").strip():
                continue
            display_messages.append({
                "id": message.get("id"),
                "role": message.get("role"),
                "content": message.get("content"),
                "created_at": _iso(message.get("created_at")),
            })
        display_events = _project_tool_events(conversation.get("events"))
        detail = {
            "id": conversation.get("id"),
            "title": conversation.get("title"),
            "provider_code": conversation.get("provider_code"),
            "model": conversation.get("model"),
            "context_mode": conversation.get("context_mode"),
            "autonomy_mode": str(conversation.get("autonomy_mode") or "ask"),
            "autonomy_profile": conversation.get("autonomy_profile"),
            "created_at": _iso(conversation.get("created_at")),
            "updated_at": _iso(conversation.get("updated_at")),
            "messages": display_messages,
            "tool_events": display_events,
            "autonomy_drafts": _project_autonomy_drafts(
                conversation.get("events")
            ),
            "result_scope": result_scope,
            "diagnostics": diagnostic_runs,
            "active_diagnostic": active_diagnostic,
            "latest_diagnostic": latest_diagnostic,
            "provider_observability": _project_provider_observability(
                conversation.get("state")
            ),
        }
        return _ok(conversation=detail, data=detail)
    except AgentStoreNotFound as exc:
        return _error(str(exc), 404)


def delete_conversation(conversation_id: str):
    _holder, owner, _role = _identity()
    try:
        _store().delete_conversation(owner, conversation_id)
        return _ok(deleted=True)
    except AgentStoreNotFound as exc:
        return _error(str(exc), 404)
    except AgentStoreConflict as exc:
        return _error(str(exc), 409)


def result_set_detail(result_set_id: str):
    _holder, owner, _role = _identity()
    try:
        result = _store().get_result_set(owner, result_set_id)
    except AgentStoreNotFound as exc:
        return _error(str(exc), 404)
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = min(100, max(1, int(request.args.get("page_size", 20))))
    except (TypeError, ValueError):
        return _error("分页参数无效")
    rows = result.get("rows") or []
    start = (page - 1) * page_size
    data = {
        "id": result["id"],
        "kind": result["kind"],
        "summary": result.get("summary") or {},
        "filters": result.get("filters") or {},
        "rows": rows[start:start + page_size],
        "page": page,
        "page_size": page_size,
        "total": len(rows),
    }
    return _ok(result=data, data=data)


def chat():
    _holder, owner, role = _identity()
    payload = _payload()
    conversation_id = str(payload.get("conversation_id") or "").strip()
    message = str(payload.get("message") or "").strip()
    app = current_app._get_current_object()
    generator = AgentRunner(
        store=_store(),
        worker_context_factory=app.app_context,
    ).run(
        owner=owner,
        role=role,
        conversation_id=conversation_id,
        message=message,
    )
    return Response(
        stream_with_context(generator),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
