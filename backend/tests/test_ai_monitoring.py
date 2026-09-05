import json
import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.fernet import Fernet
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _Response:
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield json.dumps(self.payload).encode("utf-8")


def _resolve_public(_hostname, port, **_kwargs):
    return [(2, 1, 6, "", ("192.0.2.10", port))]


def test_synthetic_protocol_harness_exercises_all_four_adapters(monkeypatch):
    from app.ai import monitoring

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def _reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/dashboards/uid/node-overview":
                return self._reply({"dashboard": {"panels": [{
                    "id": 1, "title": "Availability",
                    "datasource": {"uid": "prom-main", "type": "prometheus"},
                    "targets": [{"refId": "A", "expr": 'up{instance="$instance"}'}],
                }]}})
            if "/loki/" in self.path:
                return self._reply({
                    "status": "success",
                    "data": {"result": [{"values": [["1", "service ready"]]}]},
                })
            return self._reply({
                "status": "success",
                "data": {"result": [{"values": [[1, "1"]]}]},
            })

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            if self.path == "/api/ds/query":
                return self._reply({"results": {"A": {"frames": [{
                    "data": {"values": [[1], [1.0]]},
                }]}}})
            method = request["method"]
            results = {
                "problem.get": [],
                "item.get": [{
                    "itemid": "7", "name": "load", "key_": "system.load",
                    "lastvalue": "1", "lastclock": "1", "units": "",
                    "value_type": "0",
                }],
                "history.get": [{"itemid": "7", "clock": "1", "value": "1"}],
            }
            self._reply({"jsonrpc": "2.0", "id": 1, "result": results[method]})

    server = ThreadingHTTPServer(("localhost", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class LocalConnection(http.client.HTTPConnection):
        def __init__(self, _hostname, _address, _port, timeout):
            super().__init__("localhost", server.server_port, timeout=timeout)

    monkeypatch.setattr(monitoring, "_PinnedHTTPConnection", LocalConnection)
    adapter = monitoring.MonitoringAdapter(resolver=_resolve_public)
    base = f"http://monitoring.example.com:{server.server_port}"
    cases = [
        ({"id": 1, "name": "prom", "source_type": "prometheus", "base_url": base},
         {"labels": {"instance": "web"}}),
        ({"id": 2, "name": "grafana", "source_type": "grafana", "base_url": base},
         {"datasource_uid": "prom-main", "datasource_type": "prometheus", "dashboard_uid": "node-overview", "labels": {"instance": "web"}}),
        ({"id": 3, "name": "loki", "source_type": "loki", "base_url": base},
         {"labels": {"host": "web"}}),
        ({"id": 4, "name": "zabbix", "source_type": "zabbix", "base_url": base + "/api_jsonrpc.php"},
         {"hostid": "9"}),
    ]
    try:
        runtime_cases = [
            (adapter.query_prometheus, {
                "metric": "up", "lookback_minutes": 15, "step_seconds": 30,
            }),
            (adapter.query_grafana_panel, {
                "panel_id": 1, "lookback_minutes": 15,
            }),
            (adapter.query_loki, {
                "contains": "ready", "lookback_minutes": 15, "limit": 20,
            }),
            (adapter.query_zabbix_history, {
                "item_ids": ["7"], "lookback_minutes": 15,
            }),
        ]
        results = [method(
            source={**source, "token": "", "verify_tls": False},
            mapping=mapping, arguments=arguments,
        ) for (source, mapping), (method, arguments) in zip(cases, runtime_cases)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert [result["source_type"] for result in results] == [
        "prometheus", "grafana", "loki", "zabbix",
    ]
    assert adapter.request_count == 6


def test_dynamic_prometheus_query_returns_every_bounded_sample_to_the_model():
    from app.ai.monitoring import MonitoringAdapter

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response({
            "status": "success",
            "data": {"result": [{
                "metric": {
                    "instance": "web-01:9100", "cpu": "0",
                    "api_token": "must-not-leak",
                },
                "values": [[1, "0.2"], [2, "0.8"], [3, "0.4"]],
            }]},
        })

    result = MonitoringAdapter(request=request, resolver=_resolve_public).query_prometheus(
        source={
            "id": 7, "name": "metrics", "source_type": "prometheus",
            "base_url": "https://prometheus.example.com", "verify_tls": True,
        },
        mapping={"labels": {"instance": "web-01:9100"}},
        arguments={
            "metric": "node_cpu_seconds_total", "calculation": "rate",
            "range_minutes": 5, "aggregation": "avg", "group_by": ["cpu"],
            "lookback_minutes": 15, "step_seconds": 30,
        },
    )

    observation = result["observations"][0]
    assert calls[0][2]["params"]["query"] == (
        'avg by (cpu) (rate(node_cpu_seconds_total{instance="web-01:9100"}[5m]))'
    )
    assert observation["query"] == calls[0][2]["params"]["query"]
    assert observation["series"] == [{
        "labels": {"instance": "web-01:9100", "cpu": "0"},
        "samples": [[1.0, 0.2], [2.0, 0.8], [3.0, 0.4]],
    }]
    assert "must-not-leak" not in json.dumps(result)


def test_dynamic_loki_query_returns_every_bounded_redacted_line_to_the_model():
    from app.ai.monitoring import MonitoringAdapter

    def request(_method, _url, **_kwargs):
        return _Response({
            "status": "success",
            "data": {"result": [{
                "stream": {"host": "web-01", "token": "must-not-leak"},
                "values": [
                    ["3", "request timeout id=1"],
                    ["2", "password=hunter2 request timeout id=2"],
                    ["1", "request timeout id=3"],
                ],
            }]},
        })

    result = MonitoringAdapter(request=request, resolver=_resolve_public).query_loki(
        source={
            "id": 8, "name": "logs", "source_type": "loki",
            "base_url": "https://loki.example.com", "verify_tls": True,
        },
        mapping={"labels": {"host": "web-01"}},
        arguments={"contains": "timeout", "lookback_minutes": 15, "limit": 20},
    )

    observation = result["observations"][0]
    assert observation["line_count"] == 3
    assert [item["timestamp"] for item in observation["items"]] == ["3", "2", "1"]
    assert all(item["labels"] == {"host": "web-01"} for item in observation["items"])
    assert "hunter2" not in json.dumps(result)
    assert "must-not-leak" not in json.dumps(result)


def test_dynamic_zabbix_history_returns_every_bounded_sample():
    from app.ai.monitoring import MonitoringAdapter

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        rpc_method = kwargs["json"]["method"]
        if rpc_method == "item.get":
            payload = {"jsonrpc": "2.0", "id": 1, "result": [{
                "itemid": "88", "name": "CPU load", "key_": "system.cpu.load",
                "units": "", "value_type": "0",
            }]}
        else:
            payload = {"jsonrpc": "2.0", "id": 2, "result": [
                {"itemid": "88", "clock": "100", "value": "6.1"},
                {"itemid": "88", "clock": "101", "value": "7.2"},
            ]}
        return _Response(payload)

    result = MonitoringAdapter(request=request, resolver=_resolve_public).query_zabbix_history(
        source={
            "id": 9, "name": "zabbix", "source_type": "zabbix",
            "base_url": "https://zabbix.example.com/api_jsonrpc.php",
            "token": "zabbix-token", "verify_tls": True,
        },
        mapping={"hostid": "10101"},
        arguments={"item_ids": ["88"], "lookback_minutes": 60},
    )

    assert [call[2]["json"]["method"] for call in calls] == [
        "item.get", "history.get",
    ]
    assert all(call[0] == "POST" for call in calls)
    assert all(call[2]["headers"]["Authorization"] == "Bearer zabbix-token" for call in calls)
    assert result["observations"][0] == {
        "template": "zabbix_history",
        "sample_count": 2,
        "items": [{
            "itemid": "88", "name": "CPU load", "key": "system.cpu.load",
            "units": "", "samples": [[100, 6.1], [101, 7.2]],
        }],
    }


def test_grafana_query_runs_only_the_selected_saved_dashboard_panel():
    from app.ai.monitoring import MonitoringAdapter

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/api/dashboards/uid/node-overview"):
            return _Response({"dashboard": {"panels": [{
                "id": 6, "title": "Memory",
                "datasource": {"uid": "prom-main", "type": "prometheus"},
                "targets": [{"refId": "A", "expr": "memory_metric"}],
            }, {
                "id": 7,
                "title": "CPU usage",
                "datasource": {"uid": "prom-main", "type": "prometheus"},
                "targets": [{
                    "refId": "A",
                    "expr": '100 - avg(rate(node_cpu_seconds_total{instance="$instance",mode="idle"}[5m])) * 100',
                }],
            }]}})
        assert url.endswith("/api/ds/query")
        return _Response({"results": {"A": {"frames": [{
            "schema": {"fields": [{"name": "Time"}, {"name": "Value"}]},
            "data": {"values": [[1, 2], [12.5, 18.0]]},
        }]}}})

    result = MonitoringAdapter(request=request, resolver=_resolve_public).query_grafana_panel(
        source={
            "id": 10, "name": "grafana", "source_type": "grafana",
            "base_url": "https://grafana.example.com", "token": "grafana-token",
            "verify_tls": True,
        },
        mapping={
            "datasource_uid": "prom-main",
            "datasource_type": "prometheus",
            "dashboard_uid": "node-overview",
            "labels": {"instance": "web-01:9100", "job": "node"},
        },
        arguments={"panel_id": 7, "lookback_minutes": 60},
    )

    assert [call[1].split("grafana.example.com", 1)[1] for call in calls] == [
        "/api/dashboards/uid/node-overview",
        "/api/ds/query",
    ]
    assert [call[0] for call in calls] == ["GET", "POST"]
    query = calls[1][2]["json"]
    assert query["from"] < query["to"]
    assert query["queries"] == [{
        "refId": "A",
        "expr": '100 - avg(rate(node_cpu_seconds_total{instance="web-01:9100",mode="idle"}[5m])) * 100',
        "datasource": {"uid": "prom-main", "type": "prometheus"},
        "maxDataPoints": 1000,
        "intervalMs": 30000,
    }]
    assert result["source_type"] == "grafana"
    assert result["datasource_type"] == "prometheus"
    assert result["dashboard_uid"] == "node-overview"
    assert result["observations"] == [{
        "template": "dashboard_panel",
        "panel_id": 7,
        "panel_title": "CPU usage",
        "sample_count": 2,
        "latest": 18.0,
        "minimum": 12.5,
        "maximum": 18.0,
        "frames": [{
            "fields": [
                {"name": "Time", "type": "", "labels": {}},
                {"name": "Value", "type": "", "labels": {}},
            ],
            "values": [[1, 2], [12.5, 18.0]],
        }],
    }]


def test_grafana_discovery_returns_uid_and_identity_without_datasource_secrets():
    from app.ai.monitoring import MonitoringAdapter

    def request(_method, url, **_kwargs):
        if url.endswith("/api/datasources"):
            return _Response([{
                "uid": "prom-main", "name": "Production metrics",
                "type": "prometheus", "url": "https://prometheus.example.com",
                "secureJsonFields": {"httpHeaderValue1": True},
            }])
        return _Response({
            "status": "success", "data": ["web-01:9100", "other:9100"],
        })

    rows = MonitoringAdapter(request=request, resolver=_resolve_public).discover(
        {
            "id": 10, "name": "grafana", "source_type": "grafana",
            "base_url": "https://grafana.example.com", "token": "token",
            "verify_tls": True,
        },
        {"alias": "web-01", "ip": "192.0.2.10"},
    )

    assert rows == [{
        "label": "web-01:9100", "match": "instance",
        "source_name": "Production metrics",
        "external_ref": {
            "labels": {"instance": "web-01:9100"},
            "datasource_uid": "prom-main",
            "datasource_type": "prometheus",
        },
    }]
    assert "prometheus.example.com" not in json.dumps(rows)
    assert "secureJsonFields" not in json.dumps(rows)


def test_zabbix_connection_test_checks_version_and_token_scope():
    from app.ai.monitoring import MonitoringAdapter

    methods = []

    def request(_method, _url, **kwargs):
        methods.append(kwargs["json"]["method"])
        result = "7.0.0" if methods[-1] == "apiinfo.version" else []
        return _Response({"jsonrpc": "2.0", "id": len(methods), "result": result})

    result = MonitoringAdapter(request=request, resolver=_resolve_public).test_connection({
        "id": 9, "name": "zabbix", "source_type": "zabbix",
        "base_url": "https://zabbix.example.com/api_jsonrpc.php",
        "token": "token", "verify_tls": True,
    })

    assert methods == ["apiinfo.version", "host.get"]
    assert result == {"ok": True, "source_type": "zabbix", "version": "7.0.0"}


def test_grafana_connection_test_checks_supported_datasource_query_access():
    from app.ai.monitoring import MonitoringAdapter

    paths = []

    def request(_method, url, **_kwargs):
        paths.append(url.removeprefix("https://grafana.example.com"))
        if url.endswith("/api/health"):
            return _Response({"database": "ok", "version": "12.0.0"})
        if url.endswith("/api/login/ping"):
            return _Response({"message": "Logged in"})
        if url.endswith("/api/datasources"):
            return _Response([{"uid": "prom-main", "type": "prometheus"}])
        return _Response({"status": "success", "data": ["instance"]})

    result = MonitoringAdapter(
        request=request, resolver=_resolve_public,
    ).test_connection({
        "id": 9, "name": "grafana", "source_type": "grafana",
        "base_url": "https://grafana.example.com",
        "token": "token", "verify_tls": True,
    })

    assert result["ok"] is True
    assert paths == [
        "/api/health",
        "/api/login/ping",
        "/api/datasources",
        "/api/datasources/proxy/uid/prom-main/api/v1/labels",
    ]


def test_runtime_dns_resolution_rejects_metadata_destination_before_request():
    from app.ai.monitoring import MonitoringAdapter, MonitoringError

    called = False

    def request(_method, _url, **_kwargs):
        nonlocal called
        called = True

    def resolve(_hostname, _port, **_kwargs):
        return [(2, 1, 6, "", ("169.254.169.254", 443))]

    with pytest.raises(MonitoringError, match="unsafe monitoring destination"):
        MonitoringAdapter(request=request, resolver=resolve).test_connection({
            "id": 10, "name": "metrics", "source_type": "prometheus",
            "base_url": "https://metrics.example.com", "verify_tls": True,
        })

    assert called is False


def test_runtime_request_connects_to_validated_ip_with_original_tls_hostname(monkeypatch):
    from app.ai import monitoring

    captured = {}

    class Response:
        status = 200
        headers = {}
        body = b'{"status":"success","data":{"version":"1"}}'

        def read(self, chunk_size):
            chunk, self.body = self.body[:chunk_size], self.body[chunk_size:]
            return chunk

        def close(self):
            return None

    class Connection:
        def __init__(self, hostname, address, port, timeout, context):
            captured.update(
                hostname=hostname, address=address, port=port,
                timeout=timeout, context=context,
            )

        def request(self, _method, path, **kwargs):
            captured.update(path=path, headers=kwargs["headers"])

        def getresponse(self):
            return Response()

        def close(self):
            return None

    monkeypatch.setattr(monitoring, "_PinnedHTTPSConnection", Connection)
    result = monitoring.MonitoringAdapter(
        resolver=lambda _host, port, **_kwargs: [
            (2, 1, 6, "", ("192.0.2.44", port)),
        ],
    ).test_connection({
        "id": 10, "name": "metrics", "source_type": "prometheus",
        "base_url": "https://metrics.example.com", "verify_tls": True,
    })

    assert result["ok"] is True
    assert captured["address"] == "192.0.2.44"
    assert captured["hostname"] == "metrics.example.com"
    assert captured["context"].check_hostname is True
    assert captured["headers"]["Host"] == "metrics.example.com"


def test_grafana_discovery_ignores_unsafe_remote_datasource_uid():
    from app.ai.monitoring import MonitoringAdapter

    calls = []

    def request(_method, url, **_kwargs):
        calls.append(url)
        return _Response([{
            "uid": "../api/admin", "name": "unsafe", "type": "prometheus",
        }])

    rows = MonitoringAdapter(
        request=request, resolver=_resolve_public,
    ).discover(
        {
            "id": 11, "name": "grafana", "source_type": "grafana",
            "base_url": "https://grafana.example.com", "verify_tls": True,
        },
        {"alias": "web", "ip": "192.0.2.20"},
    )

    assert rows == []
    assert calls == ["https://grafana.example.com/api/datasources"]


def test_monitoring_output_redacts_common_credentials():
    from app.ai.monitoring import MonitoringAdapter

    def request(_method, _url, **_kwargs):
        return _Response({
            "status": "success",
            "data": {"result": [{
                "stream": {"host": "web"},
                "values": [["1", "password=hunter2 Authorization: Bearer abc"]],
            }]},
        })

    result = MonitoringAdapter(
        request=request, resolver=_resolve_public,
    ).query_loki(
        source={
            "id": 12, "name": "logs", "source_type": "loki",
            "base_url": "https://loki.example.com", "verify_tls": True,
        },
        mapping={"labels": {"host": "web"}},
        arguments={"contains": "password", "lookback_minutes": 15, "limit": 20},
    )

    text = json.dumps(result)
    assert "hunter2" not in text
    assert "Bearer abc" not in text
    assert "REDACTED" in text


def test_monitoring_adapter_enforces_cross_source_request_budget():
    from app.ai.monitoring import MonitoringAdapter, MonitoringError

    adapter = MonitoringAdapter(
        request=lambda *_args, **_kwargs: _Response({"status": "success"}),
        resolver=_resolve_public,
    )
    source = {
        "base_url": "https://metrics.example.com", "verify_tls": True,
    }
    for _ in range(12):
        adapter._http_json(source, "GET", "/api/v1/status/buildinfo")
    with pytest.raises(MonitoringError, match="request budget"):
        adapter._http_json(source, "GET", "/api/v1/status/buildinfo")


@pytest.mark.parametrize("label", ["1instance", "主机"])
def test_monitoring_mapping_rejects_nonstandard_label_names(
    monitoring_config_env, label,
):
    from app.ai.monitoring import MonitoringConfigService, MonitoringValidationError

    service = MonitoringConfigService(monitoring_config_env)
    source = service.create_source("admin", {
        "name": "metrics-" + label,
        "source_type": "prometheus",
        "base_url": "https://prometheus.example.com",
    })
    with pytest.raises(MonitoringValidationError):
        service.save_mapping("admin", source["id"], 9, {
            "labels": {label: "web"},
        })


@pytest.fixture()
def monitoring_config_env(monkeypatch):
    from app.core.db.database import (
        db, t_ai_monitoring_host_mapping, t_ai_monitoring_source,
    )

    monkeypatch.setenv("OGS_FERNET_KEYS", Fernet.generate_key().decode("ascii"))
    engine = create_engine("sqlite:///:memory:")
    db.metadata.create_all(engine, tables=[
        t_ai_monitoring_source.__table__,
        t_ai_monitoring_host_mapping.__table__,
    ])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def test_monitoring_source_secret_is_encrypted_and_never_returned(
    monitoring_config_env,
):
    from app.ai.monitoring import MonitoringConfigService
    from app.core.db.database import t_ai_monitoring_source

    service = MonitoringConfigService(monitoring_config_env)
    created = service.create_source("admin", {
        "name": "Prometheus A",
        "source_type": "prometheus",
        "base_url": "http://192.0.2.10:9090",
        "token": "secret-monitor-token",
        "verify_tls": False,
        "enabled": True,
    })

    row = monitoring_config_env.query(t_ai_monitoring_source).one()
    assert row.token_ciphertext != "secret-monitor-token"
    assert created["token_configured"] is True
    assert "token" not in created
    assert "token_ciphertext" not in json.dumps(created)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9090",
    "http://[::1]:9090",
    "http://169.254.169.254/latest/meta-data",
])
def test_monitoring_source_rejects_loopback_and_metadata_destinations(
    monitoring_config_env, url,
):
    from app.ai.monitoring import MonitoringConfigService, MonitoringValidationError

    with pytest.raises(MonitoringValidationError):
        MonitoringConfigService(monitoring_config_env).create_source("admin", {
            "name": "unsafe",
            "source_type": "prometheus",
            "base_url": url,
        })


def test_monitoring_mapping_is_confirmed_once_per_source_and_host(
    monitoring_config_env,
):
    from app.ai.monitoring import MonitoringConfigService

    service = MonitoringConfigService(monitoring_config_env)
    source = service.create_source("admin", {
        "name": "Zabbix A",
        "source_type": "zabbix",
        "base_url": "https://zabbix.example.com/api_jsonrpc.php",
    })
    first = service.save_mapping(
        "admin", source["id"], 9, {"hostid": "10101"},
    )
    second = service.save_mapping(
        "admin", source["id"], 9, {"hostid": "20202"},
    )

    assert first["external_ref"] == {"hostid": "10101"}
    assert second["id"] == first["id"]
    assert second["external_ref"] == {"hostid": "20202"}


def test_monitoring_discovery_aggregates_sources_and_preserves_partial_failure(
    monitoring_config_env,
):
    from app.ai.monitoring import (
        MonitoringAnalysisService, MonitoringConfigService, MonitoringError,
    )

    config = MonitoringConfigService(monitoring_config_env)
    prom = config.create_source("admin", {
        "name": "metrics", "source_type": "prometheus",
        "base_url": "https://prometheus.example.com",
    })
    loki = config.create_source("admin", {
        "name": "logs", "source_type": "loki",
        "base_url": "https://loki.example.com",
    })
    config.save_mapping("admin", prom["id"], 9, {"labels": {"instance": "web"}})
    config.save_mapping("admin", loki["id"], 9, {"labels": {"host": "web"}})
    for index in range(3):
        extra = config.create_source("admin", {
            "name": f"metrics-{index}", "source_type": "prometheus",
            "base_url": f"https://prometheus-{index}.example.com",
        })
        config.save_mapping(
            "admin", extra["id"], 9, {"labels": {"instance": "web"}},
        )

    class Adapter:
        def discover_capabilities(self, **kwargs):
            if kwargs["source"]["source_type"] == "loki":
                raise MonitoringError("Loki query failed")
            return {
                "source_id": kwargs["source"]["id"],
                "source_type": "prometheus", "status": "ok", "metrics": ["up"],
            }

    class Platform:
        @staticmethod
        def validate_asset_ids(host_ids):
            return host_ids == [9]

    result = MonitoringAnalysisService(
        monitoring_config_env, Platform(), Adapter(),
    ).discover({"host_id": 9})

    assert [row["status"] for row in result["rows"]] == [
        "ok", "failed", "ok", "ok", "ok",
    ]
    assert result["summary"] == {
        "title": "monitoring_catalog",
        "total": 5,
        "source_count": 5, "succeeded": 4,
        "failed": 1, "partial": True,
    }


def test_monitoring_rest_contract_keeps_admin_mutation_and_user_read_separate(
    monkeypatch,
):
    from app.ai import views
    from app.api import ai_api
    from app.ai import tools as ai_tools

    calls = []

    class Config:
        def list_sources(self):
            return [{"id": 1, "name": "metrics", "source_type": "prometheus"}]

        def create_source(self, actor, payload):
            calls.append(("create", actor, payload))
            return {"id": 2, "name": payload["name"], "token_configured": True}

        def sources_for_host(self, host_id):
            calls.append(("list_host", host_id))
            return [{"id": 1, "name": "metrics", "source_type": "prometheus"}]

    monkeypatch.setattr(ai_api, "_secure", lambda view, *_roles: view)
    monkeypatch.setattr(views, "_monitoring_config", lambda: Config())
    monkeypatch.setattr(views, "_identity", lambda: (None, "alice", "user"))
    monkeypatch.setattr(
        ai_tools, "PlatformQueryService",
        lambda *_args: type("Platform", (), {
            "validate_asset_ids": staticmethod(lambda ids: ids == [9]),
        })(),
    )
    app = Flask(__name__)
    ai_api.register_ai_routes(app)
    client = app.test_client()

    created = client.post("/ai/admin/monitoring-sources", json={
        "name": "logs", "token": "never-return-this",
    })
    listed = client.get("/ai/monitoring/sources?host_id=9")
    invalid_selection = client.post("/ai/chat", json={
        "conversation_id": "conversation-1",
        "message": "检查监控",
        "monitoring_source_types": ["prometheus", "remote_write"],
    })

    assert created.status_code == 200
    assert created.get_json()["data"] == {
        "id": 2, "name": "logs", "token_configured": True,
    }
    assert "never-return-this" not in created.get_data(as_text=True)
    assert listed.get_json()["data"] == [
        {"id": 1, "name": "metrics", "source_type": "prometheus"},
    ]
    assert invalid_selection.status_code == 400
    assert calls == [
        ("create", "alice", {"name": "logs", "token": "never-return-this"}),
        ("list_host", 9),
    ]
