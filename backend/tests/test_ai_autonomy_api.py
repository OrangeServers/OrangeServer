# -*- coding: utf-8 -*-
"""M1/S1: 自治任务最小 API 契约测试（Issue #11）。

覆盖：路由形状、feature flag 默认禁用、v1 管理员限定、异常到 HTTP
状态码的映射、decision 输入恰好为 {operation, expected_revision}，
以及功能禁用时不影响既有 AI 聊天/诊断/批量审批路由。
"""
import pytest
from flask import Flask

import app.ai.autonomy.views as views
import app.api.autonomy_routes as routes_module
from app.ai.autonomy.repository import (
    AutonomyConflict,
    AutonomyNotFound,
    AutonomyPermissionError,
    AutonomyValidationError,
)
from app.ai.autonomy.state import AutonomyStateError


class FakeRepo:
    """记录调用并按需抛错/返回的 repository 替身。"""

    def __init__(self, exc=None, result=None):
        self.calls = []
        self.exc = exc
        self.result = result or {}

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.exc is not None:
            raise self.exc
        return dict(self.result)

    def create_run(self, owner, role, **kwargs):
        return self._record("create_run", owner, role, **kwargs)

    def start_run(self, owner, role, run_id):
        return self._record("start_run", owner, role, run_id)

    def request_cancel(self, owner, role, run_id):
        return self._record("request_cancel", owner, role, run_id)

    def list_runs(self, owner):
        return self._record("list_runs", owner)

    def snapshot(self, owner, run_id):
        return self._record("snapshot", owner, run_id)

    def propose_probe(self, owner, role, run_id, probe_id, params=None):
        return self._record(
            "propose_probe", owner, role, run_id,
            probe_id=probe_id, params=params,
        )

    def decide(self, owner, role, run_id, step_id, operation, expected_revision):
        return self._record(
            "decide", owner, role, run_id, step_id,
            operation=operation, expected_revision=expected_revision,
        )

    def set_host_environment(self, host_id, environment):
        return self._record(
            "set_host_environment", host_id, environment,
        )


@pytest.fixture()
def api(monkeypatch):
    """绕过 require_role/token/CSRF，直接验证视图层契约。"""
    monkeypatch.setattr(
        routes_module, "_secure", lambda view, *_roles: view,
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    routes_module.register_autonomy_routes(app)

    state = {"repo": FakeRepo(), "identity": (None, "admin", "admin")}
    monkeypatch.setattr(views, "_repo", lambda: state["repo"])
    monkeypatch.setattr(views, "_identity", lambda: state["identity"])
    return app.test_client(), state


def _enable(monkeypatch, flag=True):
    monkeypatch.setattr(views, "AI_AUTONOMY_ENABLED", flag)


def test_routes_are_registered_with_expected_verbs():
    app = Flask(__name__)
    routes_module.register_autonomy_routes(app)
    # 同一 URL 的多个动词注册为不同 endpoint，合并后再断言。
    rules = {}
    for rule in app.url_map.iter_rules():
        rules.setdefault(rule.rule, set()).update(rule.methods)
    assert "GET" in rules["/ai/autonomy/status"]
    assert "POST" in rules["/ai/autonomous-runs"]
    assert "GET" in rules["/ai/autonomous-runs"]
    assert "GET" in rules["/ai/autonomous-runs/<string:run_id>"]
    assert "POST" in rules["/ai/autonomous-runs/<string:run_id>/start"]
    assert "POST" in rules[
        "/ai/autonomous-runs/<string:run_id>/cancel"
    ]
    assert "POST" in rules["/ai/autonomous-runs/<string:run_id>/steps"]
    assert "POST" in rules[
        "/ai/autonomous-runs/<string:run_id>/steps/<string:step_id>/decision"
    ]
    assert "POST" in rules[
        "/ai/autonomy/hosts/<int:host_id>/environment"
    ]


def test_status_probe_reports_flag_without_being_blocked(
    api, monkeypatch,
):
    client, _state = api

    def fake_readiness(*, enabled):
        return {
            "enabled": enabled,
            "configured": enabled,
            "checkpoint_ready": enabled,
            "worker_ready": enabled,
            "ready": enabled,
            "reason": "ready" if enabled else "feature_disabled",
        }

    monkeypatch.setattr(views, "autonomy_readiness", fake_readiness)
    _enable(monkeypatch, False)
    response = client.get("/ai/autonomy/status")
    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "enabled": False,
        "configured": False,
        "checkpoint_ready": False,
        "worker_ready": False,
        "ready": False,
        "reason": "feature_disabled",
    }

    _enable(monkeypatch, True)
    response = client.get("/ai/autonomy/status")
    assert response.get_json()["data"] == {
        "enabled": True,
        "configured": True,
        "checkpoint_ready": True,
        "worker_ready": True,
        "ready": True,
        "reason": "ready",
    }


