"""AI 运维入口替换未完成容器功能的静态回归测试。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_unfinished_container_feature_is_not_reachable():
    local_api = _read("backend/app/api/local_api.py")
    router = _read("frontend/src/router/index.ts")
    layout = _read("frontend/src/views/Layout.vue")
    api = _read("frontend/src/api/index.ts")

    assert "ContainerImage" not in local_api
    assert "/container/" not in local_api
    assert "container-image" not in router
    assert "容器管理" not in layout
    assert "getContainerImageList" not in api


def test_ai_agent_navigation_is_operator_only():
    router = _read("frontend/src/router/index.ts")
    layout = _read("frontend/src/views/Layout.vue")

    assert "path: 'ai-agent'" in router
    assert "path: 'ai-ops'" in router
    assert "path: 'ai-knowledge'" in router
    assert "'/ai-ops', '/ai-knowledge', '/ai-agent', '/ai-runs', '/batch-command'" in router
    assert "path: 'ai-agent', redirect: { name: 'AiOpsWorkbench' }" in router
    assert "path: 'ai-runs', redirect: { name: 'AiOpsTasks' }" in router
    assert "['admin', 'user'].includes(role)" in router
    assert 'index="/ai-ops"' in layout
    assert 'index="/ai-knowledge"' in layout
    assert "isAdmin || isUser" in layout


def test_ai_proxy_is_streaming_ready():
    vite = _read("frontend/vite.config.ts")
    physical_nginx = _read("deploy/nginx/orange_server.conf")
    container_nginx = _read("deploy/nginx/frontend_container.conf")

    assert "'^/ai(?:/|$)'" in vite
    assert "'/container'" not in vite
    for config in (physical_nginx, container_nginx):
        assert "location /ai/" in config
        assert "proxy_buffering off" in config
        assert "proxy_read_timeout 600s" in config


def test_dashboard_no_longer_exposes_container_count():
    backend = _read("backend/app/local/Basics.py")
    dashboard = _read("frontend/src/views/Dashboard.vue")

    assert "container_len" not in backend
    assert "container_len" not in dashboard
    assert "getAiProviders" in dashboard
    assert "route: '/ai-ops'" in dashboard
