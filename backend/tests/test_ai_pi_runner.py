"""Contract checks for the Pi chat bridge using synthetic data."""
import json
from types import SimpleNamespace

import pytest


def test_monitoring_notice_distinguishes_verified_samples_from_model_inference():
    from app.ai.pi_runner import monitoring_notice

    assert monitoring_notice(False, 0, 0) == ''
    assert '未对本轮监控采样逐条核验' in monitoring_notice(True, 0, 0)
    notice = monitoring_notice(True, 6, 10)
    assert '6/10 个核验项通过' in notice
    assert '其余数值与分析仍需结合原始证据核对' in notice
    assert '6/10 passed' in monitoring_notice(True, 6, 10, 'en-US')


def test_asset_history_rechecks_current_permission_and_ownership():
    from app.ai.pi_runner import asset_history_authorized

    record = {'id': 'r1', 'owner': 'alice', 'conversation_id': 'c1',
              'kind': 'assets', 'resource_ids': [1]}
    store = SimpleNamespace(get_result_set=lambda _owner, _rid: record)
    conversation = {'messages': [
        {'role': 'assistant', 'tool_calls': [{'id': 'a', 'function': {'name': 'search_assets'}}]},
        {'role': 'tool', 'tool_call_id': 'a', 'content': json.dumps({'ok': True, 'result_set_id': 'r1'})},
    ]}
    platform = SimpleNamespace(validate_asset_ids=lambda ids: ids == [1])
    assert asset_history_authorized(conversation, store, platform, 'alice', 'c1')
    platform.validate_asset_ids = lambda _ids: False
    assert not asset_history_authorized(conversation, store, platform, 'alice', 'c1')
    platform.validate_asset_ids = lambda _ids: True
    record['owner'] = 'bob'
    assert not asset_history_authorized(conversation, store, platform, 'alice', 'c1')
    record['owner'] = 'alice'
    record['conversation_id'] = 'other'
    assert not asset_history_authorized(conversation, store, platform, 'alice', 'c1')
    record['conversation_id'] = 'c1'
    record['resource_ids'] = '1'
    assert not asset_history_authorized(conversation, store, platform, 'alice', 'c1')


def test_history_rejects_tools_and_knowledge_scopes_no_longer_allowed(monkeypatch):
    from app.ai import knowledge, pi_runner
    from app.ai.pi_runner import history_authorized

    def message(name, payload):
        return [
            {'role': 'assistant', 'tool_calls': [{'id': 't', 'function': {'name': name}}]},
            {'role': 'tool', 'tool_call_id': 't', 'content': json.dumps(payload)},
        ]
    platform = SimpleNamespace(authorized_knowledge_scopes=lambda: ('global', 'host:1'))
    store = SimpleNamespace()
    document = SimpleNamespace(scope='host:1', approved=True, version=1)
    monkeypatch.setattr(pi_runner, 'db', SimpleNamespace(session=object()))
    monkeypatch.setattr(knowledge, 'KnowledgeService',
                        lambda *_: SimpleNamespace(_document_row=lambda *_: document))
    conversation = {'messages': message('search_knowledge', {
        'ok': True, 'knowledge_references': [{'scope': 'host:1', 'document_id': 'doc1', 'version': 1}],
    })}
    assert history_authorized(conversation, store, platform, 'alice', 'c1', {'search_knowledge'})
    document.approved = False
    assert not history_authorized(conversation, store, platform, 'alice', 'c1', {'search_knowledge'})
    document.approved = True
    platform.authorized_knowledge_scopes = lambda: ('global',)
    assert not history_authorized(conversation, store, platform, 'alice', 'c1', {'search_knowledge'})
    assert not history_authorized(conversation, store, platform, 'alice', 'c1', set())


