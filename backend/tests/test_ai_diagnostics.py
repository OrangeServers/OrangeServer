"""Public contracts for controlled AI diagnostics."""


def test_diagnostic_rest_routes_use_expected_methods():
    from flask import Flask

    from app.api.ai_api import register_ai_routes

    app = Flask(__name__)
    register_ai_routes(app)
    rules = {rule.rule: set(rule.methods) for rule in app.url_map.iter_rules()}

    assert "GET" in rules["/ai/diagnostic-profiles"]
    assert "POST" in rules["/ai/diagnostics"]
    assert "GET" in rules["/ai/diagnostics/<string:run_id>"]
    assert "POST" in rules["/ai/diagnostics/<string:run_id>/cancel"]
    assert "GET" in rules["/ai/diagnostics/<string:run_id>/evidence"]
    assert "GET" in rules["/ai/diagnostics/<string:run_id>/report"]


def test_profiles_endpoint_exposes_only_fixed_read_only_probes(monkeypatch):
    from flask import Flask

    import app.api.ai_api as routes

    monkeypatch.setattr(routes, "_secure", lambda view, *_roles: view)
    app = Flask(__name__)
    routes.register_ai_routes(app)

    response = app.test_client().get("/ai/diagnostic-profiles")

    assert response.status_code == 200
    payload = response.get_json()
    profiles = payload["profiles"]
    profile_ids = {item["id"] for item in profiles}
    assert {
        "system_baseline",
        "cpu_load",
        "memory_pressure",
        "disk_usage",
        "process_snapshot",
        "port_status",
        "service_status",
        "system_logs",
        "docker_health",
        "docker_logs",
    } <= profile_ids
    assert all(item["probe_count"] >= 1 for item in profiles)
    assert all("command" not in item for item in profiles)
    assert all(
        parameter["name"] != "command"
        for item in profiles
        for parameter in item["parameters"]
    )
    from app.ai.tools import TOOL_DEFINITIONS

    diagnostic_schema = TOOL_DEFINITIONS["run_diagnostic"]["function"]["parameters"]
    assert "system_user_id" in diagnostic_schema["properties"]
    assert "system_user" not in diagnostic_schema["properties"]
    assert "system_user_id" in diagnostic_schema["required"]


def test_ssh_adapter_runs_only_profile_commands_and_sanitizes_evidence():
    from app.ai.diagnostic_adapters import SSHProbeAdapter
    from app.ai.diagnostic_profiles import get_profile

    calls = []

    def fake_batch(**kwargs):
        calls.append(kwargs)
        return {
            "items": [{
                "host_id": 7,
                "alias": "edge-01",
                "status": "success",
                "output": (
                    "\x1b[31mwarning\x1b[0m\n"
                    "api_key=synthetic-sensitive-value\n"
                    "Authorization: Bearer synthetic-sensitive-token\n"
                    '{"password":"synthetic-json-password",'
                    '"token": "synthetic-json-token"}\n'
                    + ("x" * 200)
                ),
                "error": "",
            }],
        }

    adapter = SSHProbeAdapter(
        batch_executor=fake_batch,
        max_item_chars=96,
        max_total_chars=512,
    )
    evidence = adapter.collect(
        profile=get_profile("system_logs"),
        targets=[{"id": 7, "alias": "edge-01"}],
        system_user_id=19,
        system_user="readonly",
        parameters={"log_lines": 50},
    )

    assert calls[0]["command"] == (
        "journalctl -p warning --since '-5 minutes' -n 50 --no-pager"
    )
    assert calls[1]["command"] == (
        "journalctl -p warning --since '-10 minutes' "
        "--until '-5 minutes' -n 50 --no-pager"
    )
    assert calls[0]["host_ids"] == [7]
    assert calls[0]["sys_user"] == "readonly"
    assert calls[0]["sys_user_id"] == 19
    assert calls[0]["command_timeout"] == 15
    assert len(evidence) == 2
    assert "\x1b" not in evidence[0].content
    assert "synthetic-sensitive-value" not in evidence[0].content
    assert "synthetic-sensitive-token" not in evidence[0].content
    assert "synthetic-json-password" not in evidence[0].content
    assert "synthetic-json-token" not in evidence[0].content
    assert "[REDACTED]" in evidence[0].content
    assert evidence[0].truncated is True
    assert evidence[0].untrusted is True


