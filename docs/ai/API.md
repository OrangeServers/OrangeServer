# AI REST 与 SSE 契约

本文描述当前已经实现的 Provider、会话、只读诊断、自治任务、聊天和动作接口。

## 通用约定

- 所有接口都要求有效 OrangeServer 会话。
- 普通用户和管理员接口均经过 CSRF 校验；管理员配置接口额外要求 `admin` 角色。
- JSON 请求使用 `Content-Type: application/json`。
- JSON 成功响应沿用平台统一信封，业务数据同时可能出现在具名字段和 `data` 中。
- 错误使用统一 `code`、`msg` 和相应 HTTP 状态。
- 聊天 SSE 使用 POST，因此浏览器通过 `fetch` 读取流；自治 Run 事件流使用 GET，
  通过 `Last-Event-ID` 或 `after_seq` 续传。

示例成功信封：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {}
}
```

## Provider

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/ai/providers` | user/admin | 可见 Provider、可用状态和默认项 |
| GET | `/ai/stats` | user/admin/audit | 仪表盘统计：近 N 天（`?days=`，默认 7，上限 30）AI 发起的批量执行按天台次（成功/失败），数据源为 `t_command_log` 中 `log_type='AI 批量命令'` 的逐台审计行，无新增表结构 |
| GET | `/ai/admin/providers` | admin | Provider 配置列表，不含明文密钥 |
| PUT | `/ai/admin/providers/{code}` | admin | 保存 Provider |
| POST | `/ai/admin/providers/{code}/test` | admin | 验证 Tool Calling |
| POST | `/ai/admin/providers/{code}/models` | admin | 用已保存密钥发现模型 |
| POST | `/ai/admin/providers/{code}/clear-key` | admin | 清除密钥并禁用 Provider |

保存 Provider 的主要字段：

```json
{
  "base_url": "https://provider.example/v1",
  "model": "model-id",
  "api_key": "only-sent-when-changing",
  "context_window_tokens": 262144,
  "extra_body": {},
  "enabled": true,
  "is_default": true
}
```

`context_window_tokens` 只接受 `262144` 或 `1048576`。响应使用
`api_key_configured` 表示密钥状态，永远不回传 `api_key`。

## 会话和结果集

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/ai/conversations` | 当前用户最近会话 |
| POST | `/ai/conversations` | 创建会话 |
| GET | `/ai/conversations/{id}` | 会话、消息、工具事件和动作状态 |
| DELETE | `/ai/conversations/{id}` | 删除当前用户会话 |
| GET | `/ai/results/{id}` | 当前用户的权威结果集分页 |

创建会话：

```json
{
  "provider_code": "siliconflow",
  "context_mode": "standard_256k"
}
```

`provider_code` 可省略并使用默认可用 Provider。`context_mode` 可用值：

- `standard_256k`
- `deep_diagnostic_1m`

会话详情支持 `?action_summary=1`，只返回最近动作摘要，供运行中轮询使用。结果集
支持 `page` 和 `page_size`，其中 `page_size` 范围为 1–100。

资源 ID、会话和动作都按当前用户隔离。不能把其他用户或其他会话的 ID 作为能力
凭证。

## 受控诊断 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/ai/diagnostic-profiles` | 服务端固定只读档案及参数 schema |
| POST | `/ai/diagnostics` | 启动诊断并返回 Run |
| GET | `/ai/diagnostics/{run_id}` | 当前用户的权威 Run 快照 |
| POST | `/ai/diagnostics/{run_id}/cancel` | 请求取消未结束 Run |
| GET | `/ai/diagnostics/{run_id}/evidence` | 当前用户的解密、脱敏证据 |
| GET | `/ai/diagnostics/{run_id}/report` | 确定性规则报告 |

直接启动示例：

```json
{
  "profile_id": "disk_usage",
  "result_set_id": "server-generated-result-set",
  "conversation_id": "optional-conversation-id",
  "system_user_id": 12,
  "parameters": {}
}
```

