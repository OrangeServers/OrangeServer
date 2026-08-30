# -*- coding: utf-8 -*-
"""
pytest 共享配置 + fixtures

目的: 让所有 REV16 P0 HIGH 回归测试能在不连真实 DB / Redis / SSH 的前提下运行。

关键设计:
- 严格隔离: 所有外部依赖 (SQLAlchemy / Redis / paramiko) 用 monkeypatch 替换
- 可重复: 每个测试 fresh env, 不残留状态
- 离线: 任何 'OGS_*' 环境变量都被 env-clean fixture 隔离
"""

import os
import sys
import pytest
from unittest.mock import MagicMock


# 让 backend/app 变成可导入包
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TEST_ROOT = os.path.dirname(os.path.abspath(__file__))
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)


# =============================================================================
# 模块加载阶段 (阶段 1): 注入合法 OGS_* 默认值, 让后续 import 的 config.py / db 模块不 fail-fast
# =============================================================================
# 背景: 业务模块 (cron.py / ssh / assets 等) 在被 import 时会触发:
#   1) from app.core.config import ...  -> REV16 B8 fail-fast (OGS_MYSQL_USER/PASSWORD/HOST)
#   2) with app.app_context(): db.create_all()  -> 真实连 MySQL
#   3) load_cron_from_db()  -> t_cron.query.filter_by().all()  -> 真实连 MySQL
#
# 必须在 conftest 模块加载时 (autouse fixture 之前, 业务模块 import 之前)
# (a) 设合法 OGS_* 默认值, 让 config.py 顺利加载;
# (b) 预加载 app.core.db.settings, 并把 db.create_all 替换为 no-op;
# (c) 把 app.core.db.database.t_cron.query.filter_by 替换为 no-op, 让 load_cron_from_db 不查 DB。
#
# 用 os.environ.setdefault (而非 monkeypatch.setenv) 是因为这里发生在 fixture
# 框架启动前, monkeypatch 不可用. 这不会污染其他测试, 因为:
#   - autouse _ensure_valid_config_env 会在每个测试函数前重新 setdefault
#   - clean_env / valid_env fixture 可显式 delenv 测试 fail-fast 行为
for _k, _v in {
    'OGS_MYSQL_USER': 'test_user',
    'OGS_MYSQL_PASSWORD': 'test_pwd_strong_123',
    'OGS_MYSQL_HOST': '127.0.0.1',
    'OGS_MAIL_USER': 'test@example.com',
    'OGS_MAIL_PASSWORD': 'real_mail_pwd',
    'OGS_MAIL_SMTP': 'smtp.gmail.com',  # REV48: 真实邮件服务器域名, 不触发 OGS_MAIL_SMTP=smtp.example.com 占位符 fail-fast
    'OGS_FLASK_SECRET_KEY': 'test_secret_key_with_enough_entropy_xyz_12345',
    'OGS_ENV': 'dev',
    'OGS_REDIS_HOST': '127.0.0.1',
    'OGS_PROXY_LAYERS': '1',
}.items():
    os.environ.setdefault(_k, _v)


# =============================================================================
# 模块加载阶段 (阶段 2): 创建必要的 data 目录, 避免业务模块 import 时 FileNotFoundError
# =============================================================================
# 背景: app/tools/at.py 顶层 `Log = FileLogger()` 会创建
# TimedRotatingFileHandler(filename=FILE_CONF['log_path'] + 'ogsbackend.log'),
# 若目录不存在则报 FileNotFoundError 阻断 import.
# 必须在 conftest 模块加载时 (autouse fixture 之前) 创建.
# 注意: 不能硬编码 D:\ 路径, 在 Linux CI runner 上 D: 盘不存在会导致 makedirs
# 创到 /D:\code\... 这种非路径, 让 FileLogger 后续 hang 或抛 FileNotFoundError.
# 改为从 ROOT (项目根) 动态推导, 跨 Windows/Linux 通用.
_DATA_DIR = os.path.join(ROOT, 'data')
for _sub in ('log', 'file', 'key', 'img', 'image', os.path.join('image', 'temp')):
    os.makedirs(os.path.join(_DATA_DIR, _sub), exist_ok=True)


