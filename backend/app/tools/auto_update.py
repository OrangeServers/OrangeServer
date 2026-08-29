"""AutoUpdate：各模块数据变更后的自动更新逻辑（计数更新 + 权限收集更新）

REVIEW-10-P2-4 迁移:
  - 5 处裸 query.update / query.delete 全部走 osql_up / osql_de 统一封装
  - 统一异常处理 + 自动 rollback

REV44-H3 修复:
  - 首次部署 / DB 迁移后 t_auth_host 表可能没有 '所有权限' 行,
    导致 host_group_auth / sys_user_auth / acc_group_auth 三个方法静默 skip,
    关联表永远为空, 所有用户看不到 host. 增加 _ensure_all_auth_row() 兜底.

REV44-H2 修复:
  - host_group_auth / sys_user_auth / acc_group_auth 修改权限关联表无审计,
    安全相关表变更无追溯 = 安全事故无证据.
  - 修复: commit 成功后调 _audit_permission_change() 写入 t_cz_log
    (cz_name / 类型=权限操作 / info=刷新所有权限 / details=<关联表>_<count>)
"""
from app.core.db.database import t_host, t_group, t_sys_user, t_acc_user, t_acc_group, t_auth_host, \
    t_auth_host_user, t_auth_host_user_group, t_auth_host_host_group, t_auth_host_sys_user
from app.core.db.insert import osql_up, osql_de, osql_in, SqlOpError
from app.core.db.settings import db  # REV44-M1: 顶层 import, 替代函数内 from import
from app.tools.SqlListTool import ListTool
from app.tools.at import Log  # REV44-H1: 静默 catch 加 Log


# REV44-H3: '所有权限' 兜底默认 remarks
_ALL_AUTH_DEFAULT_REMARKS = '系统默认所有权限'


def _ensure_all_auth_row():
    """REV44-H3: 兜底确保 t_auth_host 表存在 '所有权限' 行.

    背景: 首次部署 / 数据库迁移后, t_auth_host 表可能没有 '所有权限' 行,
    三个 auth 方法 (host_group_auth / sys_user_auth / acc_group_auth)
    静默 skip, 导致 t_auth_host_user_group 等关联表永远为空,
    所有用户都看不到任何 host. 这里在缺失时自动创建一行.

    Returns:
        t_auth_host ORM 行 (含 id, 供后续关联表使用), or None (创建失败)
    """
    all_auth = t_auth_host.query.filter_by(name='所有权限').first()
    if all_auth:
        return all_auth
    try:
        osql_in('t_auth_host', name='所有权限', remarks=_ALL_AUTH_DEFAULT_REMARKS)
        Log.logger.info('REV44-H3: auto-created t_auth_host \"所有权限\" row')
    except SqlOpError as e:
        # REV44-H1: 静默 catch 加 Log, 不再吞错
        Log.logger.error('REV44-H3: failed to auto-create t_auth_host \"所有权限\": %s', e)
        return None
    # 重新查以拿到 auto-increment 的 id (供后续 osql_de 关联表使用)
    return t_auth_host.query.filter_by(name='所有权限').first()


