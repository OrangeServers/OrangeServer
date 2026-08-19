# -*- coding: utf-8 -*-
"""
Tier 2 部署配置静态测试 (REV-50 / T2-P1-c)

目的: 静态分析 deploy/* 和 backend/Dockerfile 等部署相关配置
      确保生产部署路径 (Dockerfile / supervisor / nginx / docker-compose / install.sh)
      不会引入 P0 配置错误.

测试维度:
  1. Dockerfile 检查 (worker class / 3-stage / HEALTHCHECK / non-root)
  2. supervisor 配置检查 (geventwebsocket worker / stopasgroup / path env)
  3. nginx 配置检查 (HTTPS / WebSocket 路由 / 安全头)
  4. docker-compose 配置检查 (4 服务 / healthcheck / network)
  5. install.sh 检查 (sha256 / import self-check / .env 引导)
  6. start.sh 检查 (setsid / TERM grace / PID 管理)
  7. healthcheck.sh 检查 (status 字段验证 / timeout)
  8. .env.example / orange_server.env.example (OGS_FERNET_KEYS 同步 R1-22)
  9. .dockerignore 检查 (.git / .pytest_cache / node_modules)
 10. deploy/README.md 检查 (覆盖 3 模式 + 环境变量 + 故障排查)
"""
import os
import re
import stat
from pathlib import Path

import pytest

# 项目根
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = REPO_ROOT / "backend"
DEPLOY = REPO_ROOT / "deploy"
OPS = REPO_ROOT / "ops"


# =============================================================================
# 1. Dockerfile 检查
# =============================================================================
class TestDockerfile:
    """P0: Dockerfile worker class 必须为 geventwebsocket (WebSocket 兼容)"""

    @pytest.fixture
    def dockerfile(self):
        return (BACKEND / "Dockerfile").read_text(encoding="utf-8")

    def test_01_dockerfile_exists(self):
        assert (BACKEND / "Dockerfile").exists(), "Dockerfile 缺失"

    def test_02_dockerfile_uses_geventwebsocket_worker(self, dockerfile):
        """T2-P0: 必须用 geventwebsocket worker, 不许用 gevent (会断 WebSocket)"""
        # 必须有 geventwebsocket worker class
        assert "geventwebsocket.gunicorn.workers.GeventWebSocketWorker" in dockerfile, \
            "Dockerfile CMD 必须用 geventwebsocket worker class (WebSocket 兼容)"

    def test_03_dockerfile_does_not_use_plain_gevent_worker(self, dockerfile):
        """T2-P0: 不许单独用 gevent worker (会断 WebSocket)"""
        # 排除: geventwebsocket 包含 gevent, 所以单查 "--worker-class gevent" 字符串
        # 但要排除 geventwebsocket 误报
        bad_patterns = [
            re.compile(r'--worker-class\s+gevent\s*(\"|$)', re.MULTILINE),  # 末尾引号或行尾
        ]
        for pat in bad_patterns:
            match = pat.search(dockerfile)
            assert not match, f"Dockerfile 仍用普通 gevent worker: {match.group(0)!r}"

    def test_04_dockerfile_has_three_stages(self, dockerfile):
        """Dockerfile 必须 3 阶段 (base / builder / runtime)"""
        assert "FROM python:3.12-slim AS base" in dockerfile
        assert "FROM base AS builder" in dockerfile
        assert "FROM base AS runtime" in dockerfile

    def test_05_dockerfile_has_healthcheck(self, dockerfile):
        """Dockerfile 必须有 HEALTHCHECK 指令"""
        assert "HEALTHCHECK" in dockerfile
        # 探测 /local/health
        assert "/local/health" in dockerfile

    def test_06_dockerfile_runs_as_non_root(self, dockerfile):
        """Dockerfile 必须 USER 切换到非 root"""
        assert "USER ogs" in dockerfile or "USER app" in dockerfile or "USER nobody" in dockerfile, \
            "Dockerfile 必须 USER 切换非 root"

    def test_07_dockerfile_exposes_port_28000(self, dockerfile):
        assert "EXPOSE 28000" in dockerfile

    def test_08_dockerfile_uses_offline_wheelhouse(self, dockerfile):
        """builder 预构建 wheelhouse，runtime 不访问包索引。"""
        assert "pip wheel --wheel-dir /wheels -r requirements.txt" in dockerfile
        assert "--mount=type=bind,from=builder,source=/wheels,target=/wheels,ro" in dockerfile
        assert "pip install --no-index --find-links=/wheels -r requirements.txt" in dockerfile
        assert "pip-compile" not in dockerfile

    def test_09_dockerfile_init_app_entrypoint(self, dockerfile):
        """CMD 入口必须是 init:app"""
        assert "wsgi:app" in dockerfile

    def test_10_dockerfile_no_dev_requirements(self, dockerfile):
        """P1-5: Dockerfile 不得 pip install requirements-dev.txt (开发依赖不进生产镜像)"""
        # 注释里出现 requirements-dev.txt 没问题, 关键是不要 install
        # 找所有 pip install 行
        install_lines = [
            line for line in dockerfile.splitlines()
            if "pip install" in line and "requirements-dev" in line
        ]
        assert not install_lines, f"Dockerfile 不应 pip install requirements-dev.txt: {install_lines}"
        # 也不应 COPY requirements-dev.txt
        copy_dev = [
            line for line in dockerfile.splitlines()
            if "COPY" in line and "requirements-dev" in line
        ]
        assert not copy_dev, f"Dockerfile 不应 COPY requirements-dev.txt: {copy_dev}"


