#!/bin/bash
# =============================================================================
# OrangeServer 物理机一键部署脚本 v4.0 (2026-07-04 重写)
# =============================================================================
# 适配 RHEL 系 (CentOS / RHEL / Rocky Linux / AlmaLinux / Amazon Linux)
# 适配 Debian/Ubuntu (自动检测后调 install-debian.sh, 走 MariaDB 10.x 路径)
#
# 修复记录 (v4.0):
#   - 去 OSS 下载: 直接从当前 git checkout 拷贝代码到 /usr/src/orangeserver
#   - 修 MySQL 8.0 临时密码分隔符: 'root@localhost: ' (原 'localhost: ' 错)
#   - 修 sed 改 OGS_MYSQL_USER=root 错误: 保持 .env.example 默认 app_user 业务账号
#   - 修密码含特殊字符破 SQL: 用 .my.cnf 临时文件传
#   - 删 libmysqlclient-dev: 项目用 PyMySQL 纯 Python 不需要 C 客户端
#   - 加 PIP_INDEX_URL 支持: 默认清华源, 海外/代理可覆盖
#   - 加 systemd 校验: 容器/chroot 环境跑 install.sh 会早退
#   - Fernet key 注释 R1-22: 单 key 起步, 与 deploy/supervisor 模板一致
#   - 多 OS 兼容保留: Debian 检测后转调 install-debian.sh
#
# 前置条件:
#   - RHEL 7+ / Rocky Linux 8+ / AlmaLinux 8+ / CentOS 7+ / Amazon Linux 2+
#   - 或 Ubuntu 20.04+ / Debian 11+
#   - 已配置 yum/apt 源
#   - 当前用户在源码根目录 (含 backend/ 目录)
#
# 部署流程:
#   1. 检测 OS_FAMILY (rhel / debian)
#   2. RHEL: 安装依赖 → MySQL 8.0 → 建库 → 配 .env
#      Debian: 转调 install-debian.sh (走 MariaDB 10.x 路径)
# =============================================================================

set -euo pipefail  # 严格模式: 出错立即退出, 未定义变量报错

installdir=/usr/src/orangeserver
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# =============================================================================
# 1. OS 检测 (/etc/os-release, 同时支持 rhel 和 debian)
# =============================================================================
if [ ! -f /etc/os-release ]; then
    echo "[FAIL] 找不到 /etc/os-release，无法识别 OS 类型"
    exit 1
fi
. /etc/os-release
OS_ID="${ID:-unknown}"
OS_VERSION="${VERSION_ID:-unknown}"
case "${OS_ID}" in
    centos|rhel|rocky|almalinux|amzn|fedora)
        OS_FAMILY="rhel"
        PKG_MGR="yum"
        if command -v dnf >/dev/null 2>&1; then
            PKG_MGR="dnf"  # CentOS 8+ / RHEL 8+ / Rocky / AlmaLinux
        fi
        ;;
    ubuntu|debian)
        OS_FAMILY="debian"
        PKG_MGR="apt-get"
        ;;
    *)
        echo "[FAIL] 未识别的 OS: ${OS_ID} (${OS_VERSION})"
        echo "  支持: centos/rhel/rocky/almalinux/amzn/fedora (RHEL 系)"
        echo "        ubuntu/debian (Debian 系)"
        exit 1
        ;;
esac
echo "[INFO] 检测到 OS: ${OS_ID} ${OS_VERSION} (family=${OS_FAMILY}, pkg_mgr=${PKG_MGR})"

# =============================================================================
# 2. 校验 systemd 可用 (容器/chroot 环境跑 install.sh 会早退)
# =============================================================================
if ! command -v systemctl >/dev/null 2>&1; then
    echo "[FAIL] 找不到 systemctl 命令"
    echo "  本脚本需要 systemd 管理 nginx/redis/mysql 服务"
    echo "  Docker 容器 / chroot 环境无 systemd，请用 Docker Compose 部署:"
    echo "    docker compose -f deploy/docker-compose.yml up -d"
    exit 1
fi
if [ ! -d /run/systemd/system ]; then
    echo "[FAIL] /run/systemd/system 不存在，systemd 未运行"
    echo "  请在真实 RHEL/Debian 系统上运行 (不是 chroot/容器)"
    exit 1
fi
echo "[INFO] systemd 可用"

