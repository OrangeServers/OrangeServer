# -*- coding: utf-8 -*-
"""REV41-H2: AccUser 改名校验单测.

通过 mock 提取核心 rename 校验逻辑, 不依赖 ords/db/AuthAutoUpdate 等外部依赖.
"""
from unittest.mock import MagicMock, patch

import pytest


# 提取要测试的纯函数: rename-conflict 检查
def _check_rename_conflict(up_user, new_name, lookup_fn):
    """检查将 up_user.name 改为 new_name 是否冲突.

    Args:
        up_user: 原 ORM 行 (mock 或真实, 需 .id 和 .name 属性)
        new_name: 前端传入的新名
        lookup_fn(name) -> ORM 行 or None: 给定 name, 返回占用此名的行

    Returns:
        None 表示无冲突
        str 表示冲突错误信息
    """
    if up_user is None:
        return '用户不存在'
    if up_user.name != new_name:
        conflict = lookup_fn(new_name)
        if conflict and conflict.id != up_user.id:
            return '该用户名已被占用'
    return None


class TestRenameConflictPure:
    """H2: 改名冲突检查纯函数测试 (易于单测)."""

    def test_id_none_returns_error(self):
        """id 查不到 (up_user is None) → 返回错误."""
        err = _check_rename_conflict(None, 'newname', lambda n: None)
        assert err == '用户不存在'

    def test_rename_to_self_allowed(self):
        """新名就是自己的旧名 → 无冲突."""
        up = MagicMock(id=1, name='alice')
        err = _check_rename_conflict(up, 'alice', lambda n: None)
        assert err is None

    def test_rename_to_others_name_rejected(self):
        """新名被他人 (不同 id) 占用 → 拒绝."""
        up = MagicMock(id=1, name='alice')

        def lookup(n):
            if n == 'bob':
                other = MagicMock(id=2, name='bob')
                return other
            return None

        err = _check_rename_conflict(up, 'bob', lookup)
        assert err == '该用户名已被占用'

    def test_rename_to_fresh_name_allowed(self):
        """新名无人占用 → 允许."""
        up = MagicMock(id=1, name='alice')
        err = _check_rename_conflict(up, 'freshname', lambda n: None)
        assert err is None

    def test_rename_to_self_id_clash_allowed(self):
        """同名查询返回的是自己 (id 相同) → 允许 (防御 self-match)."""
        up = MagicMock(id=1, name='alice')

        def lookup(n):
            if n == 'alice':
                return up
            return None
        err = _check_rename_conflict(up, 'alice', lookup)
        assert err is None


