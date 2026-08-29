from datetime import datetime

from sqlalchemy import Computed
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.db.settings import db


def _utcnow(*args, **kwargs):
    """R2-5 (REV45-H8): UTC 时间生成器, 供 SQLAlchemy default/onupdate 调用.

    SQLAlchemy 调用 default 时会传入 ctx 参数 (Insert/Update context),
    我们忽略它, 直接返回 utcnow. 用 *args, **kwargs 同时兼容:
      - SQLAlchemy ORM default 上下文: _utcnow(ctx)
      - 业务方手动调用: _utcnow()
    """
    return datetime.utcnow()


# =============================================================================
# R2-5 (REV45-H8): TimestampMixin - 自动管理 created_at / updated_at
# =============================================================================
# 问题: 业务表无 created_at / updated_at 字段, 无法审计:
#   - 何时添加的资产/账号/SSH 用户/cron
#   - 何时被修改 (e.g. 密码、配置)
#   - 谁改的 (后续可加 updated_by)
# 修复: mixin + ORM 自动管理:
#   - created_at: INSERT 时自动 default=utcnow (UTC, 避免时区混淆)
#   - updated_at: INSERT default, UPDATE onupdate 自动刷新
#   - 两个字段都加 index: 按时间范围查 / 排序 频繁
class TimestampMixin:
    """R2-5 (REV45-H8): 自动管理时间戳的 mixin.

    用法:
        class t_xxx(db.Model, TimestampMixin):
            ...

    created_at: 行创建时间 (UTC, 服务端时间), 不可修改
    updated_at: 行最后修改时间 (UTC), UPDATE 时自动刷新

    注意:
        - MySQL DATETIME 不带时区, 业务读出后按需转本地时区
        - 不用 server_default/db.func.now(): UTC 跨时区统一, 服务端/应用端时间一致
        - onupdate=_utcnow 在 ORM 层处理 (db.update 不触发, 仅 ORM 更新)
    """
    created_at = db.Column(
        db.DateTime, nullable=False,
        default=_utcnow,
        index=True,
    )
    updated_at = db.Column(
        db.DateTime, nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        index=True,
    )


# =============================================================================
# REV47-M6: SoftDeleteMixin - 业务实体软删除
# =============================================================================
# 背景: 业务实体 (host/sys_user/acc_user/group/acc_group/auth_host/cron) 删函数
#   之前是 db.session.delete() 物理删除, 误删/恶意删后无任何恢复手段,
#   审计也只能依赖日志表事后追溯. 现引入软删除, 删除操作只标记 is_deleted=True,
#   业务查询统一走 .filter_by(is_deleted=False) 隐藏软删行.
# 设计:
#   - 7 张业务实体表继承此 mixin: t_host / t_sys_user / t_acc_user / t_group /
#     t_acc_group / t_auth_host / t_cron
#   - 不涉及日志表 (t_login_log / t_command_log / t_cz_log) - append-only, 不删
#   - 不涉及统计表 (t_line_chart) - 按日清理, 软删无意义
#   - 不涉及 join 表 (t_cron_host / t_cron_group / t_auth_host_* 4 张) - FK
#     CASCADE 自动清理, 加 is_deleted 反而破坏 CASCADE 语义
#   - 不涉及 t_settings - 1 行配置, 无"删"概念
# 业务层:
#   - 删函数: obj.is_deleted = True; db.session.commit() (不再 db.session.delete)
#   - list/get 路径: .filter_by(..., is_deleted=False)
#   - 注册同名复用: 主动 .filter_by(name=..., is_deleted=True) 找软删记录, 复用其 name
# 同步 DDL:
#   - 7 张表各 ADD COLUMN is_deleted TINYINT(1) NOT NULL DEFAULT 0
#   - 7 张表各 ADD INDEX idx_xxx_is_deleted (is_deleted)
#   - 幂等 DROP COLUMN IF EXISTS 模式 (rev47_m6_soft_delete.sql)
class SoftDeleteMixin:
    """REV47-M6: 业务实体软删除 mixin.

    用法:
        class t_xxx(db.Model, TimestampMixin, SoftDeleteMixin):
            ...

    is_deleted: 软删除标志 (False=正常, True=已软删)
    """
    is_deleted = db.Column(
        db.BOOLEAN, nullable=False,
        default=False, server_default='0',
        index=True,
    )


class t_host(db.Model, TimestampMixin, SoftDeleteMixin):
    __tablename__ = 't_host'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    alias = db.Column(db.String(25), nullable=False, unique=True, index=True)
    # REVIEW-10-P1-4: host_ip 16 -> 45, 业务走 IPv6 (最长 45 字符含 zone id)。同时加 index (主机按 IP 查频繁)。
    host_ip = db.Column(db.VARCHAR(45), nullable=False, index=True)
    host_port = db.Column(db.INT, nullable=False)
    # REV45-H3: group 长度 20 -> 25 (匹配 t_group.name), 加 FK -> t_group.name
    #   背景: 长度不一致会让攻击者构造 20 字符 group 写入但 t_group.name 限制 25, 改名 group 时孤儿 host 不会被察觉
    #   修复: FK -> t_group.name ondelete=SET NULL (删组时把 host.group 置 NULL, 不误删主机)
    #   同步 DDL: ALTER TABLE t_host MODIFY `group` VARCHAR(25);
    #             ALTER TABLE t_host ADD CONSTRAINT fk_host_group FOREIGN KEY (`group`) REFERENCES t_group(name) ON DELETE SET NULL;
    group = db.Column(
        db.String(25),
        db.ForeignKey(
            't_group.name',
            ondelete='SET NULL',
            onupdate='CASCADE',
        ),
        nullable=True,
    )
    # M1/S1: AI 自治环境分级 production|staging|lab，默认 production。
    #   只有管理员可以通过 /ai/autonomy/hosts/<id>/environment 修改；
    #   名为 lab 的普通资产组不授予任何自治能力。
    #   同步 DDL: ALTER TABLE t_host ADD COLUMN ai_environment VARCHAR(10)
    #             NOT NULL DEFAULT 'production';
    ai_environment = db.Column(
        db.String(10), nullable=False,
        default='production', server_default='production',
    )


