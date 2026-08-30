# -*- coding: utf-8 -*-
"""M1/S2: draft 与待审批过期语义契约测试（Issue #13）。

有效期（默认 24 小时）超期后：draft 绝不带着陈旧草稿开跑，待审
批绝不接受陈旧决策——两者一律落 expired，且绝不自动越过审批。
惰性检查守在 start_run/decide 入口，Worker 启动扫描做周期兜底。
"""
import datetime
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.autonomy.actions import (
    StructuredAction,
    build_action_digest,
)
from app.ai.autonomy.repository import (
    AutonomyConflict,
    AutonomyRepository,
    sweep_expired_runs,
)
from app.ai.autonomy.state import AutonomyStateError
from app.core import config
from app.core.db.database import (
    db,
    t_ai_autonomous_event,
    t_ai_autonomous_run,
    t_ai_autonomous_step,
    t_group,
    t_host,
)

SECRET_KEY = "unit-test-secret-key-for-autonomy-expiry"


class FakePlatform:
    def __init__(self, owner, role):
        pass

    def validate_asset_ids(self, asset_ids):
        return True

    def validate_asset_sys_user_id_pair(self, asset_ids, sys_user_id):
        return True

    def resolve_system_user(self, sys_user_id):
        return {"id": int(sys_user_id), "alias": "readonly"}


@pytest.fixture()
def expiry_env(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    db.metadata.create_all(
        engine,
        tables=[t_group.__table__, t_host.__table__,
                t_ai_autonomous_run.__table__,
                t_ai_autonomous_step.__table__,
                t_ai_autonomous_event.__table__],
    )
    session = sessionmaker(bind=engine)()
    repo = AutonomyRepository(
        session, SECRET_KEY,
        platform_factory=lambda owner, role: FakePlatform(owner, role),
    )
    host_seq = {"n": 0}

    def create_draft(**kwargs):
        # 每个 Run 独占一台 host：同 host 活动 Run 互斥是产品契约。
        host_seq["n"] += 1
        n = host_seq["n"]
        host = t_host(
            alias="web-%02d" % n, host_ip="203.0.113.%d" % (10 + n),
            host_port=22, ai_environment="production",
        )
        session.add(host)
        session.commit()
        payload = dict(
            goal="diagnose latency",
            host_id=int(host.id),
            system_user_id=19,
            mode="ask",
        )
        payload.update(kwargs)
        return repo.create_run("admin", "admin", **payload)

    def force_waiting_approval(run_id):
        """直接构造一个等待审批的动作 Step（模拟 APPROVAL_REQUIRED）。"""
        run_row = session.get(t_ai_autonomous_run, run_id)
        step_id = "step-waiting-expiry"
        action = StructuredAction(
            kind="shell",
            target_id=int(run_row.host_id),
            system_user_id=int(run_row.system_user_id),
            parameters={"command": "systemctl restart nginx"},
            timeout_seconds=60,
            step_id=step_id,
        )
        session.add(t_ai_autonomous_step(
            id=step_id,
            run_id=run_id,
            kind="action",
            status="waiting_approval",
            seq=1,
            summary="shell command=systemctl restart nginx",
            action_json=json.dumps(
                action.to_canonical_dict(), sort_keys=True, ensure_ascii=True,
            ),
            action_digest=build_action_digest(action, SECRET_KEY),
            note="",
        ))
        run_row.status = "waiting_approval"
        session.commit()
        return step_id

    # 有效期收紧到 1 小时，拨回 2 小时即稳定超期。
    monkeypatch.setattr(config, "AI_AUTONOMY_APPROVAL_TTL_SECONDS", 3600)

    env = {
        "repo": repo,
        "session": session,
        "create_draft": create_draft,
        "force_waiting_approval": force_waiting_approval,
    }
    yield env
    session.close()
    engine.dispose()


def _rewind(session, row, hours=2):
    """把行的 updated_at 拨回，制造超期状态。"""
    row.updated_at = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    )
    session.commit()


# ---------------------------------------------------------------------------
# draft 惰性过期：超期草稿绝不启动
# ---------------------------------------------------------------------------

