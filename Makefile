# =============================================================================
# OrangeServer 顶层 Makefile
# =============================================================================
# 一站式构建入口，整合前后端 + 部署编排
#
# 用法：
#   make help         显示所有命令
#   make install      安装前后端依赖
#   make dev          启动开发模式（前后端 dev server）
#   make build        构建生产镜像（镜像内完成前端构建）
#   make build-frontend   仅构建前端（CI 拆分任务 / 部署前前端预编译）
#   make build-backend    仅构建后端 Docker 镜像
#   make test         跑测试
#   make lint         代码静态检查
#   make health       健康检查（curl /local/health）
#   make docker-up    docker compose 启动
#   make docker-down  docker compose 停止
#   make clean        清理构建产物
#
# ⓘ 生产镜像在多阶段构建中生成前端 dist，Node 不进入运行镜像。
# =============================================================================

.PHONY: help install dev dev-backend dev-frontend build build-frontend build-backend test lint health docker-up docker-up-image docker-up-host docker-down docker-ps docker-logs docker-health setup-token docker-check docs-check docker-dev-init docker-dev-up docker-dev-autonomy-up docker-dev-autonomy-down docker-dev-autonomy-ps docker-dev-autonomy-logs docker-dev-down docker-dev-reset docker-dev-ps docker-dev-logs clean

# 路径 (REV49: 用 abspath 解析 Makefile 所在目录, 不依赖 pwd/cwd)
ROOT     := $(abspath $(dir $(firstword $(MAKEFILE_LIST))))
BACKEND  := $(ROOT)/backend
FRONTEND := $(ROOT)/frontend
DEPLOY   := $(ROOT)/deploy
OPS      := $(ROOT)/ops

# REV49: --env-file 指定根 .env (变量替换), -f 指定 compose 文件
#        不使用 --project-directory (会破坏 ../backend 等相对路径基准)
COMPOSE  := docker compose --env-file "$(ROOT)/.env" -f "$(DEPLOY)/docker-compose.yml"
# 镜像入口只信任受权限保护的根 .env，避免调用者已 export 的旧 tag 静默覆盖。
IMAGE_COMPOSE := env -u OGS_BACKEND_IMAGE -u OGS_BACKEND_TAG $(COMPOSE)
DEV_COMPOSE := env -u COMPOSE_PROJECT_NAME -u OGS_DEV_BACKEND_IMAGE -u OGS_DEV_BACKEND_TAG docker compose --env-file "$(ROOT)/.env.dev" -f "$(DEPLOY)/docker-compose.dev.yml"
# 自治开关和密码只信任权限受限的 .env.dev，避免调用者 export 的旧值覆盖。
DEV_AUTONOMY_COMPOSE := env -u OGS_AI_AUTONOMY_ENABLED -u OGS_AI_AUTONOMY_REDIS_PASSWORD $(DEV_COMPOSE) -f "$(DEPLOY)/docker-compose.dev-autonomy.yml"

help: ## 显示帮助
	@echo "OrangeServer 一站式构建入口"
	@echo ""
	@echo "常用命令:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## 安装前后端依赖 (含 dev 依赖: pytest/pyflakes 等)
	@echo ">>> 安装后端 Python 依赖..."
	cd $(BACKEND) && pip install -r requirements.txt -r requirements-dev.txt
	@echo ">>> 安装前端 Node 依赖..."
	cd $(FRONTEND) && npm ci
	@echo "[OK] 依赖安装完成"

# DEPLOY-AUDIT P2-8: 旧的 dev 目标串行执行, 后端阻塞后前端永远不会启动 → 拆分
dev: ## 开发模式说明 (需两个终端分别执行)
	@echo "开发模式需要两个终端:"
	@echo "  终端 1: make dev-backend   (Flask :28000)"
	@echo "  终端 2: make dev-frontend  (Vite  :5173)"

dev-backend: ## 启动后端 dev server (:28000, 阻塞)
	cd $(BACKEND) && python init.py

dev-frontend: ## 启动前端 dev server (:5173, 阻塞)
	cd $(FRONTEND) && npm run dev

build: build-backend ## 一站式构建生产镜像

build-frontend: ## 仅构建前端（CI 拆分任务 / 部署前前端预编译）
	@echo ">>> 构建前端 dist (输出到 $(FRONTEND)/dist)..."
	cd $(FRONTEND) && npm ci && npm run build
	@echo "[OK] 前端 dist 已生成, 大小: $(du -sh $(FRONTEND)/dist 2>/dev/null | cut -f1 || echo 'unknown')"

build-backend: ## 仅构建后端 Docker 镜像
	@echo ">>> 构建后端 Docker 镜像..."
	docker build -t orangeserver-backend:latest -f "$(BACKEND)/Dockerfile" "$(ROOT)"
	@echo "[OK] 后端镜像构建完成: orangeserver-backend:latest"

test: ## 跑测试
	cd $(BACKEND) && pytest tests/ -v

lint: ## 代码静态检查 (前端为 vue-tsc 类型检查, package.json 无独立 lint script)
	cd $(BACKEND) && python -m pyflakes app/ || true
	cd $(FRONTEND) && npm run type-check

health: ## 健康检查（curl /local/health）
	@echo ">>> 健康检查 http://127.0.0.1:28000/local/health"
	@bash "$(OPS)/healthcheck.sh"

docker-check: ## 部署预检 (只读, 不启动容器)
	@bash "$(OPS)/preflight-compose.sh" bundled

docs-check: ## 文档链接与隐私检查 (需要 pwsh + ripgrep)
	pwsh -File "$(OPS)/check-docs.ps1"

docker-up: docker-check ## docker compose 启动 (生产, bundled 模式)
	$(COMPOSE) --profile bundled up -d --build --remove-orphans
	@echo "[OK] 服务已启动，查看 make docker-ps"