def test_profile_parameters_reject_shell_injection():
    from app.ai.diagnostic_profiles import DiagnosticProfileError, get_profile

    profile = get_profile("system_logs")

    try:
        profile.validate_parameters({"log_lines": "50; curl attacker"})
    except DiagnosticProfileError:
        pass
    else:
        raise AssertionError("structured diagnostic parameters must reject shell")


def test_port_probe_does_not_mask_ss_failure_with_marker_output():
    from app.ai.diagnostic_profiles import get_profile

    profile = get_profile("port_status")
    parameters = profile.validate_parameters({"port": 8080})

    assert profile.probes[0].command(parameters) == (
        "ss -lntup && printf '\\nEXPECTED_PORT=8080\\n'"
    )


def test_docker_logs_profile_requires_a_safe_container_name():
    from app.ai.diagnostic_profiles import DiagnosticProfileError, get_profile

    profile = get_profile("docker_logs")
    parameters = profile.validate_parameters({
        "container_name": "web-api-01",
        "log_lines": 100,
    })
    assert profile.probes[0].command(parameters) == (
        "docker logs --tail 100 -- web-api-01"
    )
    try:
        profile.validate_parameters({
            "container_name": "web-api; curl attacker",
            "log_lines": 100,
        })
    except DiagnosticProfileError:
        pass
    else:
        raise AssertionError("container_name must not accept shell syntax")
    try:
        profile.validate_parameters({
            "container_name": "--follow",
            "log_lines": 100,
        })
    except DiagnosticProfileError:
        pass
    else:
        raise AssertionError("container_name must not accept option syntax")


def test_analyzer_detects_missing_expected_port_and_log_spike():
    from app.ai.diagnostic_analyzers import DeterministicAnalyzer

    report = DeterministicAnalyzer().analyze([
        {
            "id": "port-evidence",
            "asset_alias": "edge-01",
            "probe_id": "listening_ports",
            "kind": "port",
            "status": "success",
            "content": "tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nEXPECTED_PORT=8080",
        },
        {
            "id": "logs-current",
            "asset_alias": "edge-01",
            "probe_id": "warning_logs_current",
            "kind": "logs",
            "status": "success",
            "content": "error\nfailed\npanic\nerror\nfailed\npanic",
        },
        {
            "id": "logs-baseline",
            "asset_alias": "edge-01",
            "probe_id": "warning_logs_baseline",
            "kind": "logs",
            "status": "success",
            "content": "error",
        },
    ])

    assert {item["title"] for item in report["findings"]} == {
        "端口 8080 未监听",
        "日志错误信号突增",
    }
    assert {
        tuple(item["evidence_ids"]) for item in report["findings"]
    } == {
        ("port-evidence",),
        ("logs-current", "logs-baseline"),
    }


def test_analyzer_does_not_claim_log_spike_when_baseline_collection_failed():
    from app.ai.diagnostic_analyzers import DeterministicAnalyzer

    report = DeterministicAnalyzer().analyze([
        {
            "id": "logs-current",
            "asset_alias": "edge-01",
            "probe_id": "warning_logs_current",
            "kind": "logs",
            "status": "success",
            "content": "error\nfailed\npanic\nerror\nfailed\npanic",
        },
        {
            "id": "logs-baseline",
            "asset_alias": "edge-01",
            "probe_id": "warning_logs_baseline",
            "kind": "logs",
            "status": "failed",
            "content": "",
            "error": "journal unavailable",
        },
    ])

    assert "日志错误信号突增" not in {
        item["title"] for item in report["findings"]
    }
    assert {item["title"] for item in report["findings"]} == {
        "诊断证据采集失败",
    }


def test_analyzer_does_not_claim_log_spike_when_baseline_is_empty():
    from app.ai.diagnostic_analyzers import DeterministicAnalyzer

    report = DeterministicAnalyzer().analyze([
        {
            "id": "logs-current",
            "asset_alias": "edge-01",
            "probe_id": "warning_logs_current",
            "kind": "logs",
            "status": "success",
            "content": "error\nfailed\npanic\nerror\nfailed\npanic",
        },
        {
            "id": "logs-baseline",
            "asset_alias": "edge-01",
            "probe_id": "warning_logs_baseline",
            "kind": "logs",
            "status": "success",
            "content": "   \n",
        },
    ])

    assert "日志错误信号突增" not in {
        item["title"] for item in report["findings"]
    }


