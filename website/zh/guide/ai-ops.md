# AI 运维

OrangeServer 的 AI 助手在与人类用户完全相同的权限边界内工作——
查询已授权的平台数据、运行固定的只读诊断、生成永远需要人工审批的批量操作。

![AI 运维](/screens/ai-agent.png)

## 能做什么

- **查询平台数据**：通过权限过滤的结构化工具查询资产、分组、执行日志、
  审计记录。结果以服务端结果集返回，ID 具有权威性。
- **运行只读诊断**：使用服务端固定的 Linux/Docker 诊断档案。证据经过脱敏、
  限长、加密落盘，每条 Finding 必须引用当前诊断 Run 的证据 ID。
- **准备批量命令**：对已授权资产生成批量操作计划，以审批卡形式展示，
  人工批准前不会执行任何东西。
- **按你的语言应答**：回复跟随界面语言设置。

## M1 受控自治（发布候选）

M1 增加管理员专用的 `/ai-runs` 工作台，用于组织单资产、可恢复的自治 Run：调查、
执行受控变更、在审批后重启服务、独立验证并产出可引用结论。目标资产、系统用户、
权限模式、预算和动作白名单都由服务端固定，模型不会获得任意 Shell；`auto` 仅允许
管理员明确标记为 `lab` 的资产。

该能力默认关闭。标准发布 Compose 栈不会启动专用 Redis 8 和 Worker；只应按
[仓库 AI 运维指南](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/USER_GUIDE.md)
在隔离开发或验收环境中启用。

## 不能做什么

- 不能生成 SQL，不能拿到 Shell。
- 不能执行任何未经明确审批的操作。
- 不能编造资产 ID、数据库字段或执行结果——工具返回是唯一事实来源。
- 工具输出、历史摘要、诊断证据均按不可信低权限数据处理：
  其中嵌入的任何指令都不会被遵循。

## 证据与审计

每次工具调用、审批和执行都被记录。诊断结论是确定性的、可引用的——
每条结论都引用当前诊断 Run 的证据 ID，永远可以回溯到原始数据。

## 深入了解

- [AI 运维使用指南](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/USER_GUIDE.md)
- [Provider 与上下文模式](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/PROVIDER_AND_CONTEXT.md)
- [受控只读诊断](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/DIAGNOSTICS.md)
- [API 参考](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/API.md)