def test_monitoring_history_rechecks_result_identity_and_mapping(monkeypatch):
    from app.ai import pi_runner as bridge

    record = {'id': 'r1', 'owner': 'alice', 'conversation_id': 'c1',
              'kind': 'monitoring_observation'}
    store = SimpleNamespace(get_result_set=lambda *_: record)
    conversation = {'messages': [
        {'role': 'assistant', 'tool_calls': [{'id': 'q', 'function': {
            'name': 'query_prometheus', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'q', 'content': json.dumps({
            'ok': True, 'result_set_id': 'r1'})},
    ]}
    calls = []
    monkeypatch.setattr(bridge, '_validate_monitoring_result_access',
                        lambda result, platform, allowed_source_types: calls.append(allowed_source_types))
    assert bridge.history_authorized(conversation, store, object(), 'alice', 'c1', {'query_prometheus'}, ('prometheus',))
    assert calls == [{'prometheus'}]
    record['conversation_id'] = 'other'
    assert not bridge.history_authorized(conversation, store, object(), 'alice', 'c1', {'query_prometheus'})


@pytest.mark.parametrize('name,kind,rows,ids,scope', [
    ('get_platform_overview', 'overview', [{}], [], {'groups': ['web'], 'cron_ids': [2]}),
    ('search_cron_jobs', 'cron_jobs', [{'id': 2}], [2], None),
    ('list_authorized_system_users', 'system_users', [{'id': 3, 'alias': 'ops'}], [3], None),
])
def test_history_rechecks_overview_cron_and_system_user_scope(name, kind, rows, ids, scope):
    from app.ai.pi_runner import history_authorized

    record = {'id': 'r1', 'owner': 'alice', 'conversation_id': 'c1', 'kind': kind,
              'rows': rows, 'resource_ids': ids, 'authorization_scope': scope}
    store = SimpleNamespace(get_result_set=lambda *_: record)
    platform = SimpleNamespace(_allowed_groups=lambda: {'web'}, validate_cron_ids=lambda *_: True,
                               resolve_system_user=lambda _id: {'alias': 'ops'})
    conversation = {'messages': [
        {'role': 'assistant', 'tool_calls': [{'id': 'a', 'function': {'name': name}}]},
        {'role': 'tool', 'tool_call_id': 'a', 'content': json.dumps({'ok': True, 'result_set_id': 'r1'})},
    ]}
    assert history_authorized(conversation, store, platform, 'alice', 'c1', {name})
    if name == 'get_platform_overview':
        record['authorization_scope'] = None
        assert not history_authorized(conversation, store, platform, 'alice', 'c1', {name})
        record['authorization_scope'] = scope
    platform._allowed_groups = lambda: set()
    platform.validate_cron_ids = lambda *_: False
    platform.resolve_system_user = lambda *_: None
    assert not history_authorized(conversation, store, platform, 'alice', 'c1', {name})


def test_checkpoint_is_encrypted_owner_bound_and_rejects_tampering(monkeypatch):
    from cryptography.fernet import Fernet
    from app.ai.pi_runner import open_checkpoint, seal_checkpoint

    monkeypatch.setenv('OGS_FERNET_KEY', Fernet.generate_key().decode())
    monkeypatch.delenv('OGS_FERNET_KEYS', raising=False)
    checkpoint = {'entries': [{'payload': 'fake-sensitive-value'}], 'leafId': 'e1'}
    ciphertext = seal_checkpoint('alice', 'c1', checkpoint)
    assert 'fake-sensitive-value' not in ciphertext
    assert open_checkpoint('alice', 'c1', ciphertext) == checkpoint
    with pytest.raises(ValueError):
        open_checkpoint('bob', 'c1', ciphertext)
    with pytest.raises(ValueError):
        open_checkpoint('alice', 'c2', ciphertext)
    with pytest.raises((RuntimeError, ValueError)):
        open_checkpoint('alice', 'c1', ciphertext[:-8] + 'abcdefgh')


def test_progress_checkpoint_is_owner_bound_and_requires_current_run_lock(monkeypatch):
    from cryptography.fernet import Fernet
    from app.ai.pi_runner import commit_checkpoint, open_checkpoint

    monkeypatch.setenv('OGS_FERNET_KEY', Fernet.generate_key().decode())
    monkeypatch.delenv('OGS_FERNET_KEYS', raising=False)
    writes = []
    redis = SimpleNamespace(eval=lambda *args: writes.append(args) or 1)
    store = SimpleNamespace(redis=redis, conversation_ttl=60,
        get_conversation=lambda owner, cid: {'owner': owner, 'id': cid, 'messages': []},
        _run_lock_key=lambda owner, cid: f'lock:{owner}:{cid}',
        _conversation_key=lambda owner, cid: f'conv:{owner}:{cid}',
        _conversation_index=lambda owner: f'index:{owner}')
    commit_checkpoint(store, 'alice', 'c1', 'current-lock', {'entries': []}, telemetry={
        'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10,
        'compressions': 1, 'context_window': 262144, 'context_tokens': 123, 'finish_reason': 'stop',
    })
    args = writes[0]
    assert args[1:5] == (3, 'lock:alice:c1', 'conv:alice:c1', 'index:alice')
    saved = json.loads(args[6])
    assert saved['messages'] == []
    assert saved['pi_checkpoint_message_count'] == 0
    assert saved['pi_history_start'] == 0
    assert saved['state']['provider_observability']['usage']['total_tokens'] == 10
    assert saved['state']['provider_observability']['compression_count'] == 1
    assert saved['state']['provider_observability']['context_budget'] == {
        'context_window_tokens': 262144, 'effective_input_tokens': 262144, 'estimated_input_tokens': 123,
    }
    assert open_checkpoint('alice', 'c1', saved['pi_checkpoint']) == {'entries': []}
    redis.eval = lambda *_args: 0
    with pytest.raises(ValueError, match='lock lost'):
        commit_checkpoint(store, 'alice', 'c1', 'stale-lock', {'entries': []})


def test_pi_runner_uses_existing_registry_streams_events_and_saves_progress(monkeypatch):
    import io
    import tempfile
    from app.ai import pi_runner as bridge

    frames = [
        {'type': 'authorize.request', 'id': 'auth-1'},
        {'type': 'tool.request', 'name': 'search_assets', 'arguments': {'alias': 'fake'}, 'id': 'tool-1'},
        {'type': 'checkpoint', 'checkpoint': {'entries': [{'type': 'session'}]}},
        {'type': 'authorize.request', 'id': 'auth-2'},
        {'type': 'text.delta', 'text': 'Found '},
        {'type': 'text.delta', 'text': 'one asset.'},
        {'type': 'final', 'text': 'Found one asset.', 'checkpoint': {'entries': [{'type': 'session'}]}},
    ]
    source = tempfile.TemporaryFile()
    source.write(('\n'.join(json.dumps(frame) for frame in frames) + '\n').encode())
    source.seek(0)
    child = SimpleNamespace(stdin=io.BytesIO(), stdout=source, poll=lambda: 0, wait=lambda **_kwargs: 0)
    conversation = {'id': 'c1', 'owner': 'alice', 'messages': [], 'state': {},
                    'provider_code': 'fake', 'autonomy_mode': 'custom',
                    'autonomy_profile': {'action_categories': ['systemd']}}
    record = {'id': 'r1', 'owner': 'alice', 'conversation_id': 'c1', 'kind': 'assets', 'resource_ids': [1]}
    def append_message(_owner, _cid, message):
        conversation['messages'].append(message)
        return conversation
    store = SimpleNamespace(acquire_run_lock=lambda *_: 'lock', release_run_lock=lambda *_: None,
        refresh_run_lock=lambda *_: None, append_message=append_message,
        get_conversation=lambda *_: conversation, get_result_set=lambda *_: record)
    row = SimpleNamespace(provider_code='fake', model='fake-model', base_url='https://example.com/v1',
                          api_key_ciphertext='fake-cipher')
    providers = SimpleNamespace(configured_row=lambda *_: row,
        runtime=lambda *_args, **_kwargs: SimpleNamespace(context_window_tokens=262144,
            adapter=SimpleNamespace(model='fake-model', extra_body={})))
    calls, saved, events, registry_args, spawn_args = [], [], [], [], []
    definitions = [{'type': 'function', 'function': {'name': 'search_assets', 'parameters': {'type': 'object'}}}]
    def registry_factory(**kwargs):
        registry_args.append(kwargs)
        def execute(name, args):
            calls.append((name, args))
            return {'result_set_id': 'r1', 'summary': {'total': 1}, 'preview': [{'id': 1}]}
        return SimpleNamespace(definitions=lambda: definitions, execute=execute)
    monkeypatch.setattr(bridge, 'session_authorized', lambda *_: True)
    monkeypatch.setattr(bridge, 'PlatformQueryService',
        lambda *_: SimpleNamespace(validate_asset_ids=lambda ids: ids == [1]))
    monkeypatch.setattr(bridge, 'ToolRegistry', registry_factory)
    monkeypatch.setattr(bridge, 'decrypt_secret', lambda *_: 'fake-api-key')
    monkeypatch.setattr(bridge, 'commit_checkpoint', lambda *args, **kwargs: saved.append((args, kwargs)))
    monkeypatch.setattr(bridge, 'db', SimpleNamespace(session=SimpleNamespace(expire_all=lambda: None, rollback=lambda: None)))
    monkeypatch.setattr(bridge.PiAgentRunner, '_record_event',
        lambda _self, _owner, _cid, name, **fields: events.append((name, fields)))
    monkeypatch.setenv('OGS_FAKE_DATABASE_PASSWORD', 'not-for-child')
    monkeypatch.setattr(bridge.subprocess, 'Popen',
                        lambda *_args, **kwargs: spawn_args.append(kwargs) or child)
    monkeypatch.setattr(bridge.select, 'select', lambda r, _w, _x, _timeout: (r, [], []))
    user_message = 'Find fake assets. ' + 'x' * 8100
    try:
        output = list(bridge.PiAgentRunner(store=store, provider_service=providers).run(
            owner='alice', role='user', conversation_id='c1', message=user_message))
    finally:
        source.close()
    assert calls == [('search_assets', {'alias': 'fake'})]
    assert registry_args[0]['autonomy_mode'] == 'custom'
    assert registry_args[0]['autonomy_profile'] == {'action_categories': ['systemd']}
    assert [name for name, _fields in events] == ['tool.started', 'tool.completed']
    assert any('event: run.completed' in item for item in output)
    assert not any('event: run.failed' in item for item in output)
    deltas = [json.loads(item.split('data: ', 1)[1])['content'] for item in output if 'event: assistant.delta' in item]
    assert deltas == ['Found ', 'one asset.']
    assert len(saved) == 2, 'completed tool turn and final answer should be checkpointed'
    assert saved[-1][0][5]['content'] == ''.join(deltas)
    init = json.loads(child.stdin.getvalue().splitlines()[0])
    assert init['message'] == user_message
    assert conversation['messages'][0]['content'] == user_message
    assert init['provider']['context_window'] == 262144
    assert init['tools'] == definitions
    assert 'OGS_FAKE_DATABASE_PASSWORD' not in spawn_args[0]['env']
    assert 'PATH' in spawn_args[0]['env']