def test_analyzer_requires_valid_cpu_count_before_claiming_high_load():
    from app.ai.diagnostic_analyzers import DeterministicAnalyzer

    load_evidence = {
        "id": "load-current",
        "asset_alias": "edge-01",
        "probe_id": "uptime",
        "kind": "load",
        "status": "success",
        "content": "load average: 8.0, 6.0, 4.0",
    }
    invalid_cpu_evidence = [
        [],
        [{
            "id": "cpu-invalid",
            "asset_alias": "edge-01",
            "probe_id": "cpu_count",
            "kind": "cpu",
            "status": "success",
            "content": "unknown",
        }],
        [{
            "id": "cpu-failed",
            "asset_alias": "edge-01",
            "probe_id": "cpu_count",
            "kind": "cpu",
            "status": "failed",
            "content": "",
            "error": "getconf unavailable",
        }],
    ]

    for cpu_evidence in invalid_cpu_evidence:
        report = DeterministicAnalyzer().analyze([
            load_evidence,
            *cpu_evidence,
        ])
        assert "系统负载超过 CPU 容量" not in {
            item["title"] for item in report["findings"]
        }


def test_analyzer_detects_load_above_valid_multicore_capacity():
    from app.ai.diagnostic_analyzers import DeterministicAnalyzer

    report = DeterministicAnalyzer().analyze([
        {
            "id": "load-current",
            "asset_alias": "edge-01",
            "probe_id": "uptime",
            "kind": "load",
            "status": "success",
            "content": "load average: 5.5, 4.0, 3.0",
        },
        {
            "id": "cpu-count",
            "asset_alias": "edge-01",
            "probe_id": "cpu_count",
            "kind": "cpu",
            "status": "success",
            "content": "4\n",
        },
    ])

    finding = next(
        item for item in report["findings"]
        if item["title"] == "系统负载超过 CPU 容量"
    )
    assert finding["severity"] == "warning"
    assert finding["summary"] == "1 分钟负载 5.5，在线 CPU 4"
    assert finding["evidence_ids"] == ["load-current", "cpu-count"]


def test_diagnostic_schema_and_rev50_migration_stay_in_sync():
    from pathlib import Path

    from app.core.db.database import (
        t_ai_diagnostic_event,
        t_ai_diagnostic_evidence,
        t_ai_diagnostic_report,
        t_ai_diagnostic_run,
    )

    assert t_ai_diagnostic_run.__tablename__ == "t_ai_diagnostic_run"
    assert t_ai_diagnostic_run.__table__.c.system_user_id.nullable is False
    assert t_ai_diagnostic_event.__table__.c.sequence.nullable is False
    assert (
        t_ai_diagnostic_evidence.__table__.c.content_ciphertext.type.__class__.__name__
        in {"TEXT", "LONGTEXT"}
    )
    assert t_ai_diagnostic_report.__table__.c.findings_json.nullable is False

    backend_root = Path(__file__).resolve().parents[1]
    fresh_schema = (backend_root / "mysqldir" / "orange.sql").read_text(
        encoding="utf-8"
    )
    migration = (
        backend_root / "mysqldir" / "rev50_ai_diagnostics.sql"
    ).read_text(encoding="utf-8")
    for table_name in (
        "t_ai_diagnostic_run",
        "t_ai_diagnostic_event",
        "t_ai_diagnostic_evidence",
        "t_ai_diagnostic_report",
    ):
        assert f"CREATE TABLE `{table_name}`" in fresh_schema
        assert f"CREATE TABLE IF NOT EXISTS `{table_name}`" in migration
    assert "`system_user_id` int NOT NULL" in fresh_schema
    assert "`system_user_id` int NOT NULL" in migration
    assert "COLUMN_NAME = 'system_user_id'" in migration


