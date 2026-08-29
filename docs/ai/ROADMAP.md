# AI 运维路线图

> 本文同时记录已实现的 M1 受控自治和正在推进的 M2。标准 bundled 栈包含统一
> Redis 与 Worker，默认可用。当前可用行为以
> [AI 运维使用指南](USER_GUIDE.md)、[受控只读诊断](DIAGNOSTICS.md) 和
> [AI REST/SSE 契约](API.md) 为准。

## 目标与边界

OrangeServer 的目标不是用 AI 包装少量 Linux 基础命令，而是复用现有资产、凭据、
权限、SSH、审批和审计能力，把调查、证据、修复和验证组织成可恢复的运维工作流。

路线图遵守以下原则：

- 聊天只负责查询、解释、诊断和创建自治草稿；远程写操作统一进入 Autonomy Run。
- LangGraph 继续负责长任务自治工作流，不重写现有 Provider 和 Tool Calling 循环。
- 文本分片只引入独立的 `langchain-text-splitters`，不引入第二套 Agent 执行循环。
- MySQL 始终是业务事实源，模型输出、SSE 和 checkpoint 都不能覆盖权威状态。
- 模型提出动作，服务端决定自动执行、等待审批或拒绝；模型不能自行提高权限。
- 先做深一个 Linux 单机闭环，再扩展监控、Docker 和 Kubernetes。
- 不提前建设公开插件系统、通用 Runbook DSL、多 Agent 或万能回滚引擎。

## 当前基线

M0 是已经发布的能力：

- AI 对话、资产和账号查询、权限过滤的结果集；
- 服务端固定档案的 Linux/Docker 只读诊断；
- 确定性 Analyzer、Evidence 引用、诊断报告和 Runbook 建议；
- 独立批量运维页面的命令预览、人工审批、执行结果和审计；
- 256K 标准上下文与 Provider 声明支持时可选的 1M 深度诊断档。

当前诊断不能提交自由 Shell，修复也不会由诊断流程直接执行。M1/S3 提供独立自治
任务工作台；标准发布栈默认启动统一 Redis 8 与 Worker，管理员仍可关闭自治入口。

## 长期里程碑

| 阶段 | 核心交付 | 进入下一阶段的门槛 |
|---|---|---|
| M0 当前基线 | AI 对话、固定只读诊断、批量命令审批、审计和上下文档位 | 已发布；后续不得破坏兼容性 |
| M1 Linux 自治 | 单机调查、安装、配置修改、服务操作、验证、审批暂停和重启恢复 | 强杀恢复、安全测试和完整纵向闭环通过 |
| M2 监控与事故工作台 | Prometheus、Loki、Alertmanager 受控查询；异常时间线、最近变更关联和告警触发调查 | 查询范围、租户、超时、脱敏和 Evidence 契约稳定 |
| M3 Docker | 容器、Compose、事件和日志诊断；受控重启、重建、旧 digest 回退及验证 | 不暴露宿主 Docker socket；结构化执行和回退证据稳定 |
| M4 Kubernetes | 先做只读 Analyzer，再做 restart、scale、rollback 等修复 | 最小 RBAC、Secret 排除、dry-run、diff、审批和复核通过 |
| M5 运维知识闭环 | 已验证处置转 Runbook、相似事故、健康节点对比、服务拓扑、影响范围和复盘草稿 | 积累足够经人工审核的真实成功案例 |
| M6 持续运维与评测 | 定时巡检、容量趋势、证书/EOL、场景回放、模型评测和 Agent 可观测性 | 用真实数据证明收益后再评估 MCP、插件或更重调度平台 |

