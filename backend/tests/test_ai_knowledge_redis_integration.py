"""Deterministic Redis 8 retrieval evaluation for the public AIOps corpus."""
import json
import os
from pathlib import Path

import pytest


REDIS_URL = os.getenv('OGS_TEST_KNOWLEDGE_REDIS_URL', '').strip()
CORPUS = Path(__file__).with_name('fixtures') / 'ai_knowledge_retrieval_corpus.json'


@pytest.mark.skipif(not REDIS_URL, reason='isolated Redis 8 URL is not configured')
def test_redisvl_hybrid_retrieval_recall_and_mrr(monkeypatch):
    from redis import Redis
    from redis.exceptions import ResponseError
    from app.ai import knowledge

    corpus = json.loads(CORPUS.read_text(encoding='utf-8'))
    vectors = {
        item['text']: item['vector']
        for group in ('documents', 'queries')
        for item in corpus[group]
    }
    index_name = 'ogs_knowledge_test'
    key_prefix = 'ogs:test:knowledge:chunk:'
    legacy_index = 'ogs_knowledge_test_legacy'
    legacy_prefix = 'ogs:test:knowledge:legacy:'
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        client.execute_command('FT.DROPINDEX', legacy_index, 'DD')
    except ResponseError:
        pass
    client.execute_command(
        'FT.CREATE', legacy_index, 'ON', 'HASH', 'PREFIX', 1,
        legacy_prefix, 'SCHEMA', 'text', 'TEXT',
    )
    monkeypatch.setattr(knowledge, 'STORE_PREFIX', index_name)
    monkeypatch.setattr(knowledge, 'KEY_PREFIX', key_prefix)
    monkeypatch.setattr(knowledge, 'LEGACY_INDEXES', (legacy_index,))
    monkeypatch.setattr(knowledge, 'autonomy_redis_url', lambda _database: REDIS_URL)
    monkeypatch.setattr(
        knowledge,
        '_embed',
        lambda _row, texts: [vectors[text] for text in texts],
    )
    row = type('EmbeddingConfig', (), {'dimension': 4})()
    records = [
        {
            'id': item['id'],
            'document_id': item['id'],
            'version': 1,
            'title': item['title'],
            'source_type': 'runbook',
            'source_ref': '',
            'heading': item['title'],
            'scope': item.get('scope', 'global'),
            'text': item['text'],
        }
        for item in corpus['documents']
    ]
    reciprocal_ranks = []
    try:
        with knowledge._redis_store(row, reset=True) as store:
            store.load(records)
            with pytest.raises(ResponseError):
                client.execute_command('FT.INFO', legacy_index)
            global_ids = {
                hit['value']['document_id']
                for hit in store.search('HOST7-ONLY', scopes=('global',), limit=8)
            }
            assert 'host-seven-only' not in global_ids
            for query in corpus['queries']:
                ranked = [
                    hit['value']['document_id']
                    for hit in store.search(
                        query['text'], scopes=tuple(query.get('scopes', ['global'])), limit=8,
                    )
                ]
                rank = ranked.index(query['expected']) + 1 if query['expected'] in ranked else 0
                reciprocal_ranks.append(1 / rank if rank else 0)
        recall_at_8 = sum(value > 0 for value in reciprocal_ranks) / len(reciprocal_ranks)
        mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)
        print(f'knowledge retrieval Recall@8={recall_at_8:.3f} MRR={mrr:.3f}')
        assert recall_at_8 == 1.0
        assert mrr == 1.0
    finally:
        try:
            client.execute_command('FT.DROPINDEX', index_name, 'DD')
        except Exception:
            pass
        try:
            client.execute_command('FT.DROPINDEX', legacy_index, 'DD')
        except Exception:
            pass
        finally:
            client.close()
