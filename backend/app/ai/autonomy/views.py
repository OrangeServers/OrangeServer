# -*- coding: utf-8 -*-
"""M1/S1: 自治任务最小 API 视图（默认禁用，仅管理员）。

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
)
from app.ai.autonomy.readiness import autonomy_readiness
from app.ai.autonomy.state import AutonomyStateError, TERMINAL_RUN_STATUSES
from app.core.config import AI_AUTONOMY_ENABLED, FLASK_SECRET_KEY
from app.core.db.database import db
from app.tools.apierr import ApiCode, api_error, api_response
from app.tools.at import get_current_user, get_current_user_role


logger = logging.getLogger(__name__)

# M1/S3 切片 5：可续传 SSE 的轮询参数。事件回放靠 MySQL 单调
# sequence，重连不重复业务转换；连接到期即关闭，客户端携
# Last-Event-ID 重连续传。测试可调为 0/极小值。
STREAM_POLL_SECONDS = 1.0
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
    return redis_holder, owner, str(get_current_user_role() or '')


def _payload():
    value = request.get_json(silent=True)
    if isinstance(value, dict):
        return value
    return {}


def _repo() -> AutonomyRepository:
    return AutonomyRepository(db.session, FLASK_SECRET_KEY)


def _disabled():
    return api_error(
        ApiCode.FORBIDDEN, 'AI 自治功能未启用 (OGS_AI_AUTONOMY_ENABLED)', 403,
    )


def _not_admin():
    return api_error(ApiCode.FORBIDDEN, '自治任务 v1 仅管理员可用', 403)


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
    status = autonomy_readiness(enabled=bool(AI_AUTONOMY_ENABLED))
    return api_response(data=status, enabled=status['enabled'])


def _guarded(role):
    """flag + v1 管理员限制的统一前置检查；通过返回 None。"""
    if not AI_AUTONOMY_ENABLED:
        return _disabled()
    if role != 'admin':
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
    blocked = _guarded(role)
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
    blocked = _guarded(role)
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
