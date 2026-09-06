#!/bin/bash
# =============================================================================
# OrangeServer docker compose 预检脚本 (REV49)
# =============================================================================
# 只读检查，不启动容器，不输出秘密值。
# 任一检查失败立即非零退出。
#
# 用法：
#   bash ops/preflight-compose.sh             # 默认 bundled 模式
#   bash ops/preflight-compose.sh bundled     # 同上
#   bash ops/preflight-compose.sh host        # host 模式 (app+worker)
# =============================================================================

set -Eeuo pipefail

# ---- 路径推断 (与 Makefile 一致) ----
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEPLOY="${ROOT}/deploy"
COMPOSE_FILE="${DEPLOY}/docker-compose.yml"

MODE="${1:-bundled}"

PASS=0
FAIL=0
WARN=0

ok()   { echo "  [OK]   $1"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $1" >&2; FAIL=$((FAIL+1)); }
warn() { echo "  [WARN] $1"; WARN=$((WARN+1)); }

# ---- 工具函数 ----
version_ge() {
    # 返回 0 若 $1 >= $2 (语义化版本比较)
    printf '%s\n%s' "$2" "$1" | sort -V -C 2>/dev/null
}

# =============================================================================
echo ">>> 1/8 Docker 版本检查"
# =============================================================================
if command -v docker >/dev/null 2>&1; then
    DOCKER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "0")
    if version_ge "${DOCKER_VER}" "20.10"; then
        ok "Docker Server ${DOCKER_VER} >= 20.10"
    else
        fail "Docker Server ${DOCKER_VER} < 20.10 (extra_hosts/host-gateway 需要 20.10+)"
    fi
else
    fail "docker 命令不存在"
fi

# Compose v2
if docker compose version >/dev/null 2>&1; then
    COMPOSE_VER=$(docker compose version --short 2>/dev/null || echo "0")
    if version_ge "${COMPOSE_VER}" "2.20"; then
        ok "Docker Compose ${COMPOSE_VER} >= 2.20"
    else
        fail "Docker Compose ${COMPOSE_VER} < 2.20 (depends_on required: false 需要 2.20+)"
    fi
else
    fail "docker compose (v2 plugin) 不可用"
fi

# =============================================================================
echo ">>> 2/8 环境文件检查"
# =============================================================================
ROOT_ENV="${ROOT}/.env"
BACKEND_ENV="${ROOT}/backend/.env"

if [ -f "${ROOT_ENV}" ]; then
    ok "根 .env 存在"
    PERM=$(stat -c '%a' "${ROOT_ENV}" 2>/dev/null || stat -f '%Lp' "${ROOT_ENV}" 2>/dev/null || echo "unknown")
    if [ "${PERM}" = "600" ]; then
        ok "根 .env 权限 ${PERM}"
    else
        warn "根 .env 权限 ${PERM} (建议 chmod 600)"
    fi
else
    fail "根 .env 不存在 (cp .env.example .env)"
fi

if [ -f "${BACKEND_ENV}" ]; then
    ok "backend/.env 存在"
    PERM=$(stat -c '%a' "${BACKEND_ENV}" 2>/dev/null || stat -f '%Lp' "${BACKEND_ENV}" 2>/dev/null || echo "unknown")
    if [ "${PERM}" = "600" ]; then
        ok "backend/.env 权限 ${PERM}"
    else
        warn "backend/.env 权限 ${PERM} (建议 chmod 600)"
    fi
else
    fail "backend/.env 不存在 (cp backend/.env.example backend/.env)"
fi

# =============================================================================
echo ">>> 3/8 MySQL SQL 文件检查"
# =============================================================================
if [ -f "${ROOT}/backend/mysqldir/orange.sql" ]; then
    ok "backend/mysqldir/orange.sql 存在"
else
    fail "backend/mysqldir/orange.sql 不存在"
fi

# =============================================================================
echo ">>> 4/8 环境变量一致性检查"
# =============================================================================

