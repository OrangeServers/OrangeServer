# -*- coding: utf-8 -*-
"""自治 Worker 的容器健康探针。

为什么需要这一层，而不是让 compose 直接 import readiness:
  readiness 在模块级 `from app.core import config`，而 config 对缺失密钥 fail-fast。
  首次部署向导完成前 OGS_FLASK_SECRET_KEY / OGS_FERNET_KEYS 还不存在，探针因此每
  10s 抛一次 RuntimeError；start_period 加 retries 耗尽后，Docker 会把一个"正在
  正确等待配置"的 Worker 标成 unhealthy —— 全新安装必然出现，且会破坏
  `docker compose up --wait`。

celery_entry 的模块 docstring 已经写明：worker 必须遵守 setup/state.resolve_mode()
的三态判定，setup / maintenance 阶段不 import 业务配置。探针是 Worker 的一部分，
同样必须遵守。非 normal 模式下没有自治任务可处理，等待本身就是正确状态。

readiness 的导入必须推迟到判定之后；放到模块级就会重现同一个 fail-fast。
"""
import sys


def main() -> int:
    from setup.state import resolve_mode

    if resolve_mode() != 'normal':
        return 0

    from app.ai.autonomy.readiness import (
        checkpoint_readiness,
        worker_readiness,
    )

    return 0 if checkpoint_readiness() and worker_readiness() else 1


if __name__ == '__main__':
    sys.exit(main())
