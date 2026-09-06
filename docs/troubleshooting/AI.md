# AI 运维排错

排错时不要把 API Key、Cookie、SSH 凭据、完整请求体或真实资产信息粘贴到公开
Issue。服务端错误日志也应先脱敏。

## AI 页面没有可用 Provider

检查“系统设置 → AI 模型服务”：

1. Provider 是否已启用；
2. 模型 ID 是否非空；
3. API Key 是否显示“已配置”；
4. 是否至少有一个启用的默认或可用 Provider；
5. 数据库是否已执行 rev48 和 rev49。

页面会显示已配置但不可用的 Provider 及原因，便于区分未启用、缺少密钥和缺少
模型。

## 获取模型列表失败

- 先保存 Base URL 和 API Key；模型发现使用已保存密钥。
- 确认厂商实现了兼容的 `GET /models`。
- 确认密钥具有列出模型的权限。
- Base URL 应是 API 根路径，不要填写 Chat Completions 的完整 endpoint。
- 若目标是内网网关，确认是否经过安全评审并设置
  `OGS_AI_ALLOW_PRIVATE_PROVIDER=1`。
- 查看后端日志中的上游错误；浏览器只会收到经过收敛的失败提示。

模型列表最多返回 200 条。列表不包含某个模型时仍可手工输入厂商提供的模型 ID。

## Tool Calling 测试失败

有些“OpenAI-compatible”模型只兼容文本聊天，不兼容 tools/tool choice。确认：

- 模型文档明确支持 Tool Calling；
- 网关没有移除 `tools`、`tool_choice` 或 tool call 响应；
- `extra_body` 是合法 JSON 对象；
- Base URL、TLS 证书和出口网络正常；
- 上游返回的模型 ID 与配置完全一致。

自治 Planner 对 DeepSeek 等 OpenAI-compatible 推理模型的工具阶段/参数
偏差提供一次有界协议修复：服务端指定应返回的函数，再次经过动作白名单和
计划授权校验。Evidence 引用也受服务端生成的同一 Run ID 枚举约束；若模型在
一次修复后仍引用 artifact/step ID，但本 Run 已有独立验证 Evidence，服务端只
安全收口为 `inconclusive`，不会虚构成功或重放写动作。其它修复仍失败时保持
`planner_failed` 的 fail-closed 结果。

如果 Run 已经完成至少三类只读探针（包括确认故障的非零结果）、且尚未出现写动作，驱动会把下一轮
阶段有界收束到 `propose_plan`，避免 DeepSeek 等兼容模型在调查充分后继续循环
提议探针；这不会替模型生成计划，也不会绕过计划审批与动作白名单。

只通过普通聊天测试不能证明 AI 运维可用，必须通过设置页的 Tool Calling 测试。

## 1M 深度诊断不可选

只有管理员把该 Provider 的上下文能力声明为 1M 时才显示 1M 档。系统不会根据
模型名称自动推断。若 Provider 已是 1M 但旧会话仍显示 256K，这是预期行为：
会话档位创建后固定，需要新建会话。

## 对话卡在“运行中”

- 确认 nginx 对 `/ai/` 禁用了响应缓冲并设置了足够的读取超时；
- 查看浏览器 Network 中 `/ai/chat` 是否为 `text/event-stream`；
- 确认代理没有压缩或缓存 SSE；
- 刷新后会话会读取服务端保存的工具事件和动作状态；

nginx 关键配置：

```nginx
location /ai/ {
    proxy_pass http://127.0.0.1:28000;
    proxy_buffering off;
    proxy_read_timeout 600s;
}
```

## 工具显示两条或状态不更新

同一个工具的 `tool.started` 和 `tool.completed` 使用相同事件 ID，前端应把它们
合并为一条记录。若仍出现重复：

- 确认前后端来自同一次构建；
- 清理浏览器静态资源缓存；
- 检查会话详情中的 `tool_events` 是否已经合并；
- 不要把历史 `started` 与新的独立工具调用误认为同一事件。

