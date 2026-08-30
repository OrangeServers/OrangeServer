# 快速开始

推荐使用固定版本的 Docker Compose 引导器。要求：Docker Engine、Docker Compose v2、
`curl` 与可使用 `sudo` 的权限。

```bash
set -o pipefail
curl -fsSL \
  https://github.com/OrangeServers/OrangeServer/releases/download/v1.1.1/bootstrap-compose.sh \
  | sudo bash -s -- --version v1.1.1
```

引导器会下载并校验同版本部署包，生成 MySQL 与 Redis 基础设施密码，并启动已发布的
`ghcr.io/orangeservers/orangeserver-backend:v1.1.1` 镜像。若环境不允许将下载内容
直接交给 shell，请先下载并审阅引导器。源码部署和复用宿主机服务的部署方式见
[部署方式](/zh/guide/deployment)。

## 中国大陆线路

中国大陆入口从 v1.0.3 起提供。固定 tag 的 Gitee 引导器会使用腾讯云 TCR 后端镜像，
以及 DaoCloud 匿名公共 Redis、MySQL 镜像：

```bash
set -o pipefail
curl -fsSL https://gitee.com/orangeservers/OrangeServer/raw/v1.1.1/ops/bootstrap-compose-cn.sh \
  | sudo bash -s -- --version v1.1.1
```

DaoCloud 是不承诺可用性 SLA 的社区公共服务；需要时可通过
`OGS_CN_REDIS_IMAGE`、`OGS_CN_MYSQL_IMAGE` 覆盖完整镜像引用。覆盖示例见
[部署方式](/zh/guide/deployment)。

浏览器打开 `http://<服务器地址>:8080`。系统没有默认管理员账号或密码。

::: tip 首次部署向导
全新安装时，后端会在 `/setup` 提供网页引导：校验连通性、建库建表、创建管理员账号、
写入应用配置并自动重启。
:::

## 配置 AI 模型服务

以管理员登录，进入 **系统设置 → AI 模型服务**：选择厂商模板（OpenAI、
Anthropic、xAI、DeepSeek、MiniMax、Kimi、Qwen、GLM、SiliconFlow），填写模型 ID
与 API Key，完成 Tool Calling 测试后保存启用。密钥在后端 Fernet 加密，
不回传浏览器。

## 升级

升级已有实例时，不要只执行 `git pull`。先备份，再按
[统一升级流程](https://github.com/OrangeServers/OrangeServer/blob/main/docs/operations/UPGRADE.md)
顺序执行数据库迁移和验证。

## 下一步

- [部署方式](/zh/guide/deployment) — Compose、物理机、systemd
- [AI 运维](/zh/guide/ai-ops) — AI 助手能做什么、不能做什么
- [完整文档](https://github.com/OrangeServers/OrangeServer/tree/main/docs)
