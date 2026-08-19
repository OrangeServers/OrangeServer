# OrangeServer 部署指南 (Tier 2)

> **本文件是简化版索引**，详细文档见根目录 [`DEPLOY.md`](../DEPLOY.md)。
> 任何具体步骤以 `DEPLOY.md` 为准。
> 已有实例升级时必须使用
> [`docs/operations/UPGRADE.md`](../docs/operations/UPGRADE.md)，本目录不维护迁移命令。

---

## 文档定位

- **完整部署文档**：[`DEPLOY.md`](../DEPLOY.md) — 物理机/Docker Compose 两条路径完整步骤
- **统一升级流程**：[`docs/operations/UPGRADE.md`](../docs/operations/UPGRADE.md) — 备份、迁移、验证与回滚
- **配置参考**：[`CONFIG.md`](../CONFIG.md) — OGS_* 全部环境变量
- **本目录文件**：
  - `docker-compose.yml` — 生产 4 服务编排（含 profiles）
  - `docker-compose.dev.yml` — 独立全容器开发模式（新 MySQL/Redis 卷、backend 源码重载、Vite HMR）
  - `docker-compose.dev-autonomy.yml` — M1 自治开发覆盖层（专用 Redis 8 + Celery Worker）
  - `nginx/orange_server.conf` — 物理机 nginx（含 frontend/dist 静态 serve + 6 个 API 前缀反代）
  - `nginx/frontend_container.conf` — 容器版 nginx（upstream=backend:28000）
  - `nginx/ogs_proxy_common.conf` — 容器版 nginx 公共反代参数
  - `docker-compose.host.yml` — host 模式覆盖文件（模式 B: extra_hosts）
  - `supervisor/orange_server.conf` — 物理机 supervisor 配置
  - `supervisor/orange_server.env.example` — supervisor env 模板
  - `daemon.json.example` — Docker daemon registry mirror 模板
  - `README.md` — 本文件

## 容器清单（bundled 模式）

`make docker-up` 启动 4 个服务：

| 服务 | 镜像 | 端口 (宿主机) | 端口 (容器内) | 用途 |
|------|------|---------------|---------------|------|
| backend | 源码部署默认本地构建；Release 安装使用固定版本 GHCR 镜像 | (无) | 28000 | Flask + gunicorn + geventwebsocket |
| frontend | nginx:1.25-alpine | ${OGS_HTTP_PORT:-8080} | 80 | 反代 backend + serve frontend/dist |
| redis | redis:7.4-alpine | (无) | 6379 | 会话/CSRF/限流 (配 OGS_REDIS_PASSWORD) |
| mysql | mysql:8.0.42 | (无) | 3306 | 业务数据库（自动导入 orange.sql）|

开发环境不叠加生产编排，也不连接宿主机旧数据库：

```bash
make docker-dev-up
make docker-dev-ps
# 删除且仅删除 orangeserver_dev 的开发数据后重建：
make docker-dev-reset
make docker-dev-up
```

M1 自治能力是默认关闭的发布候选。标准 bundled/host 发布路径不启动自治 Worker
或专用 Redis 8；仅在隔离开发/验收环境中，将 `.env.dev` 的
`OGS_AI_AUTONOMY_ENABLED` 显式改为 `true` 后运行：

```bash
make docker-dev-autonomy-up
```

该目标在普通开发栈上增加专用 Redis 8（AOF、持久卷、`noeviction`）和独立
Celery Worker；backend 与 Worker 使用同一个由当前源码构建的本地镜像，业务 Redis 7
保持不变。初始化脚本会生成独立的 `OGS_AI_AUTONOMY_REDIS_PASSWORD`；旧的
`.env.dev` 若缺少该项，启动目标会拒绝继续并提示补齐。启动后用
`make docker-dev-autonomy-ps` 查看 Worker 和 Redis 8。不要在普通用户部署中启用
这个覆盖层。完整的从零发布和 M1 验收命令见
[`docs/operations/BACKEND_IMAGE_RELEASE.md`](../docs/operations/BACKEND_IMAGE_RELEASE.md)。

开发默认入口为 `http://127.0.0.1:8081`，后端调试端口为 `28001`。源码目录
分别 bind mount 到 backend 与 Vite frontend；数据库、Redis、后端运行时数据和
`node_modules` 使用 `orangeserver_dev` 项目专属命名卷。首次打开后完成 `/setup`
初始化向导；不要使用 `orange.sql` 中仅用于引导的种子弱口令。

## 两种部署模式

| 模式 | 启动命令 | 启动服务 | 镜像来源 | 场景 |
|------|---------|----------|----------|------|
| A. bundled | `make docker-up` | 4 个 | 后端本地构建；其余 3 个拉取 | 全新部署 / 容器化基础设施 |
| B. 外部服务 | `make docker-up-host` | 2 个 | 后端本地构建；Nginx 拉取 | 本机已有 redis/mysql |