def test_expired_draft_cannot_start_and_falls_to_expired(expiry_env):
    session = expiry_env["session"]
    repo = expiry_env["repo"]
    run = expiry_env["create_draft"]()
    row = session.get(t_ai_autonomous_run, run["id"])
    row.lease_owner = "stale-worker"
    row.lease_token = "stale-claim"
    row.lease_expires_at = datetime.datetime.utcnow()
    _rewind(session, row)

    with pytest.raises(AutonomyConflict):
        repo.start_run("admin", "admin", run["id"])

    row = session.get(t_ai_autonomous_run, run["id"])
    assert row.status == "expired"
    assert row.completed_at is not None
    assert row.lease_owner is None
    assert row.lease_token is None
    assert row.lease_expires_at is None

    events = session.query(t_ai_autonomous_event).filter_by(
        run_id=run["id"],
    ).order_by(t_ai_autonomous_event.sequence).all()
    assert [event.event_type for event in events] == [
        "run_created", "run_expired",
    ]
    payload = json.loads(events[-1].payload_json)
    assert payload["reason"] == "draft_expired"

    # expired 是终态：再次启动被状态机拒绝。
    with pytest.raises(AutonomyStateError):
        repo.start_run("admin", "admin", run["id"])


def test_fresh_draft_within_ttl_starts_normally(expiry_env):
    run = expiry_env["create_draft"]()
    started = expiry_env["repo"].start_run("admin", "admin", run["id"])
    assert started["status"] == "queued"


# ---------------------------------------------------------------------------
# 待审批惰性过期：超期审批绝不执行，绝不自动越过审批
# ---------------------------------------------------------------------------

def test_expired_approval_is_rejected_on_decide(expiry_env):
    session = expiry_env["session"]
    repo = expiry_env["repo"]
    run = expiry_env["create_draft"]()
    repo.start_run("admin", "admin", run["id"])
    step_id = expiry_env["force_waiting_approval"](run["id"])
    step = session.query(t_ai_autonomous_step).filter_by(id=step_id).one()
    run_row = session.get(t_ai_autonomous_run, run["id"])
    run_row.lease_owner = "stale-worker"
    run_row.lease_token = "stale-claim"
    run_row.lease_expires_at = datetime.datetime.utcnow()
    session.commit()
    _rewind(session, step)

    with pytest.raises(AutonomyConflict):
        repo.decide(
            "admin", "admin", run["id"], step_id,
            operation="approve", expected_revision=0,
        )

    step = session.query(t_ai_autonomous_step).filter_by(id=step_id).one()
    assert step.status == "cancelled"
    assert step.note == "approval expired"

    run_row = session.get(t_ai_autonomous_run, run["id"])
    assert run_row.status == "expired"
    assert run_row.lease_owner is None
    assert run_row.lease_token is None
    assert run_row.lease_expires_at is None
    events = session.query(t_ai_autonomous_event).filter_by(
        run_id=run["id"], event_type="run_expired",
    ).all()
    assert len(events) == 1
    payload = json.loads(events[0].payload_json)
    assert payload["reason"] == "approval_expired"


# ---------------------------------------------------------------------------
# 周期清扫：Worker 启动扫描兜底，活动 Run 绝不碰
# ---------------------------------------------------------------------------

def test_sweep_expires_stale_draft_and_pending_approval(expiry_env):
    session = expiry_env["session"]
    repo = expiry_env["repo"]

    # 超期 draft。
    stale_draft = expiry_env["create_draft"]()
    _rewind(
        session, session.get(t_ai_autonomous_run, stale_draft["id"]),
    )

    # 超期待审批：Step 拨回即触发 Run 一并过期。
    pending = expiry_env["create_draft"]()
    repo.start_run("admin", "admin", pending["id"])
    expiry_env["force_waiting_approval"](pending["id"])
    step = session.query(t_ai_autonomous_step).filter_by(
        run_id=pending["id"],
    ).one()
    _rewind(session, step)

    # 新鲜 draft 与活动 Run：绝不过期。
    fresh_draft = expiry_env["create_draft"]()
    active = expiry_env["create_draft"]()
    repo.start_run("admin", "admin", active["id"])

    result = sweep_expired_runs(session, ttl_seconds=3600)
    assert result == {"expired_runs": 2, "cancelled_steps": 1}

    assert session.get(
        t_ai_autonomous_run, stale_draft["id"],
    ).status == "expired"
    assert session.get(
        t_ai_autonomous_run, pending["id"],
    ).status == "expired"
    assert session.get(
        t_ai_autonomous_run, fresh_draft["id"],
    ).status == "draft"
    assert session.get(
        t_ai_autonomous_run, active["id"],
    ).status == "queued"
