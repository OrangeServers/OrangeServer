# -*- coding: utf-8 -*-
"""M1/S3 切片 2：计划快照与计划级授权 digest 契约测试（Issue #16）。

纯函数层验证篡改矩阵：动作/参数/顺序/目标/凭据/策略/预算/图版本/
过期任一变化都使授权失效；未变化时按序重建动作放行。不碰数据库。
"""
import time

import pytest

from app.ai.autonomy.actions import (
    ActionValidationError,
    StructuredAction,
    build_action_digest,
)
from app.ai.autonomy.plans import (
    PLAN_ACTION_KINDS,
    PLAN_MAX_ACTIONS,
    PLAN_VERSION,
    POLICY_VERSION,
    PlanAuthorizationError,
    build_plan_digest,
    build_plan_snapshot,
    canonical_plan_json,
    credential_ref_for,
    parse_plan_snapshot,
    validate_plan_action,
    verify_plan_authorization,
)

SECRET_KEY = "unit-test-plan-secret"

BUDGET = {
    "duration_seconds": 3600,
    "max_loops": 20,
    "max_actions": 30,
    "command_timeout_seconds": 60,
    "step_output_bytes": 65536,
    "run_artifact_bytes": 2097152,
}

BINDING = {
    "target_id": 7,
    "credential_ref": "system_user:19",
    "mode": "ask",
    "budget": dict(BUDGET),
    "graph_version": "v2",
    "environment": "lab",
}


def _canonical_action(kind="systemd", parameters=None, step_id="a1"):
    action = StructuredAction(
        kind=kind,
        target_id=BINDING["target_id"],
        system_user_id=19,
        parameters=parameters or {"operation": "restart", "unit": "nginx"},
        timeout_seconds=60,
        step_id=step_id,
    )
    return action.to_canonical_dict()


def make_snapshot(actions=None, **overrides):
    canonical = actions if actions is not None else [
        _canonical_action(step_id="a1"),
        _canonical_action(
            parameters={"operation": "start", "unit": "nginx"}, step_id="a2",
        ),
    ]
    digests = [
        build_action_digest(
            StructuredAction(**{
                key: value for key, value in item.items()
                if key != "action_version"
            }),
            SECRET_KEY,
        )
        for item in canonical
    ]
    snapshot = build_plan_snapshot(
        graph_version="v2",
        mode="ask",
        target_id=BINDING["target_id"],
        system_user_id=19,
        budget=dict(BUDGET),
        expires_at=int(time.time()) + 3600,
        summary="restart nginx",
        actions_canonical=canonical,
        ordered_action_digests=digests,
    )
    snapshot.update(overrides)
    return snapshot


def test_authorized_unchanged_plan_rebuilds_actions_in_order():
    snapshot = make_snapshot()

    actions = verify_plan_authorization(
        snapshot, build_plan_digest(snapshot, SECRET_KEY),
        dict(BINDING), SECRET_KEY,
    )

    assert [action.step_id for action in actions] == ["a1", "a2"]
    assert actions[0].parameters["operation"] == "restart"
    assert actions[1].parameters["operation"] == "start"


def test_snapshot_round_trip_through_canonical_json():
    snapshot = make_snapshot()
    digest = build_plan_digest(snapshot, SECRET_KEY)

    parsed = parse_plan_snapshot(canonical_plan_json(snapshot))

    assert verify_plan_authorization(
        parsed, digest, dict(BINDING), SECRET_KEY,
    ) is not None


SNAPSHOT_TAMPER_FIELDS = [
    # 动作内容/顺序变化
    lambda s: s["actions"][0]["parameters"].update({"unit": "httpd"}),
    lambda s: s["actions"].reverse(),
    # 绑定字段变化
    lambda s: s.update(target_id=8),
    lambda s: s.update(credential_ref="system_user:99"),
    lambda s: s.update(policy_version="999"),
    lambda s: s.update(mode="auto"),
    lambda s: s["budget"].update(max_actions=99),
    lambda s: s.update(graph_version="v9"),
    lambda s: s.update(expires_at=1),
]


