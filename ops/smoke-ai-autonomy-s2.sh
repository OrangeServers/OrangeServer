#!/usr/bin/env bash
# Linux entry point for the S2 autonomy smoke (same scenarios and guards as
# ops/smoke-ai-autonomy-s2.ps1, which remains the Windows/WSL entry point).
#
# Usage: bash ops/smoke-ai-autonomy-s2.sh --expected-head <40-hex>
#        [--keep] [--wait-timeout <60-1800>]
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ops/smoke-ai-autonomy-lib.sh
source "${SCRIPT_DIR}/smoke-ai-autonomy-lib.sh"

REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
EXPECTED_HEAD=''
KEEP=0
WAIT_TIMEOUT=420

while [ "$#" -gt 0 ]; do
    case "$1" in
        --expected-head) EXPECTED_HEAD="${2:?}"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        --wait-timeout) WAIT_TIMEOUT="${2:?}"; shift 2 ;;
        *) smoke_fail "unknown argument: $1" ;;
    esac
done
[[ "$EXPECTED_HEAD" =~ ^[0-9a-fA-F]{40}$ ]] || smoke_fail '--expected-head must be 40 hex chars'
[ "$WAIT_TIMEOUT" -ge 60 ] && [ "$WAIT_TIMEOUT" -le 1800 ] \
    || smoke_fail '--wait-timeout must be within 60-1800'
EXPECTED_HEAD="$(printf '%s' "$EXPECTED_HEAD" | tr 'A-F' 'a-f')"

smoke_require_commands git docker tar ssh-keygen openssl sha256sum python3

HEAD="$(smoke_validate_head "$REPO_ROOT" "$EXPECTED_HEAD")"
SUFFIX="$(smoke_new_suffix "$HEAD")"
PROJECT_NAME="ogs-s2-smoke-${SUFFIX}"
BACKEND_IMAGE="orangeserver-s2-smoke:${SUFFIX}"
SSH_IMAGE="orangeserver-s2-ssh:${SUFFIX}"
SMOKE_PARENT="${REPO_ROOT}/.tmp/s2-smoke"
TEMP_ROOT="${SMOKE_PARENT}/${SUFFIX}"
SOURCE_ROOT="${TEMP_ROOT}/source"
ARCHIVE_PATH="${TEMP_ROOT}/source.tar"
FIXTURE_ROOT="${SOURCE_ROOT}/.s2-smoke"
COMPOSE_READY=0
BLOCKER_NAME=''
CLEANUP_FAILED=0
SUCCEEDED=0

cleanup() {
    local status=$?
    if [ -n "$BLOCKER_NAME" ]; then
        docker rm -f "$BLOCKER_NAME" >/dev/null 2>&1 || CLEANUP_FAILED=1
        BLOCKER_NAME=''
    fi
    if [ "$KEEP" = "1" ]; then
        echo "[S2 smoke] WARNING: resources kept for inspection: project=$PROJECT_NAME" >&2
        echo "[S2 smoke] WARNING: clean up with the same COMPOSE_PROJECT_NAME and the Compose file under $SOURCE_ROOT" >&2
    else
        if [ "$COMPOSE_READY" = "1" ]; then
            docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans --timeout 10 \
                || { CLEANUP_FAILED=1; echo "[S2 smoke] WARNING: Compose cleanup failed for $PROJECT_NAME" >&2; }
        fi
        local image existing
        for image in "$BACKEND_IMAGE" "$SSH_IMAGE"; do
            existing="$(docker image ls --quiet --no-trunc "$image" 2>/dev/null || true)"
            if [ -n "$existing" ]; then
                docker image rm "$image" >/dev/null 2>&1 \
                    || { CLEANUP_FAILED=1; echo "[S2 smoke] WARNING: image cleanup failed: $image" >&2; }
            fi
        done
        if [ "$CLEANUP_FAILED" = "0" ]; then
            smoke_remove_temp_root "$TEMP_ROOT" "$SMOKE_PARENT" || CLEANUP_FAILED=1
        fi
    fi
    if [ "$status" != "0" ]; then
        exit "$status"
    fi
    if [ "$SUCCEEDED" != "1" ]; then
        echo "[smoke] FAIL: S2 smoke did not reach its acceptance gate" >&2
        exit 1
    fi
    if [ "$CLEANUP_FAILED" != "0" ]; then
        echo "[smoke] FAIL: S2 smoke reached acceptance but cleanup failed; retry source retained at $TEMP_ROOT" >&2
        exit 1
    fi
}
trap cleanup EXIT