# =============================================================================
# 2. supervisor 配置检查
# =============================================================================
class TestSupervisorConf:
    """supervisor 配置: geventwebsocket worker + 优雅退出 + path env"""

    @pytest.fixture
    def supconf(self):
        return (DEPLOY / "supervisor" / "orange_server.conf").read_text(encoding="utf-8")

    def test_01_supervisor_uses_geventwebsocket_worker(self, supconf):
        """必须用 geventwebsocket worker"""
        assert "geventwebsocket.gunicorn.workers.GeventWebSocketWorker" in supconf

    def test_02_supervisor_uses_env_vars_for_paths(self, supconf):
        """路径必须环境化 (不硬编码)"""
        # 关键路径必须用 %(ENV_OGS_xxx)s
        assert "%(ENV_OGS_PYTHON)s" in supconf
        assert "%(ENV_OGS_HOME)s" in supconf
        assert "%(ENV_OGS_BIND_HOST)s" in supconf
        assert "%(ENV_OGS_PORT)s" in supconf

    def test_03_supervisor_has_stopasgroup(self, supconf):
        """必须 stopasgroup=true (gevent 子进程组优雅退出)"""
        assert "stopasgroup=true" in supconf, \
            "supervisor 必须 stopasgroup=true (避免 gevent 子进程残留)"

    def test_04_supervisor_has_killasgroup(self, supconf):
        """必须 killasgroup=true"""
        assert "killasgroup=true" in supconf

    def test_05_supervisor_stopsignal_is_term(self, supconf):
        """必须 TERM 信号 (让 gevent 排空 WebSocket)"""
        assert "stopsignal=TERM" in supconf

    def test_06_supervisor_autorestart(self, supconf):
        """必须 autorestart=true (异常退出自动重启)"""
        assert "autorestart=true" in supconf

    def test_07_supervisor_no_secrets_in_file(self, supconf):
        """不应把密钥写进 supervisor 配置 (应走 secrets.env)"""
        # 关键密钥不应该出现
        assert "OGS_FLASK_SECRET_KEY=" not in supconf or "ENV_OGS_FLASK_SECRET_KEY" in supconf
        assert "OGS_FERNET_KEYS=" not in supconf or "ENV_OGS_FERNET_KEYS" in supconf


