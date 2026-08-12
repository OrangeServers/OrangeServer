# -*- coding: utf-8 -*-
"""M1/S2: Celery CLI 入口（`celery -A app.ai.autonomy.celery_entry`）。

CLI 需要模块级 app 对象：worker.get_celery_app 是功能门工厂，这里
在导入期求值。功能关闭或接线未完成时立刻报错，绝不让 Worker 带
着半接线状态启动。
"""
from app.ai.autonomy.worker import get_celery_app

celery_app = get_celery_app()
if celery_app is None:
    raise RuntimeError(
        'autonomy celery app unavailable: '
        'OGS_AI_AUTONOMY_ENABLED / OGS_AI_AUTONOMY_REDIS_HOST required'
    )
