# -*- coding: utf-8 -*-
"""M1/S2: 自治执行的 Celery Worker 接线与任务契约。

设计要点：
- 自治专用 Redis 8 的 DB 1 作为 broker（DB 0 留给 checkpoint），
  与业务 Redis 7 完全隔离；功能关闭或接线未完成时不构建 Celery
  应用，现有聊天、诊断和批量审批不受影响。
- 任务只携带 run_id，按至少一次投递设计：task_acks_late +
  worker 丢失时 reject 重投，重复投递的并行安全由 lease.claim_run
  的条件 UPDATE 保证。
- 默认执行器是 drive.AutonomyDriver 驱动循环；checkpoint 未接线、
  规划器未接线等前置缺失由驱动循环 fail-closed 落库，任务层不
  伪造执行结果。
- Worker 就绪时扫描 queued / 租约过期的 Run 并重新投递，覆盖
  进程重启与 Celery 丢消息两类场景。
"""
import datetime
import logging
import math
import os
import socket

from celery import Celery
from celery.signals import worker_ready

from app.ai.autonomy.readiness import autonomy_redis_url
from app.core import config

logger = logging.getLogger('autonomy_worker')

DRIVE_RUN_TASK = 'ogs.autonomy.drive_run'
RECOVERY_SCAN_INTERVAL_SECONDS = 30

RESULT_CLAIMED = 'claimed'
RESULT_SKIPPED = 'skipped'
RESULT_EXECUTOR_UNAVAILABLE = 'executor_unavailable'

_celery_app = None
_periodic_recovery_app = None


class LeaseRetryRequired(Exception):
    """A live lease blocks this delivery; Celery must retry after expiry."""

    def __init__(self, run_id, retry_after_seconds):
        self.run_id = str(run_id)
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        # Preserve constructor args so the signal survives Celery prefork
        # exception serialization.
        super().__init__(self.run_id, self.retry_after_seconds)

    def __str__(self):
        return 'run %s lease is still active; retry in %d seconds' % (
            self.run_id, self.retry_after_seconds,
        )


def _lease_retry_delay(expires_at, now=None):
    """Return a non-zero delay just beyond the observed lease boundary."""
    now = now or datetime.datetime.utcnow()
    remaining = (expires_at - now).total_seconds()
    return max(1, int(math.ceil(remaining)) + 1)


def _has_live_claimable_lease(state, now=None):
    """Whether a delivery is blocked only by an unexpired active lease."""
    if state is None or state.get('lease_expires_at') is None:
        return False
    now = now or datetime.datetime.utcnow()
    if state['lease_expires_at'] < now:
        return False
    return state.get('status') in {
        'queued', 'running', 'waiting_approval', 'recovering',
    }


def autonomy_broker_url() -> str:
    """自治专用 broker：专用 Redis 8 的 DB 1。"""
    return autonomy_redis_url(1)


def worker_identity() -> str:
    """租约持有者标识：主机名 + PID，截断到列宽。"""
    return ('%s-%d' % (socket.gethostname(), os.getpid()))[:64]


def get_celery_app():
    """惰性构建自治 Celery 应用；功能关闭/接线未完成返回 None。"""
    global _celery_app
    if not config.AI_AUTONOMY_ENABLED:
        return None
    if not config.AI_AUTONOMY_REDIS_HOST:
        return None
    if _celery_app is None:
        app = Celery('ogs_autonomy', broker=autonomy_broker_url())
        app.conf.update(
            task_acks_late=True,
            task_reject_on_worker_lost=True,
            worker_prefetch_multiplier=1,
            broker_connection_retry_on_startup=True,
            result_backend=None,
        )
        _register_tasks(app)
        _celery_app = app
    return _celery_app


def reset_celery_app():
    """测试辅助：丢弃全局单例，迫使按当前配置重建。"""
    global _celery_app, _periodic_recovery_app
    _celery_app = None
    _periodic_recovery_app = None


def _register_tasks(app):
    @app.task(name=DRIVE_RUN_TASK, bind=True, max_retries=None)
    def drive_run(self, run_id):
        # Celery 任务不在 Flask 请求上下文里：db.session 必须先推
        # 应用上下文（与 cron/后台线程的既有模式一致）。
        from app.app_factory import app as flask_app
        from app.core.db.database import db

        with flask_app.app_context():
            try:
                return drive_run_once(
                    db.session, run_id,
                    executor=build_default_executor(db.session),
                )
            except LeaseRetryRequired as exc:
                raise self.retry(
                    exc=exc, countdown=exc.retry_after_seconds,
                )


