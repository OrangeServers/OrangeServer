# -*- coding=utf8 -*-
import argparse
import importlib
import os
import pkgutil
from app.app_factory import *
from geventwebsocket.handler import WebSocketHandler
from geventwebsocket.server import WSGIServer
from datetime import timedelta
from app.tools.at import ogs_auth_token, require_role, Log
from app.tools.csrf import csrf_protect
# REV37-H4: 全局错误响应走 api_error，统一错误格式
from app.tools.apierr import api_error, ApiCode
from app.api import local_api
from app.api import RouteRule, route as route_rule  # REV38-M1: 统一 ROUTES schema
from app.core.config import FLASK_SECRET_KEY, _env  # 集中配置入口

# REV16 B9 HIGH-3: monkey.patch_all() 移到 main 块内
#   原：import 时立即执行 → 即便只 import init.py 也替换所有原生 socket
#   风险：单元测试 / gunicorn worker / IDE 静态分析工具全部受影响
#   修复：仅当实际启动 WSGIServer 时（__main__ 块）才 patch
#   影响：gevent 协程模型必须在 patch 之后才能用 WebSocket，所以 main 内启动前先 patch

app.config.update(DEBUG=False)
# Flask session 签名密钥：来自 conf.py（生产环境必须通过 OGS_FLASK_SECRET_KEY 覆盖）
app.secret_key = FLASK_SECRET_KEY

# REV43-H4: ProxyFix 中间件 — 让 request.remote_addr 反映真实客户端 IP
#   背景: Nginx 反代后 Flask 看到的是 Nginx 的 127.0.0.1, 登录日志/限流会失真
#   修复: ProxyFix 读 X-Forwarded-For 头最左一栏作为 remote_addr
#   配置: x_for=1 表示信任 1 层反代 (Nginx), 不可超过实际代理层数 (否则会被伪造)
#   注意: 反代必须配置 proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    _OGS_PROXY_LAYERS = int(_env('OGS_PROXY_LAYERS', '1'))
    if _OGS_PROXY_LAYERS > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_OGS_PROXY_LAYERS, x_proto=_OGS_PROXY_LAYERS, x_host=_OGS_PROXY_LAYERS)
except ImportError:
    # werkzeug < 2.0 兼容路径
    from werkzeug.contrib.fixers import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app)

# P0-3: 生产环境必须覆盖 FLASK_SECRET_KEY（缺省值是公开硬编码字符串，上线即双失守）
if FLASK_SECRET_KEY == 'dev-only-secret-key-please-override-in-production':
    if _env('OGS_ENV', 'dev').lower() in ('prod', 'production'):
        raise RuntimeError(
            'OGS_FLASK_SECRET_KEY 必须在生产环境配置。\n'
            '生成方式: python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
            '配置方法: 写入 .env 或系统环境变量。'
        )


def sta_cs():
    parser = argparse.ArgumentParser('请输入 --host  --port 参数')
    # REV16 B9 HIGH-1: --host 默认值改为 127.0.0.1（原 0.0.0.0 会监听所有网卡）
    parser.add_argument("--host", help="listen host (127.0.0.1=本机回环;0.0.0.0=所有网卡,仅生产部署需要)", default='127.0.0.1')
    parser.add_argument("--port", help="port", default=28000)
    args = parser.parse_args()
    return args


# REV16 B9 HIGH-1: 生产环境 fail-fast 防御
#   OGS_ENV=prod 且 --host 仍为 0.0.0.0 时启动失败, 必须改为 127.0.0.1 或经 Nginx 反代
_PROD_HOST_DENY = {'0.0.0.0', ''}


def _validate_listen_addr(host, port):
    """B9 HIGH-1: 启动前 fail-fast 校验 listen 地址。
    - 生产环境禁止 0.0.0.0（避免直接公网暴露）
    - 端口范围 [1, 65535]
    """
    env = _env('OGS_ENV', 'dev').lower()
    is_prod = env in ('prod', 'production')
    if is_prod and host in _PROD_HOST_DENY:
        raise RuntimeError(
            'OGS_ENV=%s 时 --host 禁止为 %r（公网暴露风险）！\n'
            '生产环境必须使用 Nginx 反向代理: --host 127.0.0.1 --port <内部端口>\n'
            '或 OGS_HOST_OVERRIDE=true 显式确认 (仅内网部署场景)' % (env, host)
        )
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise RuntimeError('listen port 非法: %r (须 1..65535)' % port)
    return host, port


"""  各接口详解：
        /server/    操作资产主机接口
        /mail/    发送邮件相关接口
        /local/    本地执行命令接口
    返回结果：code
    0     正常
    2     服务内部错误
    3     接口未授权访问
    4     权限不够
    101   用户名不存在
    102   密码错误
    103   用户名已存在
    104   该邮箱已被注册
    105   验证码过期
    106   验证码错误
    111   资产(资产组)已存在
    112   连接主机失败
    113   资产密码或其他错误
    121   目录不存在
    131   禁止删除
    132   权限已存在
    141   定时任务已存在
    201   读取数据库错误
    211   传递类型错误
    231   文件已存在
    232   名称已存在
"""


