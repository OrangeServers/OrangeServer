# -*- coding: utf-8 -*-
"""M1/S2: 自治 Run 的 Worker 租约认领、心跳与启动恢复扫描。

设计要点：
- Celery 任务只携带 run_id，按至少一次投递设计；重复投递的并行安全
  完全由条件 UPDATE 的租约认领保证，不依赖先 SELECT 再写。
- 认领条件：Run 处于 queued，且租约空闲、已过期或属于本 Worker；
  认领原子地把状态推进到 running 并递增 revision。
- 心跳与释放都复核 lease_owner + 当前 revision，过期 revision 的
  旧 Worker 无法续租或释放他人持有的租约。
- 启动扫描只返回候选（queued 待认领 / 活动态租约过期），恢复策略
  由执行器按写意图与 checkpoint 边界决定，本模块不重放任何动作。
"""
import datetime

import sqlalchemy as sa

from app.ai.autonomy.state import RunStatus

# 租约覆盖的活动态：queued 等待首次认领，running/waiting_approval
# 的租约过期后由新的 Worker 重新认领并进入 recovering 重新规划。
_LEASED_ACTIVE_STATUSES = (
    RunStatus.RUNNING.value,
    RunStatus.WAITING_APPROVAL.value,
)

REASON_QUEUED = 'queued'
REASON_LEASE_EXPIRED = 'lease_expired'


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


class RunLeaseService:
    """session 注入式的租约操作层，不自建连接。"""

    def __init__(self, session):
        self.session = session

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _run_table(self):
        from app.core.db.database import t_ai_autonomous_run
        return t_ai_autonomous_run.__table__

    def _fetch(self, run_id):
        row = self.session.execute(
            sa.select(self._run_table()).where(
                self._run_table().c.id == run_id,
            )
        ).first()
        return row

    def _commit(self):
        self.session.commit()

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def get_lease_state(self, run_id):
        """返回 Run 的当前状态/租约快照；行不存在返回 None。

        供任务层区分"执行中的重复投递"与"新认领"，只读。
        """
        row = self._fetch(run_id)
        if row is None:
            return None
        return {
            'run_id': run_id,
            'status': row.status,
            'revision': int(row.revision or 0),
            'lease_owner': row.lease_owner,
            'lease_expires_at': row.lease_expires_at,
        }

    # ------------------------------------------------------------------
    # 认领 / 心跳 / 释放
    # ------------------------------------------------------------------

    def claim_run(
        self, run_id, worker_id, lease_ttl_seconds, now=None,
    ):
        """原子认领 queued Run，或接管活动态中租约已过期的 Run。

        成功返回 {'run_id', 'revision', 'lease_expires_at'}；
        行不存在、状态不可认领、或租约被任何 Worker（含自身）有效
        持有时返回 None。queued 认领把状态推进到 running；接管过期
        租约进入 recovering，由恢复切片按写意图与 checkpoint 边界
        重新规划。执行中的重复投递由任务层先查 get_lease_state 自行
        跳过，不经这里。
        """
        table = self._run_table()
        now = now or _utcnow()
        expires_at = now + datetime.timedelta(seconds=int(lease_ttl_seconds))
        worker_id = str(worker_id)[:64]

        new_status = sa.case(
            (table.c.status == RunStatus.QUEUED.value,
             RunStatus.RUNNING.value),
            else_=RunStatus.RECOVERING.value,
        )
        result = self.session.execute(
            sa.update(table)
            .where(table.c.id == run_id)
            .where(
                sa.or_(
                    sa.and_(
                        table.c.status == RunStatus.QUEUED.value,
                        sa.or_(
                            table.c.lease_owner.is_(None),
                            table.c.lease_expires_at.is_(None),
                            table.c.lease_expires_at < now,
                        ),
                    ),
                    sa.and_(
                        table.c.status.in_(_LEASED_ACTIVE_STATUSES),
                        table.c.lease_expires_at.isnot(None),
                        table.c.lease_expires_at < now,
                    ),
                )
            )
            .values(
                status=new_status,
                lease_owner=worker_id,
                lease_expires_at=expires_at,
                heartbeat_at=now,
                revision=table.c.revision + 1,
            )
        )
        self._commit()
        if result.rowcount == 0:
            return None
        row = self._fetch(run_id)
        return {
            'run_id': run_id,
            'revision': int(row.revision),
            'lease_expires_at': row.lease_expires_at,
        }

    def heartbeat(self, run_id, worker_id, expected_revision, lease_ttl_seconds, now=None):
        """持有者续租。revision 过期或非持有者一律失败。"""
        table = self._run_table()
        now = now or _utcnow()
        expires_at = now + datetime.timedelta(seconds=int(lease_ttl_seconds))
        result = self.session.execute(
            sa.update(table)
            .where(table.c.id == run_id)
            .where(table.c.lease_owner == str(worker_id)[:64])
            .where(table.c.revision == int(expected_revision))
            .values(
                heartbeat_at=now,
                lease_expires_at=expires_at,
                revision=table.c.revision + 1,
            )
        )
        self._commit()
        if result.rowcount == 0:
            return None
        row = self._fetch(run_id)
        return {
            'run_id': run_id,
            'revision': int(row.revision),
            'lease_expires_at': row.lease_expires_at,
        }

    def release_lease(self, run_id, worker_id, expected_revision):
        """持有者释放租约（Run 留在当前状态，等待下一次认领）。"""
        table = self._run_table()
        result = self.session.execute(
            sa.update(table)
            .where(table.c.id == run_id)
            .where(table.c.lease_owner == str(worker_id)[:64])
            .where(table.c.revision == int(expected_revision))
            .values(
                lease_owner=None,
                lease_expires_at=None,
                revision=table.c.revision + 1,
            )
        )
        self._commit()
        if result.rowcount == 0:
            return None
        row = self._fetch(run_id)
        return {'run_id': run_id, 'revision': int(row.revision)}

    # ------------------------------------------------------------------
    # 启动恢复扫描
    # ------------------------------------------------------------------

    def scan_recoverable(self, now=None):
        """Worker 启动扫描：queued 待认领 + 活动态租约过期。

        返回按 created_at 排序的候选列表，每项含 run_id、status 与
        reason；本方法只读，不改任何状态。
        健康暂停（waiting_approval 且租约已释放，lease_expires_at
        为 NULL）不是候选：它由决策接口的显式投递唤醒，反复扫描
        只会制造无意义的认领/释放 churn。
        """
        table = self._run_table()
        now = now or _utcnow()
        rows = self.session.execute(
            sa.select(table).where(
                sa.or_(
                    table.c.status == RunStatus.QUEUED.value,
                    sa.and_(
                        table.c.status.in_(_LEASED_ACTIVE_STATUSES),
                        table.c.lease_expires_at.isnot(None),
                        table.c.lease_expires_at < now,
                    ),
                )
            ).order_by(table.c.created_at.asc())
        ).fetchall()
        candidates = []
        for row in rows:
            if row.status == RunStatus.QUEUED.value:
                reason = REASON_QUEUED
            else:
                reason = REASON_LEASE_EXPIRED
            candidates.append({
                'run_id': row.id,
                'status': row.status,
                'reason': reason,
                'revision': int(row.revision or 0),
                'lease_owner': row.lease_owner,
            })
        return candidates