def build_default_executor(session):
    """生产默认执行器：AutonomyDriver 驱动循环 + Tool Calling 规划器。

    规划器基于现有 Provider 接线（S3）；Provider 未配置或模型提议
    非法时，驱动循环会把 Run 落 failed + planner_failed 事件
    （fail-closed），绝不伪造执行结果。
    """
    from app.ai.autonomy.drive import (
        AutonomyDriver,
        make_autonomy_heartbeat_session_factory,
        make_autonomy_saver_factory,
    )
    from app.ai.autonomy.planner import make_default_planner
    from app.core.config import FLASK_SECRET_KEY

    def executor(run_id, claimed):
        driver = AutonomyDriver(
            session, FLASK_SECRET_KEY,
            planner=make_default_planner(),
            saver_factory=make_autonomy_saver_factory(),
            heartbeat_session_factory=(
                make_autonomy_heartbeat_session_factory()
            ),
        )
        return driver.drive(run_id, claimed)

    return executor


def dispatch_drive_run(run_id, celery_app=None):
    """把指定 Run 投递给 drive_run；功能未启用时静默不投。

    供决策/启动接口在状态推进后显式唤醒 Worker；投递失败不阻断
    调用方，启动扫描与租约过期认领是兜底。
    """
    if celery_app is None:
        celery_app = get_celery_app()
    if celery_app is None:
        return False
    celery_app.send_task(DRIVE_RUN_TASK, args=[run_id])
    return True


def drive_run_once(session, run_id, *, executor=None, worker_id=None,
                   lease_ttl=None):
    """drive_run 的可测内核：认领 → 执行 → 释放。

    executor 为 None 表示执行器尚未接线：认领后立即释放租约并返回
    executor_unavailable，不产生任何远程副作用。
    返回 RESULT_* 之一；仍被有效租约占用时抛 LeaseRetryRequired，
    注册的 Celery 任务会在租约边界后延迟重投；状态已不可认领时
    返回 RESULT_SKIPPED 并成功确认。
    """
    from app.ai.autonomy.lease import RunLeaseService

    lease = RunLeaseService(session)
    identity = worker_id or worker_identity()
    ttl = lease_ttl or config.AI_AUTONOMY_LEASE_TTL_SECONDS
    claimed = lease.claim_run(run_id, identity, ttl)
    if claimed is None:
        current = lease.get_lease_state(run_id)
        if _has_live_claimable_lease(current):
            retry_after = _lease_retry_delay(current['lease_expires_at'])
            logger.info(
                'drive_run delayed: run %s lease held by %s for %ss',
                run_id, current.get('lease_owner'), retry_after,
            )
            raise LeaseRetryRequired(run_id, retry_after)
        logger.info('drive_run skipped: run %s not claimable', run_id)
        return RESULT_SKIPPED
    if executor is None:
        lease.release_lease(run_id, identity, claimed['lease_token'])
        logger.warning(
            'drive_run released run %s: executor not wired', run_id,
        )
        return RESULT_EXECUTOR_UNAVAILABLE
    executor(run_id, claimed)
    return RESULT_CLAIMED


def dispatch_recoverable(session, celery_app=None):
    """启动扫描：先清扫超期 draft/待审批，再把 queued / 租约过期的
    Run 重新投递给 drive_run。"""
    from app.ai.autonomy.lease import RunLeaseService
    from app.ai.autonomy.repository import sweep_expired_runs

    if celery_app is None:
        celery_app = get_celery_app()
    if celery_app is None:
        return []
    try:
        sweep_expired_runs(
            session, ttl_seconds=config.AI_AUTONOMY_APPROVAL_TTL_SECONDS,
        )
    except Exception:
        logger.exception('autonomy expiry sweep failed')
    candidates = RunLeaseService(session).scan_recoverable()
    for candidate in candidates:
        celery_app.send_task(DRIVE_RUN_TASK, args=[candidate['run_id']])
    return candidates


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):
    """就绪后立即补投，并用 Celery 自带 timer 持续扫漏投 Run。"""
    global _periodic_recovery_app
    app = get_celery_app()
    if app is None:
        return

    _run_recovery_scan()
    timer = getattr(sender, 'timer', None)
    if timer is not None:
        if _periodic_recovery_app is app:
            return
        timer.call_repeatedly(
            RECOVERY_SCAN_INTERVAL_SECONDS, _run_recovery_scan,
        )
        _periodic_recovery_app = app


def _run_recovery_scan():
    """One fail-safe recovery pass, suitable for Celery's worker timer."""
    from app.app_factory import app as flask_app
    from app.core.db.database import db

    try:
        with flask_app.app_context():
            dispatch_recoverable(db.session)
    except Exception:
        logger.exception('autonomy recovery scan failed')