@app.route('/')
def index():
    return 'hello from OrangeServer api server', 200


# =============================================================================
# REV39-L10: trace_id 全链路追踪机制
# =============================================================================
#   设计:
#     - before_request: 每个请求生成 trace_id (uuid4().hex), 存到 g.trace_id
#     - 业务日志 / 错误日志: 带 [trace=<id>] 前缀, 便于日志聚合 (Loki/ELK) 关联
#     - 响应: 所有响应 (含 errorhandler) 的 JSON body 含 trace_id 字段
#     - 响应头: X-Trace-Id header 同步返回, 前端 axios 拦截器可读取上报
#   价值:
#     - 前端报错时携带 X-Trace-Id, 客服 / 排查时直接 grep 锁定后端日志
#     - 分布式调用链 (web -> api -> ssh/sftp) 共享 trace_id
#     - 不影响业务代码: 视图函数不需修改, errorhandler 自动附加
# =============================================================================
import uuid as _uuid


@app.before_request
def _before_request_trace_id():
    """为每个请求生成 trace_id 存到 g.trace_id。
    REV39-L10: 优先用上游 header (X-Trace-Id / X-Request-Id) 实现分布式追踪,
                没有则用 uuid4().hex 生成新 id。
    """
    from flask import g, request as _req
    g.trace_id = (
        _req.headers.get('X-Trace-Id')
        or _req.headers.get('X-Request-Id')
        or _uuid.uuid4().hex
    )


@app.after_request
def _after_request_trace_id(resp):
    """所有响应附 X-Trace-Id header。
    REV39-L10: 前端 axios 拦截器可读 X-Trace-Id 上报到错误监控。
    """
    from flask import g
    tid = getattr(g, 'trace_id', None)
    if tid:
        resp.headers['X-Trace-Id'] = tid
    return resp


# =============================================================================
# REV47-M16: teardown_appcontext → session.remove()
# =============================================================================
#   背景: gevent monkey-patch 后, 协程切换可能在请求处理中途发生。
#         SQLAlchemy scoped_session 默认与 Flask app context 绑定 (请求级),
#         但 gevent 协程间共享同一个 scoped_session registry, 不主动 remove
#         会导致 (a) 协程 A 持有的 ORM 对象被协程 B 看到 (脏读) (b) 连接池泄漏
#   修复: 显式 teardown_appcontext 调 db.session.remove()
#     - 释放连接回池 (避免连接池耗尽, 引发后续请求 timeout)
#     - 清空 session identity map (下个请求不残留 ORM 状态)
#     - 不调 commit: R2-9 / REV45-H16 已禁止 teardown 自动提交
#   注意: 这是 R2-9 / REV45-H16 的姊妹机制
#     - H16: SQLALCHEMY_COMMIT_ON_TEARDOWN = False (禁用 teardown 自动 commit)
#     - M16: teardown_appcontext 调 session.remove() (启用 session 清理)
#   不冲突: 一个管"事务提交", 一个管"连接释放"; 都必要
# =============================================================================
@app.teardown_appcontext
def _shutdown_db_session(exception=None):
    """REV47-M16: gevent 协程间 session 隔离, 释放连接回池.

    触发时机: Flask app context 销毁时 (请求结束 / app context pop 时)
    行为:
      - 调 db.session.remove() → 返回连接 + 清空 session
      - 不调 commit (R2-9 / REV45-H16 禁止 teardown 自动提交)
      - 即便 exception 非 None, 也安全 remove (session 已部分回滚的状态可安全丢弃)
    """
    from app.core.db.settings import db
    try:
        db.session.remove()
    except Exception as _e:
        # 防御性: remove 失败不应阻断请求响应 / 后续请求
        Log.logger.warning('[REV47-M16] session.remove() 失败: %s' % _e)


# =============================================================================
# REV43-H1: CORS 配置（前后端分离 dev 环境必需）
# =============================================================================
#   背景:
#     - 前端 Vite dev server (默认 5173) 跨域调后端 (28000), 浏览器拦截
#     - 生产环境通过 Nginx 同源反代可绕过, 但 dev 环境无法调试
#     - 修复: 手工实现 CORS (不依赖 flask-cors), 支持 OGS_CORS_ORIGINS 配置白名单
#   安全要点:
#     - 严格白名单匹配 (不允许通配 '*' 与 credentials 共存, 浏览器会拒绝)
#     - 凭据请求 (cookies / csrf_token) 必须 echo back 精确 origin
#     - OPTIONS 预检直接 204, 不进 view_func (避免触发 csrf 校验或 405)
#     - Vary: Origin 防止 CDN / 浏览器缓存污染 (不同 origin 看到不同 CORS 头)
#     - 非白名单 origin 的 OPTIONS → 403 (拒绝泄露允许列表)
#   配置:
#     OGS_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
#     逗号分隔, 默认 Vite dev 默认端口; 生产通常配置为前端域名
# =============================================================================
_OGS_CORS_ORIGINS_RAW = _env('OGS_CORS_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173')


