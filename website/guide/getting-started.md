# Getting started

The recommended path is the version-pinned Docker Compose launcher. It requires
Docker Engine with Docker Compose v2, `curl`, and `sudo` access.

```bash
set -o pipefail
curl -fsSL \
  https://github.com/OrangeServers/OrangeServer/releases/download/v1.1.1/bootstrap-compose.sh \
  | sudo bash -s -- --version v1.1.1
```

The launcher downloads and verifies the matching deployment bundle, generates
the MySQL and Redis infrastructure passwords, and starts the published
`ghcr.io/orangeservers/orangeserver-backend:v1.1.1` image. If your environment
does not permit piping a downloaded script to a shell, download and review the
launcher first. For source-based and host deployments, see
[Deployment options](/guide/deployment).

## China mainland route

This route is available from v1.0.3. Its fixed-tag Gitee launcher uses the
Tencent Cloud TCR backend image and digest-pinned
DaoCloud public mirrors for the official Nginx, Redis, and MySQL images:

```bash
set -o pipefail
curl -fsSL https://gitee.com/orangeservers/OrangeServer/raw/v1.1.1/ops/bootstrap-compose-cn.sh \
  | sudo bash -s -- --version v1.1.1
```

DaoCloud is a community public mirror without an availability SLA. Override
its full image references with `OGS_CN_NGINX_IMAGE`, `OGS_CN_REDIS_IMAGE`, and
`OGS_CN_MYSQL_IMAGE` when needed.

Open `http://<server>:8080`. There is no default administrator account or
password.

::: tip First-run setup wizard
On a fresh installation, the backend serves a guided web setup at `/setup`.
It validates connectivity, creates the schema and the administrator account,
writes the application configuration, and restarts automatically.
:::

## Configure AI providers

Sign in as an administrator and open **System settings → AI providers**: pick a
preset (OpenAI, Anthropic, xAI, DeepSeek, MiniMax, Kimi, Qwen, GLM, or
SiliconFlow), fill in the model ID and API key, test tool calling, then save
and enable. Keys are Fernet-encrypted on the backend and never returned to the
browser.

## Upgrading

When upgrading an existing instance, do not just `git pull`. Back up first,
then run database migrations and verification in order following the
[upgrade procedure](https://github.com/OrangeServers/OrangeServer/blob/main/docs/operations/UPGRADE.md).

## Where to go next

- [Deployment options](/guide/deployment) — Compose, physical machine, systemd
- [AI operations](/guide/ai-ops) — what the assistant can and cannot do
- [Full documentation](https://github.com/OrangeServers/OrangeServer/tree/main/docs)