def test_post_diagnostic_executes_fixed_probe_and_get_returns_authoritative_run(
    monkeypatch,
):
    from flask import Flask

    import app.api.ai_api as routes
    from app.ai import views
    from app.ai.diagnostic_adapters import SSHProbeAdapter
    from app.ai.diagnostics import (
        DiagnosticService,
        MemoryDiagnosticRepository,
    )

    repository = MemoryDiagnosticRepository()

    class Platform:
        def validate_asset_ids(self, target_ids):
            return target_ids == [11]

        def resolve_system_user(self, system_user_id):
            return (
                {"id": 19, "alias": "root-ops", "host_user": "root"}
                if system_user_id == 19 else None
            )

        def validate_asset_sys_user_id_pair(self, target_ids, system_user_id):
            return target_ids == [11] and system_user_id == 19

    def batch(**kwargs):
        return {
            "items": [{
                "host_id": 11,
                "alias": "edge-11",
                "status": "success",
                "output": (
                    "Filesystem Type 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/sda1 ext4 100 96 4 96% /\n"
                ),
                "error": "",
            }],
        }

    service = DiagnosticService(
        repository=repository,
        platform_factory=lambda _owner, _role: Platform(),
        target_resolver=lambda _ids: [{"id": 11, "alias": "edge-11"}],
        privilege_resolver=lambda credential: credential["host_user"] == "root",
        adapter=SSHProbeAdapter(batch_executor=batch),
    )
    monkeypatch.setattr(routes, "_secure", lambda view, *_roles: view)
    monkeypatch.setattr(views, "_diagnostic_service", lambda: service)
    monkeypatch.setattr(views, "_identity", lambda: (None, "alice", "user"))
    app = Flask(__name__)
    routes.register_ai_routes(app)
    client = app.test_client()

    created = client.post("/ai/diagnostics", json={
        "profile_id": "disk_usage",
        "target_ids": [11],
        "system_user_id": 19,
        "parameters": {},
    })

    assert created.status_code == 200
    run = created.get_json()["run"]
    assert run["status"] == "completed"
    assert run["target_count"] == 1
    assert run["success_count"] == 1
    assert run["system_user"] == {
        "id": 19,
        "alias": "root-ops",
        "is_privileged": True,
    }
    assert run["summary"]["severity"] == "critical"
    assert run["latest_event_seq"] >= 4

    loaded = client.get(f"/ai/diagnostics/{run['id']}")
    assert loaded.status_code == 200
    assert loaded.get_json()["run"] == run
    recovered = client.get(
        f"/ai/diagnostics/{run['id']}?after_seq=0"
    ).get_json()["events"]
    assert [event["event_seq"] for event in recovered] == list(
        range(1, len(recovered) + 1)
    )
    assert recovered[-1]["type"] == "diagnostic_completed"
    assert recovered[-1]["success_count"] == 1
    assert recovered[-1]["failed_count"] == 0
    assert recovered[-1]["asset_progress"][0]["status"] == "completed"

    evidence = client.get(f"/ai/diagnostics/{run['id']}/evidence")
    evidence_items = evidence.get_json()["items"]
    assert len(evidence_items) == 2
    assert all("content_ciphertext" not in item for item in evidence_items)
    report = client.get(f"/ai/diagnostics/{run['id']}/report")
    findings = report.get_json()["report"]["findings"]
    assert findings[0]["evidence_ids"]


def test_diagnostic_is_owner_scoped_and_rejects_more_than_ten_targets(
    monkeypatch,
):
    from app.ai.diagnostics import (
        DiagnosticNotFound,
        DiagnosticService,
        DiagnosticValidationError,
        MemoryDiagnosticRepository,
    )

    repository = MemoryDiagnosticRepository()

    class Platform:
        def validate_asset_ids(self, _ids):
            return True

        def resolve_system_user(self, _system_user_id):
            return {"id": 21, "alias": "readonly", "host_user": "web"}

        def validate_asset_sys_user_id_pair(self, _ids, _system_user_id):
            return True

    service = DiagnosticService(
        repository=repository,
        platform_factory=lambda _owner, _role: Platform(),
        target_resolver=lambda ids: [
            {"id": item, "alias": f"host-{item}"} for item in ids
        ],
        privilege_resolver=lambda _credential: False,
        adapter=object(),
    )
    try:
        service.start(
            owner="alice",
            role="user",
            payload={
                "profile_id": "disk_usage",
                "target_ids": list(range(11)),
                "system_user_id": 21,
            },
        )
    except DiagnosticValidationError as exc:
        assert "10" in str(exc)
    else:
        raise AssertionError("diagnostic target limit must fail closed")

    repository.create_run({
        "id": "owner-scoped",
        "owner": "alice",
        "status": "queued",
    })
    try:
        service.get_run("bob", "owner-scoped")
    except DiagnosticNotFound:
        pass
    else:
        raise AssertionError("another owner must not read diagnostics")


