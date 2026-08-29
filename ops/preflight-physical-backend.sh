#!/usr/bin/env bash
# =============================================================================
# OrangeServer 物理机后端部署预检脚本 (REV49)
# =============================================================================
# 只读检查，不安装软件、不修改文件、不启动服务、不执行迁移。
# 任一 FAIL 立即非零退出。
#
# 用法：
#   bash ops/preflight-physical-backend.sh            # 必填项缺失降级 WARN（走网页向导）
#   bash ops/preflight-physical-backend.sh --strict   # 必填项缺失 FAIL（旧行为，CI 用）
# =============================================================================

set -Eeuo pipefail

STRICT_MODE=0
if [ "${1:-}" = "--strict" ]; then
    STRICT_MODE=1
fi

PASS=0
FAIL=0
WARN=0

ok()   { echo "  [OK]   $1"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $1" >&2; FAIL=$((FAIL+1)); }
warn() { echo "  [WARN] $1"; WARN=$((WARN+1)); }

# ---- 路径 ----
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND="${ROOT}/backend"
ENV_FILE="/etc/orangeserver/backend.env"
VENV_PYTHON="/opt/orangeserver/venv/bin/python"
DATA_DIR="/data/orangeserver"

# ---- 辅助函数 ----
load_env_val() {
    local file="$1" key="$2"
    grep -E "^${key}=" "$file" 2>/dev/null | head -1 | cut -d'=' -f2- | sed 's/^["'\'']//' | sed 's/["'\'']$//'
}

is_placeholder() {
    local val="$1"
    case "$val" in
        ""|__REQUIRED*__|__FILL*__|CHANGE_ME*|YOUR_*|example*|placeholder*) return 0 ;;
        *) return 1 ;;
    esac
}

# =============================================================================
echo ">>> 1/12 系统架构检查"
# =============================================================================
if [ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ]; then
    ok "Linux x86_64"
else
    fail "需要 Linux x86_64 (当前: $(uname -s) $(uname -m))"
fi

# =============================================================================
echo ">>> 2/12 glibc 版本记录"
# =============================================================================
if command -v getconf >/dev/null 2>&1; then
    GLIBC_VER=$(getconf GNU_LIBC_VERSION 2>/dev/null || echo "unknown")
    ok "glibc: ${GLIBC_VER}"
else
    warn "getconf 不可用, 无法检测 glibc 版本"
fi

# =============================================================================
echo ">>> 3/12 Python 运行时检查"
# =============================================================================
if [ -x "${VENV_PYTHON}" ]; then
    PY_VER=$("${VENV_PYTHON}" --version 2>&1)
    if echo "${PY_VER}" | grep -qE "Python 3\.(11|12)"; then
        ok "${PY_VER}"
    else
        fail "需要 Python 3.11/3.12 (当前: ${PY_VER})"
    fi
else
    fail "${VENV_PYTHON} 不存在或不可执行"
fi

# =============================================================================
echo ">>> 4/12 环境文件检查"
# =============================================================================
if [ -f "${ENV_FILE}" ]; then
    PERM=$(stat -c '%a' "${ENV_FILE}" 2>/dev/null || echo "unknown")
    if [ "${PERM}" = "600" ]; then
        ok "环境文件权限 ${PERM}"
    else
        fail "环境文件权限 ${PERM} (需要 600)"
    fi
else
    fail "${ENV_FILE} 不存在"
fi