mkdir -p "$TEMP_ROOT" "$SOURCE_ROOT"
ARCHIVE_SHA256="$(smoke_extract_exact_head "$REPO_ROOT" "$HEAD" "$SOURCE_ROOT" "$ARCHIVE_PATH" \
    AGENTS.md \
    backend/Dockerfile \
    backend/mysqldir/orange.sql \
    deploy/docker-compose.s2-smoke.yml \
    deploy/s2-smoke/Dockerfile.ssh-target \
    deploy/s2-smoke/ssh-target-entrypoint.sh \
    deploy/s2-smoke/sshd_config \
    deploy/s2-smoke/uptime-wrapper.sh \
    ops/smoke-ai-autonomy-s2.py)"
COMPOSE_FILE="${SOURCE_ROOT}/deploy/docker-compose.s2-smoke.yml"
mkdir -p "$FIXTURE_ROOT"

# `git show` is the only upgrade fixture source so the SQL cannot drift from
# the immutable v1.0.4 release tag.
git -C "$REPO_ROOT" show 'v1.0.4:backend/mysqldir/orange.sql' \
    > "${FIXTURE_ROOT}/v1.0.4-orange.sql" \
    || smoke_fail 'cannot extract backend/mysqldir/orange.sql from v1.0.4'

SSH_KEY="${FIXTURE_ROOT}/s2-client-key"
ssh-keygen -q -t rsa -b 3072 -N '' -C orangeserver-s2-disposable -f "$SSH_KEY" \
    || smoke_fail 'ssh-keygen failed'
[ -f "$SSH_KEY" ] && [ -f "${SSH_KEY}.pub" ] \
    || smoke_fail 'ssh-keygen did not produce the disposable client key pair'

export COMPOSE_PROJECT_NAME="$PROJECT_NAME"
export OGS_S2_SMOKE_GIT_HEAD="$HEAD"
export OGS_S2_SMOKE_SOURCE_ROOT="$SOURCE_ROOT"
export OGS_S2_SMOKE_BACKEND_IMAGE="$BACKEND_IMAGE"
export OGS_S2_SMOKE_SSH_IMAGE="$SSH_IMAGE"
smoke_export_secrets

COMPOSE=(compose -f "$COMPOSE_FILE")
COMPOSE_READY=1
echo "[S2 smoke] HEAD=$HEAD archive_sha256=$ARCHIVE_SHA256 project=$PROJECT_NAME"
docker "${COMPOSE[@]}" config --quiet
docker "${COMPOSE[@]}" build smoke-runner autonomy-worker ssh-target
docker "${COMPOSE[@]}" run --rm ssh-key-init
docker "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT" \
    mysql-fresh mysql-upgrade business-redis autonomy-redis ssh-target

docker "${COMPOSE[@]}" run --rm smoke-runner migrate-and-prime
docker "${COMPOSE[@]}" run --rm smoke-runner langgraph-pause-first
docker "${COMPOSE[@]}" run --rm smoke-runner langgraph-resume-to-second

# Persistence is only evidence after a real process restart.
docker "${COMPOSE[@]}" restart autonomy-redis
docker "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT" autonomy-redis
docker "${COMPOSE[@]}" run --rm smoke-runner verify-persistence
docker "${COMPOSE[@]}" run --rm smoke-runner langgraph-resume-after-restart
docker "${COMPOSE[@]}" run --rm smoke-runner ssh-prime-target