def test_diagnostic_revalidates_permissions_before_each_probe():
    from app.ai.diagnostic_adapters import SSHProbeAdapter
    from app.ai.diagnostics import (
        DiagnosticService,
        MemoryDiagnosticRepository,
    )

    class Platform:
        def __init__(self):
            self.asset_checks = 0

        def validate_asset_ids(self, _ids):
            self.asset_checks += 1
            return self.asset_checks < 3

        def resolve_system_user(self, _system_user_id):
            return {"id": 21, "alias": "readonly", "host_user": "web"}

        def validate_asset_sys_user_id_pair(self, _ids, _system_user_id):
            return True

    platform = Platform()
    calls = []

    def batch(**kwargs):
        calls.append(kwargs["command"])
        return {"items": [{
            "host_id": 1,
            "alias": "edge-01",
            "status": "success",
            "output": "ok",
            "error": "",
        }]}

    service = DiagnosticService(
        repository=MemoryDiagnosticRepository(),
        platform_factory=lambda _owner, _role: platform,
        target_resolver=lambda _ids: [{"id": 1, "alias": "edge-01"}],
        privilege_resolver=lambda _credential: False,
        adapter=SSHProbeAdapter(batch_executor=batch),
    )
    run = service.start(
        owner="alice",
        role="user",
        payload={
            "profile_id": "disk_usage",
            "target_ids": [1],
            "system_user_id": 21,
        },
    )

    assert len(calls) == 1
    assert run["status"] == "interrupted"


def test_chat_sse_projects_diagnostic_events_and_followup_context(monkeypatch):
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.ai.storage import AgentStore
    from test_ai_agent_state import FakeRedis
    from test_ai_runner import _run_pi_bridge

    worker_context = {"active": False}

    class DiagnosticService:
        def start(self, *, owner, role, payload, on_event):
            assert worker_context["active"] is True
            assert payload["conversation_id"]
            for event in (
                {"type": "diagnostic_started", "event_seq": 1, "run_id": "diag-1", "status": "running"},
                {"type": "diagnostic_progress", "event_seq": 2, "run_id": "diag-1",
                 "status": "running", "asset": {"alias": "edge-01", "status": "success"}},
                {"type": "diagnostic_completed", "event_seq": 3, "run_id": "diag-1", "status": "partial"},
            ):
                on_event(event)
            return {
                "id": "diag-1",
                "status": "partial",
                "summary": {"severity": "critical", "finding_count": 1, "evidence_count": 2},
            }

        def report(self, owner, run_id, role=None):
            return {
                "run_id": run_id,
                "status": "partial",
                "summary": "发现 1 个需关注项",
                "severity": "critical",
                "evidence_insufficient": False,
                "findings": [{
                    "title": "磁盘使用率过高",
                    "severity": "critical",
                    "asset_alias": "edge-01",
                    "summary": "最高使用率 96%",
                    "evidence_ids": ["evidence-1"],
                    "recommendation": "先定位大文件",
                }],
            }

        def conversation_runs(self, owner, conversation_id, limit=5, role=None):
            return [{
                "id": "diag-1",
                "status": "partial",
                "profile_name": "磁盘与 inode",
                "success_count": 0,
                "failed_count": 1,
                "summary": {"severity": "critical", "finding_count": 1},
            }]

    @contextmanager
    def diagnostic_worker_context():
        worker_context["active"] = True
        try:
            yield
        finally:
            worker_context["active"] = False

    store = AgentStore(FakeRedis())
    conversation = store.create_conversation("alice", "openai", "demo")
    result = store.create_result_set(
        "alice",
        conversation["id"],
        "assets",
        rows=[{"id": 1, "alias": "edge-01"}],
        resource_ids=[1],
    )
    service = DiagnosticService()
    platform = SimpleNamespace(validate_asset_ids=lambda ids: ids == [1])
    frames = [
        {"type": "authorize.request", "id": "auth-1"},
        {"type": "tool.request", "name": "run_diagnostic", "id": "diag-call", "arguments": {
            "profile_id": "disk_usage",
            "result_set_id": result["id"],
            "system_user_id": 21,
            "parameters": {},
        }},
        {"type": "checkpoint", "checkpoint": {"entries": [{"type": "session"}]}},
        {"type": "authorize.request", "id": "auth-2"},
        {"type": "text.delta", "text": "磁盘诊断完成"},
        {"type": "final", "text": "磁盘诊断完成", "checkpoint": {"entries": [{"type": "session"}]}},
    ]
    output, child, _commits, _registries = _run_pi_bridge(
        monkeypatch,
        store,
        conversation,
        frames,
        definitions=[{"type": "function", "function": {"name": "run_diagnostic", "parameters": {}}}],
        execute=lambda registry, _name, arguments: registry.diagnostic_executor(arguments),
        diagnostic_service_factory=lambda: service,
        worker_context_factory=diagnostic_worker_context,
        platform=platform,
    )

    text = "".join(output)
    assert "event: diagnostic_started" in text
    assert "event: diagnostic_progress" in text
    assert "event: diagnostic_completed" in text
    assert "event: run.completed" in text
    child_messages = [json.loads(line) for line in child.stdin.getvalue().splitlines()]
    tool_result = next(frame for frame in child_messages if frame.get("result", {}).get("report"))
    assert "磁盘使用率过高" in json.dumps(tool_result, ensure_ascii=False)
    assert "evidence-1" in json.dumps(tool_result, ensure_ascii=False)
    assert worker_context["active"] is False