def _parse_cors_origins(raw):
    """REV43-H1: 解析 OGS_CORS_ORIGINS env 值 → frozenset 白名单.

    - 逗号分隔, 空白 trim
    - 跳过空段 (避免 'a, ,b' → {'a','b'})
    - 严格大小写敏感 (origin 是 case-sensitive 标识符)
    """
    if not raw:
        return frozenset()
    return frozenset(o.strip() for o in raw.split(',') if o.strip())


_OGS_CORS_ORIGINS_SET = _parse_cors_origins(_OGS_CORS_ORIGINS_RAW)
_OGS_CORS_ALLOW_METHODS = 'GET,POST,OPTIONS'
_OGS_CORS_ALLOW_HEADERS = 'Content-Type,X-CSRF-Token,X-Requested-With,X-Trace-Id'
_OGS_CORS_ALLOW_CREDENTIALS = 'true'
_OGS_CORS_MAX_AGE = '600'  # 预检缓存 10 分钟


def _set_cors_headers(resp, origin):
    """REV43-H1: 在响应上设 CORS 响应头 (helper, 可被测试直接调用).

    注意: Access-Control-Allow-Origin 必须 echo back 精确 origin,
    不能用 '*' (与 Allow-Credentials=true 冲突, 浏览器拒绝).
    """
    resp.headers['Access-Control-Allow-Origin'] = origin
    resp.headers['Access-Control-Allow-Credentials'] = _OGS_CORS_ALLOW_CREDENTIALS
    resp.headers['Access-Control-Allow-Methods'] = _OGS_CORS_ALLOW_METHODS
    resp.headers['Access-Control-Allow-Headers'] = _OGS_CORS_ALLOW_HEADERS
    resp.headers['Vary'] = 'Origin'


@app.before_request
def _cors_preflight():
    """REV43-H1: OPTIONS 预检 204, 非白名单 origin 拒绝 403.

    浏览器跨域 POST 前先发 OPTIONS 探测服务器是否允许.
    不拦截会让 view_func 收到 OPTIONS 请求, 触发 csrf 校验或返回 405.
    """
    from flask import request, make_response
    if request.method != 'OPTIONS':
        return None
    origin = request.headers.get('Origin')
    if not origin or origin not in _OGS_CORS_ORIGINS_SET:
        # 非白名单 origin → 拒绝 (浏览器拿不到 CORS 头会拦截, 防止探测允许列表)
        return make_response('forbidden', 403)
    resp = make_response('', 204)
    _set_cors_headers(resp, origin)
    resp.headers['Access-Control-Max-Age'] = _OGS_CORS_MAX_AGE
    return resp


@app.after_request
def _cors_actual(resp):
    """REV43-H1: 真实请求附 CORS 响应头 (仅白名单 origin 才回写).

    非白名单 origin → 不回写 header, 浏览器会拦截响应.
    """
    from flask import request
    origin = request.headers.get('Origin')
    if origin and origin in _OGS_CORS_ORIGINS_SET:
        _set_cors_headers(resp, origin)
    return resp


# =============================================================================
# REV37-H4: 全局错误处理器, 所有未捕获异常 / 404 / 500 走统一 api_error 格式
# =============================================================================
#   原状: Flask 默认返回 HTML 错误页 (开发环境) 或空 body (生产环境)
#   修复: 所有错误转 JSON, 让前端 axios 拦截器统一走 res.status 判 401/403/404/500
#   REV39-L10: errorhandler 自动附加 trace_id 到响应 body + header
# =============================================================================
def _inject_trace_id(extra=None):
    """REV39-L10: 构造 errorhandler 的 extra dict, 注入 trace_id。"""
    from flask import g
    extra = dict(extra) if extra else {}
    tid = getattr(g, 'trace_id', None)
    if tid:
        extra['trace_id'] = tid
    return extra


@app.errorhandler(404)
def _err_404(_e):
    return api_error(ApiCode.DIR_NOT_FOUND, '接口不存在', **_inject_trace_id())


@app.errorhandler(405)
def _err_405(_e):
    return api_error(ApiCode.TYPE_ERROR, '请求方法不被允许', **_inject_trace_id())


@app.errorhandler(500)
def _err_500(_e):
    return api_error(ApiCode.INTERNAL_ERROR, '服务器内部错误', **_inject_trace_id())


@app.errorhandler(Exception)
def _err_exception(e):
    # Werkzeug HTTPException (如 abort(403)) 仍交给对应 status handler
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return api_error(
            ApiCode.INTERNAL_ERROR,
            e.description or str(e),
            status=e.code,
            **_inject_trace_id(),
        )
    # 业务代码未捕获异常 (REV39-L10: 日志带 trace_id)
    from flask import g
    tid = getattr(g, 'trace_id', 'unknown')
    Log.logger.exception('[UNHANDLED] [trace=%s] %s: %s' % (tid, type(e).__name__, e))
    return api_error(
        ApiCode.INTERNAL_ERROR,
        '服务器内部错误: %s' % type(e).__name__,
        **_inject_trace_id(),
    )


