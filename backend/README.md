# OgsBackend — OrangeServer 后端服务

## 简介

OrangeServer（橘子平台）是一款面向运维场景的资产 / 命令 / 文件 / 定时任务一体化管理平台。本目录为后端 API 服务，基于 **Python 3.12 + Flask 3.1 + gevent + geventwebsocket** 构建，提供资产管理、Web SSH、SFTP 文件传输、定时任务、权限审计等能力。

前端项目：[frontend/](../frontend/)

---

## 功能模块

| 模块 | 状态 | 说明 |
|------|------|------|
| 资产管理 | ✅ | 主机/主机组 CRUD，按组筛选 |
| 用户管理 | ✅ | 平台用户/用户组 CRUD |
| 系统用户 | ✅ | SSH 凭据管理（Fernet 加密密码 + 私钥） |
| 权限管理 | ✅ | 关联表权限模型（用户/组 → 主机/组 → 系统用户） |
| Web SSH | ✅ | WebSocket 直连终端（支持多标签 + 会话录制） |
| SFTP 文件传输 | ✅ | 直连资产流式传输 + MIME 嗅探 + 危险扩展名拦截 |
| 批量命令 | ✅ | 多主机 SSH 批量执行 + 危险命令拦截 + 完整审计 |
| 批量脚本 | ✅ | Shell 脚本拖拽上传 + 批量执行 |
| 定时任务 | ✅ | APScheduler 内存调度 + Redis 缓存最新结果 |
| 日志审计 | ✅ | 登录日志 / 命令日志 / 操作日志（统一 `CzToolsLog` 审计基类） |
| AI 运维 | ✅ | OpenAI-compatible 模型接入、平台工具调用与批量命令审批 |
| 系统设置 | ✅ | 安全策略 / 终端 / 审计 / 文件传输 / 通知 / 界面语言 |
| 统计图表 | ✅ | 登录/用户/错误趋势 |
| 验证码 | ✅ | PIL 图形验证码 + IP 限流（30 次/分钟） |

---

## 技术栈

| 层 | 技术 |
|----|------|
| Web 框架 | Flask 3.1 + gevent WebSocket |
| 调度器 | APScheduler（内存模式） |
| ORM | SQLAlchemy 2.0 + Flask-SQLAlchemy |
| 数据库 | MySQL 8.0（utf8mb4） |
| 缓存 | Redis 7（默认 DB 0，业务库隔离走 key prefix） |
| SSH | paramiko 3.x |
| WebSocket | gevent-websocket |
| 密码哈希 | bcrypt |
| 对称加密 | cryptography.Fernet（AES-128-CBC + HMAC-SHA256） |
| 限流 | Redis 计数器 |
| 审计 | 自研 `CzToolsLog` 基类 + 三张日志表 |

---

## 目录结构

