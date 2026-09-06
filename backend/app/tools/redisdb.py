import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Optional, Tuple
import redis
from app.core.config import REDIS_CONF


# REV47-M18: redis retry logger
#   命名空间: 'redis_retry' (与 basesec_audit / audlog_fallback 区分)
_REDIS_RETRY_LOGGER = 'redis_retry'


# =============================================================================
# REV46-H12/H13/H14 + REV47-M19: 模块级共享连接池, 所有 ConnRedis 实例复用同一池
# =============================================================================
# 设计要点 (REV47-M19 注释增强):
#   1) 为什么是模块级单例: 多个 ConnRedis 实例不应各自创建独立 ConnectionPool
#      - redis-py StrictRedis 实例本身是轻量的, 真正的连接 (Connection) 是 lazy 创建
#      - ConnectionPool 是昂贵的资源 (socket fd + TCP 握手), 必须共享
#      - Flask 在多 worker / gevent 协程下, 共享池保证连接数可控 (max_connections 上限)
#   2) gevent 兼容性: redis-py 的 ConnectionPool 内置 threading.Lock (Python 3.x 默认),
#      gevent monkey-patch 后仍工作. 但阻塞操作 (socket IO) 仍会让出协程, 不会卡死其他请求.
#   3) pool 调优 (REV46-H12/H13/H14):
#      - password: 生产 Redis 必填
#      - db: 避免测试/生产共用 db=10 冲突
#      - socket_keepalive/socket_timeout/socket_connect_timeout: 防 hang 与长连接切断
#   4) decode_responses=True: 业务层拿到 str, 不需每次 .decode(), 与 ORM 字符串语义一致
#   5) max_connections 默认 256 (从 REDIS_CONF 读): 覆盖 100 路 SSE 与普通 API 的并发会话校验峰值
# =============================================================================
_shared_pool = redis.ConnectionPool(
    host=REDIS_CONF['host'],
    port=REDIS_CONF['port'],
    db=REDIS_CONF['db'],
    password=REDIS_CONF['password'] or None,
    max_connections=REDIS_CONF['max_connections'],
    socket_timeout=REDIS_CONF['socket_timeout'],
    socket_connect_timeout=REDIS_CONF['socket_connect_timeout'],
    socket_keepalive=REDIS_CONF['socket_keepalive'],
    decode_responses=True,
)


# 操作 redis 数据库
class ConnRedis:
    """
    host-->redis 的 ip 地址,str 类型
    port-->redis 的端口,int 类型

    REV46-M16: host/port 入参已被废弃（连接池参数由 REDIS_CONF 模块级决定）,
    保留签名仅为向后兼容老调用（at.py / Settings.py / 8 个测试 mock）。
    """

    def __init__(self, host=None, port=None, max_connections=10):
        # 使用共享连接池，所有实例复用
        self.conn = redis.StrictRedis(connection_pool=_shared_pool)

    @staticmethod
    def _with_retry(callable_, *, max_retries=3, base_delay=0.1, max_delay=2.0,
                    retriable_exceptions=None, op_name='redis_op'):
        """REV47-M18: 通用 retry 包装, 3 次重试 + 指数 backoff.

        适用场景: 读操作 (get / ping) 偶发网络抖动
        不适用: 写操作 (set) 通常要求 exactly-once, 业务侧自己保证

        Args:
            callable_: 0-arg callable, 实际执行 redis 操作
            max_retries: 最多重试次数 (含首次, 默认 3 = 首次 + 2 次重试)
            base_delay: 初始 backoff 秒数 (默认 0.1s)
            max_delay: backoff 上限 (默认 2.0s)
            retriable_exceptions: tuple of exception classes, 默认 (ConnectionError, TimeoutError)
            op_name: 操作名, 用于日志聚合

        Returns:
            callable_() 的返回值 (成功时)

        Raises:
            最后一次重试失败的异常
        """
        log = logging.getLogger(_REDIS_RETRY_LOGGER)
        if retriable_exceptions is None:
            retriable_exceptions = (
                redis.exceptions.ConnectionError,
                redis.exceptions.TimeoutError,
            )
        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                return callable_()
            except retriable_exceptions as e:
                last_err = e
                if attempt >= max_retries:
                    try:
                        log.error(
                            'redis retry exhausted: op=%s, attempt=%d/%d, err=%s',
                            op_name, attempt, max_retries, e,
                        )
                    except Exception:
                        pass
                    raise
                # 指数 backoff: base * 2^(attempt-1), 上限 max_delay
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                try:
                    log.warning(
                        'redis retry: op=%s, attempt=%d/%d, delay=%.2fs, err=%s',
                        op_name, attempt, max_retries, delay, e,
                    )
                except Exception:
                    pass
                time.sleep(delay)
        # 不可达 (逻辑上 for 循环总会 raise 或 return)
        if last_err:
            raise last_err
        return None

    def ping(self):
        """REV46-H15: 健康检查方法, 用于启动时连通性自检与运维探活.

        返回 True 表示 Redis 可达, 异常 (redis.ConnectionError 等) 上抛由调用方处理.

        REV47-M18: ping 也走 retry 包装, 偶发网络抖动自动恢复
        """
        return self._with_retry(
            lambda: self.conn.ping(),
            op_name='ping',
        )

    @contextmanager
    def pipeline(self, transaction: bool = True) -> Iterator[Any]:
        """REV47-M17: pipeline 批量执行入口 (contextmanager 风格).

        适用场景: 多个 key 的批量读写 (HSET 多字段, MGET 多 key, LPUSH 多条)
                  要求原子性 (transaction=True, MULTI/EXEC)
        性能: 1 个 pipeline 含 10 个 op 比 10 个单 op 减少 80% 网络往返

        Args:
            transaction: True=MULTI/EXEC 原子事务 (默认), False=管道无事务
                         业务侧如要求失败回滚 → 保持 True; 仅批量提交 → False

        Yields:
            redis.client.Pipeline 实例, 业务侧用 .set()/.get()/.hset() 等链式调用

        用法:
            with redis_conn.pipeline(transaction=True) as pipe:
                pipe.set('k1', 'v1')
                pipe.set('k2', 'v2')
                # exit context 自动 execute() (commit)

        Raises:
            redis.exceptions.*: 任何 pipeline 执行错误透传 (e.g. WatchError)

        注意:
            - 不在 _with_retry 内: 事务半失败状态 retry 会破坏业务一致性
            - 业务侧若要 retry, 整段重试需自己包 (e.g. 捕获 WatchError 后重做整批)
        """
        pipe = self.conn.pipeline(transaction=transaction)
        try:
            yield pipe
            # context 退出时执行, 业务层调 pipe.set() 等累积的命令在这里一次性发出
            pipe.execute()
        except Exception:
            # 异常时回滚 (redis-py 自动 discard 事务, 但显式调一下保险)
            try:
                pipe.reset()
            except Exception:
                pass
            raise
