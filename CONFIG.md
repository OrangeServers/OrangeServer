# OrangeServer 配置参考手册

> 后端所有可配置项集中说明。生产环境部署前请通读。

---

## 配置加载顺序

`backend/app/core/config.py`（替代旧 `app/conf/conf.py`）按以下优先级加载：

1. **非空进程环境变量**（最高优先级，Docker / systemd 注入；空字符串按未设置处理）
2. **`<OGS_DATA_DIR>/runtime.env`**（首次部署向导 /setup 写入；路径可用
   `OGS_RUNTIME_ENV_FILE` 覆盖；仅填充进程 env 缺失/为空的 key）
3. **项目根目录 `.env` 文件**（`backend/.env`，本地开发/手工部署用）
4. **代码内置默认值**（仅开发模式可用，生产 `OGS_ENV=prod` 会强校验）

> **警告**：生产环境 (`OGS_ENV=prod`) 启动时会强校验 `OGS_FLASK_SECRET_KEY` 和 `OGS_FERNET_KEYS` 必须为非空。

> **首次部署向导**：必需项（MySQL 三项 / SECRET_KEY / FERNET_KEYS）缺失时后端进入
> `/setup` 网页向导而非启动失败；`OGS_SETUP_MODE=off` 禁用向导（恢复 fail-fast），
> `OGS_SETUP_MODE=force` 强制重开向导（运维救援，需宿主机权限）。详见 DEPLOY.md。
> bundled 模式的 `MYSQL_ROOT_PASSWORD`、`OGS_MYSQL_PASSWORD` 等容器初始化变量仍须
> 在首次启动前写入根 `.env`；向导只能生成和保存后端应用配置，不能回头改变已经
> 初始化完成的 MySQL 容器账号。

---

## Compose 层变量（不进后端进程，只被 docker-compose.yml 插值消费）

| 变量 | 所在文件 | 说明 |
|------|---------|------|
| `OGS_HTTP_PORT` | 根 `.env` | 前端 nginx 对宿主发布端口（默认 8080） |
| `MYSQL_ROOT_PASSWORD` | 根 `.env` | bundled 模式 mysql 容器 root 口令（仅初始化用） |
| `OGS_BACKEND_IMAGE` | 根 `.env` | 可选的公开后端镜像仓库；未设置时使用本地镜像名 |
| `OGS_BACKEND_TAG` | 根 `.env` | 后端镜像 tag（默认 latest；公开部署应固定版本） |
| `OGS_PORT` / `OGS_BIND_HOST` | Dockerfile/进程管理器 | gunicorn 监听参数，Python 代码不读取 |

---

## 配置分类索引