也可以传 `target_ids`，但服务端仍会重新验证全部资产和系统用户权限。单次最多
10 台；`target_ids` 和 `result_set_id` 至少提供一个。使用结果集并同时提供
`conversation_id` 时，两者必须属于同一会话。

`POST /ai/diagnostics` 是普通 JSON 请求，当前调用会等待采集和规则分析完成后返回
Run。通过聊天 `run_diagnostic` 工具启动时，进度会作为聊天 SSE 事件实时发送。

Run 的权威状态为 `queued`、`running`、`completed`、`partial`、`failed` 或
`cancelled`；类型中还为中断和过期恢复保留 `interrupted`、`expired`。证据接口
只允许 Run 所有者访问，内容带 `untrusted: true`。报告 Finding 的
`evidence_ids` 只能引用同一 Run 的证据。

详情见 [受控只读诊断](DIAGNOSTICS.md)。

## M2 告警与运维态势 API

| 方法 | 路径 | 调用方 | 说明 |
|---|---|---|---|
| GET | `/ai/ops/status` | admin/user | 返回当前用户可见的待处理告警、活动/排队 Run、最近结论、Worker 容量和知识索引状态 |
| POST | `/ai/ops/alertmanager/webhook` | Alertmanager | 使用独立 Bearer Token；单条 `firing` 幂等创建并启动 `ask` Run，`resolved` 只追加 Evidence |

Webhook 最大 256 KiB，首版每次只接受一条 Alertmanager alert。标签必须包含
`ogs_host_id`、`ogs_system_user_id` 和 `service`（或 `alertname`）；服务端会重新验证
固定管理员、资产、系统凭据和授权关系。幂等键由 `groupKey + startsAt` 计算，调用方
不能指定 Run owner、执行模式或权限。配置 Prometheus 时还必须提供 `instance` 和
`job`，缺失时整条告警会被拒绝，不会静默跳过指标证据。

配置 Prometheus 后，服务端只执行固定的服务可用性模板：最近 15 分钟、30 秒步长、
5 秒超时且最多保留 1000 个样本。Webhook 和模型都不能提供 URL、Header 或任意
PromQL。返回的 Evidence/Artifact 不包含 Prometheus 地址、认证信息或原始指标标签。

## M2 运维知识 API

MySQL 保存文档正文和版本，Redis DB0 中的 chunk/vector 是可删除并重建的派生索引；
配置接口永不返回 API Key 明文。普通用户只能读取获授权范围的来源元数据、索引健康
和检索结果；配置、正文、增删改、重建和 Run 知识沉淀仍仅管理员可用。

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET/PATCH | `/ai/knowledge/config` | admin | 读取或更新本地/远程 embedding 配置；模型或维度变化将索引标记为 `stale` |
| GET | `/ai/knowledge/documents` | admin/user | 列出获授权范围的来源元数据，不返回正文 |
| POST | `/ai/knowledge/documents` | admin | 新增管理员审核的 Markdown Runbook |
| GET/PATCH/DELETE | `/ai/knowledge/documents/{id}` | admin | 读取正文、更新版本或删除文档 |
| POST | `/ai/knowledge/search` | admin/user | 在服务端验证的 `global` 与获授权 `host:*` 范围检索；查询最多 512 字符，最多返回 8 条 |
| POST | `/ai/knowledge/reindex` | admin | 向现有 Celery Worker 提交重建任务，返回 `202`；最多生成 20,000 个 Redis 向量分片 |
| POST | `/ai/autonomous-runs/{run_id}/knowledge` | admin | 将当前管理员拥有、已解决且独立验证通过的 Run 沉淀为审核知识 |

