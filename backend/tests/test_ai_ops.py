import datetime
import json
from types import SimpleNamespace

import pytest
from flask import Flask

from app.ai import ops
from app.ai.autonomy import views
from app.api import autonomy_routes


def _payload(status="firing", **label_overrides):
    labels = {
        "alertname": "ServiceDown",
        "service": "nginx",
        "ogs_host_id": "7",
        "ogs_system_user_id": "19",
        "instance": "web-01:9100",
        "job": "node",
    }
    labels.update(label_overrides)
    return {
        "version": "4",
        "groupKey": "{}:{alertname=\"ServiceDown\"}",
        "status": status,
        "commonLabels": {"ignored": "data"},
        "alerts": [{
            "status": status,
            "labels": labels,
            "annotations": {"promql": "malicious query is ignored"},
            "startsAt": "2026-08-29T08:00:00Z",
        }],
    }


def test_alertmanager_parser_builds_opaque_stable_trigger():
    first = ops.parse_alertmanager(_payload())
    second = ops.parse_alertmanager(_payload())
    assert first == second
    assert first.trigger_ref != _payload()["groupKey"]
    assert len(first.trigger_ref) == 64
    assert first.host_id == 7
    assert first.system_user_id == 19
    assert "promql" not in first.goal.lower()


def test_alertmanager_parser_requires_a_service_identity():
    payload = _payload(service="", alertname="")
    with pytest.raises(ops.OpsValidationError, match="service or alertname"):
        ops.parse_alertmanager(payload)


@pytest.mark.parametrize("payload", [
    {},
    _payload(status="suppressed"),
    {**_payload(), "alerts": []},
    {**_payload(), "alerts": _payload()["alerts"] * 2},
    _payload(ogs_host_id="not-an-id"),
])
def test_alertmanager_parser_rejects_out_of_contract_payloads(payload):
    with pytest.raises(ops.OpsValidationError):
        ops.parse_alertmanager(payload)


def test_bearer_is_constant_contract(monkeypatch):
    monkeypatch.setattr(ops.config, "AI_ALERTMANAGER_OWNER", "admin")
    monkeypatch.setattr(ops.config, "AI_ALERTMANAGER_TOKEN", "x" * 32)
    assert ops.verify_bearer("Bearer " + "x" * 32)
    assert not ops.verify_bearer("Bearer " + "x" * 31 + "y")
    assert not ops.verify_bearer("Basic " + "x" * 32)


class _Response:
    headers = {}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        payload = {
            "status": "success",
            "data": {"result": [{
                "metric": {"secret_label": "must-not-return"},
                "values": [[1, "0"], [2, "1"]],
            }]},
        }
        yield json.dumps(payload).encode("utf-8")


def test_prometheus_uses_only_fixed_bounded_query():
    calls = []

    def request_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    client = ops.PrometheusClient(
        "https://prometheus.example.com/prom",
        "metric-token",
        request_get=request_get,
    )
    result = client.service_availability(
        instance='web"01:9100',
        job="node",
        now=datetime.datetime(2026, 8, 29, tzinfo=datetime.timezone.utc),
    )
    url, kwargs = calls[0]
    assert url == "https://prometheus.example.com/prom/api/v1/query_range"
    assert kwargs["timeout"] == 5
    assert kwargs["stream"] is True
    assert kwargs["params"]["step"] == 30
    assert kwargs["params"]["end"] - kwargs["params"]["start"] == 900
    assert kwargs["params"]["query"] == 'up{instance="web\\"01:9100",job="node"}'
    assert kwargs["headers"]["Authorization"] == "Bearer metric-token"
    assert result == {
        "query_template": "service_availability",
        "range_seconds": 900,
        "step_seconds": 30,
        "sample_count": 2,
        "values": [[1, "0"], [2, "1"]],
    }


def test_prometheus_rejects_unbounded_or_invalid_responses():
    class LargeResponse(_Response):
        def iter_content(self, chunk_size):
            yield b"x" * (ops.PROMETHEUS_MAX_BYTES + 1)

    client = ops.PrometheusClient(
        "https://prometheus.example.com", request_get=lambda *_a, **_k: LargeResponse(),
    )
    with pytest.raises(ops.PrometheusQueryError, match="too large"):
        client.service_availability(instance="web:9100", job="node")

    class BadTimestampResponse(_Response):
        def iter_content(self, chunk_size):
            payload = {
                "status": "success",
                "data": {"result": [{
                    "values": [[{"nested": True}, "1"]],
                }]},
            }
            yield json.dumps(payload).encode("utf-8")

    client = ops.PrometheusClient(
        "https://prometheus.example.com",
        request_get=lambda *_a, **_k: BadTimestampResponse(),
    )
    with pytest.raises(ops.PrometheusQueryError, match="timestamp"):
        client.service_availability(instance="web:9100", job="node")