# =============================================================================
# 模块加载阶段 (阶段 3): 预加载 db 模块, 把 db.create_all / t_cron.query 替换为 no-op
# =============================================================================
# 这一步必须在任何业务模块 (尤其 app.cron.cron) 被 import 前完成.
# 思路: 主动 import, 拿到 db 对象, monkey-patch 它的实例方法;
#       之后 import 的任何模块都从 sys.modules 拿同一个 db 对象, 因此 patch 生效.
try:
    from app.core.db.settings import db as _db
    _db.create_all = lambda *a, **kw: None
    _db.drop_all = lambda *a, **kw: None
    _db.session.execute = lambda *a, **kw: None
    _db.session.commit = lambda *a, **kw: None
    _db.session.rollback = lambda *a, **kw: None
    _db.session.add = lambda *a, **kw: None
    _db.session.flush = lambda *a, **kw: None
    _db.create_engine = lambda *a, **kw: None
except Exception as _e:
    # 若 import 链中某环节仍 fail (e.g. 缺 OGS_*), 让后续 autouse 修复
    pass

# 预加载 database 模块并 patch t_cron.query 链, 让 load_cron_from_db() 不连 MySQL
try:
    import app.core.db.database as _db_mod
    for _tbl_name in ('t_cron', 't_cron_host', 't_cron_group',
                      't_host', 't_sys_user', 't_acc_user',
                      't_auth_host', 't_auth_host_user', 't_auth_host_host_group'):
        if hasattr(_db_mod, _tbl_name):
            _tbl = getattr(_db_mod, _tbl_name)
            try:
                _tbl.query = MagicMock()
                _tbl.query.filter_by = MagicMock(return_value=MagicMock(
                    all=MagicMock(return_value=[]),
                    first=MagicMock(return_value=None),
                    count=MagicMock(return_value=0),
                ))
                _tbl.query.filter = MagicMock(return_value=MagicMock(
                    all=MagicMock(return_value=[]),
                    first=MagicMock(return_value=None),
                    count=MagicMock(return_value=0),
                ))
            except Exception:
                pass
except Exception as _e:
    pass


# =============================================================================
# autouse fixture: 保证所有测试可加载 app.core.config (避开 REV16 B8 fail-fast)
# =============================================================================
@pytest.fixture(autouse=True)
def _ensure_valid_config_env(monkeypatch):
    """预填合法的 OGS_* 环境变量, 避免 REV16 B8 fail-fast 影响无关测试。

    仅当 env 未设置时 setdefault, 不覆盖测试自己 setenv 的合法值。
    """
    defaults = {
        'OGS_MYSQL_USER': 'test_user',
        'OGS_MYSQL_PASSWORD': 'test_pwd_strong_123',
        'OGS_MYSQL_HOST': '127.0.0.1',
        'OGS_MAIL_USER': 'test@example.com',
        'OGS_MAIL_PASSWORD': 'real_mail_pwd',
        'OGS_MAIL_SMTP': 'smtp.gmail.com',  # REV48: 真实邮件服务器域名, 不触发 OGS_MAIL_SMTP=smtp.example.com 占位符 fail-fast
        'OGS_FLASK_SECRET_KEY': 'test_secret_key_with_enough_entropy_xyz_12345',
        'OGS_ENV': 'dev',
        'OGS_PROXY_LAYERS': '1',
    }
    for k, v in defaults.items():
        if k not in os.environ:
            monkeypatch.setenv(k, v)
    return monkeypatch


@pytest.fixture(autouse=True)
def _lower_bcrypt_rounds(monkeypatch):
    """测试环境降 bcrypt rounds 到 4 (默认 12 需 250ms+ / 单 hash, 慢 CI 挂死)。

    背景: basesec._BCRYPT_ROUNDS 默认 12 (生产推荐), 但 CI runner (GitHub
    ubuntu-latest free tier) 跑全量 100+ 密码测试叠加 = 拉满 15min timeout.
    4 rounds 足以验证 hash_pwd 行为, 不验证“产压测”.
    仅在 bcrypt 模块已加载且 _BCRYPT_ROUNDS 可被 setattr 时生效.
    """
    try:
        import app.tools.basesec as _bs
        monkeypatch.setattr(_bs, '_BCRYPT_ROUNDS', 4)
    except Exception:
        pass
    return monkeypatch


@pytest.fixture(autouse=True)
def _ensure_log_directories_marker():
    """已迁移到 conftest 顶层 (模块级 makedirs)。

    保留空 fixture 仅为兼容可能依赖它的测试, 无副作用。
    """
    yield