# =============================================================================
# REVIEW-15 P1-7 / P1-C：/local/health 轻量健康检查
# =============================================================================
# 设计要点：
#   - 不需 csrf / auth / DB（仅证明 Flask 进程在跑 + 可以响应 HTTP）
#   - Dockerfile.backend HEALTHCHECK 调它，失败时容器会被 K8s/Docker 重启
#   - 不调用任何耗时操作（DB / Redis / 磁盘 IO ），仅返进程级状态
#   - 返回 JSON 便于日志采集（loki/ELK）
@app.route('/local/health')
def local_health():
    import json
    from datetime import datetime
    return app.response_class(
        response=json.dumps({
            'status': 'ok',
            'service': 'orange-server-backend',
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'pid': os.getpid(),
        }, ensure_ascii=False),
        status=200,
        mimetype='application/json',
    )


# SETUP-WIZARD: normal 模式下的 setup 状态探针（仿 /local/health 裸路由）。
#   前端向导轮询它判断"重启已完成"；除 mode 外不含任何敏感信息。
#   写接口（apply 等）只存在于 setup 进程，normal 模式下天然 404。
@app.route('/setup/api/status')
def setup_status_normal():
    import json
    return app.response_class(
        response=json.dumps({'mode': 'normal'}),
        status=200,
        mimetype='application/json',
    )