class t_sys_user(db.Model, TimestampMixin, SoftDeleteMixin):
    __tablename__ = 't_sys_user'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    # REV47-M5: alias 30 -> 24 (匹配 t_acc_user.alias, 统一 user name 长度)
    #   背景: t_sys_user.alias=30, t_acc_user.alias=24 长度不一致
    #         业务可构造 24-30 字符的 sys_user alias 但 t_acc_user.alias 限制 24,
    #         跨表 join/查询时类型/长度差异会引发隐式截断
    #   修复: t_sys_user.alias 30 -> 24, 同步 t_auth_host_sys_user.sys_user_alias
    #   同步 DDL:
    #     ALTER TABLE t_sys_user MODIFY alias VARCHAR(24);
    #     ALTER TABLE t_auth_host_sys_user MODIFY sys_user_alias VARCHAR(24);
    #   注意: 已有 alias 长度 > 24 的数据时 ALTER 会失败, 需先清理
    alias = db.Column(db.String(24), nullable=False)
    host_user = db.Column(db.String(25), nullable=False)
    # host_password 存储 Fernet 密文（原 base64 存储已废弃，通过透明迁移逐步升级）。
    # Fernet 输出 = 88 字节 base64（b'gAAAAAB...' 开头的版本号 + 密文 + HMAC）。
    # 设 String(512) 以应对未来更长的密钥/编码变更。生产部署需执行：
    #   ALTER TABLE t_sys_user MODIFY host_password VARCHAR(512);
    host_password = db.Column(db.String(512), nullable=True)
    host_key = db.Column(db.String(255), nullable=True)
    agreement = db.Column(db.String(10), nullable=False)
    remarks = db.Column(db.String(30), nullable=True)


class t_acc_user(db.Model, TimestampMixin, SoftDeleteMixin):
    __tablename__ = 't_acc_user'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    alias = db.Column(db.String(24), nullable=False)
    # REV45-H1/H2: name 加 unique 约束, 与 mail 一致
    #   背景: AccUserUpdate 改名时可改成已存在的 name (REV41 H2), 业务层虽然校验,
    #     但 ORM/DB 无 unique 约束是根因 - 业务校验绕过即可写入重复 name.
    #   修复: unique=True + index=True, DB 层强制防重复.
    #   同步 DDL: ALTER TABLE t_acc_user ADD UNIQUE INDEX uq_t_acc_user_name (name);
    #   注意: 已存在重复 name 数据时 ALTER 会失败, 需先清理 (运维脚本).
    name = db.Column(db.String(24), nullable=False, unique=True, index=True)
    # REVIEW-10-P0-1: 同步生产 DB ALTER (VARCHAR(255)),
    #   避免 ORM 截断 bcrypt hash (固定 60 字符)。
    #   历史: HIGH-8 已 ALTER 生产 DB 为 255 (fix_acc_user_password_schema.py),
    #   但 ORM 未跟进 → 新注册/重置密码的写入仍会被 String(24) 截断,登录必失败。
    password = db.Column(db.String(255), nullable=False)
    usrole = db.Column(db.String(10), nullable=False)
    # REV16 P2-4/MED-3: mail 字段 24 -> 128 + unique index
    #   背景: 真实邮箱常超出 24 字符 (如 `long.user.name+tag@subdomain.company.com`),
    #     原 ORM String(24) 会静默截断, 收件地址被改成不存在的邮箱, 验证码永远收不到。
    #   修复: 128 足以覆盖所有主流邮箱格式 (RFC 5321 上限 254), 加 unique index 防重注册。
    #   同步 DDL: `ALTER TABLE t_acc_user MODIFY mail VARCHAR(128); ALTER TABLE t_acc_user ADD UNIQUE INDEX uq_t_acc_user_mail (mail);`
    mail = db.Column(db.String(128), nullable=False, unique=True)
    group = db.Column(db.String(24), nullable=False)
    remarks = db.Column(db.String(30), nullable=True)
    # R2-6 (REV45-H9): password_version 显式记录算法版本
    #   常量定义在 app/tools/basesec.py (PWD_VERSION_LEGACY_BASE64=1, PWD_VERSION_BCRYPT_1=2)
    #   这里写字面量 2 = 当前 bcrypt 默认 (避免 database.py -> basesec.py -> config.py 循环 import)
    #   业务层可统计各版本占比, 决定升级节奏
    #   登录成功后如需 rehash, 调用方应同时更新 password_version = 2
    password_version = db.Column(
        db.INT, nullable=False,
        server_default='2',
        default=2,
    )


class t_group(db.Model, SoftDeleteMixin):
    __tablename__ = 't_group'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    # SETUP-WIZARD: name 是 t_host.group 的 FK 目标，全新库 db.create_all()
    #   需要该列有索引（MySQL 1822）；存量库 create_all 不改表，零迁移风险。
    #   同步 DDL (仅新库需要): ALTER TABLE t_group ADD UNIQUE INDEX uq_t_group_name (name);
    name = db.Column(db.String(25), nullable=False, unique=True, index=True)
    nums = db.Column(db.INT, nullable=False, default=0)
    remarks = db.Column(db.String(30), nullable=True)