def test_every_mutating_endpoint_is_rejected_when_flag_disabled(
    api, monkeypatch,
):
    client, _state = api
    _enable(monkeypatch, False)
    targets = [
        ("post", "/ai/autonomous-runs", {"goal": "g"}),
        ("get", "/ai/autonomous-runs", None),
        ("get", "/ai/autonomous-runs/r1", None),
        ("post", "/ai/autonomous-runs/r1/start", {}),
        ("post", "/ai/autonomous-runs/r1/cancel", {}),
        ("post", "/ai/autonomous-runs/r1/steps", {}),
        ("post", "/ai/autonomous-runs/r1/steps/s1/decision", {}),
        ("post", "/ai/autonomy/hosts/1/environment", {}),
    ]
    for verb, url, payload in targets:
        if verb == "get":
            response = client.get(url)
        else:
            response = client.post(url, json=payload)
        assert response.status_code == 403, url
        assert "OGS_AI_AUTONOMY_ENABLED" in response.get_json()["msg"]


def test_non_admin_is_rejected_even_when_enabled(api, monkeypatch):
    client, state = api
    _enable(monkeypatch, True)
    state["identity"] = (None, "bob", "user")
    response = client.post(
        "/ai/autonomous-runs",
        json={"goal": "g", "host_id": 1, "system_user_id": 2,
              "mode": "assisted"},
    )
    assert response.status_code == 403


def test_create_run_passes_boundary_inputs_to_repository(api, monkeypatch):
    client, state = api
    _enable(monkeypatch, True)
    state["repo"] = FakeRepo(result={"id": "run-1", "status": "draft"})
    response = client.post("/ai/autonomous-runs", json={
        "goal": "diagnose latency",
        "host_id": 7,
        "system_user_id": 19,
        "mode": "assisted",
        "budget": {"max_actions": 3},
    })
    assert response.status_code == 200
    assert response.get_json()["data"]["id"] == "run-1"
    name, args, kwargs = state["repo"].calls[0]
    assert name == "create_run"
    assert args == ("admin", "admin")
    assert kwargs == {
        "goal": "diagnose latency",
        "host_id": 7,
        "system_user_id": 19,
        "mode": "assisted",
        "budget_payload": {"max_actions": 3},
    }


def test_cancel_only_records_request_and_dispatches(api, monkeypatch):
    client, state = api
    _enable(monkeypatch, True)
    state["repo"] = FakeRepo(result={
        "id": "run-1", "status": "running", "cancel_requested": True,
    })
    dispatched = []
    monkeypatch.setattr(views, "_dispatch_drive", dispatched.append)

    response = client.post("/ai/autonomous-runs/run-1/cancel", json={})

    assert response.status_code == 200
    assert state["repo"].calls == [
        ("request_cancel", ("admin", "admin", "run-1"), {}),
    ]
    assert dispatched == ["run-1"]
    assert response.get_json()["data"]["status"] == "running"
    assert response.get_json()["data"]["cancel_requested"] is True


def test_cancel_terminalized_before_execution_does_not_dispatch(api, monkeypatch):
    client, state = api
    _enable(monkeypatch, True)
    state["repo"] = FakeRepo(result={
        "id": "run-1", "status": "cancelled", "cancel_requested": True,
    })
    dispatched = []
    monkeypatch.setattr(views, "_dispatch_drive", dispatched.append)

    response = client.post("/ai/autonomous-runs/run-1/cancel", json={})

    assert response.status_code == 200
    assert dispatched == []
    assert response.get_json()["data"]["status"] == "cancelled"


