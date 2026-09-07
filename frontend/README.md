# OrangeServer Frontend — 运维管理平台前端

> Vue 3 + Vite 8 + TypeScript + Element Plus 构建的 OrangeServer 运维堡垒机前端。
> 后端项目：[../backend](../backend/)

---

## 技术栈（实际版本，2026-07）

| 层 | 技术 | 版本 |
|----|------|------|
| 框架 | Vue 3（Composition API） | ^3.5.34 |
| 语言 | TypeScript | ^5.9.3 |
| 构建 | Vite | ^8.0.12 |
| UI 库 | Element Plus + Icons | ^2.14.1 / ^2.3.2 |
| 终端 | xterm.js + fit addon | ^6.0.0 / ^0.11.0 |
| 图表 | ECharts + vue-echarts | ^6.1.0 / ^8.0.1 |
| HTTP | Axios | ^1.18.1 |
| 路由 | Vue Router | ^4.6.4 |
| URL 工具 | qs | ^6.15.2 |
| 类型检查 | vue-tsc | ^2.2.12 |

> 版本数据来自 [package.json](./package.json)。如升级版本请同步更新本表。

---

## 目录结构

```
frontend/
├── src/
│   ├── api/
│   │   └── index.ts                # Axios 实例 + 全部 API 封装
│   ├── assets/                     # 静态资源
│   ├── components/                 # 公共组件
│   ├── composables/                # 可复用逻辑（composable）
│   ├── router/
│   │   └── index.ts                # 路由表 + 鉴权守卫
│   ├── store/                      # Pinia 状态管理
│   ├── styles/
│   │   └── index.css               # 全局样式
│   ├── types/                      # TypeScript 类型定义
│   ├── utils/                      # 通用工具函数
│   │   ├── danger.ts               # 危险命令检测
│   │   ├── datetime.ts             # 日期时间格式化
│   │   ├── dev-auth-mock.ts        # 开发态鉴权 mock
│   │   ├── groupClassifier.ts      # 主机/用户组分类
│   │   ├── host.ts                 # 主机状态解析
│   │   ├── logStatus.ts            # 日志状态文本映射
│   │   └── ws.ts                   # WebSocket 封装
│   ├── views/                      # 页面组件
│   │   ├── Layout.vue              # 主布局（侧边栏 + 顶栏）
│   │   ├── Dashboard.vue           # 仪表盘
│   │   ├── Login.vue / Register.vue / UserInfo.vue
│   │   ├── HostList.vue / GroupList.vue     # 资产管理
│   │   ├── SysUserList.vue                   # 系统用户（SSH 凭据）
│   │   ├── UserList.vue / UserGroupList.vue  # 平台用户/组
│   │   ├── Authority.vue                     # 权限规则
│   │   ├── BatchCommand.vue / BatchScript.vue
│   │   ├── Cron.vue              # 定时任务
│   │   ├── RemoteSession.vue     # WebSSH / SFTP 统一入口
│   │   ├── FileTransfer.vue      # 文件传输
│   │   ├── Settings.vue          # 系统设置
│   │   ├── AIAgent.vue           # AI 运维 Agent
│   │   └── Audit*Log.vue (3 个)  # 登录/命令/操作日志
│   ├── App.vue
│   └── main.ts                    # 入口
├── public/                        # 不参与构建的静态文件（favicon 等）
├── index.html
├── vite.config.ts                 # Vite 配置（含 API 路径代理）
├── tsconfig.json
├── .env.development               # 开发环境变量
├── .env.production                # 生产环境变量
└── package.json
```

---

## 快速开始

### 安装与开发

```bash
cd frontend
npm install        # 或 pnpm install / yarn install
npm run dev        # 启动 Vite dev server，默认 http://localhost:5173
```

启动前**必须**配置 `frontend/.env.development`：

```bash
# 后端 API 地址（Vite 代理目标）
VITE_API_TARGET=http://127.0.0.1:28000

# WebSocket 直连地址（开发环境不走 Vite 代理）
VITE_WS_URL=ws://127.0.0.1:28000/local/websocket
```

> 启动时 `vite.config.js` 会强校验 `VITE_API_TARGET` 是否配置（防硬编码内网 IP 泄露后端拓扑）。

### 类型检查与构建

```bash
npm run type-check  # vue-tsc --noEmit，纯类型检查
npm run build       # vue-tsc --noEmit && vite build，输出到 dist/
npm run preview     # 本地预览 dist/
```

---

## 前后端联动

### 1. 请求代理链路

```
浏览器 ──→ Vite Proxy (开发) / Nginx (生产) ──→ Flask 后端 (:28000)
       ↑ API 路径代理 (vite.config.ts)      ↑ 鉴权（ogs_token cookie）
       ↑ 全部 API + 静态资源               ↑ ROUTES 声明式路由（88 条）
```