class TestAccUserUpdateIntegration:
    """H2 集成: 实际跑 AccUserUpdate.update, mock 所有外部依赖."""

    def test_rename_to_others_name_rejected(self, monkeypatch):
        """集成测试: 改名为他人已用名 → 返回 100 + 错误信息."""
        from app.users import user as _user_mod
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context('/account/user/update', method='POST', data={
            'id': '1',
            'alias': 'admin_alias',
            'name': 'bob',
            'usrole': 'admin',
            'mail': 'bob@example.com',
            'group': 'g1',
            'remarks': '',
        }):
            up_user_row = MagicMock()
            up_user_row.id = 1
            up_user_row.name = 'alice'
            up_user_row.usrole = 'admin'

            conflict_row = MagicMock()
            conflict_row.id = 2
            conflict_row.name = 'bob'

            def filter_by_side_effect(**kwargs):
                f = MagicMock()
                if 'id' in kwargs:
                    f.first.return_value = up_user_row
                elif kwargs.get('name') == 'bob':
                    f.first.return_value = conflict_row
                else:
                    f.first.return_value = None
                return f

            mock_query = MagicMock(filter_by=filter_by_side_effect)
            monkeypatch.setattr(_user_mod.t_acc_user, 'query', mock_query)
            mock_ords = MagicMock()
            monkeypatch.setattr(_user_mod, 'get_current_user', lambda: (mock_ords, 'cz_admin'))
            monkeypatch.setattr(_user_mod.db, 'session', MagicMock())
            monkeypatch.setattr(_user_mod, 'get_current_user_role', lambda: 'admin')

            upd = _user_mod.AccUserUpdate()
            resp = upd.update
            body = resp.get_json()
            assert body['code'] == 100
            assert '已被占用' in body['msg']

    def test_rename_to_self_name_allowed(self, monkeypatch):
        """集成测试: 改名为自己 (实际未变) → 返回 0."""
        from app.users import user as _user_mod
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context('/account/user/update', method='POST', data={
            'id': '1',
            'alias': 'admin_alias',
            'name': 'alice',
            'usrole': 'admin',
            'mail': 'alice@example.com',
            'group': 'g1',
            'remarks': '',
        }):
            up_user_row = MagicMock()
            up_user_row.id = 1
            up_user_row.name = 'alice'
            up_user_row.usrole = 'admin'

            def filter_by_side_effect(**kwargs):
                f = MagicMock()
                if 'id' in kwargs:
                    f.first.return_value = up_user_row
                else:
                    f.first.return_value = None
                return f

            mock_query = MagicMock(filter_by=filter_by_side_effect)
            monkeypatch.setattr(_user_mod.t_acc_user, 'query', mock_query)
            monkeypatch.setattr(_user_mod, 'AuthAutoUpdate', MagicMock())
            session = MagicMock()
            permission_sync = MagicMock()
            permission_sync.side_effect = (
                lambda *args, **kwargs: session.commit.assert_not_called()
            )
            monkeypatch.setattr(
                _user_mod, 'sync_user_permissions', permission_sync,
            )
            monkeypatch.setattr(_user_mod, 'hash_pwd', lambda x: 'hashed')
            mock_ords = MagicMock()
            monkeypatch.setattr(_user_mod, 'get_current_user', lambda: (mock_ords, 'cz_admin'))
            monkeypatch.setattr(_user_mod.db, 'session', session)
            monkeypatch.setattr(_user_mod, 'get_current_user_role', lambda: 'admin')

            upd = _user_mod.AccUserUpdate()
            resp = upd.update
            body = resp.get_json()
            assert body['code'] == 0
            permission_sync.assert_called_once_with(
                'alice', 'alice', True, True, commit=False,
            )

    def test_id_not_found_rejected(self, monkeypatch):
        """集成测试: id 不存在 → 返回 100, 不 AttributeError."""
        from app.users import user as _user_mod
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context('/account/user/update', method='POST', data={
            'id': '999',
            'alias': 'x',
            'name': 'newname',
            'usrole': 'user',
            'mail': 'n@example.com',
            'group': 'g1',
            'remarks': '',
        }):
            def filter_by_side_effect(**kwargs):
                f = MagicMock()
                f.first.return_value = None
                return f
            mock_query = MagicMock(filter_by=filter_by_side_effect)
            monkeypatch.setattr(_user_mod.t_acc_user, 'query', mock_query)
            mock_ords = MagicMock()
            monkeypatch.setattr(_user_mod, 'get_current_user', lambda: (mock_ords, 'cz_admin'))
            monkeypatch.setattr(_user_mod, 'get_current_user_role', lambda: 'admin')

            upd = _user_mod.AccUserUpdate()
            resp = upd.update
            body = resp.get_json()
            assert body['code'] == 100
            assert '操作失败' in body['msg']

    def test_rename_to_fresh_name_allowed(self, monkeypatch):
        """集成测试: 新名无人占用 → 返回 0."""
        from app.users import user as _user_mod
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context('/account/user/update', method='POST', data={
            'id': '1',
            'alias': 'admin_alias',
            'name': 'freshname',
            'usrole': 'user',
            'mail': 'f@example.com',
            'group': 'g1',
            'remarks': '',
        }):
            up_user_row = MagicMock()
            up_user_row.id = 1
            up_user_row.name = 'alice'
            up_user_row.usrole = 'user'

            def filter_by_side_effect(**kwargs):
                f = MagicMock()
                if 'id' in kwargs:
                    f.first.return_value = up_user_row
                else:
                    f.first.return_value = None
                return f

            mock_query = MagicMock(filter_by=filter_by_side_effect)
            monkeypatch.setattr(_user_mod.t_acc_user, 'query', mock_query)
            monkeypatch.setattr(_user_mod, 'AuthAutoUpdate', MagicMock())
            permission_sync = MagicMock()
            monkeypatch.setattr(
                _user_mod, 'sync_user_permissions', permission_sync,
            )
            monkeypatch.setattr(_user_mod, 'hash_pwd', lambda x: 'hashed')
            mock_ords = MagicMock()
            monkeypatch.setattr(_user_mod, 'get_current_user', lambda: (mock_ords, 'cz_admin'))
            monkeypatch.setattr(_user_mod.db, 'session', MagicMock())
            monkeypatch.setattr(_user_mod, 'get_current_user_role', lambda: 'admin')

            upd = _user_mod.AccUserUpdate()
            resp = upd.update
            body = resp.get_json()
            assert body['code'] == 0
            permission_sync.assert_called_once_with(
                'alice', 'freshname', False, False, commit=False,
            )
            mock_ords.conn.delete.assert_any_call('alice_role')
            mock_ords.conn.delete.assert_any_call('alice_alias')


class TestRenameCheckModuleFunction:
    """确保 user.py 内部也提供 _check_rename_conflict 函数供测试覆盖."""

    def test_module_exports_function(self):
        """app.users.user._check_rename_conflict 必须存在 (修复必须暴露)."""
        from app.users import user as _user_mod
        assert hasattr(_user_mod, '_check_rename_conflict')

    def test_function_is_callable(self):
        from app.users import user as _user_mod
        up = MagicMock(id=1, name='alice')
        result = _user_mod._check_rename_conflict(up, 'alice', lambda n: None)
        assert result is None

    def test_function_returns_error_on_conflict(self):
        from app.users import user as _user_mod
        up = MagicMock(id=1, name='alice')
        conflict = MagicMock(id=2, name='bob')
        result = _user_mod._check_rename_conflict(up, 'bob', lambda n: conflict)
        assert result == '该用户名已被占用'
