"""Issue #27: current user grants and owner-scoped knowledge boundaries."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.autonomy.repository import resolve_current_autonomy_role
from app.ai.tools import PlatformQueryService
from app.core.db.database import (
    db,
    t_acc_user,
    t_auth_host,
    t_auth_host_host_group,
    t_auth_host_sys_user,
    t_auth_host_user,
    t_group,
    t_host,
    t_sys_user,
)


def _add_auth(session, name, *, user_name, host_group, sys_user_alias=None):
    auth = t_auth_host(name=name)
    session.add(auth)
    session.flush()
    session.add(t_auth_host_user(
        auth_id=auth.id, user_name=user_name,
    ))
    session.add(t_auth_host_host_group(
        auth_id=auth.id, group_name=host_group,
    ))
    if sys_user_alias is not None:
        session.add(t_auth_host_sys_user(
            auth_id=auth.id, sys_user_alias=sys_user_alias,
        ))
    return auth


def _permission_fixture():
    engine = create_engine("sqlite:///:memory:")
    db.metadata.create_all(engine, tables=[
        t_group.__table__,
        t_host.__table__,
        t_acc_user.__table__,
        t_sys_user.__table__,
        t_auth_host.__table__,
        t_auth_host_user.__table__,
        t_auth_host_host_group.__table__,
        t_auth_host_sys_user.__table__,
    ])
    session = sessionmaker(bind=engine)()
    session.add_all([
        t_group(name="ops"),
        t_group(name="other"),
        t_group(name="secret"),
        t_acc_user(
            alias="Alice", name="alice", password="fake-hash",
            usrole="user", mail="alice@example.com", group="",
        ),
        t_sys_user(
            id=7, alias="readonly", host_user="tester", agreement="ssh",
        ),
    ])
    session.flush()
    hosts = {}
    for alias, group in (
        ("ops-01", "ops"),
        ("other-01", "other"),
        ("secret-01", "secret"),
    ):
        host = t_host(
            alias=alias, host_ip="203.0.113.%d" % (10 + len(hosts)),
            host_port=22, group=group, ai_environment="production",
        )
        session.add(host)
        hosts[group] = host
    session.flush()
    auth_ops = _add_auth(
        session, "ops-grant", user_name="alice", host_group="ops",
    )
    auth_other = _add_auth(
        session, "other-credential-grant", user_name="alice",
        host_group="other", sys_user_alias="readonly",
    )
    session.commit()
    return engine, session, hosts, auth_ops, auth_other


def test_user_asset_and_credential_grants_must_be_the_same_pair():
    engine, session, hosts, auth_ops, _auth_other = _permission_fixture()
    try:
        platform = PlatformQueryService("alice", "user", session=session)

        # Each independent grant is visible, but neither is enough to create
        # an authorized host/credential combination for ops-01.
        assert platform.validate_asset_ids([hosts["ops"].id]) is True
        assert platform.resolve_system_user(7)["alias"] == "readonly"
        assert platform.validate_asset_sys_user_id_pair(
            [hosts["ops"].id], 7,
        ) is False

        session.add(t_auth_host_sys_user(
            auth_id=auth_ops.id, sys_user_alias="readonly",
        ))
        session.commit()
        assert platform.validate_asset_sys_user_id_pair(
            [hosts["ops"].id], 7,
        ) is True
    finally:
        session.close()
        engine.dispose()


def test_user_knowledge_scopes_follow_current_active_host_grants():
    engine, session, hosts, auth_ops, _auth_other = _permission_fixture()
    try:
        platform = PlatformQueryService("alice", "user", session=session)
        scopes = platform.authorized_knowledge_scopes()
        assert scopes == (
            "global",
            "host:%d" % hosts["ops"].id,
            "host:%d" % hosts["other"].id,
        )
        assert "host:%d" % hosts["secret"].id not in scopes

        auth_ops.is_deleted = True
        session.commit()
        assert platform.authorized_knowledge_scopes() == (
            "global", "host:%d" % hosts["other"].id,
        )
    finally:
        session.close()
        engine.dispose()


def test_current_role_resolution_is_session_scoped_and_fail_closed():
    engine, session, _hosts, _auth_ops, _auth_other = _permission_fixture()
    try:
        assert resolve_current_autonomy_role(session, "alice") == "user"
        session.query(t_acc_user).filter_by(name="alice").update({
            "usrole": "operator",
        })
        session.commit()
        assert resolve_current_autonomy_role(session, "alice") is None
    finally:
        session.close()
        engine.dispose()