def _register_routes_from_module(module):
    """从模块的 ROUTES 表自动注册路由到 Flask app。

    REV38-M1: 统一 ROUTES schema 为 RouteRule namedtuple（8 字段）。
    REV38-M4: 新增第 9 字段 is_alias；启动时检测同一 view_class.method 重复注册:
      - 第一条注册作为主路由 (is_alias=False 隐含)
      - 后续注册若未标 is_alias → WARNING (疑似错误, 同一 class.method 通常应只注册一次)
      - 后续注册若标 is_alias=True → 静默 (合法 alias 模式, 用于前端兼容期)
    兼容旧 tuple 写法（5-tuple / 6-tuple），自动归一化为 RouteRule。

    RouteRule 字段:
        url            URL 路径
        view_class     视图类
        method         实例方法名/属性名
        need_auth      是否需登录鉴权
        is_property    True=属性调用, False=方法调用
        roles          允许角色列表（None=所有登录用户）
        description    接口描述（API 文档自动生成）
        skip_csrf      是否跳过 CSRF 校验（默认 False）
        is_alias       REV38-M4: 是否为 alias（同一 view_class.method 重复注册）
    """
    routes = getattr(module, 'ROUTES', [])
    for route in routes:
        # REV38-M1: 归一化为 RouteRule
        if isinstance(route, RouteRule):
            rule = route
        elif isinstance(route, tuple):
            # 兼容旧 tuple 写法（5-tuple 或 6-tuple）
            url, cls, method_name, need_auth, is_property = route[:5]
            roles = route[5] if len(route) > 5 else None
            rule = RouteRule(
                url=url, view_class=cls, method=method_name,
                need_auth=need_auth, is_property=is_property,
                roles=list(roles) if roles else None,
                description='[LEGACY]', skip_csrf=False, is_alias=False,
            )
        else:
            Log.logger.warning('[INIT_DIAG] Skip invalid route entry: %r' % (route,))
            continue

        # REV38-M7: 启动时检测 URL 重复 (不管主/alias, URL 重复就 raise)
        #   - Flask 真的不允许同 URL + 同一 endpoint 注册两次
        #   - 与 M4 不同: M4 是 (view_class, method) 配对的软警告, M7 是 URL 硬冲突
        #   - 早期 raise 避免启动到一半被 Flask 抛 BadRequestKeyError
        if rule.url in _SEEN_URLS:
            first_cls, first_method, first_alias = _SEEN_URLS[rule.url]
            # URL 冲突: 真正会让 Flask 抛 RuntimeError 的硬冲突
            raise RuntimeError(
                '[REV38-M7] Duplicate URL registration: %r is registered twice. '
                'First: %s.%s (is_alias=%s). '
                'Second: %s.%s (is_alias=%s). '
                'URL 冲突会让 Flask add_url_rule 抛 AssertionError, 启动崩溃. '
                '请改用不同 URL 或在 ROUTES 表合并两条规则.' %
                (rule.url,
                 first_cls.__name__, first_method, first_alias,
                 rule.view_class.__name__, rule.method, rule.is_alias)
            )
        _SEEN_URLS[rule.url] = (rule.view_class, rule.method, rule.is_alias)

        # REV38-M4: 启动时检测重复注册, 仅约束主路由 (is_alias=False)
        #   - 主路由 (is_alias=False): 必须唯一, 同一 (cls, method) 第二次出现 → WARNING
        #   - alias (is_alias=True): 总合法, 静默 (兼容期别名路由不受约束)
        if not rule.is_alias:
            dup_key = (id(rule.view_class), rule.method)
            if dup_key in _ROUTE_DUP_KEYS:
                first_url = _ROUTE_DUP_KEYS[dup_key]
                Log.logger.warning(
                    '[REV38-M4] Duplicate route registration: '
                    '%s.%s registered to BOTH %r and %r. '
                    'Mark the second one with is_alias=True to suppress this warning.' %
                    (rule.view_class.__name__, rule.method, first_url, rule.url)
                )
            else:
                _ROUTE_DUP_KEYS[dup_key] = rule.url

        def _make_view(c, m, auth, prop, r):
            def view_func(**kwargs):
                inst = c()
                # REV43-H5 / ROUTE-PROPERTY-ONCE:
                #   getattr 同时完成存在性检查和取值。不能先 hasattr 再 getattr，
                #   因为 hasattr 会求值 @property，导致状态变更接口每次执行两遍。
                try:
                    attr = getattr(inst, m)
                except AttributeError as exc:
                    raise AttributeError(
                        '[REV43-H5] View class %s missing method %r' % (c.__name__, m)
                    ) from exc
                result = attr if prop else attr()
                return result
            # ti2-API: 给 view_func 注入 YAML docstring, 让 flasgger 自动生成 /apidocs
            #   规则: 从 rule.description + rule.url 提取 tag + summary
            #   tags 取 URL 第一段 (e.g. /account/login_dl2 -> account)
            #   description 作为 first_line (flasgger 显示成 summary, 不进 yaml.safe_load)
            #   注意: flasgger 0.9.7 yaml.safe_load 解析 --- 之后整段;
            #         description 原文 (含中文 + 括号 + :) 直接进 YAML 会触发 ScannerError.
            #         故 desc 放 docstring 顶部 (--- 之前), YAML 只放 tags/responses.
            _desc = (rule.description or '').strip()
            if _desc:
                _tag = (rule.url.lstrip('/').split('/', 1)[0] or 'default')
                _summary = _desc.replace('\n', ' ').strip()
                view_func.__doc__ = (
                    '%s\n'
                    '---\n'
                    'tags:\n'
                    '  - %s\n'
                    'responses:\n'
                    '  200:\n'
                    '    description: 成功响应 (业务 code=0)\n'
                    '  401:\n'
                    '    description: 未登录 (code=3)\n'
                    '  403:\n'
                    '    description: 权限不足 (code=4)\n'
                    '  500:\n'
                    '    description: 服务器错误 (code=2)\n'
                ) % (_summary, _tag)
            if auth:
                # REV37-H3/H4 + REV38-M1: 装饰器链顺序 (从内到外: role -> auth -> csrf)
                #   实际调用顺序: csrf -> auth -> role -> view_func
                #   校验顺序说明:
                #     1) csrf 最外层先拦截未带 csrf_token 的请求, 不走到 auth
                #        -> 节省一次 Redis IO (cookie token 不在 Redis)
                #        -> 错误响应 401 (REV37-H4 统一走 api_error)
                #     2) auth 通过后再到 role 校验, 最终进 view_func
                #   need_auth=False 时仍包 csrf (防匿名 POST 接口被 CSRF 攻击)
                view_func = require_role(*r)(view_func) if r else view_func
                view_func = ogs_auth_token(view_func)
                # REV38-M1: skip_csrf=True 时跳过 csrf 校验（公开接口）
                if not rule.skip_csrf:
                    view_func = csrf_protect(view_func)
            else:
                # 匿名路由: 按 skip_csrf 决定是否走 csrf（默认 False，POST 公开接口需 CSRF）
                if not rule.skip_csrf:
                    view_func = csrf_protect(view_func)
            return view_func

        view = _make_view(rule.view_class, rule.method, rule.need_auth, rule.is_property, rule.roles)
        # REV43-H3 (R2-4-2): 显式 endpoint= 防 Flask 重复命名冲突
        #   原: Flask 默认用 view_func.__name__ 作 endpoint, 同 URL 的不同 view_func
        #       会被 Flask 报 AssertionError: "View function mapping is overwriting
        #       an existing endpoint function"
        #   修: 显式传 endpoint=URL 生成的名字, 跟 view.__name__ 一致, 杜绝冲突
        endpoint_name = rule.url.strip('/').replace('/', '_') or 'root'
        view.__name__ = endpoint_name
        # P0-2: methods 移除小写 'get'，仅保留 'POST'
        #   原 ['POST', 'get'] 让 GET 落到 csrf 豁免分支，状态变更接口可被 GET 触发
        app.add_url_rule(rule.url, endpoint=endpoint_name, view_func=view, methods=['POST'])