长期里程碑只在本文维护。M2 总工作包由
[Issue #25](https://github.com/OrangeServers/OrangeServer/issues/25) 跟踪；后续阶段不提前
创建占位 Issue。

### M2：监控与事故工作台

M2 分三段交付：S0 先收敛执行链、Worker 并发和四容器部署；S1 接入第一条
Alertmanager → Prometheus/SSH → Autonomy Run 闭环；S2 增加只索引审核 Runbook 与
已验证 Run 的可重建向量知识库。M2 不迁移 ASGI/asyncio，不替换 Celery prefork，
也不新增默认监控或向量数据库容器。

当前 Unreleased 已完成 S0 实现，并实现 S1 的单条 Alertmanager 告警入口、固定
Prometheus 可用性观察、Run 触发契约和运维态势首页；真实告警纵向验收与 S2 仍由
Issue #25 跟踪，未完成前不视为 M2 发布门通过。

- Prometheus 和 Loki 请求必须经过服务端查询代理；模型不能提供任意 URL、Header、
  tenant 或无限制 PromQL/LogQL。
- 服务端限制时间范围、步长、返回量、并发、超时和可用标签，并在进入模型前聚合、
  脱敏和外置大结果。
- Alertmanager 首版只触发只读调查；创建 silence 或修改告警配置属于独立写动作。
- 事故工作台以症状、假设、Evidence、动作、验证和结论时间线为权威事实，聊天只是入口。
- 关联 OrangeServer 审计、定时任务、监控拐点以及后续接入的发布和容器事件。

### M3：Docker

- Docker 是自治执行器之后的独立 Adapter，不恢复通用容器 CRUD 页面。
- 不把宿主 `/var/run/docker.sock` 挂给 Web 或 Agent；优先使用 rootless Docker，
  否则通过 SSH 后的最小权限结构化 gateway。
- 先提供状态、health、OOM、重启次数、事件、Compose 拓扑和限长日志，再提供 restart、
  pull/recreate 及旧 digest 回退。
- 任意 `docker exec`、privileged、host mount、设备映射和 volume 删除保持强审批或拒绝。

### M4：Kubernetes

- 使用独立凭据 Adapter，不经通用 SSH Shell 复用 kubeconfig 或 ServiceAccount token。
- 先实现 namespace 范围的只读资源、Event、日志和指标 Analyzer，默认完全排除 Secret 内容。
- 写操作使用独立最小 RBAC；先 server-side dry-run 和 diff，再审批、执行和自动复核。
- 首批修复只包括 rollout restart、scale、rollback 和删除单个可重建 Pod；不开放通用
  `kubectl`、`exec` 或 `cluster-admin`。

### M5～M6：知识闭环和持续运维

- 只有经过人工审核且验证成功的 Run 才能转成版本化 Runbook。
- 历史相似事故只提供候选方案，不能自动继承旧事故的权限或安全结论。
- 健康实例对比、服务拓扑和影响分析必须基于可追溯数据，不由模型凭空补全。
- 复盘草稿区分事实、证据和模型推断，用户确认后才能归档。
- 持续巡检先由确定性规则发现证书到期、容量趋势、反复重启、备份失败和软件 EOL，
  再按需触发模型，避免周期性发送全部原始数据。
- 至少存在两个稳定外部 Adapter 后，才评估公开 MCP 或插件接口。

## M1：Linux 受控自治

### 产品形态

M1 增加独立的自治任务工作台，已实现路由为 `/ai-runs` 和
`/ai-runs/:runId`。现有 AI 对话只增加“创建自治任务草稿”和“打开任务”的引用卡；
模型不能从聊天直接启动任务。

v1 仅管理员可用，一次 Run 固定一个目标资产、一个系统用户和一个 Agent。Run 启动后，
模型不能改变目标、凭据、模式或服务端预算。同一资产最多存在一个活动自治 Run。

### 模块职责

| 模块 | 唯一职责 |
|---|---|
| MySQL | Run、Step、审批、Event、Artifact 引用和最终结果的业务事实 |
| LangGraph | `计划 → 策略 → 按需审批暂停 → 执行 → 观察 → 验证 → 决策` 的流程游标 |
| 统一 Redis 8 | DB0 checkpoint/可重建向量，DB1 Celery broker，DB2 会话/缓存；不保存业务最终结果 |
| Celery Worker | 按 `run_id` 推进有界步骤，不承担流程状态或结果存储 |
| 自治执行器 | 复用现有权限、凭据、SSH host-key 校验和审计，作为唯一远程副作用入口 |
| 独立工作台 | 展示权威快照、进度、审批、证据、恢复状态和验证结果 |

统一 Redis 8 使用 AOF 和沿用的 `autonomy-redis-data` 持久卷，限制 512MiB 并采用
`volatile-lru`；只有 DB2 中带 TTL 的缓存可被淘汰，checkpoint、向量和 broker 数据不
设置 TTL。不启用 Celery result backend，不引入 Flower，不自研 MySQL Checkpointer。

WP0 优先验证官方 shallow Redis saver 是否满足 interrupt、resume 和重启恢复；若不满足，
使用官方完整 Redis saver。两种方案都只保存紧凑 Graph State。

### 最小领域模型

新增五张表：

- `t_ai_autonomous_run`
- `t_ai_autonomous_step`
- `t_ai_autonomous_event`
- `t_ai_autonomous_artifact`
- `t_ai_autonomous_evidence`

资产增加服务端管理的 `ai_environment=production|staging|lab`，默认 `production`，
只有管理员可以修改。审批字段保存在对应 Step 中，不再拆分 Action、Approval 或
Verification 表。

状态契约：

- Run：`draft | queued | running | waiting_approval | recovering | needs_attention | completed | failed | cancelled | expired`
- Outcome：`resolved | not_resolved | inconclusive`
- Step：`proposed | waiting_approval | approved | running | succeeded | failed | skipped | outcome_unknown | cancelled`
- Step kind：`plan | action | verification`

实现必须满足：

- 活动 Run 使用数据库唯一约束和 `owner + 一次性 token + 到期时间` 租约 fencing
  防止并行执行及旧 Worker 回写；
- Run 持久化 `revision`、`graph_version`、预算、心跳、取消请求和最新 Event 序号；
- Event 在 Run 内单调递增，GET 快照始终比 SSE 增量权威；
- Artifact 清控制字符、脱敏、限长后使用现有 Fernet 体系加密；
- Artifact 默认保留 7 天，Run、Step 和 Event 默认保留 90 天；
- Graph State 只保存 ID、阶段、计数和短摘要，不放凭据、完整命令、原始日志或完整 Prompt。

### 权限档案和动作

M1 采用成熟 Agent Harness 常见的确定性 `allow | ask | deny` 决策，不让模型直接决定
自己是否有权执行：

- `allow`：动作已落在服务端固定档案内，沿 LangGraph 连续执行；
- `ask`：进入现有 interrupt 暂停，由所选权限档案路由给人或后续 Guardian；
- `deny`：服务端硬拒绝，人和 Guardian 都不能把它提升为允许。

用户选择的是权限档案，而不是为每条命令重新选择策略：

| 权限档案 | `ask` 的处理方式 |
|---|---|
| 每次询问（`ask`） | 交给人；服务端白名单内的只读探针仍连续执行 |
| AI 审查（`ai_review`） | S3 仅把可审查边界交给独立 Guardian；不可审查、高风险或 Guardian 不可用时转人工 |
| 自动（`auto`） | 仅 `lab` 资产可选；固定档案内的 `allow` 连续执行，剩余 `ask` 仍转人工 |
| 自定义（`custom`） | S3 由管理员组合固定动作类别和目标范围；不提供表达式或脚本策略语言 |

旧实验数据中的 `read_only | assisted | lab_autonomous` 只用于恢复已落库的 `v1` Run；
新 Run 使用上述四个值和条件路由的 `v2` 图。权限档案只改变 `ask` 的去向，不能扩大
当前用户、资产、凭据、环境、预算和服务端永久拒绝规则。

v1 动作保持克制：

- 观察、限长文件和日志读取；
- 已配置软件源中的包查询和安装；
- 带备份与 diff 的结构化文件补丁和恢复；
- systemd 状态、启动、停止和重启；
- 端口、HTTP、进程、日志和服务状态验证；
- 任意 Shell 可以提交，但始终等待绑定完整动作摘要的人工审批。

以下动作即使在“自动”档中也必须审批：新增软件源、下载并执行、账号或 SSH
配置、网络或防火墙、内核、Docker daemon、重启关机以及范围无法确定的修改。

以下动作在 v1 永久拒绝：磁盘分区或格式化、根目录或宽范围删除、主动读取密钥、
横向 SSH、绕过审计或绕过权限。危险命令黑名单只提供风险信号，不能证明安全。

审批 digest 必须绑定目标、凭据、工具、规范化参数、工作目录、超时、Step ID 和动作
版本；任一字段变化都会使审批失效。每个动作执行前重新检查当前用户、资产权限、
凭据授权、资产环境和 digest。

S2 只修正 `allow` 被驱动层错误改成逐条审批的问题，仍以精确 Step 人工审批处理
`ask`。S3 在 Planner 产出完整变更计划后增加一次计划授权：计划摘要绑定有序动作
digest、目标、凭据引用、策略版本、预算和有效期；计划内动作可连续执行，任一动作、
参数、顺序、目标、凭据或策略变化都使授权失效并重新进入 `ask`。Guardian 只审这种
稳定的计划边界，可复用当前配置的 Provider 但使用独立会话；普通调查不调用 Guardian，
模型调用失败、超时、格式错误或不确定时一律转人工。

### 恢复、取消和回退

Celery 任务只携带 `run_id`，按至少一次投递设计。Worker 通过数据库 revision 和带
一次性 token 的租约幂等认领 Run，并在启动时及运行期间周期扫描 queued、请求恢复和
租约过期的 Run：

- 只读动作可以自动重试；
- 已确认尚未执行的结构化动作可以继续；
- 写动作可能已经生效但结果未落库时，Step 进入 `outcome_unknown`，Run 进入
  `needs_attention`，绝不自动重放；
- Redis checkpoint 丢失时，只能从 MySQL 已确认的安全边界重建；
- 取消是请求，执行器确认停止前不能把 Run 标记为 `cancelled`；
- LangGraph 的 interrupt 节点不得包含副作用，因为恢复会从节点开头重新执行；
- 升级时按 Run 保存的 `graph_version` 选择兼容图，不能让暂停中的旧 Run 跳入新版节点。

M1 不承诺通用自动回滚。结构化文件补丁必须有备份并可恢复；包安装、任意 Shell 等
动作只提供独立验证和人工补偿方案。

### 上下文和执行预算

- 单 Run 默认最长 60 分钟、最多 20 次模型循环和 30 个动作；
- 单命令默认 60 秒、服务端硬上限 600 秒；
- 单 Step 最多保存 64 KiB 输出，单 Run Artifact 合计最多 2 MiB；
- draft 和审批默认 24 小时过期；
- 256K 为默认上下文，1M 仅在 Provider 明确支持时可选；
- 长日志始终外置为 Artifact，通过 Evidence 检索和分层摘要按需进入上下文；
- 记录模型 usage、finish reason、耗时和截断原因，但不保存完整敏感 Prompt。

### 已实现接口

以下接口已实现，但只有管理员且在 `OGS_AI_AUTONOMY_ENABLED` 显式打开时才允许创建、
启动或推进 Run。`GET /ai/autonomy/status` 始终可用于区分 feature flag、Redis
checkpoint 和 Worker 是否就绪；接口字段以 [AI REST/SSE 契约](API.md) 为准。

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/ai/autonomy/status` | feature、Worker 和 checkpoint 就绪状态 |
| POST | `/ai/autonomous-runs` | 创建并预检 draft |
| POST | `/ai/autonomous-runs/{run_id}/start` | 锁定范围并异步启动 |
| GET | `/ai/autonomous-runs` | 当前管理员的分页列表 |
| GET | `/ai/autonomous-runs/{run_id}` | 权威快照、Step、结果和 `allowed_operations` |
| GET | `/ai/autonomous-runs/{run_id}/stream?after_seq=` | 从指定 Event 序号续传 SSE |
| POST | `/ai/autonomous-runs/{run_id}/steps/{step_id}/decision` | 对当前允许操作作出决定 |
| POST | `/ai/autonomous-runs/{run_id}/cancel` | 请求取消 |
| GET | `/ai/autonomous-runs/{run_id}/artifacts/{artifact_id}` | 获取所有者隔离的脱敏 Artifact |

decision 请求只提交 `{operation, expected_revision}`，且 operation 必须来自服务端返回的
`allowed_operations`。SSE 支持 `Last-Event-ID`；断线恢复先获取权威快照，再从
`latest_event_seq` 续传。终态 Event 到达后，前端仍重新获取最终快照。

## M1 稳定化工作包

| WP | 内容 | 完成门 |
|---|---|---|
| S1 安全与审批 | 领域表、资产环境、结构化动作、服务端只读探针、权限复核、不可变动作快照和 revision/digest 审批 | 全新安装/升级 schema 一致；伪装写入、越权、篡改、旧 revision 和重复审批失败 |
| S2 执行与恢复 | Redis、Celery、LangGraph `allow/ask/deny` 路由、数据库租约、checkpoint fail-closed、可取消 SSH、写意图和未知结果 | 真实 MySQL/Redis/Worker 下通过重复投递、强杀、取消和 checkpoint 丢失测试 |
| S3 规划、证据与产品闭环 | Planner、一次计划授权、可选 Guardian、脱敏 Evidence、独立 Verification、三态 Outcome、REST/SSE、工作台和聊天引用 | 已通过完成门；标准发布栈默认启动依赖，入口仍可由管理员关闭 |

S1、S2、S3 均已完成实现与隔离验收。标准发布栈使用统一 Redis 8 和独立 Worker；
稳定 tag、GitHub Release、GHCR/TCR、Gitee 同名 tag、从零安装和升级/
回滚属于合入 `main` 之后的独立发布操作，不能由旧的 CI 结果、普通聊天 draft 或
“容器已启动”替代。

后续约束：

1. 正式发布只能来自公开 `main` 上的稳定 tag；非标准部署仍需显式启用 feature flag。
2. M2 以后不提前创建占位 Issue，也不建立永久 `develop` 分支。
3. 后续实现不得自行改变 MySQL/LangGraph/Redis/Celery 的职责、审批规则或恢复语义；
   需要改变时另开设计复核。

## M1 最终验收

发布前验收按两层记录：标准发布路径验证从零安装、schema、镜像和前端静态产物；M1
自治路径在独立的 Redis 8、Worker 和测试资产上验证执行、恢复、审批和结论。可复制的
命令入口见[后端镜像与部署包发布](../operations/BACKEND_IMAGE_RELEASE.md)和
[部署手册](../../DEPLOY.md)。

每个阶段运行相关后端或前端测试；S3 在本地运行完整后端测试、前端类型检查、生产
构建、新增的最小 Vitest 和 Compose 集成测试，不依赖远端 CI 额度。

最终必须覆盖：

- Web、Worker、Redis 分别重启后的自动恢复；
- Worker 在只读动作、写动作开始前、写动作执行中和 checkpoint 写入前后被强杀；
- 重复 Celery 投递、重复审批、过期租约和审批摘要篡改；
- 权限、凭据授权或 `lab` 环境在运行中撤销；
- 取消不虚报，未知写结果不自动重放；
- Prompt 注入、ANSI 控制字符、超长输出、敏感字段和横向访问；
- 自治功能关闭或基础设施不可用时，现有聊天、诊断和批量审批仍正常；
- 调查故障、安装或修改、重启、独立验证并输出可引用证据的完整单机闭环；
- 工作台桌面和窄屏视觉验收，测试机信息、凭据、截图和私有路径不进入 Git。

## 设计参考

这些项目和官方文档用于约束设计，不代表 OrangeServer 会直接引入完整产品：

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
  和 [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)：长任务 checkpoint、
  暂停和恢复语义；
- [LangGraph Redis saver](https://github.com/redis-developer/langgraph-redis)：Redis 8
  checkpoint 的官方实现与模块要求；
- [Celery](https://docs.celeryq.dev/en/stable/getting-started/introduction.html)：Worker、Broker
  和至少一次任务投递；
- [OpenHands](https://docs.openhands.dev/openhands/usage/architecture/runtime)：
  Action/Observation、隔离执行和风险确认思路；
- [HolmesGPT](https://github.com/HolmesGPT/holmesgpt)：只读/修复工具分层、Runbook 和多数据源调查；
- [K8sGPT](https://github.com/k8sgpt-ai/k8sgpt)：确定性 Analyzer 先发现、模型后解释；
- [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)
  和 [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/)：
  受控指标与日志查询；
- [Docker Engine security](https://docs.docker.com/engine/security/)
  和 [Kubernetes RBAC good practices](https://kubernetes.io/docs/concepts/security/rbac-good-practices/)：
  容器与集群最小权限边界。