# =============================================================================
echo ">>> 5/12 必填环境变量检查"
# =============================================================================
if [ -f "${ENV_FILE}" ]; then
    REQUIRED_KEYS="OGS_FLASK_SECRET_KEY OGS_FERNET_KEYS OGS_MYSQL_HOST OGS_MYSQL_PORT OGS_MYSQL_DBNAME OGS_MYSQL_USER OGS_MYSQL_PASSWORD OGS_REDIS_HOST OGS_REDIS_PORT OGS_DATA_DIR"

    # 兼容旧版 OGS_FERNET_KEY
    if grep -q '^OGS_FERNET_KEY=' "${ENV_FILE}" 2>/dev/null && ! grep -q '^OGS_FERNET_KEYS=' "${ENV_FILE}" 2>/dev/null; then
        warn "使用旧版 OGS_FERNET_KEY (推荐改用 OGS_FERNET_KEYS)"
        REQUIRED_KEYS="${REQUIRED_KEYS//OGS_FERNET_KEYS/OGS_FERNET_KEY}"
    fi

    # SETUP-WIZARD: 必填项为空/占位不再直接 FAIL——首次启动会进入网页配置向导。
    #   传 --strict 保留旧行为（要求 env 文件配置齐全，CI/自动化场景用）。
    MISSING_REQUIRED=0
    for key in ${REQUIRED_KEYS}; do
        val=$(load_env_val "${ENV_FILE}" "${key}")
        if is_placeholder "${val}"; then
            MISSING_REQUIRED=1
            if [ "${STRICT_MODE:-0}" = "1" ]; then
                fail "${key} 为空或仍为占位符 (--strict)"
            else
                warn "${key} 未配置——首次启动将进入 /setup 网页配置向导"
            fi
        else
            ok "${key} 已设置"
        fi
    done
fi

# =============================================================================
echo ">>> 6/12 系统工具检查"
# =============================================================================
for cmd in rsync ssh curl ss systemctl; do
    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$cmd 可用"
    else
        fail "$cmd 不可用"
    fi
done

# =============================================================================
echo ">>> 7/12 数据目录检查"
# =============================================================================
REQUIRED_DIRS="avatars file key log containers/temp"
for d in ${REQUIRED_DIRS}; do
    dir="${DATA_DIR}/${d}"
    if [ -d "${dir}" ]; then
        ok "${dir} 存在"
    else
        fail "${dir} 不存在 (需要预创建)"
    fi
done

# =============================================================================
echo ">>> 8/12 端口占用检查"
# =============================================================================
if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | grep -q ':28000 '; then
        fail "端口 28000 已被占用"
    else
        ok "端口 28000 未被占用"
    fi
else
    warn "ss 不可用, 跳过端口检查"
fi

# =============================================================================
echo ">>> 9/12 Python 依赖检查"
# =============================================================================
if [ -x "${VENV_PYTHON}" ]; then
    if "${VENV_PYTHON}" -m pip check >/dev/null 2>&1; then
        ok "pip check 通过"
    else
        warn "pip check 有警告"
    fi
fi