```
backend/
├── app/
│   ├── api/                   # 路由注册（ROUTES 声明式）
│   │   ├── __init__.py
│   │   ├── account_api.py     # 用户/组/日志路由
│   │   ├── auth_api.py        # 权限路由
│   │   ├── local_api.py       # 本地命令/文件路由
│   │   └── server_api.py      # 服务器资产/SSH路由
│   ├── core/                  # 基础设施
│   │   ├── config.py          # 配置加载（OGS_* 环境变量）
│   │   └── db/
│   │       ├── database.py    # 23 张表 ORM 模型
│   │       └── settings.py    # SQLAlchemy 实例 + session
│   ├── audit/                 # 审计日志
│   │   └── loginlogs.py       # 登录日志查询
│   ├── auth/                  # 认证
│   │   └── AuthHost.py        # 权限规则模型
│   ├── ai/                    # AI Provider、会话、工具与审批
│   ├── cron/                  # 定时任务
│   │   ├── CronSettings.py    # APScheduler 初始化
│   │   └── cron.py            # 任务 CRUD + 执行回调
│   ├── files/                 # 文件上传/管理
│   │   └── file.py            # 路径校验 + MIME 嗅探 + 危险扩展名拦截
│   ├── local/                 # 本地操作
│   │   ├── Basics.py          # LocalDirList / LocalShell
│   │   ├── Captcha.py         # 验证码生成/校验
│   │   ├── LocalInit.py       # 启动初始化
│   │   ├── LocalShell.py      # shell 执行
│   │   ├── Settings.py        # t_settings 模型
│   │   └── download.py        # 文件下载
│   ├── mail/                  # 邮件
│   │   └── MailApi.py         # SMTP 发送 + 限流
│   ├── ssh/                   # Web SSH / SFTP
│   │   ├── sftp.py            # SFTP WebSocket
│   │   └── webssh.py          # SSH WebSocket
│   ├── assets/                # 资产
│   │   ├── ServerGroup.py     # 主机组 CRUD
│   │   ├── ServerManagement.py # ServerCmd（含 CzToolsLog 审计）
│   │   └── SysUser.py         # 系统用户（SSH 凭据）
│   ├── users/                 # 平台用户
│   │   ├── group.py           # 用户组 CRUD
│   │   └── user.py            # 用户 CRUD
│   ├── tools/                 # 通用工具
│   │   ├── apierr.py          # 统一响应包装（ApiCode + api_response/api_error）
│   │   ├── at.py              # 鉴权装饰器（require_role / ogs_auth_token / ws_auth）
│   │   ├── audlog.py          # CzToolsLog 审计基类
│   │   ├── basesec.py         # Fernet 加解密
│   │   ├── csrf.py            # CSRF token + Origin/Referer 校验
│   │   ├── redisdb.py         # Redis 连接池
│   │   ├── shellcmd.py        # SSH 连接工厂
│   │   ├── sendmail.py        # 邮件发送
│   │   ├── ws_helper.py       # WebSocket 握手辅助
│   │   ├── auto_update.py     # 自动更新
│   │   ├── SqlListTool.py     # 列表/字典处理
│   │   └── migrate_comma_to_junction.py  # 旧数据迁移脚本
│   ├── conf/                  # 配置目录（保留兼容）
│   ├── app_factory.py         # Flask app 工厂
│   └── __init__.py
├── tests/                     # 单元与集成测试
├── data/                      # 运行时数据（日志/上传/加密 key）
├── mysqldir/
│   ├── orange.sql                 # 建库脚本（18 表 + 种子数据）
│   ├── rev20_p2_4_low7_y2038.sql  # REV20 2038 问题修复
│   ├── rev45_h1_h2_acc_user_unique.sql  # REV45 acc_user.name 唯一约束
│   ├── rev45_h3_h4_h5_fk_length.sql     # REV45 外键 + 字段长度统一
│   ├── rev45_h6_cron_host_fk.sql         # REV45 t_cron.host 外键
│   ├── rev47_h7_cron_owner_fk.sql        # REV47 t_cron.job_owner 外键
│   ├── rev47_h8_timestamps.sql           # REV47 时间戳字段
│   ├── rev47_h9_password_version.sql     # REV47 密码版本字段
│   └── rev47_m6_soft_delete.sql          # REV47-M6 全表 soft_delete 字段
├── ops/                       # 运维脚本（顶层 ops/ 软链，不在本目录内）
├── Dockerfile                 # 后端容器镜像（多阶段构建，3 stage）
├── requirements.txt           # Python 依赖（pin 版本，hash 校验）
├── requirements.in            # 依赖源清单（pip-compile 输入）
├── init.py                    # 入口文件（启动后端）
└── README.md
```

---

## 快速部署

### 推荐方式：Docker Compose

统一入口是仓库根目录的 `make docker-up`（bundled）/ `make docker-up-host`（外部
MySQL/Redis），完整流程见顶层 [DEPLOY.md](../DEPLOY.md) §Docker Compose 部署。