class t_acc_group(db.Model, SoftDeleteMixin):
    __tablename__ = 't_acc_group'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    # SETUP-WIZARD: 同上，t_auth_host_user_group 等表的 FK 目标需要索引
    name = db.Column(db.String(25), nullable=False, unique=True, index=True)
    nums = db.Column(db.INT, nullable=False, default=0)
    remarks = db.Column(db.String(30), nullable=True)


class t_login_log(db.Model):
    __tablename__ = 't_login_log'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    # REVIEW-10-P1-2: log_name 30 -> 24 + FK -> t_acc_user.name (匹配 PK 类型)
    # REVIEW-10-P1-3: log_name + log_time 加 index (审计查询频繁)
    log_name = db.Column(db.String(24), db.ForeignKey('t_acc_user.name', ondelete='SET NULL'), nullable=True, index=True)
    # REVIEW-7-P0-4: IP 字段加宽到 45 (IPv6 最长 45 字符 含 zone id), 原 String(20) 会截断/写入失败
    # REVIEW-10-P1-1: 同步 DDL, orange.sql log_*_ip 也已 ALTER 为 45
    log_nw_ip = db.Column(db.String(45), nullable=False)
    log_gw_ip = db.Column(db.String(45), nullable=True)
    log_gw_cs = db.Column(db.String(45), nullable=True)
    # REVIEW-7-P2-3: log_agent 20 -> 255, 多数现代 UA 超 100 字符
    log_agent = db.Column(db.String(255), nullable=False)
    # REVIEW-10-P1-5: ORM String(255) 同步 DDL 调整 (DDL 原本 varchar(10))
    log_status = db.Column(db.String(255), nullable=False)
    log_reason = db.Column(db.String(30), nullable=True)
    # REV20-P2-4-LOW-7: db.TIMESTAMP -> db.DateTime, 解决 2038 问题 (32-bit Unix ts 溢出)
    #   SQL DDL (orange.sql:439) 已用 datetime, 仅 ORM 不同步
    #   迁移: ALTER TABLE t_login_log MODIFY log_time DATETIME NOT NULL
    log_time = db.Column(db.DateTime, nullable=False, index=True)
    # REV47-M9: 加 session_id / csrf_nonce 字段, 用于安全审计
    #   背景: 登录事件仅记 log_name/IP/UA, 缺少 session 关联 + CSRF 验证证据
    #         1) session_id: 关联 flask session (登录后 session 标识), 用于
    #            事后追溯 "那次登录对应的 session 在什么时间被复用/失效"
    #         2) csrf_nonce: CSRF token nonce (登录表单提交时验证), 用于
    #            复现 "是否被 CSRF 攻击伪造请求" 排查
    #   字段 nullable=True, 历史数据不补, 仅新登录日志填充
    #   同步 DDL:
    #     ALTER TABLE t_login_log ADD COLUMN log_session_id VARCHAR(64) NULL;
    #     ALTER TABLE t_login_log ADD COLUMN log_csrf_nonce VARCHAR(64) NULL;
    log_session_id = db.Column(db.String(64), nullable=True)
    log_csrf_nonce = db.Column(db.String(64), nullable=True)


class t_command_log(db.Model):
    __tablename__ = 't_command_log'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    # REVIEW-10-P1-2: log_name FK -> t_acc_user.name (30 -> 24 匹配 PK)
    # REVIEW-10-P1-3: log_name + log_time 加 index
    log_name = db.Column(db.String(24), db.ForeignKey('t_acc_user.name', ondelete='SET NULL'), nullable=True, index=True)
    log_type = db.Column(db.String(30), nullable=False)
    log_info = db.Column(db.String(255), nullable=False)
    # REVIEW-10-P1-2: log_host -> t_host.alias FK (30 足以覆盖 hostname)
    log_host = db.Column(db.String(30), db.ForeignKey('t_host.alias', ondelete='SET NULL'), nullable=True, index=True)
    # REV45-H10 (R2-6-4): log_status 10 -> 32, 与 audlog ComToolsLog._FIELDS 一致
    log_status = db.Column(db.String(32), nullable=False)
    log_reason = db.Column(db.String(255), nullable=True)
    # REV20-P2-4-LOW-7: TIMESTAMP -> DateTime
    log_time = db.Column(db.DateTime, nullable=False, index=True)
    # REV47-M10 (R2-3 P2): 加 exit_code / duration_ms 字段, 用于命令执行结果分析
    #   背景: t_command_log 仅有 log_status(成功/失败) 无具体退出码与耗时,
    #         运维复盘 "哪些 SSH 命令高频失败/超时" 时只能看 status 字符串,
    #         无法定位 (a) 进程退出码 (b) 实际耗时分布
    #   修复:
    #     - log_exit_code: INT, 进程退出码 (0=成功, 1-255=失败, -1=超时/未执行)
    #     - log_duration_ms: INT, 命令执行耗时 (毫秒, 0=未执行, -1=超时)
    #   字段 nullable=True, 历史数据不补, 仅新执行的命令日志填充
    #   同步 DDL:
    #     ALTER TABLE t_command_log ADD COLUMN log_exit_code INT NULL;
    #     ALTER TABLE t_command_log ADD COLUMN log_duration_ms INT NULL;
    log_exit_code = db.Column(db.INT, nullable=True)
    log_duration_ms = db.Column(db.INT, nullable=True)


