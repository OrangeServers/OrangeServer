# -*- coding: utf-8 -*-
"""M1/S2: RunLeaseService 租约认领、心跳与启动扫描契约测试（Issue #13）。

与 test_ai_autonomy_repository 相同的注入式 SQLite 内存引擎方案：
conftest 把 db.session patch 成 no-op，这里用独立引擎验证真实落库。
"""
import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.autonomy.lease import (
    REASON_LEASE_EXPIRED,
    REASON_QUEUED,
    RunLeaseService,
)
from app.ai.autonomy.repository import AutonomyRepository
from app.core.db.database import (
    db,
    t_ai_autonomous_event,
    t_ai_autonomous_run,
    t_ai_autonomous_step,
    t_group,
    t_host,
)

SECRET_KEY = "unit-test-secret-key-for-autonomy-lease"
TTL = 300


class FakePlatform:
    def __init__(self, owner, role):
        pass

    def validate_asset_ids(self, asset_ids):
        return True

    def resolve_system_user(self, sys_user_id):
        return {"id": int(sys_user_id), "alias": "readonly"}


@pytest.fixture()
def lease_env():
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

    def create_queued_run(**kwargs):
        # 每个 Run 独立资产，避开"同一资产最多一个活动 Run"约束。
        host_seq["n"] += 1
        n = host_seq["n"]
        host = t_host(
            alias="web-%02d" % n, host_ip="203.0.113.%d" % (10 + n),
            host_port=22, ai_environment="lab",
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
        run = repo.create_run("admin", "admin", **payload)
        return repo.start_run("admin", "admin", run["id"])

    env = {
        "session": session,
        "repo": repo,
        "lease": RunLeaseService(session),
        "create_queued_run": create_queued_run,
    }
    yield env
    session.close()
    engine.dispose()


def _row(session, run_id):
    return session.query(t_ai_autonomous_run).filter_by(id=run_id).one()


# ---------------------------------------------------------------------------
# 认领：重复投递互斥、同 Worker 幂等、过期租约可再认领
# ---------------------------------------------------------------------------

def test_claim_moves_queued_run_to_running_with_lease(lease_env):
    run = lease_env["create_queued_run"]()
    claimed = lease_env["lease"].claim_run(run["id"], "worker-a", TTL)
    assert claimed is not None
    row = _row(lease_env["session"], run["id"])
    assert row.status == "running"
    assert row.lease_owner == "worker-a"
    assert row.lease_token == claimed["lease_token"]
    assert len(claimed["lease_token"]) >= 32
    assert row.lease_expires_at is not None
    assert row.heartbeat_at is not None
    assert claimed["revision"] == int(row.revision)
    assert claimed["revision"] == run["revision"] + 1
    assert "lease_token" not in lease_env["repo"].get_run(
        "admin", run["id"],
    )


def test_duplicate_delivery_never_runs_run_in_parallel(lease_env):
    """两个 Worker 同时认领同一 queued Run，恰好一个成功。"""
    run = lease_env["create_queued_run"]()
    first = lease_env["lease"].claim_run(run["id"], "worker-a", TTL)
    second = lease_env["lease"].claim_run(run["id"], "worker-b", TTL)
    assert first is not None
    assert second is None
    row = _row(lease_env["session"], run["id"])
    assert row.lease_owner == "worker-a"
    assert row.status == "running"


def test_reclaim_while_lease_valid_is_rejected_even_for_owner(lease_env):
    """租约有效期间再认领一律 None（含持有者自身）；
    执行中的重复投递由任务层查 get_lease_state 自行跳过。"""
    run = lease_env["create_queued_run"]()
    first = lease_env["lease"].claim_run(run["id"], "worker-a", TTL)
    later = datetime.datetime.utcnow() + datetime.timedelta(seconds=5)
    second = lease_env["lease"].claim_run(
        run["id"], "worker-a", TTL, now=later,
    )
    assert first is not None
    assert second is None
    row = _row(lease_env["session"], run["id"])
    assert row.lease_owner == "worker-a"
    assert row.status == "running"


def test_get_lease_state_snapshot(lease_env):
    """get_lease_state 只读返回状态/租约快照，行不存在返回 None。"""
    lease = lease_env["lease"]
    assert lease.get_lease_state("missing") is None
    run = lease_env["create_queued_run"]()
    state = lease.get_lease_state(run["id"])
    assert state["status"] == "queued"
    assert state["lease_owner"] is None
    claimed = lease.claim_run(run["id"], "worker-a", TTL)
    state = lease.get_lease_state(run["id"])
    assert state["status"] == "running"
    assert state["lease_owner"] == "worker-a"
    assert state["revision"] == claimed["revision"]
    # 只读：快照不改变行状态。
    row = _row(lease_env["session"], run["id"])
    assert row.revision == claimed["revision"]


def test_expired_lease_can_be_reclaimed_by_another_worker(lease_env):
    """租约过期后其他 Worker 接管并进入 recovering；未过期则不行。"""
    run = lease_env["create_queued_run"]()
    now = datetime.datetime.utcnow()
    assert lease_env["lease"].claim_run(run["id"], "worker-a", TTL, now=now)
    # 租约仍在有效期内：worker-b 抢不走。
    mid = now + datetime.timedelta(seconds=TTL - 1)
    assert lease_env["lease"].claim_run(run["id"], "worker-b", TTL, now=mid) is None
    # 租约过期（持有者失联）：worker-b 接管，Run 回到 recovering 重新规划。
    late = now + datetime.timedelta(seconds=TTL + 1)
    reclaimed = lease_env["lease"].claim_run(run["id"], "worker-b", TTL, now=late)
    assert reclaimed is not None
    row = _row(lease_env["session"], run["id"])
    assert row.lease_owner == "worker-b"
    assert row.status == "recovering"


def test_expired_lease_reclaim_fences_stale_same_identity(lease_env):
    """每次 claim 都产生新 token；进程 identity 复用也不能越过接管。"""
    run = lease_env["create_queued_run"]()
    now = datetime.datetime.utcnow()
    first = lease_env["lease"].claim_run(
        run["id"], "worker-a", TTL, now=now,
    )
    late = now + datetime.timedelta(seconds=TTL + 1)
    second = lease_env["lease"].claim_run(
        run["id"], "worker-a", TTL, now=late,
    )

    assert first["lease_token"] != second["lease_token"]
    assert lease_env["lease"].heartbeat(
        run["id"], "worker-a", first["lease_token"], TTL,
        now=late + datetime.timedelta(seconds=1),
    ) is None
    assert lease_env["lease"].release_lease(
        run["id"], "worker-a", first["lease_token"],
        now=late + datetime.timedelta(seconds=1),
    ) is None
    row = _row(lease_env["session"], run["id"])
    assert row.lease_token == second["lease_token"]


def test_expired_waiting_approval_lease_can_be_reclaimed(lease_env):
    """waiting_approval 的租约过期后同样可被接管。"""
    session = lease_env["session"]
    run = lease_env["create_queued_run"]()
    now = datetime.datetime.utcnow()
    lease_env["lease"].claim_run(run["id"], "worker-a", TTL, now=now)
    row = _row(session, run["id"])
    row.status = "waiting_approval"
    session.commit()
    late = now + datetime.timedelta(seconds=TTL + 1)
    reclaimed = lease_env["lease"].claim_run(run["id"], "worker-b", TTL, now=late)
    assert reclaimed is not None
    row = _row(session, run["id"])
    assert row.status == "recovering"
    assert row.lease_owner == "worker-b"


def test_claim_rejects_non_queued_or_unknown_run(lease_env):
    lease = lease_env["lease"]
    assert lease.claim_run("does-not-exist", "worker-a", TTL) is None
    run = lease_env["create_queued_run"]()
    row = _row(lease_env["session"], run["id"])
    row.status = "completed"
    lease_env["session"].commit()
    assert lease.claim_run(run["id"], "worker-a", TTL) is None


# ---------------------------------------------------------------------------
# 心跳 / 释放：lease owner 隔离，不与业务 revision 相互干扰
# ---------------------------------------------------------------------------

def test_heartbeat_refreshes_lease_without_bumping_business_revision(lease_env):
    run = lease_env["create_queued_run"]()
    claimed = lease_env["lease"].claim_run(run["id"], "worker-a", TTL)
    renewed = lease_env["lease"].heartbeat(
        run["id"], "worker-a", claimed["lease_token"], TTL,
    )
    assert renewed is not None
    assert renewed["revision"] == claimed["revision"]


def test_heartbeat_survives_unrelated_business_revision_change(lease_env):
    """审批/取消等业务 revision 变化不能误杀仍持有租约的 Worker。"""
    run = lease_env["create_queued_run"]()
    claimed = lease_env["lease"].claim_run(run["id"], "worker-a", TTL)
    row = _row(lease_env["session"], run["id"])
    row.revision += 1
    row.cancel_requested = True
    lease_env["session"].commit()
    renewed = lease_env["lease"].heartbeat(
        run["id"], "worker-a", claimed["lease_token"], TTL,
    )
    assert renewed is not None
    assert renewed["revision"] == claimed["revision"] + 1


def test_heartbeat_by_non_owner_is_rejected(lease_env):
    run = lease_env["create_queued_run"]()
    claimed = lease_env["lease"].claim_run(run["id"], "worker-a", TTL)
    assert lease_env["lease"].heartbeat(
        run["id"], "worker-b", claimed["lease_token"], TTL,
    ) is None


def test_expired_owner_cannot_revive_or_release_lease(lease_env):
    run = lease_env["create_queued_run"]()
    now = datetime.datetime.utcnow()
    claimed = lease_env["lease"].claim_run(
        run["id"], "worker-a", TTL, now=now,
    )
    expired = now + datetime.timedelta(seconds=TTL + 1)
    assert lease_env["lease"].heartbeat(
        run["id"], "worker-a", claimed["lease_token"], TTL, now=expired,
    ) is None
    assert lease_env["lease"].release_lease(
        run["id"], "worker-a", claimed["lease_token"], now=expired,
    ) is None


def test_release_clears_lease_only_for_current_owner(lease_env):
    run = lease_env["create_queued_run"]()
    claimed = lease_env["lease"].claim_run(run["id"], "worker-a", TTL)
    assert lease_env["lease"].release_lease(
        run["id"], "worker-b", claimed["lease_token"],
    ) is None
    released = lease_env["lease"].release_lease(
        run["id"], "worker-a", claimed["lease_token"],
    )
    assert released is not None
    row = _row(lease_env["session"], run["id"])
    assert row.lease_owner is None
    assert row.lease_token is None
    assert row.lease_expires_at is None


# ---------------------------------------------------------------------------
# 启动扫描：queued 待认领 + 活动态租约过期
# ---------------------------------------------------------------------------

def test_scan_recoverable_returns_queued_and_expired_candidates(lease_env):
    session = lease_env["session"]
    lease = lease_env["lease"]

    queued_run = lease_env["create_queued_run"]()

    expired_run = lease_env["create_queued_run"]()
    now = datetime.datetime.utcnow()
    lease.claim_run(expired_run["id"], "worker-a", TTL, now=now)
    late = now + datetime.timedelta(seconds=TTL + 1)
    # 健康租约：在扫描时刻刚刚认领，仍在有效期内。
    healthy_run = lease_env["create_queued_run"]()
    lease.claim_run(healthy_run["id"], "worker-a", TTL, now=late)

    # 终态 Run 不应出现在扫描结果里。
    done_run = lease_env["create_queued_run"]()
    row = _row(session, done_run["id"])
    row.status = "completed"
    session.commit()

    candidates = lease.scan_recoverable(now=late)
    by_id = {c["run_id"]: c for c in candidates}

    assert by_id[queued_run["id"]]["reason"] == REASON_QUEUED
    assert by_id[expired_run["id"]]["reason"] == REASON_LEASE_EXPIRED
    assert by_id[expired_run["id"]]["status"] == "running"
    assert healthy_run["id"] not in by_id
    assert done_run["id"] not in by_id


def test_scan_recoverable_is_read_only(lease_env):
    run = lease_env["create_queued_run"]()
    lease_env["lease"].scan_recoverable()
    row = _row(lease_env["session"], run["id"])
    assert row.status == "queued"
    assert row.lease_owner is None
    assert row.revision == run["revision"]