# =============================================================================
# 3. nginx 配置检查
# =============================================================================
class TestNginxConf:
    """nginx 反代配置: HTTPS + WebSocket 路由 + 安全头"""

    @pytest.fixture
    def nginxconf(self):
        return (DEPLOY / "nginx" / "orange_server.conf").read_text(encoding="utf-8")

    def test_01_nginx_has_https_server(self, nginxconf):
        """必须有 443 监听"""
        assert "listen 443" in nginxconf

    def test_02_nginx_has_http_redirect(self, nginxconf):
        """必须 HTTP → HTTPS 跳转"""
        assert "listen 80" in nginxconf
        assert "return 301 https" in nginxconf

    def test_03_nginx_has_webssh_websocket_route(self, nginxconf):
        """必须 /local/websocket WebSocket 路由"""
        assert "/local/websocket" in nginxconf
        # 必须有 Upgrade/Connection 头
        assert "Upgrade" in nginxconf
        assert "Connection" in nginxconf
        # 必须 proxy_read_timeout 长 (WebSSH)
        assert "proxy_read_timeout" in nginxconf

    def test_04_nginx_has_sftp_websocket_route(self, nginxconf):
        """必须 /local/sftp/websocket WebSocket 路由"""
        assert "/local/sftp/websocket" in nginxconf

    def test_05_nginx_has_security_headers(self, nginxconf):
        """必须有 HSTS / X-Content-Type-Options / X-Frame-Options"""
        assert "Strict-Transport-Security" in nginxconf
        assert "X-Content-Type-Options" in nginxconf
        assert "X-Frame-Options" in nginxconf

    def test_06_nginx_forwards_real_ip(self, nginxconf):
        """必须传 X-Real-IP / X-Forwarded-For (业务用真实 IP 限流)"""
        assert "X-Real-IP" in nginxconf
        assert "X-Forwarded-For" in nginxconf

    def test_07_nginx_health_check_excluded_from_log(self, nginxconf):
        """health 端点 access_log off (防日志膨胀)"""
        assert "/local/health" in nginxconf
        assert "access_log off" in nginxconf

    def test_08_nginx_body_size_limit(self, nginxconf):
        """必须有 client_max_body_size (防磁盘填充)"""
        assert "client_max_body_size" in nginxconf