> 不要在本目录手敲裸 `docker compose` 命令：会绕过预检、缺 `--env-file` 与
> `--profile bundled`，且 compose 模式后端端口不对宿主发布（健康检查走
> `http://127.0.0.1:8080/local/health`）。

### 物理机部署（CentOS / RHEL）

一键安装：`sudo bash ../ops/install.sh`（Debian/Ubuntu 用 `install-debian.sh`）。
supervisor / systemd 的启动与环境变量注入方式见
[DEPLOY.md](../DEPLOY.md) §物理机部署（supervisor 需 `systemctl edit supervisor`
注入 EnvironmentFile，`source` env 文件对已运行的 daemon 无效）。

### 开发模式

```bash
# 启动后端 dev server（端口 28000）
python init.py
```

> 前端开发服务器：`cd ../frontend && npm run dev`（端口 5173）

---

## 数据库结构

共 **23 张表**（`orange.sql` 导入 + `db.create_all()` 自动补建；数量随迁移演进，
以 `orange.sql` 的 `CREATE TABLE` 为准）。

### 核心业务表

| 表 | 用途 |
|----|------|
| `t_host` | 主机资产（alias, host_ip, host_port, group） |
| `t_group` | 主机组分类 |
| `t_sys_user` | 系统用户/SSH 凭据（Fernet 加密 password） |
| `t_acc_user` | 平台用户（bcrypt 哈希 password，128 字符邮箱） |
| `t_acc_group` | 平台用户组 |
| `t_auth_host` | 权限规则主表 |
| `t_auth_host_user` | 权限 → 用户 |
| `t_auth_host_user_group` | 权限 → 用户组 |
| `t_auth_host_host_group` | 权限 → 主机组 |
| `t_auth_host_sys_user` | 权限 → 系统用户 |
| `t_cron` | 定时任务主表 |
| `t_cron_host` | 定时 → 主机 |
| `t_cron_group` | 定时 → 主机组 |
| `t_settings` | 系统设置（安全/终端/审计/文件/通知/界面语言） |

### 日志 / 统计表

| 表 | 用途 |
|----|------|
| `t_login_log` | 登录日志（含 IP/UA/状态/原因） |
| `t_command_log` | 命令执行日志 |
| `t_cz_log` | 操作审计日志（统一通过 `CzToolsLog.host_log` 写入） |
| `t_line_chart` | 统计图表数据 |

### 初始化种子数据

| 数据 | 用途 |
|------|------|
| 管理员账号 | `admin / admin` |
| 管理员组 | `admin`（id=1） |
| 默认权限 | "所有权限"（关联管理员用户+组） |
| 系统设置 | 全部使用默认值（可后台修改） |

---

## API 接口规范

### 路由注册（ROUTES 声明式）

旧业务路由通过 `app/api/*_api.py` 的 `ROUTES` 列表统一注册（REV38-M1 后使用
`route()` 构造统一的 9 字段 `RouteRule`）。AI Agent 因为需要 REST 动词、
URL 参数和 POST SSE，由 `app/api/ai_api.py` 单独注册：

```python
ROUTES = [
    route('/account/login_dl2', UserLogin2, 'login_dl',
          need_auth=False, is_property=False, skip_csrf=True),
    route('/account/group/list', AccGroupList, 'group_list',
          is_property=False, roles=['admin']),
    route('/server/host/cmd', ServerCmd, 'sh_cmd',
          description='远程执行 SSH 命令'),
    # ...
]
```

每条 `RouteRule` 字段：
1. `URL` - 路径
2. `Class` - 视图类（必须包含对应 `method` 方法）
3. `method` - 调用的实例方法名
4. `need_auth` - 是否需要登录
5. `is_property` - 是否作为属性调用
6. `roles` - 允许角色
7. `description` - 接口描述
8. `skip_csrf` - 是否跳过 CSRF（仅公开接口）
9. `is_alias` - 是否为兼容别名路由

