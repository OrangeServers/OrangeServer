# OrangeServer 统一升级流程

这是项目内数据库升级、验证和回滚的唯一操作入口。README、配置说明和组件
README 只链接本文，不重复维护迁移命令。

> 不同历史版本可能需要更早的迁移。先阅读当前版本的 `CHANGELOG.md`，确认起始
> 版本，不要跳过未执行的 `backend/mysqldir/rev*.sql`。

## 1. 升级前

1. 安排维护窗口，通知用户 WebSSH 和批量任务可能中断。
2. 记录当前代码版本、镜像标签和运行中的容器。
3. 确认 `.env`、Fernet keys 和外部数据库配置已安全备份。
4. 备份 MySQL、Redis 持久化数据和 `OGS_DATA_DIR`。
5. 在副本或测试环境验证迁移。

MySQL 备份示例：

```bash
mysqldump \
  --single-transaction \
  --routines \
  --triggers \
  -h <mysql-host> -u <mysql-user> -p \
  <database> > orangeserver-before-upgrade.sql
```

确认备份非空并可读取：

```bash
test -s orangeserver-before-upgrade.sql
```

备份文件包含凭据和业务数据，应加密保存且禁止提交 Git。

## 2. 准备目标版本

先判断安装类型：

- 源码检出目录包含 `.git/`，使用“源码检出”路径；
- 一键 Release 安装目录包含 `.orangeserver-version` 且没有 `.git/`，使用
  “Release bundle”路径。不要对这种目录执行 `git pull`，也不要重新运行首次安装
  引导器；引导器会为保护已有数据而拒绝覆盖。

### 源码检出

```bash
git fetch --tags origin
git switch --detach vX.Y.Z
```

Docker Compose 源码部署需要构建目标镜像：

```bash
make build-backend
```

### Release bundle 安装

下载目标版本的 bundle 和校验文件，解压到与当前安装目录同一文件系统的暂存目录。
下面命令不会覆盖当前安装；把 `vX.Y.Z` 替换为目标稳定版本。安装时使用过
`--install-dir` 的实例，也必须把每个代码块开头的 `install_dir` 改为同一个实际
路径：

```bash
target_version=vX.Y.Z
install_dir=/opt/orangeserver
next_dir="${install_dir}-next-${target_version}"
work_dir="$(mktemp -d)"

test -f "${install_dir}/.orangeserver-version"
test ! -e "${next_dir}"

curl -fsSL --retry 3 -o "${work_dir}/bundle.tar.gz" \
  "https://github.com/OrangeServers/OrangeServer/releases/download/${target_version}/orangeserver-deploy-${target_version}.tar.gz"
curl -fsSL --retry 3 -o "${work_dir}/bundle.tar.gz.sha256" \
  "https://github.com/OrangeServers/OrangeServer/releases/download/${target_version}/orangeserver-deploy-${target_version}.tar.gz.sha256"

# 校验文件记录的是正式文件名，先恢复该文件名再验证。
mv "${work_dir}/bundle.tar.gz" \
  "${work_dir}/orangeserver-deploy-${target_version}.tar.gz"
mv "${work_dir}/bundle.tar.gz.sha256" \
  "${work_dir}/orangeserver-deploy-${target_version}.tar.gz.sha256"
(cd "${work_dir}" && sha256sum -c "orangeserver-deploy-${target_version}.tar.gz.sha256")

tar -C "${work_dir}" -xzf \
  "${work_dir}/orangeserver-deploy-${target_version}.tar.gz"
mv "${work_dir}/orangeserver" "${next_dir}"
cp -p "${install_dir}/.env" "${next_dir}/.env"
cp -p "${install_dir}/backend/.env" "${next_dir}/backend/.env"
sed -i "s/^OGS_BACKEND_TAG=.*/OGS_BACKEND_TAG=${target_version}/" "${next_dir}/.env"
printf '%s\n' "${target_version}" > "${next_dir}/.orangeserver-version"
chmod 600 "${next_dir}/.env" "${next_dir}/backend/.env" \
  "${next_dir}/.orangeserver-version"
rm -rf -- "${work_dir}"
```