docker-up-image: docker-check ## 拉取已发布后端镜像并启动 (公开发布后使用)
	@image="$$(sed -n 's/^OGS_BACKEND_IMAGE=//p' "$(ROOT)/.env" | tail -1)"; \
	tag="$$(sed -n 's/^OGS_BACKEND_TAG=//p' "$(ROOT)/.env" | tail -1)"; \
	if [ -z "$$image" ] || [ "$$image" = "CHANGE_ME" ]; then \
		echo "[FAIL] 请先在根 .env 设置已发布的 OGS_BACKEND_IMAGE"; \
		exit 1; \
	fi; \
	if ! printf '%s\n' "$$tag" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$$'; then \
		echo "[FAIL] OGS_BACKEND_TAG 必须固定为稳定版本 (例如 v1.2.3)，禁止 latest"; \
		exit 1; \
	fi
	$(IMAGE_COMPOSE) --profile bundled pull
	$(IMAGE_COMPOSE) --profile bundled up -d --no-build --remove-orphans
	@echo "[OK] 已使用预构建后端镜像启动，查看 make docker-ps"

# DEPLOY-AUDIT P1-1: host 模式 (外部 MySQL/Redis) 需要叠加 host.yml 提供
#   host.docker.internal 解析; 之前文档给的裸命令缺该文件且绕过预检
docker-up-host: ## docker compose 启动 (host 模式, 连接外部 MySQL/Redis)
	@bash "$(OPS)/preflight-compose.sh" host
	$(COMPOSE) -f "$(DEPLOY)/docker-compose.host.yml" up -d --build --remove-orphans app worker
	@echo "[OK] host 模式已启动 (外部 MySQL/Redis)"

docker-down: ## docker compose 停止
	$(COMPOSE) down

docker-dev-init: ## 首次生成全容器开发环境配置 (.env.dev)
	@bash "$(OPS)/init-dev-env.sh"

docker-dev-up: docker-dev-init ## 启动独立全容器开发环境 (源码映射 + Vite HMR)
	$(DEV_COMPOSE) up -d --wait --wait-timeout 180
	@echo "[OK] 开发环境已启动: http://127.0.0.1:$$(sed -n 's/^OGS_DEV_HTTP_PORT=//p' "$(ROOT)/.env.dev")"

docker-dev-autonomy-up: docker-dev-init ## 启动 M1 自治开发栈 (显式 feature flag + Redis 8 + Worker)
	@flag="$$(sed -n 's/^OGS_AI_AUTONOMY_ENABLED=//p' "$(ROOT)/.env.dev" | tail -1 | tr '[:upper:]' '[:lower:]')"; \
	password="$$(sed -n 's/^OGS_AI_AUTONOMY_REDIS_PASSWORD=//p' "$(ROOT)/.env.dev" | tail -1)"; \
	if ! printf '%s\n' "$$flag" | grep -Eq '^(1|true|yes|on)$$'; then \
		echo "[FAIL] 请先在 .env.dev 显式设置 OGS_AI_AUTONOMY_ENABLED=true"; \
		exit 1; \
	fi; \
	if [ -z "$$password" ]; then \
		echo "[FAIL] 请先在 .env.dev 设置非空 OGS_AI_AUTONOMY_REDIS_PASSWORD"; \
		exit 1; \
	fi
	$(DEV_AUTONOMY_COMPOSE) up -d --build --wait --wait-timeout 240
	@echo "[OK] M1 自治开发栈已启动；使用 make docker-dev-autonomy-ps 检查 Worker 和 Redis 8"

docker-dev-autonomy-down: docker-dev-init ## 停止 M1 自治开发栈并保留隔离数据卷
	$(DEV_AUTONOMY_COMPOSE) down

docker-dev-autonomy-ps: docker-dev-init ## 查看 M1 自治开发栈状态
	$(DEV_AUTONOMY_COMPOSE) ps

docker-dev-autonomy-logs: docker-dev-init ## 跟踪 M1 自治 Worker/Redis 8 日志
	$(DEV_AUTONOMY_COMPOSE) logs --tail=200 -f autonomy-worker autonomy-redis

docker-dev-down: docker-dev-init ## 停止开发环境，保留数据库和依赖卷
	$(DEV_COMPOSE) down

docker-dev-reset: docker-dev-init ## 重置开发环境（删除该项目的数据库/Redis/运行时卷）
	$(DEV_COMPOSE) down --volumes --remove-orphans

docker-dev-ps: docker-dev-init ## 查看开发环境容器状态
	$(DEV_COMPOSE) ps

docker-dev-logs: docker-dev-init ## 跟踪开发环境日志
	$(DEV_COMPOSE) logs --tail=200 -f

docker-ps: ## docker compose 查看状态
	$(COMPOSE) --profile bundled ps

docker-logs: ## docker compose 查看日志
	$(COMPOSE) --profile bundled logs --tail=200

docker-health: ## docker compose 健康检查 (经前端端口)
	@port="$$(sed -n 's/^OGS_HTTP_PORT=//p' "$(ROOT)/.env" | tail -1)"; \
	curl -fsS "http://127.0.0.1:$${port:-8080}/local/health"

setup-token: ## 读取首次初始化向导的一次性 Token
	@$(COMPOSE) exec -T app cat /app/data/setup_token.txt

clean: ## 清理构建产物 (frontend/dist 是入仓制品, 不删)
	find $(ROOT) -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find $(ROOT) -type d -name node_modules -exec rm -rf {} + 2>/dev/null || true
	# REV48 (P2-5): frontend/dist 是入仓制品, make clean 不删 (之前会删)
	@echo "[OK] 清理完成 (frontend/dist 保留)"