# =============================================================================
echo ">>> 10/12 依赖导入检查"
# =============================================================================
if [ -x "${VENV_PYTHON}" ]; then
    IMPORT_RESULT=$("${VENV_PYTHON}" -c "
import flask
import gevent
import geventwebsocket
import gunicorn
import cryptography
import bcrypt
import PIL
import pymysql
import redis
print('IMPORT_OK')
" 2>&1 || echo "IMPORT_FAIL")

    if echo "${IMPORT_RESULT}" | grep -q "IMPORT_OK"; then
        ok "全部依赖导入成功"
    else
        fail "依赖导入失败: $(echo "${IMPORT_RESULT}" | head -5)"
    fi
fi

# =============================================================================
echo ">>> 11/12 数据库/Redis 连通检查"
# =============================================================================
if [ -f "${ENV_FILE}" ] && [ -x "${VENV_PYTHON}" ]; then
    MYSQL_HOST=$(load_env_val "${ENV_FILE}" "OGS_MYSQL_HOST")
    MYSQL_PORT=$(load_env_val "${ENV_FILE}" "OGS_MYSQL_PORT")
    MYSQL_USER=$(load_env_val "${ENV_FILE}" "OGS_MYSQL_USER")
    MYSQL_PASS=$(load_env_val "${ENV_FILE}" "OGS_MYSQL_PASSWORD")
    MYSQL_DB=$(load_env_val "${ENV_FILE}" "OGS_MYSQL_DBNAME")

    if ! is_placeholder "${MYSQL_PASS}"; then
        # DEPLOY-AUDIT P0-4: 旧版把字面量 '***' 当密码传给 pymysql, 预检必失败。
        #   凭据经环境变量传递 (不进命令行/日志), 输出不含明文。
        MYSQL_RESULT=$(PF_HOST="${MYSQL_HOST}" PF_PORT="${MYSQL_PORT}" \
            PF_USER="${MYSQL_USER}" PF_PASS="${MYSQL_PASS}" PF_DB="${MYSQL_DB}" \
            "${VENV_PYTHON}" -c "
import os
import pymysql
try:
    conn = pymysql.connect(host=os.environ['PF_HOST'], port=int(os.environ['PF_PORT']),
                           user=os.environ['PF_USER'], password=os.environ['PF_PASS'],
                           database=os.environ['PF_DB'], connect_timeout=5)
    cur = conn.cursor()
    cur.execute('SELECT 1')
    conn.close()
    print('MYSQL_OK')
except Exception as e:
    print(f'MYSQL_FAIL: {type(e).__name__}')
" 2>&1 || echo "MYSQL_FAIL: script error")

        if echo "${MYSQL_RESULT}" | grep -q "MYSQL_OK"; then
            ok "MySQL 连通且认证成功"
        else
            fail "MySQL 连接失败: $(echo "${MYSQL_RESULT}" | head -1)"
        fi
    else
        warn "MySQL 密码为占位符, 跳过连通检查"
    fi

    REDIS_HOST=$(load_env_val "${ENV_FILE}" "OGS_REDIS_HOST")
    REDIS_PORT=$(load_env_val "${ENV_FILE}" "OGS_REDIS_PORT")
    REDIS_PASS=$(load_env_val "${ENV_FILE}" "OGS_REDIS_PASSWORD")

    # DEPLOY-AUDIT P0-4: 同上, 真实密码经 env 传递 (占位符/空按无密码处理)
    REDIS_RESULT=$(PF_HOST="${REDIS_HOST}" PF_PORT="${REDIS_PORT}" PF_PASS="${REDIS_PASS}" \
        "${VENV_PYTHON}" -c "
import os
import redis
try:
    pw = os.environ.get('PF_PASS') or ''
    r = redis.Redis(host=os.environ['PF_HOST'], port=int(os.environ['PF_PORT']),
                    password=pw if pw and not pw.startswith('__') else None,
                    socket_connect_timeout=5)
    r.ping()
    print('REDIS_OK')
except Exception as e:
    print(f'REDIS_FAIL: {type(e).__name__}')
" 2>&1 || echo "REDIS_FAIL: script error")

    if echo "${REDIS_RESULT}" | grep -q "REDIS_OK"; then
        ok "Redis PING 成功"
    else
        fail "Redis 连接失败: $(echo "${REDIS_RESULT}" | head -1)"
    fi
fi

# =============================================================================
echo ">>> 12/12 配置加载检查 (仅 config, 不导入 wsgi)"
# =============================================================================
if [ -f "${ENV_FILE}" ] && [ -x "${VENV_PYTHON}" ]; then
    CONFIG_RESULT=$(cd "${BACKEND}" && "${VENV_PYTHON}" -c "
import os
# 加载环境文件
from dotenv import load_dotenv
load_dotenv('${ENV_FILE}', override=True)
try:
    # DEPLOY-AUDIT P0-4: config.py 是模块级常量而非 Config 类,
    #   import 本身即触发 fail-fast 校验
    import app.core.config  # noqa: F401
    print('CONFIG_OK')
except SystemExit as e:
    print(f'CONFIG_FAIL: SystemExit({e.code})')
except Exception as e:
    print(f'CONFIG_FAIL: {type(e).__name__}: {e}')
" 2>&1 || echo "CONFIG_FAIL: script error")

    if echo "${CONFIG_RESULT}" | grep -q "CONFIG_OK"; then
        ok "配置加载成功"
    else
        fail "配置加载失败: $(echo "${CONFIG_RESULT}" | head -3)"
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
    echo "[ABORT] ${FAIL} 项检查失败, 修复后再启动服务"
    exit 1
fi

if [ "${WARN}" -gt 0 ]; then
    echo "[WARN] ${WARN} 项警告 (可继续, 但建议修复)"
fi

echo "[OK] 预检通过"
exit 0
