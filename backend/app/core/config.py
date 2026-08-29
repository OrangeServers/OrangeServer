import os
from pathlib import Path

try:
    from dotenv import load_dotenv, dotenv_values
    # 自动加载 backend 根目录下的 .env 文件（不存在不报错）
    _ENV_PATH = Path(__file__).resolve().parents[2] / '.env'
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=False)

    # SETUP-WIZARD: 加载首次部署向导写入的 runtime.env（fill-empty 语义）。
    # compose 会把未设置的宿主变量以空串注入容器（如 OGS_MYSQL_PASSWORD: ${...}），
    # 而 load_dotenv(override=False) 见到空串会认为"已设置"而跳过——
    # 因此这里只在进程 env 缺失或为空串时才填充，非空进程 env 始终最高优先。
    # runtime.env 不存在时零行为变化。
    _RUNTIME_ENV_PATH = Path(
        os.environ.get('OGS_RUNTIME_ENV_FILE')
        or Path(os.environ.get('OGS_DATA_DIR') or (Path(os.getcwd()) / 'data')) / 'runtime.env'
    )
    if _RUNTIME_ENV_PATH.exists():
        for _k, _v in (dotenv_values(_RUNTIME_ENV_PATH) or {}).items():
            if _v is not None and os.environ.get(_k) in (None, ''):
                os.environ[_k] = _v
except ImportError:
    # 没装 python-dotenv 时，依赖系统环境变量
    pass


def _env(key, default):
    """从环境变量读取；未设置则返回 default。空字符串按未设置处理。"""
    val = os.environ.get(key)
    if val is None or val == '':
        return default
    return val


def _env_required(key, error_hint=''):
    """REV16 B8 HIGH-1/2/3: 必填环境变量。空/未设置时立即抛 RuntimeError。
    用于敏感凭据 (MYSQL_PASSWORD / MAIL_PASSWORD / FLASK_SECRET_KEY) 的 fail-fast 校验。
    """
    val = os.environ.get(key)
    if val is None or val == '':
        raise RuntimeError(
            f'OGS 环境变量 {key} 未配置！{error_hint}\n'
            f'生成示例 (FLASK_SECRET_KEY): python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
            f'配置方法: 写入 backend/.env 或系统环境变量。'
        )
    return val


# 测试环境配置
DEFAULT_DIR1_PATH = _env('OGS_DEFAULT_DIR1_PATH', 'ls /data')
DEFAULT_DIR2_PATH = _env('OGS_DEFAULT_DIR2_PATH', "ls /data/%s")
RSYNC_SHELL_CMD = _env('OGS_RSYNC_SHELL_CMD', "/usr/bin/rsync -av /data/%s/%s /data/tmp/")

DEFAULT_DATA_DIR = _env('OGS_DATA_DIR', os.getcwd() + '/data')  # 服务的数据目录

# 邮件配置（敏感，REV16 B8 HIGH-2: 凭据必须 env 配置，禁止占位符）
#   fail-fast: 启动时若仍是占位符/默认值则抛 RuntimeError
_OGS_MAIL_USER = _env('OGS_MAIL_USER', '')
_OGS_MAIL_PASSWORD = _env('OGS_MAIL_PASSWORD', '')
_OGS_MAIL_SMTP = _env('OGS_MAIL_SMTP', '')
_PLACEHOLDER_MAIL = {'you mail name', 'you mail password', 'you mail smtp server',
                      'smtp.example.com', 'changeme', 'placeholder'}


def _is_real(v):
    """REV48 v2: 真实配置 = 非空 + 非占位符"""
    return bool(v) and v not in _PLACEHOLDER_MAIL