class t_cz_log(db.Model):
    __tablename__ = 't_cz_log'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    # REVIEW-10-P1-2: log_name FK -> t_acc_user.name (30 -> 24 匹配 PK)
    # REVIEW-10-P1-3: log_name + log_time 加 index
    log_name = db.Column(db.String(24), db.ForeignKey('t_acc_user.name', ondelete='SET NULL'), nullable=True, index=True)
    log_type = db.Column(db.String(30), nullable=False)
    log_info = db.Column(db.String(255), nullable=False)
    log_details = db.Column(db.String(255), nullable=False)
    # REV45-H10 (R2-6-4): log_status 10 -> 32, 与 audlog CzToolsLog._FIELDS 一致
    #   背景: ORM 列宽 10 但 audlog 截断到 32 字符, 写入时会因 10 字符以外被截断
    #   修法: 加宽到 32, 同步 DDL ALTER TABLE t_cz_log MODIFY log_status VARCHAR(32) NOT NULL
    #   一致性: t_login_log.log_status 已经是 255, t_command_log.log_status 32 (与 t_cz_log 对齐后)
    log_status = db.Column(db.String(32), nullable=False)
    log_reason = db.Column(db.String(255), nullable=True)
    # REV20-P2-4-LOW-7: TIMESTAMP -> DateTime
    log_time = db.Column(db.DateTime, nullable=False, index=True)


class t_auth_host(db.Model, TimestampMixin, SoftDeleteMixin):
    __tablename__ = 't_auth_host'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    name = db.Column(db.String(25), nullable=False)
    remarks = db.Column(db.String(255), nullable=True)