# REV38-M4: 启动时重复注册检测的全局表
#   key = (id(view_class), method_name) → 首次注册的 URL
#   用途：检测疑似误用，同一 class.method 通常只应注册一次（alias 例外）
_ROUTE_DUP_KEYS = {}

# REV38-M7: 启动时 URL 重复检测的全局表
#   key = url string → 首次注册的 (view_class, method, is_alias)
#   用途：URL 重复会让 Flask add_url_rule 直接 AssertionError 崩溃,
#         必须启动前 raise 才能让部署者看到清晰错误
_SEEN_URLS = {}

# REV38-M9: 核心 API 模块列表 - 这些模块加载失败会导致前端整片 404
#   生产环境下 (OGS_ENV=prod) 启动时 raise, dev/test 仍 warning + 跳过
_CORE_API_MODULES = frozenset({
    'auth_api',
    'local_api',
    'server_api',
    'account_api',
})


def _is_prod_env():
    """REV38-M9: 判断当前是否生产环境。

    读取 OGS_ENV 环境变量 (默认 'dev'):
      - 'prod' / 'production' → True
      - 其他 (含 'dev' / 'test' / 未设置) → False
    """
    env = os.environ.get('OGS_ENV', 'dev').strip().lower()
    return env in ('prod', 'production')


def _reset_route_dup_state():
    """REV38-M7: 重置 M4+M7 全局表 (仅测试用).

    真实启动时 _ROUTE_DUP_KEYS / _SEEN_URLS 是单例累积状态 (覆盖整个 app 生命周期).
    测试中如果多次调用 _register_routes_from_module, 需要手动清空避免跨测试污染.
    生产环境无需调用.
    """
    _ROUTE_DUP_KEYS.clear()
    _SEEN_URLS.clear()


