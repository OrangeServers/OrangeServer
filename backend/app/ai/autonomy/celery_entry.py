# -*- coding: utf-8 -*-
"""M1/S2: Celery CLI 入口（`celery -A app.ai.autonomy.celery_entry`）。

CLI 需要模块级 app 对象：worker.get_celery_app 是功能门工厂，这里
在导入期求值。功能关闭或接线未完成时立刻报错，绝不让 Worker 带
着半接线状态启动。

首次部署在 setup 向导完成前缺少 OGS_FLASK_SECRET_KEY / OGS_FERNET_KEYS，
而 app.core.config 会对缺失密钥 fail-fast。backend 由 wsgi 的三态判定
（setup/state.resolve_mode）绕过这一校验；worker 也必须遵守同一判定：
setup / maintenance 阶段没有自治任务可处理，不 import 业务配置，而是
等待 runtime.env 落盘、配置就绪后再构建 Celery app，避免初始化阶段
崩溃循环。config.py 在 import 时会加载 runtime.env 到 os.environ，因此
等待到 normal 模式后再 import 即可拿到向导生成的密钥。
"""
import logging
import time

from setup import state

logger = logging.getLogger('autonomy_worker')

_POLL_SECONDS = 5


def _wait_until_configured() -> None:
    """阻塞直到配置就绪（setup 向导完成），避免 import 时缺密钥崩溃。"""
    warned = False
    while True:
        mode = state.resolve_mode()
        if mode == 'normal':
            return
        if not warned:
            logger.warning(
                'setup 未完成（mode=%s），worker 等待配置就绪后启动', mode,
            )
            warned = True
        time.sleep(_POLL_SECONDS)


_wait_until_configured()

from app.ai.autonomy.worker import get_celery_app  # noqa: E402

celery_app = get_celery_app()
if celery_app is None:
    raise RuntimeError(
        'autonomy celery app unavailable: '
        'OGS_AI_AUTONOMY_ENABLED / OGS_AI_AUTONOMY_REDIS_HOST required'
    )
