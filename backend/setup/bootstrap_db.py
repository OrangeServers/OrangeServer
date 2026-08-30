# -*- coding: utf-8 -*-
"""setup apply 的建库子进程：`python -m setup.bootstrap_db`。

由 setup/app.py 以候选配置注入 env 后 subprocess 调用（参数走 env + stdin
JSON，不进 argv 防 /proc 泄露）。此时配置已齐，可安全 import 业务代码。

职责（全部幂等）：
1. db.create_all() 以 ORM 模型为准建表（绕开 orange.sql 种子漂移问题）
2. 幂等种子：t_acc_group admin 组、t_settings default 行、system 内置账号
3. 管理员账号：按向导输入创建 usrole='admin'；若种子 admin/admin 弱口令行
   已存在（compose bundled 已被 mysql 容器 init 的场景）则原地覆盖
4. 可选设置写 t_settings（system_name / register_status / login_notice）

stdout 输出 JSON 步骤结果；exit code 非 0 = 失败（apply 不落任何配置文件）。
"""
from __future__ import annotations

import json
import sys


def _ensure_database_exists(step) -> None:
    """目标库不存在则尝试创建（utf8mb4）。无建库权限时不中断——
    随后的 create_all 会给出明确报错，由向导展示。"""
    import os

    import pymysql

    dbname = os.environ.get('OGS_MYSQL_DBNAME') or 'orange'
    try:
        conn = pymysql.connect(
            host=os.environ.get('OGS_MYSQL_HOST') or '',
            port=int(os.environ.get('OGS_MYSQL_PORT') or 3306),
            user=os.environ.get('OGS_MYSQL_USER') or '',
            password=os.environ.get('OGS_MYSQL_PASSWORD') or '',
            connect_timeout=5, charset='utf8mb4',
        )
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    'CREATE DATABASE IF NOT EXISTS `%s` '
                    'CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'
                    % dbname.replace('`', '')
                )
            conn.commit()
            step('ensure_db', True, '数据库 %s 就绪' % dbname)
        finally:
            conn.close()
    except Exception as exc:
        step('ensure_db', False, '建库跳过（%s: %s）——若库已存在可忽略'
             % (type(exc).__name__, str(exc)[:120]))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or '{}')
    except json.JSONDecodeError:
        print(json.dumps({'ok': False, 'error': 'stdin 不是合法 JSON'}))
        return 2

    steps = []

    def step(name, ok, msg=''):
        steps.append({'name': name, 'ok': bool(ok), 'msg': str(msg)[:300]})

    try:
        # 配置已注入 env，import 期的 config fail-fast 应当通过
        from app.app_factory import app
        from app.core.db.database import (
            db,
            t_acc_group,
            t_acc_user,
            t_settings,
        )
        from app.tools.auto_update import sync_user_permissions
        from app.tools.basesec import hash_pwd
        from app.mail.config import save_configuration
        step('import', True, '业务模块加载成功')
    except Exception as exc:
        step('import', False, '%s: %s' % (type(exc).__name__, exc))
        print(json.dumps({'ok': False, 'steps': steps}, ensure_ascii=False))
        return 3

    admin = payload.get('admin') or {}
    username = str(admin.get('username') or '').strip()
    password = str(admin.get('password') or '')
    email = str(admin.get('email') or '').strip()
    optional = payload.get('settings') or {}
    mail = payload.get('mail') or {}

    try:
        _ensure_database_exists(step)
        with app.app_context():
            db.create_all()
            step('create_all', True, '表结构就绪（模型为准，存量表不修改）')

            if not t_acc_group.query.filter_by(name='admin').first():
                db.session.add(t_acc_group(name='admin', nums=1, remarks='管理员组'))
            if not t_settings.query.filter_by(name='default').first():
                db.session.add(t_settings(name='default'))
            if not t_acc_user.query.filter_by(name='system').first():
                db.session.add(t_acc_user(
                    id=99, alias='system', name='system',
                    password=hash_pwd('!disabled-system-account!'),
                    usrole='member', mail='system@orange.local',
                    group='admin', remarks='内置系统账号, 不可删除',
                ))
            db.session.commit()
            step('seed', True, '基础种子就绪')

            if username and password:
                hashed = hash_pwd(password)
                existing = t_acc_user.query.filter_by(name=username).first()
                legacy = t_acc_user.query.filter_by(name='admin').first()
                previous_username = None
                if existing is not None:
                    if legacy is not None and legacy.id != existing.id:
                        previous_username = legacy.name
                        legacy.usrole = 'user'
                        legacy.is_deleted = True
                        legacy.password = hash_pwd(
                            '!disabled-legacy-admin-account!'
                        )
                    existing.password = hashed
                    existing.usrole = 'admin'
                    existing.group = 'admin'
                    existing.is_deleted = False
                    existing.password_version = 2
                    if email:
                        existing.mail = email
                    step('admin', True, '已更新既有账号 %s 为管理员' % username)
                elif legacy is not None and username != 'admin':
                    # 覆盖种子 admin/admin 弱口令行：改名为向导管理员
                    previous_username = legacy.name
                    legacy.alias = username
                    legacy.name = username
                    legacy.password = hashed
                    legacy.usrole = 'admin'
                    legacy.password_version = 2
                    legacy.mail = email or ('%s@orange.local' % username)
                    step('admin', True, '种子 admin 弱口令行已替换为 %s' % username)
                else:
                    db.session.add(t_acc_user(
                        alias=username, name=username, password=hashed,
                        usrole='admin',
                        mail=email or ('%s@orange.local' % username),
                        group='admin', remarks='首次部署向导创建',
                    ))
                    step('admin', True, '管理员 %s 已创建' % username)
                sync_user_permissions(
                    previous_username,
                    username,
                    True,
                    True,
                    commit=False,
                )
                db.session.commit()
                step('admin_auth', True, '管理员已绑定所有权限')
            else:
                step('admin', False, '缺少管理员用户名或密码')
                raise ValueError('admin required')

            allowed = {'system_name', 'register_status', 'login_notice'}
            updates = {k: str(v) for k, v in optional.items() if k in allowed and v is not None}
            if updates:
                t_settings.query.filter_by(name='default').update(updates)
                db.session.commit()
            step('settings', True, '可选设置已写入' if updates else '无可选设置')

            if mail:
                settings_row = t_settings.query.filter_by(name='default').first()
                save_configuration(settings_row, mail)
                db.session.commit()
            step('smtp', True, 'SMTP 设置已加密写入' if mail else '已跳过 SMTP 设置')
    except Exception as exc:
        db.session.rollback()
        step('apply_db', False, '%s: %s' % (type(exc).__name__, exc))
        print(json.dumps({'ok': False, 'steps': steps}, ensure_ascii=False))
        return 4

    print(json.dumps({'ok': True, 'steps': steps}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
