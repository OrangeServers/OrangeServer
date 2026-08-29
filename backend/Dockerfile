# =============================================================================
# OrangeServer Backend Dockerfile (REVIEW-15 P1-5/P1-6/P1-7 精修版 + ti3-DOCKER)
# =============================================================================
# 构建:   docker build -t orangeserver-backend:$(date +%Y%m%d) -f backend/Dockerfile .
# 运行:   docker run -d --name ogs-backend -p 28000:28000 \
#             -e OGS_FLASK_SECRET_KEY=xxx -e OGS_MYSQL_PASSWORD=xxx \
#             -e OGS_FERNET_KEYS=xxx -e OGS_REDIS_HOST=redis_host \
#             orangeserver-backend:latest
#
# 镜像分层（4 stage，多阶段构建）：
#   1. base:    slim Python 3.12 + 统一镜像源/运行环境
#   2. frontend: Node 构建 SPA
#   3. builder: 依据已提交的 requirements.txt 一次性构建 wheelhouse
#   4. runtime: 从 wheelhouse 离线安装，只保留 runtime deps
#
# P1-5: 不引用 requirements-dev.txt（pip-audit/bandit/pytest 仅开发用）
# P1-6: COPY 全部代码但配合 .dockerignore 排除 _archive/data/verify 脚本
# P1-7: HEALTHCHECK 调 /local/health（不需 csrf / auth，3s 超时）
# T2-P0 (2026-07-01): worker-class 必须为 geventwebsocket (WebSocket 兼容)
#   - 原: --worker-class gevent → 普通 gevent worker, WebSocket 升级会失败
#   - 修复: geventwebsocket.gunicorn.workers.GeventWebSocketWorker
#   - 同步: deploy/supervisor/orange_server.conf 已用相同 worker class
# ti3-DOCKER (2026-07-02): 多阶段构建 + slim 镜像优化
#   - LABEL org.opencontainers.image.* 镜像元信息
#   - STOPSIGNAL SIGTERM → gunicorn graceful shutdown (30s 超时)
#   - 配合 .dockerignore 排除 tests/ + dev configs → 镜像体积明显减少
#   - 实测镜像大小约 273MB（依赖 wheelhouse 通过只读挂载安装，不进入最终镜像层）
# =============================================================================

# ---------- Stage 1: base ----------
FROM python:3.12-slim AS base

# REV49: 国内镜像加速 (apt 阿里云 + pip 清华源)
#   海外构建可注释掉以下两行，回退到默认源
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null; \
    true

# 防止 .pyc 落盘 + 强制 stdout/stderr 不缓冲（Docker log 实时可见）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8


# ---------- Stage 2: frontend ----------
FROM node:22.22.1-alpine AS frontend-builder

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


# ---------- Stage 3: Python builder ----------
FROM base AS builder

# build 阶段需要的编译工具链
#   - build-essential:  编译 C 扩展（gevent / cryptography / bcrypt / Pillow）
#   - libffi-dev:       cffi（cryptography 的依赖）
#   - libssl-dev:       OpenSSL 头文件（cryptography 编译时）
#   - zlib1g-dev:       Pillow PNG/JPEG 支持
#   注：PyMySQL 是纯 Python，无需 libmysqlclient-dev
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# requirements.txt 是镜像构建的唯一依赖输入；依赖更新在开发/CI 阶段完成。
COPY backend/requirements.txt requirements.txt

# 预构建全部直接和传递依赖的 wheel；runtime 不再访问包索引或现场解析。
RUN pip install --upgrade pip \
    && pip wheel --wheel-dir /wheels -r requirements.txt \
    && echo "[BUILD] dependency wheelhouse ready"


# ---------- Stage 4: runtime ----------
FROM base AS runtime

# runtime 只需要 base lib，不要 -dev
# 删 build-essential / libffi-dev / libssl-dev / zlib1g-dev 等编译工具链
# T2-P1 (2026-07-02): bookworm 包名修正
#   - zlib1g1 -> zlib1g (bookworm 去掉末尾 1; bullseye 才叫 zlib1g1)
#   - libffi8 保留 (bookworm 和 bullseye 都是 libffi8)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libffi8 \
        libssl3 \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行（生产要求）
RUN groupadd -r ogs && useradd -r -g ogs -d /app -s /sbin/nologin ogs

WORKDIR /app

# 从 builder 复制 wheelhouse，离线安装 runtime 依赖。
COPY backend/requirements.txt /app/requirements.txt
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels,ro \
    pip install --no-index --find-links=/wheels -r requirements.txt \
    && pip check \
    && echo "[RUNTIME] Python deps installed"

# 复制业务代码（配合 .dockerignore 排除 _archive / data / verify 脚本）
COPY --chown=ogs:ogs backend/ /app/
COPY --from=frontend-builder --chown=ogs:ogs /frontend/dist/ /app/app/static/

# 预创建运行时目录
#   avatars/         = 用户头像上传
#   file/            = SFTP 文件落地
#   key/             = SSH 私钥（运行时生成，不入仓）
#   log/             = 日志
RUN mkdir -p /app/data/avatars /app/data/file /app/data/key /app/data/log /app/.gunicorn \
    && chown -R ogs:ogs /app/data /app/.gunicorn \
    && echo "[RUNTIME] Data directories ready"

USER ogs

# 镜像元信息 (OCI 标准)
#   - name:           仓库地址/镜像名
#   - source:         源码仓库
#   - licenses:       Apache-2.0
#   - title/description: 镜像描述
#   - created:        构建时间（动态生成见 deploy/build.sh）
LABEL org.opencontainers.image.title="OrangeServer Backend" \
      org.opencontainers.image.description="OrangeServer Flask backend (geventwebsocket + WebSSH/SFTP)" \
      org.opencontainers.image.source="https://github.com/OrangeServers/OrangeServer" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="OrangeServer" \
      org.opencontainers.image.version="1.1.1"

# ti3-DOCKER (2026-07-02): SIGTERM 让 gunicorn 走 graceful shutdown
#   gunicorn 默认收到 SIGTERM 后等 worker 处理完当前请求 (--graceful-timeout=30s)
#   docker stop 默认 10s 超时后 SIGKILL
#   推荐: docker stop --time=30 <container>
STOPSIGNAL SIGTERM

EXPOSE 28000

# REV49: 健康检查用 OGS_PORT 环境变量, 不硬编码端口
#   默认 28000, --network host 模式可改用 28001 等
HEALTHCHECK --interval=30s --timeout=3s --retries=3 --start-period=15s \
    CMD python -c "import os,urllib.request; port=os.environ.get('OGS_PORT','28000'); r=urllib.request.urlopen(f'http://127.0.0.1:{port}/local/health', timeout=2); exit(0 if r.status==200 else 1)" || exit 1

# 默认启动 gunicorn（生产）
#   - workers 1（APScheduler 模块导入时启动, 多 worker 会重复执行定时任务）
#   - geventwebsocket worker class（兼容 WebSSH/SFTP WebSocket 升级）
#   - timeout 60（WebSSH 长连接 + 命令执行余量）
# REV49: 使用 shell form CMD 以支持 OGS_PORT 环境变量
#   换端口: docker run -e OGS_PORT=28001 ... (同时覆盖 CMD 中的 --bind)
#   或: docker run ... gunicorn ... --bind 0.0.0.0:28001 ... wsgi:app
ENV OGS_PORT=28000
CMD ["sh", "-c", "exec gunicorn --workers 1 --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --worker-connections 1000 --bind 0.0.0.0:${OGS_PORT} --timeout 60 --keep-alive 5 --access-logfile - --error-logfile - --log-level info wsgi:app"]
