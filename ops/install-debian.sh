#!/bin/bash
# =============================================================================
# OrangeServer 物理机一键部署脚本 - Debian/Ubuntu 版 v4.0 (2026-07-04)
# =============================================================================
# 适配 Debian 11+ / Ubuntu 20.04+
# 注意: Debian 仓库 mysql-server 是 MariaDB 10.x metapackage (不是 MySQL 8.0)
#       PyMySQL 1.x 连 MariaDB 走 mysql_native_password 兼容, 无影响
#
# RHEL/CentOS/Rocky 用户请用 ops/install.sh
#
# 部署流程:
#   1. 检测 OS (限定 Debian/Ubuntu)
#   2. 校验 systemd 可用
#   3. 安装系统依赖 (python3, nginx, redis, mariadb-server)
#   4. 拷贝本地源码到 /usr/src/orangeserver
#   5. pip 安装 Python 包
#   6. 导入自检
#   7. MariaDB 10.x 安装 (apt)
#   8. 创建 orange 数据库 + 导入 orange.sql
#   9. 数据迁移
#  10. 引导 .env 配置
# =============================================================================

set -euo pipefail  # 严格模式

installdir=/usr/src/orangeserver
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# =============================================================================
# 1. OS 检测 (限定 Debian/Ubuntu)
# =============================================================================
if [ ! -f /etc/os-release ]; then
    echo "[FAIL] 找不到 /etc/os-release"
    exit 1
fi
. /etc/os-release
OS_ID="${ID:-unknown}"
case "${OS_ID}" in
    ubuntu|debian)
        PKG_MGR="apt-get"
        ;;
    centos|rhel|rocky|almalinux|amzn|fedora)
        echo "[FAIL] 检测到 ${OS_ID}，但本脚本仅支持 Debian/Ubuntu"
        echo "  RHEL 系用户请改用: sudo bash ops/install.sh"
        exit 1
        ;;
    *)
        echo "[FAIL] 未识别的 OS: ${OS_ID}"
        echo "  本脚本仅支持: ubuntu / debian"
        echo "  RHEL 系用户请用: sudo bash ops/install.sh"
        exit 1
        ;;
esac
echo "[INFO] 检测到 OS: ${OS_ID} (注意: Debian 仓库 mysql-server 是 MariaDB 10.x)"

# =============================================================================
# 2. 校验 systemd 可用
# =============================================================================
if ! command -v systemctl >/dev/null 2>&1; then
    echo "[FAIL] 找不到 systemctl 命令"
    echo "  请在真实 Debian/Ubuntu 系统上运行 (不是 chroot/容器)"
    echo "  Docker 容器请用 Docker Compose 部署"
    exit 1
fi
echo "[INFO] systemd 可用"

# =============================================================================
# 3. 安装系统依赖
# =============================================================================
echo ">>> 安装系统依赖 (${PKG_MGR})..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    nginx redis-server \
    wget curl ca-certificates \
    mariadb-client || true
echo "[INFO] 已装 mariadb-client (PyMySQL 兼容 MariaDB 协议)"

# =============================================================================
# 4. 拷贝本地源码到 installdir
# =============================================================================
if [ ! -d "${SCRIPT_DIR}/../backend" ]; then
    echo "[FAIL] 当前目录结构异常，找不到 backend/ 子目录"
    echo "  请在 OrangeServer 仓库根目录下运行本脚本"
    exit 1
fi

echo ">>> 拷贝本地源码到 ${installdir}..."
mkdir -p "${installdir}"
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude='.git' --exclude='.github' \
        --exclude='data' --exclude='node_modules' \
        --exclude='dist' --exclude='__pycache__' \
        --exclude='.pytest_cache' --exclude='_archive*' \
        --exclude='*.log' \
        "${SCRIPT_DIR}/.." "${installdir}/"
else
    cp -a "${SCRIPT_DIR}/.." "${installdir}/"
    rm -rf "${installdir}/.git" "${installdir}/data" "${installdir}/node_modules" \
          "${installdir}/dist" "${installdir}/_archive"* 2>/dev/null || true
    find "${installdir}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    find "${installdir}" -name "*.log" -delete 2>/dev/null || true
fi
echo "[OK] 源码已部署到 ${installdir}/ (backend/ 子目录)"

# =============================================================================
# 5. pip 安装 Python 依赖 (PIP_INDEX_URL 可覆盖)
# =============================================================================
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
echo ">>> pip 安装 Python 依赖..."
pip3 install --upgrade pip setuptools wheel
pip3 install -i "${PIP_INDEX_URL}" -r "${installdir}/backend/requirements.txt"

