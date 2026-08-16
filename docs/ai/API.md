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

## M1 自治任务 API（已实现，默认关闭）

自治接口只对管理员开放，并且还受 `OGS_AI_AUTONOMY_ENABLED` 二次门控。标准发布
Compose 栈不启动自治 Worker 或专用 Redis 8；未同时满足 feature flag、checkpoint
和 Worker 就绪条件时，Run 不能启动。聊天只拥有创建草稿引用卡的能力，不能启动、
审批或取消 Run。

### 就绪状态与生命周期

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/ai/autonomy/status` | admin/user | 返回 `enabled`、专用 Redis 配置、checkpoint、Worker 和 `ready` 布尔值，以及固定 `reason` 码 |
| POST | `/ai/autonomous-runs` | admin | 校验目标资产、系统用户、模式和预算，创建 `draft` |
| GET | `/ai/autonomous-runs` | admin | 当前管理员的 Run 列表 |
| GET | `/ai/autonomous-runs/{run_id}` | admin | 当前管理员的权威快照、步骤和 `allowed_operations` |
| POST | `/ai/autonomous-runs/{run_id}/start` | admin | 重新校验边界后将 `draft` 排入执行 |
| POST | `/ai/autonomous-runs/{run_id}/cancel` | admin | 请求取消；远端停止被确认前不会虚报 `cancelled` |
| POST | `/ai/autonomous-runs/{run_id}/steps` | admin | 提议服务端固定探针，不接受任意 Shell 作为探针参数 |
| POST | `/ai/autonomous-runs/{run_id}/steps/{step_id}/decision` | admin | 对服务端返回的待审批 Step 作 `approve` 或 `reject` 决策 |
| GET | `/ai/autonomous-runs/{run_id}/artifacts` | admin | 获取本 Run 的脱敏 Artifact 元数据 |
| GET | `/ai/autonomous-runs/{run_id}/artifacts/{artifact_id}` | admin | 读取单条未过期 Artifact 的解密正文 |
| GET | `/ai/autonomous-runs/{run_id}/evidence` | admin | 获取本 Run 的不可信 Evidence 引用 |
| GET | `/ai/autonomous-runs/{run_id}/stream` | admin | 按事件序号续传 SSE；支持 `after_seq` 或 `Last-Event-ID` |
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

Run 快照包含目标和凭据的服务端绑定、权限模式、预算、状态、三态结论、`revision`、
`graph_version`、最新事件序号、取消请求和时间戳；每个 Step 包含 `kind`、状态、顺序、
人类可读摘要、动作 digest 和受限备注。`GET /ai/autonomous-runs/{run_id}` 返回的
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
独立验证时，服务端会降级为 `inconclusive`，绝不自动重放写动作。

### M1 持久化数据结构

全新安装由 `backend/mysqldir/orange.sql` 一次创建；已有实例按
[统一升级流程](../operations/UPGRADE.md) 依次执行 rev53、rev54、rev55、rev56。表是
业务事实源，Redis 8 只保存 LangGraph checkpoint 和 Celery broker 数据。

| 表/字段 | 用途 | 关键约束 |
|---|---|---|
| `t_host.ai_environment` | 资产环境 | `production\|staging\|lab`，默认 `production`，仅管理员维护 |
| `t_ai_autonomous_run` | Run 权威快照 | 目标资产/系统用户、`mode`/`custom_profile_json`、状态/结论、预算、`revision`/事件游标、租约 fencing、心跳和 `graph_version`；活动状态按 `active_host_id` 唯一约束封住同资产并行 Run |
| `t_ai_autonomous_step` | 有序计划、动作和验证 | `(run_id, seq)` 唯一；保存不可变动作摘要/digest、审批状态和有限备注 |
| `t_ai_autonomous_event` | Run 内追加式事件 | `(run_id, sequence)` 唯一且单调；payload 不保存凭据 |
| `t_ai_autonomous_artifact` | 加密执行产物 | 输出清洗、脱敏、限长后以 Fernet 密文保存；正文单条读取并按 `expires_at` 过期 |
| `t_ai_autonomous_evidence` | 观察索引 | 只保存有界摘要和同 Run Artifact ID 列表；`trusted` 恒为 `0`，不是结论凭据 |

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
| `approval.required` | `action_id`, `expires_at` | 需要用户确认 |
| `diagnostic_started` | `event_seq`, `run_id`, `profile_id` | 只读诊断开始 |
| `diagnostic_progress` | `event_seq`, `run_id`, `asset` | 逐资产探针进度 |
| `diagnostic_evidence` | `event_seq`, `run_id`, `evidence_id` | 新证据已保存 |
| `diagnostic_completed` | `event_seq`, `run_id`, `report` | 完成或部分完成 |
| `diagnostic_failed` | `event_seq`, `run_id`, `message` | 失败或取消 |
| `run.completed` | `waiting_for_approval` | 本轮正常结束 |
| `run.failed` | `message` | 本轮失败 |

`tool.started` 和对应的 `tool.completed` 使用相同 `id`。客户端应更新同一条时间线
记录，而不是追加两条卡片。`assistant.delta` 是展示增量；刷新后的权威内容来自
会话详情和诊断 Run 快照。诊断事件带递增 `event_seq`，Run 的
`latest_event_seq` 可用于客户端判断快照新旧；当前没有公开事件重放接口。

同一会话一次只允许一个运行锁；单轮最多执行有限个工具步骤，同一轮最多创建一个
待审批批量动作。

## 动作审批

| 方法 | 路径 | 响应 |
|---|---|---|
| POST | `/ai/actions/{id}/approve` | SSE |
| POST | `/ai/actions/{id}/cancel` | JSON |

审批 SSE：

| 事件 | 关键字段 | 含义 |
|---|---|---|
| `action.progress` | `action_id`, `alias`, `status`, `output`, `error` | 单资产进度 |
| `action.completed` | `summary`, `outcome`, `results`, `status` | 最终聚合结果 |
| `run.completed` | `action_id` | 审批流完成 |
| `run.failed` | `action_id`, `message` | 校验或执行失败 |

`action.completed.results` 只返回资产别名，不返回主机 ID/IP。单项输出限制为 8,192
字符，错误限制为 2,048 字符；截断项带 `truncated: true`。

审批不是对模型建议的盲信。服务端会原子认领动作并重新验证所有权、有效期、
`result_set_id`、资产权限、系统用户权限、目标数量和危险命令规则。

## 保留和并发

- AI 会话和结果集默认 TTL：7 天。
- 每用户最多会话：20。
- 待审批动作默认 TTL：10 分钟。
- 会话展示事件最多保留最近 200 条。
- 诊断原始证据默认写入 7 天到期时间，过期后证据接口不再返回；结构化报告、
  Run 快照和事件默认在 90 天后级联删除。管理员可通过环境变量调整两类保留期。
- 会话删除时如果仍有待审批动作会返回冲突。

## OpenAPI

OrangeServer 提供 `/openapi.json`、`/openapi.yaml` 和 `/apidocs`。AI 路由因需要
REST 动词、URL 参数和 POST SSE 而单独注册，当前生成文档可能未完整覆盖所有 AI
事件字段。集成前应同时核对本文、当前版本的 `app/api/ai_api.py` 和运行中响应；
发现不一致请提交文档修复。
