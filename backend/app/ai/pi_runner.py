"""Pi chat integration backed by the platform's existing tool registry."""
from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from app.ai.diagnostic_adapters import sanitize_evidence
from app.ai.monitoring import MonitoringAnalysisService
from app.ai.runner import AgentRunner, _configured_language, sse_event
from app.ai.storage import AgentStoreConflict
from app.ai.tools import (PlatformQueryService, ToolError, ToolRegistry,
                          _validate_monitoring_result_access)
from app.core.db.database import db
from app.tools.at import get_current_user, get_current_user_role, _ws_session_check_and_renew
from app.tools.basesec import decrypt_secret, encrypt_secret
from flask import request

logger = logging.getLogger(__name__)

_COMMIT_WITH_LOCK = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
redis.call('ZADD', KEYS[3], ARGV[4], ARGV[5])
redis.call('EXPIRE', KEYS[3], ARGV[3])
return 1
"""

_EVIDENCE_INSTRUCTIONS = """
监控调查引用 result_set_id 和具体时间、指标；需要原始 Prometheus 采样时使用
read_monitoring_evidence，完整序列统计以工具返回为准。可用 verify_monitoring_citations
验证采样引用；不确定或无法获取的数值应说明未知，推断不能当作已验证的事实。
不要把自行计算的数值说成工具计算结果；平台 SSH 探活状态不能证明监控数据不可用。
"""
_RESET_NOTICE = {
    'zh-CN': '历史证据无法复核、已过期或不在当前权限和来源范围内，本次将重新调查；原聊天记录仍可查看。\n\n',
    'en-US': 'Earlier evidence is unavailable or outside the current access or source scope. '
             'This investigation starts with fresh context; the chat history remains visible.\n\n',
}


def monitoring_notice(queried, verified, total, language='zh-CN'):
    if not queried:
        return ''
    if language == 'en-US':
        if not total:
            return 'The server did not verify individual monitoring samples in this turn. Check the original evidence.\n\n'
        return (f'Server sample checks: {verified}/{total} passed. Only those specific samples were verified; '
                'check all other values and analysis against the original evidence.\n\n')
    if not total:
        return '服务端未对本轮监控采样逐条核验；模型分析请结合原始证据核对。\n\n'
    return (f'服务端采样核验：{verified}/{total} 个核验项通过；只有通过的具体采样经过核验，'
            '其余数值与分析仍需结合原始证据核对。\n\n')


def asset_history_authorized(conversation, store, platform, owner, conversation_id):
    calls = {call.get('id') for message in conversation.get('messages') or []
             for call in message.get('tool_calls') or []
             if (call.get('function') or {}).get('name') == 'search_assets'}
    try:
        for message in conversation.get('messages') or []:
            if message.get('role') != 'tool' or message.get('tool_call_id') not in calls:
                continue
            payload = json.loads(message.get('content') or '')
            if payload.get('ok') is not True:
                continue
            ref = payload.get('result_set_id')
            record = store.get_result_set(owner, ref)
            ids = record.get('resource_ids')
            if (not ref or record.get('id') != ref or record.get('owner') != owner
                    or record.get('conversation_id') != conversation_id or record.get('kind') != 'assets'
                    or not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids)
                    or (ids and not platform.validate_asset_ids(ids))):
                return False
        return True
    except Exception:
        return False


def history_authorized(conversation, store, platform, owner, conversation_id, allowed_names,
                       monitoring_source_types=None):
    """Recheck the authority of retained tool data before replaying it to a model."""
    if not asset_history_authorized(conversation, store, platform, owner, conversation_id):
        return False
    monitoring = {'discover_monitoring', 'query_prometheus', 'query_loki',
                  'query_grafana_panel', 'query_zabbix_history', 'read_monitoring_evidence',
                  'verify_monitoring_citations'}
    types = set(monitoring_source_types if monitoring_source_types is not None
                else ('prometheus', 'loki', 'grafana', 'zabbix'))
    calls = {}
    try:
        for message in conversation.get('messages') or []:
            for call in message.get('tool_calls') or []:
                name = (call.get('function') or {}).get('name')
                if name not in allowed_names or not call.get('id') or call['id'] in calls:
                    return False
                calls[call['id']] = call.get('function') or {}
            if message.get('role') != 'tool':
                continue
            function = calls.get(message.get('tool_call_id'))
            if not function:
                return False
            name = function.get('name')
            payload = json.loads(message.get('content') or '')
            if payload.get('ok') is not True:
                continue
            if name in {'get_platform_overview', 'search_cron_jobs', 'list_authorized_system_users'}:
                ref = payload.get('result_set_id')
                record = store.get_result_set(owner, ref)
                expected_kind = {'get_platform_overview': 'overview', 'search_cron_jobs': 'cron_jobs',
                                 'list_authorized_system_users': 'system_users'}[name]
                if (record.get('id') != ref or record.get('owner') != owner
                        or record.get('conversation_id') != conversation_id or record.get('kind') != expected_kind):
                    return False
                ids, rows = record.get('resource_ids'), record.get('rows')
                if not isinstance(ids, list) or not isinstance(rows, list):
                    return False
                if name == 'get_platform_overview':
                    scope = record.get('authorization_scope')
                    if (not isinstance(scope, dict) or not isinstance(scope.get('groups'), list)
                            or not isinstance(scope.get('cron_ids'), list)
                            or any(not isinstance(group, str) for group in scope['groups'])
                            or any(type(item) is not int or item <= 0 for item in scope['cron_ids'])
                            or not set(scope['groups']).issubset(platform._allowed_groups())
                            or not platform.validate_cron_ids(scope['cron_ids'])):
                        return False
                else:
                    if (any(type(item) is not int or item <= 0 for item in ids)
                            or {row.get('id') for row in rows} != set(ids)):
                        return False
                    if name == 'search_cron_jobs' and not platform.validate_cron_ids(ids):
                        return False
                    if name == 'list_authorized_system_users':
                        for row in rows:
                            current = platform.resolve_system_user(row['id'])
                            if not current or current.get('alias') != row.get('alias'):
                                return False
            if name == 'search_knowledge':
                from app.ai.knowledge import KnowledgeService

                scopes = set(platform.authorized_knowledge_scopes())
                for item in payload.get('knowledge_references') or []:
                    document = KnowledgeService(db.session)._document_row(item.get('document_id'))
                    if (item.get('scope') not in scopes or document.scope != item.get('scope')
                            or not document.approved or int(document.version) != item.get('version')):
                        return False
            if name not in monitoring:
                continue
            arguments = json.loads(function.get('arguments') or '{}')
            refs = set()
            if payload.get('result_set_id'):
                refs.add(payload['result_set_id'])
            if arguments.get('result_set_id'):
                refs.add(arguments['result_set_id'])
            refs.update(claim.get('result_set_id') for claim in arguments.get('citations', [])
                        if isinstance(claim, dict) and claim.get('result_set_id'))
            for ref in refs:
                record = store.get_result_set(owner, ref)
                if record.get('id') != ref or record.get('owner') != owner or record.get('conversation_id') != conversation_id:
                    return False
                if record.get('kind') == 'monitoring_catalog':
                    ids = record.get('resource_ids')
                    if not isinstance(ids, list) or len(ids) != 1 or not platform.validate_asset_ids(ids):
                        return False
                    service = MonitoringAnalysisService(db.session, platform)
                    current = {source.id: source.source_type for source, _ in service._mapped_sources(ids[0])}
                    if any(current.get(row.get('source_id')) != row.get('source_type')
                           or row.get('source_type') not in types for row in record.get('rows') or []):
                        return False
                else:
                    _validate_monitoring_result_access(record, platform, allowed_source_types=types)
        return True
    except Exception:
        return False


def seal_checkpoint(owner, conversation_id, checkpoint):
    return encrypt_secret(json.dumps({'owner': owner, 'conversation_id': conversation_id,
        'checkpoint': checkpoint}, ensure_ascii=False, separators=(',', ':')))


def open_checkpoint(owner, conversation_id, ciphertext):
    payload = json.loads(decrypt_secret(ciphertext))
    if (not isinstance(payload, dict) or set(payload) != {'owner', 'conversation_id', 'checkpoint'}
            or payload['owner'] != owner or payload['conversation_id'] != conversation_id
            or not isinstance(payload['checkpoint'], dict)):
        raise ValueError('checkpoint identity mismatch')
    return payload['checkpoint']


def commit_checkpoint(store, owner, conversation_id, token, checkpoint, message=None,
                      history_start=0, role=None, telemetry=None):
    conversation = store.get_conversation(owner, conversation_id)
    if conversation.get('owner') != owner or conversation.get('id') != conversation_id:
        raise ValueError('conversation identity mismatch')
    if message is not None:
        conversation['messages'].append(message)
    conversation['pi_checkpoint'] = seal_checkpoint(owner, conversation_id, checkpoint)
    conversation['pi_checkpoint_message_count'] = len(conversation['messages'])
    conversation['pi_history_start'] = history_start
    if role is not None:
        conversation['pi_role'] = role
    if telemetry:
        metrics = conversation.setdefault('state', {}).setdefault('provider_observability', {})
        totals = metrics.setdefault('usage', {})
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            totals[key] = max(0, int(totals.get(key) or 0)) + telemetry.get(key, 0)
        metrics['compression_count'] = max(0, int(metrics.get('compression_count') or 0)) + telemetry.get('compressions', 0)
        if telemetry.get('finish_reason'):
            metrics['last_finish_reason'] = telemetry['finish_reason']
        metrics['context_budget'] = {'context_window_tokens': telemetry['context_window']}
        if telemetry.get('context_tokens') is not None:
            metrics['context_budget'].update({'effective_input_tokens': telemetry['context_window'],
                                              'estimated_input_tokens': telemetry['context_tokens']})
        metrics.pop('truncation_reason', None)
    conversation['updated_at'] = time.time()
    committed = store.redis.eval(_COMMIT_WITH_LOCK, 3, store._run_lock_key(owner, conversation_id),
        store._conversation_key(owner, conversation_id), store._conversation_index(owner), token,
        json.dumps(conversation, ensure_ascii=False, separators=(',', ':')), store.conversation_ttl,
        conversation['updated_at'], conversation_id)
    if committed != 1:
        raise ValueError('conversation run lock lost')


def session_authorized(owner, role):
    alive, _ = _ws_session_check_and_renew(request.cookies.get('ogs_token'))
    if not alive:
        return False
    _, current_owner = get_current_user()
    return current_owner == owner and str(get_current_user_role() or '') == role and role in {'admin', 'user'}


def write_frame(stream, value):
    remaining = memoryview((json.dumps(value, ensure_ascii=False) + '\n').encode())
    while remaining:
        try:
            written = stream.write(remaining)
        except BlockingIOError as exc:
            written = getattr(exc, 'characters_written', 0) or None
        if written is None:
            if not select.select([], [stream.fileno()], [], 10)[1]:
                raise TimeoutError('Pi protocol write stalled')
            continue
        if written <= 0:
            raise BrokenPipeError('Pi protocol write stopped')
        remaining = remaining[written:]
    stream.flush()


class PiAgentRunner(AgentRunner):
    """Bridge Pi's agent session to the existing authorized platform tools."""

    def run(self, *, owner, role, conversation_id, message, monitoring_source_types=None):
        run_id, token, child = uuid.uuid4().hex, None, None
        try:
            if not str(message or '').strip():
                raise ValueError('empty message')
            token = self.store.acquire_run_lock(owner, conversation_id)
            conversation = self.store.get_conversation(owner, conversation_id)
            runtime = self.providers.runtime(conversation.get('provider_code'),
                                             context_mode=conversation.get('context_mode'))
            row = self.providers.configured_row(conversation.get('provider_code'))
            platform = PlatformQueryService(owner, role)
            registry = ToolRegistry(store=self.store, platform=platform, owner=owner, role=role,
                conversation_id=conversation_id, monitoring_source_types=monitoring_source_types,
                autonomy_mode=conversation.get('autonomy_mode') or 'ask',
                autonomy_profile=conversation.get('autonomy_profile'))
            diagnostic_queue = [None]

            def execute_diagnostic(arguments):
                service = self._diagnostic_service()

                def relay(event):
                    if diagnostic_queue[0] is not None:
                        diagnostic_queue[0].put(('diagnostic', event))

                run = service.start(owner=owner, role=role,
                    payload={**dict(arguments), 'conversation_id': conversation_id}, on_event=relay)
                return {'diagnostic_run_id': run['id'], 'status': run['status'],
                        'summary': run.get('summary') or {}, 'report': service.report(owner, str(run['id']))}

            registry.diagnostic_executor = execute_diagnostic

            def execute_monitoring(operation, arguments):
                service = MonitoringAnalysisService(db.session, platform)
                return service.discover(arguments) if operation == 'discover_monitoring' else service.query(operation, arguments)

            registry.monitoring_executor = execute_monitoring
            definitions = registry.definitions()
            allowed_names = {entry['function']['name'] for entry in definitions}

            history_start = conversation.get('pi_history_start', 0)
            messages = conversation.get('messages') or []
            if type(history_start) is not int or not 0 <= history_start <= len(messages):
                raise ValueError('invalid history position')

            def authorized():
                if not session_authorized(owner, role):
                    return False
                db.session.expire_all()
                current = self.store.get_conversation(owner, conversation_id)
                current = {**current, 'messages': (current.get('messages') or [])[history_start:]}
                return history_authorized(current, self.store, platform, owner, conversation_id,
                                          allowed_names, monitoring_source_types)

            if not session_authorized(owner, role):
                raise PermissionError('session authorization changed')
            reset_history = (conversation.get('pi_role') not in (None, role) or not authorized())
            if reset_history:
                history_start = len(messages)
            stored = conversation.get('pi_checkpoint')
            language = _configured_language()
            reset_notice = _RESET_NOTICE[language]
            checkpoint = open_checkpoint(owner, conversation_id, stored) if stored else None
            count = conversation.get('pi_checkpoint_message_count', 0) if checkpoint else 0
            if type(count) is not int or not 0 <= count <= len(messages):
                raise ValueError('invalid checkpoint position')
            started = {event.get('id') for event in conversation.get('events') or []
                       if event.get('type') == 'tool.started' and event.get('tool') == 'create_autonomy_draft'}
            completed = {event.get('id') for event in conversation.get('events') or []
                         if event.get('type') == 'tool.completed'}
            if started - completed:
                raise ValueError('previous draft outcome unknown')
            projection = self._provider_messages({**conversation,
                'messages': [] if reset_history else messages[count:],
                'state': {} if reset_history else {key: value for key, value in
                    (conversation.get('state') or {}).items() if key != 'provider_observability'},
                'summary': conversation.get('summary') if not checkpoint and not reset_history else ''},
                extra_context=self._latest_diagnostic_context(owner, conversation_id, role))
            history = projection[1:]
            self.store.append_message(owner, conversation_id, {'id': uuid.uuid4().hex, 'role': 'user',
                'content': str(message), 'created_at': time.time()})
            yield sse_event('run.started', run_id=run_id, conversation_id=conversation_id)
            if reset_history:
                yield sse_event('assistant.delta', run_id=run_id, content=reset_notice)
            child_path = Path(__file__).parent / 'pi' / 'web-child.mjs'
            child_env = {key: os.environ[key] for key in (
                'PATH', 'LANG', 'LC_ALL', 'TZ', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy',
                'NODE_USE_ENV_PROXY', 'NODE_EXTRA_CA_CERTS', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP',
            ) if key in os.environ}
            child = subprocess.Popen(['node', str(child_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0, env=child_env)
            api_key = decrypt_secret(row.api_key_ciphertext)
            write_frame(child.stdin, {'provider': {'code': row.provider_code, 'model': runtime.adapter.model,
                'base_url': row.base_url, 'api_key': api_key, 'context_window': runtime.context_window_tokens,
                'extra_body': runtime.adapter.extra_body or {}}, 'message': str(message),
                'system_prompt': projection[0]['content'] + _EVIDENCE_INSTRUCTIONS,
                'tools': definitions, 'checkpoint': checkpoint,
                'history': history, 'reset_history': reset_history})
            buffer = bytearray()
            search_from = 0
            telemetry = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
                         'compressions': 0, 'context_window': runtime.context_window_tokens}
            monitoring_queried = any((call.get('function') or {}).get('name') in {
                'query_prometheus', 'query_loki', 'query_grafana_panel', 'query_zabbix_history',
            } for item in messages[history_start:] for call in item.get('tool_calls') or [])
            verified_samples = total_samples = 0
            streamed_text = []
            while True:
                ready, _, _ = select.select([child.stdout], [], [], 10)
                self.store.refresh_run_lock(owner, conversation_id, token)
                if not ready:
                    if child.poll() is not None:
                        raise ValueError('Pi process stopped')
                    yield sse_event('heartbeat', run_id=run_id)
                    continue
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    raise ValueError('Pi process closed')
                buffer.extend(chunk)
                while (newline := buffer.find(b'\n', search_from)) != -1:
                    line = buffer[:newline]
                    del buffer[:newline + 1]
                    search_from = 0
                    frame = json.loads(line)
                    kind = frame.get('type')
                    context_usage = frame.get('contextUsage')
                    if isinstance(context_usage, dict):
                        value = context_usage.get('tokens')
                        telemetry['context_tokens'] = value if type(value) is int and value >= 0 else None
                    if kind == 'text.delta':
                        text = frame.get('text')
                        if not isinstance(text, str):
                            raise ValueError('invalid text delta')
                        streamed_text.append(text)
                        yield sse_event('assistant.delta', run_id=run_id, content=text)
                    elif kind == 'authorize.request':
                        write_frame(child.stdin, {'id': frame['id'], 'ok': authorized()})
                    elif kind == 'usage':
                        def tokens(key):
                            value = frame.get(key)
                            return value if type(value) is int and value >= 0 else 0
                        prompt = tokens('input') + tokens('cacheRead') + tokens('cacheWrite')
                        completion = tokens('output')
                        telemetry['prompt_tokens'] += prompt
                        telemetry['completion_tokens'] += completion
                        telemetry['total_tokens'] += prompt + completion
                        if frame.get('stopReason') in {'stop', 'toolUse', 'length', 'error', 'aborted'}:
                            telemetry['finish_reason'] = frame['stopReason']
                    elif kind == 'compaction_end':
                        if frame.get('ok') is True:
                            telemetry['compressions'] += 1
                    elif kind == 'tool.request':
                        name, args, call_id = frame.get('name'), frame.get('arguments'), frame.get('id')
                        if not authorized():
                            raise PermissionError('session or evidence authorization changed')
                        if name not in allowed_names or not isinstance(args, dict) or not isinstance(call_id, str):
                            raise ValueError('invalid tool request')
                        self._record_event(owner, conversation_id, 'tool.started', id=call_id,
                                           tool=name, label=name, status='running')
                        yield sse_event('tool.started', id=call_id, tool=name, arguments=args, run_id=run_id)
                        if name in {'query_prometheus', 'query_loki', 'query_grafana_panel', 'query_zabbix_history'}:
                            monitoring_queried = True
                        try:
                            if name == 'run_diagnostic':
                                from gevent import spawn
                                from gevent.queue import Queue

                                queue = diagnostic_queue[0] = Queue()

                                def run_diagnostic():
                                    try:
                                        context = self.worker_context_factory() if self.worker_context_factory else nullcontext()
                                        with context:
                                            queue.put(('result', registry.execute(name, args)))
                                    except Exception as exc:
                                        queue.put(('error', exc))

                                spawn(run_diagnostic)
                                while True:
                                    item_type, item = queue.get()
                                    self.store.refresh_run_lock(owner, conversation_id, token)
                                    if item_type == 'diagnostic':
                                        event_type = str(item.get('type') or 'diagnostic_progress')
                                        fields = {key: value for key, value in item.items() if key != 'type'}
                                        self._record_event(owner, conversation_id, event_type, **fields)
                                        yield sse_event(event_type, **fields)
                                    elif item_type == 'result':
                                        result = item
                                        break
                                    else:
                                        raise item
                                diagnostic_queue[0] = None
                            else:
                                result = registry.execute(name, args)
                            if name == 'verify_monitoring_citations' and isinstance(result, dict):
                                verified, total = result.get('verified_count'), result.get('total')
                                if type(verified) is int and type(total) is int and 0 <= verified <= total:
                                    verified_samples += verified
                                    total_samples += total
                            payload = {'ok': True, **result}
                        except Exception as exc:
                            db.session.rollback()
                            result = None
                            payload = {'ok': False, 'error': sanitize_evidence(str(exc))[:200]
                                       if isinstance(exc, ToolError) else '平台工具执行失败'}
                        call = {'id': call_id, 'type': 'function', 'function': {
                            'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}
                        assistant = {'id': uuid.uuid4().hex, 'role': 'assistant', 'content': '',
                                     'tool_calls': [call], 'created_at': time.time()}
                        if isinstance(frame.get('reasoning_content'), str) and frame['reasoning_content']:
                            assistant['reasoning_content'] = frame['reasoning_content']
                        self.store.append_message(owner, conversation_id, assistant)
                        self.store.append_message(owner, conversation_id, {'role': 'tool', 'tool_call_id': call_id,
                            'content': json.dumps(payload, ensure_ascii=False), 'created_at': time.time()})
                        write_frame(child.stdin, {'id': call_id, 'ok': result is not None,
                                                  'result': result, 'error': payload.get('error')})
                        if isinstance(result, dict) and isinstance(result.get('autonomy_draft'), dict):
                            draft = result['autonomy_draft']
                            self._record_event(owner, conversation_id, 'autonomy.draft_created', id=call_id, **draft)
                            yield sse_event('autonomy.draft_created', id=call_id, **draft)
                        scope = ({'result_set_id': result.get('result_set_id'), **(result.get('summary') or {})}
                                 if result and result.get('result_set_id') else None)
                        self._record_event(owner, conversation_id, 'tool.completed', id=call_id,
                            tool=name, label=name, status='success' if result is not None else 'error',
                            summary='' if result is not None else payload['error'], result_scope=scope)
                        yield sse_event('tool.completed', id=call_id, tool=name, result=result,
                            result_scope=({**scope, 'sample': result.get('preview') or []} if scope else None),
                            error=None if result is not None else payload['error'], run_id=run_id)
                    elif kind == 'checkpoint':
                        serialized = json.dumps(frame.get('checkpoint'), ensure_ascii=False)
                        if api_key and (api_key in serialized or json.dumps(api_key, ensure_ascii=False)[1:-1] in serialized):
                            raise ValueError('unsafe Pi checkpoint')
                        commit_checkpoint(self.store, owner, conversation_id, token, frame['checkpoint'],
                                          history_start=history_start, role=role, telemetry=telemetry)
                        telemetry = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
                                     'compressions': 0, 'context_window': runtime.context_window_tokens}
                    elif kind == 'final':
                        if not authorized():
                            raise PermissionError('session or evidence authorization changed')
                        text = ''.join(streamed_text)
                        if not text:
                            raise ValueError('empty answer')
                        notice = monitoring_notice(monitoring_queried, verified_samples, total_samples, language)
                        if notice:
                            notice = '\n\n' + notice.rstrip()
                            text += notice
                        answer = {'id': uuid.uuid4().hex, 'role': 'assistant',
                                  'content': (reset_notice if reset_history else '') + text,
                                  'created_at': time.time()}
                        commit_checkpoint(self.store, owner, conversation_id, token, frame['checkpoint'], answer,
                                          history_start=history_start, role=role, telemetry=telemetry)
                        if notice:
                            yield sse_event('assistant.delta', run_id=run_id, content=notice)
                        yield sse_event('run.completed', run_id=run_id, conversation_id=conversation_id)
                        return
                    elif kind == 'error':
                        raise ValueError('Pi process failed')
                search_from = len(buffer)
        except Exception as exc:
            db.session.rollback()
            logger.warning('Pi chat failed: run_id=%s error_type=%s', run_id, type(exc).__name__)
            if isinstance(exc, PermissionError):
                error = '会话或权限已发生变化，请重新登录或联系管理员。'
            elif isinstance(exc, AgentStoreConflict):
                error = '该会话正在运行，请等待当前请求完成。'
            elif str(exc) == 'previous draft outcome unknown':
                error = '上次草稿创建结果不确定，请先到自治任务中确认，再新建会话继续，避免重复创建。'
            else:
                error = '调查未完成，请稍后重试。'
            yield sse_event('run.failed', run_id=run_id, message=error)
        finally:
            if child is not None:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            if token:
                self.store.release_run_lock(owner, conversation_id, token)