# 从根 .env 读取变量 (不 export, 避免污染)
load_env_val() {
    local file="$1" key="$2"
    # 缺少可选变量是正常状态；在 set -euo pipefail 下必须显式吞掉 grep 的 1。
    grep -E "^${key}=" "$file" 2>/dev/null | head -1 | cut -d'=' -f2- | sed 's/^["'\'']//' | sed 's/["'\'']$//' || true
}

if [ -f "${ROOT_ENV}" ] && [ -f "${BACKEND_ENV}" ]; then
    # MySQL 一致性
    ROOT_MYSQL_USER=$(load_env_val "${ROOT_ENV}" "OGS_MYSQL_USER")
    BACK_MYSQL_USER=$(load_env_val "${BACKEND_ENV}" "OGS_MYSQL_USER")
    ROOT_MYSQL_DB=$(load_env_val "${ROOT_ENV}" "OGS_MYSQL_DBNAME")
    BACK_MYSQL_DB=$(load_env_val "${BACKEND_ENV}" "OGS_MYSQL_DBNAME")
    ROOT_MYSQL_PWD=$(load_env_val "${ROOT_ENV}" "OGS_MYSQL_PASSWORD")
    BACK_MYSQL_PWD=$(load_env_val "${BACKEND_ENV}" "OGS_MYSQL_PASSWORD")

    [ "${ROOT_MYSQL_USER}" = "${BACK_MYSQL_USER}" ] && ok "OGS_MYSQL_USER 两处一致" || fail "OGS_MYSQL_USER 不一致 (根=${ROOT_MYSQL_USER} 后端=${BACK_MYSQL_USER})"
    [ "${ROOT_MYSQL_DB}"   = "${BACK_MYSQL_DB}"   ] && ok "OGS_MYSQL_DBNAME 两处一致" || fail "OGS_MYSQL_DBNAME 不一致"
    [ -n "${ROOT_MYSQL_PWD}" ] && [ "${ROOT_MYSQL_PWD}" = "${BACK_MYSQL_PWD}" ] && ok "OGS_MYSQL_PASSWORD 两处一致" || fail "OGS_MYSQL_PASSWORD 两处不一致或未设置"

    # Redis 密码一致性
    ROOT_REDIS_PWD=$(load_env_val "${ROOT_ENV}" "OGS_REDIS_PASSWORD")
    BACK_REDIS_PWD=$(load_env_val "${BACKEND_ENV}" "OGS_REDIS_PASSWORD")
    if [ -n "${ROOT_REDIS_PWD}" ] && [ "${ROOT_REDIS_PWD}" = "${BACK_REDIS_PWD}" ]; then
        ok "OGS_REDIS_PASSWORD 两处一致"
    elif [ -z "${ROOT_REDIS_PWD}" ] && [ -z "${BACK_REDIS_PWD}" ]; then
        warn "OGS_REDIS_PASSWORD 两处均空 (dev 可接受, 生产必须设置)"
    else
        fail "OGS_REDIS_PASSWORD 两处不一致"
    fi
fi

