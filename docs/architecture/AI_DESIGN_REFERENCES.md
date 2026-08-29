# AI 运维设计参考

OrangeServer 的 AI 运维能力优先复用现有资产、凭据、SSH、审批和审计体系。
下列成熟项目用于校验设计方向，不是运行时依赖，也不会被直接嵌入服务。

> 本文同时讨论当前实现和未来方向。已发布能力包括固定只读诊断、人工审批动作，
> 以及 M1 受控自治；监控、Docker 和 Kubernetes Adapter 仍属于
> [AI 运维路线图](../ai/ROADMAP.md)中的规划能力。

## 参考项目

### HolmesGPT

[HolmesGPT](https://github.com/HolmesGPT/holmesgpt) 将诊断数据源组织为受控
toolset，并支持 Runbook 驱动的调查流程。OrangeServer 借鉴其“工具分层、按需取证、
先调查后处置”的思路，但首版只实现现有 SSH 通道上的内部 Adapter，不引入 sidecar
或公开插件系统。

### K8sGPT

[K8sGPT](https://github.com/k8sgpt-ai/k8sgpt) 通过 Analyzer 先生成确定性问题描述，
再把结构化结果交给模型解释。OrangeServer 同样把阈值判断、状态分类和证据引用放在
服务端 Analyzer 中，避免让模型直接从无限原始日志中猜测。

### OpenTelemetry

[OpenTelemetry Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/)
提供跨日志、事件、指标和资源的一致命名方式。当前诊断事件采用 OrangeServer 自身的
稳定契约；未来接入 Prometheus、Loki 或其他 Adapter 时，优先映射到兼容的资源与事件
字段，而不是为每个数据源发明新的语义。

### LangGraph 与 Celery

[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
和 [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) 提供 checkpoint、
暂停和恢复语义，用于已实现的长任务自治工作流。OrangeServer 不迁移现有聊天 Runner；
LangGraph 只负责流程游标，MySQL 继续保存权威业务状态。

[Celery](https://docs.celeryq.dev/en/stable/getting-started/introduction.html) 只负责把有界工作
交给独立 Worker。任务按至少一次投递设计，幂等和最终结果仍由 MySQL 领域状态保证，
不把 Celery result backend 当作事实源。

### OpenHands

[OpenHands Runtime](https://docs.openhands.dev/openhands/usage/architecture/runtime) 将 Action、
Observation 与隔离执行环境分开。OrangeServer 只借鉴动作提案、风险确认、执行预算和
可恢复验证，不引入其完整 Runtime，也不把通用 Shell 直接交给模型。

### OpenCode、Continue 与 mini-SWE-agent

[OpenCode permissions](https://opencode.ai/docs/permissions) 和
[Continue tool permissions](https://docs.continue.dev/cli/tool-permissions) 都先用确定性
规则把工具调用归为允许、询问或拒绝，再由用户选择是否扩大当前会话的授权；
[mini-SWE-agent](https://mini-swe-agent.com/latest/advanced/environments/) 则把确认模式与
本地、容器等执行环境分开。OrangeServer 借鉴的是这套最小分层：服务端固定
`allow | ask | deny`、用户选择权限档案、执行器保持独立安全边界。不会复制它们面向
本机代码工作区的路径规则，也不会引入通用策略语言。

## 当前边界与受控自治

当前只读诊断仍不接受 Shell。聊天里的修复动作仍需人工确认。M1 的“自动”权限档案
也不是绕过这条边界：资产环境、结构化动作、策略判断、预算、checkpoint 和独立验证
都由服务端固定，且仅 `lab` 资产可选 `auto`。白名单只读探针判为 `allow` 后可以连续
调查；`ask` 才进入 LangGraph interrupt，`deny` 永远不能由模型、Guardian 或人工提升。

任意 Shell 提案始终需要绑定目标、凭据、参数和步骤的精确审批。已实现的计划授权
允许对不可变计划作一次批准，计划内的调查、变更和验证连续执行；动作、参数、顺序、
目标、凭据或策略版本发生变化时授权立即失效。可选 Guardian 只替代 `ask` 的边界判断
且只能收紧权限，不参与每个白名单探针，也不成为授权源。

## 明确不照搬的部分

- 不提供绕过服务端策略和审批的任意 Shell、Docker exec 或集群管理入口。
- 不用危险命令黑名单替代参数白名单和服务端固定探针。
- 不因引入 AI 绕过 OrangeServer 现有的资产权限、凭据授权、审批和审计。
- 不把 1M 上下文当作日志仓库；长证据仍使用持久化、检索和分层摘要。
- 不因引入 LangGraph 就迁移当前聊天循环，也不整包引入 LangChain。
- 不在只有一个实现时预先建设公开 Adapter、MCP 或插件系统。

具体安全边界见[架构与信任边界](TRUST_BOUNDARIES.md)，诊断状态和证据契约见
[AI REST 与 SSE API](../ai/API.md)。
