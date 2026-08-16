"""M1/S3 切片 7 smoke 探针：聊天侧只能创建自治草稿，绝不启动 Run。

复用 S2 exact-head Compose 栈（mysql-fresh / business-redis / 后端镜像），
由 deploy/docker-compose.s3-smoke.yml 薄 overlay 挂载本脚本，
经 ops/smoke-ai-autonomy-s3.ps1 在 exact HEAD 上执行。
"""
import os
import sys
from datetime import datetime, timezone

import pymysql

CHAT_DRAFT_HOST_ID = 190301
CHAT_DRAFT_SYSTEM_USER_ID = 190301

# 契约边界：聊天工具面绝不允许出现的 Run 生命周期工具名。
RUN_LIFECYCLE_TOOLS = {
    'start_autonomy_run',
    'approve_autonomy_run',
    'approve_autonomy_step',
    'cancel_autonomy_run',
    'update_autonomy_run',
}


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
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_one(connection, sql, args=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.fetchone()


def _seed_chat_draft_fixtures(connection):
    """一次性主机/凭据夹具：192.0.2.0/24 为文档保留地址，不对应真实资产。"""
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT id FROM t_ai_autonomous_run WHERE host_id = %s',
            (CHAT_DRAFT_HOST_ID,),
        )
        for row in cursor.fetchall():
            cursor.execute(
                'DELETE FROM t_ai_autonomous_step WHERE run_id = %s',
                (row['id'],),
            )
        cursor.execute(
            'DELETE FROM t_ai_autonomous_run WHERE host_id = %s',
            (CHAT_DRAFT_HOST_ID,),
        )
        cursor.execute(
            'DELETE FROM t_host WHERE id = %s', (CHAT_DRAFT_HOST_ID,),
        )
        cursor.execute(
            'DELETE FROM t_sys_user WHERE id = %s',
            (CHAT_DRAFT_SYSTEM_USER_ID,),
        )
        cursor.execute(
            """
            INSERT INTO t_host
                (id, alias, host_ip, host_port, ai_environment, is_deleted)
            VALUES
                (%s, 's3-chat-draft-host', '192.0.2.10', 22, 'lab', 0)
            """,
            (CHAT_DRAFT_HOST_ID,),
        )
        cursor.execute(
            """
            INSERT INTO t_sys_user
                (id, alias, host_user, agreement, is_deleted)
            VALUES
                (%s, 's3-chat-draft-user', 'smoke', 'password', 0)
            """,
            (CHAT_DRAFT_SYSTEM_USER_ID,),
        )


def chat_draft_only():
    """聊天工具创建草稿/引用卡；不能启动、批准、取消或变更 Run。"""
    from app.core import config
    import init as backend_init

    require(
        config.AI_AUTONOMY_ENABLED is True,
        'S3 chat-draft probe requires autonomy enabled',
    )
    connection = mysql_connection(os.environ['OGS_MYSQL_HOST'])
    try:
        _seed_chat_draft_fixtures(connection)
        backend_init.orange_init_api()
        app = backend_init.app

        with app.app_context():
            from app.ai.storage import AgentStore
            from app.ai.tools import (
                ADMIN_ONLY_TOOLS,
                TOOL_DEFINITIONS,
                PlatformQueryService,
                ToolNotAllowed,
                ToolRegistry,
            )
            from app.tools.redisdb import ConnRedis

            # 静态边界：聊天工具面没有任何 Run 生命周期能力。
            require(
                not RUN_LIFECYCLE_TOOLS & set(TOOL_DEFINITIONS),
                'chat tool surface exposes autonomy Run lifecycle tools',
            )
            require(
                'create_autonomy_draft' in ADMIN_ONLY_TOOLS,
                'create_autonomy_draft must remain admin-only',
            )

            store = AgentStore(ConnRedis().conn)
            conversation = store.create_conversation(
                'admin', 'smoke', 'smoke', title='s3 chat draft smoke',
            )
            conversation_id = conversation['id']

            # 非管理员聊天不能创建草稿。
            user_registry = ToolRegistry(
                store=store,
                platform=PlatformQueryService('smoke-user', 'user'),
                owner='smoke-user',
                role='user',
                conversation_id=conversation_id,
            )
            try:
                user_registry.execute('create_autonomy_draft', {
                    'goal': 'S3 unauthorized draft',
                    'host_id': CHAT_DRAFT_HOST_ID,
                    'system_user_id': CHAT_DRAFT_SYSTEM_USER_ID,
                })
                raise AssertionError(
                    'non-admin chat executed create_autonomy_draft',
                )
            except ToolNotAllowed:
                pass

            # 管理员聊天只产生 draft 落库，绝不产生执行态。
            registry = ToolRegistry(
                store=store,
                platform=PlatformQueryService('admin', 'admin'),
                owner='admin',
                role='admin',
                conversation_id=conversation_id,
            )
            result = registry.execute('create_autonomy_draft', {
                'goal': 'S3 chat draft smoke',
                'host_id': CHAT_DRAFT_HOST_ID,
                'system_user_id': CHAT_DRAFT_SYSTEM_USER_ID,
                'mode': 'ask',
            })
            draft = result['autonomy_draft']
            require(
                draft['status'] == 'draft',
                'chat draft was not created in draft status',
            )
            run_id = draft['run_id']

            run_row = fetch_one(
                connection,
                'SELECT status, owner, started_at FROM t_ai_autonomous_run'
                ' WHERE id = %s',
                (run_id,),
            )
            require(
                run_row is not None
                and run_row['status'] == 'draft'
                and run_row['owner'] == 'admin'
                and run_row['started_at'] is None,
                'chat-created draft row was not a pristine draft',
            )
            step_row = fetch_one(
                connection,
                'SELECT COUNT(*) AS c FROM t_ai_autonomous_step'
                ' WHERE run_id = %s',
                (run_id,),
            )
            require(
                int(step_row['c']) == 0,
                'chat-created draft produced Run steps',
            )

            # 引用卡事件持久化后，会话详情投影必须能恢复它。
            store.append_event('admin', conversation_id, {
                'id': 'call-s3-draft',
                'type': 'autonomy.draft_created',
                'created_at': datetime.now(timezone.utc).isoformat(),
                **draft,
            })
            from app.ai.views import _project_autonomy_drafts

            saved = store.get_conversation('admin', conversation_id)
            cards = _project_autonomy_drafts(saved.get('events'))
            require(
                len(cards) == 1
                and cards[0]['run_id'] == run_id
                and cards[0]['status'] == 'draft',
                'conversation detail lost the draft reference card',
            )
    finally:
        connection.close()


PHASES = {
    'chat-draft-only': chat_draft_only,
}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in PHASES:
        choices = ', '.join(sorted(PHASES))
        raise SystemExit(
            'usage: smoke-ai-autonomy-s3.py {%s}' % choices,
        )
    phase = sys.argv[1]
    PHASES[phase]()
    print('[S3 smoke] phase PASS: %s' % phase)


if __name__ == '__main__':
    main()
