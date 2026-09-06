"""M2/S2 bounded knowledge truth and derived-index contracts."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.knowledge import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    KnowledgeService,
    KnowledgeValidationError,
    _HybridKnowledgeStore,
    _chunks,
)
from app.core.db.database import (
    db,
    t_ai_autonomous_evidence,
    t_ai_autonomous_run,
    t_ai_autonomous_step,
    t_ai_embedding_config,
    t_ai_knowledge_document,
)


class FakeStoreFactory:
    def __init__(self):
        self.operations = []
        self.hits = []
        self.reset_calls = 0
        self.on_batch = None
        self.search_calls = []

    @contextmanager
    def __call__(self, _row, *, reset=False):
        if reset:
            self.reset_calls += 1
            self.operations.clear()
        yield self

    def load(self, records):
        self.operations.extend(records)
        if self.on_batch is not None:
            callback, self.on_batch = self.on_batch, None
            callback()
        return [None] * len(records)

    def search(self, query, *, scopes, limit):
        assert query
        self.search_calls.append((query, scopes, limit))
        return [
            hit for hit in self.hits
            if (hit.get('value') or {}).get('scope') in scopes
        ][:limit]


@pytest.fixture()
def knowledge_env():
    engine = create_engine('sqlite:///:memory:')
    db.metadata.create_all(engine, tables=[
        t_ai_autonomous_run.__table__,
        t_ai_autonomous_step.__table__,
        t_ai_autonomous_evidence.__table__,
        t_ai_embedding_config.__table__,
        t_ai_knowledge_document.__table__,
    ])
    session = sessionmaker(bind=engine)()
    store = FakeStoreFactory()
    service = KnowledgeService(session, store_factory=store)
    yield session, store, service
    session.close()
    engine.dispose()


def test_markdown_splitter_is_bounded_and_overlapping():
    chunks = _chunks('A' * 900, 'Runbook')
    assert len(chunks) >= 3
    assert all(0 < len(item['text']) <= CHUNK_SIZE for item in chunks)
    assert chunks[0]['text'][-CHUNK_OVERLAP:] == chunks[1]['text'][:CHUNK_OVERLAP]


def test_hybrid_store_fuses_redisvl_text_and_vector_rankings(monkeypatch):
    from app.ai import knowledge

    class Index:
        queries = []

        def query(self, query):
            self.queries.append(query)
            if 'KNN 32' in str(query):
                return [
                    {'document_id': 'doc-2', 'text': 'semantic'},
                    {'document_id': 'doc-1', 'text': 'exact'},
                ]
            return [{'document_id': 'doc-1', 'text': 'exact'}]

    index = Index()
    monkeypatch.setattr(
        knowledge, '_embed', lambda _row, texts: [[0.1, 0.2]] * len(texts),
    )
    store = _HybridKnowledgeStore(SimpleNamespace(dimension=2), index)

    hits = store.search('disk full', scopes=('global', 'host:7'), limit=8)

    query_texts = [str(query) for query in index.queries]
    assert len(query_texts) == 2
    assert any('@text:(disk | full)' in query for query in query_texts)
    assert all('@scope:{global}' in query and r'@scope:{host\:7}' in query for query in query_texts)
    assert any('KNN 32' in query for query in query_texts)
    assert [hit['value']['document_id'] for hit in hits] == ['doc-1', 'doc-2']


def test_hybrid_store_serializes_vectors_for_hash_storage(monkeypatch):
    from app.ai import knowledge

    class Index:
        records = None

        def load(self, records, **_kwargs):
            self.records = records

    index = Index()
    monkeypatch.setattr(knowledge, '_embed', lambda _row, _texts: [[0.1, 0.2]])

    _HybridKnowledgeStore(SimpleNamespace(dimension=2), index).load([
        {'id': 'doc-1', 'text': 'disk full'},
    ])

    assert isinstance(index.records[0]['embedding'], bytes)
    assert len(index.records[0]['embedding']) == 2 * 4


def test_document_lifecycle_reindex_and_search_filter(knowledge_env):
    session, store, service = knowledge_env
    document = service.create_document('admin', {
        'title': 'Disk runbook',
        'scope': 'global',
        'content': '# Disk\nCheck filesystem usage before cleanup.',
    })
    assert document['version'] == 1
    assert 'content' not in service.list_documents()[0]
    assert service.get_document(document['id'])['content'].startswith('# Disk')
    assert service.index_state() == 'stale'

    unchanged = service.update_document(document['id'], {'title': 'Disk runbook'})
    assert unchanged['version'] == 1
    updated = service.update_document(document['id'], {
        'content': '# Disk\nInspect filesystem and inode usage.',
    })
    assert updated['version'] == 2

    indexed = service.reindex()
    assert indexed['index_state'] == 'ready'
    assert indexed['indexed_chunks'] == len(store.operations) == 1
    assert store.reset_calls == 1

    value = dict(store.operations[0])
    store.hits = [{'value': value, 'score': 0.91}]
    result = service.search('disk full', limit=99)
    assert result[0]['citation_id'] == 'K1'
    assert result[0]['version'] == 2
    assert result[0]['scope'] == 'global'
    assert result[0]['match_reason'] == 'hybrid BM25 + semantic'

    value['version'] = 1
    store.hits = [{'value': value, 'score': 0.99}]
    assert service.search('stale version') == []

    service.delete_document(document['id'])
    assert service.list_documents() == []
    assert service.index_state() == 'stale'
    session.rollback()


def test_only_resolved_independently_verified_run_can_be_captured(knowledge_env):
    session, _store, service = knowledge_env
    run = t_ai_autonomous_run(
        id='run-1', owner='admin', goal='repair cron', host_id=7,
        host_alias='node-7', system_user_id=3, system_user_alias='root',
        mode='ask', status='completed', outcome='resolved', revision=4,
        budget_json='{}', latest_event_seq=0, graph_version='v2',
    )
    session.add(run)
    session.add(t_ai_autonomous_step(
        id='step-1', run_id='run-1', kind='verification',
        status='failed', seq=1, summary='crond is active', note='',
    ))
    session.commit()

    with pytest.raises(KnowledgeValidationError, match='verification'):
        service.capture_run('admin', 'run-1')

    session.add(t_ai_autonomous_evidence(
        id='ev-1', run_id='run-1', step_id='step-1',
        kind='verification_observation', summary='crond active',
        artifact_ids_json='[]', trusted=False,
    ))
    session.commit()
    with pytest.raises(KnowledgeValidationError, match='successful'):
        service.capture_run('admin', 'run-1')

    session.query(t_ai_autonomous_step).filter_by(id='step-1').update({
        'status': 'succeeded',
    })
    session.commit()
    document = service.capture_run('admin', 'run-1')
    assert document['source_type'] == 'verified_run'
    assert document['source_ref'] == 'run-1'
    assert document['scope'] == 'host:7'
    assert 'crond active' in document['content']


def test_document_size_and_embedding_config_state_are_bounded(knowledge_env):
    _session, _store, service = knowledge_env
    with pytest.raises(KnowledgeValidationError, match='1 MiB'):
        service.create_document('admin', {
            'title': 'too large', 'content': '测' * 400_000,
        })
    config = service.save_config({'provider_type': 'local'})
    assert config['dimension'] == 512
    assert config['api_key_configured'] is False


def test_reindex_request_is_single_flight(knowledge_env):
    _session, _store, service = knowledge_env
    assert service.request_reindex()['index_state'] == 'rebuilding'
    from app.ai.knowledge import KnowledgeConflict
    with pytest.raises(KnowledgeConflict, match='already rebuilding'):
        service.request_reindex()


def test_reindex_failure_and_midflight_change_never_publish_ready(
    knowledge_env, monkeypatch,
):
    from app.ai import knowledge

    session, store, service = knowledge_env
    document = service.create_document('admin', {
        'title': 'Runbook', 'content': 'initial content',
    })
    monkeypatch.setattr(
        knowledge,
        '_chunks',
        lambda _content, _title: [
            {'text': 'x', 'heading': 'h'}
        ] * (knowledge.MAX_INDEX_CHUNKS + 1),
    )
    with pytest.raises(KnowledgeValidationError, match='20000'):
        service.reindex()
    assert service.index_state() == 'error'

    monkeypatch.setattr(
        knowledge, '_chunks',
        lambda content, _title: [{'text': content, 'heading': 'h'}],
    )
    store.on_batch = lambda: service.update_document(document['id'], {
        'content': 'changed during rebuild',
    })
    assert service.reindex()['index_state'] == 'stale'


def test_search_enforces_global_and_current_host_scopes(knowledge_env):
    _session, store, service = knowledge_env
    document = service.create_document('admin', {
        'title': 'Host runbook',
        'scope': 'host:7',
        'content': 'host seven only',
    })
    service.reindex()
    store.hits = [{
        'value': dict(store.operations[0]), 'score': 0.9,
    }]

    assert service.search('host issue') == []
    assert service.search('host issue', scopes=('global', 'host:8')) == []
    result = service.search('host issue', scopes=('global', 'host:7'))
    assert result[0]['document_id'] == document['id']
    assert store.search_calls[-1][1] == ('global', 'host:7')


def test_list_documents_filters_metadata_by_requested_scopes(knowledge_env):
    _session, _store, service = knowledge_env
    global_doc = service.create_document('admin', {
        'title': 'Global runbook', 'scope': 'global', 'content': 'global',
    })
    host_doc = service.create_document('admin', {
        'title': 'Host runbook', 'scope': 'host:7', 'content': 'host seven',
    })
    service.create_document('admin', {
        'title': 'Other host runbook', 'scope': 'host:8',
        'content': 'host eight',
    })

    documents = service.list_documents(scopes=('global', 'host:7'))

    assert {item['id'] for item in documents} == {global_doc['id'], host_doc['id']}
    assert all('content' not in item for item in documents)


def test_previous_index_layout_is_stale_until_rebuilt(knowledge_env):
    session, store, service = knowledge_env
    service.create_document('admin', {
        'title': 'Runbook', 'content': 'restart cron safely',
    })
    service.reindex()
    row = session.get(t_ai_embedding_config, 1)
    row.model_fingerprint = 'legacy-layout'
    row.indexed_fingerprint = 'legacy-layout'
    row.index_state = 'ready'
    session.commit()

    store.search_calls.clear()
    assert service.index_state() == 'stale'
    assert service.search('restart cron') == []
    assert store.search_calls == []

    rebuilt = service.reindex()
    assert rebuilt['index_state'] == 'ready'
    assert rebuilt['model_fingerprint'] != 'legacy-layout'


def test_remote_embedding_config_is_validated_and_marks_index_stale(
    knowledge_env, monkeypatch,
):
    from app.ai import knowledge

    _session, _store, service = knowledge_env
    monkeypatch.setattr(knowledge, 'encrypt_secret', lambda value: 'encrypted:' + value)
    with pytest.raises(KnowledgeValidationError, match='integer'):
        service.save_config({
            'provider_type': 'openai_compatible',
            'base_url': 'https://embedding.example.com/v1',
            'model': 'embed-v1', 'dimension': True, 'api_key': 'fake',
        })
    config = service.save_config({
        'provider_type': 'openai_compatible',
        'base_url': 'https://embedding.example.com/v1',
        'model': 'embed-v1', 'dimension': 1024, 'api_key': 'fake',
    })
    assert config['api_key_configured'] is True
    assert config['index_state'] == 'stale'
    assert config['dimension'] == 1024