def test_conversation_detail_includes_active_and_latest_diagnostic(monkeypatch):
    from flask import Flask

    from app.ai import views
    from app.ai.storage import AgentStore
    from test_ai_agent_state import FakeRedis

    store = AgentStore(FakeRedis())
    conversation = store.create_conversation("alice", "openai", "demo")
    runs = [
        {"id": "running-1", "status": "running"},
        {"id": "completed-1", "status": "completed"},
    ]
    stored = store.get_conversation("alice", conversation["id"])
    stored["state"]["provider_observability"] = {
        "usage": {"total_tokens": 120},
        "last_finish_reason": "length",
        "last_latency_ms": 321,
        "truncation_reason": "output_limit",
        "compression_count": 2,
        "context_budget": {
            "context_window_tokens": 262144,
            "effective_input_tokens": 220000,
            "estimated_input_tokens": 1000,
        },
        "prompt": "must-not-leak",
    }
    store.save_conversation("alice", stored)

    class Service:
        def conversation_runs(
            self, owner, conversation_id, limit=5, role=None
        ):
            assert owner == "alice"
            assert conversation_id == conversation["id"]
            return runs

    monkeypatch.setattr(views, "_store", lambda: store)
    monkeypatch.setattr(views, "_diagnostic_service", lambda: Service())
    monkeypatch.setattr(views, "_identity", lambda: (None, "alice", "user"))
    app = Flask(__name__)
    with app.test_request_context(
        f"/ai/conversations/{conversation['id']}"
    ):
        response = views.conversation_detail(conversation["id"])

    payload = response.get_json()["conversation"]
    assert payload["diagnostics"] == runs
    assert payload["active_diagnostic"]["id"] == "running-1"
    assert payload["latest_diagnostic"]["id"] == "running-1"
    observability = payload["provider_observability"]
    assert observability["usage"] == {"total_tokens": 120}
    assert observability["truncation_reason"] == "output_limit"
    assert "prompt" not in observability