# MAIL_ENABLED: 三项全真实配置才启用, 任一占位符视为整体未配置 (跳过检查)
MAIL_ENABLED = _is_real(_OGS_MAIL_USER) and _is_real(_OGS_MAIL_PASSWORD) and _is_real(_OGS_MAIL_SMTP)
# REV48: 三项全空 = MAIL_ENABLED=False (禁用邮件), 任一占位符也 = False (视为未配齐)
# 仅 MAIL_ENABLED=True 才校验 (真要发邮件时三项必须都真实)
if MAIL_ENABLED and (
    not _is_real(_OGS_MAIL_USER)
    or not _is_real(_OGS_MAIL_PASSWORD)
    or not _is_real(_OGS_MAIL_SMTP)
):
    raise RuntimeError(
        'OGS_MAIL_USER / OGS_MAIL_PASSWORD / OGS_MAIL_SMTP 配置不完整或含占位符！\n'
        '请在 backend/.env 中设置真实邮件账户 (三项都填真实值)。\n'
        '如不需要邮件功能, 三项全部留空即可 (MAIL_ENABLED 自动 False)。\n'
        '  OGS_MAIL_USER=your-account@domain.com\n'
        '  OGS_MAIL_PASSWORD=your-smtp-auth-code\n'
        '  OGS_MAIL_SMTP=smtp.domain.com'
    )
MAIL_CONF = {
    'form_mail': _OGS_MAIL_USER,
    'password': _OGS_MAIL_PASSWORD,
    'smtp_server': _OGS_MAIL_SMTP,
}

# mysql 配置（REV16 B8 HIGH-1: 凭据必须 env 配置，硬编码默认值 zkfc123/192.0.2.1 禁止遗留）
#   fail-fast: 启动时若仍是默认值则抛 RuntimeError
_OGS_MYSQL_USER = _env('OGS_MYSQL_USER', '')
_OGS_MYSQL_PASSWORD = _env('OGS_MYSQL_PASSWORD', '')
_OGS_MYSQL_HOST = _env('OGS_MYSQL_HOST', '')
_DEFAULT_MYSQL_INSECURE = {'zkfc', 'zkfc123', '192.0.2.1', 'root', '', 'changeme', 'placeholder'}


def _mysql_insecure(value):
    """DEPLOY-AUDIT P2-6: .env.example 的 CHANGE_ME* 前缀也算占位符——
    旧版只认小写 changeme, 用户忘改 CHANGE_ME_STRONG 也能带弱配置启动。"""
    return value in _DEFAULT_MYSQL_INSECURE or value.upper().startswith('CHANGE_ME')


if (_mysql_insecure(_OGS_MYSQL_USER)
        or _mysql_insecure(_OGS_MYSQL_PASSWORD)
        or _mysql_insecure(_OGS_MYSQL_HOST)):
    raise RuntimeError(
        'OGS_MYSQL_USER / OGS_MYSQL_PASSWORD / OGS_MYSQL_HOST 仍为硬编码默认值！\n'
        '请在 backend/.env 中设置：\n'
        '  OGS_MYSQL_USER=your-db-user\n'
        '  OGS_MYSQL_PASSWORD=your-db-password\n'
        '  OGS_MYSQL_HOST=your-db-host'
    )
MYSQL_CONF = {
    'dbname': _env('OGS_MYSQL_DBNAME', 'orange'),
    'user': _OGS_MYSQL_USER,
    'password': _OGS_MYSQL_PASSWORD,
    'host': _OGS_MYSQL_HOST,
    'port': int(_env('OGS_MYSQL_PORT', '3306')),
}

# REV16 B8 HIGH-4: MYSQL_URI 密码 URL 编码 (防特殊字符 @ / : / # 破坏 URI 结构)
#   需在配置加载后 import: urllib.parse.quote
from urllib.parse import quote_plus as _quote_plus
MYSQL_URI = "mysql+pymysql://{}:{}@{}:{}/{}?charset=utf8mb4".format(
    MYSQL_CONF['user'],
    _quote_plus(MYSQL_CONF['password'], safe=''),
    MYSQL_CONF['host'], MYSQL_CONF['port'], MYSQL_CONF['dbname'],
)