@pytest.mark.parametrize("mutate", SNAPSHOT_TAMPER_FIELDS, ids=[
    "argument", "order", "target", "credential", "policy",
    "mode", "budget", "graph_version", "expiry",
])
def test_tamper_matrix_invalidates_the_plan(mutate):
    """快照任一字段被篡改：digest 不再匹配，授权必失效。

    HMAC 覆盖全部绑定字段，所以篡改的具体原因统一表现为
    digest_mismatch；绑定漂移（目标/凭据/策略/预算/图版本）与
    过期在 digest 仍有效的场景下另有专项用例。
    """
    snapshot = make_snapshot()
    digest = build_plan_digest(snapshot, SECRET_KEY)
    mutate(snapshot)

    with pytest.raises(PlanAuthorizationError) as excinfo:
        verify_plan_authorization(
            snapshot, digest, dict(BINDING), SECRET_KEY,
        )

    assert excinfo.value.reason == "digest_mismatch"


@pytest.mark.parametrize("mutate,reason", [
    (lambda s: s["actions"][0]["parameters"].update({"unit": "httpd"}),
     "action_digest_mismatch"),
    (lambda s: s["actions"].reverse(), "action_digest_mismatch"),
    (lambda s: s.update(policy_version="999"), "policy_changed"),
    (lambda s: s.update(expires_at=1), "expired"),
], ids=["argument", "order", "policy", "expiry"])
def test_inconsistent_snapshot_is_rejected_even_if_resigned(mutate, reason):
    """深度防御：即使快照被重新签名，内部一致性/策略/过期仍逐层复核。"""
    snapshot = make_snapshot()
    mutate(snapshot)
    resigned = build_plan_digest(snapshot, SECRET_KEY)

    with pytest.raises(PlanAuthorizationError) as excinfo:
        verify_plan_authorization(
            snapshot, resigned, dict(BINDING), SECRET_KEY,
        )

    assert excinfo.value.reason == reason


def test_stored_digest_mismatch_fails_closed():
    snapshot = make_snapshot()

    with pytest.raises(PlanAuthorizationError) as excinfo:
        verify_plan_authorization(
            snapshot, "0" * 64, dict(BINDING), SECRET_KEY,
        )

    assert excinfo.value.reason == "digest_mismatch"


def test_expired_authorization_fails_closed_even_with_valid_digest():
    snapshot = make_snapshot(expires_at=int(time.time()) - 1)
    digest = build_plan_digest(snapshot, SECRET_KEY)

    with pytest.raises(PlanAuthorizationError) as excinfo:
        verify_plan_authorization(
            snapshot, digest, dict(BINDING), SECRET_KEY,
        )

    assert excinfo.value.reason == "expired"


@pytest.mark.parametrize("binding,reason", [
    ({"target_id": 8}, "target_changed"),
    ({"credential_ref": "system_user:99"}, "credential_changed"),
    ({"mode": "auto"}, "mode_changed"),
    ({"budget": {**BUDGET, "max_loops": 1}}, "budget_changed"),
    ({"graph_version": "v1"}, "graph_version_changed"),
])
def test_binding_drift_invalidates_the_plan(binding, reason):
    """授权后运行边界漂移（目标/凭据/策略/预算/图版本）→ 回 ask。"""
    snapshot = make_snapshot()
    digest = build_plan_digest(snapshot, SECRET_KEY)
    current = dict(BINDING)
    current.update(binding)

    with pytest.raises(PlanAuthorizationError) as excinfo:
        verify_plan_authorization(
            snapshot, digest, current, SECRET_KEY,
        )

    assert excinfo.value.reason == reason