M2 标准 bundled 栈把业务 Redis 与自治 Redis 合并为 Redis 8，并继续挂载旧
`autonomy-redis-data` 卷以保留 checkpoint。业务会话和缓存可重建，不迁移旧业务 Redis
卷。启动前把两份环境配置统一到同一个 Redis 密码（幂等，可重复执行）：

```bash
redis_password="$(openssl rand -hex 24)"
set_env_key() {
    local file="$1" key="$2" value="$3"
    if grep -q "^${key}=" "$file"; then
        sed -i "s|^${key}=.*$|${key}=${value}|" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}
for env_file in "${next_dir}/.env" "${next_dir}/backend/.env"; do
    set_env_key "$env_file" OGS_REDIS_HOST redis
    set_env_key "$env_file" OGS_REDIS_DB 2
    set_env_key "$env_file" OGS_REDIS_PASSWORD "$redis_password"
    set_env_key "$env_file" OGS_AI_AUTONOMY_ENABLED true
    set_env_key "$env_file" OGS_AI_AUTONOMY_REDIS_HOST redis
    set_env_key "$env_file" OGS_AI_AUTONOMY_REDIS_PORT 6379
    set_env_key "$env_file" OGS_AI_AUTONOMY_REDIS_PASSWORD "$redis_password"
    set_env_key "$env_file" OGS_AUTONOMY_WORKER_CONCURRENCY 2
done
chmod 600 "${next_dir}/.env" "${next_dir}/backend/.env"
```

统一 Redis 镜像默认为 `redis:8.10.0`（包含 checkpoint/RAG 所需的 Search 和 JSON）。无法访问 Docker Hub 的网络先把该镜像
转存到内部仓库，再在根 `.env` 设置 `OGS_REDIS_IMAGE` 指向转存地址。

目标 bundle 包含 `CHANGELOG.md`、本文和数据库迁移 SQL。先比较当前
`.orangeserver-version` 与目标版本说明，只执行跨越版本所要求的迁移。

不要先删除旧镜像。保留一个已验证的应用镜像作为回滚点；具体标签和环境路径只
记录在部署方私有运维系统中。

## 3. 按顺序执行迁移

先停止会写业务数据的前后端，保持 bundled MySQL/Redis 运行：

```bash
install_dir=/opt/orangeserver
cd "${install_dir}"
docker compose --env-file .env -f deploy/docker-compose.yml \
  --profile bundled stop app worker
```

对尚未启用 AI Provider 和受控诊断的旧实例，依次执行：

```bash
mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev48_ai_provider.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev49_ai_context_window.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev50_ai_diagnostics.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev51_settings_language.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev52_smtp_settings.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev53_ai_autonomy_baseline.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev54_ai_autonomy_lease.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev55_ai_autonomy_custom_profile.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev56_ai_autonomy_evidence.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev57_ai_ops_trigger.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev58_ai_knowledge.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev59_ai_autonomy_conclusion.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev60_admin_all_permissions.sql

mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < backend/mysqldir/rev61_ai_monitoring.sql
```

顺序不可颠倒：rev49 修改 rev48 创建的 `t_ai_provider`，rev50 增加受控诊断的
Run、事件、加密证据和报告表，rev51 为 `t_settings` 增加界面语言字段（默认
zh-CN，存量行为不变），rev52 增加由管理界面维护的 SMTP 配置字段，授权码仅
保存 Fernet 密文，rev53 为 AI 自治（M1/S1）增加资产环境列与 Run/Step/事件/
产物四张表；rev54 为自治 Run 表追加 Worker 租约、一次性 fencing token、心跳与
图版本列（M1/S2），rev55 为自治 Run 表追加可选的自定义权限档案列，rev56 增加
脱敏 Evidence 引用表（M1/S3），rev57 为 Run 增加触发类型、幂等引用和脱敏触发
摘要（M2/S1），rev58 增加 embedding 单例配置和已审核知识文档表（M2/S2），
rev59 为 Run 增加结构化结论详情（M2），rev60 幂等回填存量管理员到现有
“所有权限”规则，避免自定义管理员名仍沿用旧 `admin` 权限关系；
rev61 增加只读监控数据源与显式资产身份映射表，监控 Token 只保存 Fernet 密文；
Redis 向量是可重建数据，不需要数据库 chunk 表。标准 bundled 栈会启动统一 Redis 8
与 Worker，自治能力默认可用。
embedding 模型、维度或向量索引布局变化时，知识库会自动显示“索引待更新”；管理员
执行一次知识库重建后才恢复检索，旧布局不会被误报为可用。
各脚本针对其自身变更设计了重复执行保护，但重复运行前仍应
确认输出和目标数据库正确。