# =============================================================================
echo ">>> 5/8 密钥检查 (backend/.env)"
# =============================================================================
# SETUP-WIZARD: SECRET_KEY / FERNET_KEYS 缺失降级 WARN——首次启动会进入
#   /setup 网页向导并由服务端自动生成落盘（<数据卷>/runtime.env）。
#   第二个参数传 --strict 保留旧的 FAIL 行为（CI/自动化场景）。
STRICT_MODE=0
[ "${2:-}" = "--strict" ] && STRICT_MODE=1
if [ -f "${BACKEND_ENV}" ]; then
    SECRET_KEY=$(load_env_val "${BACKEND_ENV}" "OGS_FLASK_SECRET_KEY")
    if [ -z "${SECRET_KEY}" ] || [ "${SECRET_KEY}" = "CHANGE_ME" ] || [ "${SECRET_KEY}" = "CHANGE_ME_STRONG" ]; then
        if [ "${STRICT_MODE}" = "1" ]; then
            fail "OGS_FLASK_SECRET_KEY 为空或占位符 (--strict)"
        else
            warn "OGS_FLASK_SECRET_KEY 未配置——首次启动将由 /setup 向导生成"
        fi
    else
        ok "OGS_FLASK_SECRET_KEY 已设置"
    fi

    FERNET_KEYS=$(load_env_val "${BACKEND_ENV}" "OGS_FERNET_KEYS")
    FERNET_KEY=$(load_env_val "${BACKEND_ENV}" "OGS_FERNET_KEY")
    if [ -n "${FERNET_KEYS}" ]; then
        ok "OGS_FERNET_KEYS 已设置"
    elif [ -n "${FERNET_KEY}" ]; then
        warn "使用旧版 OGS_FERNET_KEY (推荐改用 OGS_FERNET_KEYS)"
    elif [ "${STRICT_MODE}" = "1" ]; then
        fail "OGS_FERNET_KEYS / OGS_FERNET_KEY 均未设置 (--strict)"
    else
        warn "OGS_FERNET_KEYS 未配置——首次启动将由 /setup 向导生成"
    fi
fi

# =============================================================================
echo ">>> 6/8 端口占用检查"
# =============================================================================
HTTP_PORT_VALUE=$(load_env_val "${ROOT_ENV:-/dev/null}" "OGS_HTTP_PORT")
HTTP_PORT_VALUE="${HTTP_PORT_VALUE:-8080}"

# DEPLOY-AUDIT P1-2: 端口被本项目自己的 app 容器占用是"重启/升级"的正常
#   状态, 判 FAIL 会让 make docker-up 不可重入 → 降级为 WARN 并说明。
_port_owned_by_self() {
    command -v docker >/dev/null 2>&1 \
        && docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null \
            | grep -E '[-_]app[-_ ]' | grep -q ":${HTTP_PORT_VALUE}->"
}
_port_in_use() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | grep -q ":${HTTP_PORT_VALUE} "
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | grep -q ":${HTTP_PORT_VALUE} "
    else
        return 1
    fi
}
if command -v ss >/dev/null 2>&1 || command -v netstat >/dev/null 2>&1; then
    if _port_in_use; then
        if _port_owned_by_self; then
            warn "端口 ${HTTP_PORT_VALUE} 由本项目 app 容器占用 (重启场景, compose 会接管)"
        else
            fail "端口 ${HTTP_PORT_VALUE} 已被其他进程占用 (换 OGS_HTTP_PORT 或停止占用进程)"
        fi
    else
        ok "端口 ${HTTP_PORT_VALUE} 未被占用"
    fi
else
    warn "ss/netstat 不可用, 跳过端口检查"
fi

# 宽泛的 ip_local_port_range 可能把公开服务端口分配给出站连接。即使没有进程
# LISTEN，残留 TIME_WAIT 也会让 Docker userland proxy 绑定失败。
if [ -r /proc/sys/net/ipv4/ip_local_port_range ]; then
    read -r EPHEMERAL_LOW EPHEMERAL_HIGH \
        < /proc/sys/net/ipv4/ip_local_port_range
    if [ "${HTTP_PORT_VALUE}" -ge "${EPHEMERAL_LOW}" ] \
        && [ "${HTTP_PORT_VALUE}" -le "${EPHEMERAL_HIGH}" ]; then
        RESERVED_PORTS=""
        [ -r /proc/sys/net/ipv4/ip_local_reserved_ports ] \
            && RESERVED_PORTS=$(cat /proc/sys/net/ipv4/ip_local_reserved_ports)
        PORT_RESERVED=0
        OLD_IFS="${IFS}"
        IFS=','
        for range in ${RESERVED_PORTS}; do
            case "${range}" in
                *-*)
                    range_low="${range%-*}"
                    range_high="${range#*-}"
                    if [ "${HTTP_PORT_VALUE}" -ge "${range_low}" ] \
                        && [ "${HTTP_PORT_VALUE}" -le "${range_high}" ]; then
                        PORT_RESERVED=1
                    fi
                    ;;
                "${HTTP_PORT_VALUE}")
                    PORT_RESERVED=1
                    ;;
            esac
        done
        IFS="${OLD_IFS}"
        if [ "${PORT_RESERVED}" = "1" ]; then
            ok "端口 ${HTTP_PORT_VALUE} 已从临时出站端口范围保留"
        else
            fail "端口 ${HTTP_PORT_VALUE} 位于临时出站端口范围 ${EPHEMERAL_LOW}-${EPHEMERAL_HIGH}, 且未配置 ip_local_reserved_ports"
            warn "先把 ${HTTP_PORT_VALUE} 合并进 net.ipv4.ip_local_reserved_ports, 等待现有 TIME_WAIT 释放后再启动"
        fi
    fi
