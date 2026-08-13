#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Disposable M1/S2 smoke probe; invoked only by the test Compose stack."""
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import sys
import time

import pymysql
from pymysql.constants import CLIENT
from redis import Redis


AUTONOMY_TABLES = (
    't_ai_autonomous_run',
    't_ai_autonomous_step',
    't_ai_autonomous_event',
    't_ai_autonomous_artifact',
)
TERMINAL_STATUSES = {'completed', 'failed', 'cancelled', 'expired'}
PERSISTENCE_KEY = 'ogs:s2-smoke:redis8-aof-marker'
WORKER_KILL_RUN_ID = 'smoke-worker-kill'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def mysql_connection(host):
    return pymysql.connect(
        host=host,
        port=3306,
        user=os.environ['OGS_MYSQL_USER'],
        password=os.environ['OGS_MYSQL_PASSWORD'],
        database=os.environ['OGS_MYSQL_DBNAME'],
        charset='utf8mb4',
        autocommit=True,
        client_flag=CLIENT.MULTI_STATEMENTS,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=15,
        write_timeout=15,
    )


def execute_sql_script(connection, path):
    sql = Path(path).read_text(encoding='utf-8')
    with connection.cursor() as cursor:
        cursor.execute(sql)
        while True:
            if cursor.description is not None:
                cursor.fetchall()
            if not cursor.nextset():
                break


def fetch_all(connection, sql, args=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, args)
        return list(cursor.fetchall())


def fetch_one(connection, sql, args=()):
    rows = fetch_all(connection, sql, args)
    require(len(rows) == 1, 'expected exactly one database row')
    return rows[0]


def schema_snapshot(connection):
    placeholders = ','.join(['%s'] * len(AUTONOMY_TABLES))
    columns = fetch_all(
        connection,
        """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE,
               COLUMN_DEFAULT, EXTRA, GENERATION_EXPRESSION
          FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME IN (%s)
        """ % placeholders,
        AUTONOMY_TABLES,
    )
    indexes = fetch_all(
        connection,
        """
        SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME
          FROM information_schema.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME IN (%s)
        """ % placeholders,
        AUTONOMY_TABLES,
    )
    foreign_keys = fetch_all(
        connection,
        """
        SELECT TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME,
               REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
          FROM information_schema.KEY_COLUMN_USAGE
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME IN (%s)
           AND REFERENCED_TABLE_NAME IS NOT NULL
        """ % placeholders,
        AUTONOMY_TABLES,
    )

    def canonical(rows):
        return {
            tuple(
                '' if value is None else str(value).lower()
                for value in row.values()
            )
            for row in rows
        }

    return {
        'columns': canonical(columns),
        'indexes': canonical(indexes),
        'foreign_keys': canonical(foreign_keys),
    }


def assert_generated_unique(connection):
    column = fetch_one(
        connection,
        """
        SELECT EXTRA, GENERATION_EXPRESSION
          FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = 't_ai_autonomous_run'
           AND COLUMN_NAME = 'active_host_id'
        """,
    )
    expression = str(column['GENERATION_EXPRESSION'] or '').lower()
    require('stored generated' in str(column['EXTRA']).lower(),
            'active_host_id must be a STORED generated column')
    for fragment in ('status', 'host_id', *sorted(TERMINAL_STATUSES)):
        require(fragment in expression,
                'active_host_id expression is missing %s' % fragment)
    index = fetch_one(
        connection,
        """
        SELECT NON_UNIQUE, COLUMN_NAME
          FROM information_schema.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = 't_ai_autonomous_run'
           AND INDEX_NAME = 'uq_ai_auto_run_active_host'
        """,
    )
    require(int(index['NON_UNIQUE']) == 0,
            'active host index must be unique')
    require(index['COLUMN_NAME'] == 'active_host_id',
            'active host unique index targets the wrong column')


def insert_run(connection, run_id, host_id, status):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO t_ai_autonomous_run
                (id, owner, goal, host_id, host_alias, system_user_id,
                 system_user_alias, mode, status, revision, budget_json,
                 latest_event_seq, graph_version)
            VALUES
                (%s, 'smoke', 'disposable S2 smoke', %s, 'smoke-host', 1,
                 'smoke-user', 'assisted', %s, 0, %s, 0, 'v1')
            """,
            (run_id, host_id, status, json.dumps({
                'duration_seconds': 3600,
                'max_loops': 20,
                'max_actions': 30,
                'command_timeout_seconds': 60,
                'step_output_bytes': 65536,
                'run_artifact_bytes': 2097152,
            }, sort_keys=True)),
        )


def delete_run(connection, run_id):
    with connection.cursor() as cursor:
        cursor.execute(
            'DELETE FROM t_ai_autonomous_run WHERE id = %s',
            (run_id,),
        )


def insert_step(connection, run_id, step_id, status):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO t_ai_autonomous_step
                (id, run_id, kind, status, seq, summary, action_json,
                 action_digest, note)
            VALUES
                (%s, %s, 'action', %s, 1, 'disposable smoke boundary',
                 '{}', NULL, '')
            """,
            (step_id, run_id, status),
        )


def append_event(connection, run_id, event_type, payload):
    row = fetch_one(
        connection,
        'SELECT latest_event_seq FROM t_ai_autonomous_run WHERE id = %s',
        (run_id,),
    )
    sequence = int(row['latest_event_seq'] or 0) + 1
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO t_ai_autonomous_event
                (run_id, sequence, event_type, payload_json)
            VALUES (%s, %s, %s, %s)
            """,
            (run_id, sequence, event_type, json.dumps(payload)),
        )
        cursor.execute(
            """
            UPDATE t_ai_autonomous_run
               SET latest_event_seq = %s
             WHERE id = %s
            """,
            (sequence, run_id),
        )


def wait_for_run(connection, run_id, predicate, description, timeout=45):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = fetch_one(
            connection,
            """
            SELECT status, revision, cancel_requested, lease_owner,
                   lease_expires_at
              FROM t_ai_autonomous_run WHERE id = %s
            """,
            (run_id,),
        )
        if predicate(row):
            return row
        time.sleep(0.1)
    raise AssertionError('%s; last row=%r' % (description, row))


def dispatch_run(run_id):
    from app.ai.autonomy.worker import DRIVE_RUN_TASK, get_celery_app

    celery_app = get_celery_app()
    require(celery_app is not None, 'Celery application was not constructed')
    celery_app.send_task(DRIVE_RUN_TASK, args=[run_id])


def assert_unique_behavior(connection, prefix, host_id):
    suffixes = ('-active', '-duplicate', '-done-a', '-done-b')
    ids = [prefix + suffix for suffix in suffixes]
    with connection.cursor() as cursor:
        cursor.execute(
            'DELETE FROM t_ai_autonomous_run WHERE id IN (%s, %s, %s, %s)',
            ids,
        )
    insert_run(connection, ids[0], host_id, 'queued')
    try:
        insert_run(connection, ids[1], host_id, 'draft')
    except pymysql.err.IntegrityError as exc:
        require(exc.args and int(exc.args[0]) == 1062,
                'duplicate active Run failed for the wrong reason')
    else:
        raise AssertionError('database accepted two active Runs for one host')
    insert_run(connection, ids[2], host_id, 'completed')
    insert_run(connection, ids[3], host_id, 'failed')
    rows = fetch_all(
        connection,
        'SELECT id, active_host_id FROM t_ai_autonomous_run '
        'WHERE id IN (%s, %s, %s) ORDER BY id',
        (ids[0], ids[2], ids[3]),
    )
    active = {row['id']: row['active_host_id'] for row in rows}
    require(int(active[ids[0]]) == host_id,
            'active Run did not reserve its host')
    require(active[ids[2]] is None and active[ids[3]] is None,
            'terminal Run history must map active_host_id to NULL')


def assert_orm_parity(connection):
    from app.core.db.database import (
        t_ai_autonomous_artifact,
        t_ai_autonomous_event,
        t_ai_autonomous_run,
        t_ai_autonomous_step,
    )

    models = {
        't_ai_autonomous_run': t_ai_autonomous_run,
        't_ai_autonomous_step': t_ai_autonomous_step,
        't_ai_autonomous_event': t_ai_autonomous_event,
        't_ai_autonomous_artifact': t_ai_autonomous_artifact,
    }
    for table_name, model in models.items():
        rows = fetch_all(
            connection,
            """
            SELECT COLUMN_NAME
              FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            (table_name,),
        )
        database_columns = {row['COLUMN_NAME'] for row in rows}
        orm_columns = {column.name for column in model.__table__.columns}
        require(database_columns == orm_columns,
                '%s differs between MySQL and ORM' % table_name)


def redis_client(database):
    from app.ai.autonomy.readiness import autonomy_redis_url

    return Redis.from_url(
        autonomy_redis_url(database),
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


def assert_redis_policy(client):
    require(client.ping(), 'autonomy Redis ping failed')
    require(client.config_get('appendonly').get('appendonly') == 'yes',
            'autonomy Redis AOF is disabled')
    policy = client.config_get('maxmemory-policy').get('maxmemory-policy')
    require(policy == 'noeviction',
            'autonomy Redis must use noeviction')


def migrate_and_prime():
    fresh = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    upgrade = mysql_connection(os.environ['OGS_S2_SMOKE_UPGRADE_MYSQL_HOST'])
    try:
        for _ in range(2):
            execute_sql_script(
                upgrade, '/smoke/sql/rev53_ai_autonomy_baseline.sql',
            )
            execute_sql_script(
                upgrade, '/smoke/sql/rev54_ai_autonomy_lease.sql',
            )
        require(schema_snapshot(fresh) == schema_snapshot(upgrade),
                'fresh and v1.0.4 upgraded autonomy schemas differ')
        for connection, prefix, host_id in (
            (fresh, 'smoke-fresh', 190001),
            (upgrade, 'smoke-upgrade', 190002),
        ):
            assert_generated_unique(connection)
            assert_unique_behavior(connection, prefix, host_id)
        assert_orm_parity(fresh)
        host_column = fetch_one(
            upgrade,
            """
            SELECT COLUMN_DEFAULT, IS_NULLABLE
              FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = 't_host'
               AND COLUMN_NAME = 'ai_environment'
            """,
        )
        require(str(host_column['COLUMN_DEFAULT']) == 'production',
                'v1.0.4 upgrade missed t_host.ai_environment default')
        require(host_column['IS_NULLABLE'] == 'NO',
                't_host.ai_environment must be non-null')
    finally:
        fresh.close()
        upgrade.close()

    from app.ai.autonomy.readiness import checkpoint_readiness

    db0 = redis_client(0)
    db1 = redis_client(1)
    try:
        assert_redis_policy(db0)
        require(checkpoint_readiness(timeout=5),
                'Redis 8 lacks checkpoint JSON/Search readiness')
        marker = os.environ['OGS_S2_SMOKE_GIT_HEAD']
        db0.set(PERSISTENCE_KEY, marker)
        persisted = db0.execute_command('WAITAOF', 1, 0, 10000)
        require(
            isinstance(persisted, (list, tuple))
            and int(persisted[0]) >= 1,
            'Redis did not acknowledge a local AOF fsync',
        )
        require(db1.get(PERSISTENCE_KEY) is None,
                'DB 0 checkpoint data leaked into DB 1 broker')
    finally:
        db0.close()
        db1.close()


def verify_persistence():
    from app.ai.autonomy.readiness import checkpoint_readiness

    db0 = redis_client(0)
    try:
        assert_redis_policy(db0)
        marker = os.environ['OGS_S2_SMOKE_GIT_HEAD']
        require(db0.get(PERSISTENCE_KEY) == marker,
                'DB 0 marker did not survive Redis process restart')
        require(checkpoint_readiness(timeout=5),
                'checkpoint readiness failed after Redis restart')
    finally:
        db0.close()


def assert_concurrent_lease_claim(run_id):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.ai.autonomy.lease import RunLeaseService
    from app.core import config

    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                'DELETE FROM t_ai_autonomous_run WHERE id = %s',
                (run_id,),
            )
        insert_run(connection, run_id, 190003, 'queued')
    finally:
        connection.close()

    engine = create_engine(config.MYSQL_URI, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine)

    def claim(worker_id):
        session = sessions()
        try:
            return RunLeaseService(session).claim_run(
                run_id, worker_id, 60,
            )
        finally:
            session.close()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            workers = ('smoke-worker-a', 'smoke-worker-b')
            results = list(pool.map(claim, workers))
        require(sum(result is not None for result in results) == 1,
                'concurrent real-MySQL lease claim did not have one winner')
    finally:
        engine.dispose()


def worker_and_duplicate():
    from app.ai.autonomy.readiness import (
        checkpoint_readiness, worker_readiness,
    )
    from app.ai.autonomy.worker import DRIVE_RUN_TASK, get_celery_app

    require(checkpoint_readiness(timeout=5),
            'checkpoint DB 0 is not ready with the worker running')
    require(worker_readiness(timeout=8),
            'real Celery worker did not register drive_run on DB 1')

    db1 = redis_client(1)
    try:
        require(db1.ping(), 'Celery broker DB 1 ping failed')
        require(db1.get(PERSISTENCE_KEY) is None,
                'checkpoint marker leaked into the Celery broker DB')
        require(any(db1.scan_iter(match='_kombu.binding.*', count=100)),
                'DB 1 contains no Celery broker bindings')
    finally:
        db1.close()

    assert_concurrent_lease_claim('smoke-lease-race')

    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    run_id = 'smoke-celery-duplicate'
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                'DELETE FROM t_ai_autonomous_run WHERE id = %s',
                (run_id,),
            )
        insert_run(connection, run_id, 190004, 'queued')
        celery_app = get_celery_app()
        require(
            celery_app is not None,
            'Celery application was not constructed',
        )
        celery_app.send_task(DRIVE_RUN_TASK, args=[run_id])
        celery_app.send_task(DRIVE_RUN_TASK, args=[run_id])

        deadline = time.monotonic() + 45
        row = None
        while time.monotonic() < deadline:
            row = fetch_one(
                connection,
                """
                SELECT status, revision, lease_owner, lease_expires_at
                  FROM t_ai_autonomous_run WHERE id = %s
                """,
                (run_id,),
            )
            if row['status'] in TERMINAL_STATUSES:
                break
            time.sleep(0.25)
        require(
            row is not None and row['status'] == 'failed',
            'duplicate delivery did not fail closed at planner boundary',
        )
        require(row['lease_owner'] is None and row['lease_expires_at'] is None,
                'terminal Run retained a worker lease')
        event = fetch_one(
            connection,
            """
            SELECT COUNT(*) AS count
              FROM t_ai_autonomous_event
             WHERE run_id = %s AND event_type = 'planner_unavailable'
            """,
            (run_id,),
        )
        require(int(event['count']) == 1,
                'duplicate delivery produced more than one terminal event')
    finally:
        connection.close()


def lease_and_boundary():
    """Real MySQL expiry takeover plus approved-step boundary selection.

    The approved action is deliberately not executed here: doing so would
    cross the real SSH gate. This phase proves only that recovery chooses the
    persisted execute boundary and never replans.
    """
    import sqlalchemy as sa
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.ai.autonomy.lease import RunLeaseService
    from app.ai.autonomy.recovery import MODE_BOUNDARY, RecoveryService
    from app.ai.autonomy.repository import AutonomyRepository
    from app.core import config
    from app.core.db.database import t_ai_autonomous_run

    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    lease_run = 'smoke-lease-expiry'
    boundary_run = 'smoke-approved-boundary'
    try:
        delete_run(connection, lease_run)
        delete_run(connection, boundary_run)
        insert_run(connection, lease_run, 190005, 'queued')
        insert_run(connection, boundary_run, 190006, 'recovering')
        insert_step(
            connection, boundary_run, 'smoke-approved-step', 'approved',
        )
    finally:
        connection.close()

    engine = create_engine(config.MYSQL_URI, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine)
    old = datetime.datetime.utcnow() - datetime.timedelta(seconds=30)
    first_session = sessions()
    second_session = sessions()
    boundary_session = sessions()
    try:
        first = RunLeaseService(first_session).claim_run(
            lease_run, 'expired-worker', 10, now=old,
        )
        require(first is not None, 'initial real-MySQL lease claim failed')
        second = RunLeaseService(second_session).claim_run(
            lease_run, 'takeover-worker', 10,
        )
        require(second is not None, 'expired lease was not claimable')
        lease_row = second_session.get(t_ai_autonomous_run, lease_run)
        require(lease_row.status == 'recovering',
                'expired lease takeover must enter recovering')
        require(lease_row.lease_owner == 'takeover-worker',
                'expired lease takeover retained the old owner')

        boundary_row = boundary_session.get(
            t_ai_autonomous_run, boundary_run,
        )
        repo = AutonomyRepository(
            boundary_session, config.FLASK_SECRET_KEY,
        )
        outcome = RecoveryService(boundary_session, repo).resolve(
            boundary_row, checkpoint_present=False,
        )
        require(outcome.mode == MODE_BOUNDARY,
                'approved step did not select MySQL execute boundary')
        require(outcome.entry['pending_step_id'] == 'smoke-approved-step',
                'boundary recovery selected the wrong step')
        event = boundary_session.execute(
            sa.text(
                """
                SELECT COUNT(*) FROM t_ai_autonomous_event
                 WHERE run_id = :run_id
                   AND event_type = 'recovery_boundary_rebuild'
                """
            ),
            {'run_id': boundary_run},
        ).scalar_one()
        require(int(event) == 1,
                'boundary selection did not persist its recovery event')
    finally:
        first_session.close()
        second_session.close()
        boundary_session.close()
        engine.dispose()


def checkpoint_and_cancel():
    """Real Worker recovery with no checkpoint, without crossing SSH."""
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    waiting_run = 'smoke-checkpoint-wait'
    write_run = 'smoke-checkpoint-write'
    cancel_run = 'smoke-cancel-before-start'
    try:
        for run_id in (waiting_run, write_run, cancel_run):
            delete_run(connection, run_id)

        insert_run(connection, waiting_run, 190007, 'queued')
        insert_step(
            connection, waiting_run, 'smoke-waiting-step',
            'waiting_approval',
        )

        insert_run(connection, write_run, 190008, 'queued')
        insert_step(
            connection, write_run, 'smoke-write-running', 'running',
        )
        append_event(
            connection, write_run, 'write_intent',
            {'step_id': 'smoke-write-running', 'kind': 'shell'},
        )

        insert_run(connection, cancel_run, 190009, 'queued')
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE t_ai_autonomous_run SET cancel_requested = 1
                 WHERE id = %s
                """,
                (cancel_run,),
            )

        # These persisted steps intentionally have no DB 0 checkpoint. The
        # production Worker must derive its response only from MySQL.
        for run_id in (waiting_run, write_run, cancel_run):
            dispatch_run(run_id)

        waiting = wait_for_run(
            connection, waiting_run,
            lambda row: (
                row['status'] == 'waiting_approval'
                and row['lease_owner'] is None
            ),
            'missing checkpoint crossed the pending approval boundary',
        )
        require(int(waiting['revision']) >= 2,
                'waiting boundary did not pass through a real Worker lease')

        unknown = wait_for_run(
            connection, write_run,
            lambda row: (
                row['status'] == 'needs_attention'
                and row['lease_owner'] is None
            ),
            'write intent with missing checkpoint did not fail closed',
        )
        require(int(unknown['revision']) >= 2,
                'write boundary did not pass through a real Worker lease')
        step = fetch_one(
            connection,
            'SELECT status FROM t_ai_autonomous_step WHERE id = %s',
            ('smoke-write-running',),
        )
        require(step['status'] == 'outcome_unknown',
                'interrupted write was not marked outcome_unknown')
        recovery_event = fetch_one(
            connection,
            """
            SELECT COUNT(*) AS count FROM t_ai_autonomous_event
             WHERE run_id = %s
               AND event_type = 'recovery_write_outcome_unknown'
            """,
            (write_run,),
        )
        require(int(recovery_event['count']) == 1,
                'write recovery event was not exactly-once')

        cancelled = wait_for_run(
            connection, cancel_run,
            lambda row: (
                row['status'] == 'cancelled'
                and row['lease_owner'] is None
            ),
            'pre-side-effect cancellation was not confirmed',
        )
        require(bool(cancelled['cancel_requested']),
                'cancel request was lost before terminal confirmation')
        cancel_event = fetch_one(
            connection,
            """
            SELECT COUNT(*) AS count FROM t_ai_autonomous_event
             WHERE run_id = %s AND event_type = 'run_cancelled'
               AND payload_json LIKE '%%confirmed before side effects%%'
            """,
            (cancel_run,),
        )
        require(int(cancel_event['count']) == 1,
                'pre-side-effect cancellation lacks confirmation evidence')
    finally:
        connection.close()


def hold_worker_lock():
    """Hold the interrupted Step row after queuing a real drive task.

    This is infrastructure-only failure injection.  The production Worker can
    consume its Celery message and commit the Run lease, but its recovery
    transaction blocks when it tries to persist the write-outcome boundary.
    The wrapper observes that committed lease before sending SIGKILL.
    """
    setup = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        delete_run(setup, WORKER_KILL_RUN_ID)
        insert_run(setup, WORKER_KILL_RUN_ID, 190010, 'queued')
        insert_step(
            setup, WORKER_KILL_RUN_ID,
            'smoke-worker-kill-step', 'running',
        )
        append_event(
            setup, WORKER_KILL_RUN_ID, 'write_intent',
            {'step_id': 'smoke-worker-kill-step', 'kind': 'shell'},
        )
    finally:
        setup.close()

    locker = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        locker.begin()
        with locker.cursor() as cursor:
            cursor.execute(
                """
                SELECT id FROM t_ai_autonomous_step
                 WHERE id = %s FOR UPDATE
                """,
                ('smoke-worker-kill-step',),
            )
            require(cursor.fetchone() is not None,
                    'worker-kill Step row was not lockable')

        # The Worker is container-paused by the wrapper.  Dispatch first, then
        # publish readiness through another connection, so observing readiness
        # means both the row lock and durable broker message already exist.
        # concurrency=1 leaves the duplicate broker delivery queued while the
        # first task blocks on this row lock.  After immediate Worker restart,
        # that duplicate must retry across the still-live lease boundary.
        for _ in range(2):
            dispatch_run(WORKER_KILL_RUN_ID)
        signal = mysql_connection(os.environ['OGS_MYSQL_HOST'])
        try:
            append_event(
                signal, WORKER_KILL_RUN_ID, 'smoke_lock_ready', {},
            )
        finally:
            signal.close()

        # The wrapper force-removes this one-off container immediately after
        # killing the Worker, which rolls back and releases the row lock.
        time.sleep(900)
    finally:
        locker.rollback()
        locker.close()


def wait_worker_lock_ready():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = fetch_one(
                connection,
                """
                SELECT COUNT(*) AS count FROM t_ai_autonomous_event
                 WHERE run_id = %s AND event_type = 'smoke_lock_ready'
                """,
                (WORKER_KILL_RUN_ID,),
            )
            if int(row['count']) == 1:
                return
            time.sleep(0.1)
        raise AssertionError('worker-kill row-lock fixture was not ready')
    finally:
        connection.close()


def wait_worker_lease():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        row = wait_for_run(
            connection, WORKER_KILL_RUN_ID,
            lambda current: (
                current['status'] == 'running'
                and bool(current['lease_owner'])
                and current['lease_expires_at'] is not None
            ),
            'real Worker did not claim the queued Run before SIGKILL',
            timeout=30,
        )
        print('[S2 smoke] observed real Worker lease owner=%s' % (
            row['lease_owner'],
        ))
        append_event(
            connection, WORKER_KILL_RUN_ID, 'smoke_old_lease_observed', {
                'lease_owner': row['lease_owner'],
                'lease_expires_at': row['lease_expires_at'].isoformat(),
            },
        )
    finally:
        connection.close()


def verify_restart_before_expiry():
    """Prove the replacement Worker was ready while the old lease lived."""
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        row = fetch_one(
            connection,
            """
            SELECT status, revision, lease_owner, lease_expires_at
              FROM t_ai_autonomous_run WHERE id = %s
            """,
            (WORKER_KILL_RUN_ID,),
        )
        require(row['status'] == 'running',
                'replacement Worker crossed the live lease too early')
        require(row['lease_owner'] is not None,
                'SIGKILL residue lost the old lease owner')
        require(
            row['lease_expires_at'] is not None
            and row['lease_expires_at'] > datetime.datetime.utcnow(),
            'replacement Worker was not ready before old lease expiry',
        )
        observed = fetch_one(
            connection,
            """
            SELECT payload_json FROM t_ai_autonomous_event
             WHERE run_id = %s
               AND event_type = 'smoke_old_lease_observed'
            """,
            (WORKER_KILL_RUN_ID,),
        )
        evidence = json.loads(observed['payload_json'])
        require(evidence.get('lease_owner') == row['lease_owner'],
                'old lease owner changed before expiry takeover')
        require(
            evidence.get('lease_expires_at')
            == row['lease_expires_at'].isoformat(),
            'old lease expiry evidence changed before takeover',
        )
    finally:
        connection.close()


def verify_worker_kill_recovery():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        row = wait_for_run(
            connection, WORKER_KILL_RUN_ID,
            lambda current: (
                current['status'] == 'needs_attention'
                and current['lease_owner'] is None
                and current['lease_expires_at'] is None
            ),
            'restarted Worker did not recover expired SIGKILL lease',
            timeout=45,
        )
        require(int(row['revision']) >= 3,
                'recovery did not include original claim and expiry takeover')
        step = fetch_one(
            connection,
            'SELECT status FROM t_ai_autonomous_step WHERE id = %s',
            ('smoke-worker-kill-step',),
        )
        require(step['status'] == 'outcome_unknown',
                'Worker crash recovery replayed an uncertain write')
        recovery_event = fetch_one(
            connection,
            """
            SELECT COUNT(*) AS count FROM t_ai_autonomous_event
             WHERE run_id = %s
               AND event_type = 'recovery_write_outcome_unknown'
            """,
            (WORKER_KILL_RUN_ID,),
        )
        require(int(recovery_event['count']) == 1,
                'Worker crash safety event was not exactly-once')
        unsafe_events = fetch_one(
            connection,
            """
            SELECT COUNT(*) AS count FROM t_ai_autonomous_event
             WHERE run_id = %s
               AND event_type IN ('write_intent', 'step_executed')
            """,
            (WORKER_KILL_RUN_ID,),
        )
        require(int(unsafe_events['count']) == 0,
                'Worker crash recovery produced a remote-side-effect event')
    finally:
        connection.close()


PHASES = {
    'checkpoint-and-cancel': checkpoint_and_cancel,
    'hold-worker-lock': hold_worker_lock,
    'lease-and-boundary': lease_and_boundary,
    'migrate-and-prime': migrate_and_prime,
    'verify-persistence': verify_persistence,
    'verify-restart-before-expiry': verify_restart_before_expiry,
    'verify-worker-kill-recovery': verify_worker_kill_recovery,
    'wait-worker-lease': wait_worker_lease,
    'wait-worker-lock-ready': wait_worker_lock_ready,
    'worker-and-duplicate': worker_and_duplicate,
}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in PHASES:
        choices = ', '.join(sorted(PHASES))
        raise SystemExit('usage: smoke-ai-autonomy-s2.py {%s}' % choices)
    phase = sys.argv[1]
    PHASES[phase]()
    print('[S2 smoke] phase PASS: %s' % phase)


if __name__ == '__main__':
    main()