固定边界为：单文档 1 MiB、分片 400 字符并重叠 60、检索最多 8 条、注入模型上下文
最多 16 KiB。只允许 `runbook` 和 `verified_run` 来源。列表接口不返回正文；读取或编辑
时使用单文档接口。索引状态为 `empty | ready | stale | rebuilding | error`，只有
`ready` 且文档版本、模型及索引布局 fingerprint 一致时才返回引用；布局变化会
自动显示为 `stale`，重建完成前不会查询旧布局。

文档范围只接受 `global` 或 `host:<资产 ID>`。聊天检索默认使用 `global`；工具参数提供
当前用户有权访问的 `host_id` 或精确 `host_alias` 时，也使用对应资产范围。Autonomy
Planner 仅使用 `global` 和当前 Run 的目标资产范围。

聊天提供 `search_knowledge` 工具。Autonomy Planner 会用 Run 目标检索最多 4 条
引用；系统提示固定说明这些引用只供假设，不能授权远程动作，也不能替代本次 Run 的
实时 Evidence 和独立验证。

## M1 自治任务 API

管理员和普通用户都可以在各自已授权的资产与系统凭据范围内管理自己创建的 Run；
跨 owner 访问保持拒绝。标准 bundled 栈启动统一 Redis 8 与 Worker，默认可用。
未同时满足进程启用、checkpoint 和 Worker 就绪条件时，Run 不能启动。聊天只拥有
创建草稿引用卡的能力，启动、审批和取消仍通过工作台调用既有 Run 接口。