### 2. Vite 开发代理

[vite.config.ts](./vite.config.ts) 中配置的代理前缀（`server.proxy`）：

| 前缀 | 代理目标 | 说明 |
|------|---------|------|
| `/local` | → 后端 :28000 | 本地命令/文件/WebSocket 握手 |
| `/server` | → 后端 :28000 | 资产管理 + WebSSH/SFTP 业务 |
| `/account` | → 后端 :28000 | 用户/登录/会话 |
| `/auth` | → 后端 :28000 | 权限管理 |
| `/ai` | → 后端 :28000 | AI 对话、模型配置与操作审批 |
| `/mail` | → 后端 :28000 | 邮件 |

> **WebSocket 说明**：开发环境 WebSSH/SFTP 走 `VITE_WS_URL` **直连后端**，不经过 Vite 代理（Vite 8.x 的 WS 代理不转发升级请求，会导致 1005 关闭码）。

### 3. Axios 封装（[src/api/index.ts](./src/api/index.ts)）

```ts
// 请求拦截：POST 统一发 application/x-www-form-urlencoded
// 数组参数 → key=a&key=b（与 jQuery traditional:true 一致）
http.interceptors.request.use(config => {
  const params = new URLSearchParams()
  // 自动扁平化对象 + 数组 repeat 编码
  config.headers['Content-Type'] = 'application/x-www-form-urlencoded'
  ...
})

// 响应拦截：自动提取 res.data，401 跳登录
http.interceptors.response.use(
  res => res.data,
  err => { if (err.response?.status === 401) window.location.href = '/login' }
)
```

所有 API 函数集中导出，视图层直接 `import { getHostList, pauseCron, ... } from '@/api'` 使用。

AI 接口有一个受限例外：`src/utils/aiStream.ts` 是 AI 功能的专用传输边界，
集中负责 POST SSE 及同一 AI 生命周期内的 JSON 请求。`EventSource` 不支持
POST，Axios 也不能消费浏览器流式响应，而 Provider 配置还需要保留嵌套 JSON，
因此 AI 页面可直接使用该模块并发送 `application/json`。该模块必须继续携带
同源会话 Cookie、CSRF Header，并与 Axios 客户端保持相同的 401 跳转行为；
其他业务接口仍遵循上面的 Axios 与 `@/api` 约定。

### 4. 鉴权流程

```
[用户登录] → POST /account/login_dl2
            → 后端生成 token
            → Set-Cookie: ogs_token
            → 浏览器自动存 Cookie
            → 每次请求自动携带 Cookie
            → @ogs_auth_token 装饰器校验
            → 有效：放行；无效：code:3 (401)
            → 前端 401 拦截 → 跳转 /login
```

路由守卫（[src/router/index.ts](./src/router/index.ts)）：
- 每次页面切换调用 `/local/app_auth_ck` 验证 token
- `code=3` 或网络错误 → 跳转登录页
- `/login` / `/register` / `/user-info` 页免鉴权

### 5. WebSocket 连接（WebSSH / SFTP，统一在 `RemoteSession.vue`）

| 环境 | WebSocket URL | 说明 |
|------|--------------|------|
| 开发 | `ws://{VITE_API_TARGET 主机}/local/websocket` | 直连后端，不经过 Vite 代理 |
| 生产 | `wss://{domain}/local/websocket` | Nginx 代理，需配置 Upgrade 头 |

```nginx
# Nginx 生产环境 WebSocket 配置（关键）
location /local/websocket {
    proxy_pass http://127.0.0.1:28000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}
```

> **常见问题**：WebSSH 出现 `1005` 关闭码，说明 WebSocket 升级请求未正确转发。检查 Nginx `Upgrade` / `Connection` 头配置，或确认 Vite 代理 **不走 WS**（开发环境用 `VITE_WS_URL` 直连）。

---

## 页面路由表