@pytest.fixture
def clean_env(monkeypatch):
    """清除所有 OGS_* / FLASK_SECRET_KEY 等敏感环境变量, 测试可控制.

    REV16 B8 HIGH-1/2/3 修复后, config.py 启动时会 fail-fast 检测占位符/默认值.
    因此单元测试必须在干净 env 下手动 set 合法值, 或验证 raise RuntimeError 行为.
    """
    keys_to_clean = [
        'OGS_ENV', 'OGS_MYSQL_USER', 'OGS_MYSQL_PASSWORD', 'OGS_MYSQL_HOST',
        'OGS_MAIL_USER', 'OGS_MAIL_PASSWORD', 'OGS_MAIL_SMTP',
        'OGS_FLASK_SECRET_KEY', 'OGS_REDIS_HOST',
        'FLASK_SECRET_KEY', 'MYSQL_PASSWORD', 'MAIL_PASSWORD',
    ]
    for key in keys_to_clean:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def prod_env(clean_env):
    """模拟生产环境: OGS_ENV=prod, 但其他凭据仍缺失."""
    clean_env.setenv('OGS_ENV', 'prod')
    return clean_env


@pytest.fixture
def valid_env(clean_env):
    """配置所有合法凭据, 让 config.py 顺利加载."""
    clean_env.setenv('OGS_MYSQL_USER', 'test_user')
    clean_env.setenv('OGS_MYSQL_PASSWORD', 'test_pwd_strong')
    clean_env.setenv('OGS_MYSQL_HOST', '127.0.0.1')
    clean_env.setenv('OGS_MAIL_USER', 'test@example.com')
    clean_env.setenv('OGS_MAIL_PASSWORD', 'real_mail_pwd')
    clean_env.setenv('OGS_MAIL_SMTP', 'smtp.example.com')
    clean_env.setenv(
        'OGS_FLASK_SECRET_KEY',
        'real_random_secret_key_with_enough_entropy_xyz_123456789'
    )
    clean_env.setenv('OGS_ENV', 'dev')
    return clean_env


@pytest.fixture
def flask_request_ctx(monkeypatch):
    """为测试提供真实的 Flask request + app context.

    背景: 用 mock 的 _FakeRequest 替换 flask.request 不会作用到
    `from flask import request` 已绑定的 LocalProxy, 且 PUTUserImage.init 走
    request.files / request.values 会被 werkzeug 检测到
    "Working outside of request context" 抛错.

    修复: 用 app.test_request_context() 真的 push context, 返回一个 _set 函数
    可动态修改 request.values / request.files 中的参数。
    """
    # 确保 app.app_factory 被加载（可能尚末初始化）
    try:
        from app.app_factory import app
    except Exception:
        # 若 app_factory 也依赖 MySQL, 提供一个 fallback mock app
        from flask import Flask as _F
        app = _F(__name__)
        # 注册 app 以使 current_app 可用
        import sys as _sys
        _sys.modules.setdefault('app.app_factory', _sys.modules[__name__])
        import app.app_factory as _af
        _af.app = app

    # 把 request.values 存为可修改 dict
    state = {'values': {}, 'files': {}}

    # 模拟 werkzeug 的 MultiDict (request.values 是 CombinedMultiDict([args, form]))
    class _FakeMultiDict(dict):
        def get(self, key, default=None, type=None):
            v = super().get(key, default)
            if type and v is not None and not isinstance(v, type):
                try:
                    v = type(v)
                except (ValueError, TypeError):
                    return default
            return v

        def getlist(self, key):
            v = super().get(key)
            if v is None:
                return []
            return v if isinstance(v, list) else [v]

    # push real app context
    ctx = app.test_request_context()
    ctx.push()

    def _set(values=None, files=None):
        if values:
            state['values'].update(values)
        if files:
            state['files'].update(files)
        # 同步到 request.values / request.files
        from flask import request as _real_req
        for k, v in state['values'].items():
            _real_req.values.set(k, v) if hasattr(_real_req.values, 'set') else None
        # 退到 _FakeMultiDict 并覆盖
        if values:
            _real_req.values = _FakeMultiDict(state['values'])
        if files:
            # request.files 是 FileMultiDict, 我们只用 .get('file') -> None
            _real_req.files = _FakeMultiDict(state['files'])

    yield _set
    ctx.pop()