def test_provider_adapter_reports_usage_and_length():
    from types import SimpleNamespace

    from app.ai.provider import OpenAICompatibleAdapter

    response = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="length",
            message=SimpleNamespace(content="partial", tool_calls=[]),
        )],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30, total_tokens=150),
    )

    class Completions:
        def create(self, **kwargs):
            if kwargs["stream"]:
                raise RuntimeError("no stream")
            return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    adapter = OpenAICompatibleAdapter(
        api_key="synthetic-test-key",
        base_url="https://example.test/v1",
        model="demo",
        client_factory=lambda **_kwargs: client,
    )

    result = adapter.complete(messages=[{"role": "user", "content": "x"}])

    assert result.usage == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }
    assert result.finish_reason == "length"
    assert result.truncated is True
    assert result.latency_ms >= 0


def test_pi_runner_persists_provider_usage_without_prompt_or_credentials(monkeypatch):
    from app.ai.storage import AgentStore
    from test_ai_agent_state import FakeRedis
    from test_ai_runner import _run_pi_bridge

    store = AgentStore(FakeRedis())
    conversation = store.create_conversation("alice", "openai", "demo")
    frames = [
        {"type": "authorize.request", "id": "auth-1"},
        {"type": "usage", "input": 100, "cacheRead": 10, "cacheWrite": 5,
         "output": 20, "stopReason": "length", "contextUsage": {"tokens": 1000}},
        {"type": "compaction_end", "ok": True},
        {"type": "text.delta", "text": "输出达到上限"},
        {"type": "final", "text": "输出达到上限", "checkpoint": {"entries": []}},
    ]
    output, _child, commits, _registries = _run_pi_bridge(
        monkeypatch, store, conversation, frames,
    )

    assert "event: run.completed" in "".join(output)
    telemetry = commits[-1]["telemetry"]
    assert telemetry["prompt_tokens"] == 115
    assert telemetry["completion_tokens"] == 20
    assert telemetry["total_tokens"] == 135
    assert telemetry["finish_reason"] == "length"
    assert telemetry["compressions"] == 1
    assert telemetry["context_window"] == 262144
    assert telemetry["context_tokens"] == 1000
    serialized = str(telemetry).lower()
    assert "api_key" not in serialized
    assert "messages" not in serialized


def test_expired_evidence_and_reports_are_not_returned_and_can_be_purged():
    from datetime import datetime, timedelta, timezone

    from app.ai.diagnostics import (
        DiagnosticNotFound,
        MemoryDiagnosticRepository,
    )

    repository = MemoryDiagnosticRepository()
    run = repository.create_run({
        "owner": "alice",
        "status": "completed",
        "evidence_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    })
    repository.add_evidence("alice", run["id"], {
        "content": "expired",
        "error": "",
    })
    repository.save_report("alice", run["id"], {
        "status": "completed",
        "severity": "info",
        "summary": "old",
        "findings": [],
        "evidence_insufficient": False,
    })
    repository.reports[run["id"]]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    assert repository.list_evidence("alice", run["id"]) == []
    try:
        repository.get_report("alice", run["id"])
    except DiagnosticNotFound:
        pass
    else:
        raise AssertionError("expired report must not be returned")
    removed = repository.purge_expired()
    assert removed == {"evidence": 1, "reports": 1, "runs": 0}


def test_expired_audit_run_cascades_events_evidence_and_report():
    from datetime import datetime, timedelta, timezone

    from app.ai.diagnostics import (
        DiagnosticNotFound,
        MemoryDiagnosticRepository,
    )

    repository = MemoryDiagnosticRepository()
    run = repository.create_run({
        "owner": "alice",
        "status": "completed",
        "audit_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    })
    repository.append_event(
        "alice",
        run["id"],
        "diagnostic_completed",
        {"report": {"findings": [{"asset_alias": "edge-01"}]}},
    )
    repository.add_evidence(
        "alice",
        run["id"],
        {"content": "bounded", "error": ""},
    )
    repository.save_report(
        "alice",
        run["id"],
        {
            "status": "completed",
            "severity": "info",
            "summary": "done",
            "findings": [],
            "evidence_insufficient": False,
        },
    )

    removed = repository.purge_expired()

    assert removed["runs"] == 1
    assert run["id"] not in repository.events
    assert run["id"] not in repository.evidence
    assert run["id"] not in repository.reports
    try:
        repository.get_run("alice", run["id"])
    except DiagnosticNotFound:
        pass
    else:
        raise AssertionError("expired diagnostic audit run must be deleted")