### 就绪状态与生命周期

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/ai/autonomy/status` | admin/user | 返回 `enabled`、Redis checkpoint、Worker pool/并发和 `ready` 布尔值，以及固定 `reason` 码 |
| POST | `/ai/autonomous-runs` | admin/user | 校验目标资产、系统用户组合授权、模式和预算，创建 `draft` |
| GET | `/ai/autonomous-runs` | admin/user | 当前用户拥有的 Run 列表 |
| GET | `/ai/autonomous-runs/{run_id}` | admin/user | 当前用户拥有 Run 的权威快照、步骤和 `allowed_operations` |
| POST | `/ai/autonomous-runs/{run_id}/start` | admin/user | 重新校验资产与系统凭据组合授权后将 `draft` 排入执行 |
| POST | `/ai/autonomous-runs/{run_id}/cancel` | admin/user | 请求取消；跨 owner 拒绝，远端停止被确认前不会虚报 `cancelled` |
| POST | `/ai/autonomous-runs/{run_id}/steps` | admin | 提议服务端固定探针，不接受任意 Shell 作为探针参数 |
| POST | `/ai/autonomous-runs/{run_id}/steps/{step_id}/decision` | admin/user | 对自己 Run 的待审批 Step 作 `approve` 或 `reject` 决策，副作用前再次验证授权 |
| GET | `/ai/autonomous-runs/{run_id}/artifacts` | admin/user | 获取自己 Run 的脱敏 Artifact 元数据 |
| GET | `/ai/autonomous-runs/{run_id}/artifacts/{artifact_id}` | admin/user | 读取自己 Run 的单条未过期 Artifact 解密正文 |
| GET | `/ai/autonomous-runs/{run_id}/evidence` | admin/user | 获取自己 Run 的不可信 Evidence 引用 |
| GET | `/ai/autonomous-runs/{run_id}/stream` | admin/user | 按事件序号续传 SSE；支持 `after_seq` 或 `Last-Event-ID` |
| POST | `/ai/autonomy/hosts/{host_id}/environment` | admin | 设置资产的 `production`、`staging` 或 `lab` 环境 |

创建草稿：

```json
{
  "goal": "检查示例资产上的磁盘使用率并在确认后修复服务配置",
  "host_id": 12,
  "system_user_id": 7,
  "mode": "ask",
  "budget": {
    "duration_seconds": 3600,
    "max_loops": 20,
    "max_actions": 30,
    "command_timeout_seconds": 60,
    "step_output_bytes": 65536,
    "run_artifact_bytes": 2097152
  }
}
```

`mode` 可用 `ask`、`ai_review`、`auto`、`custom`。`custom` 还必须提交服务端白名单
动作类别组成的 `profile`；`auto` 只有资产环境为 `lab` 时允许。服务端会重新解析
和限制预算，调用方不能用请求体抬高硬上限。

步骤决策请求严格只有两个业务字段，`operation` 必须来自当前快照的
`allowed_operations`，不能使用旧快照或客户端自定义动作：

```json
{
  "operation": "approve",
  "expected_revision": 4
}
```

### 快照、事件和结论

Run 快照包含目标和凭据的服务端绑定、权限模式、预算、状态、三态结论、`trigger_type`、
脱敏的 `trigger_summary`、`revision`、
`graph_version`、最新事件序号、取消请求和时间戳；每个 Step 包含 `kind`、状态、顺序、
人类可读摘要、动作 digest 和受限备注；待审批计划还返回从签名快照生成的
`plan_actions` 完整动作参数，操作者不需要从被截断的模型摘要猜测实际动作。参数不含
凭据引用；文件补丁内容命中常见密码、Token、Authorization 或私钥模式时服务端直接拒绝。
`GET /ai/autonomous-runs/{run_id}` 返回的
`steps` 按 `seq` 排序，`allowed_operations` 为空表示当前没有待决策操作。

状态集合：

- Run：`draft | queued | running | waiting_approval | recovering | needs_attention | completed | failed | cancelled | expired`
- Outcome：`resolved | not_resolved | inconclusive`
- Step：`proposed | waiting_approval | approved | running | succeeded | failed | skipped | outcome_unknown | cancelled`
- Step kind：`plan | action | verification`

SSE 事件使用 MySQL 内的 Run 级单调 `sequence`。客户端断线后应先重新获取权威快照，
再从最后一个已处理序号续传；收到 `terminal` 事件后仍应重新获取最终快照。快照比
聊天文本、SSE 增量和 Redis checkpoint 更权威。

结论由服务端 Planner/Worker 写入，不提供客户端直接改写结论的接口。`resolved` 必须
至少引用同一 Run 的 `verification_observation`；存在 `outcome_unknown` 写动作或缺少
独立验证时，服务端会降级为 `inconclusive`，绝不自动重放写动作。`conclusion`
对新结论固定包含已确认事实、影响范围、根因假设、置信度、未知项、推荐动作、最终
状态和同 Run Evidence ID 引用；所有文本和列表均有服务端上限并经过凭据脱敏。rev59
以前已结束的历史 Run 没有这些字段时，`conclusion` 为 `null`。

### M1 持久化数据结构

全新安装由 `backend/mysqldir/orange.sql` 一次创建；已有实例按
[统一升级流程](../operations/UPGRADE.md) 依次执行 rev53、rev54、rev55、rev56、rev57、
rev58、rev59、rev60。表是业务事实源，Redis 8 保存 LangGraph checkpoint、Celery broker 和可重建
知识向量。

| 表/字段 | 用途 | 关键约束 |
|---|---|---|
| `t_host.ai_environment` | 资产环境 | `production\|staging\|lab`，默认 `production`，仅管理员维护 |
| `t_ai_autonomous_run` | Run 权威快照 | 目标资产/系统用户、`mode`/`custom_profile_json`、触发类型/幂等引用/脱敏摘要、状态/`conclusion_json`、预算、`revision`/事件游标、租约 fencing、心跳和 `graph_version`；活动状态按 `active_host_id` 唯一约束封住同资产并行 Run |
| `t_ai_autonomous_step` | 有序计划、动作和验证 | `(run_id, seq)` 唯一；保存不可变动作摘要/digest、审批状态和有限备注 |
| `t_ai_autonomous_event` | Run 内追加式事件 | `(run_id, sequence)` 唯一且单调；payload 不保存凭据 |
| `t_ai_autonomous_artifact` | 加密执行产物 | 输出清洗、脱敏、限长后以 Fernet 密文保存；正文单条读取并按 `expires_at` 过期 |
| `t_ai_autonomous_evidence` | 观察索引 | 只保存有界摘要和同 Run Artifact ID 列表；`trusted` 恒为 `0`，不是结论凭据 |
| `t_ai_embedding_config` | embedding 与索引状态 | 单例配置；远程 API Key 为 Fernet 密文，模型 fingerprint 绑定索引版本 |
| `t_ai_knowledge_document` | 已审核知识正文 | MySQL 保存正文、来源、版本、范围和 hash；Redis 不作为文档事实源 |

凭据、完整 Prompt、完整远端输出和可复用的授权不会写入 Graph State、Event payload
或 Evidence 摘要。所有读取接口都会重新检查当前管理员和 Run 所有权；跨 Run 的
Artifact/Evidence ID 不能成为访问凭证。

## 聊天 SSE

```http
POST /ai/chat
Content-Type: application/json
Accept: text/event-stream
```

```json
{
  "conversation_id": "server-generated-id",
  "message": "查询我能访问的在线资产"
}
```

每个 SSE 帧同时提供标准 `event` 字段和 JSON `data.type`：

```text
event: assistant.delta
data: {"type":"assistant.delta","run_id":"...","content":"..."}