@pytest.fixture(autouse=True)
def cron_scheduler_skip(monkeypatch):
    """阻止 APScheduler + SQLAlchemy create_all 在 import 业务模块时触发。

    背景: app/cron/cron.py 顶层有 `scheduler.start()` 和 `db.create_all()`,
    都会在 import 时执行:
      - scheduler.start()  -> SchedulerAlreadyRunningError
      - db.create_all()    -> 真实连 MySQL (ConnectionRefusedError)
    修复: 在 import 前同时 patch 这两个调用为 no-op。
    """
    # 1. patch APScheduler.start
    from apscheduler.schedulers.background import BackgroundScheduler
    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self: None)
    # 2. patch SQLAlchemy db.create_all
    from sqlalchemy.orm import scoped_session
    # 尝试从 app.core.db.settings 或 app.core.db.database 拿 db 对象
    # 不依赖其存在 (可能未加载), 走全局 patch
    import sqlalchemy
    if hasattr(sqlalchemy, 'create_all'):
        monkeypatch.setattr(sqlalchemy, 'create_all', lambda *a, **kw: None)
    # 3. patch 可能已加载的 db 对象
    import sys
    for mod_name in ('app.core.db.settings', 'app.core.db.database', 'app.core.db.insert'):
        if mod_name in sys.modules:
            mod = sys.modules[mod_name]
            if hasattr(mod, 'db'):
                monkeypatch.setattr(mod.db, 'create_all', lambda *a, **kw: None)
            if hasattr(mod, 'osql_in'):
                monkeypatch.setattr(mod, 'osql_in', lambda *a, **kw: None)
                monkeypatch.setattr(mod, 'osql_up', lambda *a, **kw: None)
    return monkeypatch


@pytest.fixture
def fake_db(monkeypatch):
    """mock SQLAlchemy model query 用于 cron / SysUser / SSH host 权限等场景.

    用法:
        def test_x(fake_db):
            fake_db.set('t_cron', [{'id': 1, 'job_owner': 'alice', ...}])
            # 可选第三参数: filter_kwargs 用于精确匹配 (作为 rows 索引提示)
            ...
    """
    state = {'tables': {}}

    def set_table(name, rows, **_filter):
        state['tables'][name] = rows

    def get_query(table_name):
        rows = state['tables'].get(table_name, [])

        class _Q:
            def __init__(self, rs):
                self._rs = rs

            def filter_by(self, **kw):
                # 简化: 把 filter_by 当作 transparent, 返回所有行
                # 调用方应该预先 set 过滤后的 rows
                return _Q(self._rs)

            def filter(self, *a, **kw):
                return _Q(self._rs)

            def all(self):
                return self._rs

            def first(self):
                return self._rs[0] if self._rs else None

            def count(self):
                return len(self._rs)

            def offset(self, n):
                return _Q(self._rs)

            def limit(self, n):
                return _Q(self._rs)

            def order_by(self, *a, **kw):
                return _Q(self._rs)

            def with_entities(self, *a, **kw):
                return _Q(self._rs)

            def update(self, *a, **kw):
                return 1

            def delete(self):
                return 1

        return _Q(rows)

    for name in (
        't_host', 't_sys_user', 't_auth_host', 't_auth_host_user',
        't_auth_host_user_group', 't_auth_host_host_group', 't_acc_user',
        't_cron', 't_cron_host', 't_cron_group',
    ):
        mock_cls = MagicMock()
        mock_cls.query = MagicMock()
        mock_cls.query.filter_by = MagicMock(
            side_effect=lambda **kw: get_query(name)
        )
        mock_cls.query.filter = MagicMock(
            side_effect=lambda *a, **kw: get_query(name)
        )
        # 同时 patch database 模块和已加载的业务模块 (cron / ssh / assets)
        monkeypatch.setattr(f'app.core.db.database.{name}', mock_cls)
        # 仅 patch 已加载的业务模块 (避免 ModuleNotFoundError)
        for mod_path in (
            'app.cron.cron', 'app.cron.CronSettings',
            'app.ssh.webssh', 'app.ssh.sftp',
            'app.assets.SysUser', 'app.assets.ServerManagement',
            'app.local.Basics', 'app.local.download',
        ):
            if mod_path in sys.modules:
                try:
                    monkeypatch.setattr(f'{mod_path}.{name}', mock_cls)
                except (AttributeError, KeyError):
                    # 该表不在该模块中
                    pass
    return type('DB', (), {'set': set_table, 'state': state})()
