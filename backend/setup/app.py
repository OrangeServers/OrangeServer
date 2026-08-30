# -*- coding: utf-8 -*-
"""setup / maintenance 两个最小 Flask app 工厂。

setup app：/setup/api/*（status 匿名，其余需 X-Setup-Token），业务前缀
catch-all 返 503 {setup_required: true} 供前端路由守卫识别。
maintenance app：只读错误页——把"配置坏了反复 crash-loop"变成可诊断状态。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, request

from setup import checks, envwrite, security, state
from setup.spa import register_spa

BUSINESS_PREFIXES = ('/local', '/account', '/server', '/mail', '/auth', '/ai')
ADMIN_MIN_PASSWORD = 8
MYSQL_ENV_FIELDS = {
    'host': 'OGS_MYSQL_HOST',
    'port': 'OGS_MYSQL_PORT',
    'dbname': 'OGS_MYSQL_DBNAME',
    'user': 'OGS_MYSQL_USER',
    'password': 'OGS_MYSQL_PASSWORD',
}
REDIS_ENV_FIELDS = {
    'host': 'OGS_REDIS_HOST',
    'port': 'OGS_REDIS_PORT',
    'password': 'OGS_REDIS_PASSWORD',
    'db': 'OGS_REDIS_DB',
}


def _detect_deployment() -> str:
    if Path('/.dockerenv').exists():
        return 'docker'
    try:
        cgroup = Path('/proc/1/cgroup').read_text(encoding='utf-8')
        if 'docker' in cgroup or 'containerd' in cgroup:
            return 'docker'
    except OSError:
        pass
    return 'bare'


def _guard():
    """写接口共同门禁：Origin 同源 + X-Setup-Token。返回错误响应或 None。"""
    if not security.same_origin(request):
        return jsonify({'ok': False, 'msg': '跨源请求被拒绝'}), 403
    token = request.headers.get('X-Setup-Token') or ''
    if not security.verify_token(token):
        return jsonify({'ok': False, 'msg': 'Setup Token 无效或已锁定，请稍后重试'}), 401
    return None


def _payload():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def _with_locked_env(payload, field_map):
    """Process-level deployment values are authoritative but never sent to the browser."""
    merged = dict(payload)
    for field, env_key in field_map.items():
        value = os.environ.get(env_key)
        if value:
            merged[field] = value
    return merged


def create_setup_app() -> Flask:
    app = Flask(__name__)
    security.ensure_token()

    @app.get('/local/health')
    def setup_health():
        # setup 是可服务状态：容器必须保持 healthy，反代才能让用户完成向导。
        return jsonify({'status': 'setup', 'setup_required': True}), 200

    @app.post('/local/app_auth_ck')
    def setup_auth_probe():
        # 前端路由守卫的模式探测是正常控制流，不应制造一条预期内的 503。
        return jsonify({'code': 100, 'setup_required': True}), 200

    @app.get('/setup/api/status')
    def setup_status():
        return jsonify({
            'mode': 'setup',
            'missing': state.missing_required(),
            'env_locked': state.env_locked_keys(),
            'deployment': _detect_deployment(),
            'token_file': str(state.token_path()),
        })

    @app.post('/setup/api/verify_token')
    def verify_token():
        err = _guard()
        if err:
            return err
        return jsonify({'ok': True})

    @app.get('/setup/api/prefill')
    def prefill():
        err = _guard()
        if err:
            return err
        env = os.environ
        return jsonify({
            'mysql': {
                'host': env.get('OGS_MYSQL_HOST', ''),
                'port': int(env.get('OGS_MYSQL_PORT') or 3306),
                'dbname': env.get('OGS_MYSQL_DBNAME', 'orange'),
                'user': env.get('OGS_MYSQL_USER', ''),
                'password_configured': bool(env.get('OGS_MYSQL_PASSWORD')),
            },
            'redis': {
                'host': env.get('OGS_REDIS_HOST', ''),
                'port': int(env.get('OGS_REDIS_PORT') or 6379),
                'db': int(env.get('OGS_REDIS_DB') or 0),
                'password_configured': bool(env.get('OGS_REDIS_PASSWORD')),
            },
        })

    @app.post('/setup/api/test_mysql')
    def test_mysql():
        err = _guard()
        if err:
            return err
        return jsonify(checks.test_mysql(_with_locked_env(_payload(), MYSQL_ENV_FIELDS)))

    @app.post('/setup/api/test_redis')
    def test_redis():
        err = _guard()
        if err:
            return err
        return jsonify(checks.test_redis(_with_locked_env(_payload(), REDIS_ENV_FIELDS)))

    @app.post('/setup/api/test_smtp')
    def test_smtp():
        err = _guard()
        if err:
            return err
        return jsonify(checks.test_smtp(_payload()))

    @app.post('/setup/api/apply')
    def apply():
        err = _guard()
        if err:
            return err
        # 幂等再校验：已配置系统绝不允许 apply
        if not state.should_enter_setup():
            return jsonify({'ok': False, 'msg': '系统已完成配置'}), 409
        payload = _payload()
        result, status_code = _orchestrate_apply(payload)
        return jsonify(result), status_code

    @app.route('/setup/api/<path:_rest>', methods=['GET', 'POST', 'PUT', 'DELETE'])
    def setup_unknown(_rest):
        return jsonify({'ok': False, 'msg': 'unknown setup endpoint'}), 404

    _register_business_catchall(app)
    register_spa(app)
    return app


def _orchestrate_apply(payload):
    """校验 → 生成密钥 → 复测连接 → bootstrap_db 子进程 → 写 runtime.env
    → 哨兵/删 token → 延迟自杀重启。步骤 1-4 任一失败不落任何文件。"""
    steps = []

    def fail(msg, extra_steps=None):
        return {'ok': False, 'msg': msg, 'steps': steps + (extra_steps or [])}, 400

    mysql = _with_locked_env(payload.get('mysql') or {}, MYSQL_ENV_FIELDS)
    redis_cfg = _with_locked_env(payload.get('redis') or {}, REDIS_ENV_FIELDS)
    admin = payload.get('admin') or {}
    secrets_cfg = payload.get('secrets') or {}

    username = str(admin.get('username') or '').strip()
    password = str(admin.get('password') or '')
    if not username or len(password) < ADMIN_MIN_PASSWORD:
        return fail('管理员用户名必填，密码至少 %d 位' % ADMIN_MIN_PASSWORD)
    if username == 'system':
        return fail('system 为内置保留账号')

    secret_key = str(secrets_cfg.get('secret_key') or '') or security.generate_secret_key()
    fernet_key = str(secrets_cfg.get('fernet_key') or '') or security.generate_fernet_key()
    if len(secret_key) < state.SECRET_MIN_LEN or any(
        bad in secret_key.lower() for bad in state.SECRET_INSECURE_SUBSTR
    ):
        return fail('自定义 SECRET_KEY 不满足强度要求（≥32 字符且非占位符）')
    steps.append({'name': 'secrets', 'ok': True, 'msg': '安全密钥就绪'})

    # 不信任前端"已测过"，服务端复测
    mysql_result = checks.test_mysql(mysql)
    steps.append({'name': 'mysql', 'ok': mysql_result['ok'], 'msg': mysql_result['msg']})
    if not mysql_result['ok']:
        return fail('MySQL 连接复测失败')
    redis_result = checks.test_redis(redis_cfg)
    steps.append({'name': 'redis', 'ok': redis_result['ok'], 'msg': redis_result['msg']})
    if not redis_result['ok']:
        return fail('Redis 连接复测失败')
    mail = payload.get('mail') or {}
    if mail:
        smtp_result = checks.test_smtp(mail)
        steps.append({
            'name': 'smtp',
            'ok': smtp_result['ok'],
            'msg': smtp_result['msg'],
        })
        if not smtp_result['ok']:
            return fail('SMTP 连接复测失败')

    candidate = {
        'OGS_MYSQL_HOST': str(mysql.get('host') or ''),
        'OGS_MYSQL_PORT': str(mysql.get('port') or '3306'),
        'OGS_MYSQL_DBNAME': str(mysql.get('dbname') or 'orange'),
        'OGS_MYSQL_USER': str(mysql.get('user') or ''),
        'OGS_MYSQL_PASSWORD': str(mysql.get('password') or ''),
        'OGS_REDIS_HOST': str(redis_cfg.get('host') or ''),
        'OGS_REDIS_PORT': str(redis_cfg.get('port') or '6379'),
        'OGS_REDIS_PASSWORD': str(redis_cfg.get('password') or ''),
        'OGS_REDIS_DB': str(redis_cfg.get('db') or '0'),
        'OGS_FLASK_SECRET_KEY': secret_key,
        'OGS_FERNET_KEYS': fernet_key,
    }

    # bootstrap 子进程：候选 env 注入（进程 env 里的空串会挡住 dotenv，
    # 这里显式覆盖为候选值），参数走 stdin
    child_env = dict(os.environ)
    child_env.update({k: v for k, v in candidate.items() if v})
    child_input = json.dumps({
        'admin': {'username': username, 'password': password,
                  'email': str(admin.get('email') or '').strip()},
        'settings': payload.get('settings') or {},
        'mail': mail,
    })
    try:
        proc = subprocess.run(
            [sys.executable, '-m', 'setup.bootstrap_db'],
            input=child_input, capture_output=True, text=True,
            env=child_env, cwd=str(state.BACKEND_DIR), timeout=120,
        )
    except subprocess.TimeoutExpired:
        return fail('数据库初始化超时（120s）')
    try:
        bootstrap = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        bootstrap = {}
    steps.extend(bootstrap.get('steps') or [])
    if proc.returncode != 0 or not bootstrap.get('ok'):
        detail = (proc.stderr or '').strip().splitlines()
        return fail('数据库初始化失败' + ('：%s' % detail[-1][:200] if detail else ''))

    # 全部通过才落盘
    env_path = envwrite.write_runtime_env(candidate)
    steps.append({'name': 'write_env', 'ok': True, 'msg': '配置已写入 %s' % env_path})
    state.mark_configured()
    security.drop_token()
    steps.append({'name': 'finalize', 'ok': True, 'msg': '即将重启后端加载新配置'})

    def _suicide():
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Timer(1.0, _suicide).start()
    return {'ok': True, 'steps': steps}, 200


def _register_business_catchall(app: Flask) -> None:
    def unavailable(_rest=''):
        return jsonify({
            'code': 100,
            'msg': '系统尚未完成初始化配置',
            'setup_required': True,
        }), 503

    for index, prefix in enumerate(BUSINESS_PREFIXES):
        app.add_url_rule(
            '%s/<path:_rest>' % prefix, 'setup_catchall_%d' % index,
            unavailable, methods=['GET', 'POST', 'PUT', 'DELETE'],
        )
        app.add_url_rule(
            prefix, 'setup_catchall_root_%d' % index,
            unavailable, methods=['GET', 'POST', 'PUT', 'DELETE'],
        )


def create_maintenance_app(error: BaseException | None = None) -> Flask:
    app = Flask(__name__)
    summary = '%s: %s' % (type(error).__name__, str(error)[:300]) if error else '配置不完整'

    @app.get('/setup/api/status')
    def maintenance_status():
        return jsonify({
            'mode': 'maintenance',
            'missing': state.missing_required(),
            'error': summary,
            'hint': '检查 runtime.env 与部署环境变量后重启；救援通道 OGS_SETUP_MODE=force',
        })

    def unavailable(_rest=''):
        return jsonify({
            'code': 100,
            'msg': '后端启动失败，处于维护模式：%s' % summary,
        }), 503

    for index, prefix in enumerate(BUSINESS_PREFIXES):
        app.add_url_rule(
            '%s/<path:_rest>' % prefix, 'maint_catchall_%d' % index,
            unavailable, methods=['GET', 'POST', 'PUT', 'DELETE'],
        )
    register_spa(app)
    return app
