# -*- coding: utf-8 -*-
"""M1/S1: 自治任务最小 API 视图（默认禁用，按 owner 隔离）。

本工作包不实现任何远程副作用：创建/启动只落库与状态转换，
探针提议只做服务端分类与审批排队，执行器属于 S2。
"""
import json
import logging
import time
from datetime import datetime

from flask import Response, request, stream_with_context

from app.ai.autonomy.repository import (
    AutonomyConflict,
    AutonomyNotFound,
    AutonomyPermissionError,
    AutonomyRepository,
    AutonomyValidationError,
    resolve_current_autonomy_role,
)
from app.ai.autonomy.readiness import autonomy_readiness
from app.ai.autonomy.state import AutonomyStateError, TERMINAL_RUN_STATUSES
from app.ai.autonomy.flags import is_autonomy_enabled
from app.core import config
from app.core.config import FLASK_SECRET_KEY
from app.core.db.database import db
from app.tools.apierr import ApiCode, api_error, api_response
from app.tools.at import get_current_user


logger = logging.getLogger(__name__)

# M1/S3 切片 5：可续传 SSE 的轮询参数。事件回放靠 MySQL 单调
# sequence，重连不重复业务转换；连接到期即关闭，客户端携
# Last-Event-ID 重连续传。测试可调为 0/极小值。
STREAM_POLL_SECONDS = 2.0
STREAM_MAX_SECONDS = 300.0


