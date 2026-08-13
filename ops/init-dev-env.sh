#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${ROOT}/.env.dev"

if [[ -f "${TARGET}" ]]; then
  echo "[OK] 已存在 ${TARGET}，保持不变"
  exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "[FAIL] 缺少 openssl，无法生成开发环境密钥" >&2
  exit 1
fi

umask 077
tmp="${TARGET}.tmp.$$"
trap 'rm -f -- "${tmp}"' EXIT

random_hex() {
  openssl rand -hex "$1"
}

cat >"${tmp}" <<EOF
# 本文件由 ops/init-dev-env.sh 生成，仅供本机全容器开发环境使用。
COMPOSE_PROJECT_NAME=orangeserver_dev
OGS_DEV_HTTP_PORT=8081
OGS_DEV_BACKEND_PORT=28001
OGS_DEV_BACKEND_IMAGE=ghcr.io/orangeservers/orangeserver-backend
OGS_DEV_BACKEND_TAG=v1.0.4

MYSQL_ROOT_PASSWORD=$(random_hex 24)
OGS_MYSQL_DBNAME=orange
OGS_MYSQL_USER=app_user
OGS_MYSQL_PASSWORD=$(random_hex 24)
OGS_REDIS_PASSWORD=$(random_hex 24)
# M1 自治功能默认关闭；密码仍预先随机生成，启用时不允许空密码回退。
# 仅用 docker-dev-autonomy-up 做隔离验收时启用。
OGS_AI_AUTONOMY_ENABLED=false
OGS_AI_AUTONOMY_REDIS_PASSWORD=$(random_hex 24)
# 留空以进入 /setup；向导会生成并写入 dev-backend-data/runtime.env。
OGS_FLASK_SECRET_KEY=
OGS_FERNET_KEYS=
OGS_SSH_HOST_KEY_POLICY=auto
OGS_HTTPS=false
OGS_PROXY_LAYERS=1
EOF

mv -- "${tmp}" "${TARGET}"
trap - EXIT
echo "[OK] 已生成权限受限的 ${TARGET}"