class _WebhookRepo:
    def __init__(self):
        self.run = None
        self.calls = []

    def find_run_by_trigger(self, owner, trigger_type, trigger_ref):
        return dict(self.run) if self.run else None

    def create_run(self, owner, role, **kwargs):
        self.calls.append(("create", owner, role, kwargs))
        self.run = {
            "id": "run-1", "status": "draft", "trigger_ref": kwargs["trigger_ref"],
        }
        return dict(self.run)

    def record_evidence(self, owner, run_id, **kwargs):
        self.calls.append(("evidence", owner, run_id, kwargs))
        return {"id": "evidence-1"}

    def start_run(self, owner, role, run_id):
        self.calls.append(("start", owner, role, run_id))
        self.run["status"] = "queued"
        return dict(self.run)


def test_firing_webhook_creates_starts_and_deduplicates(monkeypatch):
    monkeypatch.setattr(autonomy_routes, "_secure", lambda view, *_roles: view)
    app = Flask(__name__)
    app.config["TESTING"] = True
    autonomy_routes.register_autonomy_routes(app)
    repo = _WebhookRepo()
    dispatched = []
    monkeypatch.setattr(views, "_repo", lambda: repo)
    monkeypatch.setattr(views, "is_autonomy_enabled", lambda: True)
    monkeypatch.setattr(views, "_configured_alert_owner", lambda: "admin")
    monkeypatch.setattr(views, "_dispatch_drive", dispatched.append)
    monkeypatch.setattr(views.config, "AI_PROMETHEUS_BASE_URL", "")
    monkeypatch.setattr(ops.config, "AI_ALERTMANAGER_OWNER", "admin")
    monkeypatch.setattr(ops.config, "AI_ALERTMANAGER_TOKEN", "x" * 32)
    monkeypatch.setattr(
        "app.ai.tools.PlatformQueryService",
        lambda *_args: SimpleNamespace(
            validate_asset_sys_user_id_pair=lambda *_values: True,
        ),
    )
    client = app.test_client()
    headers = {"Authorization": "Bearer " + "x" * 32}

    first = client.post(
        "/ai/ops/alertmanager/webhook", json=_payload(), headers=headers,
    )
    assert first.status_code == 202
    assert first.get_json()["data"]["duplicate"] is False
    assert repo.calls[0][3]["mode"] == "ask"
    assert repo.calls[0][3]["trigger_type"] == "alertmanager"
    assert dispatched == ["run-1"]

    second = client.post(
        "/ai/ops/alertmanager/webhook", json=_payload(), headers=headers,
    )
    assert second.status_code == 202
    assert second.get_json()["data"]["duplicate"] is True
    assert [call[0] for call in repo.calls].count("create") == 1


def test_webhook_rejects_wrong_token_before_parsing(monkeypatch):
    monkeypatch.setattr(autonomy_routes, "_secure", lambda view, *_roles: view)
    app = Flask(__name__)
    autonomy_routes.register_autonomy_routes(app)
    monkeypatch.setattr(views, "is_autonomy_enabled", lambda: True)
    monkeypatch.setattr(ops.config, "AI_ALERTMANAGER_OWNER", "admin")
    monkeypatch.setattr(ops.config, "AI_ALERTMANAGER_TOKEN", "x" * 32)
    response = app.test_client().post(
        "/ai/ops/alertmanager/webhook",
        data=b"not-json",
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_webhook_requires_query_labels_when_prometheus_is_configured(monkeypatch):
    monkeypatch.setattr(autonomy_routes, "_secure", lambda view, *_roles: view)
    app = Flask(__name__)
    autonomy_routes.register_autonomy_routes(app)
    monkeypatch.setattr(views, "is_autonomy_enabled", lambda: True)
    monkeypatch.setattr(ops.config, "AI_ALERTMANAGER_OWNER", "admin")
    monkeypatch.setattr(ops.config, "AI_ALERTMANAGER_TOKEN", "x" * 32)
    monkeypatch.setattr(
        ops.config, "AI_PROMETHEUS_BASE_URL", "https://prometheus.example.com",
    )
    response = app.test_client().post(
        "/ai/ops/alertmanager/webhook",
        json=_payload(instance="", job=""),
        headers={"Authorization": "Bearer " + "x" * 32},
    )
    assert response.status_code == 400


def test_webhook_bounds_stream_without_content_length(monkeypatch):
    monkeypatch.setattr(autonomy_routes, "_secure", lambda view, *_roles: view)
    app = Flask(__name__)
    autonomy_routes.register_autonomy_routes(app)
    monkeypatch.setattr(views, "is_autonomy_enabled", lambda: True)
    monkeypatch.setattr(ops.config, "AI_ALERTMANAGER_OWNER", "admin")
    monkeypatch.setattr(ops.config, "AI_ALERTMANAGER_TOKEN", "x" * 32)
    response = app.test_client().post(
        "/ai/ops/alertmanager/webhook",
        data=b"x" * (ops.ALERTMANAGER_MAX_BYTES + 1),
        headers={"Authorization": "Bearer " + "x" * 32},
        environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True},
    )
    assert response.status_code == 413