def test_decision_input_is_exactly_operation_and_expected_revision(
    api, monkeypatch,
):
    client, state = api
    _enable(monkeypatch, True)
    state["repo"] = FakeRepo(result={"id": "s1", "status": "approved"})
    response = client.post(
        "/ai/autonomous-runs/r1/steps/s1/decision",
        json={"operation": "approve", "expected_revision": 4,
              "ignored_extra": "x"},
    )
    assert response.status_code == 200
    name, args, kwargs = state["repo"].calls[0]
    assert name == "decide"
    assert args == ("admin", "admin", "r1", "s1")
    assert kwargs == {"operation": "approve", "expected_revision": 4}


def test_propose_step_forwards_probe_id_and_params(api, monkeypatch):
    client, state = api
    _enable(monkeypatch, True)
    state["repo"] = FakeRepo(result={"id": "s1", "status": "proposed"})
    response = client.post("/ai/autonomous-runs/r1/steps", json={
        "probe_id": "service.status", "params": {"unit": "nginx"},
    })
    assert response.status_code == 200
    name, args, kwargs = state["repo"].calls[0]
    assert name == "propose_probe"
    assert args == ("admin", "admin", "r1")
    assert kwargs == {"probe_id": "service.status", "params": {"unit": "nginx"}}


def test_set_host_environment_forwards_admin_values(api, monkeypatch):
    client, state = api
    _enable(monkeypatch, True)
    state["repo"] = FakeRepo(result={
        "host_id": 7, "alias": "web-01",
        "previous": "production", "ai_environment": "lab",
    })
    response = client.post(
        "/ai/autonomy/hosts/7/environment", json={"environment": "lab"},
    )
    assert response.status_code == 200
    assert state["repo"].calls[0] == (
        "set_host_environment", (7, "lab"), {},
    )


@pytest.mark.parametrize("exc,status", [
    (AutonomyNotFound("gone"), 404),
    (AutonomyPermissionError("revoked"), 403),
    (AutonomyValidationError("bad goal"), 400),
    (AutonomyConflict("stale revision"), 409),
    (AutonomyStateError("illegal transition"), 409),
])
def test_autonomy_errors_map_to_documented_status_codes(
    api, monkeypatch, exc, status,
):
    client, state = api
    _enable(monkeypatch, True)
    state["repo"] = FakeRepo(exc=exc)
    response = client.post(
        "/ai/autonomous-runs",
        json={"goal": "g", "host_id": 1, "system_user_id": 2,
              "mode": "assisted"},
    )
    assert response.status_code == status
    assert exc.args[0] in response.get_json()["msg"]


def test_unexpected_error_becomes_500_without_details(api, monkeypatch):
    client, state = api
    _enable(monkeypatch, True)
    state["repo"] = FakeRepo(exc=RuntimeError("db exploded"))
    response = client.post(
        "/ai/autonomous-runs",
        json={"goal": "g", "host_id": 1, "system_user_id": 2,
              "mode": "assisted"},
    )
    assert response.status_code == 500
    assert "db exploded" not in response.get_json()["msg"]


def test_disabling_flag_does_not_touch_existing_ai_features():
    """既有 AI 聊天/诊断/批量审批不依赖自治 flag。"""
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    for relpath in (
        "app/api/ai_api.py",
        "app/assets/batch_service.py",
        "app/ai/tools.py",
        "app/ai/runner.py",
    ):
        source = (backend / relpath).read_text(encoding="utf-8")
        assert "AI_AUTONOMY_ENABLED" not in source, relpath


def test_existing_ai_routes_still_register_when_flag_module_loads():
    """加载自治路由模块不改变既有 AI 诊断路由形状。"""
    from app.api.ai_api import register_ai_routes

    app = Flask(__name__)
    register_ai_routes(app)
    routes_module.register_autonomy_routes(app)
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/ai/diagnostic-profiles" in rules
    assert "/ai/diagnostics" in rules
    assert "/ai/autonomous-runs" in rules
