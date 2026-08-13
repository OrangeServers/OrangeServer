# -*- coding: utf-8 -*-
"""Static contract for the disposable, exact-checkout M1/S2 smoke entry."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'deploy' / 'docker-compose.s2-smoke.yml'
WRAPPER = ROOT / 'ops' / 'smoke-ai-autonomy-s2.ps1'
PROBE = ROOT / 'ops' / 'smoke-ai-autonomy-s2.py'


def test_s2_smoke_compose_is_isolated_and_builds_current_backend():
    compose = COMPOSE.read_text(encoding='utf-8')

    assert compose.count('context: ${OGS_S2_SMOKE_SOURCE_ROOT') == 2
    assert compose.count('source: ${OGS_S2_SMOKE_SOURCE_ROOT') == 4
    assert compose.count('pull_policy: never') == 2
    assert 'ports:' not in compose
    assert 'internal: true' in compose
    assert 'mysql:8.0.42' in compose
    assert 'redis:8.0-alpine' in compose
    assert 'redis:7.4-alpine' in compose
    assert 'target: /docker-entrypoint-initdb.d/00-orange.sql' in compose
    assert '/.s2-smoke/v1.0.4-orange.sql' in compose
    assert '--appendonly yes' in compose
    assert '--appendfsync everysec' in compose
    assert '--maxmemory-policy noeviction' in compose
    assert '--concurrency=1' in compose
    assert 'OGS_AI_AUTONOMY_LEASE_TTL_SECONDS: 30' in compose
    assert 'OGS_AI_AUTONOMY_REDIS_HOST: autonomy-redis' in compose
    assert 'OGS_S2_SMOKE_UPGRADE_MYSQL_HOST: mysql-upgrade' in compose
    assert '.env' not in compose


def test_s2_smoke_wrapper_pins_upgrade_source_and_cleans_by_default():
    wrapper = WRAPPER.read_text(encoding='utf-8')

    assert "'a4ef2c43efaea7b50cdc7f4fc6a7334a8966f0a8'" in wrapper
    assert "[Parameter(Mandatory)]" in wrapper
    assert "[string]$ExpectedHead" in wrapper
    assert "$Head -ne $ExpectedHead.ToLowerInvariant()" in wrapper
    assert "diff --quiet" in wrapper
    assert "diff --cached --quiet" in wrapper
    assert "status --porcelain=v1 --untracked-files=all" in wrapper
    assert "archive --format=tar --output=$ArchivePath $Head" in wrapper
    assert "tar -tf $ArchivePath" in wrapper
    assert "tar -xf $ArchivePath -C $SourceRoot" in wrapper
    assert "$ComposeFile = Join-Path $SourceRoot" in wrapper
    assert "$env:OGS_S2_SMOKE_SOURCE_ROOT = $SourceRoot" in wrapper
    assert "show 'v1.0.4:backend/mysqldir/orange.sql'" in wrapper
    assert 'New-UrlSafeSecret' in wrapper
    assert 'config --quiet' in wrapper
    assert 'build smoke-runner autonomy-worker' in wrapper
    assert 'restart autonomy-redis' in wrapper
    assert 'worker-and-duplicate' in wrapper
    assert 'lease-and-boundary' in wrapper
    assert 'checkpoint-and-cancel' in wrapper
    assert 'pause autonomy-worker' in wrapper
    assert 'hold-worker-lock' in wrapper
    assert "inspect --format '{{.State.Running}}' $BlockerName" in wrapper
    assert 'wait-worker-lock-ready' in wrapper
    assert 'unpause autonomy-worker' in wrapper
    assert 'wait-worker-lease' in wrapper
    assert 'kill --signal SIGKILL autonomy-worker' in wrapper
    assert 'docker rm -f $BlockerName' in wrapper
    assert 'verify-restart-before-expiry' in wrapper
    assert 'verify-worker-kill-recovery' in wrapper
    assert 'NOT_RUN_SSH_GATE' in wrapper
    assert 'INFRASTRUCTURE_SUBSET_PASS' in wrapper
    assert "docker image inspect --format '{{.Id}}'" in wrapper
    assert "docker inspect --format '{{.Image}}'" in wrapper
    assert "[S2 smoke] image {0}={1}" in wrapper
    assert 'down --volumes --remove-orphans' in wrapper
    assert '$ResolvedTemp.StartsWith($ExpectedPrefix' in wrapper
    assert 'Remove-Item -LiteralPath $ResolvedTemp -Recurse -Force' in wrapper
    assert '10.0.' not in wrapper

    killed = wrapper.index('kill --signal SIGKILL autonomy-worker')
    restarted = wrapper.index(
        'up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-worker',
        killed,
    )
    old_lease_checked = wrapper.index(
        'verify-restart-before-expiry', restarted,
    )
    recovered = wrapper.index(
        'verify-worker-kill-recovery', old_lease_checked,
    )
    assert killed < restarted < old_lease_checked < recovered
    assert 'wait-worker-lease-expiry' not in wrapper


def test_s2_smoke_probe_covers_migration_persistence_and_delivery():
    probe = PROBE.read_text(encoding='utf-8')

    assert "for _ in range(2):" in probe
    assert "rev53_ai_autonomy_baseline.sql" in probe
    assert "rev54_ai_autonomy_lease.sql" in probe
    assert 'schema_snapshot(fresh) == schema_snapshot(upgrade)' in probe
    assert "'uq_ai_auto_run_active_host'" in probe
    assert "int(exc.args[0]) == 1062" in probe
    assert "client.config_get('appendonly')" in probe
    assert "client.config_get('maxmemory-policy')" in probe
    assert "execute_command('WAITAOF', 1, 0, 10000)" in probe
    assert 'checkpoint_readiness(timeout=5)' in probe
    assert 'worker_readiness(timeout=8)' in probe
    assert "redis_client(0)" in probe
    assert "redis_client(1)" in probe
    assert "sum(result is not None for result in results) == 1" in probe
    assert probe.count('celery_app.send_task(DRIVE_RUN_TASK') == 3
    assert "event_type = 'planner_unavailable'" in probe
    assert "def lease_and_boundary():" in probe
    assert "expired lease takeover must enter recovering" in probe
    assert "outcome.mode == MODE_BOUNDARY" in probe
    assert "checkpoint_present=False" in probe
    assert "def checkpoint_and_cancel():" in probe
    assert "'recovery_write_outcome_unknown'" in probe
    assert "'outcome_unknown'" in probe
    assert "confirmed before side effects" in probe
    assert "def hold_worker_lock():" in probe
    assert 'FOR UPDATE' in probe
    assert "'smoke_lock_ready'" in probe
    assert (
        "for _ in range(2):\n"
        "            dispatch_run(WORKER_KILL_RUN_ID)"
    ) in probe
    assert "def wait_worker_lock_ready():" in probe
    assert "def wait_worker_lease():" in probe
    assert "'smoke_old_lease_observed'" in probe
    assert "def verify_restart_before_expiry():" in probe
    assert "replacement Worker was not ready before old lease expiry" in probe
    assert "def verify_worker_kill_recovery():" in probe
    assert "Worker crash recovery replayed an uncertain write" in probe
    assert "Worker crash safety event was not exactly-once" in probe
    assert 'CLIENT\', \'PAUSE' not in probe
    assert 'ssh_runner' not in probe