rev54 还在数据库层保证同一资产最多存在一个活动自治 Run。若曾在实验性集成版本中
并发写入重复的活动 Run，唯一索引创建会安全失败并列出冲突资产；迁移不会自动删除、
结束或选择其中任何一条 Run。请先核对每条 Run 的实际状态并人工处置，再重新执行
rev54。终态 `completed`、`failed`、`cancelled`、`expired` 的历史 Run 不受此限制。

如果你的起始版本尚未完成旧授权关系迁移，还需要在对应发布说明指导下运行：

```bash
cd backend
python -m app.tools.migrate_comma_to_junction
```

上面的 Python 迁移只适用于包含完整后端源码的检出目录；Release bundle 不含
`backend/app`，不要在 bundle 安装上运行。需要跨越该旧迁移的 bundle 用户应先在
副本环境验证并使用对应版本发布说明提供的容器化迁移入口。

不要在不了解起始 schema 的情况下批量运行全部历史 SQL。

bundled MySQL 可使用下面的形式执行目标 bundle 中明确要求的单个迁移；不要把
通配符直接交给生产数据库：

```bash
install_dir=/opt/orangeserver
next_dir="${install_dir}-next-vX.Y.Z"
docker compose --env-file "${install_dir}/.env" \
  -f "${install_dir}/deploy/docker-compose.yml" \
  --profile bundled exec -T mysql \
  sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root "$MYSQL_DATABASE"' \
  < "${next_dir}/backend/mysqldir/<required-migration>.sql"
```

## 4. 验证数据库

```sql
SHOW TABLES LIKE 't_ai_provider';
SHOW COLUMNS FROM t_ai_provider LIKE 'context_window_tokens';
SHOW TABLES LIKE 't_ai_diagnostic_run';
SHOW TABLES LIKE 't_ai_diagnostic_event';
SHOW COLUMNS FROM t_settings LIKE 'mail_smtp_host';
SHOW COLUMNS FROM t_settings LIKE 'mail_password_encrypted';
SHOW TABLES LIKE 't_ai_diagnostic_evidence';
SHOW TABLES LIKE 't_ai_diagnostic_report';
SHOW COLUMNS FROM t_ai_autonomous_run LIKE 'active_host_id';
SHOW INDEX FROM t_ai_autonomous_run
WHERE Key_name = 'uq_ai_auto_run_active_host';
SHOW COLUMNS FROM t_ai_autonomous_run LIKE 'custom_profile_json';
SHOW COLUMNS FROM t_ai_autonomous_run LIKE 'conclusion_json';
SHOW TABLES LIKE 't_ai_autonomous_evidence';
SHOW TABLES LIKE 't_ai_embedding_config';
SHOW TABLES LIKE 't_ai_knowledge_document';
SHOW TABLES LIKE 't_ai_monitoring_source';
SHOW TABLES LIKE 't_ai_monitoring_host_mapping';

SELECT
    provider_code,
    model,
    enabled,
    is_default,
    context_window_tokens
FROM t_ai_provider
ORDER BY provider_code;

SELECT admin_user.name AS admin_without_all_permissions
FROM t_acc_user AS admin_user
LEFT JOIN t_auth_host AS all_auth
  ON all_auth.name = '所有权限' AND all_auth.is_deleted = 0
LEFT JOIN t_auth_host_user AS binding
  ON binding.auth_id = all_auth.id
 AND binding.user_name = admin_user.name
WHERE admin_user.usrole = 'admin' AND admin_user.is_deleted = 0
GROUP BY admin_user.name
HAVING COUNT(binding.id) = 0;
```

预期：