docker "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT" autonomy-worker
# Feature-off is an application-isolation contract.
docker "${COMPOSE[@]}" stop autonomy-worker autonomy-redis
docker "${COMPOSE[@]}" run --rm --no-deps \
    --env 'OGS_AI_AUTONOMY_ENABLED=false' \
    --env 'OGS_AI_AUTONOMY_REDIS_HOST=autonomy-redis-unavailable' \
    smoke-runner feature-off-isolation
docker "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT" autonomy-redis
docker "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT" autonomy-worker
docker "${COMPOSE[@]}" run --rm smoke-runner production-checkpoint-loss-boundary
docker "${COMPOSE[@]}" run --rm smoke-runner worker-and-duplicate
docker "${COMPOSE[@]}" run --rm smoke-runner lease-and-boundary
docker "${COMPOSE[@]}" run --rm smoke-runner checkpoint-and-cancel

# Deterministic real Worker crash: freeze the ready Worker, hold the interrupted
# Step row lock, observe the committed lease, then send real SIGKILL.
docker "${COMPOSE[@]}" pause autonomy-worker
BLOCKER_NAME="${PROJECT_NAME}-worker-kill-lock"
docker "${COMPOSE[@]}" run -d --name "$BLOCKER_NAME" smoke-runner hold-worker-lock >/dev/null \
    || smoke_fail 'cannot start the worker-kill row-lock fixture'
[ "$(docker inspect --format '{{.State.Running}}' "$BLOCKER_NAME")" = 'true' ] \
    || smoke_fail 'worker-kill row-lock fixture exited before readiness'
docker "${COMPOSE[@]}" run --rm smoke-runner wait-worker-lock-ready
docker "${COMPOSE[@]}" unpause autonomy-worker
LEASE_OUTPUT="$(docker "${COMPOSE[@]}" run --rm smoke-runner wait-worker-lease)" \
    || smoke_fail 'cannot observe the real Worker lease before SIGKILL'
LEASE_EVIDENCE="$(printf '%s\n' "$LEASE_OUTPUT" | grep -c '^S2_WORKER_LEASE_EVIDENCE=')"
[ "$LEASE_EVIDENCE" = '1' ] \
    || smoke_fail 'Worker lease probe did not return exactly one evidence record'
LEASE_JSON="$(printf '%s\n' "$LEASE_OUTPUT" | grep '^S2_WORKER_LEASE_EVIDENCE=' | cut -d= -f2-)"
LEASE_OWNER="$(printf '%s' "$LEASE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["lease_owner"])')"
LEASE_REVISION="$(printf '%s' "$LEASE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')"
LEASE_EXPIRES="$(printf '%s' "$LEASE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["lease_expires_at"])')"
[ -n "$LEASE_OWNER" ] && [ "$LEASE_REVISION" -ge 1 ] && [ -n "$LEASE_EXPIRES" ] \
    || smoke_fail 'Worker lease evidence omitted its owner or expiry'
docker "${COMPOSE[@]}" kill --signal SIGKILL autonomy-worker
docker rm -f "$BLOCKER_NAME" >/dev/null
BLOCKER_NAME=''
# Restart while the persisted old lease is still live.
docker "${COMPOSE[@]}" up -d autonomy-worker
docker "${COMPOSE[@]}" run --rm \
    --env "OGS_S2_EXPECTED_LEASE_OWNER=${LEASE_OWNER}" \
    --env "OGS_S2_EXPECTED_LEASE_REVISION=${LEASE_REVISION}" \
    --env "OGS_S2_EXPECTED_LEASE_EXPIRES_AT=${LEASE_EXPIRES}" \
    smoke-runner verify-restart-before-expiry
docker "${COMPOSE[@]}" run --rm \
    --env "OGS_S2_EXPECTED_LEASE_EXPIRES_AT=${LEASE_EXPIRES}" \
    smoke-runner verify-worker-kill-recovery

# Crash before the Executor can commit execution_started/write_intent.
docker "${COMPOSE[@]}" pause autonomy-worker
BLOCKER_NAME="${PROJECT_NAME}-ssh-pre-intent-lock"
docker "${COMPOSE[@]}" run -d --name "$BLOCKER_NAME" smoke-runner hold-ssh-pre-intent-lock >/dev/null \
    || smoke_fail 'cannot start the pre-intent SSH row-lock fixture'
