# -*- coding: utf-8 -*-
"""Static contract for the disposable, exact-checkout M1/S2 smoke entry."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'deploy' / 'docker-compose.s2-smoke.yml'
WRAPPER = ROOT / 'ops' / 'smoke-ai-autonomy-s2.ps1'
PROBE = ROOT / 'ops' / 'smoke-ai-autonomy-s2.py'


def test_s2_smoke_compose_is_isolated_and_builds_current_backend():
    compose = COMPOSE.read_text(encoding='utf-8')

    assert compose.count('context: ${OGS_S2_SMOKE_SOURCE_ROOT') == 3
    assert compose.count('source: ${OGS_S2_SMOKE_SOURCE_ROOT') == 6
    assert compose.count('pull_policy: never') == 4
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
    assert 'ssh-target:' in compose
    assert 'dockerfile: Dockerfile.ssh-target' in compose
    assert 'OGS_SSH_HOST_KEY_POLICY: auto' in compose
    assert 'target: /run/secrets/s2-client-key' in compose
    assert 'target: /run/secrets/s2-client-key.pub' in compose
    assert compose.count('target: /tmp/orangeserver-s2-smoke/key') == 2
    assert 'install -m 0600 -o ogs -g ogs' in compose
    assert 'smoke-ssh-client-key:' in compose
    assert 'read_only: true' in compose
    assert 'uid=10001,gid=10001' in compose
    assert 'OGS_S2_SMOKE_SSH_PASSWORD' not in compose
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
    assert 'ssh-keygen' in wrapper
    assert "'-t', 'rsa'" in wrapper
    assert 'config --quiet' in wrapper
    assert 'build smoke-runner autonomy-worker ssh-target' in wrapper
    assert 'langgraph-pause-first' in wrapper
    assert 'langgraph-resume-to-second' in wrapper
    assert 'restart autonomy-redis' in wrapper
    assert 'langgraph-resume-after-restart' in wrapper
    assert 'worker-and-duplicate' in wrapper
    assert 'lease-and-boundary' in wrapper
    assert 'checkpoint-and-cancel' in wrapper
    assert 'pause autonomy-worker' in wrapper
    assert 'hold-worker-lock' in wrapper
    assert "inspect --format '{{.State.Running}}' $BlockerName" in wrapper
    assert 'wait-worker-lock-ready' in wrapper
    assert 'unpause autonomy-worker' in wrapper
    assert 'wait-worker-lease' in wrapper
    assert 'S2_WORKER_LEASE_EVIDENCE=' in wrapper
    assert 'ConvertFrom-Json' in wrapper
    assert 'OGS_S2_EXPECTED_LEASE_OWNER=' in wrapper
    assert 'OGS_S2_EXPECTED_LEASE_EXPIRES_AT=' in wrapper
    assert 'kill --signal SIGKILL autonomy-worker' in wrapper
    assert 'docker rm -f $BlockerName' in wrapper
    assert 'verify-restart-before-expiry' in wrapper
    assert 'verify-worker-kill-recovery' in wrapper
    assert 'hold-ssh-pre-intent-lock' in wrapper
    assert 'wait-ssh-pre-intent-lease' in wrapper
    assert 'verify-ssh-pre-intent-recovery' in wrapper
    assert 'worker-timer-recovery' in wrapper
    assert 'ssh-exit-and-streams' in wrapper
    assert 'ssh-cancel-process-group' in wrapper
    assert 'ssh-start-write' in wrapper
    assert 'wait-ssh-write-started' in wrapper
    assert 'verify-ssh-write-recovery' in wrapper
    assert 'ssh-symlink-boundary' in wrapper
    assert 'NOT_RUN_SSH_GATE' not in wrapper
    assert 'DISPOSABLE_S2_PASS' in wrapper
    assert "docker image inspect --format '{{.Id}}'" in wrapper
    assert "docker inspect --format '{{.Image}}'" in wrapper
    assert "[S2 smoke] image {0}={1}" in wrapper
    assert 'down --volumes --remove-orphans' in wrapper
    assert '$ResolvedTemp.StartsWith($ExpectedPrefix' in wrapper
    assert 'Remove-Item -LiteralPath $ResolvedTemp -Recurse -Force' in wrapper
    assert '10.0.' not in wrapper

    symlink_checked = wrapper.index('ssh-symlink-boundary')
    started_write = wrapper.index('ssh-start-write')
    unknown_verified = wrapper.index('verify-ssh-write-recovery')
    assert symlink_checked < started_write < unknown_verified

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
    assert 'ShallowRedisSaver' in probe
    assert "interrupt({'gate': 'first'})" in probe
    assert "interrupt({'gate': 'second'})" in probe
    assert "Command(resume='resume-first')" in probe
    assert "Command(resume='resume-second')" in probe
    assert "state.next == ('second_gate',)" in probe
    assert "['resume-first', 'resume-second']" in probe
    assert 'def langgraph_pause_first():' in probe
    assert 'def langgraph_resume_to_second():' in probe
    assert 'def langgraph_resume_after_restart():' in probe
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
    wait_lease = probe.split('def wait_worker_lease():', 1)[1].split(
        'def verify_restart_before_expiry():', 1,
    )[0]
    assert 'append_event(' not in wait_lease
    assert 'S2_WORKER_LEASE_EVIDENCE=' in wait_lease
    assert "'lease_token':" not in wait_lease
    assert "def verify_restart_before_expiry():" in probe
    assert "os.environ['OGS_S2_EXPECTED_LEASE_OWNER']" in probe
    assert "os.environ['OGS_S2_EXPECTED_LEASE_EXPIRES_AT']" in probe
    assert "replacement Worker was not ready before old lease expiry" in probe
    assert "def verify_worker_kill_recovery():" in probe
    assert "Worker crash recovery replayed an uncertain write" in probe
    assert "Worker crash safety event was not exactly-once" in probe
    assert "'Worker crash fixture lost its durable write intent'" in probe
    assert (
        "'Worker crash recovery claimed a remote execution outcome'"
        in probe
    )
    assert 'CLIENT\', \'PAUSE' not in probe
    assert 'from app.ai.autonomy.ssh_runner import run_ssh_command' in probe
    assert 'def ssh_exit_and_streams():' in probe
    assert "get('exit_code') == 23" in probe
    assert "'s2-exact-stdout'" in probe
    assert "'s2-exact-stderr'" in probe
    assert 'def ssh_cancel_process_group():' in probe
    assert "'probe_id': 'system.load'" in probe
    assert 'cancel.fifo' not in probe
    assert "row['status'] == 'cancelled'" in probe
    assert "'remote process group survived confirmed cancellation'" in probe
    assert 'def ssh_start_write():' in probe
    assert 'def wait_ssh_write_started():' in probe
    assert 'def verify_ssh_write_recovery():' in probe
    assert "'outcome_unknown'" in probe
    assert "'started write was replayed after Worker SIGKILL'" in probe
    assert 'def hold_ssh_pre_intent_lock():' in probe
    assert 'def wait_ssh_pre_intent_lease():' in probe
    assert 'def verify_ssh_pre_intent_recovery():' in probe
    assert (
        "'approved write did not execute exactly once after recovery'"
        in probe
    )
    assert 'def worker_timer_recovery():' in probe
    assert (
        '# Intentionally no dispatch_run/send_task call in this phase.'
        in probe
    )
    assert "not bool(current['lease_present'])" in probe
    assert 'def ssh_symlink_boundary():' in probe
    assert "'symlink target escaped the bounded root'" in probe
    assert "SSH_TARGET_KEY_PATH = 's2-client-key'" in probe


def test_wait_for_run_never_exposes_the_opaque_lease_token():
    probe = PROBE.read_text(encoding='utf-8')
    wait_for_run = probe.split('def wait_for_run(', 1)[1].split(
        '\ndef dispatch_run(', 1,
    )[0]

    assert 'lease_token IS NOT NULL AS lease_present' in wait_for_run
    assert 'lease_token,' not in wait_for_run
    assert "['lease_token']" not in probe


def test_s2_smoke_requires_real_readonly_sigkill_recovery():
    wrapper = WRAPPER.read_text(encoding='utf-8')
    probe = PROBE.read_text(encoding='utf-8')

    for phase in (
        'ssh-start-readonly',
        'wait-ssh-readonly-started',
        'release-ssh-readonly-first-attempt',
        'verify-ssh-readonly-recovery',
    ):
        assert phase in wrapper
    assert 'def ssh_start_readonly():' in probe
    assert 'def wait_ssh_readonly_started():' in probe
    assert 'def release_ssh_readonly_first_attempt():' in probe
    assert 'def verify_ssh_readonly_recovery():' in probe
    assert "event_type = 'recovery_readonly_retry'" in probe
    assert "int(events['started_count'] or 0) == 2" in probe
    assert "int(events['executed_count'] or 0) == 1" in probe
    assert "int(events['intent_count'] or 0) == 0" in probe

    started = wrapper.index('wait-ssh-readonly-started')
    killed = wrapper.index(
        'kill --signal SIGKILL autonomy-worker', started,
    )
    released = wrapper.index('release-ssh-readonly-first-attempt', killed)
    restarted = wrapper.index(
        'up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-worker',
        released,
    )
    verified = wrapper.index('verify-ssh-readonly-recovery', restarted)
    assert started < killed < released < restarted < verified


def test_s2_smoke_proves_feature_off_without_autonomy_dependencies():
    wrapper = WRAPPER.read_text(encoding='utf-8')
    probe = PROBE.read_text(encoding='utf-8')

    stopped = wrapper.index('stop autonomy-worker autonomy-redis')
    isolated = wrapper.index('smoke-runner feature-off-isolation', stopped)
    restored = wrapper.index(
        'up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-redis',
        isolated,
    )
    assert stopped < isolated < restored
    assert '--no-deps' in wrapper[stopped:isolated]
    assert 'OGS_AI_AUTONOMY_ENABLED=false' in wrapper[stopped:isolated]
    assert 'OGS_AI_AUTONOMY_REDIS_HOST=autonomy-redis-unavailable' in (
        wrapper[stopped:isolated]
    )

    assert 'def feature_off_isolation():' in probe
    assert "client.get('/local/health')" in probe
    assert "client.get('/local/captcha/get')" in probe
    assert "client.post('/account/login_dl2'" in probe
    assert "client.get('/ai/diagnostic-profiles')" in probe
    assert "client.get('/server/host/list_all')" in probe
    assert "client.post('/ai/chat'" in probe
    assert "client.post('/server/group/cmd'" in probe
    assert "login.get_json()['code'] == 0" in probe
    assert "diagnostics.status_code == 200" in probe
    assert "inventory.status_code == 200" in probe
    assert "chat.status_code == 200" in probe
    assert "'event: run.failed' in chat.get_data(as_text=True)" in probe
    assert "batch.status_code == 200" in probe
    assert "batch.get_json()['code'] == 100" in probe
    assert "'dangerous command blocked:'" in probe
    assert "response.status_code == 401" not in probe
    assert 'serves legacy HTTP' not in wrapper[stopped:isolated]
    assert 'exercises legacy Flask route/application' in wrapper[stopped:isolated]
    assert 'Gunicorn/listener/network-path smoke' in wrapper[stopped:isolated]
    # Compare decoded JSON, not source text.
    assert "'status' == 'ok'" not in probe
    assert "health.get_json()['status'] == 'ok'" in probe
    assert "SELECT COUNT(*) AS count FROM t_ai_autonomous_run" in probe
    assert "SELECT COUNT(*) AS count FROM t_command_log" in probe


def test_s2_wrapper_fail_closes_archive_and_cleanup_lifecycle():
    wrapper = WRAPPER.read_text(encoding='utf-8')

    guarded = wrapper.index('try {')
    archived = wrapper.index(
        'archive --format=tar --output=$ArchivePath $Head'
    )
    extracted = wrapper.index('tar -xf $ArchivePath -C $SourceRoot')
    keygen = wrapper.index('ssh-keygen @SshKeyArguments')
    finalizer = wrapper.rindex('finally {')
    passed = wrapper.index('DISPOSABLE_S2_PASS')
    assert guarded < archived < extracted < keygen < finalizer < passed

    assert 'ls-tree -r $Head' in wrapper
    assert "StartsWith('120000 ')" in wrapper
    assert 'tar -tvf $ArchivePath' in wrapper
    assert "StartsWith('l') -or $Entry.StartsWith('h')" in wrapper
    assert '$CleanupFailed = $true' in wrapper
    assert '$ComposeReady' in wrapper
    assert 'retry source retained' in wrapper

    cleanup = wrapper[finalizer:passed]
    assert 'if (-not $CleanupFailed)' in cleanup
    assert "'orangeserver-s2-smoke:' + $Suffix" in wrapper
    assert "'orangeserver-s2-ssh:' + $Suffix" in wrapper
    assert '& docker image rm $ImageName' in cleanup
    assert 'Remove-Item -LiteralPath $ResolvedTemp -Recurse -Force' in cleanup


def test_s2_smoke_deletes_a_real_production_checkpoint_before_rebuild():
    wrapper = WRAPPER.read_text(encoding='utf-8')
    probe = PROBE.read_text(encoding='utf-8')

    assert 'production-checkpoint-loss-boundary' in wrapper
    assert 'def production_checkpoint_loss_boundary():' in probe
    assert 'AutonomyDriver(' in probe
    assert 'make_autonomy_saver_factory()' in probe
    assert "context['repo'].propose_probe(" in probe
    assert "THREAD_ID_PREFIX + PRODUCTION_CHECKPOINT_RUN_ID" in probe
    assert 'created_checkpoint_keys = after_keys - before_keys' in probe
    assert 'db0.delete(*created_checkpoint_keys)' in probe
    assert 'remaining_checkpoint_keys == set()' in probe
    assert "event_type = 'recovery_boundary_rebuild'" in probe
    assert "int(events['boundary_count'] or 0) == 1" in probe
    assert "int(events['executed_count'] or 0) == 1" in probe
    assert "int(events['started_count'] or 0) == 1" in probe

    primed = wrapper.index('ssh-prime-target')
    real_loss = wrapper.index('production-checkpoint-loss-boundary', primed)
    fake_loss = wrapper.index('checkpoint-and-cancel', real_loss)
    assert primed < real_loss < fake_loss


def test_s2_smoke_revokes_lab_authority_during_real_ssh():
    wrapper = WRAPPER.read_text(encoding='utf-8')
    probe = PROBE.read_text(encoding='utf-8')

    assert 'ssh-runtime-environment-revocation' in wrapper
    assert 'def ssh_runtime_environment_revocation():' in probe
    assert "SET ai_environment = 'production'" in probe
    assert "SET ai_environment = 'lab'" in probe
    assert "event_type = 'execution_authorization_revoked'" in probe
    assert "payload.get('stop_confirmed') is True" in probe
    assert "int(events['revoked_count'] or 0) == 1" in probe
    assert "int(events['intent_count'] or 0) == 0" in probe
    assert "_remote_process_groups('sleep') == set()" in probe

    revocation = wrapper.index('ssh-runtime-environment-revocation')
    attention = wrapper.index('ssh-start-write', revocation)
    assert revocation < attention

    focused = (
        ROOT / 'backend' / 'tests' / 'test_ai_autonomy_executor.py'
    ).read_text(encoding='utf-8')
    assert 'def test_permission_revoked_blocks_execution(' in focused
    assert (
        'def test_authorization_revoked_during_execution_fails_run('
        in focused
    )


def test_s2_smoke_patches_and_restores_a_real_remote_file():
    wrapper = WRAPPER.read_text(encoding='utf-8')
    probe = PROBE.read_text(encoding='utf-8')

    assert 'ssh-file-patch-restore' in wrapper
    assert 'def ssh_file_patch_restore():' in probe
    assert "'file_patch'" in probe
    assert "'file_restore'" in probe
    assert "artifacts['patch_diff']" in probe
    assert "artifacts['backup_ref'] == backup_path" in probe
    assert "artifact_rows['patch_diff']['truncated']" in probe
    assert "artifact_rows['backup_ref']['truncated']" in probe
    assert "'mode=before'" in probe
    assert "'mode=after'" in probe
    assert probe.count(
        "not in artifact_rows['patch_diff']['content_ciphertext']"
    ) == 2
    assert "not in artifact_rows['backup_ref']['content_ciphertext']" in probe
    assert "restored.stdout == 'mode=before\\n'" in probe

    patch_gate = wrapper.index('ssh-file-patch-restore')
    attention = wrapper.index('ssh-start-write', patch_gate)
    assert patch_gate < attention


def test_s2_ssh_fixture_helpers_insert_each_action_once():
    probe = PROBE.read_text(encoding='utf-8')
    run_helper = probe.split('def insert_ssh_action_run(', 1)[1].split(
        '\ndef insert_ssh_action_step(', 1,
    )[0]
    step_helper = probe.split('def insert_ssh_action_step(', 1)[1].split(
        '\ndef wait_for_step(', 1,
    )[0]

    assert run_helper.count('INSERT INTO t_ai_autonomous_run') == 1
    assert run_helper.count('insert_ssh_action_step(') == 1
    assert step_helper.count('INSERT INTO t_ai_autonomous_step') == 1
    assert step_helper.count('action = StructuredAction(') == 1