| 路径 | 页面 | 鉴权 | 说明 |
|------|------|------|------|
| `/login` | Login | ❌ | 登录 |
| `/register` | Register | ❌ | 注册（需邮箱验证码） |
| `/user-info` | UserInfo | ❌ | 个人信息（设免鉴权以便修改密码） |
| `/` → `/dashboard` | Dashboard | ✅ | 仪表盘（统计图表） |
| `/host-list` | HostList | ✅ | 资产列表（CRUD + 批量删除 + 搜索/筛选） |
| `/group-list` | GroupList | ✅ | 资产组 |
| `/sys-user` | SysUserList | ✅ | 系统用户/SSH 凭据 |
| `/user-list` | UserList | ✅ | 平台用户 |
| `/user-group` | UserGroupList | ✅ | 用户组 |
| `/authority` | Authority | ✅ | 权限规则 |
| `/batch-command` | BatchCommand | ✅ | 批量命令执行 |
| `/batch-script` | BatchScript | ✅ | 批量脚本（拖拽上传） |
| `/cron` | Cron | ✅ | 定时任务（启停 + 日志） |
| `/remote-session` | RemoteSession | ✅ | WebSSH + SFTP 统一入口（xterm.js） |
| `/file` | FileTransfer | ✅ | SFTP 文件传输 |
| `/settings` | Settings | ✅ | 系统设置（安全/终端/审计/通知） |
| `/log-login` | AuditUserLog | ✅ | 登录日志 |
| `/log-exec` | AuditComLog | ✅ | 执行日志 |
| `/log-op` | AuditCzLog | ✅ | 操作日志 |
| `/ai-ops` | AiOpsShell / AIAgent | ✅ user/admin | AI 运维工作台与当前对话 |
| `/ai-ops/tasks` | AiRuns | ✅ user/admin | 按状态分组的自治任务 |
| `/ai-ops/tasks/:runId` | AiRunDetail | ✅ user/admin | Run 的审批、证据、验证和结论 |
| `/ai-ops/alerts` | AiRuns | ✅ user/admin | Alertmanager 触发的 Run |
| `/ai-ops/sources` | Settings | ✅ admin | 监控数据源与资产映射 |
| `/ai-knowledge` | AiKnowledge | ✅ user/admin | 知识源、索引状态和检索测试 |
| `/ai-agent`, `/ai-runs*` | Redirect | ✅ user/admin | 兼容旧书签，重定向到新工作台 |

---

## 生产环境部署

### Nginx 完整配置

```nginx
server {
    listen 80;
    server_name your-domain.com;

    # 前端静态文件
    location / {
        root /data/www/orangefront;     # dist/ 解压到此
        index index.html;
        try_files $uri $uri/ /index.html;
    }

    # ⚠️ WebSocket 必须放在同路径普通代理之前（Nginx 最长匹配优先）
    location /local/websocket {
        proxy_pass http://127.0.0.1:28000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host:$server_port;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 3600s;
    }

    # 后端 API 代理（与 Vite 代理对齐）
    location /local/      { proxy_pass http://127.0.0.1:28000; proxy_set_header X-Real-IP $remote_addr; }
    location /server/     { proxy_pass http://127.0.0.1:28000; proxy_set_header X-Real-IP $remote_addr; }
    location /account/    { proxy_pass http://127.0.0.1:28000; proxy_set_header X-Real-IP $remote_addr; }
    location /auth/       { proxy_pass http://127.0.0.1:28000; proxy_set_header X-Real-IP $remote_addr; }
    location /ai/ {
        proxy_pass http://127.0.0.1:28000;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
        proxy_read_timeout 600s;
    }
    location /mail/       { proxy_pass http://127.0.0.1:28000; proxy_set_header X-Real-IP $remote_addr; }
}
```

### 构建部署命令

```bash
# 本机构建
npm run build            # 输出到 dist/

# 部署到 Nginx
cp -r dist/* /data/www/orangefront/

# 重载 Nginx
nginx -s reload
```

或使用项目顶层 [Makefile](../Makefile)：

```bash
make build               # 一站式构建（含后端 Docker 镜像）
```

或直接用 [deploy/docker-compose.yml](../deploy/docker-compose.yml) 一键起整套服务栈（前端走 nginx:alpine 容器）。

---

## 环境变量

| 变量 | 文件 | 必填 | 说明 |
|------|------|------|------|
| `VITE_API_TARGET` | `.env.development` | ✅ | 开发环境后端 HTTP 地址（Vite 代理目标） |
| `VITE_WS_URL` | `.env.development` | ✅ | WebSocket 直连地址（不走 Vite 代理） |
| `VITE_API_BASE_URL` | `../.env` | 顶层 | 生产构建时注入 API baseURL（默认 `/api`） |
| `VITE_WS_BASE_URL` | `../.env` | 顶层 | 生产构建时注入 WS baseURL（默认 `/ws`） |

> `.env.production` 通常留空，`VITE_WS_URL` 不设时 `RemoteSession.vue` 会自动用 `ws(s)://{host}/local/websocket`。

---

## 相关文档

- [../README.md](../README.md) — 项目总览
- [../README.zh-CN.md](../README.zh-CN.md) — 中文完整首页
- [../DEPLOY.md](../DEPLOY.md) — 三种部署方式
- [../docs/ai/API.md](../docs/ai/API.md) — AI REST/SSE 传输契约
- [../CONFIG.md](../CONFIG.md) — 后端 OGS_* 环境变量参考
- [../backend/README.md](../backend/README.md) — 后端模块说明
- [../deploy/README.md](../deploy/README.md) — Tier 2 部署指南

---

## License

见项目根目录 [Apache License 2.0](../LICENSE)。