```

| 事件 | 关键字段 | 含义 |
|---|---|---|
| `run.started` | `run_id`, `conversation_id` | 一次 Agent 运行开始 |
| `assistant.delta` | `run_id`, `content` | 模型文本增量 |
| `tool.started` | `id`, `tool`, `arguments` | 工具开始 |
| `tool.completed` | `id`, `tool`, `result`/`error` | 同一工具完成 |
| `diagnostic_started` | `event_seq`, `run_id`, `profile_id` | 只读诊断开始 |
| `diagnostic_progress` | `event_seq`, `run_id`, `asset` | 逐资产探针进度 |
| `diagnostic_evidence` | `event_seq`, `run_id`, `evidence_id` | 新证据已保存 |
| `diagnostic_completed` | `event_seq`, `run_id`, `report` | 完成或部分完成 |
| `diagnostic_failed` | `event_seq`, `run_id`, `message` | 失败或取消 |
| `autonomy.draft_created` | `run_id`, `goal`, `status` | 已创建自治任务草稿 |
| `run.completed` | `conversation_id` | 本轮正常结束 |
| `run.failed` | `message` | 本轮失败 |

`tool.started` 和对应的 `tool.completed` 使用相同 `id`。客户端应更新同一条时间线
记录，而不是追加两条卡片。`assistant.delta` 是展示增量；刷新后的权威内容来自
会话详情和诊断 Run 快照。诊断事件带递增 `event_seq`，Run 的
`latest_event_seq` 可用于客户端判断快照新旧；当前没有公开事件重放接口。

同一会话一次只允许一个运行锁，单轮最多执行有限个工具步骤。聊天工具只提供查询、
固定只读诊断和 `create_autonomy_draft`；所有远程写操作必须进入 Autonomy Run，
聊天侧没有启动、审批、取消或执行接口。

## 保留和并发

- AI 会话和结果集默认 TTL：7 天。
- 每用户最多会话：20。
- 会话展示事件最多保留最近 200 条。
- 诊断原始证据默认写入 7 天到期时间，过期后证据接口不再返回；结构化报告、
  Run 快照和事件默认在 90 天后级联删除。管理员可通过环境变量调整两类保留期。
- 删除会话会同时删除其临时结果集；已经创建的 Autonomy Run 不属于聊天临时状态。

## OpenAPI

OrangeServer 提供 `/openapi.json`、`/openapi.yaml` 和 `/apidocs`。AI 路由因需要
REST 动词、URL 参数和 POST SSE 而单独注册，当前生成文档可能未完整覆盖所有 AI
事件字段。集成前应同时核对本文、当前版本的 `app/api/ai_api.py` 和运行中响应；
发现不一致请提交文档修复。