def test_environment_downgrade_denies_the_plan_at_execution():
    snapshot = make_snapshot()
    digest = build_plan_digest(snapshot, SECRET_KEY)
    current = dict(BINDING)
    current["environment"] = "production"
    current["mode"] = "read_only"
    # 快照里的 mode 仍是 ask，与当前不一致：先按模式漂移失效。
    with pytest.raises(PlanAuthorizationError) as excinfo:
        verify_plan_authorization(
            snapshot, digest, current, SECRET_KEY,
        )
    assert excinfo.value.reason == "mode_changed"

    # 模式一致但动作在新环境下被策略拒绝：按策略漂移失效。
    snapshot2 = make_snapshot(mode="read_only")
    digest2 = build_plan_digest(snapshot2, SECRET_KEY)
    current2 = dict(BINDING)
    current2["mode"] = "read_only"
    with pytest.raises(PlanAuthorizationError) as excinfo:
        verify_plan_authorization(
            snapshot2, digest2, current2, SECRET_KEY,
        )
    assert excinfo.value.reason == "policy_changed"


@pytest.mark.parametrize("raw", [
    "", "not-json", "[1, 2]", "{}",
    canonical_plan_json({"plan_version": 999}),
])
def test_malformed_snapshots_are_rejected(raw):
    with pytest.raises(PlanAuthorizationError) as excinfo:
        parse_plan_snapshot(raw)

    assert excinfo.value.reason == "malformed_plan"


def test_digest_length_mismatch_is_malformed():
    snapshot = make_snapshot()
    snapshot["ordered_action_digests"] = snapshot["ordered_action_digests"][:1]

    with pytest.raises(PlanAuthorizationError) as excinfo:
        parse_plan_snapshot(canonical_plan_json(snapshot))

    assert excinfo.value.reason == "malformed_plan"


def test_plan_version_is_bound_into_the_snapshot():
    assert make_snapshot()["plan_version"] == PLAN_VERSION
    assert make_snapshot()["policy_version"] == POLICY_VERSION


# ---------------------------------------------------------------------------
# 计划动作校验：kind 锁定 + 参数白名单 + 构造期复核
# ---------------------------------------------------------------------------

def test_validate_plan_action_supports_typed_families_only():
    assert "shell" not in PLAN_ACTION_KINDS
    normalized = validate_plan_action(
        "systemd", {"operation": "restart", "unit": "nginx"},
        run_id="run-1", step_id="s1", target_host="203.0.113.9",
    )
    assert normalized == {"operation": "restart", "unit": "nginx"}


def test_validate_plan_action_rejects_shell_and_unknown_kinds():
    for kind in ("shell", "file_read", "rm", ""):
        with pytest.raises(ActionValidationError):
            validate_plan_action(
                kind, {"command": "true"},
                run_id="run-1", step_id="s1", target_host="203.0.113.9",
            )


def test_validate_plan_probe_requires_probe_id_and_whitelist():
    normalized = validate_plan_action(
        "probe", {"probe_id": "system.load"},
        run_id="run-1", step_id="s1", target_host="203.0.113.9",
    )
    assert normalized["probe_id"] == "system.load"
    with pytest.raises(ActionValidationError):
        validate_plan_action(
            "probe", {"probe_id": "no.such.probe"},
            run_id="run-1", step_id="s1", target_host="203.0.113.9",
        )


def test_validate_plan_file_patch_checks_path_whitelist():
    with pytest.raises(ActionValidationError):
        validate_plan_action(
            "file_patch", {"path": "/etc/shadow", "content": "x"},
            run_id="run-1", step_id="s1", target_host="203.0.113.9",
        )


def test_plan_action_count_limit_is_bounded():
    assert PLAN_MAX_ACTIONS == 10


def test_credential_reference_only_carries_the_id():
    assert credential_ref_for(19) == "system_user:19"
    snapshot = make_snapshot()
    assert "system_user:19" == snapshot["credential_ref"]
