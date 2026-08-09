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
import logging
import os
import socket

from celery import Celery
from celery.signals import worker_ready

from app.core import config

logger = logging.getLogger('autonomy_worker')

DRIVE_RUN_TASK = 'ogs.autonomy.drive_run'

RESULT_CLAIMED = 'claimed'
RESULT_SKIPPED = 'skipped'
RESULT_EXECUTOR_UNAVAILABLE = 'executor_unavailable'

_celery_app = None


def autonomy_broker_url() -> str:
    """自治专用 broker：专用 Redis 8 的 DB 1。"""
    password = config.AI_AUTONOMY_REDIS_PASSWORD
    auth = ':%s@' % password if password else ''
    return 'redis://%s%s:%d/1' % (
        auth, config.AI_AUTONOMY_REDIS_HOST, config.AI_AUTONOMY_REDIS_PORT,
    )


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
    global _celery_app
    _celery_app = None


def _register_tasks(app):
    @app.task(name=DRIVE_RUN_TASK, bind=True)
    def drive_run(self, run_id):
        from app.core.db.database import db

        return drive_run_once(
            db.session, run_id, executor=build_default_executor(db.session),
        )


def build_default_executor(session):
    """生产默认执行器：AutonomyDriver 驱动循环。

    规划器尚未接线的切片里，驱动循环会把 Run 落 failed +
    planner_unavailable 事件（fail-closed），绝不伪造执行结果。
    """
    from app.ai.autonomy.drive import (
        AutonomyDriver,
        make_autonomy_heartbeat_session_factory,
        make_autonomy_saver_factory,
    )
    from app.core.config import FLASK_SECRET_KEY

    def executor(run_id, claimed):
        driver = AutonomyDriver(
            session, FLASK_SECRET_KEY,
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
    返回 RESULT_* 之一；认领失败（重复投递/他人持有/不可认领）
    返回 RESULT_SKIPPED，任务按成功确认，避免无意义重投风暴。
    """
    from app.ai.autonomy.lease import RunLeaseService
    from app.ai.autonomy.state import RunStatus

    lease = RunLeaseService(session)
    identity = worker_id or worker_identity()
    ttl = lease_ttl or config.AI_AUTONOMY_LEASE_TTL_SECONDS
    # 执行中的重复投递：本 Worker 已持有 running 租约，原任务仍在
    # 推进，直接确认跳过，绝不二次执行。
    current = lease.get_lease_state(run_id)
    if (
        current is not None
        and current['lease_owner'] == identity
        and current['status'] == RunStatus.RUNNING.value
    ):
        logger.info(
            'drive_run skipped: duplicate delivery of running run %s',
            run_id,
        )
        return RESULT_SKIPPED
    claimed = lease.claim_run(run_id, identity, ttl)
    if claimed is None:
        logger.info('drive_run skipped: run %s not claimable', run_id)
        return RESULT_SKIPPED
    if executor is None:
        lease.release_lease(run_id, identity, claimed['revision'])
        logger.warning(
            'drive_run released run %s: executor not wired', run_id,
        )
        return RESULT_EXECUTOR_UNAVAILABLE
    executor(run_id, claimed)
    return RESULT_CLAIMED


def dispatch_recoverable(session, celery_app=None):
    """启动扫描：把 queued / 租约过期的 Run 重新投递给 drive_run。"""
    from app.ai.autonomy.lease import RunLeaseService

    if celery_app is None:
        celery_app = get_celery_app()
    if celery_app is None:
        return []
    candidates = RunLeaseService(session).scan_recoverable()
    for candidate in candidates:
        celery_app.send_task(DRIVE_RUN_TASK, args=[candidate['run_id']])
    return candidates


@worker_ready.connect
def _on_worker_ready(**kwargs):
    """Celery worker 就绪后补投漏掉/中断的 Run（仅功能启用时）。"""
    if get_celery_app() is None:
        return
    from app.core.db.database import db

    try:
        dispatch_recoverable(db.session)
    except Exception:
        logger.exception('autonomy startup scan failed')
