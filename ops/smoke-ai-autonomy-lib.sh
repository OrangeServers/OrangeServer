#!/usr/bin/env bash
# Shared exact-head facilities for the autonomy smoke drivers.
#
# The PowerShell drivers (ops/smoke-ai-autonomy-s2.ps1 / -s3.ps1) remain the
# Windows/WSL entry points; the bash drivers (ops/smoke-ai-autonomy-s2.sh /
# -s3.sh) source this file so plain Linux hosts can run the same gate without
# installing a PowerShell runtime. Both drivers must keep the same scenario
# step names; tests/test_clean_deploy_contract.py compares them.
set -Eeuo pipefail

SMOKE_PINNED_V104_COMMIT='a4ef2c43efaea7b50cdc7f4fc6a7334a8966f0a8'

smoke_fail() {
    echo "[smoke] FAIL: $*" >&2
    exit 1
}

# git -C needs git >= 1.8.5; release test machines may ship older git, so run
# git inside a subshell of the target directory instead.
smoke_git() {
    local dir="$1"
    shift
    (cd "$dir" && exec git "$@")
}

smoke_require_commands() {
    local cmd
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 \
            || smoke_fail "$cmd is required for the exact-head smoke"
    done
}

# Refuse anything but the reviewed, pristine HEAD: the smoke must never prove
# a dirty developer checkout or consume staged/untracked input.
smoke_validate_head() {
    local repo_root="$1" expected="$2" head porcelain
    head="$(smoke_git "$repo_root" rev-parse HEAD)" \
        || smoke_fail 'cannot resolve the reviewed Git HEAD'
    [[ "$head" =~ ^[0-9a-f]{40}$ ]] \
        || smoke_fail "cannot resolve the reviewed Git HEAD: $head"
    [ "$head" = "$expected" ] \
        || smoke_fail "HEAD $head does not match expected head $expected"
    smoke_git "$repo_root" diff --quiet \
        || smoke_fail 'working tree has unstaged changes; exact-head smoke refused'
    smoke_git "$repo_root" diff --cached --quiet \
        || smoke_fail 'index has staged changes; exact-head smoke refused'
    porcelain="$(smoke_git "$repo_root" status --porcelain=v1 --untracked-files=all)" \
        || smoke_fail 'cannot inspect repository cleanliness'
    [ -z "$porcelain" ] \
        || smoke_fail 'working tree contains untracked or modified files; exact-head smoke refused'
    local v104
    v104="$(smoke_git "$repo_root" rev-list -n 1 v1.0.4)" \
        || smoke_fail 'cannot resolve the pinned v1.0.4 commit'
    [ "$v104" = "$SMOKE_PINNED_V104_COMMIT" ] \
        || smoke_fail "v1.0.4 must resolve to the pinned commit $SMOKE_PINNED_V104_COMMIT"
    printf '%s\n' "$head"
}

smoke_new_suffix() {
    local head="$1"
    printf '%s-%s\n' "${head:0:10}" "$(openssl rand -hex 4)"
}

smoke_urlsafe_secret() {
    local bytes="$1" keep_padding="${2:-}" encoded
    encoded="$(openssl rand -base64 "$bytes" | tr '+/' '-_')"
    if [ "$keep_padding" != "keep" ]; then
        encoded="${encoded%%=*}"
        encoded="$(printf '%s' "$encoded" | tr -d '=')"
    fi
    printf '%s\n' "$encoded"
}

# Reject Git links and unsafe archive paths before extracting the reviewed
# HEAD into the disposable source root.
smoke_extract_exact_head() {
    local repo_root="$1" head="$2" source_root="$3" archive="$4"
    shift 4
    local entry
    while IFS= read -r entry; do
        case "$entry" in
            120000\ *) smoke_fail "exact-head Git tree contains a forbidden symlink: $entry" ;;
        esac
    done < <(smoke_git "$repo_root" ls-tree -r "$head")
    smoke_git "$repo_root" archive --format=tar --output="$archive" "$head"
    [ -s "$archive" ] || smoke_fail 'cannot inspect the exact-head source archive'
    while IFS= read -r entry; do
        [ -n "$entry" ] || smoke_fail 'unsafe path in Git archive: (empty)'
        case "$entry" in
            /*) smoke_fail "unsafe path in Git archive: $entry" ;;
            *[\\]*) smoke_fail "unsafe path in Git archive: $entry" ;;
        esac
        [[ "$entry" =~ ^[A-Za-z]: ]] && smoke_fail "unsafe path in Git archive: $entry"
        [[ "$entry" =~ (^|/)\.\.(/|$) ]] && smoke_fail "unsafe path in Git archive: $entry"
    done < <(tar -tf "$archive")
    while IFS= read -r entry; do
        entry="${entry#"${entry%%[![:space:]]*}"}"
        case "$entry" in
            l*|h*) smoke_fail "exact-head archive contains a forbidden link: $entry" ;;
        esac
    done < <(tar -tvf "$archive")
    tar -xf "$archive" -C "$source_root"
    local required
    for required in "$@"; do
        [ -f "$source_root/$required" ] \
            || smoke_fail "exact-head archive is missing $required"
    done
    sha256sum "$archive" | awk '{print $1}'
}

smoke_export_secrets() {
    export OGS_S2_SMOKE_MYSQL_ROOT_PASSWORD="$(smoke_urlsafe_secret 30)"
    export OGS_S2_SMOKE_MYSQL_PASSWORD="$(smoke_urlsafe_secret 30)"
    export OGS_S2_SMOKE_BUSINESS_REDIS_PASSWORD="$(smoke_urlsafe_secret 30)"
    export OGS_S2_SMOKE_AUTONOMY_REDIS_PASSWORD="$(smoke_urlsafe_secret 30)"
    export OGS_S2_SMOKE_FLASK_SECRET="$(smoke_urlsafe_secret 48)"
    export OGS_S2_SMOKE_FERNET_KEY="$(smoke_urlsafe_secret 32 keep)"
}

# Remove the disposable temp root only when it still lives under the expected
# smoke parent directory.
smoke_remove_temp_root() {
    local temp_root="$1" smoke_parent="$2" resolved
    [ -n "$temp_root" ] && [ -e "$temp_root" ] || return 0
    resolved="$(cd "$(dirname "$temp_root")" && pwd)/$(basename "$temp_root")"
    case "$resolved" in
        "$smoke_parent"/*) rm -rf -- "$resolved" ;;
        *) echo "[smoke] WARNING: refusing to remove unexpected smoke path: $resolved" >&2; return 1 ;;
    esac
}