# =============================================================================
# 3. OS 路由: Debian 系转调 install-debian.sh (避免重复 200 行逻辑)
# =============================================================================
if [ "${OS_FAMILY}" = "debian" ]; then
    # ⓘ 修复: Debian 系检测后转调专门脚本 (避免 apt-get / DEBIAN_FRONTEND=noninteractive
    #   在两个脚本里重复维护). install-debian.sh 走 MariaDB 10.x 路径
    #   (因为 Debian 仓库的 mysql-server metapackage 是 MariaDB, 不是 MySQL 8.0)
    echo "[INFO] Debian/Ubuntu 系: 转调 install-debian.sh (走 MariaDB 10.x 路径)"
    DEBIAN_FRONTEND=noninteractive  # 防 apt 交互卡住 (转给子脚本)
    exec "${SCRIPT_DIR}/install-debian.sh" "$@"
    # 下面 RHEL 分支不会执行
fi

# =============================================================================
# 以下是 RHEL 系专属路径 (MySQL 8.0 官方 yum 源)
# =============================================================================

# 4. 安装系统依赖
echo ">>> 安装系统依赖 (${PKG_MGR})..."
${PKG_MGR} -y install python3 epel-release wget
${PKG_MGR} -y install nginx redis
${PKG_MGR} -y remove mariadb* 2>/dev/null || true  # RHEL 8 自带 mariadb，先卸

# =============================================================================
# 5. 拷贝本地源码到 installdir (替代原 OSS 下载) + sha256 校验
# =============================================================================
if [ ! -d "${SCRIPT_DIR}/../backend" ]; then
    echo "[FAIL] 当前目录结构异常，找不到 backend/ 子目录"
    echo "  请在 OrangeServer 仓库根目录下运行本脚本"
    echo "  即: cd /path/to/OrangeServer && sudo bash ops/install.sh"
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

# ⓘ 修复: 拷贝后用 sha256sum 算源码指纹落日志 (替代原 OSS 下载 sha256 校验)
#   目的: 安装后可回溯 "这次装的代码是哪一份 commit"
echo ">>> 计算源码 sha256 (用于回溯)..."
( cd "${installdir}" && find . -type f -not -path './data/*' -not -name '*.log' \
    | sort | xargs sha256sum | sha256sum ) | tee /tmp/ogs_source_sha256.txt

# =============================================================================
# 6. pip 安装 Python 依赖 (PIP_INDEX_URL 可覆盖)
# =============================================================================
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
echo ">>> pip 安装 Python 依赖 (PIP_INDEX_URL=${PIP_INDEX_URL})..."
pip3 install --upgrade pip setuptools wheel
pip3 install -i "${PIP_INDEX_URL}" -r "${installdir}/backend/requirements.txt"

