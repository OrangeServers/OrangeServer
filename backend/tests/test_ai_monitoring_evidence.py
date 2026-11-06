"""Monitoring evidence tools exercise the production ToolRegistry boundary."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from test_ai_agent_state import FakeRedis


class _SyntheticAdapter:
    def query_prometheus(self, *, source, mapping, arguments):
        from app.ai.monitoring import MonitoringAdapter

        series, sample_count = MonitoringAdapter._prometheus_series([{
            "metric": {
                "instance": "synthetic-host.example.invalid",
                "api_token": "fictional-monitor-secret",
            },
            "values": [[1000, "1"], [1030, "2"], [1090, "3"], [1120, "4"]],
        }])
        return {
            "source_id": source["id"],
            "source_name": source["name"],
            "source_type": "prometheus",
            "status": "ok",
            "lookback_minutes": arguments.get("lookback_minutes", 60),
            "observations": [{
                "template": "prometheus_query",
                "query": "node_load1{instance=\"synthetic-host\"}",
                "step_seconds": arguments.get("step_seconds", 30),
                "sample_count": sample_count,
                "series": series,
            }],
        }

    query_loki = query_grafana_panel = query_zabbix_history = query_prometheus


class _Platform:
    def __init__(self, session):
        self.session = session
        self.allowed_ids = {9}

    def validate_asset_ids(self, asset_ids):
        return set(asset_ids).issubset(self.allowed_ids)


@pytest.fixture
def evidence_env():
    from app.ai.monitoring import MonitoringAnalysisService
    from app.ai.storage import AgentStore
    from app.ai.tools import ToolRegistry
    from app.core.db.database import (
        db,
        t_ai_monitoring_host_mapping,
        t_ai_monitoring_source,
    )

    engine = create_engine("sqlite:///:memory:")
    db.metadata.create_all(engine, tables=[
        t_ai_monitoring_source.__table__,
        t_ai_monitoring_host_mapping.__table__,
    ])
    session = sessionmaker(bind=engine)()
    source = t_ai_monitoring_source(
        name="synthetic metrics",
        source_type="prometheus",
        base_url="https://prometheus.example.invalid",
        enabled=True,
        verify_tls=True,
        created_by="test",
    )
    session.add(source)
    session.flush()
    mapping = t_ai_monitoring_host_mapping(
        source_id=source.id,
        host_id=9,
        external_ref_json='{"labels":{"instance":"synthetic-host"}}',
        confirmed_by="test",
    )
    session.add(mapping)
    session.commit()

    platform = _Platform(session)
    service = MonitoringAnalysisService(session, platform, _SyntheticAdapter())
    store = AgentStore(FakeRedis())
    conversation = store.create_conversation("alice", "synthetic", "test")
    registry = ToolRegistry(
        store=store,
        platform=platform,
        owner="alice",
        role="user",
        conversation_id=conversation["id"],
        monitoring_executor=lambda operation, arguments: service.query(
            operation, arguments,
        ),
    )
    try:
        yield {
            "session": session,
            "source": source,
            "mapping": mapping,
            "platform": platform,
            "store": store,
            "conversation": conversation,
            "registry": registry,
        }
    finally:
        session.close()
        engine.dispose()


def _query(registry, source_id, **extra):
    return registry.execute("query_prometheus", {
        "host_id": 9,
        "source_id": source_id,
        "metric": "node_load1",
        "lookback_minutes": 15,
        "step_seconds": 30,
        **extra,
    })


def _read(registry, result_set_id, *, offset=0, limit=2):
    return registry.execute("read_monitoring_evidence", {
        "result_set_id": result_set_id,
        "observation_index": 0,
        "series_index": 0,
        "offset": offset,
        "limit": limit,
    })


def test_corrupt_samples_are_not_returned_to_the_model():
    from app.ai.tools import ToolNotAllowed, _monitoring_series_page

    observation = {'series': [{'samples': [[1000.0, 'untrusted text']]}]}
    with pytest.raises(ToolNotAllowed):
        _monitoring_series_page(observation, 0, 0, 1)


def test_registry_pages_evidence_and_computes_statistics_from_full_series(evidence_env):
    from app.ai.tools import TOOL_DEFINITIONS

    registry = evidence_env["registry"]
    names = {item["function"]["name"] for item in registry.definitions()}
    assert {"read_monitoring_evidence", "verify_monitoring_citations"} <= names
    assert "read_monitoring_evidence" in TOOL_DEFINITIONS

    query_result = _query(
        registry, evidence_env["source"].id,
        fictional_secret_note="fictional-monitor-secret",
    )
    result_set_id = query_result["result_set_id"]
    first = _read(registry, result_set_id, offset=0, limit=2)
    second = _read(registry, result_set_id, offset=2, limit=2)

    assert first["page"] == {"offset": 0, "limit": 2, "total": 4}
    assert first["samples"] == [[1000.0, 1.0], [1030.0, 2.0]]
    assert second["samples"] == [[1090.0, 3.0], [1120.0, 4.0]]
    assert first["series_statistics"] == {
        "result_set_id": result_set_id,
        "observation_index": 0,
        "series_index": 0,
        "metric": "node_load1",
        "series": {"labels": {"instance": "synthetic-host.example.invalid"}},
        "count": 4,
        "min": 1.0,
        "max": 4.0,
        "mean": 2.5,
        "first_timestamp": 1000.0,
        "last_timestamp": 1120.0,
        "step_seconds": 30,
        "gap_count": 1,
    }
    assert "fictional-monitor-secret" not in json.dumps(first)
    assert "fictional-monitor-secret" not in json.dumps(second)


def test_registry_verifies_samples_and_rejects_tampered_citations(evidence_env):
    registry = evidence_env["registry"]
    result_set_id = _query(registry, evidence_env["source"].id)["result_set_id"]
    valid = {
        "result_set_id": result_set_id,
        "observation_index": 0,
        "series_index": 0,
        "sample_index": 2,
        "timestamp": 1090.0,
        "value": 3.0,
        "metric": "node_load1",
    }
    tampered = {**valid, "value": 3.5}

    result = registry.execute("verify_monitoring_citations", {
        "citations": [valid, tampered],
    })

    assert result["results"] == [
        {"index": 0, "verified": True},
        {"index": 1, "verified": False},
    ]
    assert result["verified_count"] == 1
    assert result["statistics"][0]["count"] == 4
    assert result["statistics"][0]["mean"] == 2.5


def test_citation_statistics_keep_each_series_separate(evidence_env):
    registry, store = evidence_env['registry'], evidence_env['store']
    result_id = _query(registry, evidence_env['source'].id)['result_set_id']
    record = store.get_result_set('alice', result_id)
    record['rows'][0]['observations'][0]['series'].append({
        'labels': {'instance': 'other'}, 'samples': [[1000.0, 10.0], [1030.0, 20.0]],
    })
    store.redis.set(store._result_key('alice', result_id), store._json(record), ex=store.conversation_ttl)
    common = {'result_set_id': result_id, 'observation_index': 0, 'sample_index': 0,
              'timestamp': 1000.0, 'metric': 'node_load1'}
    result = registry.execute('verify_monitoring_citations', {'citations': [
        {**common, 'series_index': 0, 'value': 1.0},
        {**common, 'series_index': 1, 'value': 10.0},
    ]})
    assert result['verified_count'] == 2
    assert [item['mean'] for item in result['statistics']] == [2.5, 15.0]


def test_registry_rejects_other_conversations_and_tampered_result_scope(evidence_env):
    from app.ai.tools import ToolNotAllowed, ToolRegistry

    registry = evidence_env["registry"]
    result_set_id = _query(registry, evidence_env["source"].id)["result_set_id"]
    store = evidence_env["store"]
    result = store.get_result_set("alice", result_set_id)
    result["rows"][0]["observations"][0]["access_token"] = (
        "fictional-monitor-secret"
    )
    store.redis.set(
        store._result_key("alice", result_set_id),
        store._json(result),
        ex=store.conversation_ttl,
    )
    assert "fictional-monitor-secret" not in json.dumps(
        _read(registry, result_set_id),
    )

    other_conversation = store.create_conversation("alice", "synthetic", "other")
    other_registry = ToolRegistry(
        store=store,
        platform=evidence_env["platform"],
        owner="alice",
        role="user",
        conversation_id=other_conversation["id"],
    )
    with pytest.raises(ToolNotAllowed):
        _read(other_registry, result_set_id)

    other_owner_conversation = store.create_conversation("bob", "synthetic", "other")
    other_owner_registry = ToolRegistry(
        store=store,
        platform=evidence_env["platform"],
        owner="bob",
        role="user",
        conversation_id=other_owner_conversation["id"],
    )
    with pytest.raises(ToolNotAllowed):
        _read(other_owner_registry, result_set_id)

    result = store.get_result_set("alice", result_set_id)
    result["resource_ids"] = [10]
    store.redis.set(
        store._result_key("alice", result_set_id),
        store._json(result),
        ex=store.conversation_ttl,
    )
    with pytest.raises(ToolNotAllowed):
        _read(registry, result_set_id)


@pytest.mark.parametrize("revocation", ["asset", "mapping", "source"])
def test_registry_rechecks_asset_and_monitoring_source_permissions(evidence_env, revocation):
    from app.ai.tools import ToolNotAllowed

    registry = evidence_env["registry"]
    result_set_id = _query(registry, evidence_env["source"].id)["result_set_id"]
    if revocation == "asset":
        evidence_env["platform"].allowed_ids.clear()
    elif revocation == "mapping":
        evidence_env["session"].delete(evidence_env["mapping"])
        evidence_env["session"].commit()
    else:
        evidence_env["source"].enabled = False
        evidence_env["session"].commit()

    with pytest.raises(ToolNotAllowed):
        _read(registry, result_set_id)

    rejected = registry.execute("verify_monitoring_citations", {"citations": [{
        "result_set_id": result_set_id,
        "observation_index": 0,
        "series_index": 0,
        "sample_index": 0,
        "timestamp": 1000.0,
        "value": 1.0,
        "metric": "node_load1",
    }]})
    assert rejected["verified_count"] == 0
    assert rejected["statistics"] == []