# REV46-H12/H13/H14: Redis 完整配置（password / db / max_connections / socket_keepalive / socket_timeout）
#   生产环境建议配置：
#     OGS_REDIS_PASSWORD=<your-redis-password>      # 必填, 防止未授权访问
#     OGS_REDIS_DB=2                                # bundled 栈的业务会话和缓存库
#     OGS_REDIS_SOCKET_TIMEOUT=5                    # 单次操作超时, 防 hang
#     OGS_REDIS_SOCKET_KEEPALIVE=true               # 启用 TCP keepalive, 防长连接被中间设备切断
REDIS_CONF = {
    'host': _env('OGS_REDIS_HOST', '192.0.2.1'),
    'port': int(_env('OGS_REDIS_PORT', '6389')),
    # H12: 密码为空时 redis-py 传 None = 不认证（仅当 Redis 未设 requirepass 时可用）
    'password': _env('OGS_REDIS_PASSWORD', ''),
    # H13: db 索引配置化, 避免测试/生产共用 db=10 导致 key 冲突
    'db': int(_env('OGS_REDIS_DB', '2')),
    # 连接池上限
    'max_connections': int(_env('OGS_REDIS_MAX_CONNECTIONS', '10')),
    # H14: socket 维度的 timeout / keepalive, 防止单次操作 hang 或长连接被切断
    'socket_timeout': float(_env('OGS_REDIS_SOCKET_TIMEOUT', '5')),
    'socket_connect_timeout': float(_env('OGS_REDIS_CONNECT_TIMEOUT', '5')),
    'socket_keepalive': _env('OGS_REDIS_SOCKET_KEEPALIVE', 'true').lower() in ('1', 'true', 'yes', 'on'),
}

# 文件路径配置
#   目录用途说明（REVIEW-17 P0 优化：语义化重命名）：
#     avatars/    = 用户头像上传（GetUserImage / PutUserImage）
#     file/       = LocalFilePut SFTP 文件传输落地目录
#     key/        = SSH 私钥（按 sys_user.alias 命名 _rsa 后缀）
#     log/        = gunicorn / ogsbackend 日志（TimedRotatingFileHandler）
FILE_CONF = {
    'image_path': DEFAULT_DATA_DIR + '/avatars/',         # 原 'img/'
    'file_path': DEFAULT_DATA_DIR + '/file/',
    'file_path2': DEFAULT_DATA_DIR + '/file',
    'key_path': DEFAULT_DATA_DIR + '/key/',
    'log_path': DEFAULT_DATA_DIR + '/log/',
}