# =============================================================================
# 7. 导入自检 (13 个关键模块)
# =============================================================================
echo '>>> 导入自检...'
python3 << 'PYEOF'
import sys
required = [
    ('flask',          'Flask'),
    ('sqlalchemy',     'SQLAlchemy'),
    ('pymysql',        'PyMySQL (MySQL 驱动)'),
    ('redis',          'redis 客户端'),
    ('paramiko',       'SSH 客户端'),
    ('bcrypt',         'bcrypt 密码哈希'),
    ('cryptography',   'Fernet 对称加密 (隐藏依赖)'),
    ('apscheduler',    'APScheduler 调度'),
    ('gevent',         'gevent 协程'),
    ('geventwebsocket','geventwebsocket (WebSSH/SFTP)'),
    ('PIL',            'Pillow (P1-5 captcha 后端化必需)'),
    ('gunicorn',       'gunicorn (生产 WSGI)'),
    ('dotenv',         'python-dotenv (.env 加载)'),
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
    print('请手动执行: pip3 install -r requirements.txt')
    sys.exit(1)
print('\n>>> 所有模块自检通过 ✓')
PYEOF

# =============================================================================
# 8. MySQL 8.0 安装 (官方 yum 源)
# =============================================================================
MYSQL_VERSION="${MYSQL_VERSION:-8.0}"
MYSQL_REPO_URL="${MYSQL_REPO_URL:-https://dev.mysql.com/get/mysql80-community-release-el7-7.noarch.rpm}"

mysqlinstall=$(rpm -qa 2>/dev/null | grep -E "mysql-community-(server|client)" | wc -l)
if [ "$mysqlinstall" = "0" ]; then
    echo ">>> 安装 MySQL ${MYSQL_VERSION} 官方 yum 源..."
    ${PKG_MGR} -y install "${MYSQL_REPO_URL}"

    if [ "${MYSQL_VERSION%%.*}" = "8" ]; then
        ${PKG_MGR}-config-manager --disable mysql80-community >/dev/null 2>&1 || true
        ${PKG_MGR}-config-manager --enable mysql80-community >/dev/null 2>&1 || true
    fi

    echo ">>> 通过 ${PKG_MGR} 安装 MySQL ${MYSQL_VERSION}..."
    ${PKG_MGR} -y install mysql-community-server mysql-community-client

    systemctl start mysqld
    systemctl enable mysqld

    # ⓘ 修复: MySQL 8.0 临时密码日志实际格式是
    #   [Note] [MY-010454] [Server] A temporary password is generated for root@localhost: <pwd>
    # 分隔符应是 "root@localhost: " (不是 "localhost: ")
    echo ">>> 等待 MySQL 启动并生成临时密码..."
    mysqlpasswd=""
    for i in 1 2 3 4 5 6 7 8 9 10; do
        mysqlpasswd=$(grep 'temporary password' /var/log/mysqld.log 2>/dev/null | awk -F 'root@localhost: ' '{print $2}' | tail -1)
        [ -n "$mysqlpasswd" ] && break
        sleep 2
    done
    if [ -z "$mysqlpasswd" ]; then
        echo "[FAIL] 找不到 MySQL 临时密码 (等待 20s 后)，查看 /var/log/mysqld.log"
        exit 1
    fi
else
    mysqlpasswd=""
fi

# =============================================================================
# 9. MySQL 密码 (带强度校验) — 用 .my.cnf 临时文件传避免命令行注入
# =============================================================================
mysqlpass1=""
while true; do
    read -s -p "请输入新的 MySQL root 密码 (>=12位含大小写+数字, 不含单引号): " mysqlpass1; echo
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

OGS_MYCNF=/tmp/.ogsmysql.cnf
cat > "$OGS_MYCNF" <<EOF
[client]
user=root
password='$mysqlpass1'
EOF
chmod 600 "$OGS_MYCNF"

# 改 root 密码
if [ -n "$mysqlpasswd" ]; then
    mysql --defaults-file="$OGS_MYCNF" --connect-expired-password -u root <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '$mysqlpass1';
SQL
fi

# =============================================================================
# 10. 建库 + 建表
# =============================================================================
echo ">>> 创建 orange 数据库..."
mysql --defaults-file="$OGS_MYCNF" \
    -e "CREATE DATABASE IF NOT EXISTS orange DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_general_ci;"

echo ">>> 导入完整 SQL (含全部业务表 + 关联表 + 初始化种子数据)..."
mysql --defaults-file="$OGS_MYCNF" orange \
    < "${installdir}/backend/mysqldir/orange.sql"

echo ">>> 数据库列表："
mysql --defaults-file="$OGS_MYCNF" -e "SHOW DATABASES;"
mysql --defaults-file="$OGS_MYCNF" -e "USE orange; SHOW TABLES;"

# =============================================================================
# 10.5 创建业务账号 app_user (DEPLOY-AUDIT P0-5)
#   旧版从未创建 app_user, 却把 root 密码写进 .env 的业务口令 → 后端必然
#   Access denied。此处创建独立业务账号 + 独立随机口令, 只授权 orange 库。
# =============================================================================
echo ">>> 创建业务数据库账号 app_user..."
apppass=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
mysql --defaults-file="$OGS_MYCNF" <<SQL
CREATE USER IF NOT EXISTS 'app_user'@'localhost' IDENTIFIED BY '${apppass}';
ALTER USER 'app_user'@'localhost' IDENTIFIED BY '${apppass}';
GRANT ALL PRIVILEGES ON orange.* TO 'app_user'@'localhost';
FLUSH PRIVILEGES;
SQL
echo ">>> app_user 已创建 (口令已写入 backend/.env, 请勿外泄)"

# =============================================================================
# 11. .env 配置
# =============================================================================
echo ""
echo "============================================================"
echo "  现在配置 .env"
echo "============================================================"

if [ ! -f "${installdir}/backend/.env" ]; then
    cp "${installdir}/backend/.env.example" "${installdir}/backend/.env"
    chmod 600 "${installdir}/backend/.env"
fi

# ⓘ 修复: OGS_MYSQL_USER 保持 app_user 业务账号, OGS_REDIS_PORT = 6379 标准端口
sed -i "s|^OGS_MYSQL_HOST=.*|OGS_MYSQL_HOST=127.0.0.1|" "${installdir}/backend/.env"
sed -i "s|^OGS_MYSQL_PORT=.*|OGS_MYSQL_PORT=3306|" "${installdir}/backend/.env"
sed -i "s|^OGS_MYSQL_DBNAME=.*|OGS_MYSQL_DBNAME=orange|" "${installdir}/backend/.env"
# DEPLOY-AUDIT P0-5: 写业务账号口令 (apppass), 不再误写 root 口令
sed -i "s|^OGS_MYSQL_USER=.*|OGS_MYSQL_USER=app_user|" "${installdir}/backend/.env"
sed -i "s|^OGS_MYSQL_PASSWORD=.*|OGS_MYSQL_PASSWORD=${apppass}|" "${installdir}/backend/.env"
sed -i "s|^OGS_REDIS_HOST=.*|OGS_REDIS_HOST=127.0.0.1|" "${installdir}/backend/.env"
sed -i "s|^OGS_REDIS_PORT=.*|OGS_REDIS_PORT=6379|" "${installdir}/backend/.env"

echo ""
echo "请编辑 ${installdir}/backend/.env，至少修改以下 2 项："
echo "  OGS_FLASK_SECRET_KEY  (生产必填，python -c \"import secrets; print(secrets.token_urlsafe(48))\")"
echo "  OGS_FERNET_KEYS        (R1-22 推荐: 单 key 起步，支持 rotation)"
echo "                           python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
echo "                           多 key rotation: OGS_FERNET_KEYS=<key2>,<key1>  (新 key 在前)"
echo ""

read -p "
----------------
是否启用邮箱功能
----------------
1. 启用
2. 禁用
-----------
请输入数字:" mailwork
if [ "$mailwork" = "1" ]; then
    read -p "请输入您的邮箱账号: " mailzh
    read -p "请输入您的邮箱smtp授权码: " mailstmppasswd
    read -p "请输入您的邮箱smtp域名: " mailstmpdomain
    sed -i "s|^OGS_MAIL_USER=.*|OGS_MAIL_USER=${mailzh}|" "${installdir}/backend/.env"
    sed -i "s|^OGS_MAIL_PASSWORD=.*|OGS_MAIL_PASSWORD=${mailstmppasswd}|" "${installdir}/backend/.env"
    sed -i "s|^OGS_MAIL_SMTP=.*|OGS_MAIL_SMTP=${mailstmpdomain}|" "${installdir}/backend/.env"
fi

rm -f "$OGS_MYCNF"

# =============================================================================
# 12. 数据迁移
#   DEPLOY-AUDIT P1-6: 迁移依赖 OGS_FLASK_SECRET_KEY 等已配置 (config.py
#   fail-fast), 旧版在提示用户填密钥后未等待就执行, 必抛 RuntimeError 且被
#   tee 吞掉退出码。改为等待用户确认配置完成后执行, 并检查退出码。
# =============================================================================
echo ''
read -p ">>> 请先按上方提示编辑 backend/.env 填好 SECRET_KEY / FERNET_KEYS, 完成后按回车继续执行数据迁移..." _confirm
cd "${installdir}/backend"
pythondir=$(which python3)
if "$pythondir" -m app.tools.migrate_comma_to_junction 2>&1 | tee /tmp/migrate_comma.log \
    && [ "${PIPESTATUS[0]}" -eq 0 ]; then
    echo '>>> 迁移完成'
else
    echo '!!! 数据迁移失败, 详见 /tmp/migrate_comma.log (常见原因: .env 密钥未配置或数据库不可达)' >&2
    exit 1
fi

# =============================================================================
# 13. 收尾
# =============================================================================
echo ''
echo '============================================================'
echo '  部署完成 (RHEL 系 + MySQL 8.0)'
echo '  初始化账号: admin / admin'
echo '  业务端口: 28000'
echo '  业务账号: app_user'
echo '  源码 sha256: ' $(cat /tmp/ogs_source_sha256.txt | awk '{print $1}')
echo ''
echo '  推荐启动方式:'
echo '    生产: supervisorctl start orange:*'
echo '    开发: ./ops/start.sh start'
echo '============================================================'