def orange_init_api():
    # 幂等守卫 (ti2-API): 已初始化过直接 return, 避免 pytest 跨测试文件 / 跨 fixture
    #   setup 重入时 add_url_rule 触发 Flask "can no longer be called" assert.
    global _orange_api_inited
    if globals().get('_orange_api_inited'):
        Log.logger.info('[ti2-API] orange_init_api already done, skip')
        return
    # ---- 自动注册：遍历 app.api 下所有模块的 ROUTES 表 ----
    import app.api as service_pkg
    for importer, modname, ispkg in pkgutil.iter_modules(service_pkg.__path__):
        if modname.startswith('_'):
            continue
        try:
            module = importlib.import_module(f'app.api.{modname}')
        except ImportError as e:
            # REV38-M9: 生产环境下, 核心路由模块加载失败应 raise, 避免 silent 漏配
            #   - 'auth_api' / 'local_api' / 'server_api' / 'account_api' 缺失
            #     会导致前端所有 /local/* / /server/* / /account/* 404, 但启动只 warning
            #   - 生产环境不放过这种可能, 启动期 fail-fast
            #   - dev / test 仍 warning + continue, 方便单模块调试
            is_core = modname in _CORE_API_MODULES
            if is_core and _is_prod_env():
                raise ImportError(
                    '[REV38-M9] Core API module %r failed to load in production env (OGS_ENV=%s). '
                    'Fail-fast to prevent silent 404 for all routes from this module. '
                    'Original error: %s' % (modname, os.environ.get('OGS_ENV', 'dev'), e)
                ) from e
            Log.logger.warning('[INIT_DIAG] Skip service module %s: %s' % (modname, e))
            continue
        if hasattr(module, 'ROUTES'):
            _register_routes_from_module(module)

    # AI Agent uses GET/PUT/DELETE, URL params and POST SSE, so it cannot use
    # the legacy POST-only RouteRule registry.
    from app.api.ai_api import register_ai_routes
    register_ai_routes(app)

    # M1/S1: 自治任务最小 API（默认禁用，OGS_AI_AUTONOMY_ENABLED 门控）。
    from app.api.autonomy_routes import register_autonomy_routes
    register_autonomy_routes(app)

    # ---- 手动注册：特殊路由（WebSocket / 带参数 / 需要传参构造） ----
    # P0-1: /local/init 加 csrf_protect + ogs_auth_token
    #   原裸路由：任何匿名用户可触发初始化逻辑
    #   修复后：必须登录态 + 携带 csrf_token 才能调用
    # REV38-M3: 改注册为正式 /local/status 路由 + /local/init alias
    #   - 正式 endpoint /local/status 不受启动阶段限制, 任何登录态可查
    #   - /local/init alias 在运行时返 410 Gone, 前端迁移期间保留兼容
    app.add_url_rule(
        '/local/status',
        view_func=csrf_protect(ogs_auth_token(local_api.local_app_status)),
        methods=['POST'],
    )
    app.add_url_rule(
        '/local/init',
        view_func=csrf_protect(ogs_auth_token(local_api.local_app_status_alias)),
        methods=['POST'],
    )

    # P0-2: 状态变更接口 methods 移除 'get'，仅保留 'POST'
    #   /local/dir/group  执行 cmdlist_shell
    #   /local/rsync      执行 rsync 命令
    #   /local/file       文件上传 put_file
    #   /local/dir/project 是 getdir1()（执行 ls /data），归为状态变更（实际跑 shell 命令）
    app.add_url_rule('/local/dir/group', view_func=csrf_protect(local_api.local_dir_group), methods=['POST'])
    app.add_url_rule('/local/dir/project', view_func=csrf_protect(local_api.local_dir_project), methods=['POST'])
    app.add_url_rule('/local/rsync', view_func=csrf_protect(local_api.local_rsync_code), methods=['POST'])

    # 文件上传
    app.add_url_rule('/local/file', view_func=csrf_protect(local_api.local_data_file_put), methods=['POST'])

    # 图片获取（带 URL 参数，保留 GET：前端 store/index.js:114 用 <img src=...> 直接加载）
    # csrf_protect 天然豁免 GET/HEAD/OPTIONS，所以仍受 csrf 保护
    # REV39-L1: 显式声明 <string:img_name> 替代默认转换器
    #   - Flask string converter 默认就不接受 '/'，但显式声明更清晰且防止误改
    #   - 双重防御：URL 段不含 '/' + GetUserImage.get_img 内部 regex 白名单 + realpath 越界检测
    #   - 攻击面: '/local/image/test_get/..%2F..%2Fetc' → string 接收 '..%2F..%2Fetc'，regex 拒绝 → 默认图
    app.add_url_rule(
        '/local/image/test_get/<string:img_name>',
        view_func=csrf_protect(local_api.local_image_get),
        methods=['GET', 'POST'],
    )

    # WebSocket 路由（必须声明 websocket=True）
    app.add_url_rule('/local/websocket', view_func=local_api.local_web_ssh, websocket=True)
    app.add_url_rule('/local/sftp/websocket', view_func=local_api.local_sftp_connect, websocket=True)

    # 启动时初始化
    with app.app_context():
        local_api.local_app_init()
        local_api.local_chart_into()

    # ti2-API: 注册 Swagger UI 路由 (/apidocs) 和 OpenAPI spec 路由 (/apispec_1.json)
    #   - 依赖 app_factory._Swagger, 如 flasgger 未装则跳过
    #   - /apidocs 返 HTML Swagger UI
    #   - /apispec_1.json 返 OpenAPI 2.0 JSON 规范 (front-end / 集成可直接用)
    #   - 在 orange_init_api() 末尾注册, 确保所有 view_func 已 add_url_rule 完毕
    #     (Swagger.init_app 会扫所有 view_func docstring 收集 spec)
    from app.app_factory import _Swagger as _swag
    if _swag is not None:
        try:
            _swag.init_app(app)
            Log.logger.info('[ti2-API] Swagger UI enabled: /apidocs, /apispec_1.json')
        except Exception as _e:
            # Swagger init 失败不应阻断 init (例如 docstring 格式错)
            Log.logger.warning('[ti2-API] Swagger init_app failed: %s' % _e)

    # ti4-OPENAPI: 暴露 OpenAPI 3.0 规范导出端点 (/openapi.json + /openapi.yaml)
    #   - 业界 2024+ 主流工具链 (Stoplight / Redoc / openapi-generator) 倾向 3.0
    #   - flasgger 0.9.7.1 内部仍产 2.0 spec (docstring YAML 模板是 2.0 格式);
    #     这里加 2.0→3.0 转换层 (app.tools.openapi3), 业务代码零侵入
    #   - 同时保留 /apispec_1.json (2.0) 兼容性, 不破坏现有 ti2-API 集成测试
    #   - 路由无 csrf / auth 保护: OpenAPI 规范是公开元数据, 让前端 / 第三方工具能拉
    if _swag is not None:
        from app.tools.openapi3 import convert_swagger2_to_openapi3
        import json as _json
        import yaml as _yaml

        def _openapi3_json():
            """ti4-OPENAPI: 返 OpenAPI 3.0 JSON 规范 (Content-Type: application/json)."""
            # flasgger 0.9.7.1 get_apispecs() 直接返 dict (非 list)
            # 内部用 current_app.url_map 收集 spec, 需要在 Flask app context 中调用.
            # 视图函数本身就在 app context 内, 所以这里直接调即可.
            spec2 = _swag.get_apispecs()
            spec3 = convert_swagger2_to_openapi3(spec2)
            body = _json.dumps(spec3, ensure_ascii=False, sort_keys=True)
            return app.response_class(
                response=body,
                status=200,
                mimetype='application/json',
            )

        def _openapi3_yaml():
            """ti4-OPENAPI: 返 OpenAPI 3.0 YAML 规范 (Content-Type: application/yaml)."""
            spec2 = _swag.get_apispecs()
            spec3 = convert_swagger2_to_openapi3(spec2)
            # allow_unicode=True 保中文, sort_keys=True 与 JSON 输出对齐便于 diff
            body = _yaml.safe_dump(spec3, allow_unicode=True, sort_keys=True)
            return app.response_class(
                response=body,
                status=200,
                mimetype='application/yaml',
            )

        # 直接 app.add_url_rule, 不走 csrf_protect / ogs_auth_token
        # (OpenAPI spec 是公开元数据, 无敏感信息)
        app.add_url_rule('/openapi.json', view_func=_openapi3_json, methods=['GET'])
        app.add_url_rule('/openapi.yaml', view_func=_openapi3_yaml, methods=['GET'])
        Log.logger.info('[ti4-OPENAPI] OpenAPI 3.0 spec enabled: /openapi.json, /openapi.yaml')

    # ti2-API: 标记初始化完成, 二次调用 orange_init_api() 直接 return (幂等)
    #   避免 pytest module-scope fixture 重入时 add_url_rule 触发 Flask assert.
    _orange_api_inited = True