[ "$(docker inspect --format '{{.State.Running}}' "$BLOCKER_NAME")" = 'true' ] \
    || smoke_fail 'pre-intent SSH row-lock fixture exited before readiness'
docker "${COMPOSE[@]}" run --rm smoke-runner wait-ssh-pre-intent-lock
docker "${COMPOSE[@]}" unpause autonomy-worker
docker "${COMPOSE[@]}" run --rm smoke-runner wait-ssh-pre-intent-lease
docker "${COMPOSE[@]}" kill --signal SIGKILL autonomy-worker
docker rm -f "$BLOCKER_NAME" >/dev/null
BLOCKER_NAME=''
docker "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT" autonomy-worker
docker "${COMPOSE[@]}" run --rm smoke-runner verify-ssh-pre-intent-recovery

# No broker publish here: the running Worker's real timer must dispatch it.
docker "${COMPOSE[@]}" run --rm smoke-runner worker-timer-recovery

# End-to-end production side effect path against the disposable SSH target.
docker "${COMPOSE[@]}" run --rm smoke-runner ssh-exit-and-streams
docker "${COMPOSE[@]}" run --rm smoke-runner ssh-cancel-process-group
docker "${COMPOSE[@]}" run --rm smoke-runner ssh-runtime-environment-revocation
docker "${COMPOSE[@]}" run --rm smoke-runner ssh-file-patch-restore

docker "${COMPOSE[@]}" run --rm smoke-runner ssh-start-readonly
docker "${COMPOSE[@]}" run --rm smoke-runner wait-ssh-readonly-started
docker "${COMPOSE[@]}" kill --signal SIGKILL autonomy-worker
docker "${COMPOSE[@]}" run --rm smoke-runner release-ssh-readonly-first-attempt
docker "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT" autonomy-worker
docker "${COMPOSE[@]}" run --rm smoke-runner verify-ssh-readonly-recovery

docker "${COMPOSE[@]}" run --rm smoke-runner ssh-symlink-boundary

docker "${COMPOSE[@]}" run --rm smoke-runner ssh-start-write
docker "${COMPOSE[@]}" run --rm smoke-runner wait-ssh-write-started
docker "${COMPOSE[@]}" kill --signal SIGKILL autonomy-worker
docker "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT" autonomy-worker
docker "${COMPOSE[@]}" run --rm smoke-runner verify-ssh-write-recovery

BACKEND_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$BACKEND_IMAGE")" \
    || smoke_fail 'cannot resolve the exact-head backend image ID'
[[ "$BACKEND_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || smoke_fail 'cannot resolve the exact-head backend image ID'
echo "[S2 smoke] image backend=$BACKEND_IMAGE_ID"
for SERVICE in mysql-fresh mysql-upgrade business-redis autonomy-redis ssh-target autonomy-worker; do
    CONTAINER_ID="$(docker "${COMPOSE[@]}" ps -q "$SERVICE")" \
        || smoke_fail "cannot resolve container for $SERVICE"
    [ -n "$CONTAINER_ID" ] || smoke_fail "cannot resolve container for $SERVICE"
    IMAGE_ID="$(docker inspect --format '{{.Image}}' "$CONTAINER_ID")" \
        || smoke_fail "cannot resolve image ID for $SERVICE"
    [[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || smoke_fail "cannot resolve image ID for $SERVICE"
    echo "[S2 smoke] image $SERVICE=$IMAGE_ID"
done

SUCCEEDED=1
echo '[S2 smoke] DISPOSABLE_S2_PASS: fresh/upgrade MySQL, real ShallowRedisSaver double-interrupt AOF recovery, duplicate delivery, lease expiry, Worker SIGKILL recovery, exact SSH exit/dual streams, confirmed process-group cancellation, uncertain-write non-replay, and bounded-root symlink refusal'