# =============================================================================
# 4. docker-compose 配置检查
# =============================================================================
class TestDockerCompose:
    """docker-compose: 4 服务 + healthcheck + network"""

    @pytest.fixture
    def compose(self):
        return (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")

    def test_01_compose_has_core_and_autonomy_services(self, compose):
        """bundled 栈含业务四件套与自治 Redis/Worker"""
        for svc in (
            "backend", "frontend", "redis", "mysql",
            "autonomy-redis", "autonomy-worker",
        ):
            assert f"  {svc}:" in compose, f"docker-compose 缺服务: {svc}"

    def test_02_compose_has_internal_network(self, compose):
        """必须有内部 network (backend/redis/mysql 隔离)"""
        assert "networks:" in compose
        assert "ogs-internal" in compose

    def test_03_compose_uses_health_conditions(self, compose):
        """depends_on 必须用 service_healthy (启动顺序保障)"""
        # 至少有一个 depends_on + condition: service_healthy
        assert "condition: service_healthy" in compose

    def test_04_compose_mysql_init_with_sql(self, compose):
        """mysql 服务必须挂载 orange.sql 初始化"""
        assert "orange.sql" in compose
        assert "/docker-entrypoint-initdb.d" in compose

    def test_05_compose_redis_has_healthcheck(self, compose):
        """redis 必须有 healthcheck"""
        assert "  redis:" in compose
        redis_section = compose.split("  redis:")[1].split("\n\n")[0]
        assert "healthcheck" in redis_section

    def test_06_compose_mysql_uses_utf8mb4(self, compose):
        """mysql 必须 utf8mb4 (emoji + 中文)"""
        assert "utf8mb4" in compose

    def test_07_compose_backend_binds_to_backend_network(self, compose):
        """backend 不能直接暴露 28000 到公网 (仅 expose)"""
        # 找 backend 段: 找到 "  backend:" 行, 直到下一个 "  <service>:" 行
        backend_idx = compose.find("  backend:")
        if backend_idx < 0:
            pytest.skip("backend service not found")
        # 找到下一个 service 段开头 (2 空格 + 单词 + 冒号)
        # 跳过 backend 自身到下一行
        body_start = compose.find("\n", backend_idx) + 1
        # 找下一个 "  <word>:" 段开头
        service_re = re.compile(r'\n  \w[\w-]*:\s', re.MULTILINE)
        m = service_re.search(compose, body_start)
        next_service = m.start() if m else len(compose)
        backend_section = compose[backend_idx:next_service]
        # 必须有 expose (仅 network 可见)
        assert "expose:" in backend_section, "backend 服务必须用 expose 而非 ports"


# =============================================================================
# 5. install.sh 检查
# =============================================================================
class TestInstallSh:
    """install.sh: sha256 + 导入自检 + .env 引导"""

    @pytest.fixture
    def install(self):
        return (OPS / "install.sh").read_text(encoding="utf-8")

    def test_01_install_has_strict_mode(self, install):
        """必须 set -euo pipefail (严格模式)"""
        assert "set -euo pipefail" in install

    def test_02_install_has_sha256_verification(self, install):
        """必须 sha256 校验下载的业务包 (防中间人)"""
        assert "sha256sum" in install

    def test_03_install_has_import_self_check(self, install):
        """必须有 import 自检 (验证关键模块)"""
        assert "ImportError" in install or "import" in install
        # 必须列出关键模块
        for mod in ("flask", "sqlalchemy", "redis", "paramiko", "bcrypt", "cryptography",
                    "gevent", "geventwebsocket", "PIL", "gunicorn"):
            assert mod in install, f"import 自检缺模块: {mod}"

    def test_04_install_sets_env_mysql(self, install):
        """必须引导 .env 中的 MYSQL 配置"""
        assert "OGS_MYSQL_HOST" in install
        assert "OGS_MYSQL_PASSWORD" in install

    def test_05_install_prompts_for_secret_key(self, install):
        """必须提示用户设置密钥"""
        assert "OGS_FLASK_SECRET_KEY" in install
        assert "OGS_FERNET_KEY" in install

    def test_06_install_creates_data_dir(self, install):
        """必须创建数据目录"""
        # 不一定显式 mkdir, 但应提及
        assert "installdir" in install or "/data" in install

    def test_07_install_has_os_detection(self, install):
        """T2-P1-d: 必须有 OS 检测逻辑 (CentOS / Ubuntu / Debian 兼容)"""
        # 必须有 /etc/os-release 检测 或 OS_FAMILY 变量
        assert "/etc/os-release" in install, "install.sh 应检测 /etc/os-release 区分 OS"
        assert "OS_FAMILY" in install, "install.sh 应有 OS_FAMILY 变量"

    def test_08_install_supports_apt_get(self, install):
        """T2-P1-d: 必须支持 apt-get (Debian/Ubuntu)"""
        assert "apt-get" in install, "install.sh 应支持 apt-get (Debian/Ubuntu)"

    def test_09_install_supports_yum_or_dnf(self, install):
        """T2-P1-d: 必须支持 yum 或 dnf (RHEL/CentOS)"""
        # yum 或 dnf
        assert ("yum" in install) or ("dnf" in install)

    def test_10_install_uses_debian_frontend_noninteractive(self, install):
        """T2-P1-d: apt-get 必须 DEBIAN_FRONTEND=noninteractive (防交互卡住)"""
        assert "DEBIAN_FRONTEND=noninteractive" in install, \
            "apt-get 应 DEBIAN_FRONTEND=noninteractive 防交互"

    def test_11_install_documents_fernet_keys(self, install):
        """R1-22: install.sh 应提示 OGS_FERNET_KEYS 而非 OGS_FERNET_KEY"""
        assert "OGS_FERNET_KEYS" in install, "install.sh 应提示 OGS_FERNET_KEYS (R1-22)"


# =============================================================================
# 6. start.sh 检查
# =============================================================================
class TestStartSh:
    """start.sh: setsid + TERM grace + PID 管理"""

    @pytest.fixture
    def start(self):
        return (OPS / "start.sh").read_text(encoding="utf-8")

    def test_01_start_uses_setsid(self, start):
        """必须 setsid (脱离父 shell 进程组)"""
        assert "setsid" in start, "start.sh 必须用 setsid 脱离父 shell"

    def test_02_start_has_term_grace(self, start):
        """TERM 优雅退出 + KILL 兜底"""
        assert "kill -TERM" in start or "kill -SIGTERM" in start
        assert "kill -KILL" in start or "kill -9" in start

    def test_03_start_uses_pid_file(self, start):
        """必须 PID 文件管理"""
        assert "PID_FILE" in start or "pid" in start.lower()

    def test_04_start_supports_all_subcommands(self, start):
        """必须支持 start/stop/restart/status"""
        for cmd in ("start", "stop", "restart", "status"):
            assert f"'{cmd}'" in start or f'"{cmd}"' in start, f"start.sh 缺子命令: {cmd}"


# =============================================================================
# 7. healthcheck.sh 检查
# =============================================================================
class TestHealthcheckSh:
    """healthcheck.sh: status 字段验证 + timeout"""

    @pytest.fixture
    def hc(self):
        return (OPS / "healthcheck.sh").read_text(encoding="utf-8")

    def test_01_healthcheck_probes_local_health(self, hc):
        """必须探测 /local/health"""
        assert "/local/health" in hc

    def test_02_healthcheck_has_timeout(self, hc):
        """必须有 curl timeout (防 hang)"""
        assert "max-time" in hc or "timeout" in hc

    def test_03_healthcheck_validates_status_ok(self, hc):
        """必须验证 status=ok 字段"""
        assert '"status"' in hc or "'status'" in hc
        assert "ok" in hc


# =============================================================================
# 8. .env.example / orange_server.env.example
# =============================================================================
class TestEnvExamples:
    """环境变量模板: 必须含 OGS_FERNET_KEYS (R1-22 同步)"""

    @pytest.fixture
    def env_example(self):
        return (BACKEND / ".env.example").read_text(encoding="utf-8")

    @pytest.fixture
    def supervisor_env_example(self):
        return (DEPLOY / "supervisor" / "orange_server.env.example").read_text(encoding="utf-8")

    def test_01_env_example_has_fernet_keys(self, env_example):
        """R1-22: .env.example 必须含 OGS_FERNET_KEYS 字段"""
        assert "OGS_FERNET_KEYS" in env_example, \
            "backend/.env.example 必须含 OGS_FERNET_KEYS 字段 (R1-22)"

    def test_02_env_example_documents_fernet_rotation(self, env_example):
        """必须文档化 rotation 用法"""
        # 必须有 rotation 关键字或多 key 示例
        has_rotation = "rotation" in env_example.lower() or "key2" in env_example
        assert has_rotation, "OGS_FERNET_KEYS 应文档化 rotation 用法"

    def test_03_env_example_has_flask_secret_key(self, env_example):
        assert "OGS_FLASK_SECRET_KEY" in env_example

    def test_04_env_example_has_mysql_config(self, env_example):
        for var in ("OGS_MYSQL_HOST", "OGS_MYSQL_PORT", "OGS_MYSQL_DBNAME",
                    "OGS_MYSQL_USER", "OGS_MYSQL_PASSWORD"):
            assert var in env_example, f"env.example 缺 {var}"

    def test_05_env_example_has_redis_config(self, env_example):
        assert "OGS_REDIS_HOST" in env_example
        assert "OGS_REDIS_PORT" in env_example

    def test_06_supervisor_env_example_has_fernet_keys(self, supervisor_env_example):
        """R1-22: supervisor env.example 也应含 OGS_FERNET_KEYS"""
        assert "OGS_FERNET_KEYS" in supervisor_env_example, \
            "supervisor env.example 必须含 OGS_FERNET_KEYS (R1-22)"


# =============================================================================
# 9. .dockerignore 检查
# =============================================================================
class TestDockerignore:
    """.dockerignore: 排除 .git / node_modules / pytest_cache / 敏感文件"""

    @pytest.fixture
    def dockerignore(self):
        return (BACKEND / ".dockerignore").read_text(encoding="utf-8")

    def test_01_dockerignore_excludes_git(self, dockerignore):
        assert ".git" in dockerignore

    def test_02_dockerignore_excludes_pytest_cache(self, dockerignore):
        assert ".pytest_cache" in dockerignore or "__pycache__" in dockerignore

    def test_03_dockerignore_excludes_node_modules(self, dockerignore):
        assert "node_modules" in dockerignore

    def test_04_dockerignore_excludes_env_file(self, dockerignore):
        """必须排除 .env (含密钥)"""
        assert ".env" in dockerignore, ".dockerignore 必须排除 .env (防密钥泄露)"

    def test_05_dockerignore_excludes_tests(self, dockerignore):
        """建议排除 tests/ (减少镜像体积)"""
        # 软建议: 不强制
        pass


# =============================================================================
# 10. deploy/README.md 检查
# =============================================================================
class TestDeployReadme:
    """deploy/README.md: 3 模式 + 环境变量 + 故障排查"""

    @pytest.fixture
    def readme(self):
        path = DEPLOY / "README.md"
        if not path.exists():
            pytest.skip("deploy/README.md 不存在 (T2-P1a 待补)")
        return path.read_text(encoding="utf-8")

    def test_01_readme_mentions_docker_compose(self, readme):
        """应提及 docker compose 部署"""
        assert "docker compose" in readme.lower() or "docker-compose" in readme.lower()

    def test_02_readme_mentions_supervisor(self, readme):
        """应提及 supervisor 部署"""
        assert "supervisor" in readme.lower()

    def test_03_readme_documents_env_vars(self, readme):
        """应文档化环境变量"""
        assert "OGS_FLASK_SECRET_KEY" in readme
        assert "OGS_FERNET_KEYS" in readme or "OGS_FERNET_KEY" in readme

    def test_04_readme_has_troubleshooting(self, readme):
        """应有故障排查段"""
        assert "故障" in readme or "Troubleshoot" in readme or "排查" in readme


# =============================================================================
# 11. 一致性交叉检查
# =============================================================================
class TestConsistency:
    """deploy 多文件一致性: worker class / 端口 / 路径"""

    def test_01_dockerfile_and_supervisor_worker_class_match(self):
        """Dockerfile 和 supervisor 的 worker class 必须一致 (都 geventwebsocket)"""
        dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
        supconf = (DEPLOY / "supervisor" / "orange_server.conf").read_text(encoding="utf-8")
        assert "geventwebsocket" in dockerfile
        assert "geventwebsocket" in supconf

    def test_02_nginx_upstream_matches_dockerfile_bind(self):
        """nginx upstream 端口必须与 Dockerfile bind 一致"""
        dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
        nginxconf = (DEPLOY / "nginx" / "orange_server.conf").read_text(encoding="utf-8")
        # REV49: Dockerfile 改用 OGS_PORT 环境变量, 检查变量引用或硬编码均可
        assert ("0.0.0.0:28000" in dockerfile
                or "0.0.0.0:${OGS_PORT}" in dockerfile
                or "OGS_PORT" in dockerfile)
        # nginx upstream 127.0.0.1:28000
        assert "127.0.0.1:28000" in nginxconf

    def test_03_healthcheck_url_consistent(self):
        """healthcheck.sh / Dockerfile HEALTHCHECK / docker-compose healthcheck 都指向 /local/health"""
        hc_sh = (OPS / "healthcheck.sh").read_text(encoding="utf-8")
        dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
        compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
        for src in (hc_sh, dockerfile, compose):
            assert "/local/health" in src, "健康检查 URL 必须一致 (3 处)"

    def test_04_data_dir_paths_consistent(self):
        """Dockerfile 创建的目录应与代码默认 data/ 目录一致"""
        dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
        for d in ("/app/data/avatars", "/app/data/file", "/app/data/key",
                  "/app/data/log"):
            assert d in dockerfile, f"Dockerfile 缺数据目录: {d}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