if __name__ == "__main__":
    # REV20-P2-5-LOW-3: 启动横幅从 print 改为 Log.logger.info, 统一日志入口
    Log.logger.info('=' * 60)
    Log.logger.info('[INIT_DIAG] Starting OrangeServer...')
    Log.logger.info('=' * 60)

    # REV16 B9 HIGH-3: monkey.patch_all() 仅在真正启动 WSGIServer 时执行
    #   gevent 必须在 patch 之后才能接管 socket, 必须放在 main 块内启动前
    from gevent import monkey
    monkey.patch_all()

    orange_init_api()

    class DiagMiddleware:
        def __init__(self, app):
            self.app = app

        def __call__(self, environ, start_response):
            return self.app(environ, start_response)

    diag_app = DiagMiddleware(app)

    # REV16 B9 HIGH-1: argparse + fail-fast
    args = sta_cs()
    listen_host, listen_port = _validate_listen_addr(args.host, int(args.port))
    # REV20-P2-5-LOW-3: 启动信息从 print 改为 Log.logger.info
    Log.logger.info('[INIT_DIAG] Listening on %s:%s (OGS_ENV=%s)' % (
        listen_host, listen_port, _env('OGS_ENV', 'dev')))
    Log.logger.info('[INIT_DIAG] All routes registered. Starting WSGIServer on %s:%s' % (listen_host, listen_port))
    # REV16 B9 HIGH-1: WSGIServer 默认监听改为 127.0.0.1（防公网暴露）
    #   原：'0.0.0.0' 绑定所有网卡 → 若服务器有公网 IP 则服务直接暴露
    #   修复：默认 127.0.0.1（仅本地访问），须通过反向代理 (Nginx) 暴露
    #   argparse --host 0.0.0.0 仍可用（生产部署需要）, 但 dev 默认不开
    #   额外防御: 生产环境(OGS_ENV=prod) 且 host 仍为 0.0.0.0 时, fail-fast 阻断
    http_server = WSGIServer((listen_host, listen_port), application=diag_app, handler_class=WebSocketHandler)

    # REV43-H6 (R2-4-4): main 块加 atexit + signal 优雅关闭
    #   原: 直接 http_server.serve_forever(), SIGTERM/SIGINT 触发 KeyboardInterrupt
    #        后进程立即退出, DB session / Redis pool / 临时文件未被清理
    #   修: atexit 注册清理钩子, signal 触发优雅 stop (gevent.signal), 给 in-flight
    #       请求 5s 完成, 然后 server.close() + DB session remove
    def _graceful_shutdown():
        """REV43-H6: 优雅关闭钩子 - 关闭 WS server, 清理 DB/Redis session."""
        try:
            Log.logger.info('[INIT_DIAG] Graceful shutdown initiated (REV43-H6)...')
            http_server.stop(timeout=5)
        except Exception as e:
            Log.logger.error('[INIT_DIAG] server.stop error: %s' % e)
        try:
            # DB session 清理 - 防止未关闭事务跨进程持有连接
            from app.core.db.database import db
            db.session.remove()
        except Exception as e:
            Log.logger.error('[INIT_DIAG] db.session.remove error: %s' % e)
        try:
            # Redis pool 清理 (如已配置)
            from app.tools.at import _REDIS_POOL  # type: ignore
            if _REDIS_POOL is not None:
                _REDIS_POOL.disconnect()
        except Exception:
            pass
        Log.logger.info('[INIT_DIAG] Graceful shutdown complete.')

    import atexit
    atexit.register(_graceful_shutdown)

    # gevent.signal 注册 SIGTERM 处理器 (gevent 接管后 signal 必须用 gevent.signal)
    try:
        import signal
        from gevent import signal as gevent_signal
        gevent_signal(signal.SIGTERM, _graceful_shutdown)
        # SIGINT (Ctrl-C) 仍走 KeyboardInterrupt → serve_forever 退出 → atexit 兜底
    except Exception as e:
        Log.logger.warning('[INIT_DIAG] gevent.signal registration failed: %s' % e)

    http_server.serve_forever()
