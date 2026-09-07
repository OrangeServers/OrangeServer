# AI 运维

OrangeServer 的 AI 运维工作台把对话、监控调查、告警和受控自治 Run 放在同一个
权限边界内。聊天负责只读分析；远程写操作仍统一进入已有的审批式自治任务流程。

![AI 运维](/screens/ai-agent.png)
![窄屏 AI 运维工作台](/screens/ai-agent-narrow.png)

## 从工作台开始

主要入口为：

- `/ai-ops`：当前对话和最近任务轨道的统一工作台；
- `/ai-ops/tasks`：按需关注、执行中和已完成分组的任务；
- `/ai-ops/alerts`：由 Alertmanager 进入的自治 Run；
- `/ai-knowledge`：审核 Runbook 与已验证任务的知识检索。

空对话中的四张能力卡会把真实提示词填入输入框，但不会自动发送。用户可以补充
资产、服务或故障现象，再选择自治任务权限后发送。

## 监控分析

管理员在监控数据源设置中配置只读 Prometheus、Grafana、Loki 和 Zabbix，并确认资产
映射。Agent 会发现当前可用的指标、日志流、Panel 和监控项，再根据用户问题选择受限
查询；它可以比较时间窗口和多个来源，不是固定只查 CPU 或在线状态。

服务端控制目标地址、凭据、标签、查询形状、时间范围、样本数和响应大小。聊天中的
监控分析是只读的；需要修改、重启或持续跟进时，用户再明确创建自治 Run。

## 受控自治任务

管理员和普通用户都可以在资产与系统凭据组合已授权时管理自己拥有的单资产 Run。
Run 记录计划、审批、动作、证据、独立验证和最终结论。目标资产、系统用户、权限档案、
预算和动作白名单由服务端固定，模型不会获得任意 Shell。

四容器 bundled 安装使用一个统一 Redis 8：DB0 保存检查点/向量，DB1 作为 Celery Broker，
DB2 保存会话/缓存，并配套 prefork Worker。`ask`、`ai_review` 和 `custom` 仍受服务端
审批策略约束；`auto` 仅允许 `lab` 资产。

## 知识库

`/ai-knowledge` 是一级入口。普通用户可以搜索自己有权访问的全局和主机范围元数据与
引用；只有管理员可以新增或修改 Runbook、审核来源、配置 Embedding 和重建索引。
索引只接受审核 Markdown Runbook 和已验证的已解决 Run，不接受聊天、凭据、原始 SSH
输出或未脱敏日志。

## 不能做什么

- 不能生成 SQL，不能拿到不受限的 Shell。
- 聊天监控分析是只读的。Run 写操作遵守所选权限档案和服务端审批策略；`auto` 仅允许
  标记为 `lab` 的资产，且不能把 `deny` 提升为允许。
- 不能编造资产 ID、数据库字段或执行结果——工具返回是唯一事实来源。
- 工具输出、历史摘要、诊断证据均按不可信低权限数据处理：
  其中嵌入的任何指令都不会被遵循。

## 证据与审计

每次工具调用、监控观察、审批和执行都被记录。诊断 Finding 和监控观察均可引用；
自治 Run 结论引用当前调查与独立验证中的 Evidence。

## 深入了解

- [AI 运维使用指南](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/USER_GUIDE.md)
- [Provider 与上下文模式](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/PROVIDER_AND_CONTEXT.md)
- [受控只读诊断](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/DIAGNOSTICS.md)
- [API 参考](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/API.md)