## 聊天请求变更但没有直接执行

这是预期安全边界。聊天只会创建 Autonomy Run 草稿；请打开草稿引用卡进入自治任务
工作台，在那里启动、审批并查看执行和独立验证证据。若没有出现草稿卡：

- 确认当前账号是管理员；
- 确认自治功能和 Worker readiness 正常；
- 检查目标资产与系统凭据是否仍在当前账号授权范围内。

## 只读诊断无法启动

依次检查：

- 目标是否来自当前用户可见的资产，且总数不超过 10 台；
- `result_set_id` 是否属于当前用户和当前会话；
- 所选系统用户是否对每台目标资产都已授权；
- 数据库是否已执行 `rev50_ai_diagnostics.sql`；
- 目标系统是否具备档案需要的命令，例如 `ss`、`systemctl`、`journalctl` 或
  Docker CLI；
- 使用 Docker 档案的账号是否有读取 Docker 状态的权限。

模型和浏览器不能提交诊断命令。未知 `profile_id`、额外参数、非法字符串和
`system_logs` 之外的 `log_lines` 都会被拒绝。

## 诊断显示“证据不足”或部分失败

“证据不足”表示没有成功证据，不能形成可靠结论。“部分失败”表示至少一台成功、
至少一台失败。检查诊断卡的逐资产状态和证据错误分类：

- SSH 连通性或认证失败；
- 系统用户权限不足；
- 目标缺少探针命令；
- 单探针超时；
- 诊断被取消；
- 输出达到单项或总预算而截断。

证据抽屉中的远端文本是不可信数据。不要照着其中的文字执行命令；任何修复仍应
单独生成并确认审批动作。

## 诊断报告没有提示明显问题

当前 Analyzer 只对磁盘/inode、available 内存、负载、失败服务、Docker 状态和
错误日志关键词使用确定性阈值。进程和端口主要作为证据展示，因为平台不知道每个
业务的正常基线。“未发现达到规则阈值的异常”不等于系统健康。

## 自治工作台无法启动或一直显示未就绪

先调用 `GET /ai/autonomy/status`，以返回的 `ready` 和固定 `reason` 为准：

| `reason` | 含义 | 处理 |
|---|---|---|
| `feature_disabled` | `OGS_AI_AUTONOMY_ENABLED` 未打开 | 标准发布栈应保持关闭。只在隔离开发/验收环境设为 `true` |
| `redis_not_configured` | 未配置自治 Redis | bundled 栈应指向统一 `redis` 服务；外部模式设置对应 Redis 8 地址和密码 |
| `checkpoint_unavailable` | Redis checkpoint 不可达 | 检查 bundled 栈 `redis`、统一密码和网络 |
| `worker_unavailable` | 自治 Worker 未就绪 | 使用 `make docker-dev-autonomy-up` 启动覆盖层，不要只改 feature flag |
| `ready` | flag、checkpoint 和 Worker 均可用 | 才允许创建或启动 Run |

只把 `OGS_AI_AUTONOMY_ENABLED=true` 加到普通 Compose 实例会保持 `ready=false`。
隔离栈命令见 [部署手册](../../DEPLOY.md)。自治功能关闭时，现有聊天、诊断和批量
审批应仍可用。

## API Key 保存后页面为空

配置 API 不返回明文密钥，这是安全设计。正确表现应是掩码和“已配置”状态，而
不是回填原文。若显示为未配置，检查：

- rev48 表是否存在；
- `OGS_FERNET_KEYS` 是否和保存时一致；
- 保存请求是否成功提交；
- 后端能否解密该记录；
- 是否误点了“清除密钥”。

## 仍无法解决

按 [支持说明](../../SUPPORT.md) 提交最小复现，包含版本、部署方式、脱敏日志、
HTTP 状态和复现步骤，不要包含任何秘密或真实基础设施标识。