class t_line_chart(db.Model):
    __tablename__ = 't_line_chart'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    login_count = db.Column(db.INT, nullable=False, default=0)
    user_count = db.Column(db.INT, nullable=False, default=0)
    logerr_count = db.Column(db.INT, nullable=False, default=0)
    chart_date = db.Column(db.DATE, nullable=False)
    # REV47-M1: 加 log_name 字段, 支持按用户维度统计 (与 t_acc_user.name FK)
    #   - aggregate 行 (按日汇总): log_name = NULL
    #   - per-user 行 (单用户统计): log_name = '<user name>'
    #   原 schema 仅按日汇总, 无法按用户维度做趋势图
    #   修复: 字段 nullable + FK -> t_acc_user.name (ondelete=SET NULL, 删用户不级联)
    #   同步 DDL: ALTER TABLE t_line_chart ADD COLUMN log_name VARCHAR(24) NULL,
    #             ADD CONSTRAINT fk_lc_log_name FOREIGN KEY (log_name) REFERENCES t_acc_user(name) ON DELETE SET NULL;
    log_name = db.Column(
        db.String(24),
        db.ForeignKey('t_acc_user.name', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )


class t_cron(db.Model, TimestampMixin, SoftDeleteMixin):
    __tablename__ = 't_cron'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    job_name = db.Column(db.String(30), nullable=False, unique=True, index=True)
    job_minute = db.Column(db.String(20), nullable=False)
    job_hour = db.Column(db.String(20), nullable=False)
    job_day = db.Column(db.String(20), nullable=False)
    job_month = db.Column(db.String(20), nullable=False)
    job_week = db.Column(db.String(20), nullable=False)
    # 迁移到 t_cron_host / t_cron_group 后的残留列已删除 (REVIEW-10-P2-5):
    #   - 旧数据在 app/tools/migrate_comma_to_junction.py 一次性迁移,后无写入
    #   - cron.py:460-475 读取路径已转为 t_cron_host/t_cron_group 关联表
    #   - 请求体参数名 'job_hosts' / 'job_groups' 仍保留 (cron.py:189-190) 只作为 list/getlist 输入,ORM 已不持久化
    job_sys_user = db.Column(db.String(255), nullable=False)
    job_command = db.Column(db.String(255), nullable=False)
    job_status = db.Column(db.String(20), nullable=False)
    job_remarks = db.Column(db.String(255), nullable=True)
    # REV45-H7 (R2-4): job_owner 加 FK 约束, 防垃圾字符串 (e.g. SQL注入 payload)
    #   策略: db.ForeignKey('t_acc_user.name', ondelete='SET DEFAULT') + default='system'
    #   - 写入路径: current_user(=t_acc_user.name) 必须存在于 t_acc_user 表
    #   - 删除路径: 删 acc_user 时, 他创建的 cron 自动重置 owner='system'
    #   - system 用户: 是不可删除的内置用户 (其 cron 是历史遗留, 无真实 owner)
    #   - 同步 DDL: ALTER TABLE t_cron ADD CONSTRAINT fk_cron_owner FOREIGN KEY (job_owner)
    #              REFERENCES t_acc_user(name) ON DELETE SET DEFAULT;
    #              ALTER TABLE t_acc_user ADD UNIQUE INDEX uq_t_acc_user_name (name); -- 已有
    job_owner = db.Column(db.String(30),
                          db.ForeignKey('t_acc_user.name',
                                        ondelete='SET DEFAULT'),
                          nullable=False,
                          server_default='system',
                          default='system')


class t_cron_host(db.Model):
    __tablename__ = 't_cron_host'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    cron_id = db.Column(db.INTEGER, db.ForeignKey('t_cron.id', ondelete='CASCADE'), nullable=False)
    # REV45-H6: host_alias 长度 100 -> 25 (匹配 t_host.alias) + FK -> t_host.alias
    #   背景: 关联表字段 > 主表 PK, 无 FK, 删主机时 cron_host 行不级联清理
    #   修复: 长度统一 + FK -> t_host.alias ondelete=CASCADE (删主机时同步清关联表)
    #   同步 DDL: ALTER TABLE t_cron_host MODIFY host_alias VARCHAR(25);
    #             ALTER TABLE t_cron_host ADD CONSTRAINT fk_cron_host_host_alias FOREIGN KEY (host_alias) REFERENCES t_host(alias) ON DELETE CASCADE;
    host_alias = db.Column(db.String(25), db.ForeignKey('t_host.alias', ondelete='CASCADE'), nullable=False)


class t_cron_group(db.Model):
    __tablename__ = 't_cron_group'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    cron_id = db.Column(db.INTEGER, db.ForeignKey('t_cron.id', ondelete='CASCADE'), nullable=False)
    group_name = db.Column(db.String(100), nullable=False)


class t_auth_host_user(db.Model):
    __tablename__ = 't_auth_host_user'
    __table_args__ = (db.UniqueConstraint('auth_id', 'user_name', name='uq_auth_user'),)
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    auth_id = db.Column(db.INTEGER, db.ForeignKey('t_auth_host.id', ondelete='CASCADE'), nullable=False)
    # REV45-H5: user_name 长度 100 -> 24 (匹配 t_acc_user.name PK)
    #   背景: 攻击者可构造 100 字符 user_name 写入但 t_acc_user.name 限制 24, 关联表成为孤儿
    #   修复: 长度与主表 PK 严格一致, 应用层也已校验 (at.py:46)
    #   同步 DDL: ALTER TABLE t_auth_host_user MODIFY user_name VARCHAR(24);
    user_name = db.Column(db.String(24), nullable=False)


class t_auth_host_user_group(db.Model):
    __tablename__ = 't_auth_host_user_group'
    __table_args__ = (db.UniqueConstraint('auth_id', 'group_name', name='uq_auth_user_group'),)
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    auth_id = db.Column(db.INTEGER, db.ForeignKey('t_auth_host.id', ondelete='CASCADE'), nullable=False)
    # REV45-H4/H5: group_name 长度 100 -> 25 (匹配 t_acc_group.name) + 加 FK
    #   背景: 关联表字段 > 主表 PK, 无 FK, 删组时关联表行不级联清理, 应用层靠 query filter
    #   修复: 长度统一 + FK -> t_acc_group.name ondelete=CASCADE (删组时同步清关联表)
    #   同步 DDL: ALTER TABLE t_auth_host_user_group MODIFY group_name VARCHAR(25);
    #             ALTER TABLE t_auth_host_user_group ADD CONSTRAINT fk_ahug_group_name FOREIGN KEY (group_name) REFERENCES t_acc_group(name) ON DELETE CASCADE;
    group_name = db.Column(
        db.String(25),
        db.ForeignKey(
            't_acc_group.name',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
        nullable=False,
    )


class t_auth_host_host_group(db.Model):
    __tablename__ = 't_auth_host_host_group'
    __table_args__ = (db.UniqueConstraint('auth_id', 'group_name', name='uq_auth_host_group'),)
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    auth_id = db.Column(db.INTEGER, db.ForeignKey('t_auth_host.id', ondelete='CASCADE'), nullable=False)
    # REV45-H4/H5: group_name 长度 100 -> 25 (匹配 t_group.name) + 加 FK
    #   背景: 同 t_auth_host_user_group, 关联表字段 > 主表 PK + 无 FK
    #   修复: 长度统一 + FK -> t_group.name ondelete=CASCADE (删组时同步清关联表)
    #   同步 DDL: ALTER TABLE t_auth_host_host_group MODIFY group_name VARCHAR(25);
    #             ALTER TABLE t_auth_host_host_group ADD CONSTRAINT fk_ahhg_group_name FOREIGN KEY (group_name) REFERENCES t_group(name) ON DELETE CASCADE;
    group_name = db.Column(
        db.String(25),
        db.ForeignKey(
            't_group.name',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
        nullable=False,
    )


class t_auth_host_sys_user(db.Model):
    __tablename__ = 't_auth_host_sys_user'
    __table_args__ = (db.UniqueConstraint('auth_id', 'sys_user_alias', name='uq_auth_sys_user'),)
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    auth_id = db.Column(db.INTEGER, db.ForeignKey('t_auth_host.id', ondelete='CASCADE'), nullable=False)
    # REV45-H5: sys_user_alias 长度 100 -> 30 (匹配 t_sys_user.alias)
    #   背景: 关联表字段 > 主表 PK, 攻击者构造 100 字符别名可写入但 t_sys_user.alias 限制 30
    #   修复: 长度与主表 PK 严格一致
    # REV47-M5: 30 -> 24 (再次统一, 匹配 t_acc_user.alias)
    #   同步 DDL: ALTER TABLE t_auth_host_sys_user MODIFY sys_user_alias VARCHAR(24);
    sys_user_alias = db.Column(db.String(24), nullable=False)


class t_ai_provider(db.Model, TimestampMixin):
    """LLM Provider 配置.

    API Key 只保存 Fernet 密文；业务层通过 basesec.encrypt_secret /
    decrypt_secret 读写。一个厂商首版只保留一条配置。
    """
    __tablename__ = 't_ai_provider'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    provider_code = db.Column(
        db.String(32), nullable=False, unique=True, index=True,
    )
    base_url = db.Column(db.String(255), nullable=False)
    model = db.Column(
        db.String(128), nullable=False, default='', server_default='',
    )
    context_window_tokens = db.Column(
        db.INTEGER, nullable=False, default=262144, server_default='262144',
    )
    api_key_ciphertext = db.Column(db.String(1024), nullable=True)
    enabled = db.Column(
        db.BOOLEAN, nullable=False, default=False, server_default='0',
        index=True,
    )
    is_default = db.Column(
        db.BOOLEAN, nullable=False, default=False, server_default='0',
        index=True,
    )
    extra_body_json = db.Column(db.Text, nullable=True)


class t_ai_diagnostic_run(db.Model, TimestampMixin):
    """Durable authoritative snapshot for a controlled diagnostic run."""

    __tablename__ = 't_ai_diagnostic_run'
    id = db.Column(db.String(32), primary_key=True)
    owner = db.Column(db.String(24), nullable=False, index=True)
    conversation_id = db.Column(db.String(32), nullable=True, index=True)
    profile_id = db.Column(db.String(64), nullable=False, index=True)
    profile_name = db.Column(db.String(128), nullable=False)
    status = db.Column(db.String(16), nullable=False, index=True)
    target_count = db.Column(db.INTEGER, nullable=False, default=0)
    success_count = db.Column(db.INTEGER, nullable=False, default=0)
    failed_count = db.Column(db.INTEGER, nullable=False, default=0)
    system_user_id = db.Column(db.INTEGER, nullable=False, index=True)
    system_user_alias = db.Column(db.String(24), nullable=False)
    parameters_json = db.Column(db.Text, nullable=False)
    summary_json = db.Column(db.Text, nullable=False)
    asset_progress_json = db.Column(db.Text, nullable=False)
    latest_event_seq = db.Column(db.INTEGER, nullable=False, default=0)
    cancel_requested = db.Column(
        db.BOOLEAN, nullable=False, default=False, server_default='0',
    )
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    evidence_expires_at = db.Column(db.DateTime, nullable=False, index=True)
    audit_expires_at = db.Column(db.DateTime, nullable=False, index=True)


class t_ai_diagnostic_event(db.Model):
    """Append-only, monotonically sequenced diagnostic event."""

    __tablename__ = 't_ai_diagnostic_event'
    __table_args__ = (
        db.UniqueConstraint(
            'run_id', 'sequence', name='uq_ai_diagnostic_event_sequence',
        ),
    )
    id = db.Column(db.BIGINT, primary_key=True, autoincrement=True)
    run_id = db.Column(
        db.String(32),
        db.ForeignKey('t_ai_diagnostic_run.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    sequence = db.Column(db.INTEGER, nullable=False)
    event_type = db.Column(db.String(32), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)


class t_ai_diagnostic_evidence(db.Model):
    """Encrypted, bounded evidence. Plaintext must never be persisted."""

    __tablename__ = 't_ai_diagnostic_evidence'
    id = db.Column(db.String(32), primary_key=True)
    run_id = db.Column(
        db.String(32),
        db.ForeignKey('t_ai_diagnostic_run.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    target_id = db.Column(db.INTEGER, nullable=True)
    asset_alias = db.Column(db.String(25), nullable=False)
    probe_id = db.Column(db.String(64), nullable=False)
    title = db.Column(db.String(128), nullable=False)
    kind = db.Column(db.String(32), nullable=False)
    status = db.Column(db.String(16), nullable=False)
    content_ciphertext = db.Column(LONGTEXT, nullable=False)
    error_ciphertext = db.Column(LONGTEXT, nullable=False)
    truncated = db.Column(
        db.BOOLEAN, nullable=False, default=False, server_default='0',
    )
    collected_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)


class t_ai_diagnostic_report(db.Model):
    """Structured analyzer output; evidence is referenced by opaque ID."""

    __tablename__ = 't_ai_diagnostic_report'
    run_id = db.Column(
        db.String(32),
        db.ForeignKey('t_ai_diagnostic_run.id', ondelete='CASCADE'),
        primary_key=True,
    )
    status = db.Column(db.String(16), nullable=False)
    severity = db.Column(db.String(16), nullable=False)
    summary = db.Column(db.Text, nullable=False)
    findings_json = db.Column(db.Text, nullable=False)
    evidence_insufficient = db.Column(
        db.BOOLEAN, nullable=False, default=False, server_default='0',
    )
    generated_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)


class t_ai_autonomous_run(db.Model, TimestampMixin):
    """M1/S1: 自治任务的权威快照。revision 随每次状态变化递增，
    审批决策必须携带当前 revision；budget/目标/凭据引用创建后不可变。"""

    __tablename__ = 't_ai_autonomous_run'
    __table_args__ = (
        db.UniqueConstraint(
            'active_host_id', name='uq_ai_auto_run_active_host',
        ),
        db.UniqueConstraint(
            'trigger_type', 'trigger_ref',
            name='uq_ai_auto_run_trigger',
        ),
    )
    id = db.Column(db.String(32), primary_key=True)
    owner = db.Column(db.String(24), nullable=False, index=True)
    goal = db.Column(db.String(512), nullable=False)
    host_id = db.Column(db.INTEGER, nullable=False, index=True)
    host_alias = db.Column(db.String(25), nullable=False)
    system_user_id = db.Column(db.INTEGER, nullable=False)
    system_user_alias = db.Column(db.String(24), nullable=False)
    mode = db.Column(db.String(16), nullable=False)
    # M2/S1: 触发来源只保存服务端枚举、不可逆幂等引用和脱敏摘要。
    # 手工/聊天创建没有 trigger_ref；Alertmanager 使用
    # sha256(groupKey + startsAt)，唯一键封住重复投递竞态。
    trigger_type = db.Column(
        db.String(16), nullable=False,
        default='manual', server_default='manual',
    )
    trigger_ref = db.Column(db.String(64), nullable=True)
    trigger_summary = db.Column(
        db.String(512), nullable=False, default='', server_default='',
    )
    # M1/S3: custom 权限档案（仅 mode='custom' 时非空）。服务端固定
    #   动作类别集合 + Run 已绑定的单一主机；不引入策略表达式语言。
    #   同步 DDL: backend/mysqldir/rev55_ai_autonomy_custom_profile.sql
    custom_profile_json = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, index=True)
    outcome = db.Column(db.String(16), nullable=True)
    revision = db.Column(db.INTEGER, nullable=False, default=0)
    budget_json = db.Column(db.Text, nullable=False)
    latest_event_seq = db.Column(db.INTEGER, nullable=False, default=0)
    cancel_requested = db.Column(
        db.BOOLEAN, nullable=False, default=False, server_default='0',
    )
    # M1/S2: Worker 租约与心跳。Worker 通过 lease_owner/lease_token/
    #   lease_expires_at 幂等认领 Run；每次认领生成不可复用 token，
    #   即使 Worker identity 被复用，旧执行者也不能越过接管落结果。
    #   graph_version 固定每个 Run 对应的兼容图版本，升级时按此选图。
    #   同步 DDL: backend/mysqldir/rev54_ai_autonomy_lease.sql
    lease_owner = db.Column(db.String(64), nullable=True)
    lease_token = db.Column(db.String(64), nullable=True)
    lease_expires_at = db.Column(db.DateTime, nullable=True)
    heartbeat_at = db.Column(db.DateTime, nullable=True)
    graph_version = db.Column(
        db.String(32), nullable=False,
        default='v1', server_default='v1',
    )
    # M1/S2: MySQL 不支持带 WHERE 的部分唯一索引；把活动态映射为
    # host_id、终态映射为 NULL，再利用 UNIQUE 允许多个 NULL 的语义，
    # 在数据库层封住“同一资产最多一个活动 Run”的并发竞态。
    # 未知/未来状态默认走 ELSE，按活动态 fail-closed。
    active_host_id = db.Column(
        db.INTEGER,
        Computed(
            "CASE WHEN status IN "
            "('completed', 'failed', 'cancelled', 'expired') "
            "THEN NULL ELSE host_id END",
            persisted=True,
        ),
        nullable=True,
    )
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)


class t_ai_autonomous_step(db.Model, TimestampMixin):
    """M1/S1: 审批对象。action_json 是审批前落库的不可变动作快照，
    action_digest 绑定该快照；审批通过后执行前必须复核一致。"""

    __tablename__ = 't_ai_autonomous_step'
    __table_args__ = (
        db.UniqueConstraint('run_id', 'seq', name='uq_ai_autonomy_step_seq'),
    )
    id = db.Column(db.String(32), primary_key=True)
    run_id = db.Column(
        db.String(32),
        db.ForeignKey('t_ai_autonomous_run.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    kind = db.Column(db.String(16), nullable=False)
    status = db.Column(db.String(20), nullable=False, index=True)
    seq = db.Column(db.INTEGER, nullable=False)
    summary = db.Column(db.String(255), nullable=False)
    action_json = db.Column(db.Text, nullable=True)
    action_digest = db.Column(db.String(64), nullable=True)
    note = db.Column(db.String(255), nullable=False, default='')


class t_ai_autonomous_event(db.Model):
    """M1/S1: Run 内单调递增的追加式事件。payload 永不包含凭据。"""

    __tablename__ = 't_ai_autonomous_event'
    __table_args__ = (
        db.UniqueConstraint(
            'run_id', 'sequence', name='uq_ai_autonomy_event_sequence',
        ),
    )
    # MySQL 落 BIGINT AUTO_INCREMENT（与 orange.sql/rev53 一致）；
    # SQLite 测试引擎只自动递增 INTEGER 主键，退化为 INTEGER。
    id = db.Column(
        db.BIGINT().with_variant(db.Integer(), 'sqlite'),
        primary_key=True, autoincrement=True,
    )
    run_id = db.Column(
        db.String(32),
        db.ForeignKey('t_ai_autonomous_run.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    sequence = db.Column(db.INTEGER, nullable=False)
    event_type = db.Column(db.String(32), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)


class t_ai_autonomous_artifact(db.Model):
    """M1/S1: 清洗、脱敏、限长后 Fernet 加密的执行产物引用。"""

    __tablename__ = 't_ai_autonomous_artifact'
    id = db.Column(db.String(32), primary_key=True)
    run_id = db.Column(
        db.String(32),
        db.ForeignKey('t_ai_autonomous_run.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    step_id = db.Column(db.String(32), nullable=True)
    kind = db.Column(db.String(32), nullable=False)
    title = db.Column(db.String(128), nullable=False)
    # MySQL 落 LONGTEXT（与 orange.sql/rev53 一致）；SQLite 测试引擎退化为 TEXT。
    content_ciphertext = db.Column(
        db.Text().with_variant(LONGTEXT(), 'mysql'), nullable=False,
    )
    size_bytes = db.Column(db.INTEGER, nullable=False, default=0)
    truncated = db.Column(
        db.BOOLEAN, nullable=False, default=False, server_default='0',
    )
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)


class t_ai_autonomous_evidence(db.Model):
    """M1/S3 切片 4：执行观察归一化后的有界脱敏 Evidence 引用。

    Evidence 永远标记不可信（trusted 恒为 0）：它只是不可信远程
    输出的有界索引，大输出本体仍在加密 Artifact 里；结论只能引用
    同一 Run 的 Evidence ID。owner 隔离经 run_id 归属复核。
    """

    __tablename__ = 't_ai_autonomous_evidence'
    id = db.Column(db.String(32), primary_key=True)
    run_id = db.Column(
        db.String(32),
        db.ForeignKey('t_ai_autonomous_run.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    step_id = db.Column(db.String(32), nullable=True)
    kind = db.Column(db.String(32), nullable=False)
    summary = db.Column(db.String(500), nullable=False)
    artifact_ids_json = db.Column(db.Text, nullable=False, default='[]')
    trusted = db.Column(
        db.BOOLEAN, nullable=False, default=False, server_default='0',
    )
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)


class t_ai_embedding_config(db.Model, TimestampMixin):
    """M2/S2 singleton embedding/index configuration; secrets stay encrypted."""

    __tablename__ = 't_ai_embedding_config'
    id = db.Column(db.INTEGER, primary_key=True, default=1)
    provider_type = db.Column(
        db.String(24), nullable=False, default='local', server_default='local',
    )
    base_url = db.Column(db.String(255), nullable=True)
    model = db.Column(db.String(128), nullable=False)
    api_key_ciphertext = db.Column(db.String(1024), nullable=True)
    dimension = db.Column(db.INTEGER, nullable=False, default=512)
    model_fingerprint = db.Column(db.String(64), nullable=False)
    indexed_fingerprint = db.Column(db.String(64), nullable=True)
    index_state = db.Column(
        db.String(16), nullable=False, default='empty', server_default='empty',
    )
    indexed_chunks = db.Column(
        db.INTEGER, nullable=False, default=0, server_default='0',
    )


class t_ai_knowledge_document(db.Model, TimestampMixin):
    """M2/S2 approved Markdown truth; Redis chunks are rebuildable derivatives."""

    __tablename__ = 't_ai_knowledge_document'
    __table_args__ = (
        db.UniqueConstraint(
            'source_type', 'source_ref', name='uq_ai_knowledge_source',
        ),
    )
    id = db.Column(db.String(32), primary_key=True)
    title = db.Column(db.String(128), nullable=False)
    source_type = db.Column(db.String(16), nullable=False, index=True)
    source_ref = db.Column(db.String(64), nullable=True)
    scope = db.Column(
        db.String(128), nullable=False, default='global', server_default='global',
    )
    content = db.Column(
        db.Text().with_variant(LONGTEXT(), 'mysql'), nullable=False,
    )
    content_sha256 = db.Column(db.String(64), nullable=False)
    version = db.Column(db.INTEGER, nullable=False, default=1, server_default='1')
    approved = db.Column(
        db.BOOLEAN, nullable=False, default=True, server_default='1', index=True,
    )
    indexed_fingerprint = db.Column(db.String(64), nullable=True)
    chunk_count = db.Column(
        db.INTEGER, nullable=False, default=0, server_default='0',
    )
    created_by = db.Column(db.String(24), nullable=False, index=True)


class t_settings(db.Model):
    __tablename__ = 't_settings'
    id = db.Column(db.INTEGER, primary_key=True, autoincrement=True)
    # REV10-P2-2: 实际数据是字符串 'default' (见 orange.sql:485 INSERT 与 Settings.py:36 query),
    #   default=0 是误导性代码,改为 server_default='default'
    # REV47-M2: 加 unique=True, 限制 t_settings 只允许 1 行 (name='default')
    #   背景: 业务全用 name='default' 查, 但 ORM 无 unique 约束时业务方误传
    #         第二个 name='default' 会成功, 导致 Settings() 读 row0 还是 row1 难定
    #   修复: name unique=True + name 保持 server_default='default' 双层防
    #   同步 DDL: ALTER TABLE t_settings ADD UNIQUE INDEX uq_t_settings_name (name);
    #   注意: 已存在重复 name='default' 行时 ALTER 会失败, 需先清理 (运维脚本)
    name = db.Column(db.String(20), nullable=False, unique=True, server_default='default', default='default')
    login_time = db.Column(db.INT, nullable=False, default=3)
    register_status = db.Column(db.String(5), nullable=False, default="on")
    color_matching = db.Column(db.String(10), nullable=False, default="black")
    # 安全设置
    login_fail_limit = db.Column(db.INT, nullable=False, default=5)           # 登录失败锁定次数
    lock_duration = db.Column(db.INT, nullable=False, default=30)             # 账号锁定时长(分钟)
    password_expire_days = db.Column(db.INT, nullable=False, default=90)      # 密码有效期(天), 0=永不过期
    mfa_enabled = db.Column(db.String(5), nullable=False, default="off")     # 双因素认证开关
    password_complexity = db.Column(db.String(5), nullable=False, default="off")  # 强制密码复杂度
    # 终端设置
    ssh_timeout = db.Column(db.INT, nullable=False, default=30)              # SSH连接超时(秒)
    terminal_scrollback = db.Column(db.INT, nullable=False, default=10000)   # 终端回滚行数
    session_record = db.Column(db.String(5), nullable=False, default="on")   # 会话录制开关
    max_concurrent_sessions = db.Column(db.INT, nullable=False, default=3)   # 最大并发会话数
    # 审计设置
    log_retention_days = db.Column(db.INT, nullable=False, default=180)      # 日志保留天数
    command_audit = db.Column(db.String(5), nullable=False, default="on")    # 命令审计开关
    # 文件传输设置
    upload_size_limit = db.Column(db.INT, nullable=False, default=500)       # 上传大小限制(MB)
    allow_upload = db.Column(db.String(5), nullable=False, default="on")     # 允许上传
    allow_download = db.Column(db.String(5), nullable=False, default="on")   # 允许下载
    # 通知设置
    mail_notify = db.Column(db.String(5), nullable=False, default="off")    # 邮件通知开关
    alert_email = db.Column(db.String(100), nullable=True, default="")      # 告警邮箱
    mail_smtp_host = db.Column(db.String(253), nullable=True)
    mail_smtp_port = db.Column(db.INT, nullable=True)
    mail_smtp_security = db.Column(db.String(10), nullable=True)
    mail_from = db.Column(db.String(254), nullable=True)
    mail_password_encrypted = db.Column(db.Text, nullable=True)
    # 系统设置
    system_name = db.Column(db.String(50), nullable=False, default="OrangeServer")  # 系统名称
    login_notice = db.Column(db.String(255), nullable=True, default="")     # 登录公告
    # I18N (rev51): 界面语言, zh-CN | en-US (Settings.py 白名单校验)
    language = db.Column(db.String(10), nullable=False, default="zh-CN", server_default="zh-CN")