# =============================================================================
# 6. 导入自检 (复用 RHEL 脚本的检查项)
# =============================================================================
echo '>>> 导入自检...'
python3 << 'PYEOF'
import sys
required = [
    ('flask',          'Flask'),
    ('sqlalchemy',     'SQLAlchemy'),
    ('pymysql',        'PyMySQL (兼容 MySQL/MariaDB)'),
    ('redis',          'redis 客户端'),
    ('paramiko',       'SSH 客户端'),
    ('bcrypt',         'bcrypt 密码哈希'),
    ('cryptography',   'Fernet 对称加密'),
    ('apscheduler',    'APScheduler 调度'),
    ('gevent',         'gevent 协程'),
    ('geventwebsocket','geventwebsocket (WebSSH/SFTP)'),
    ('PIL',            'Pillow'),
    ('gunicorn',       'gunicorn (生产 WSGI)'),
    ('dotenv',         'python-dotenv'),
]
failed = []
for mod, desc in required:
    try:
        __import__(mod)
        print(f'  [OK] {mod:20s} {desc}')
    except ImportError as e:
        print(f'  [FAIL] {mod:20s} {desc}  -- {e}')
        failed.append(mod)
if failed:
    print(f'\n[ABORT] {len(failed)} 模块 import 失败: {", ".join(failed)}')
    sys.exit(1)
print('\n>>> 所有模块自检通过 ✓')
PYEOF

# =============================================================================
# 7. MariaDB 安装 (apt 源)
# =============================================================================
if ! dpkg -l | grep -qE 'mariadb-server'; then
    echo ">>> 通过 apt 安装 MariaDB..."
    apt-get install -y --no-install-recommends mariadb-server || {
        echo "[FAIL] apt 装 MariaDB 失败"
        exit 1
    }
    systemctl start mariadb || service mariadb start || true
    systemctl enable mariadb || true
    echo "[OK] MariaDB 已安装, 默认无密码 (socket auth)"
else
    echo "[INFO] MariaDB 已装, 跳过"
fi