def sync_user_permissions(
    old_name,
    new_name,
    was_admin,
    is_admin,
    *,
    ensure_all_auth_row=None,
    auth_model=t_auth_host,
    auth_user_model=t_auth_host_user,
    session=db.session,
    commit=True,
):
    """Keep direct permission bindings valid across account rename/setup.

    Admin is a role, but several asset and automation authorization paths
    still consume the existing all-access junction row.  Keep that row in
    sync instead of duplicating administrator bypasses across every consumer.
    """
    def all_auth_row(create):
        if ensure_all_auth_row is not None:
            return ensure_all_auth_row()
        row = auth_model.query.filter_by(
            name='所有权限',
            is_deleted=False,
        ).first()
        if row is None and create:
            row = auth_model(
                name='所有权限',
                remarks=_ALL_AUTH_DEFAULT_REMARKS,
            )
            session.add(row)
            session.flush()
        return row

    changed = False
    if old_name and old_name != new_name:
        for binding in auth_user_model.query.filter_by(
            user_name=old_name,
        ).all():
            duplicate = auth_user_model.query.filter_by(
                auth_id=binding.auth_id,
                user_name=new_name,
            ).first()
            if duplicate is None:
                binding.user_name = new_name
            else:
                session.delete(binding)
            changed = True
    if is_admin:
        all_auth = all_auth_row(True)
        if all_auth is None:
            raise RuntimeError('无法初始化所有权限规则')
        existing = auth_user_model.query.filter_by(
            auth_id=all_auth.id,
            user_name=new_name,
        ).first()
        if existing is None:
            session.add(auth_user_model(
                auth_id=all_auth.id,
                user_name=new_name,
            ))
            changed = True
    elif was_admin:
        all_auth = all_auth_row(False)
        binding = None if all_auth is None else auth_user_model.query.filter_by(
            auth_id=all_auth.id,
            user_name=new_name,
        ).first()
        if binding is not None:
            session.delete(binding)
            changed = True
    if changed and commit:
        session.commit()
    return changed


def _audit_permission_change(operation, count):
    """REV44-H2: 审计权限关联表变更 (host_group / sys_user / user_group).

    写入 t_cz_log (CzToolsLog):
      - log_name = 当前登录用户 (cz_name)
      - log_type = '权限操作'
      - log_info = '刷新所有权限'
      - log_details = '<关联表>_count=<count>'
      - log_status = '成功'

    安全考量:
      - 失败 silent (REV44-H4 一致: 审计失败不能拖垮主业务)
      - 未登录态不审计 (理论上路由层 roles=['admin'] 已过滤, 此处二次防御)
      - 与现有 _ensure_all_auth_row helper 一样用顶层辅助函数, 测试可独立 mock
    """
    try:
        from app.tools.at import get_current_user
        from app.tools.audlog import CzToolsLog
        _ords, cz_name = get_current_user()
        if not cz_name:
            # 未登录态理论上不应走到这里 (路由层 + 类内 _require_admin_or_raise 已拦截)
            # 此处防御性 silent skip, 避免空 cz_name 写库污染审计
            return
        CzToolsLog().host_log(
            cz_name,
            '权限操作',
            '刷新所有权限',
            '%s_count=%d' % (operation, count),
            '成功',
        )
    except Exception:
        # REV44-H4: 审计失败不阻断主业务 (silent pass)
        # 兜底: 与 _BaseToolsLog._write 双重保护一致
        pass