### AI Agent API

| API | 方法 | 说明 |
|---|---|---|
| `/ai/providers` | GET | 当前用户可用 Provider |
| `/ai/admin/providers` | GET | 管理员读取 Provider 配置（不含密钥） |
| `/ai/admin/providers/<code>` | PUT | 管理员保存加密配置 |
| `/ai/admin/providers/<code>/test` | POST | Tool Calling 连接测试 |
| `/ai/admin/providers/<code>/models` | POST | 使用已保存密钥发现模型 |
| `/ai/admin/providers/<code>/clear-key` | POST | 清除密钥并禁用 Provider |
| `/ai/conversations` | GET / POST | 会话列表与创建 |
| `/ai/conversations/<id>` | GET / DELETE | 恢复或删除会话 |
| `/ai/chat` | POST SSE | 模型增量、只读工具和自治草稿引用 |
| `/ai/results/<id>` | GET | 权威结果集分页 |
| `/ai/diagnostic-profiles` | GET | 服务端固定只读诊断档案 |
| `/ai/diagnostics` | POST | 启动受控诊断 |
| `/ai/diagnostics/<id>` | GET | 权威诊断 Run 快照 |
| `/ai/diagnostics/<id>/cancel` | POST | 请求取消诊断 |
| `/ai/diagnostics/<id>/evidence` | GET | 所有者可见的脱敏证据 |
| `/ai/diagnostics/<id>/report` | GET | 确定性规则报告 |

会话和结果集使用 Redis 保存；聊天 TTL 7 天、每用户最多 20 个会话。聊天记录不
永久写入数据库，远程写操作只允许通过 Autonomy Run 执行。
诊断 Run、事件、加密证据和报告写入 MySQL；证据默认保留 7 天，报告、Run 与
事件默认保留 90 天并级联清理，保留期可配置。服务端固定探针不接受模型提交的
Shell。

请求字段、响应投影、上下文档位和 SSE 事件契约见
[AI REST 与 SSE 契约](../docs/ai/API.md)。

### 统一响应格式

所有路由通过 [apierr.py](app/tools/apierr.py) 包装：

```json
{
  "code": 0,
  "msg": "ok",
  "data": { ... }
}
```

错误响应使用 `api_error(ApiCode.BUSINESS_UNAUTHORIZED, '未授权访问')`，
HTTP 状态码自动映射（如 `code=3/100 → 401`，`code=4 → 403`）。

完整错误码清单见 [apierr.py](app/tools/apierr.py) `ApiCode` 类。

### 装饰器链

所有需鉴权路由的装饰器链（顺序固定）：

```python
@csrf_protect         # 1. CSRF Origin/Referer + token 校验
@ogs_auth_token       # 2. Session 鉴权（写入 g.username）
@require_role('admin')# 3. 角色校验（可选）
def view_func():
    ...
```

错误响应统一通过 `api_error()` 返回，自动带 HTTP 状态码（403 / 401）。

---

## 测试

```bash
# 跑全部测试
pytest tests/ -v

```

测试覆盖：
- API 路由注册 + 装饰器链
- 鉴权装饰器（require_role / ws_auth）
- CSRF 校验
- 命令执行审计
- 文件上传 MIME 嗅探
- 数据库 ORM 模型
- 错误响应统一格式

---

## 相关文档

- [CONFIG.md](../CONFIG.md) — OGS_* 环境变量完整清单
- [DEPLOY.md](../DEPLOY.md) — 部署详细步骤
- [../docs/operations/UPGRADE.md](../docs/operations/UPGRADE.md) — 数据库升级、验证与回滚
- [../docs/ai/API.md](../docs/ai/API.md) — AI REST/SSE 契约
- [../README.md](../README.md) — 项目总览
- [../frontend/README.md](../frontend/README.md) — 前端文档

---

## License

见项目根目录 [Apache License 2.0](../LICENSE)。

