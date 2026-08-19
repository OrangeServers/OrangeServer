# OrangeServer 文档中心

本目录按读者要完成的任务组织。配置项的准确含义以代码和
[配置参考](../CONFIG.md) 为准；数据库升级步骤只在
[统一升级流程](operations/UPGRADE.md) 维护。

## 使用者

- [项目官网](https://orangeservers.github.io/OrangeServer/)：产品总览与快速入口。
- [官网部署指南](https://orangeservers.github.io/OrangeServer/zh/guide/deployment.html)：固定版本一键安装与部署方式。
- [批量命令与批量脚本](operations/BATCH_OPERATIONS.md)：配置检查、执行结果、失败重试和能力边界。
- [AI 运维使用指南](ai/USER_GUIDE.md)：查询、结果集、审批、执行结果和会话。
- [受控只读诊断](ai/DIAGNOSTICS.md)：固定探针、证据、规则报告和 Runbook。
- [AI Provider 与上下文](ai/PROVIDER_AND_CONTEXT.md)：模型服务、密钥、256K/1M。
- [AI 常见问题](troubleshooting/AI.md)：模型列表、Tool Calling、SSE 和上下文排错。
- [中文项目总览](../README.zh-CN.md)：功能、快速开始和能力边界。

## 管理员

- [官网部署指南](https://orangeservers.github.io/OrangeServer/zh/guide/deployment.html)：普通全新安装入口。
- [部署手册](../DEPLOY.md)：Docker、物理机和 Kubernetes 部署。
- [后端镜像与部署包发布](operations/BACKEND_IMAGE_RELEASE.md)：版本标签、镜像、部署包和从零验收命令。
- [批量操作安全与审计](operations/BATCH_OPERATIONS.md)：权限复核、脚本限制和兼容接口。
- [统一升级流程](operations/UPGRADE.md)：备份、迁移、验证和回滚。
- [配置参考](../CONFIG.md)：`OGS_*` 环境变量。
- [架构与信任边界](architecture/TRUST_BOUNDARIES.md)：数据流、权限和秘密管理。
- [AI 运维设计参考](architecture/AI_DESIGN_REFERENCES.md)：成熟项目的借鉴与取舍。
- [安全策略](../SECURITY.md)：报告漏洞与部署责任。

## 开发者

- [编码模型工作约定](../AGENTS.md)：开工检查、仓库血缘、安全边界、验证和交付格式。
- [文档编写规范](WRITING.md)：入口职责、唯一事实源、内容状态和隐私规则。
- [后端开发说明](../backend/README.md)
- [前端开发说明](../frontend/README.md)
- [AI REST/SSE 契约](ai/API.md)
- [AI 运维路线图](ai/ROADMAP.md)：当前边界、Linux 受控自治工作包和长期里程碑。
- [贡献指南](../CONTRIBUTING.md)
- [变更记录](../CHANGELOG.md)

## 文档边界

- 测试数量、分支名、测试机地址、部署机目录和临时镜像名不写入公开文档。
- 内部研究与开发记录不构成产品行为或兼容性承诺。