# =============================================================================
# 8. MariaDB root 密码（带强度校验）
# =============================================================================
mysqlpass1=""
while true; do
    read -s -p "请输入新的 MariaDB root 密码 (>=12位含大小写+数字, 不含单引号): " mysqlpass1; echo
    if [ ${#mysqlpass1} -lt 12 ]; then
        echo "[FAIL] 密码长度不足 12 位"; continue
    fi
    if ! [[ "$mysqlpass1" =~ [A-Z] ]]; then
        echo "[FAIL] 密码必须包含大写字母"; continue
    fi
    # DEPLOY-AUDIT: 修复 [[ ]] 括号不匹配的存量语法错误 (脚本此前无法执行)
    if ! [[ "$mysqlpass1" =~ [a-z] ]]; then
        echo "[FAIL] 密码必须包含小写字母"; continue
    fi
    if ! [[ "$mysqlpass1" =~ [0-9] ]]; then
        echo "[FAIL] 密码必须包含数字"; continue
    fi
    if [[ "$mysqlpass1" =~ \' ]]; then
        echo "[FAIL] 密码不能含单引号"; continue
    fi
    read -s -p "再次输入密码确认: " mysqlpass2; echo
    if [ "$mysqlpass1" != "$mysqlpass2" ]; then
        echo "[FAIL] 两次密码不一致"; continue
    fi
    break
done

# 准备 .my.cnf
OGS_MYCNF=/tmp/.ogsmysql.cnf
cat > "$OGS_MYCNF" <<EOF
[client]
user=root
password='$mysqlpass1'
EOF
chmod 600 "$OGS_MYCNF"

# MariaDB 首次安装默认无密码 (socket auth), 设密码
echo ">>> 设置 MariaDB root 密码..."
mysql -u root <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '$mysqlpass1';
FLUSH PRIVILEGES;
SQL

# =============================================================================
# 9. 建库 + 建表
# =============================================================================
echo ">>> 创建 orange 数据库..."
mysql --defaults-file="$OGS_MYCNF" \
    -e "CREATE DATABASE IF NOT EXISTS orange DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_general_ci;"

echo ">>> 导入完整 SQL..."
mysql --defaults-file="$OGS_MYCNF" orange \
    < "${installdir}/backend/mysqldir/orange.sql"

echo ">>> 数据库列表："
mysql --defaults-file="$OGS_MYCNF" -e "SHOW DATABASES;"
mysql --defaults-file="$OGS_MYCNF" -e "USE orange; SHOW TABLES;"

# DEPLOY-AUDIT P0-5: 创建独立业务账号 (旧版从未建 app_user 却把 root 口令写进 .env)
echo ">>> 创建业务数据库账号 app_user..."
apppass=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
mysql --defaults-file="$OGS_MYCNF" <<SQL
CREATE USER IF NOT EXISTS 'app_user'@'localhost' IDENTIFIED BY '${apppass}';
ALTER USER 'app_user'@'localhost' IDENTIFIED BY '${apppass}';
GRANT ALL PRIVILEGES ON orange.* TO 'app_user'@'localhost';
FLUSH PRIVILEGES;
SQL

rm -f "$OGS_MYCNF"

# =============================================================================
# 10. .env 配置
# =============================================================================
echo ""
echo "============================================================"
echo "  现在配置 .env"
echo "============================================================"

if [ ! -f "${installdir}/backend/.env" ]; then
    cp "${installdir}/backend/.env.example" "${installdir}/backend/.env"
    chmod 600 "${installdir}/backend/.env"
fi

# ⓘ 与 RHEL 版一致: OGS_MYSQL_USER 保持 app_user 业务账号, OGS_REDIS_PORT=6379
sed -i "s|^OGS_MYSQL_HOST=.*|OGS_MYSQL_HOST=127.0.0.1|" "${installdir}/backend/.env"
sed -i "s|^OGS_MYSQL_PORT=.*|OGS_MYSQL_PORT=3306|" "${installdir}/backend/.env"
sed -i "s|^OGS_MYSQL_DBNAME=.*|OGS_MYSQL_DBNAME=orange|" "${installdir}/backend/.env"
# DEPLOY-AUDIT P0-5: 写业务账号口令, 不再误写 root 口令
sed -i "s|^OGS_MYSQL_USER=.*|OGS_MYSQL_USER=app_user|" "${installdir}/backend/.env"
sed -i "s|^OGS_MYSQL_PASSWORD=.*|OGS_MYSQL_PASSWORD=${apppass}|" "${installdir}/backend/.env"
sed -i "s|^OGS_REDIS_HOST=.*|OGS_REDIS_HOST=127.0.0.1|" "${installdir}/backend/.env"
sed -i "s|^OGS_REDIS_PORT=.*|OGS_REDIS_PORT=6379|" "${installdir}/backend/.env"

# 提示用户设置密钥
echo ""
echo "请编辑 ${installdir}/backend/.env，至少修改以下 2 项："
echo "  OGS_FLASK_SECRET_KEY  (生产必填)"
echo "  OGS_FERNET_KEYS        (R1-22 推荐: 单 key 起步)"
echo ""

# 邮箱功能
read -p "
是否启用邮箱功能 (1/2): " mailwork
if [ "$mailwork" = "1" ]; then
    read -p "邮箱账号: " mailzh
    read -p "SMTP 授权码: " mailstmppasswd
    read -p "SMTP 域名: " mailstmpdomain
    sed -i "s|^OGS_MAIL_USER=.*|OGS_MAIL_USER=${mailzh}|" "${installdir}/backend/.env"
    sed -i "s|^OGS_MAIL_PASSWORD=.*|OGS_MAIL_PASSWORD=${mailstmppasswd}|" "${installdir}/backend/.env"
    sed -i "s|^OGS_MAIL_SMTP=.*|OGS_MAIL_SMTP=${mailstmpdomain}|" "${installdir}/backend/.env"
fi

# =============================================================================
# 11. 数据迁移 (DEPLOY-AUDIT P1-6: 等待密钥配置完成后执行, 并检查退出码)
# =============================================================================
echo ''
read -p ">>> 请先编辑 backend/.env 填好 SECRET_KEY / FERNET_KEYS, 完成后按回车继续执行数据迁移..." _confirm
cd "${installdir}/backend"
pythondir=$(which python3)
if "$pythondir" -m app.tools.migrate_comma_to_junction 2>&1 | tee /tmp/migrate_comma.log \
    && [ "${PIPESTATUS[0]}" -eq 0 ]; then
    echo '>>> 迁移完成'
else
    echo '!!! 数据迁移失败, 详见 /tmp/migrate_comma.log' >&2
    exit 1
fi

# =============================================================================
# 12. 收尾
# =============================================================================
echo ''
echo '============================================================'
echo '  部署完成 (Debian/Ubuntu + MariaDB 10.x)'
echo '  初始化账号: admin / admin'
echo '  业务端口: 28000'
echo '  数据库: MariaDB 10.x (兼容 MySQL 8.0 协议, PyMySQL 可正常连接)'
echo '  业务账号: app_user (与 .env.example 默认一致)'
echo ''
echo '  推荐启动方式:'
echo '    生产: supervisorctl start orange:*'
echo '    开发: ./ops/start.sh start (从 backend/ 目录)'
echo '============================================================'
