from unittest.mock import MagicMock, call


def test_admin_permission_sync_moves_renamed_user_and_binds_all_permissions():
    from app.tools.auto_update import sync_user_permissions

    all_auth = MagicMock(id=7)
    auth_user = MagicMock()
    old_bindings = MagicMock(auth_id=3)
    old_query = MagicMock()
    new_binding = MagicMock()

    def filter_by(**kwargs):
        if kwargs == {'user_name': 'admin'}:
            return old_query
        if kwargs == {'auth_id': 3, 'user_name': 'custom_admin'}:
            duplicate = MagicMock()
            duplicate.first.return_value = None
            return duplicate
        if kwargs == {'auth_id': 7, 'user_name': 'custom_admin'}:
            return new_binding
        raise AssertionError(kwargs)

    auth_user.query.filter_by.side_effect = filter_by
    old_query.all.return_value = [old_bindings]
    new_binding.first.return_value = None
    session = MagicMock()

    assert sync_user_permissions(
        'admin', 'custom_admin', True, True,
        ensure_all_auth_row=lambda: all_auth,
        auth_user_model=auth_user,
        session=session,
    )

    assert old_bindings.user_name == 'custom_admin'
    auth_user.assert_called_once_with(auth_id=7, user_name='custom_admin')
    session.add.assert_called_once_with(auth_user.return_value)
    session.commit.assert_called_once_with()


def test_non_admin_rename_preserves_direct_permission_bindings():
    from app.tools.auto_update import sync_user_permissions

    auth_user = MagicMock()
    old_binding = MagicMock(auth_id=4)
    old_query = MagicMock()
    old_query.all.return_value = [old_binding]
    duplicate_query = MagicMock()
    duplicate_query.first.return_value = None

    def filter_by(**kwargs):
        if kwargs == {'user_name': 'alice'}:
            return old_query
        if kwargs == {'auth_id': 4, 'user_name': 'alice-renamed'}:
            return duplicate_query
        raise AssertionError(kwargs)

    auth_user.query.filter_by.side_effect = filter_by
    session = MagicMock()

    assert sync_user_permissions(
        'alice', 'alice-renamed', False, False,
        ensure_all_auth_row=MagicMock(),
        auth_user_model=auth_user,
        session=session,
    )

    assert old_binding.user_name == 'alice-renamed'
    session.commit.assert_called_once_with()


def test_admin_demotion_removes_automatic_all_access():
    from app.tools.auto_update import sync_user_permissions

    all_auth = MagicMock(id=7)
    binding = MagicMock()
    auth_user = MagicMock()
    query = auth_user.query.filter_by.return_value
    query.first.return_value = binding
    session = MagicMock()

    assert sync_user_permissions(
        'alice', 'alice', True, False,
        ensure_all_auth_row=lambda: all_auth,
        auth_user_model=auth_user,
        session=session,
    )

    auth_user.query.filter_by.assert_called_once_with(
        auth_id=7, user_name='alice',
    )
    session.delete.assert_called_once_with(binding)
    session.commit.assert_called_once_with()


def test_admin_rename_deduplicates_stale_target_binding():
    from app.tools.auto_update import sync_user_permissions

    all_auth = MagicMock(id=7)
    old_binding = MagicMock(auth_id=7)
    old_query = MagicMock()
    old_query.all.return_value = [old_binding]
    target_binding = MagicMock()
    target_query = MagicMock()
    target_query.first.return_value = target_binding
    auth_user = MagicMock()

    def filter_by(**kwargs):
        if kwargs == {'user_name': 'admin'}:
            return old_query
        if kwargs == {'auth_id': 7, 'user_name': 'custom_admin'}:
            return target_query
        raise AssertionError(kwargs)

    auth_user.query.filter_by.side_effect = filter_by
    session = MagicMock()

    assert sync_user_permissions(
        'admin', 'custom_admin', True, True,
        ensure_all_auth_row=lambda: all_auth,
        auth_user_model=auth_user,
        session=session,
    )

    session.delete.assert_called_once_with(old_binding)
    session.add.assert_not_called()
    session.commit.assert_called_once_with()


def test_admin_sync_creates_all_access_in_callers_transaction():
    from app.tools.auto_update import sync_user_permissions

    all_auth = MagicMock(id=7)
    auth_model = MagicMock(return_value=all_auth)
    auth_model.query.filter_by.return_value.first.return_value = None
    auth_user = MagicMock()
    auth_user.query.filter_by.return_value.first.return_value = None
    session = MagicMock()

    assert sync_user_permissions(
        None, 'custom_admin', False, True,
        auth_model=auth_model,
        auth_user_model=auth_user,
        session=session,
        commit=False,
    )

    session.flush.assert_called_once_with()
    session.commit.assert_not_called()
    assert session.add.call_args_list == [
        call(all_auth),
        call(auth_user.return_value),
    ]
