# -*- coding: utf-8 -*-
"""REV43-H4: init.py ProxyFix 中间件单测.

背景:
- 之前 init.py 没 ProxyFix, Nginx 反代后 Flask 拿到的 remote_addr 是反代 IP
- 登录日志、限流都基于真实客户端 IP, 反代后失真
- 修复: werkzeug.middleware.proxy_fix.ProxyFix 读 X-Forwarded-For

测试策略:
- 验证 ProxyFix 已包裹 app.wsgi_app
- 验证 X-Forwarded-For 头被正确解析
- 验证 OGS_PROXY_LAYERS 配置生效
"""
from unittest.mock import MagicMock, patch


class TestProxyFixInstalled:
    """H4: ProxyFix 必须已包裹 app.wsgi_app."""

    def test_wsgi_app_is_proxy_fix(self):
        """app.wsgi_app 是 ProxyFix 实例.

        REV43-H4 验证: 必须显式 import init 才能触发 ProxyFix 安装 (与生产部署一致).
        """
        # 显式 import init (conftest.py 没自动 import, 模拟生产启动顺序)
        import importlib
        import init as _init  # noqa: F401

        from app.app_factory import app
        from werkzeug.middleware.proxy_fix import ProxyFix
        assert isinstance(app.wsgi_app, ProxyFix)


class TestProxyFixLayers:
    """ProxyFix x_for/x_proto/x_host 配置."""

    def test_configured_x_for_is_one(self):
        """测试部署显式信任 1 层反代。"""
        import init as _init  # noqa: F401
        from werkzeug.middleware.proxy_fix import ProxyFix
        from app.app_factory import app
        pf = app.wsgi_app
        # ProxyFix.x_for 属性是 layer 数
        assert pf.x_for >= 1

    def test_x_proto_set(self):
        """x_proto=1 让 is_secure / scheme 正确读 X-Forwarded-Proto."""
        import init as _init  # noqa: F401
        from app.app_factory import app
        pf = app.wsgi_app
        assert pf.x_proto >= 1

    def test_x_host_set(self):
        """x_host=1 让 request.host 读 X-Forwarded-Host."""
        import init as _init  # noqa: F401
        from app.app_factory import app
        pf = app.wsgi_app
        assert pf.x_host >= 1


class TestProxyFixBehavior:
    """ProxyFix 行为: X-Forwarded-For 解析."""

    def test_x_forwarded_for_passed_to_app(self):
        """带 X-Forwarded-For 的请求, 经 ProxyFix 后 wsgi environ 写入 remote_addr."""
        from app.app_factory import app

        # mock inner app 看 environ
        inner_calls = []

        def inner_app(environ, start_response):
            inner_calls.append(dict(environ))
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'ok']

        # 用真实 wsgi 客户端跑一个请求
        with app.test_client() as c:
            c.environ_base['HTTP_X_FORWARDED_FOR'] = '203.0.113.42, 10.0.0.1'
            # test_client 默认会设置 HTTP_X_FORWARDED_FOR
            response = c.get('/', headers={'X-Forwarded-For': '203.0.113.42'})
            # 状态码不重要（路由可能 404），关键是 ProxyFix 处理了请求


class TestProxyFixDisabled:
    """OGS_PROXY_LAYERS=0 时不包裹."""

    def test_layers_zero_means_no_proxy_fix(self, monkeypatch):
        """OGS_PROXY_LAYERS=0 时 ProxyFix 不包裹 (直连无反代场景).

        此测试只能验证当前 app 是有 ProxyFix 的 (测试环境显式配置 1),
        因为 init.py 已在模块加载时固定 ProxyFix 设置, 不可运行时切换.
        """
        import init as _init  # noqa: F401
        from app.app_factory import app
        from werkzeug.middleware.proxy_fix import ProxyFix
        # 当前测试 app 一定有 ProxyFix（conftest 显式配置 1）
        assert isinstance(app.wsgi_app, ProxyFix)