模式 B 需 .env 改 `OGS_MYSQL_HOST` + `OGS_REDIS_HOST` 为外部地址。
必须使用 `make docker-up-host`，由它叠加 host overlay 并执行预检（详见 DEPLOY.md）。

## 快速命令参考

完整步骤在 DEPLOY.md，本节是手动 Compose 速查。普通新安装优先使用根部署手册的
固定版本一键入口，由薄引导器自动生成两份 `.env` 并使用已发布后端镜像。

```bash
# 1. 准备 .env (两份都要)
cp .env.example .env
cp backend/.env.example backend/.env
chmod 600 .env backend/.env
# 编辑两文件：
# - OGS_MYSQL_{DBNAME,USER,PASSWORD} 和 OGS_REDIS_PASSWORD 两处一致
# - MYSQL_ROOT_PASSWORD 只填根 .env
# - 修改 OGS_HTTP_PORT 时，同步 backend/.env 的 OGS_CSRF_ALLOWED_ORIGINS

# 2. Docker Hub 镜像加速（仅帮助 nginx/redis/mysql，不代理 ghcr.io）
#    先把 registry-mirrors 等键合并进现有配置，不要覆盖已有 daemon.json。
sudoedit /etc/docker/daemon.json
# 参照 daemon.json.example 合并所需键，保存后先验证：
sudo dockerd --validate --config-file=/etc/docker/daemon.json
sudo systemctl restart docker

# 3. 启动 (bundled 模式, 自动预检)
#    源码检出会本地构建后端；Release 一键安装使用固定版本 GHCR 镜像。
make docker-up

# 4. 冒烟测试
sleep 60  # 等 mysql 初始化
make docker-health
# setup 或 ok，HTTP 200 均正常
make docker-ps

# 5. 关闭
make docker-down
```

## 物理机部署关键点

- **支持等级：参考。** 以下内容从已验证的 Compose 拓扑整理，尚未完成全新
  物理机端到端验收，不作为公开发布的一键部署承诺。
- Supervisor 配置仅供既有环境维护；单独的启动参数模板不包含完整应用配置。
  新部署优先使用 Docker Compose 或 systemd。
- `OGS_MYSQL_USER=app_user`（不能用 root，config.py 黑名单）
- nginx 配置用 [`orange_server.conf`](nginx/orange_server.conf)（含 dist 静态 serve + WebSocket）
- 详见 DEPLOY.md §1.3-1.6

## 环境变量要点

- `OGS_FLASK_SECRET_KEY`：Flask session 签名密钥（生产必填，>=48 字节）
- `OGS_FERNET_KEYS`：Fernet 加密密钥列表（SSH 凭据加密，支持 rotation）
- `OGS_MYSQL_PASSWORD`：业务账号 app_user 密码（根 .env 和 backend/.env 两处一致）
- `MYSQL_ROOT_PASSWORD`：mysql 容器 root 密码（仅根 .env，与业务账号不同）
- `OGS_REDIS_PASSWORD`：Redis requirepass（两处一致）
- `OGS_HTTP_PORT`：前端宿主机端口（默认 8080）
- 若该端口落在 `net.ipv4.ip_local_port_range` 内，需改用范围外端口或先合并到
  `net.ipv4.ip_local_reserved_ports`；否则出站连接的 TIME_WAIT 可能阻止 Docker 绑定
- `OGS_CSRF_ALLOWED_ORIGINS`：公开访问地址（scheme + 主机 + 实际端口），修改
  `OGS_HTTP_PORT` 时必须同步
- `OGS_BACKEND_IMAGE`：公开镜像仓库地址；Release 安装固定为 GHCR，源码部署不设置时本地构建
- `OGS_BACKEND_TAG`：使用公开镜像时固定为 `vX.Y.Z`，不要依赖 `latest`
- 完整清单见 [`DEPLOY.md`](../DEPLOY.md) 和 [`CONFIG.md`](../CONFIG.md)

## 故障排查

详见 [`DEPLOY.md`](../DEPLOY.md) §五「常见问题」，典型场景：

- `OGS_FLASK_SECRET_KEY is required`：检查 backend/.env 是否设置且 OGS_ENV=prod
- MySQL 连接失败：确认 OGS_MYSQL_HOST/PORT/USER/PASSWORD 正确
- Redis 连接失败：`redis-cli -h <host> -p <port> ping` 返回 PONG
- Compose config 解析失败：`make docker-check` 查看具体错误
- 端口冲突：读取 `.env` 中的 `OGS_HTTP_PORT`，再用
  `ss -ltn | grep ":${OGS_HTTP_PORT:-8080} "` 检查是否被占用

## 旧版 docker-compose (v1)

`docker-compose`（连字符，v1）已 EOL，Docker 24+ 默认装 v2 plugin（`docker compose` 空格）。建议升 v2。