fi

# =============================================================================
echo ">>> 7/8 Shell 环境变量覆盖检查"
# =============================================================================
# Compose 插值时 shell env 优先于 --env-file, 防止已 export 的旧值覆盖
OVERRIDE_VARS="OGS_MYSQL_PASSWORD OGS_REDIS_PASSWORD MYSQL_ROOT_PASSWORD OGS_HTTP_PORT OGS_BACKEND_IMAGE OGS_BACKEND_TAG"
OVERRIDE_FOUND=0
for v in ${OVERRIDE_VARS}; do
    if [ -n "${!v:-}" ]; then
        warn "Shell 已 export ${v} (会覆盖 .env 中的值, 建议 unset ${v})"
        OVERRIDE_FOUND=1
    fi
done
[ "${OVERRIDE_FOUND}" = "0" ] && ok "Shell 环境变量无覆盖"

# =============================================================================
echo ">>> 8/8 Compose 配置解析检查"
# =============================================================================
if [ -f "${ROOT_ENV}" ] && [ -f "${COMPOSE_FILE}" ]; then
    PROFILE_ARG=""
    [ "${MODE}" = "bundled" ] && PROFILE_ARG="--profile bundled"

    if docker compose --env-file "${ROOT_ENV}" -f "${COMPOSE_FILE}" ${PROFILE_ARG} config --quiet 2>/dev/null; then
        ok "docker compose config --quiet 通过"
    else
        fail "docker compose config 解析失败"
    fi
else
    warn "跳过 compose config 检查 (缺少 .env 或 compose 文件)"
fi

# =============================================================================
# MySQL 旧卷警告 (仅 bundled 模式)
# =============================================================================
if [ "${MODE}" = "bundled" ]; then
    PROJECT_NAME=$(load_env_val "${ROOT_ENV:-/dev/null}" "COMPOSE_PROJECT_NAME")
    PROJECT_NAME="${PROJECT_NAME:-orangeserver}"
    VOL_NAME="${PROJECT_NAME}_mysql-data"
    if docker volume inspect "${VOL_NAME}" >/dev/null 2>&1; then
        warn "已存在 MySQL 数据卷 ${VOL_NAME}"
        warn "  - 首次部署: orange.sql 不会重新执行 (数据已存在)"
        warn "  - 密码变更: 旧数据中的账号密码不会自动更新"
        warn "  - 如需清空: 人工确认后执行 docker volume rm ${VOL_NAME}"
    fi
fi

# =============================================================================
# 汇总
# =============================================================================
echo ""
echo "============================================"
echo "  预检汇总: OK=${PASS}  FAIL=${FAIL}  WARN=${WARN}"
echo "============================================"

if [ "${FAIL}" -gt 0 ]; then
    echo "[ABORT] ${FAIL} 项检查失败, 请先修复后再 make docker-up"
    exit 1
fi

if [ "${WARN}" -gt 0 ]; then
    echo "[WARN] ${WARN} 项警告 (可继续, 但建议修复)"
fi

echo "[OK] 预检通过"
exit 0