| 类别 | 变量数 | 必填（prod） |
|------|--------|--------------|
| [Flask / Session](#flask--session) | 4 | 部分 |
| [Fernet（SSH 凭据加密）](#fernetssh-凭据加密) | 2 | ✅ |
| [MySQL](#mysql) | 5 | ✅ |
| [Redis](#redis) | 8 | ✅ |
| [Mail (SMTP)](#mail-smtp) | 14 | 部分 |
| [SSH / Remote](#ssh--remote) | 7 | ✅ |
| [Login rate limit](#login-rate-limit) | 2 | 推荐 |
| [Captcha](#captcha) | 2 | 推荐 |
| [CSRF](#csrf) | 1 | ✅ |
| [Cron / SFTP](#cron--sftp) | 2 | 推荐 |
| [HTTPS](#https) | 1 | ✅ |
| [Paths](#paths) | 5 | 部分 |

> 注：变量数依据 [backend/.env.example](backend/.env.example) 实际模板统计。如有不一致以 .env.example 为准。

---

## Flask / Session

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_ENV` | `dev` | ✅ | `dev` / `prod`。生产必须设 `prod` |
| `OGS_FLASK_SECRET_KEY` | 空 | ✅ (prod) | Flask session/cookie 签名密钥。生产必须 ≥32 字节随机字符串 |
| `OGS_SESSION_EXP_SECONDS` | `10800` | ❌ | Session 滑动过期时间（秒），默认 3 小时 |
| `OGS_SESSION_RENEW_RATIO` | `0.5` | ❌ | 自动续期阈值（0-1）。剩余 TTL < `阈值 × 总 TTL` 时续期 |

**生成密钥**：
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## Fernet（SSH 凭据加密）

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_FERNET_KEYS` | 空 | ✅ (prod) | **多 key rotation 列表**（逗号分隔，第 1 个为最新），用于 `t_sys_user.host_password` 字段 AES-128-CBC + HMAC-SHA256 对称加密 |
| `OGS_FERNET_KEY` | 空 | ✅ (prod, 兼容) | 单 key 兼容模式（**不推荐**，仅旧版升级期使用） |

**生成密钥**：
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Key Rotation**：
```bash
# 阶段 1: 单 key 起步
OGS_FERNET_KEYS=<key1>
# 阶段 2: 加新 key 到 list[0], 旧 key 留 list[1]
OGS_FERNET_KEYS=<key2>,<key1>
# 阶段 3: 全部迁移完成后移除旧 key
OGS_FERNET_KEYS=<key2>
```

> **重要**：丢失所有 key = 所有已加密的 SSH 主机密码无法恢复，需重新录入。

---

## MySQL

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_MYSQL_HOST` | `127.0.0.1` | ✅ | MySQL 服务器地址。**按部署模式填**（见下表） |
| `OGS_MYSQL_PORT` | `3306` | ❌ | MySQL 端口 |
| `OGS_MYSQL_DBNAME` | `orange` | ❌ | 数据库名（默认 `orange`，需先 `CREATE DATABASE`） |
| `OGS_MYSQL_USER` | `app_user` | ✅ | 数据库用户。**`root` 在启动黑名单里会直接拒绝**，必须用专用业务账号 |
| `OGS_MYSQL_PASSWORD` | 空 | ✅ | 数据库密码 |

**按部署模式填 OGS_MYSQL_HOST / OGS_MYSQL_PORT（生产必读）**：

| 部署模式 | OGS_MYSQL_HOST | OGS_MYSQL_PORT |
|----------|----------------|----------------|
| 物理机 + supervisor（本机数据库） | `127.0.0.1` | `3306` |
| 物理机 + supervisor（远程数据库） | 数据库服务器内网 IP | `3306` |
| Docker Compose | `mysql`（compose 服务名） | `3306` |
| 仅后端 Docker | `host.docker.internal` | `3306` |
| Kubernetes | `mysql.<namespace>.svc.cluster.local` | `3306` |

> ⚠️ `backend/.env.example` 里默认 `OGS_MYSQL_HOST=192.0.2.1` 是**开发者本机 dev 默认值**。config.py fail-fast 会检查这个值是不是占位符（命中 `_DEFAULT_MYSQL_INSECURE` 清单会启动失败），但**只检查 host/user/password**，**不检查 port**。生产前必须按上表改。

**初始化数据库**：
```bash
mysql -u root -p -e "CREATE DATABASE orange DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_general_ci;"
mysql -u root -p orange < backend/mysqldir/orange.sql
```

---

## Redis

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_REDIS_HOST` | `192.0.2.1`（代码内置 dev 值） | ✅ | Redis 服务器地址，生产必改 |
| `OGS_REDIS_PORT` | `6379`（模板；无模板时代码回退 6389） | ✅ | Redis 端口，生产按实际改 |
| `OGS_REDIS_PASSWORD` | 空 | 推荐 (prod) | Redis requirepass（生产环境强烈建议配置，防未授权访问） |
| `OGS_REDIS_DB` | `2` | ❌ | 业务会话和带 TTL 缓存的 db 索引；标准 bundled 栈固定使用 DB 2 |
| `OGS_REDIS_MAX_CONNECTIONS` | `10` | ❌ | 连接池上限 |
| `OGS_REDIS_SOCKET_TIMEOUT` | `5` | ❌ | 单次操作超时秒（防 hang） |
| `OGS_REDIS_CONNECT_TIMEOUT` | `5` | ❌ | 连接超时秒 |
| `OGS_REDIS_SOCKET_KEEPALIVE` | `true` | ❌ | TCP keepalive（防长连接被中间设备切断） |

**按部署模式填 OGS_REDIS_HOST / OGS_REDIS_PORT（生产必读）**：

| 部署模式 | OGS_REDIS_HOST | OGS_REDIS_PORT |
|----------|----------------|----------------|
| 物理机 + supervisor（本机 Redis） | `127.0.0.1` | `6379` |
| 物理机 + supervisor（远程 Redis） | Redis 服务器内网 IP | `6379` |
| Docker Compose | `redis`（compose 服务名） | `6379` |
| 仅后端 Docker | `host.docker.internal` | `6379` |
| Kubernetes | `redis.<namespace>.svc.cluster.local` | `6379` |

> ⚠️ `backend/.env.example` 使用 `OGS_REDIS_HOST=192.0.2.1` /
> `OGS_REDIS_PORT=6379`；如果完全不加载模板，代码回退端口才是 `6389`。
> `192.0.2.1` 是文档保留网段，必然连不上。**config.py 不对 Redis 做
> fail-fast 检查**——生产前必须按上表改。

> **注意**：标准 bundled 栈使用 DB 0 保存 checkpoint/向量、DB 1 作为 Celery broker、DB 2 保存业务会话和带 TTL 缓存。外部 Redis 部署仍可显式覆盖。

---

## Mail (SMTP)

管理员可在“系统设置 → 通知设置”或首次安装向导中配置 SMTP。界面保存的授权码
使用 `OGS_FERNET_KEYS` 加密后写入数据库，接口只返回
`password_configured`，不会回显明文或密文。数据库中存在完整配置时优先使用；
否则回退到下列环境变量，方便现有部署保持原有运维方式。

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_MAIL_USER` | 空 | 部分 | 发件邮箱账号（如 `noreply@company.com`） |
| `OGS_MAIL_PASSWORD` | 空 | 部分 | SMTP **授权码**（不是登录密码） |
| `OGS_MAIL_SMTP` | 空 | 部分 | SMTP 服务器域名 |
| `OGS_MAIL_PORT` | `587` | ❌ | 587=STARTTLS / 465=SSL / 25=裸连 |
| `OGS_MAIL_USE_TLS` | `true` | ❌ | STARTTLS 加密（推荐） |
| `OGS_MAIL_USE_SSL` | `false` | ❌ | SSL 加密（465 端口时启用） |
| `OGS_SMTP_CONNECT_TIMEOUT` | `10` | ❌ | SMTP 连接超时（秒，防慢 SMTP 阻塞） |
| `OGS_SMTP_OP_TIMEOUT` | `30` | ❌ | SMTP 操作超时（秒） |
| `OGS_MAIL_RELAY_LIMIT_MIN` | `5` | ❌ | 邮件中继每分钟限流（防滥发） |
| `OGS_MAIL_RELAY_LIMIT_HOUR` | `30` | ❌ | 邮件中继每小时限流 |
| `OGS_MAIL_VERIFY_PREFIX` | `mail_verification:` | ❌ | 验证码 Redis key 前缀 |
| `OGS_MAIL_VERIFY_TTL` | `180` | ❌ | 验证码 Redis TTL（秒） |
| `OGS_EMAIL_MAX_LEN` | `254` | ❌ | 邮箱地址最大长度（RFC 5321） |
| `OGS_HEADER_MAX_LEN` | `200` | ❌ | 邮件 Header 最大长度 |
| `OGS_MESSAGE_MAX_LEN` | `10000` | ❌ | 邮件正文最大长度 |

> 邮箱功能为可选项。未配置时注册验证码、找回密码和邮件中继接口会明确返回
> “管理员尚未配置邮件服务”，不会尝试连接占位服务器。

---

## SSH / Remote

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_SSH_HOST_KEY_POLICY` | `reject`（代码默认） | ✅ | 主机 key 校验策略：`reject`（默认，需预置 known_hosts）/ `auto`（自动接受新主机 key，便利性优先）/ `warning`（报警放行） |
| `OGS_SSH_CONNECT_TIMEOUT` | `10` | ❌ | SSH 连接超时（秒） |
| `OGS_SSH_KEEPALIVE_INTERVAL` | `60` | ❌ | SSH keepalive 间隔（秒），`0`=禁用 |
| `OGS_SSH_CMD_MAX_OUTPUT_BYTES` | `10485760` | ❌ | `ssh_cmd` stdout/stderr 输出上限（字节，默认 10MB，防 OOM） |
| `OGS_SSH_CMD_TIMEOUT` | `30` | ❌ | `ssh_cmd` 单命令执行 select 轮询超时（防慢命令永久挂起 worker） |
| `OGS_SSH_DANGEROUS_COMMANDS` | `rm -rf /,mkfs,...` | ❌ | SSH 危险命令黑名单（逗号分隔），生产只追加不删默认项 |
| `OGS_SUBPROCESS_ENCODING` | `utf-8` | ❌ | subprocess shell 输出编码（防二进制输出解码错误） |

**默认危险命令清单**（禁止覆盖，仅可追加）：
```
rm -rf /, mkfs, dd if=, shutdown, reboot, init 0, init 6, halt, poweroff,
:(){:|:&};:, chmod -R 777 /, chown -R
```

---

## Login rate limit

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_LOGIN_FAIL_LIMIT` | `5` | ❌ | 账号维度失败阈值（防单账号暴力破解） |
| `OGS_LOGIN_FAIL_LIMIT_IP` | `20` | ❌ | IP 维度失败阈值（防公司 NAT 出口共享 IP 误锁全公司） |

---

## Captcha

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_CAPTCHA_GET_LIMIT_MIN` | `10`（模板；无模板时代码回退 30） | ❌ | `CaptchaGet` IP 限流（次/分钟，防 Redis 内存膨胀同时给用户重试余量） |
| `OGS_CAPTCHA_GET_PREFIX_MIN` | `captcha_get_min:` | ❌ | 限流 Redis key 前缀 |

---

## CSRF

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_CSRF_ALLOWED_ORIGINS` | 空 | ✅ (跨域) | Origin/Referer 白名单（逗号分隔），仅 scheme+netloc |

**配置示例**：
```bash
# 生产单域名
OGS_CSRF_ALLOWED_ORIGINS=https://app.example.com

# 多域名
OGS_CSRF_ALLOWED_ORIGINS=https://app.example.com,https://admin.example.com

# 开发容器由 Vite 同源代理到 backend，并保留浏览器 Host，无需额外 CORS/CSRF Origin。
OGS_CSRF_ALLOWED_ORIGINS=
```

---

## Cron / SFTP

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_CRON_RESULT_RETENTION` | `604800` | ❌ | cron `last_result` Redis TTL（秒，默认 7 天，防永久膨胀） |
| `OGS_SFTP_MAX_UPLOAD_SIZE` | `209715200` | ❌ | sftp 上传总大小限制（字节，默认 200MB，防磁盘填充 DoS） |

---

## AI Provider

模型 Base URL、模型名称和 API Key 由管理员在“系统设置 → AI 模型服务”中保存到
`t_ai_provider`。API Key 使用 `OGS_FERNET_KEYS` 加密，环境变量中不配置厂商密钥。
每个 Provider 还保存管理员确认的模型上下文能力：默认 256K，也可标记为 1M。
AI 运维会话默认使用 256K；只有能力标记为 1M 的 Provider 才能创建 1M 深度诊断会话。

已有实例的数据库准备、备份和 rev48/rev49/rev50 执行顺序只在
[统一升级流程](docs/operations/UPGRADE.md) 维护。完整设置步骤和上下文行为见
[AI Provider 与上下文](docs/ai/PROVIDER_AND_CONTEXT.md)。

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_AI_ALLOW_PRIVATE_PROVIDER` | `0` | ❌ | 是否允许 Provider Base URL 解析到私网、环回或链路本地地址。只有受控的私有模型网关部署才可设为 `1` |
| `OGS_AI_DIAGNOSTIC_EVIDENCE_RETENTION_DAYS` | `7` | ❌ | AI 诊断原始脱敏证据保留天数，允许范围 1–3650 |
| `OGS_AI_DIAGNOSTIC_REPORT_RETENTION_DAYS` | `90` | ❌ | AI 诊断结构化报告与审计引用保留天数，允许范围 1–3650 |
| `OGS_AI_AUTONOMY_ENABLED` | 空（进程关闭） | ❌ | 进程级关闭开关。bundled Compose 默认写入 `true`。设为 `false` 可强制关闭全部自治 API |
| `OGS_AI_AUTONOMY_REDIS_HOST` | 空 | 启用自治时 ✅ | 自治 Redis 地址；bundled 栈与业务缓存共用 `redis` 服务并按 DB 隔离 |
| `OGS_AI_AUTONOMY_REDIS_PORT` | `6379` | ❌ | 自治 Redis 端口；DB 0 用于 checkpoint/向量，DB 1 用于 Celery broker |
| `OGS_AI_AUTONOMY_REDIS_PASSWORD` | 空 | 网络部署时 ✅ | 自治 Redis 密码；bundled 栈与 `OGS_REDIS_PASSWORD` 相同，URI 保留字符由后端安全编码 |
| `OGS_AUTONOMY_WORKER_CONCURRENCY` | `2` | ❌ | Celery prefork 并发槽数，必须为正整数；受限机器可设为 `1` |
| `OGS_AI_AUTONOMY_LEASE_TTL_SECONDS` | `120` | ❌ | Worker 租约有效期，服务端硬下限 10 秒 |
| `OGS_AI_AUTONOMY_APPROVAL_TTL_SECONDS` | `86400` | ❌ | draft 和待审批步骤有效期，服务端硬下限 60 秒 |

标准 bundled Compose 会启动统一 Redis 8 与 Worker，自治默认可用。
`OGS_AI_AUTONOMY_ENABLED=false` 只作为紧急关闭。见
[部署手册](DEPLOY.md)与[AI 运维使用指南](docs/ai/USER_GUIDE.md)。

默认支持 OpenAI、Anthropic、xAI、DeepSeek、MiniMax、Kimi、Qwen、GLM 和硅基流动。一个 Provider
配置一个模型；管理员可以读取厂商模型列表，也可以直接填写模型 ID。Anthropic 原生 API 非
OpenAI 兼容，需通过中转代理（如 OpenRouter）接入；xAI 原生兼容。

---

## HTTPS

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_HTTPS` | `false` | ✅ (prod) | `true` / `1` / `yes` / `on` 任一即视为 HTTPS 模式，Flask cookie 加 Secure 标志 |

> 仅当 Nginx 终止 TLS 反代 HTTP 到 gunicorn 时才设 `true`。

---

## Paths

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `OGS_DATA_DIR` | 空 | ❌ | 运行时数据目录（日志/上传/密钥），不设走当前工作目录下 `data/` |
| `OGS_DEFAULT_DIR1_PATH` | `ls /data` | ❌ | `/local/dir/group` 默认目录命令 |
| `OGS_DEFAULT_DIR2_PATH` | `ls /data/%s` | ❌ | `/local/dir/group` 子目录命令 |
| `OGS_RSYNC_SHELL_CMD` | `/usr/bin/rsync -av /data/%s/%s /data/tmp/` | ❌ | rsync 模板命令 |

---

## 完整 .env 示例（最小可用）

```bash
# 必填（生产）
OGS_ENV=prod
OGS_FLASK_SECRET_KEY=<48字节随机字符串>
OGS_FERNET_KEYS=<Fernet.generate_key()>          # R1-22 后推荐多 key rotation（单 key 起步）
OGS_MYSQL_HOST=mysql                              # docker compose 下为容器名；物理机部署改 127.0.0.1
OGS_MYSQL_USER=app_user
OGS_MYSQL_PASSWORD=<强密码>
OGS_REDIS_HOST=redis
OGS_REDIS_PASSWORD=<Redis requirepass>            # 生产必填
OGS_REDIS_DB=2
OGS_HTTPS=true
OGS_SSH_HOST_KEY_POLICY=reject
OGS_CSRF_ALLOWED_ORIGINS=https://your.domain.com

# 可选
OGS_SESSION_EXP_SECONDS=10800
OGS_SSH_DANGEROUS_COMMANDS=rm -rf /,mkfs,dd if=,shutdown,reboot
OGS_LOGIN_FAIL_LIMIT=5
OGS_LOGIN_FAIL_LIMIT_IP=20
OGS_CAPTCHA_GET_LIMIT_MIN=10
```

---

## 相关文档

- 部署详细步骤：[DEPLOY.md](DEPLOY.md)
- 后端模块说明：[backend/README.md](backend/README.md)
- 配置模板：[backend/.env.example](backend/.env.example)

---