- 存在 `t_ai_provider`；
- 存在 `context_window_tokens`；
- 旧 Provider 默认回填 `262144`；
- 只有管理员明确确认模型支持时才改为 `1048576`；
- 存在 4 张 `t_ai_diagnostic_*` 表及其外键；
- `t_ai_autonomous_run.active_host_id` 是生成列，且存在
  `uq_ai_auto_run_active_host` 唯一索引；
- `t_ai_autonomous_run.custom_profile_json` 和
  `t_ai_autonomous_run.conclusion_json` 存在；
  `t_ai_autonomous_evidence` 存在；Evidence 的 `trusted` 默认值为 `0`；
- `t_ai_embedding_config` 只有默认本地模型配置，且
  `t_ai_knowledge_document` 已创建；
- `t_ai_monitoring_source` 与 `t_ai_monitoring_host_mapping` 已创建；
- `admin_without_all_permissions` 查询返回空结果；
- 查询结果中不应出现明文 API Key。

## 5. 启动和冒烟验证

Docker Compose：

```bash
# Release bundle 安装：迁移成功后原子切换目录，保留旧目录作为应用回滚点。
target_version=vX.Y.Z
install_dir=/opt/orangeserver
old_version="$(cat "${install_dir}/.orangeserver-version")"
next_dir="${install_dir}-next-${target_version}"
rollback_dir="${install_dir}-rollback-${old_version}"
test ! -e "${rollback_dir}"
mv "${install_dir}" "${rollback_dir}"
mv "${next_dir}" "${install_dir}"

cd "${install_dir}"
make docker-up-image
make docker-ps

# 源码检出安装仍使用：
# make docker-up
```

启动命令会带 `--remove-orphans`，因此旧版的 `frontend`、`autonomy-worker` 和
`autonomy-redis` 容器会在新四容器栈接管后被移除；命名卷不会被删除。

基础检查：

```bash
make docker-health
```

浏览器验证：

1. 管理员登录和退出正常；
2. 资产列表、授权、批量命令和审计页面可打开；
3. WebSSH 能建立和关闭会话；
4. “系统设置 → AI 模型服务”显示 256K/1M 能力；
5. “运维知识库”可新建 Runbook、重建索引并显示 `ready`；
6. 已配置 API Key 显示掩码状态而非明文或空配置；
7. Provider Tool Calling 测试成功；
8. 新建 256K 会话，执行一次只读资产查询；
9. 对测试资产运行一个固定只读诊断，检查进度、证据引用和报告；
10. 创建批量命令计划，检查预览后取消，不在升级冒烟中执行破坏性命令。
11. 在“系统设置 → AI 模型服务”新增一个测试监控来源，测试连接并为测试资产确认映射；
12. 在 AI 运维对话查询该资产最近监控数据，确认返回来源证据且没有触发远程写操作。

## 6. 回滚

应用回滚和数据库回滚是两件事：

- 应用启动失败但 schema 向后兼容时，可先恢复上一镜像。
- 如果旧应用不能识别新 schema，停止写入后恢复升级前 MySQL 备份。
- rev48/rev49/rev50/rev53/rev54/rev55/rev56/rev57/rev58/rev59/rev60/rev61 不提供自动 down migration；
  不要在生产手工删除列或表。rev58 的 Redis 向量可从 MySQL 文档重建。
- 恢复数据库前先保留失败现场的日志和当前数据库快照。
- Release bundle 安装可停止前后端，将当前安装目录移回
  `<安装目录>-next-<failed-version>`，再把保留的
  `<安装目录>-rollback-<old-version>` 恢复为原安装目录，最后执行旧目录中的
  `make docker-up-image`。若迁移不向后兼容，必须同时恢复升级前数据库备份。

MySQL 恢复示例：

```bash
mysql -h <mysql-host> -u <mysql-user> -p <database> \
  < orangeserver-before-upgrade.sql
```

恢复后重新运行健康检查和关键浏览器流程。

## 7. 升级后

- 将实际版本、迁移结果、验证结果和回滚点写入私有变更记录。
- 删除包含秘密的临时命令历史和未加密备份副本。
- 观察后端错误、Redis、MySQL、SSH 和 Provider 请求。
- 不要把测试机地址、部署目录、Cookie、密钥或真实资产截图添加到 Issue 或 Git。