class AuthAutoUpdate:
    """统一的数据变更后自动更新类，替代分散在各文件中的 Auto 类。"""

    # ---- 计数更新 ----

    @staticmethod
    def host_grp_count(group):
        """更新资产组内资产计数。原 ServerAuto.host_grp_auto_update"""
        try:
            # REV47-M6: 计数过滤软删行
            host_count = t_host.query.filter_by(group=group, is_deleted=False).count()
            osql_up('t_group', {'name': group}, {'nums': host_count})
            return True
        except SqlOpError as e:
            Log.logger.error('AuthAutoUpdate.host_grp_count failed: %s', e)
            return False

    @staticmethod
    def user_grp_count(group):
        """更新用户组内用户计数。原 UserAuto.user_grp_auto_update"""
        try:
            # REV47-M6: 计数过滤软删行
            user_count = t_acc_user.query.filter_by(group=group, is_deleted=False).count()
            osql_up('t_acc_group', {'name': group}, {'nums': user_count})
            return True
        except SqlOpError as e:
            Log.logger.error('AuthAutoUpdate.user_grp_count failed: %s', e)
            return False

    # ---- R2-10 (REV44-H1): 显式 caller-aware wrapper ----
    # 问题: 原 host_grp_count / user_grp_count 失败只 log.error + return False,
    #   9 处 caller 全部忽略返回值, 组成员数和实际计数长期不一致, 静默难以追踪.
    # 修复: 加 _safe_* wrapper, 失败时再记一次 WARNING 标记 caller,
    #   并记录 op_label 用于定位是哪次操作触发的计数偏差.
    @staticmethod
    def _safe_group_count(counter_fn, group, op_label):
        """R2-10: 显式 caller-aware 包装. failure 时 caller 侧记 WARNING."""
        try:
            if not counter_fn(group):
                # counter 已 log.error, 这里再加 WARNING 标记 caller
                Log.logger.warning(
                    'R2-10 group count failed (count drift possible): '
                    'op=%s group=%s', op_label, group)
                return False
            return True
        except Exception as e:
            Log.logger.warning(
                'R2-10 group count exception (count drift possible): '
                'op=%s group=%s err=%s', op_label, group, e)
            return False

    @classmethod
    def safe_host_grp_count(cls, group, op_label='host_grp'):
        """R2-10: caller-aware host_grp_count 包装"""
        return cls._safe_group_count(cls.host_grp_count, group, op_label)

    @classmethod
    def safe_user_grp_count(cls, group, op_label='user_grp'):
        """R2-10: caller-aware user_grp_count 包装"""
        return cls._safe_group_count(cls.user_grp_count, group, op_label)

    # ---- 权限收集更新 ----

    @staticmethod
    def host_group_auth():
        """收集所有资产组名 → 更新"所有权限"的 host_group 关联表。原 ServerGroupAuto.grp_auth_auto_update"""
        try:
            # REV47-M6: 收集过滤软删组
            query_msg = ListTool.list_gather(t_group.query.filter_by(is_deleted=False).with_entities(t_group.name).all())
            all_auth = _ensure_all_auth_row()  # REV44-H3: 首次部署自动创建
            if all_auth:
                # 删除走统一封装 (自动 rollback + SqlOpError)
                osql_de('t_auth_host_host_group', {'auth_id': all_auth.id})
                for gname in query_msg:
                    db.session.add(t_auth_host_host_group(auth_id=all_auth.id, group_name=gname))
                db.session.commit()
                # REV44-H2: 权限表变更加审计 (谁动了"所有权限"的 host_group 关联表)
                _audit_permission_change('host_group', len(query_msg))
            return True
        except SqlOpError as e:
            Log.logger.error('AuthAutoUpdate.host_group_auth failed: %s', e)
            return False

    @staticmethod
    def sys_user_auth():
        """收集所有系统用户别名 → 更新"所有权限"的 sys_user 关联表。原 SysUserAuto.user_auth_auto_update"""
        try:
            # REV47-M6: 收集过滤软删 sys_user
            query_msg = ListTool.list_gather(t_sys_user.query.filter_by(is_deleted=False).with_entities(t_sys_user.alias).all())
            all_auth = _ensure_all_auth_row()  # REV44-H3: 首次部署自动创建
            if all_auth:
                osql_de('t_auth_host_sys_user', {'auth_id': all_auth.id})
                for alias in query_msg:
                    db.session.add(t_auth_host_sys_user(auth_id=all_auth.id, sys_user_alias=alias))
                db.session.commit()
                # REV44-H2: 权限表变更加审计
                _audit_permission_change('sys_user', len(query_msg))
            return True
        except SqlOpError as e:
            Log.logger.error('AuthAutoUpdate.sys_user_auth failed: %s', e)
            return False

    @staticmethod
    def acc_group_auth():
        """收集所有用户组名 → 更新"所有权限"的 user_group 关联表。原 AccGroupAuto.grp_auth_auto_update"""
        try:
            # REV47-M6: 收集过滤软删 acc_group
            query_msg = ListTool.list_gather(t_acc_group.query.filter_by(is_deleted=False).with_entities(t_acc_group.name).all())
            all_auth = _ensure_all_auth_row()  # REV44-H3: 首次部署自动创建
            if all_auth:
                osql_de('t_auth_host_user_group', {'auth_id': all_auth.id})
                for gname in query_msg:
                    db.session.add(t_auth_host_user_group(auth_id=all_auth.id, group_name=gname))
                db.session.commit()
                # REV44-H2: 权限表变更加审计
                _audit_permission_change('user_group', len(query_msg))
            return True
        except SqlOpError as e:
            Log.logger.error('AuthAutoUpdate.acc_group_auth failed: %s', e)
            return False
