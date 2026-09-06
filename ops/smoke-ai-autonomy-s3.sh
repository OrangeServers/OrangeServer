#!/usr/bin/env bash
# Linux entry point for the S3 autonomy smoke (same scenarios and guards as
# ops/smoke-ai-autonomy-s3.ps1, which remains the Windows/WSL entry point).
# Reuses the S2 exact-head archive/Compose facilities and only runs the S3
# single-stage chat-draft-only probe.
#
# Usage: bash ops/smoke-ai-autonomy-s3.sh --expected-head <40-hex>
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

smoke_require_commands git docker tar openssl sha256sum

HEAD="$(smoke_validate_head "$REPO_ROOT" "$EXPECTED_HEAD")"
SUFFIX="$(smoke_new_suffix "$HEAD")"
PROJECT_NAME="ogs-s3-smoke-${SUFFIX}"
BACKEND_IMAGE="orangeserver-s3-smoke:${SUFFIX}"
SSH_IMAGE="orangeserver-s3-ssh:${SUFFIX}"
SMOKE_PARENT="${REPO_ROOT}/.tmp/s3-smoke"
TEMP_ROOT="${SMOKE_PARENT}/${SUFFIX}"
SOURCE_ROOT="${TEMP_ROOT}/source"
ARCHIVE_PATH="${TEMP_ROOT}/source.tar"
COMPOSE_READY=0
CLEANUP_FAILED=0
SUCCEEDED=0

cleanup() {
    local status=$?
    if [ "$KEEP" = "1" ]; then
        echo "[S3 smoke] WARNING: resources kept for inspection: project=$PROJECT_NAME" >&2
        echo "[S3 smoke] WARNING: clean up with the same COMPOSE_PROJECT_NAME and the Compose files under $SOURCE_ROOT" >&2
    else
        if [ "$COMPOSE_READY" = "1" ]; then
            docker compose -f "$S2_COMPOSE" -f "$S3_COMPOSE" down --volumes --remove-orphans --timeout 10 \
                || { CLEANUP_FAILED=1; echo "[S3 smoke] WARNING: Compose cleanup failed for $PROJECT_NAME" >&2; }
        fi
        local image existing
        for image in "$BACKEND_IMAGE" "$SSH_IMAGE"; do
            existing="$(docker image ls --quiet --no-trunc "$image" 2>/dev/null || true)"
            if [ -n "$existing" ]; then
                docker image rm "$image" >/dev/null 2>&1 \
                    || { CLEANUP_FAILED=1; echo "[S3 smoke] WARNING: image cleanup failed: $image" >&2; }
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
        echo "[smoke] FAIL: S3 smoke did not reach its acceptance gate" >&2
        exit 1
    fi
    if [ "$CLEANUP_FAILED" != "0" ]; then
        echo "[smoke] FAIL: S3 smoke reached acceptance but cleanup failed; retry source retained at $TEMP_ROOT" >&2
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
    deploy/docker-compose.s3-smoke.yml \
    ops/smoke-ai-autonomy-s3.py)"
S2_COMPOSE="${SOURCE_ROOT}/deploy/docker-compose.s2-smoke.yml"
S3_COMPOSE="${SOURCE_ROOT}/deploy/docker-compose.s3-smoke.yml"

export COMPOSE_PROJECT_NAME="$PROJECT_NAME"
export OGS_S2_SMOKE_GIT_HEAD="$HEAD"
export OGS_S2_SMOKE_SOURCE_ROOT="$SOURCE_ROOT"
export OGS_S2_SMOKE_BACKEND_IMAGE="$BACKEND_IMAGE"
export OGS_S2_SMOKE_SSH_IMAGE="$SSH_IMAGE"
smoke_export_secrets

COMPOSE=(compose -f "$S2_COMPOSE" -f "$S3_COMPOSE")
COMPOSE_READY=1
echo "[S3 smoke] HEAD=$HEAD archive_sha256=$ARCHIVE_SHA256 project=$PROJECT_NAME"
docker "${COMPOSE[@]}" config --quiet
docker "${COMPOSE[@]}" build smoke-runner

# The chat-draft probe only needs fresh MySQL and business Redis.
docker "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT" mysql-fresh business-redis
docker "${COMPOSE[@]}" run --rm --no-deps smoke-runner chat-draft-only

SUCCEEDED=1
echo '[S3 smoke] DISPOSABLE_S3_PASS: chat created only an autonomy draft/reference card; no Run was started, approved, or mutated, non-admin chat was refused, and the detail projection restores the card'