def _jsonable(value):
    """REST 输出统一把 datetime 转 ISO 字符串。

    Flask 默认 JSON 序列化把 datetime 输出为 RFC 1123（http_date），
    前端 parseLogTime 无法解析；SSE 路径已用 default=str 输出 ISO，
    这里把 REST 路径对齐为同一格式。
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _identity():
    redis_holder, owner = get_current_user()
    role = (
        resolve_current_autonomy_role(db.session, owner)
        if owner else None
    )
    return redis_holder, owner, str(role or '')


def _payload():
    value = request.get_json(silent=True)
    if isinstance(value, dict):
        return value
    return {}


def _repo() -> AutonomyRepository:
    return AutonomyRepository(db.session, FLASK_SECRET_KEY)


def _disabled():
    return api_error(
        ApiCode.FORBIDDEN, 'AI 自治功能未启用', 403,
    )


def _not_admin():
    return api_error(ApiCode.FORBIDDEN, '此自治操作仅管理员可用', 403)


def _handle(exc):
    """统一映射自治模块异常到 HTTP 响应。

    owner 隔离的 Not Found 返回 404；跨 Run 的 Step 冲突返回 409，
    避免泄露其他 Run 内 Step 的存在性。
    """
    if isinstance(exc, AutonomyNotFound):
        return api_error(ApiCode.FORBIDDEN, str(exc), 404)
    if isinstance(exc, AutonomyPermissionError):
        return api_error(ApiCode.FORBIDDEN, str(exc), 403)
    if isinstance(exc, AutonomyValidationError):
        return api_error(ApiCode.TYPE_ERROR, str(exc), 400)
    if isinstance(exc, (AutonomyConflict, AutonomyStateError)):
        return api_error(ApiCode.FORBIDDEN, str(exc), 409)
    db.session.rollback()
    logger.exception('autonomy request failed')
    return api_error(ApiCode.INTERNAL_ERROR, '自治任务处理失败，请查看服务端日志', 500)


def autonomy_status():
    """GET /ai/autonomy/status：功能与基础设施状态（不受 flag 阻断）。"""
    _holder, owner, _role = _identity()
    status = autonomy_readiness(enabled=is_autonomy_enabled())
    status.update(_ops_capacity(owner, status))
    return api_response(data=status, enabled=status['enabled'])


def _ops_capacity(owner, readiness=None):
    """Augment readiness with bounded DB counts; never expose credentials."""
    summary = _repo().ops_summary(owner)
    readiness = readiness or autonomy_readiness(enabled=is_autonomy_enabled())
    configured = int(readiness.get('worker_concurrency_configured') or 1)
    observed = readiness.get('worker_concurrency_observed')
    from app.ai.knowledge import KnowledgeService

    try:
        knowledge_state = KnowledgeService(db.session).index_state()
    except Exception as exc:
        # A missing rev58 table during upgrade is itself an actionable state;
        # keep the readiness endpoint usable and never misreport it as empty.
        logger.warning('knowledge index state unavailable: %s', exc)
        knowledge_state = 'error'

    summary.update({
        'web_worker_class': 'gevent',
        'autonomy_pool': readiness.get('worker_pool') or 'prefork',
        'autonomy_concurrency': int(observed or configured),
        'knowledge_index_state': knowledge_state,
    })
    return summary


def ops_status():
    """GET /ai/ops/status: the AIOps landing-page aggregate."""
    _holder, owner, role = _identity()
    from app.ai.ops import alertmanager_configured, prometheus_configured

    readiness = autonomy_readiness(enabled=is_autonomy_enabled())
    data = dict(readiness)
    data.update(_ops_capacity(owner, readiness))
    data['alertmanager_configured'] = alertmanager_configured()
    data['prometheus_configured'] = prometheus_configured()
    return api_response(data=_jsonable(data))


def _configured_alert_owner():
    from app.core.db.database import t_acc_user

    owner = config.AI_ALERTMANAGER_OWNER
    row = t_acc_user.query.filter_by(
        name=owner, usrole='admin', is_deleted=False,
    ).first()
    return owner if row is not None else ''


def _record_prometheus(repo, owner, run_id, trigger):
    from app.ai.ops import PrometheusClient, PrometheusQueryError

    if not config.AI_PROMETHEUS_BASE_URL or not trigger.instance or not trigger.job:
        return
    try:
        result = PrometheusClient().service_availability(
            instance=trigger.instance, job=trigger.job,
        )
        artifact = repo.create_artifact(
            owner, run_id,
            kind='prometheus_query',
            title='Service availability (15 minutes)',
            content=json.dumps(result, ensure_ascii=False, sort_keys=True),
            commit=False,
        )
        values = result.get('values') or []
        latest = values[-1][1] if values else 'no samples'
        repo.record_evidence(
            owner, run_id,
            kind='prometheus_observation',
            summary='15-minute availability: %d samples, latest=%s' % (
                int(result.get('sample_count') or 0), latest,
            ),
            artifact_ids=[artifact['id']],
            event_type='prometheus_observed',
        )
    except PrometheusQueryError:
        db.session.rollback()
        repo.record_evidence(
            owner, run_id,
            kind='prometheus_observation',
            summary='15-minute availability query failed within fixed boundary',
            event_type='prometheus_observed',
        )


def alertmanager_webhook():
    """POST /ai/ops/alertmanager/webhook: Bearer-authenticated machine route."""
    from app.ai.ops import (
        ALERTMANAGER_MAX_BYTES,
        OpsValidationError,
        alertmanager_configured,
        parse_alertmanager,
        verify_bearer,
    )
    from app.ai.tools import PlatformQueryService

    if not is_autonomy_enabled():
        return api_error(ApiCode.FORBIDDEN, 'AI autonomy is disabled', 503)
    if not alertmanager_configured():
        return api_error(ApiCode.FORBIDDEN, 'Alertmanager is not configured', 503)
    if not verify_bearer(request.headers.get('Authorization', '')):
        return api_error(ApiCode.FORBIDDEN, 'invalid bearer token', 401)
    if request.content_length is not None and request.content_length > ALERTMANAGER_MAX_BYTES:
        return api_error(ApiCode.TYPE_ERROR, 'webhook payload is too large', 413)
    raw = request.stream.read(ALERTMANAGER_MAX_BYTES + 1)
    if len(raw) > ALERTMANAGER_MAX_BYTES:
        return api_error(ApiCode.TYPE_ERROR, 'webhook payload is too large', 413)
    try:
        trigger = parse_alertmanager(json.loads(raw.decode('utf-8')))
    except (UnicodeDecodeError, json.JSONDecodeError, OpsValidationError) as exc:
        return api_error(ApiCode.TYPE_ERROR, str(exc), 400)
    if config.AI_PROMETHEUS_BASE_URL and (
        not trigger.instance or not trigger.job
    ):
        return api_error(
            ApiCode.TYPE_ERROR,
            'instance and job labels are required when Prometheus is configured',
            400,
        )

    owner = _configured_alert_owner()
    if not owner:
        return api_error(ApiCode.FORBIDDEN, 'Alertmanager owner is unavailable', 503)
    platform = PlatformQueryService(owner, 'admin')
    if not platform.validate_asset_sys_user_id_pair(
        [trigger.host_id], trigger.system_user_id,
    ):
        return api_error(ApiCode.FORBIDDEN, 'asset credential binding denied', 403)

    repo = _repo()
    try:
        run = repo.find_run_by_trigger(
            owner, 'alertmanager', trigger.trigger_ref,
        )
        if trigger.status == 'resolved':
            if run is None:
                return api_response(
                    data={'accepted': True, 'run': None, 'ignored': True},
                    status=202,
                )
            repo.record_evidence(
                owner, run['id'],
                kind='alert_observation',
                summary='Alertmanager reported resolved; independent verification is still required',
                event_type='alert_resolved',
            )
            return api_response(
                data={'accepted': True, 'run': _jsonable(run), 'ignored': False},
                status=202,
            )

        duplicate = run is not None
        if run is None:
            try:
                run = repo.create_run(
                    owner, 'admin',
                    goal=trigger.goal,
                    host_id=trigger.host_id,
                    system_user_id=trigger.system_user_id,
                    mode='ask',
                    trigger_type='alertmanager',
                    trigger_ref=trigger.trigger_ref,
                    trigger_summary=trigger.trigger_summary,
                )
            except AutonomyConflict:
                db.session.rollback()
                run = repo.find_run_by_trigger(
                    owner, 'alertmanager', trigger.trigger_ref,
                )
                if run is None:
                    raise
                duplicate = True
            if not duplicate:
                repo.record_evidence(
                    owner, run['id'],
                    kind='alert_observation',
                    summary='Alertmanager firing: %s' % trigger.service,
                    event_type='alert_firing',
                )
                _record_prometheus(repo, owner, run['id'], trigger)
        if run['status'] == 'draft':
            run = repo.start_run(owner, 'admin', run['id'])
        if run['status'] not in {
            status.value for status in TERMINAL_RUN_STATUSES
        }:
            _dispatch_drive(run['id'])
        return api_response(
            data={
                'accepted': True,
                'duplicate': duplicate,
                'run': _jsonable(run),
            },
            status=202,
        )
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


def _guarded(role, *, admin_only=False):
    """feature flag and role checks shared by Run lifecycle endpoints."""
    if not is_autonomy_enabled():
        return _disabled()
    if str(role or '') not in {'admin', 'user'}:
        return _not_admin()
    if admin_only and role != 'admin':
        return _not_admin()
    return None


def _dispatch_drive(run_id):
    """状态推进后显式唤醒 Worker；投递失败不阻断接口，
    启动扫描与租约过期认领是兜底。"""
    try:
        from app.ai.autonomy import worker

        worker.dispatch_drive_run(run_id)
    except Exception:
        logger.exception('autonomy drive dispatch failed')


def create_run():
    _holder, owner, role = _identity()
    blocked = _guarded(role)
    if blocked:
        return blocked
    payload = _payload()
    try:
        run = _repo().create_run(
            owner, role,
            goal=str(payload.get('goal') or ''),
            host_id=payload.get('host_id'),
            system_user_id=payload.get('system_user_id'),
            mode=str(payload.get('mode') or ''),
            budget_payload=payload.get('budget'),
            profile_payload=payload.get('profile'),
            trigger_type='manual',
        )
        run = _jsonable(run)
        return api_response(data=run, run=run)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


def start_run(run_id):
    _holder, owner, role = _identity()
    blocked = _guarded(role)
    if blocked:
        return blocked
    try:
        run = _repo().start_run(owner, role, run_id)
        _dispatch_drive(run_id)
        run = _jsonable(run)
        return api_response(data=run, run=run)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


def cancel_run(run_id):
    """请求取消；Worker/执行器确认远端停止后才允许进入 cancelled。"""
    _holder, owner, role = _identity()
    blocked = _guarded(role)
    if blocked:
        return blocked
    try:
        run = _repo().request_cancel(owner, role, run_id)
        if run.get('status') not in {
            status.value for status in TERMINAL_RUN_STATUSES
        }:
            _dispatch_drive(run_id)
        run = _jsonable(run)
        return api_response(data=run, run=run)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


def list_runs():
    _holder, owner, role = _identity()
    blocked = _guarded(role)
    if blocked:
        return blocked
    try:
        runs = _jsonable(_repo().list_runs(owner))
        return api_response(data={'runs': runs}, runs=runs)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


def run_detail(run_id):
    _holder, owner, role = _identity()
    blocked = _guarded(role)
    if blocked:
        return blocked
    try:
        run = _jsonable(_repo().snapshot(owner, run_id))
        return api_response(data=run, run=run)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


def propose_step(run_id):
    """提议一个服务端自有探针动作（结构化参数白名单校验）。"""
    _holder, owner, role = _identity()
    blocked = _guarded(role, admin_only=True)
    if blocked:
        return blocked
    payload = _payload()
    try:
        step = _repo().propose_probe(
            owner, role, run_id,
            probe_id=str(payload.get('probe_id') or ''),
            params=payload.get('params') or {},
        )
        step = _jsonable(step)
        return api_response(data=step, step=step)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


def decide_step(run_id, step_id):
    """POST decision：输入恰好为 {operation, expected_revision}。"""
    _holder, owner, role = _identity()
    blocked = _guarded(role)
    if blocked:
        return blocked
    payload = _payload()
    try:
        step = _repo().decide(
            owner, role, run_id, step_id,
            operation=str(payload.get('operation') or ''),
            expected_revision=payload.get('expected_revision'),
        )
        _dispatch_drive(run_id)
        step = _jsonable(step)
        return api_response(data=step, step=step)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


def set_host_environment(host_id):
    """POST：管理员维护 t_host.ai_environment。"""
    _holder, owner, role = _identity()
    blocked = _guarded(role, admin_only=True)
    if blocked:
        return blocked
    payload = _payload()
    try:
        result = _repo().set_host_environment(
            host_id, str(payload.get('environment') or ''),
        )
        logger.info(
            'ai_environment changed by %s: host=%s %s -> %s',
            owner, host_id, result['previous'], result['ai_environment'],
        )
        return api_response(data=result)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


def list_artifacts(run_id):
    """GET：Run 内 Artifact 元数据（owner 隔离，正文单条读取）。"""
    _holder, owner, role = _identity()
    blocked = _guarded(role)
    if blocked:
        return blocked
    try:
        artifacts = _jsonable(_repo().list_artifacts(owner, run_id))
        return api_response(data={'artifacts': artifacts}, artifacts=artifacts)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


def artifact_content(run_id, artifact_id):
    """GET：单条 Artifact 解密正文；过期/跨 Run 一律 404。"""
    _holder, owner, role = _identity()
    blocked = _guarded(role)
    if blocked:
        return blocked
    try:
        artifact = _jsonable(_repo().get_artifact(owner, run_id, artifact_id))
        return api_response(data=artifact, artifact=artifact)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


def list_evidence(run_id):
    """GET：Run 内归一化 Evidence（不可信观察的有界索引）。"""
    _holder, owner, role = _identity()
    blocked = _guarded(role)
    if blocked:
        return blocked
    try:
        evidence = _jsonable(_repo().list_evidence(owner, run_id))
        return api_response(data={'evidence': evidence}, evidence=evidence)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)


# ----------------------------------------------------------------------
# M2/S2: reviewed knowledge truth and rebuildable Redis vector index
# ----------------------------------------------------------------------

def _knowledge_service():
    from app.ai.knowledge import KnowledgeService

    return KnowledgeService(db.session)


def _knowledge_scopes(owner, role):
    """Derive searchable scopes from current server-side asset grants."""
    from app.ai.tools import PlatformQueryService

    return PlatformQueryService(owner, role, session=db.session).authorized_knowledge_scopes()


def _knowledge_search_limit(value):
    if isinstance(value, bool):
        raise ValueError('limit must be an integer')
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('limit must be an integer') from exc
    if limit < 1:
        raise ValueError('limit must be at least 1')
    return min(limit, 8)


def _knowledge_search_query(value):
    from app.ai.knowledge import _bounded_text

    return _bounded_text(value, 'query', 512)


def _admin_only(role):
    if str(role or '') != 'admin':
        return _not_admin()
    return None


def _handle_knowledge(exc):
    from app.ai.knowledge import (
        KnowledgeConflict,
        KnowledgeNotFound,
        KnowledgeValidationError,
    )

    if isinstance(exc, KnowledgeNotFound):
        return api_error(ApiCode.FORBIDDEN, str(exc), 404)
    if isinstance(exc, KnowledgeValidationError):
        return api_error(ApiCode.TYPE_ERROR, str(exc), 400)
    if isinstance(exc, KnowledgeConflict):
        return api_error(ApiCode.FORBIDDEN, str(exc), 409)
    db.session.rollback()
    logger.exception('knowledge request failed')
    return api_error(ApiCode.INTERNAL_ERROR, '知识库处理失败，请查看服务端日志', 500)


def knowledge_config():
    """GET/PATCH embedding config; API keys are write-only."""
    _holder, _owner, role = _identity()
    blocked = _admin_only(role)
    if blocked:
        return blocked
    try:
        service = _knowledge_service()
        data = (
            service.save_config(_payload())
            if request.method == 'PATCH' else service.config()
        )
        return api_response(data=_jsonable(data))
    except Exception as exc:
        db.session.rollback()
        return _handle_knowledge(exc)


def knowledge_documents():
    """GET approved documents or POST an administrator-reviewed runbook."""
    _holder, owner, role = _identity()
    if request.method == 'POST':
        blocked = _admin_only(role)
        if blocked:
            return blocked
    try:
        service = _knowledge_service()
        if request.method == 'POST':
            return api_response(
                data=_jsonable(service.create_document(owner, _payload())),
                status=201,
            )
        if role == 'admin':
            documents = service.list_documents()
        else:
            documents = service.list_documents(
                scopes=_knowledge_scopes(owner, role),
            )
            documents = [
                {
                    key: value for key, value in document.items()
                    if key not in {'content_sha256', 'created_by'}
                }
                for document in documents
            ]
        return api_response(data={
            'documents': _jsonable(documents),
        })
    except Exception as exc:
        db.session.rollback()
        return _handle_knowledge(exc)


def knowledge_document(document_id):
    """GET/PATCH/DELETE one reviewed knowledge document."""
    _holder, _owner, role = _identity()
    blocked = _admin_only(role)
    if blocked:
        return blocked
    try:
        service = _knowledge_service()
        if request.method == 'DELETE':
            service.delete_document(document_id)
            return api_response(data={'deleted': True})
        data = (
            service.update_document(document_id, _payload())
            if request.method == 'PATCH' else service.get_document(document_id)
        )
        return api_response(data=_jsonable(data))
    except Exception as exc:
        db.session.rollback()
        return _handle_knowledge(exc)


def knowledge_reindex():
    """Queue a bounded Redis index rebuild on the existing prefork Worker."""
    _holder, _owner, role = _identity()
    blocked = _admin_only(role)
    if blocked:
        return blocked
    try:
        from app.ai.autonomy.worker import dispatch_knowledge_reindex

        service = _knowledge_service()
        data = service.request_reindex()
        try:
            dispatched = dispatch_knowledge_reindex()
        except Exception:
            service.mark_index_error()
            raise
        if not dispatched:
            service.mark_index_error()
            return api_error(
                ApiCode.FORBIDDEN, '自治 Worker 未就绪，无法重建知识索引', 503,
            )
        return api_response(data=_jsonable(data), status=202)
    except Exception as exc:
        db.session.rollback()
        return _handle_knowledge(exc)


def knowledge_capture_run(run_id):
    """Promote one owned, resolved and independently verified Run."""
    _holder, owner, role = _identity()
    blocked = _admin_only(role)
    if blocked:
        return blocked
    try:
        data = _knowledge_service().capture_run(owner, run_id)
        return api_response(data=_jsonable(data), status=201)
    except Exception as exc:
        db.session.rollback()
        return _handle_knowledge(exc)


def knowledge_search():
    """Search bounded, server-authorized knowledge references."""
    _holder, owner, role = _identity()
    payload = _payload()
    try:
        query = _knowledge_search_query(payload.get('query'))
        limit = _knowledge_search_limit(payload.get('limit', 8))
        items = _knowledge_service().search(
            query,
            limit=limit,
            scopes=_knowledge_scopes(owner, role),
        )
        return api_response(data={
            'results': _jsonable(items),
            'count': len(items),
        })
    except (TypeError, ValueError) as exc:
        return api_error(ApiCode.TYPE_ERROR, str(exc), 400)
    except Exception as exc:
        db.session.rollback()
        return _handle_knowledge(exc)


# ----------------------------------------------------------------------
# M1/S3 切片 5：可续传 SSE
# ----------------------------------------------------------------------

_TERMINAL_STREAM_STATUSES = frozenset(
    status.value for status in TERMINAL_RUN_STATUSES
)


def _sse_frame(event, data, event_id=None):
    body = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    lines = []
    if event_id is not None:
        lines.append('id: %s' % event_id)
    lines.append('event: %s' % event)
    lines.append('data: %s' % body)
    return '\n'.join(lines) + '\n\n'


def _resume_position():
    """重连位置：Last-Event-ID（SSE 标准重连头）优先于 after_seq。"""
    for raw in (request.headers.get('Last-Event-ID'),
                request.args.get('after_seq')):
        if raw is None or str(raw).strip() == '':
            continue
        try:
            return max(0, int(str(raw).strip()))
        except ValueError:
            continue
    return 0


def _drain_db_session():
    """每轮轮询后归还连接，避免长流独占连接池。"""
    try:
        db.session.expire_all()
        db.session.remove()
    except Exception:
        pass


def _stream_generator(owner, run_id, after_seq):
    """从 MySQL 单调 sequence 回放事件；终态后发终局快照并关流。

    重放完全由持久化的 sequence 驱动，重连不重复业务转换；
    客户端收到 terminal 事件后应重取最终权威快照。
    """
    delivered = max(0, int(after_seq))
    deadline = time.monotonic() + STREAM_MAX_SECONDS
    while time.monotonic() < deadline:
        try:
            repo = _repo()
            events = repo.list_events(
                owner, run_id, after_seq=delivered,
                limit=repo.MAX_EVENT_BATCH,
            )
            for item in events:
                delivered = int(item['sequence'])
                yield _sse_frame(
                    str(item['event_type']), item['payload'],
                    event_id=delivered,
                )
            run = repo.get_run(owner, run_id)
            if (
                str(run.get('status')) in _TERMINAL_STREAM_STATUSES
                and delivered >= int(run.get('latest_event_seq') or 0)
            ):
                yield _sse_frame('terminal', repo.snapshot(owner, run_id))
                return
        except AutonomyNotFound:
            yield _sse_frame('error', {'reason': 'run not found'})
            return
        except Exception:
            logger.exception('autonomy stream poll failed')
            yield _sse_frame('error', {'reason': 'stream interrupted'})
            return
        finally:
            _drain_db_session()
        if STREAM_POLL_SECONDS > 0:
            # Standard SSE comment: flushes the connection and detects peers
            # that disappeared while no business event was emitted.
            yield ': keepalive\n\n'
            time.sleep(STREAM_POLL_SECONDS)


def stream_run(run_id):
    """GET stream?after_seq=：可续传的 Run 事件流（SSE）。"""
    _holder, owner, role = _identity()
    blocked = _guarded(role)
    if blocked:
        return blocked
    try:
        # 开流前先复核存在性与 owner 归属，失败仍是标准 JSON 错误。
        _repo().get_run(owner, run_id)
    except Exception as exc:
        db.session.rollback()
        return _handle(exc)
    after_seq = _resume_position()
    return Response(
        stream_with_context(_stream_generator(owner, run_id, after_seq)),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )
