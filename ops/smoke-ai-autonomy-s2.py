#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Disposable M1/S2 smoke probe; invoked only by the test Compose stack."""
import concurrent.futures
import datetime
import json
import math
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
LANGGRAPH_THREAD_PREFIX = 'ogs:s2-smoke:langgraph:'
WORKER_KILL_RUN_ID = 'smoke-worker-kill'
SSH_TARGET_HOST = 'ssh-target'
SSH_TARGET_PORT = 2222
SSH_TARGET_HOST_ID = 190020
SSH_TARGET_SYSTEM_USER_ID = 190020
SSH_TARGET_KEY_PATH = 's2-client-key'
SSH_EXIT_RUN_ID = 's2-ssh-exit'
SSH_EXIT_STEP_ID = 's2-ssh-exit-step'
SSH_CANCEL_RUN_ID = 's2-ssh-cancel'
SSH_CANCEL_STEP_ID = 's2-ssh-cancel-step'
SSH_READONLY_RUN_ID = 's2-ssh-readonly-kill'
SSH_READONLY_STEP_ID = 's2-ssh-readonly-step'
SSH_REVOCATION_RUN_ID = 's2-ssh-runtime-revocation'
SSH_REVOCATION_STEP_ID = 's2-ssh-runtime-revocation-step'
SSH_PATCH_RUN_ID = 's2-ssh-file-patch-restore'
SSH_PATCH_STEP_ID = 's2-ssh-file-patch-step'
SSH_RESTORE_STEP_ID = 's2-ssh-file-restore-step'
SSH_WRITE_RUN_ID = 's2-ssh-write-kill'
SSH_WRITE_STEP_ID = 's2-ssh-write-step'
SSH_PREINTENT_RUN_ID = 's2-ssh-pre-intent-kill'
SSH_PREINTENT_STEP_ID = 's2-ssh-pre-intent-step'
SSH_SYMLINK_RUN_ID = 's2-ssh-symlink'
SSH_SYMLINK_STEP_ID = 's2-ssh-symlink-step'
PRODUCTION_CHECKPOINT_RUN_ID = 's2-production-checkpoint-loss'
WORKER_TIMER_RUN_ID = 's2-worker-timer-recovery'
SSH_EXACT_STDOUT = 's2-exact-stdout'
SSH_EXACT_STDERR = 's2-exact-stderr'
SSH_SYMLINK_MARKER = 's2-symlink-outside-marker'


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


def insert_ssh_action_run(
    connection, run_id, step_id, kind, parameters, *, timeout_seconds=60,
):
    """Insert one immutable approved action at the production resume seam."""
    delete_run(connection, run_id)
    budget = {
        'duration_seconds': 3600,
        'max_loops': 20,
        'max_actions': 30,
        'command_timeout_seconds': 600,
        'step_output_bytes': 65536,
        'run_artifact_bytes': 2097152,
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO t_ai_autonomous_run
                (id, owner, goal, host_id, host_alias, system_user_id,
                 system_user_alias, mode, status, revision, budget_json,
                 latest_event_seq, graph_version)
            VALUES
                (%s, 'admin', 'disposable real SSH gate', %s,
                 's2-ssh-target', %s, 's2-ssh-user', 'ask', 'queued',
                 0, %s, 0, 'v2')
            """,
            (
                run_id, SSH_TARGET_HOST_ID, SSH_TARGET_SYSTEM_USER_ID,
                json.dumps(budget, sort_keys=True),
            ),
        )
    insert_ssh_action_step(
        connection, run_id, step_id, kind, parameters,
        seq=1, timeout_seconds=timeout_seconds,
    )


def insert_ssh_action_step(
    connection, run_id, step_id, kind, parameters, *, seq,
    timeout_seconds=60,
):
    """Append another immutable approved action to an active smoke Run."""
    from app.ai.autonomy.actions import (
        StructuredAction, build_action_digest,
    )
    from app.core import config

    action = StructuredAction(
        kind=kind,
        target_id=SSH_TARGET_HOST_ID,
        system_user_id=SSH_TARGET_SYSTEM_USER_ID,
        parameters=parameters,
        timeout_seconds=timeout_seconds,
        step_id=step_id,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO t_ai_autonomous_step
                (id, run_id, kind, status, seq, summary, action_json,
                 action_digest, note)
            VALUES
                (%s, %s, 'action', 'approved', %s,
                 'disposable real SSH action', %s, %s, '')
            """,
            (
                step_id, run_id, int(seq),
                json.dumps(action.to_canonical_dict(), sort_keys=True),
                build_action_digest(action, config.FLASK_SECRET_KEY),
            ),
        )


def wait_for_step(
    connection, step_id, predicate, description, timeout=60,
):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = fetch_one(
            connection,
            """
            SELECT status, note FROM t_ai_autonomous_step WHERE id = %s
            """,
            (step_id,),
        )
        if predicate(row):
            return row
        time.sleep(0.1)
    raise AssertionError('%s; last row=%r' % (description, row))


def _run_remote(command, *, timeout_seconds=15):
    from app.ai.autonomy.ssh_runner import run_ssh_command
    from app.app_factory import app as flask_app

    with flask_app.app_context():
        return run_ssh_command(
            command,
            host=SSH_TARGET_HOST,
            port=SSH_TARGET_PORT,
            system_user_id=SSH_TARGET_SYSTEM_USER_ID,
            timeout_seconds=timeout_seconds,
            max_output_bytes=16384,
        )


def _require_remote_exit(result, exit_code=0):
    require(result.started, 'real OpenSSH command never started')
    require(
        result.stop_confirmed,
        'real OpenSSH command did not finish cleanly',
    )
    require(result.termination == 'exited',
            'real OpenSSH command had an unexpected termination')
    require(result.exit_code == exit_code,
            'real OpenSSH command returned the wrong exit status')


def _remote_process_groups(command_name):
    result = _run_remote(
        'ps -C %s -o pgid=' % command_name,
    )
    require(result.termination == 'exited',
            'remote process-group query did not exit')
    require(result.exit_code in (0, 1),
            'remote process-group query failed unexpectedly')
    groups = set()
    for value in result.stdout.split():
        try:
            groups.add(int(value))
        except ValueError:
            raise AssertionError('remote process-group query was malformed')
    return groups


def _all_remote_process_groups():
    result = _run_remote('ps -eo pgid=')
    _require_remote_exit(result)
    groups = set()
    for value in result.stdout.split():
        try:
            groups.add(int(value))
        except ValueError:
            raise AssertionError('remote process table was malformed')
    return groups


def _artifact_contents(connection, run_id, step_id):
    from app.tools.basesec import decrypt_secret

    rows = fetch_all(
        connection,
        """
        SELECT kind, content_ciphertext
          FROM t_ai_autonomous_artifact
         WHERE run_id = %s AND step_id = %s
         ORDER BY kind, id
        """,
        (run_id, step_id),
    )
    return {
        row['kind']: str(decrypt_secret(row['content_ciphertext']) or '')
        for row in rows
    }


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
                   lease_token IS NOT NULL AS lease_present,
                   lease_expires_at
              FROM t_ai_autonomous_run WHERE id = %s
            """,
            (run_id,),
        )
        if predicate(row):
            return row
        time.sleep(0.1)
    raise AssertionError('%s; last row=%r' % (description, row))


def _feature_off_side_effect_counts(connection):
    run_count = fetch_one(
        connection,
        'SELECT COUNT(*) AS count FROM t_ai_autonomous_run',
    )
    command_count = fetch_one(
        connection,
        'SELECT COUNT(*) AS count FROM t_command_log',
    )
    return (int(run_count['count']), int(command_count['count']))


def feature_off_isolation():
    """Exercise existing HTTP routes with autonomy infrastructure absent."""
    from app.core import config
    import init as backend_init

    require(
        config.AI_AUTONOMY_ENABLED is False,
        'feature-off probe did not start with autonomy disabled',
    )
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        before = _feature_off_side_effect_counts(connection)
        backend_init.orange_init_api()
        client = backend_init.app.test_client()

        health = client.get('/local/health')
        require(health.status_code == 200, 'feature-off health request failed')
        require(
            health.get_json()['status'] == 'ok',
            'feature-off health response was malformed',
        )

        # Authenticate through the same public captcha/login flow as the UI.
        # This proves business Redis, MySQL and the normal session/CSRF
        # boundaries remain usable while the separate autonomy dependencies
        # are absent.
        captcha = client.post('/local/captcha/get')
        require(
            captcha.status_code == 200,
            'feature-off captcha request failed',
        )
        captcha_data = captcha.get_json()
        require(captcha_data['code'] == 0, 'feature-off captcha was rejected')
        expression = str(captcha_data.get('captcha_expr') or '').split()
        require(
            len(expression) == 5
            and expression[1] in ('+', '-')
            and expression[3:] == ['=', '?'],
            'feature-off captcha expression was malformed',
        )
        left = int(expression[0])
        right = int(expression[2])
        answer = left + right if expression[1] == '+' else left - right
        login = client.post('/account/login_dl2', json={
            'username': 'admin',
            'password': 'admin',
            'captcha_id': captcha_data['captcha_id'],
            'captcha_answer': str(answer),
        })
        require(login.status_code == 200, 'feature-off login request failed')
        require(
            login.get_json()['code'] == 0,
            'feature-off login did not create a business session',
        )
        csrf_cookie = client.get_cookie('csrf_token')
        require(
            csrf_cookie is not None,
            'feature-off login omitted CSRF state',
        )
        csrf_headers = {'X-CSRF-Token': csrf_cookie.value}

        diagnostics = client.get('/ai/diagnostic-profiles')
        require(
            diagnostics.status_code == 200
            and isinstance(diagnostics.get_json().get('profiles'), list),
            'feature-off diagnostic profiles were unavailable',
        )

        inventory = client.post(
            '/server/host/list_all', headers=csrf_headers, json={},
        )
        require(
            inventory.status_code == 200
            and inventory.get_json().get('code') == 0,
            'feature-off asset inventory was unavailable',
        )

        # A non-existent conversation exercises the authenticated chat SSE
        # route and business Redis without requiring an external model.
        chat = client.post('/ai/chat', headers=csrf_headers, json={
            'conversation_id': 's2-feature-off-missing-conversation',
            'message': 'smoke',
        })
        require(
            chat.status_code == 200,
            'feature-off chat route was unavailable',
        )
        require(
            'event: run.failed' in chat.get_data(as_text=True),
            'feature-off chat did not return its stable business failure',
        )

        # The legacy batch route must still reach its own safety policy.  Use
        # a rejected command so this evidence cannot execute a remote effect.
        batch = client.post('/server/group/cmd', headers=csrf_headers, json={
            'group': 's2-feature-off-no-target',
            'command': 'rm -rf /',
        })
        require(
            batch.status_code == 200,
            'feature-off batch route was unavailable',
        )
        require(
            batch.get_json()['code'] == 100
            and 'dangerous command blocked:' in batch.get_json()['msg'],
            'feature-off batch request bypassed its safety policy',
        )

        after = _feature_off_side_effect_counts(connection)
        require(
            after == before,
            'feature-off HTTP requests produced an unexpected side effect',
        )
    finally:
        connection.close()


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


def _langgraph_config():
    return {
        'configurable': {
            'thread_id': (
                LANGGRAPH_THREAD_PREFIX
                + os.environ['OGS_S2_SMOKE_GIT_HEAD']
            ),
        },
    }


def _build_interrupt_graph():
    """Build the same two-gate graph in every disposable probe process."""
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    def first_gate(state):
        decision = interrupt({'gate': 'first'})
        return {
            'phase': 'first-resumed',
            'resumes': [str(decision)],
        }

    def second_gate(state):
        decision = interrupt({'gate': 'second'})
        return {
            'phase': 'completed',
            'resumes': list(state.get('resumes') or []) + [str(decision)],
        }

    builder = StateGraph(dict)
    builder.add_node('first_gate', first_gate)
    builder.add_node('second_gate', second_gate)
    builder.add_edge(START, 'first_gate')
    builder.add_edge('first_gate', 'second_gate')
    builder.add_edge('second_gate', END)
    return builder


def _open_interrupt_graph():
    from langgraph.checkpoint.redis import ShallowRedisSaver

    from app.ai.autonomy.drive import autonomy_checkpoint_url

    manager = ShallowRedisSaver.from_conn_string(
        autonomy_checkpoint_url(),
    )
    saver = manager.__enter__()
    saver.setup()
    return manager, _build_interrupt_graph().compile(checkpointer=saver)


def _require_interrupt(state, node, gate):
    require(state.next == (node,), 'LangGraph stopped at the wrong node')
    require(len(state.interrupts) == 1,
            'LangGraph did not expose exactly one interrupt')
    require(state.interrupts[0].value == {'gate': gate},
            'LangGraph exposed the wrong interrupt payload')


def langgraph_pause_first():
    manager, graph = _open_interrupt_graph()
    try:
        graph.invoke(
            {'phase': 'new', 'resumes': []},
            _langgraph_config(),
        )
        state = graph.get_state(_langgraph_config())
        _require_interrupt(state, 'first_gate', 'first')
        require(list(state.values.get('resumes') or []) == [],
                'first interrupt persisted a fabricated resume value')
    finally:
        manager.__exit__(None, None, None)


def langgraph_resume_to_second():
    from langgraph.types import Command

    manager, graph = _open_interrupt_graph()
    try:
        graph.invoke(
            Command(resume='resume-first'),
            _langgraph_config(),
        )
        state = graph.get_state(_langgraph_config())
        require(state.next == ('second_gate',),
                'first resume did not stop at the second interrupt')
        _require_interrupt(state, 'second_gate', 'second')
        require(
            list(state.values.get('resumes') or []) == ['resume-first'],
            'first resume value was not checkpointed exactly',
        )
        db0 = redis_client(0)
        try:
            persisted = db0.execute_command('WAITAOF', 1, 0, 10000)
            require(
                isinstance(persisted, (list, tuple))
                and int(persisted[0]) >= 1,
                'second interrupt checkpoint was not AOF-fsynced',
            )
        finally:
            db0.close()
    finally:
        manager.__exit__(None, None, None)


def langgraph_resume_after_restart():
    from langgraph.types import Command

    # This saver and compiled graph are intentionally new objects created only
    # after the Redis 8 process restart in the wrapper.
    manager, graph = _open_interrupt_graph()
    try:
        before = graph.get_state(_langgraph_config())
        _require_interrupt(before, 'second_gate', 'second')
        require(
            list(before.values.get('resumes') or []) == ['resume-first'],
            'new saver instance did not recover the first resume from AOF',
        )
        graph.invoke(
            Command(resume='resume-second'),
            _langgraph_config(),
        )
        state = graph.get_state(_langgraph_config())
        require(not state.next and not state.interrupts,
                'second resume did not complete the graph')
        require(state.values.get('phase') == 'completed',
                'completed graph has the wrong final phase')
        require(
            list(state.values.get('resumes') or [])
            == ['resume-first', 'resume-second'],
            'new saver instance did not complete with both exact resumes',
        )
    finally:
        manager.__exit__(None, None, None)


def ssh_prime_target():
    """Create only opaque asset/credential references for the SSH fixture."""
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        for run_id in (
            SSH_EXIT_RUN_ID, SSH_CANCEL_RUN_ID,
            SSH_READONLY_RUN_ID,
            SSH_REVOCATION_RUN_ID,
            SSH_PATCH_RUN_ID,
            SSH_WRITE_RUN_ID, SSH_PREINTENT_RUN_ID,
            SSH_SYMLINK_RUN_ID, PRODUCTION_CHECKPOINT_RUN_ID,
            WORKER_TIMER_RUN_ID,
        ):
            delete_run(connection, run_id)
        with connection.cursor() as cursor:
            cursor.execute(
                'DELETE FROM t_host WHERE id = %s OR alias = %s',
                (SSH_TARGET_HOST_ID, 's2-ssh-target'),
            )
            cursor.execute(
                'DELETE FROM t_sys_user WHERE id = %s OR alias = %s',
                (SSH_TARGET_SYSTEM_USER_ID, 's2-ssh-user'),
            )
            cursor.execute(
                """
                INSERT INTO t_host
                    (id, alias, host_ip, host_port, `group`, ai_environment)
                VALUES (%s, 's2-ssh-target', %s, %s, NULL, 'lab')
                """,
                (SSH_TARGET_HOST_ID, SSH_TARGET_HOST, SSH_TARGET_PORT),
            )
            cursor.execute(
                """
                INSERT INTO t_sys_user
                    (id, alias, host_user, host_password, host_key,
                     agreement, remarks)
                VALUES
                    (%s, 's2-ssh-user', 's2agent', NULL, %s,
                     'ssh', 'disposable S2')
                """,
                (SSH_TARGET_SYSTEM_USER_ID, SSH_TARGET_KEY_PATH),
            )
    finally:
        connection.close()

    identity = _run_remote('id -u; id -G')
    _require_remote_exit(identity)
    lines = identity.stdout.splitlines()
    require(len(lines) == 2, 'low-privilege identity output was malformed')
    require(int(lines[0].strip()) == 10001,
            'OpenSSH target did not execute as the fixture user')
    groups = {int(value) for value in lines[1].split()}
    require(0 not in groups,
            'OpenSSH target fixture user retained the root group')
    fixtures = _run_remote(
        'test -L /opt/s2-smoke/bounded-link '
        '&& test -x /usr/local/bin/uptime '
        '&& test -e /opt/s2-smoke/slow-uptime',
    )
    _require_remote_exit(fixtures)


def production_checkpoint_loss_boundary():
    """Delete a checkpoint created by the production graph, then recover."""
    from app.ai.autonomy.drive import (
        AutonomyDriver,
        RESULT_PAUSED,
        THREAD_ID_PREFIX,
        make_autonomy_heartbeat_session_factory,
        make_autonomy_saver_factory,
    )
    from app.ai.autonomy.lease import RunLeaseService
    from app.ai.autonomy.repository import AutonomyRepository
    from app.app_factory import app as flask_app
    from app.core import config
    from app.core.db.database import (
        db,
        t_ai_autonomous_run,
        t_ai_autonomous_step,
    )

    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    db0 = redis_client(0)
    thread_id = THREAD_ID_PREFIX + PRODUCTION_CHECKPOINT_RUN_ID
    try:
        delete_run(connection, PRODUCTION_CHECKPOINT_RUN_ID)
        before_keys = set(db0.scan_iter(match='*', count=100))
        budget = {
            'duration_seconds': 3600,
            'max_loops': 20,
            'max_actions': 30,
            'command_timeout_seconds': 60,
            'step_output_bytes': 65536,
            'run_artifact_bytes': 2097152,
        }
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t_ai_autonomous_run
                    (id, owner, goal, host_id, host_alias, system_user_id,
                     system_user_alias, mode, status, revision, budget_json,
                     latest_event_seq, graph_version)
                VALUES
                    (%s, 'admin', 'production checkpoint loss gate', %s,
                     's2-ssh-target', %s, 's2-ssh-user', 'ask', 'queued',
                     0, %s, 0, 'v1')
                """,
                (
                    PRODUCTION_CHECKPOINT_RUN_ID,
                    SSH_TARGET_HOST_ID,
                    SSH_TARGET_SYSTEM_USER_ID,
                    json.dumps(budget, sort_keys=True),
                ),
            )

        def planner(context):
            return [context['repo'].propose_probe(
                'admin', 'admin', PRODUCTION_CHECKPOINT_RUN_ID,
                'system.load', {},
            )]

        with flask_app.app_context():
            claimed = RunLeaseService(db.session).claim_run(
                PRODUCTION_CHECKPOINT_RUN_ID,
                's2-production-checkpoint-seed',
                config.AI_AUTONOMY_LEASE_TTL_SECONDS,
            )
            require(
                claimed is not None,
                'production graph Run was not claimed',
            )
            driver = AutonomyDriver(
                db.session,
                config.FLASK_SECRET_KEY,
                planner=planner,
                saver_factory=make_autonomy_saver_factory(),
                heartbeat_session_factory=(
                    make_autonomy_heartbeat_session_factory()
                ),
                worker_id='s2-production-checkpoint-seed',
            )
            require(
                driver.drive(PRODUCTION_CHECKPOINT_RUN_ID, claimed)
                == RESULT_PAUSED,
                'production graph did not persist its approval checkpoint',
            )

            after_keys = set(db0.scan_iter(match='*', count=100))
            created_checkpoint_keys = after_keys - before_keys
            require(
                created_checkpoint_keys,
                'production graph did not create any DB 0 checkpoint key',
            )
            require(
                all(thread_id in key for key in created_checkpoint_keys),
                'new DB 0 keys were not scoped to the production thread',
            )
            persisted = db0.execute_command('WAITAOF', 1, 0, 10000)
            require(
                isinstance(persisted, (list, tuple))
                and int(persisted[0]) >= 1,
                'production approval checkpoint was not AOF-fsynced',
            )

            run = db.session.query(t_ai_autonomous_run).filter_by(
                id=PRODUCTION_CHECKPOINT_RUN_ID,
            ).first()
            step = db.session.query(t_ai_autonomous_step).filter_by(
                run_id=PRODUCTION_CHECKPOINT_RUN_ID,
                status='waiting_approval',
            ).one()
            AutonomyRepository(
                db.session, config.FLASK_SECRET_KEY,
            ).decide(
                'admin', 'admin', PRODUCTION_CHECKPOINT_RUN_ID,
                step.id, 'approve', int(run.revision),
            )

        queued = fetch_one(
            connection,
            """
            SELECT r.status, s.status AS step_status
              FROM t_ai_autonomous_run r
              JOIN t_ai_autonomous_step s ON s.run_id = r.id
             WHERE r.id = %s
            """,
            (PRODUCTION_CHECKPOINT_RUN_ID,),
        )
        require(
            queued['status'] == 'queued'
            and queued['step_status'] == 'approved',
            'MySQL did not retain the approved safe boundary',
        )

        deleted = db0.delete(*created_checkpoint_keys)
        require(
            int(deleted) == len(created_checkpoint_keys),
            'did not delete the complete production thread checkpoint',
        )
        remaining_checkpoint_keys = (
            set(db0.scan_iter(match='*', count=100))
            & created_checkpoint_keys
        )
        require(
            remaining_checkpoint_keys == set(),
            'production thread checkpoint keys survived deletion',
        )

        dispatch_run(PRODUCTION_CHECKPOINT_RUN_ID)
        wait_for_run(
            connection,
            PRODUCTION_CHECKPOINT_RUN_ID,
            lambda row: (
                row['status'] == 'completed'
                and row['lease_owner'] is None
                and not bool(row['lease_present'])
            ),
            'production checkpoint loss did not rebuild from MySQL',
            timeout=90,
        )
        events = fetch_one(
            connection,
            """
            SELECT
              SUM(event_type = 'recovery_boundary_rebuild') AS boundary_count,
              SUM(event_type = 'step_execution_started') AS started_count,
              SUM(event_type = 'step_executed') AS executed_count,
              SUM(event_type = 'planner_unavailable') AS planner_count
              FROM t_ai_autonomous_event
             WHERE run_id = %s
            """,
            (PRODUCTION_CHECKPOINT_RUN_ID,),
        )
        require(int(events['boundary_count'] or 0) == 1,
                'MySQL safe-boundary rebuild was not exactly once')
        require(int(events['started_count'] or 0) == 1,
                'rebuilt production probe started more than once')
        require(int(events['executed_count'] or 0) == 1,
                'rebuilt production probe did not execute exactly once')
        require(int(events['planner_count'] or 0) == 0,
                'checkpoint recovery incorrectly re-entered planning')
    finally:
        db0.close()
        connection.close()


def ssh_runtime_environment_revocation():
    """Revoke the lab boundary while a real read-only SSH action runs."""
    from app.ai.autonomy.drive import make_autonomy_heartbeat_session_factory
    from app.ai.autonomy.executor import AutonomyExecutor
    from app.app_factory import app as flask_app
    from app.core import config
    from app.core.db.database import db
    from sqlalchemy.orm import Session

    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    marker = _run_remote('touch /opt/s2-smoke/slow-uptime')
    _require_remote_exit(marker)
    insert_ssh_action_run(
        connection,
        SSH_REVOCATION_RUN_ID,
        SSH_REVOCATION_STEP_ID,
        'probe',
        {'probe_id': 'system.load'},
        timeout_seconds=120,
    )

    def execute_real_step():
        with flask_app.app_context():
            session = Session(bind=db.engine)
            try:
                return AutonomyExecutor(
                    session, config.FLASK_SECRET_KEY,
                ).execute_step(
                    'admin', 'admin',
                    SSH_REVOCATION_RUN_ID,
                    SSH_REVOCATION_STEP_ID,
                    control_session_factory=(
                        make_autonomy_heartbeat_session_factory()
                    ),
                )
            finally:
                session.close()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execute_real_step)
            wait_for_step(
                connection,
                SSH_REVOCATION_STEP_ID,
                lambda row: row['status'] == 'running',
                'real SSH revocation step did not enter running',
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if len(_remote_process_groups('sleep')) == 1:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(
                    'real SSH payload did not start before '
                    'environment revocation'
                )

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE t_host SET ai_environment = 'production'
                     WHERE id = %s
                    """,
                    (SSH_TARGET_HOST_ID,),
                )
            result = future.result(timeout=30)

        require(
            result.get('termination') == 'authorization_revoked',
            'runtime environment change did not revoke execution',
        )
        row = fetch_one(
            connection,
            """
            SELECT r.status, s.status AS step_status
              FROM t_ai_autonomous_run r
              JOIN t_ai_autonomous_step s ON s.run_id = r.id
             WHERE r.id = %s AND s.id = %s
            """,
            (SSH_REVOCATION_RUN_ID, SSH_REVOCATION_STEP_ID),
        )
        require(
            row['status'] == 'failed' and row['step_status'] == 'failed',
            'revoked read action did not fail closed',
        )
        event = fetch_one(
            connection,
            """
            SELECT payload_json FROM t_ai_autonomous_event
             WHERE run_id = %s
               AND event_type = 'execution_authorization_revoked'
            """,
            (SSH_REVOCATION_RUN_ID,),
        )
        payload = json.loads(event['payload_json'])
        require(payload.get('stop_confirmed') is True,
                'authorization revocation did not confirm remote stop')
        events = fetch_one(
            connection,
            """
            SELECT
              SUM(event_type = 'execution_authorization_revoked')
                  AS revoked_count,
              SUM(event_type = 'write_intent') AS intent_count
              FROM t_ai_autonomous_event WHERE run_id = %s
            """,
            (SSH_REVOCATION_RUN_ID,),
        )
        require(int(events['revoked_count'] or 0) == 1,
                'runtime authorization revocation was not exactly once')
        require(int(events['intent_count'] or 0) == 0,
                'read-only revocation unexpectedly persisted write intent')
        require(_remote_process_groups('sleep') == set(),
                'revoked SSH process group survived control stop')
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE t_host SET ai_environment = 'lab'
                 WHERE id = %s
                """,
                (SSH_TARGET_HOST_ID,),
            )
        delete_run(connection, SSH_REVOCATION_RUN_ID)
        connection.close()


def ssh_file_patch_restore():
    """Patch and restore a real remote file through production actions."""
    from app.ai.autonomy.actions import patch_backup_path
    from app.ai.autonomy.drive import make_autonomy_heartbeat_session_factory
    from app.ai.autonomy.executor import AutonomyExecutor
    from app.app_factory import app as flask_app
    from app.core import config
    from app.core.db.database import db
    from sqlalchemy.orm import Session

    path = '/opt/s2-smoke/managed.conf'
    original = 'mode=before\n'
    replacement = 'mode=after\n'
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])

    def execute_action(step_id):
        with flask_app.app_context():
            session = Session(bind=db.engine)
            try:
                return AutonomyExecutor(
                    session, config.FLASK_SECRET_KEY,
                ).execute_step(
                    'admin', 'admin', SSH_PATCH_RUN_ID, step_id,
                    control_session_factory=(
                        make_autonomy_heartbeat_session_factory()
                    ),
                )
            finally:
                session.close()

    try:
        prepared = _run_remote(
            "printf '%s\\n' mode=before > /opt/s2-smoke/managed.conf; "
            'rm -rf /opt/s2-smoke/.ogs-autonomy-backup',
        )
        _require_remote_exit(prepared)
        insert_ssh_action_run(
            connection,
            SSH_PATCH_RUN_ID,
            SSH_PATCH_STEP_ID,
            'file_patch',
            {'path': path, 'content': replacement},
        )
        patched_result = execute_action(SSH_PATCH_STEP_ID)
        require(
            patched_result.get('step_status') == 'succeeded',
            'production file_patch action did not succeed',
        )
        patched = _run_remote('cat -- /opt/s2-smoke/managed.conf')
        _require_remote_exit(patched)
        require(patched.stdout == replacement,
                'production file_patch did not write exact content')

        backup_path = patch_backup_path(
            path, SSH_PATCH_RUN_ID, SSH_PATCH_STEP_ID,
        )
        artifacts = _artifact_contents(
            connection, SSH_PATCH_RUN_ID, SSH_PATCH_STEP_ID,
        )
        require(
            '-mode=before' in artifacts['patch_diff']
            and '+mode=after' in artifacts['patch_diff'],
            'encrypted patch_diff did not contain the unified change',
        )
        require(artifacts['backup_ref'] == backup_path,
                'backup_ref was not the complete deterministic reference')
        artifact_rows = {
            row['kind']: row
            for row in fetch_all(
                connection,
                """
                SELECT kind, content_ciphertext, size_bytes, truncated
                  FROM t_ai_autonomous_artifact
                 WHERE run_id = %s AND step_id = %s
                   AND kind IN ('patch_diff', 'backup_ref')
                """,
                (SSH_PATCH_RUN_ID, SSH_PATCH_STEP_ID),
            )
        }
        require(set(artifact_rows) == {'patch_diff', 'backup_ref'},
                'file_patch omitted required encrypted artifacts')
        require(not bool(artifact_rows['patch_diff']['truncated']),
                'patch_diff was unexpectedly truncated')
        require(not bool(artifact_rows['backup_ref']['truncated']),
                'backup_ref was unexpectedly truncated')
        require(
            int(artifact_rows['backup_ref']['size_bytes'])
            == len(backup_path.encode('utf-8')),
            'backup_ref size proves it was not stored completely',
        )
        require(
            'mode=before'
            not in artifact_rows['patch_diff']['content_ciphertext'],
            'mode=before leaked into plaintext artifact storage',
        )
        require(
            'mode=after'
            not in artifact_rows['patch_diff']['content_ciphertext'],
            'mode=after leaked into plaintext artifact storage',
        )
        require(
            backup_path
            not in artifact_rows['backup_ref']['content_ciphertext'],
            'backup reference leaked into plaintext artifact storage',
        )

        insert_ssh_action_step(
            connection,
            SSH_PATCH_RUN_ID,
            SSH_RESTORE_STEP_ID,
            'file_restore',
            {'path': path, 'backup_path': backup_path},
            seq=2,
        )
        restore_result = execute_action(SSH_RESTORE_STEP_ID)
        require(
            restore_result.get('step_status') == 'succeeded',
            'production file_restore action did not succeed',
        )
        restored = _run_remote('cat -- /opt/s2-smoke/managed.conf')
        _require_remote_exit(restored)
        require(restored.stdout == 'mode=before\n',
                'file_restore did not restore the original content exactly')
        require(restored.stdout == original,
                'file_restore result differs from the fixture original')
    finally:
        delete_run(connection, SSH_PATCH_RUN_ID)
        connection.close()


def ssh_exit_and_streams():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        command = (
            "printf '%s\\n' " + SSH_EXACT_STDOUT
            + "; printf '%s\\n' " + SSH_EXACT_STDERR
            + ' >&2; exit 23'
        )
        insert_ssh_action_run(
            connection, SSH_EXIT_RUN_ID, SSH_EXIT_STEP_ID,
            'shell', {'command': command},
        )
        dispatch_run(SSH_EXIT_RUN_ID)
        wait_for_step(
            connection, SSH_EXIT_STEP_ID,
            lambda row: row['status'] == 'failed',
            'production SSH step did not retain the exact non-zero exit',
        )
        wait_for_run(
            connection, SSH_EXIT_RUN_ID,
            lambda row: (
                row['status'] in TERMINAL_STATUSES
                and row['lease_owner'] is None
            ),
            'production Driver did not settle the exact-exit Run',
        )
        event = fetch_one(
            connection,
            """
            SELECT payload_json FROM t_ai_autonomous_event
             WHERE run_id = %s AND event_type = 'step_executed'
            """,
            (SSH_EXIT_RUN_ID,),
        )
        require(json.loads(event['payload_json']).get('exit_code') == 23,
                'production Executor lost the exact SSH exit code')
        artifacts = _artifact_contents(
            connection, SSH_EXIT_RUN_ID, SSH_EXIT_STEP_ID,
        )
        require(artifacts.get('step_stdout') == SSH_EXACT_STDOUT + '\n',
                's2-exact-stdout was not preserved in its own artifact')
        require(artifacts.get('step_stderr') == SSH_EXACT_STDERR + '\n',
                's2-exact-stderr was not preserved in its own artifact')
        started = fetch_one(
            connection,
            """
            SELECT
              SUM(event_type = 'step_execution_started') AS started_count,
              SUM(event_type = 'write_intent') AS intent_count
              FROM t_ai_autonomous_event WHERE run_id = %s
            """,
            (SSH_EXIT_RUN_ID,),
        )
        require(int(started['started_count'] or 0) == 1,
                'production Driver started the exact-exit step more than once')
        require(int(started['intent_count'] or 0) == 1,
                'production Executor did not persist one shell write intent')
    finally:
        connection.close()


def _request_cancel(run_id):
    from app.ai.autonomy.repository import AutonomyRepository
    from app.app_factory import app as flask_app
    from app.core import config
    from app.core.db.database import db

    with flask_app.app_context():
        AutonomyRepository(
            db.session, config.FLASK_SECRET_KEY,
        ).request_cancel('admin', 'admin', run_id)


def ssh_cancel_process_group():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        insert_ssh_action_run(
            connection, SSH_CANCEL_RUN_ID, SSH_CANCEL_STEP_ID,
            'probe', {'probe_id': 'system.load'},
            timeout_seconds=120,
        )
        dispatch_run(SSH_CANCEL_RUN_ID)
        wait_for_step(
            connection, SSH_CANCEL_STEP_ID,
            lambda row: row['status'] == 'running',
            'blocking system.load probe did not enter running',
        )

        deadline = time.monotonic() + 30
        observed_groups = set()
        while time.monotonic() < deadline:
            observed_groups = _remote_process_groups('sleep')
            if observed_groups:
                break
            time.sleep(0.1)
        require(len(observed_groups) == 1,
                'could not isolate the running probe process group')

        _request_cancel(SSH_CANCEL_RUN_ID)
        cancelled = wait_for_run(
            connection, SSH_CANCEL_RUN_ID,
            lambda row: (
                row['status'] == 'cancelled'
                and row['lease_owner'] is None
            ),
            'production Driver did not confirm in-flight cancellation',
        )
        require(bool(cancelled['cancel_requested']),
                'production cancellation request was lost')
        step = wait_for_step(
            connection, SSH_CANCEL_STEP_ID,
            lambda row: row['status'] == 'cancelled',
            'read-only SSH step was not cancelled after remote stop',
        )
        require('remote stop confirmed' in str(step['note']),
                'cancelled Step lacks remote-stop confirmation')

        deadline = time.monotonic() + 10
        surviving = set(observed_groups)
        while time.monotonic() < deadline:
            surviving = observed_groups & _all_remote_process_groups()
            if not surviving:
                break
            time.sleep(0.1)
        require(
            not surviving,
            'remote process group survived confirmed cancellation',
        )
        event = fetch_one(
            connection,
            """
            SELECT payload_json FROM t_ai_autonomous_event
             WHERE run_id = %s AND event_type = 'step_cancelled'
            """,
            (SSH_CANCEL_RUN_ID,),
        )
        require(
            json.loads(event['payload_json']).get('stop_confirmed') is True,
            'step_cancelled event did not bind stop confirmation',
        )
    finally:
        connection.close()


def ssh_start_readonly():
    """Start a real server-owned read probe that will be SIGKILLed."""
    marker = _run_remote('touch /opt/s2-smoke/slow-uptime')
    _require_remote_exit(marker)
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        insert_ssh_action_run(
            connection, SSH_READONLY_RUN_ID, SSH_READONLY_STEP_ID,
            'probe', {'probe_id': 'system.load'},
            timeout_seconds=120,
        )
        dispatch_run(SSH_READONLY_RUN_ID)
    finally:
        connection.close()


def wait_ssh_readonly_started():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        wait_for_step(
            connection, SSH_READONLY_STEP_ID,
            lambda row: row['status'] == 'running',
            'read-only SSH probe did not enter running before SIGKILL',
        )
        wait_for_run(
            connection, SSH_READONLY_RUN_ID,
            lambda row: (
                row['status'] == 'running'
                and bool(row['lease_owner'])
                and bool(row['lease_present'])
            ),
            'read-only SSH probe did not retain a fenced Worker lease',
        )
        deadline = time.monotonic() + 30
        groups = set()
        while time.monotonic() < deadline:
            groups = _remote_process_groups('sleep')
            if groups:
                break
            time.sleep(0.1)
        require(len(groups) == 1,
                'read-only SSH payload did not start before SIGKILL')
        events = fetch_one(
            connection,
            """
            SELECT
              SUM(event_type = 'step_execution_started') AS started_count,
              SUM(event_type = 'write_intent') AS intent_count
              FROM t_ai_autonomous_event WHERE run_id = %s
            """,
            (SSH_READONLY_RUN_ID,),
        )
        require(int(events['started_count'] or 0) == 1,
                'read-only first attempt was not durably started once')
        require(int(events['intent_count'] or 0) == 0,
                'read-only probe incorrectly persisted a write intent')
    finally:
        connection.close()


def release_ssh_readonly_first_attempt():
    """Prove the killed Worker's SSH payload stopped before allowing retry."""
    deadline = time.monotonic() + 15
    groups = set()
    while time.monotonic() < deadline:
        groups = _remote_process_groups('sleep')
        if not groups:
            break
        time.sleep(0.1)
    require(not groups,
            'SIGKILLed read-only Worker left its remote payload running')
    marker = _run_remote('rm -f /opt/s2-smoke/slow-uptime')
    _require_remote_exit(marker)


def verify_ssh_readonly_recovery():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        wait_for_run(
            connection, SSH_READONLY_RUN_ID,
            lambda row: (
                row['status'] == 'completed'
                and row['lease_owner'] is None
                and not bool(row['lease_present'])
            ),
            'read-only SIGKILL recovery did not complete',
            timeout=90,
        )
        step = wait_for_step(
            connection, SSH_READONLY_STEP_ID,
            lambda row: row['status'] == 'succeeded',
            'retried read-only SSH probe did not succeed',
        )
        require('exit_code=0' in str(step['note']),
                'retried read-only SSH probe lost its known outcome')
        events = fetch_one(
            connection,
            """
            SELECT
              SUM(event_type = 'step_execution_started') AS started_count,
              SUM(event_type = 'step_executed') AS executed_count,
              SUM(event_type = 'write_intent') AS intent_count,
              SUM(event_type = 'recovery_readonly_retry') AS retry_count
              FROM t_ai_autonomous_event WHERE run_id = %s
            """,
            (SSH_READONLY_RUN_ID,),
        )
        require(int(events['started_count'] or 0) == 2,
                'read-only recovery did not perform exactly one retry')
        require(int(events['executed_count'] or 0) == 1,
                'read-only recovery persisted more than one outcome')
        require(int(events['intent_count'] or 0) == 0,
                'read-only recovery persisted a write intent')
        require(int(events['retry_count'] or 0) == 1,
                'read-only recovery boundary was not exactly once')
    finally:
        connection.close()


def _remote_counter(path):
    result = _run_remote(
        'if [ -f %s ]; then wc -l < %s; else exit 7; fi'
        % (path, path),
    )
    if result.exit_code == 7:
        return 0
    _require_remote_exit(result)
    try:
        return int(result.stdout.strip())
    except ValueError:
        raise AssertionError('remote execution counter was malformed')


def hold_ssh_pre_intent_lock():
    """Block the production pre-side-effect commit on the Step row."""
    cleanup = _run_remote('rm -f /tmp/s2-pre-intent-count')
    _require_remote_exit(cleanup)
    setup = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        insert_ssh_action_run(
            setup, SSH_PREINTENT_RUN_ID, SSH_PREINTENT_STEP_ID,
            'shell', {
                'command': (
                    "printf '%s\\n' executed "
                    '>> /tmp/s2-pre-intent-count'
                ),
            },
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
                (SSH_PREINTENT_STEP_ID,),
            )
            require(cursor.fetchone() is not None,
                    'pre-intent SSH Step row was not lockable')
        for _ in range(2):
            dispatch_run(SSH_PREINTENT_RUN_ID)
        signal = mysql_connection(os.environ['OGS_MYSQL_HOST'])
        try:
            append_event(
                signal, SSH_PREINTENT_RUN_ID,
                'smoke_pre_intent_lock_ready', {},
            )
        finally:
            signal.close()
        time.sleep(900)
    finally:
        locker.rollback()
        locker.close()


def wait_ssh_pre_intent_lock():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = fetch_one(
                connection,
                """
                SELECT COUNT(*) AS count FROM t_ai_autonomous_event
                 WHERE run_id = %s
                   AND event_type = 'smoke_pre_intent_lock_ready'
                """,
                (SSH_PREINTENT_RUN_ID,),
            )
            if int(row['count']) == 1:
                return
            time.sleep(0.1)
        raise AssertionError('pre-intent SSH row-lock fixture was not ready')
    finally:
        connection.close()


def wait_ssh_pre_intent_lease():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        row = wait_for_run(
            connection, SSH_PREINTENT_RUN_ID,
            lambda current: (
                current['status'] == 'running'
                and bool(current['lease_owner'])
                and bool(current['lease_present'])
                and current['lease_expires_at'] is not None
            ),
            'Worker did not claim the pre-intent SSH Run',
            timeout=30,
        )
        require(row['lease_expires_at'] > datetime.datetime.utcnow(),
                'pre-intent lease expired before failure injection')
        step = fetch_one(
            connection,
            'SELECT status FROM t_ai_autonomous_step WHERE id = %s',
            (SSH_PREINTENT_STEP_ID,),
        )
        require(step['status'] == 'approved',
                'pre-intent Step crossed its locked commit boundary')
        events = fetch_one(
            connection,
            """
            SELECT
              SUM(event_type = 'step_execution_started') AS started_count,
              SUM(event_type = 'write_intent') AS intent_count
              FROM t_ai_autonomous_event WHERE run_id = %s
            """,
            (SSH_PREINTENT_RUN_ID,),
        )
        require(int(events['started_count'] or 0) == 0,
                'execution_started committed before the forced Worker kill')
        require(int(events['intent_count'] or 0) == 0,
                'write_intent committed before the forced Worker kill')
        require(_remote_counter('/tmp/s2-pre-intent-count') == 0,
                'pre-intent write executed before its durable boundary')
    finally:
        connection.close()


def verify_ssh_pre_intent_recovery():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        wait_for_run(
            connection, SSH_PREINTENT_RUN_ID,
            lambda row: (
                row['status'] == 'completed'
                and row['lease_owner'] is None
                and not bool(row['lease_present'])
            ),
            'approved pre-intent write was not recovered to completion',
            timeout=75,
        )
        step = wait_for_step(
            connection, SSH_PREINTENT_STEP_ID,
            lambda row: row['status'] == 'succeeded',
            'recovered pre-intent write did not succeed',
        )
        require('exit_code=0' in str(step['note']),
                'recovered write lost its exact successful outcome')
        events = fetch_one(
            connection,
            """
            SELECT
              SUM(event_type = 'step_execution_started') AS started_count,
              SUM(event_type = 'write_intent') AS intent_count,
              SUM(event_type = 'step_executed') AS executed_count,
              SUM(event_type = 'step_outcome_unknown') AS unknown_count
              FROM t_ai_autonomous_event WHERE run_id = %s
            """,
            (SSH_PREINTENT_RUN_ID,),
        )
        require(int(events['started_count'] or 0) == 1,
                'pre-intent recovery started the write more than once')
        require(int(events['intent_count'] or 0) == 1,
                'pre-intent recovery wrote more than one intent')
        require(int(events['executed_count'] or 0) == 1,
                'pre-intent recovery persisted the outcome more than once')
        require(int(events['unknown_count'] or 0) == 0,
                'known pre-intent kill was misclassified as outcome_unknown')
        require(_remote_counter('/tmp/s2-pre-intent-count') == 1,
                'approved write did not execute exactly once after recovery')
    finally:
        connection.close()


def _require_no_enabled_provider(connection):
    """S2 门的 fail-closed 前提：实验室不得配置启用的 Provider。

    S3 接线后 planner 真实可用；若实验室配置了 Provider，这些
    场景的 Run 会真正开始调查而不是在规划边界失败，门就不再
    确定。S3 纵向闭环在独立的 S3 门脚本里配置 Provider。
    """
    row = fetch_one(
        connection,
        'SELECT COUNT(*) AS count FROM t_ai_provider WHERE enabled = 1',
        (),
    )
    require(int(row['count']) == 0,
            'S2 gate requires a lab without an enabled AI provider')


def worker_timer_recovery():
    """Require the real Worker timer to find an unpublished queued Run."""
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        _require_no_enabled_provider(connection)
        delete_run(connection, WORKER_TIMER_RUN_ID)
        insert_run(connection, WORKER_TIMER_RUN_ID, 190021, 'queued')
        # Intentionally no dispatch_run/send_task call in this phase.
        row = wait_for_run(
            connection, WORKER_TIMER_RUN_ID,
            lambda current: (
                current['status'] == 'failed'
                and current['lease_owner'] is None
                and not bool(current['lease_present'])
            ),
            'real Worker periodic scan did not recover an unpublished Run',
            timeout=50,
        )
        require(int(row['revision']) >= 2,
                'timer-recovered Run did not pass through a Worker lease')
        event = fetch_one(
            connection,
            """
            SELECT COUNT(*) AS count FROM t_ai_autonomous_event
             WHERE run_id = %s
               AND event_type IN ('planner_unavailable', 'planner_failed')
            """,
            (WORKER_TIMER_RUN_ID,),
        )
        require(int(event['count']) == 1,
                'timer recovery produced duplicate terminal work')
    finally:
        connection.close()


def ssh_start_write():
    cleanup = _run_remote(
        'rm -f /tmp/s2-write-count /tmp/s2-write-running',
    )
    _require_remote_exit(cleanup)
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        command = (
            "printf '%s\\n' started >> /tmp/s2-write-count; "
            ': > /tmp/s2-write-running; sleep 300'
        )
        insert_ssh_action_run(
            connection, SSH_WRITE_RUN_ID, SSH_WRITE_STEP_ID,
            'shell', {'command': command}, timeout_seconds=600,
        )
        # concurrency=1 leaves one delivery queued while the first delivery
        # crosses the real SSH side-effect boundary. The queued copy makes
        # lease-expiry recovery deterministic after a container SIGKILL.
        for _ in range(2):
            dispatch_run(SSH_WRITE_RUN_ID)
    finally:
        connection.close()


def _remote_write_count():
    return _remote_counter('/tmp/s2-write-count')


def wait_ssh_write_started():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        wait_for_step(
            connection, SSH_WRITE_STEP_ID,
            lambda row: row['status'] == 'running',
            'approved shell did not enter running before Worker SIGKILL',
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            intent = fetch_one(
                connection,
                """
                SELECT COUNT(*) AS count FROM t_ai_autonomous_event
                 WHERE run_id = %s AND event_type = 'write_intent'
                """,
                (SSH_WRITE_RUN_ID,),
            )
            if int(intent['count']) == 1 and _remote_write_count() == 1:
                row = fetch_one(
                    connection,
                    """
                    SELECT lease_owner,
                           lease_token IS NOT NULL AS lease_present,
                           lease_expires_at
                      FROM t_ai_autonomous_run WHERE id = %s
                    """,
                    (SSH_WRITE_RUN_ID,),
                )
                require(row['lease_owner'] and bool(row['lease_present']),
                        'started write has no fenced Worker lease')
                require(row['lease_expires_at'] > datetime.datetime.utcnow(),
                        'started-write lease expired before failure injection')
                return
            time.sleep(0.1)
        raise AssertionError(
            'remote write did not start after durable write_intent',
        )
    finally:
        connection.close()


def verify_ssh_write_recovery():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        wait_for_run(
            connection, SSH_WRITE_RUN_ID,
            lambda row: (
                row['status'] == 'needs_attention'
                and row['lease_owner'] is None
            ),
            'SIGKILLed started write did not fail closed on lease recovery',
            timeout=75,
        )
        step = fetch_one(
            connection,
            'SELECT status FROM t_ai_autonomous_step WHERE id = %s',
            (SSH_WRITE_STEP_ID,),
        )
        require(step['status'] == 'outcome_unknown',
                'SIGKILLed started write did not become outcome_unknown')
        events = fetch_one(
            connection,
            """
            SELECT
              SUM(event_type = 'step_execution_started') AS started_count,
              SUM(event_type = 'write_intent') AS intent_count,
              SUM(event_type = 'recovery_write_outcome_unknown')
                AS recovery_count,
              SUM(event_type = 'step_executed') AS executed_count
              FROM t_ai_autonomous_event WHERE run_id = %s
            """,
            (SSH_WRITE_RUN_ID,),
        )
        require(int(events['started_count'] or 0) == 1,
                'started write crossed production Executor more than once')
        require(int(events['intent_count'] or 0) == 1,
                'started write produced duplicate write intent')
        require(int(events['recovery_count'] or 0) == 1,
                'started write recovery was not exactly once')
        require(int(events['executed_count'] or 0) == 0,
                'unknown write was reported as executed')
        require(
            _remote_write_count() == 1,
            'started write was replayed after Worker SIGKILL',
        )
    finally:
        connection.close()


def ssh_symlink_boundary():
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        insert_ssh_action_run(
            connection, SSH_SYMLINK_RUN_ID, SSH_SYMLINK_STEP_ID,
            'probe', {
                'probe_id': 'file.read_bounded',
                'lines': '1',
                'path': '/opt/s2-smoke/bounded-link',
            },
        )
        dispatch_run(SSH_SYMLINK_RUN_ID)
        wait_for_step(
            connection, SSH_SYMLINK_STEP_ID,
            lambda row: row['status'] in {'succeeded', 'failed'},
            'symlink boundary probe did not reach a known result',
        )
        step = fetch_one(
            connection,
            'SELECT status FROM t_ai_autonomous_step WHERE id = %s',
            (SSH_SYMLINK_STEP_ID,),
        )
        require(step['status'] == 'failed',
                'bounded file probe followed a symlink outside its root')
        wait_for_run(
            connection, SSH_SYMLINK_RUN_ID,
            lambda row: (
                row['status'] in TERMINAL_STATUSES
                and row['lease_owner'] is None
            ),
            'symlink rejection Run did not settle',
        )
        artifacts = _artifact_contents(
            connection, SSH_SYMLINK_RUN_ID, SSH_SYMLINK_STEP_ID,
        )
        combined = '\n'.join(artifacts.values())
        require(
            SSH_SYMLINK_MARKER not in combined,
            'symlink target escaped the bounded root',
        )
        event = fetch_one(
            connection,
            """
            SELECT payload_json FROM t_ai_autonomous_event
             WHERE run_id = %s AND event_type = 'step_executed'
            """,
            (SSH_SYMLINK_RUN_ID,),
        )
        require(int(json.loads(event['payload_json'])['exit_code']) != 0,
                'symlink refusal returned a successful exit code')
    finally:
        connection.close()


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
        _require_no_enabled_provider(connection)
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
                SELECT status, revision, lease_owner,
                       lease_token IS NOT NULL AS lease_present,
                       lease_expires_at
                  FROM t_ai_autonomous_run WHERE id = %s
                """,
                (run_id,),
            )
            if row['status'] in TERMINAL_STATUSES:
                break
            time.sleep(0.25)
        require(
            row is not None and row['status'] == 'failed',
            'duplicate delivery did not fail closed at the planner boundary',
        )
        require(
            row['lease_owner'] is None
            and not bool(row['lease_present'])
            and row['lease_expires_at'] is None,
            'terminal Run retained a worker lease fence',
        )
        event = fetch_one(
            connection,
            """
            SELECT COUNT(*) AS count
              FROM t_ai_autonomous_event
             WHERE run_id = %s
               AND event_type IN ('planner_unavailable', 'planner_failed')
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
                and bool(current['lease_present'])
                and current['lease_expires_at'] is not None
            ),
            'real Worker did not claim the queued Run before SIGKILL',
            timeout=30,
        )
        # Do not write an observation event on this Run: the production
        # recovery transaction holds its Run row while blocked on the Step
        # fixture.  The wrapper captures this read-only evidence and passes it
        # to the post-restart phase without exposing the opaque lease token.
        print('S2_WORKER_LEASE_EVIDENCE=' + json.dumps({
            'lease_owner': row['lease_owner'],
            'revision': int(row['revision']),
            'lease_expires_at': row['lease_expires_at'].isoformat(),
        }, separators=(',', ':')))
    finally:
        connection.close()


def verify_restart_before_expiry():
    """Prove the replacement Worker was ready while the old lease lived."""
    from app.ai.autonomy.readiness import worker_readiness

    expected_owner = os.environ['OGS_S2_EXPECTED_LEASE_OWNER']
    expected_revision = int(os.environ['OGS_S2_EXPECTED_LEASE_REVISION'])
    expected_expiry_text = os.environ[
        'OGS_S2_EXPECTED_LEASE_EXPIRES_AT'
    ]
    expected_expiry = datetime.datetime.fromisoformat(expected_expiry_text)
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        replacement_ready = False
        while True:
            row = fetch_one(
                connection,
                """
                SELECT status, revision, lease_owner,
                       lease_token IS NOT NULL AS lease_present,
                       lease_expires_at,
                       UTC_TIMESTAMP(6) AS observed_at
                  FROM t_ai_autonomous_run WHERE id = %s
                """,
                (WORKER_KILL_RUN_ID,),
            )
            if row['observed_at'] >= expected_expiry:
                break
            require(row['status'] == 'running',
                    'replacement Worker crossed the live lease too early')
            require(row['lease_owner'] == expected_owner,
                    'old lease owner changed before expiry takeover')
            require(int(row['revision']) == expected_revision,
                    'Run revision changed before expiry takeover')
            require(bool(row['lease_present']),
                    'SIGKILL residue lost the fenced lease token')
            require(
                row['lease_expires_at'] is not None
                and row['lease_expires_at'].isoformat()
                == expected_expiry_text,
                'old lease expiry evidence changed before takeover',
            )
            if worker_readiness(timeout=1):
                # Inspect readiness is not the evidence boundary: re-read the
                # Run and DB clock after the Worker reply, so a slow inspect
                # cannot accidentally classify a legal expiry as early use.
                ready_row = fetch_one(
                    connection,
                    """
                    SELECT status, revision, lease_owner,
                           lease_token IS NOT NULL AS lease_present,
                           lease_expires_at,
                           UTC_TIMESTAMP(6) AS observed_at
                      FROM t_ai_autonomous_run WHERE id = %s
                    """,
                    (WORKER_KILL_RUN_ID,),
                )
                if ready_row['observed_at'] >= expected_expiry:
                    break
                require(ready_row['status'] == 'running',
                        'ready Worker crossed the live lease too early')
                require(ready_row['lease_owner'] == expected_owner,
                        'old lease owner changed after Worker readiness')
                require(int(ready_row['revision']) == expected_revision,
                        'Run revision changed after Worker readiness')
                require(bool(ready_row['lease_present']),
                        'ready Worker replaced the fenced lease token')
                require(
                    ready_row['lease_expires_at'] is not None
                    and ready_row['lease_expires_at'].isoformat()
                    == expected_expiry_text,
                    'old lease expiry changed after Worker readiness',
                )
                replacement_ready = True
                break
            time.sleep(0.1)
        require(
            replacement_ready,
            'harness missed the live-lease observation window',
        )
    finally:
        connection.close()


def verify_worker_kill_recovery():
    expected_expiry = datetime.datetime.fromisoformat(
        os.environ['OGS_S2_EXPECTED_LEASE_EXPIRES_AT'],
    )
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        observed = fetch_one(
            connection, 'SELECT UTC_TIMESTAMP(6) AS observed_at',
        )
        remaining = max(
            0.0,
            (expected_expiry - observed['observed_at']).total_seconds(),
        )
        row = wait_for_run(
            connection, WORKER_KILL_RUN_ID,
            lambda current: (
                current['status'] == 'needs_attention'
                and current['lease_owner'] is None
                and not bool(current['lease_present'])
                and current['lease_expires_at'] is None
            ),
            'restarted Worker did not recover expired SIGKILL lease',
            timeout=max(45, int(math.ceil(remaining)) + 45),
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
        execution_events = fetch_one(
            connection,
            """
            SELECT
              SUM(event_type = 'write_intent') AS intent_count,
              SUM(event_type = 'step_executed') AS executed_count
              FROM t_ai_autonomous_event WHERE run_id = %s
            """,
            (WORKER_KILL_RUN_ID,),
        )
        require(int(execution_events['intent_count'] or 0) == 1,
                'Worker crash fixture lost its durable write intent')
        require(
            int(execution_events['executed_count'] or 0) == 0,
            'Worker crash recovery claimed a remote execution outcome',
        )
    finally:
        connection.close()


PHASES = {
    'checkpoint-and-cancel': checkpoint_and_cancel,
    'feature-off-isolation': feature_off_isolation,
    'hold-worker-lock': hold_worker_lock,
    'hold-ssh-pre-intent-lock': hold_ssh_pre_intent_lock,
    'langgraph-pause-first': langgraph_pause_first,
    'langgraph-resume-after-restart': langgraph_resume_after_restart,
    'langgraph-resume-to-second': langgraph_resume_to_second,
    'lease-and-boundary': lease_and_boundary,
    'migrate-and-prime': migrate_and_prime,
    'production-checkpoint-loss-boundary': (
        production_checkpoint_loss_boundary
    ),
    'release-ssh-readonly-first-attempt': (
        release_ssh_readonly_first_attempt
    ),
    'ssh-cancel-process-group': ssh_cancel_process_group,
    'ssh-exit-and-streams': ssh_exit_and_streams,
    'ssh-file-patch-restore': ssh_file_patch_restore,
    'ssh-prime-target': ssh_prime_target,
    'ssh-runtime-environment-revocation': (
        ssh_runtime_environment_revocation
    ),
    'ssh-start-readonly': ssh_start_readonly,
    'ssh-start-write': ssh_start_write,
    'ssh-symlink-boundary': ssh_symlink_boundary,
    'verify-persistence': verify_persistence,
    'verify-ssh-readonly-recovery': verify_ssh_readonly_recovery,
    'verify-ssh-pre-intent-recovery': verify_ssh_pre_intent_recovery,
    'verify-ssh-write-recovery': verify_ssh_write_recovery,
    'verify-restart-before-expiry': verify_restart_before_expiry,
    'verify-worker-kill-recovery': verify_worker_kill_recovery,
    'wait-worker-lease': wait_worker_lease,
    'wait-worker-lock-ready': wait_worker_lock_ready,
    'wait-ssh-pre-intent-lease': wait_ssh_pre_intent_lease,
    'wait-ssh-pre-intent-lock': wait_ssh_pre_intent_lock,
    'wait-ssh-readonly-started': wait_ssh_readonly_started,
    'wait-ssh-write-started': wait_ssh_write_started,
    'worker-and-duplicate': worker_and_duplicate,
    'worker-timer-recovery': worker_timer_recovery,
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