# Flask / Session 签名密钥。REV16 B8 HIGH-3: 启动时 fail-fast，禁止使用可识别默认值
#   即使是 dev 环境也应使用随机 key，避免多 dev 实例共享同一签名导致会话被伪造
FLASK_SECRET_KEY = _env_required(
    'OGS_FLASK_SECRET_KEY',
    error_hint='生成方式: python -c "import secrets; print(secrets.token_urlsafe(48))"',
)
# 额外防御: 防止 OGS_FLASK_SECRET_KEY 被设为常见可识别字符串
_INSECURE_SECRET_SUBSTR = ('dev-only', 'change-me', 'please-override', 'placeholder', 'changeme', 'change_me')
if any(s in FLASK_SECRET_KEY.lower() for s in _INSECURE_SECRET_SUBSTR):
    raise RuntimeError(
        'OGS_FLASK_SECRET_KEY 包含可识别默认字符串 (dev-only/change-me/...)！\n'
        '请使用随机密钥: python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )
# REV16 P2-4-LOW-12: 长度校验 (< 32 字符 HMAC 安全性不足)
#   RFC 2104 HMAC 建议密钥长度 >= hash 输出长度, SHA-256 = 32 字节
if len(FLASK_SECRET_KEY) < 32:
    raise RuntimeError(
        'OGS_FLASK_SECRET_KEY 长度不足 32 字符 (当前 %d 字符)！\n'
        '原因: HMAC-SHA256 安全性需要密钥长度 >= 32 字节 (RFC 2104)。\n'
        '请生成随机密钥: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        % len(FLASK_SECRET_KEY)
    )

# P1-1: Session 续期配置（sliding session）
#   默认 3 小时 = 10800 秒；剩余 TTL 不到一半时自动续期
#   避免活跃用户每 3h 强制重新登录
#   OGS_SESSION_EXP_SECONDS  单位秒
#   OGS_SESSION_RENEW_RATIO  阈值比例 (0.5 = 续期阈值 = 10800 * 0.5 = 5400)
SESSION_DEFAULT_EXP_SECONDS = int(_env('OGS_SESSION_EXP_SECONDS', str(3 * 3600)))
SESSION_RENEW_THRESHOLD_RATIO = float(_env('OGS_SESSION_RENEW_RATIO', '0.5'))

# P1-3: 登录失败限流阈值拆分
#   账号维度 fail_limit (默认 5)   防止单账号被暴力破解
#   IP   维度 fail_limit_ip (默认 20)  防公司 NAT 出口共享 IP 时误锁全公司
LOGIN_FAIL_LIMIT = int(_env('OGS_LOGIN_FAIL_LIMIT', '5'))
LOGIN_FAIL_LIMIT_IP = int(_env('OGS_LOGIN_FAIL_LIMIT_IP', '20'))

# REVIEW-5-E-1: cron last_result Redis TTL（防永久膨胀 + 敏感命令输出残留）
#   默认 7 天 = 7 * 86400 秒
CRON_RESULT_RETENTION = int(_env('OGS_CRON_RESULT_RETENTION', str(7 * 24 * 3600)))

# REVIEW-5-E-2: sftp 上传总大小限制（防磁盘填充 DoS）
#   默认 200MB
SFTP_MAX_UPLOAD_SIZE = int(_env('OGS_SFTP_MAX_UPLOAD_SIZE', str(200 * 1024 * 1024)))

# REVIEW-11-P0-2: SSH 主机 host key 策略
#   prod=reject (默认,生产环境严格,拒绝未知主机 host key 防止 MITM)
#   dev=warning (开发环境,报警但放行)
#   auto =AutoAddPolicy (仅限本地测试,生产严禁)
SSH_HOST_KEY_POLICY = _env('OGS_SSH_HOST_KEY_POLICY', 'reject')

# REVIEW-11-P2-1: SSH 连接超时 (秒)
SSH_CONNECT_TIMEOUT = int(_env('OGS_SSH_CONNECT_TIMEOUT', '10'))

# REVIEW-11-P2-2: SSH keepalive 间隔 (秒,0=禁用)
SSH_KEEPALIVE_INTERVAL = int(_env('OGS_SSH_KEEPALIVE_INTERVAL', '60'))

# REVIEW-11-P1-2: ssh_cmd stdout/stderr 输出上限 (字节)
#   防止恶意/错误命令产生大量输出耗尽后端内存 (默认 10MB)
SSH_CMD_MAX_OUTPUT_BYTES = int(_env('OGS_SSH_CMD_MAX_OUTPUT_BYTES', str(10 * 1024 * 1024)))

# REV46-M22: ssh_cmd 单命令执行 select 轮询超时 (秒)
#   旧实现: stdout.read() 同步阻塞, 死循环/慢命令会让 worker 永久挂起
#   新实现: select 主动 poll, 超过 SSH_CMD_TIMEOUT 秒未收到 EOF/数据则中断
#   默认 30s 兼容常见监控/部署命令 (apt/dnf 慢, 可调大)
SSH_CMD_TIMEOUT = int(_env('OGS_SSH_CMD_TIMEOUT', '30'))

# AI 诊断证据与报告保留期（天）。设置边界避免误配置造成即时过期或永久堆积。
AI_DIAGNOSTIC_EVIDENCE_RETENTION_DAYS = max(
    1,
    min(3650, int(_env('OGS_AI_DIAGNOSTIC_EVIDENCE_RETENTION_DAYS', '7'))),
)
AI_DIAGNOSTIC_REPORT_RETENTION_DAYS = max(
    1,
    min(3650, int(_env('OGS_AI_DIAGNOSTIC_REPORT_RETENTION_DAYS', '90'))),
)

# M1/S1: AI 自治安全与审批基线 feature flag。
#   默认关闭；仅在显式设置 OGS_AI_AUTONOMY_ENABLED=1/true/yes/on 时启用。
#   关闭时自治 API 全部返回 403，现有聊天、诊断和批量审批不受影响。
AI_AUTONOMY_ENABLED = str(_env('OGS_AI_AUTONOMY_ENABLED', '')).strip().lower() in (
    '1', 'true', 'yes', 'on',
)

# M1/S2: 自治 Redis 8 接线。标准部署与业务缓存共用实例但隔离 DB：
#   DB 0 存 LangGraph checkpoint/向量，DB 1 作为 Celery broker，DB 2 缓存；
#   服务端要求 AOF + volatile-lru + 持久卷（部署层保证）。
#   主机留空视为接线未完成，自治 Worker 不启动，等价于禁用。
AI_AUTONOMY_REDIS_HOST = str(_env('OGS_AI_AUTONOMY_REDIS_HOST', '')).strip()
AI_AUTONOMY_REDIS_PORT = int(_env('OGS_AI_AUTONOMY_REDIS_PORT', '6379'))
AI_AUTONOMY_REDIS_PASSWORD = str(_env('OGS_AI_AUTONOMY_REDIS_PASSWORD', ''))
# M2/S0: Celery prefork worker slots.  Reject invalid values at startup so a
# typo cannot silently turn the worker into an effectively serial process.
try:
    AI_AUTONOMY_WORKER_CONCURRENCY = int(
        _env('OGS_AUTONOMY_WORKER_CONCURRENCY', '2')
    )
except (TypeError, ValueError) as exc:
    raise RuntimeError(
        'OGS_AUTONOMY_WORKER_CONCURRENCY must be a positive integer'
    ) from exc
if AI_AUTONOMY_WORKER_CONCURRENCY < 1:
    raise RuntimeError(
        'OGS_AUTONOMY_WORKER_CONCURRENCY must be a positive integer'
    )
#   Worker 租约有效期；心跳必须显著短于此（执行器切片负责心跳）。
AI_AUTONOMY_LEASE_TTL_SECONDS = max(
    10, int(_env('OGS_AI_AUTONOMY_LEASE_TTL_SECONDS', '120')),
)
#   M1/S2: draft 与待审批的有效期（ROADMAP 默认 24 小时）；超时后
#   Run 落 expired，待审 Step 落 cancelled，绝不自动越过审批。
AI_AUTONOMY_APPROVAL_TTL_SECONDS = max(
    60, int(_env('OGS_AI_AUTONOMY_APPROVAL_TTL_SECONDS', '86400')),
)
#   M1/S3: 可选 Guardian（ai_review 档案的稳定计划边界独立复核）。
#   默认关闭；关闭或 Provider 未配置时 ai_review 计划照常走人工审批，
#   任何复核失败/不确定也一律回退人工。
AI_AUTONOMY_GUARDIAN_ENABLED = str(
    _env('OGS_AI_AUTONOMY_GUARDIAN_ENABLED', ''),
).strip().lower() in ('1', 'true', 'yes', 'on')

# M2/S1: Alertmanager 是独立机器调用方，使用固定 Bearer token 和固定
# 管理员 owner；标签只能提供资产/凭据 ID，不能选择用户、URL 或 PromQL。
AI_ALERTMANAGER_TOKEN = str(_env('OGS_AI_ALERTMANAGER_TOKEN', '')).strip()
AI_ALERTMANAGER_OWNER = str(_env('OGS_AI_ALERTMANAGER_OWNER', '')).strip()
AI_PROMETHEUS_BASE_URL = str(_env('OGS_AI_PROMETHEUS_BASE_URL', '')).strip()
AI_PROMETHEUS_BEARER_TOKEN = str(
    _env('OGS_AI_PROMETHEUS_BEARER_TOKEN', '')
).strip()

# REVIEW-11-P1-1: SSH 危险命令黑名单
#   拦截 rm -rf / / mkfs / dd if= / shutdown / reboot / fork 炸弹 等
#   逗号分隔, 可通过 env 覆盖 (生产环境只追加,不删除默认项)
SSH_DANGEROUS_COMMANDS = _env(
    'OGS_SSH_DANGEROUS_COMMANDS',
    'rm -rf /,mkfs,dd if=,shutdown,reboot,init 0,init 6,halt,poweroff,:(){:|:&};:,chmod -R 777 /,chown -R'
).split(',')

# REVIEW-11-P2-3: subprocess shell 编码 (防二进制输出/本地编码不一致解码错误)
SUBPROCESS_ENCODING = _env('OGS_SUBPROCESS_ENCODING', 'utf-8')

# ====== REVIEW-13: 邮件 / 验证码主题 ======
# P0-1: 邮箱正则 (RFC 5322 简化版, 防 SMTP header / Bcc 注入的前置条件)
#   ^[^@\s]+@[^@\s]+\.[^@\s]+$
#   - 用户名段不允许 @ 或空白
#   - 域名段至少一个 . 分隔
#   - 整段不允许 \r \n \0 (Header 注入载体)
EMAIL_REGEX = r'^[^@\s\r\n\0]+@[^@\s\r\n\0]+\.[^@\s\r\n\0]+$'

# P0-1: 邮箱 / header / message 长度上限 (RFC 5321 路径 <254 字符)
EMAIL_MAX_LEN = int(_env('OGS_EMAIL_MAX_LEN', '254'))
HEADER_MAX_LEN = int(_env('OGS_HEADER_MAX_LEN', '200'))
MESSAGE_MAX_LEN = int(_env('OGS_MESSAGE_MAX_LEN', '10000'))

# P0-2: 邮件中继限流 (Redis 计数)
#   MAIL_RELAY_LIMIT_MIN  - 单 IP 1 分钟最多 MAIL_RELAY_LIMIT_MIN 次
#   MAIL_RELAY_LIMIT_HOUR - 单 IP 1 小时最多 MAIL_RELAY_LIMIT_HOUR 次
MAIL_RELAY_LIMIT_MIN = int(_env('OGS_MAIL_RELAY_LIMIT_MIN', '5'))
MAIL_RELAY_LIMIT_HOUR = int(_env('OGS_MAIL_RELAY_LIMIT_HOUR', '30'))
MAIL_RELAY_PREFIX_MIN = 'mail_relay_min:'
MAIL_RELAY_PREFIX_HOUR = 'mail_relay_hour:'

# P0-3: 验证码 Redis key 前缀 (防与其他业务 key 冲突)
#   CheckMail.send 和 UserRegister.register 共用此前缀
MAIL_VERIFY_PREFIX = _env('OGS_MAIL_VERIFY_PREFIX', 'mail_verification:')
MAIL_VERIFY_TTL = int(_env('OGS_MAIL_VERIFY_TTL', '180'))  # 3 分钟

# P1-1: SMTP 连接超时 (秒, 防慢 SMTP / 网络抖动导致请求 hang 住)
SMTP_CONNECT_TIMEOUT = int(_env('OGS_SMTP_CONNECT_TIMEOUT', '10'))
SMTP_OP_TIMEOUT = int(_env('OGS_SMTP_OP_TIMEOUT', '30'))

# REV46-M3: 邮件模板系统 (JSON 格式 env)
#   格式: '{"register_verify": {"subject": "注册验证码", "body": "您的验证码: {code}", "mime_type": "plain"}}'
#   占位符用 {key} 形式, render_email_template 时替换
#   空配置表示禁用模板系统, 调用 render 会抛 ValueError
import json as _json  # REV46-M3
_EMAIL_TEMPLATES_RAW = _env('OGS_EMAIL_TEMPLATES', '{}')
try:
    EMAIL_TEMPLATES = _json.loads(_EMAIL_TEMPLATES_RAW)
    if not isinstance(EMAIL_TEMPLATES, dict):
        EMAIL_TEMPLATES = {}
except (ValueError, TypeError):
    EMAIL_TEMPLATES = {}

# P1-2: SMTP 加密配置 (默认 TLS 587)
#   OGS_MAIL_USE_SSL=true   -> SMTP_SSL (传统 465)
#   OGS_MAIL_USE_TLS=false  -> 裸连 (25/587 不推荐)
MAIL_PORT = int(_env('OGS_MAIL_PORT', '587'))
MAIL_USE_TLS = _env('OGS_MAIL_USE_TLS', 'true').lower() in ('1', 'true', 'yes', 'on')
MAIL_USE_SSL = _env('OGS_MAIL_USE_SSL', 'false').lower() in ('1', 'true', 'yes', 'on')

# P1-3: CaptchaGet IP 限流 (Redis 计数)
#   同一 IP 1 分钟最多 CAPTCHA_GET_LIMIT_MIN 次 (防攻击者刷 Redis 内存膨胀)
# REV38-M10: 把默认 rate limit 从 10/分钟 提到 30/分钟 (REV36-M10 建议)
#   - 30/分钟 仍足以阻挡刷请求攻击 (单 IP 每秒 0.5 次)
#   - 但给用户重试/网络抖动留余量
CAPTCHA_GET_LIMIT_MIN = int(_env('OGS_CAPTCHA_GET_LIMIT_MIN', '30'))
CAPTCHA_GET_PREFIX_MIN = _env('OGS_CAPTCHA_GET_PREFIX_MIN', 'captcha_get_min:')
