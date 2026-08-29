#!/usr/bin/env bash
# Lightweight, offline contract checks for the public bootstrap entry points.
#
# This script intentionally replaces Docker, git and the installer helpers with
# small fakes.  It must never contact a registry, clone a real repository or
# create a persistent deployment.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CANONICAL_INSTALLER="${SCRIPT_DIR}/bootstrap-compose.sh"
CN_INSTALLER="${SCRIPT_DIR}/bootstrap-compose-cn.sh"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
FAKE_BIN="${TEST_ROOT}/bin"

fail() {
    echo "[FAIL] $*" >&2
    exit 1
}

cleanup() {
    rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

expect_file_contains() {
    local file="$1"
    local expected="$2"
    grep -Fqx -- "$expected" "$file" >/dev/null \
        || fail "expected ${file} to contain: ${expected}"
}

expect_failure() {
    local message="$1"
    shift
    if "$@" >"${TEST_ROOT}/failure.out" 2>&1; then
        fail "expected failure: ${message}"
    fi
    grep -F -- "$message" "${TEST_ROOT}/failure.out" >/dev/null \
        || fail "failure output did not contain: ${message}"
}

make_minimal_bundle() {
    local version="$1"
    local stage="${TEST_ROOT}/bundle-stage"
    local bundle_root="${stage}/orangeserver"
    local archive="${TEST_ROOT}/orangeserver-deploy-${version}.tar.gz"

    mkdir -p "${bundle_root}/backend" "${bundle_root}/ops"
    : > "${bundle_root}/.env.example"
    : > "${bundle_root}/backend/.env.example"
    cat > "${bundle_root}/ops/preflight-compose.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "${bundle_root}/ops/preflight-compose.sh"
    tar -C "$stage" -czf "$archive" orangeserver
    sha256sum "$archive" > "${archive}.sha256"
    printf '%s\n' "$archive"
}

mkdir -p "$FAKE_BIN"
cat > "${FAKE_BIN}/id" <<'EOF'
#!/bin/bash
if [ "${1:-}" = "-u" ]; then
    printf '0\n'
    exit 0
fi
exit 1
EOF
cat > "${FAKE_BIN}/docker" <<'EOF'
#!/bin/bash
case "${1:-}" in
    info)
        exit 0
        ;;
    compose)
        [ "${2:-}" = "version" ] && exit 0
        ;;
    volume)
        [ "${2:-}" = "inspect" ] && exit 1
        ;;
esac
echo "unexpected docker invocation: $*" >&2
exit 1
EOF
cat > "${FAKE_BIN}/openssl" <<'EOF'
#!/bin/bash
[ "${1:-}" = "rand" ] && [ "${2:-}" = "-hex" ] || exit 1
printf 'test-secret\n'
EOF
cat > "${FAKE_BIN}/make" <<'EOF'
#!/bin/bash
[ "${1:-}" = "docker-up-image" ] || exit 1
grep '^OGS_.*IMAGE=' .env > "${TEST_RECORD:?}"
EOF
chmod +x "${FAKE_BIN}/id" "${FAKE_BIN}/docker" "${FAKE_BIN}/openssl" "${FAKE_BIN}/make"

VERSION="v1.2.3"
BUNDLE_FILE="$(make_minimal_bundle "$VERSION")"
CHECKSUM_FILE="${BUNDLE_FILE}.sha256"

run_canonical() {
    local name="$1"
    local expected_image="$2"
    shift 2
    local record="${TEST_ROOT}/${name}.env"
    if ! TEST_RECORD="$record" PATH="${FAKE_BIN}:${PATH}" "$CANONICAL_INSTALLER" \
        --version "$VERSION" \
        --bundle-file "$BUNDLE_FILE" \
        --checksum-file "$CHECKSUM_FILE" \
        --install-dir "${TEST_ROOT}/${name}" \
        "$@" \
        >"${TEST_ROOT}/${name}.out" 2>&1; then
        cat "${TEST_ROOT}/${name}.out" >&2
        fail "canonical installer failed for ${name}"
    fi
    expect_file_contains "$record" "OGS_BACKEND_IMAGE=${expected_image}"
    expect_file_contains "$record" "OGS_REDIS_IMAGE=redis:8.10.0"
    expect_file_contains "$record" "OGS_MYSQL_IMAGE=mysql:8.0.42"
}

run_canonical "canonical-default" \
    "ghcr.io/orangeservers/orangeserver-backend"
run_canonical "canonical-custom" \
    "ccr.ccs.tencentyun.com/xuwei777/orangeserver-backend" \
    --backend-image "ccr.ccs.tencentyun.com/xuwei777/orangeserver-backend"
run_canonical "canonical-registry-port" \
    "registry.example.test:5000/orangeserver/backend" \
    --backend-image "registry.example.test:5000/orangeserver/backend"
custom_record="${TEST_ROOT}/canonical-images.env"
TEST_RECORD="$custom_record" PATH="${FAKE_BIN}:${PATH}" "$CANONICAL_INSTALLER" \
    --version "$VERSION" \
    --bundle-file "$BUNDLE_FILE" \
    --checksum-file "$CHECKSUM_FILE" \
    --install-dir "${TEST_ROOT}/canonical-images" \
    --redis-image "mirror.example.test/redis@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
    --mysql-image "registry.example.test:5000/mysql:8.0" \
    >"${TEST_ROOT}/canonical-images.out" 2>&1 \
    || fail "canonical installer failed for image overrides"
expect_file_contains "$custom_record" "OGS_REDIS_IMAGE=mirror.example.test/redis@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
expect_file_contains "$custom_record" "OGS_MYSQL_IMAGE=registry.example.test:5000/mysql:8.0"

expect_failure "--backend-image must be an untagged lowercase container image path" \
    "$CANONICAL_INSTALLER" \
    --version "$VERSION" \
    --backend-image "ccr.ccs.tencentyun.com/xuwei777/orangeserver-backend:v9"
expect_failure "--backend-image must be an untagged lowercase container image path" \
    "$CANONICAL_INSTALLER" \
    --version "$VERSION" \
    --backend-image "ccr.ccs.tencentyun.com:5000/xuwei777/orangeserver-backend:v9"
expect_failure "--backend-image must be an untagged lowercase container image path" \
    "$CANONICAL_INSTALLER" \
    --version "$VERSION" \
    --backend-image "GHCR.IO/orangeservers/orangeserver-backend"
expect_failure "--mysql-image must be a lowercase tagged or digest-pinned container image reference" \
    "$CANONICAL_INSTALLER" \
    --version "$VERSION" \
    --mysql-image "registry.example.test:5000/mysql"

cat > "${FAKE_BIN}/git" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "${TEST_GIT_ARGS:?}"
[ "${1:-}" = "clone" ] || exit 1
destination="${!#}"
mkdir -p "${destination}/ops"
: > "${destination}/ops/build-deploy-bundle.sh"
: > "${destination}/ops/bootstrap-compose.sh"
chmod +x "${destination}/ops/build-deploy-bundle.sh" "${destination}/ops/bootstrap-compose.sh"
EOF
cat > "${FAKE_BIN}/bash" <<'EOF'
#!/bin/bash
case "${1:-}" in
    ops/build-deploy-bundle.sh)
        printf '%s\n' "$@" > "${TEST_BUILD_ARGS:?}"
        output_dir=""
        while [ "$#" -gt 0 ]; do
            if [ "$1" = "--output-dir" ]; then
                output_dir="$2"
                break
            fi
            shift
        done
        [ -n "$output_dir" ] || exit 1
        mkdir -p "$output_dir"
        : > "${output_dir}/orangeserver-deploy-v1.2.3.tar.gz"
        : > "${output_dir}/orangeserver-deploy-v1.2.3.tar.gz.sha256"
        exit 0
        ;;
    ops/bootstrap-compose.sh)
        printf '%s\n' "$@" > "${TEST_INSTALL_ARGS:?}"
        exit 0
        ;;
esac
exec /bin/bash "$@"
EOF
chmod +x "${FAKE_BIN}/git" "${FAKE_BIN}/bash"

CN_GIT_ARGS="${TEST_ROOT}/cn-git-args"
CN_BUILD_ARGS="${TEST_ROOT}/cn-build-args"
CN_INSTALL_ARGS="${TEST_ROOT}/cn-installer-args"
TEST_GIT_ARGS="$CN_GIT_ARGS" \
TEST_BUILD_ARGS="$CN_BUILD_ARGS" \
TEST_INSTALL_ARGS="$CN_INSTALL_ARGS" \
PATH="${FAKE_BIN}:${PATH}" \
"$CN_INSTALLER" \
    --version "$VERSION" \
    --install-dir /opt/orangeserver-cn \
    --port 18081 \
    --project-name orangeserver_cn \
    >"${TEST_ROOT}/cn.out" 2>&1 \
    || fail "China entry point failed"

expect_file_contains "$CN_GIT_ARGS" "clone"
expect_file_contains "$CN_GIT_ARGS" "--depth"
expect_file_contains "$CN_GIT_ARGS" "1"
expect_file_contains "$CN_GIT_ARGS" "--branch"
expect_file_contains "$CN_GIT_ARGS" "$VERSION"
expect_file_contains "$CN_GIT_ARGS" "--single-branch"
expect_file_contains "$CN_GIT_ARGS" "https://gitee.com/orangeservers/OrangeServer.git"
expect_file_contains "$CN_BUILD_ARGS" "--version"
expect_file_contains "$CN_BUILD_ARGS" "$VERSION"
expect_file_contains "$CN_INSTALL_ARGS" "--backend-image"
expect_file_contains "$CN_INSTALL_ARGS" "ccr.ccs.tencentyun.com/xuwei777/orangeserver-backend"
expect_file_contains "$CN_INSTALL_ARGS" "--redis-image"
expect_file_contains "$CN_INSTALL_ARGS" "m.daocloud.io/docker.io/library/redis:8.10.0"
expect_file_contains "$CN_INSTALL_ARGS" "--mysql-image"
expect_file_contains "$CN_INSTALL_ARGS" "m.daocloud.io/docker.io/library/mysql@sha256:63823b8e2cbe4ae0c558155e02d00beba56130fbc3d147efccbdb328ae2dbb9e"
expect_file_contains "$CN_INSTALL_ARGS" "--bundle-file"
expect_file_contains "$CN_INSTALL_ARGS" "--checksum-file"
expect_file_contains "$CN_INSTALL_ARGS" "--install-dir"
expect_file_contains "$CN_INSTALL_ARGS" "/opt/orangeserver-cn"
expect_file_contains "$CN_INSTALL_ARGS" "--port"
expect_file_contains "$CN_INSTALL_ARGS" "18081"
expect_file_contains "$CN_INSTALL_ARGS" "--project-name"
expect_file_contains "$CN_INSTALL_ARGS" "orangeserver_cn"

TEST_GIT_ARGS="${TEST_ROOT}/cn-override-git-args" \
TEST_BUILD_ARGS="${TEST_ROOT}/cn-override-build-args" \
TEST_INSTALL_ARGS="${TEST_ROOT}/cn-override-installer-args" \
OGS_CN_REDIS_IMAGE="mirror.example.test/redis:7.4" \
OGS_CN_MYSQL_IMAGE="mirror.example.test/mysql:8.0" \
PATH="${FAKE_BIN}:${PATH}" \
"$CN_INSTALLER" \
    --version "$VERSION" \
    >"${TEST_ROOT}/cn-override.out" 2>&1 \
    || fail "China entry point failed for dependency image overrides"
expect_file_contains "${TEST_ROOT}/cn-override-installer-args" "mirror.example.test/redis:7.4"
expect_file_contains "${TEST_ROOT}/cn-override-installer-args" "mirror.example.test/mysql:8.0"

expect_failure "unknown argument: --backend-image" \
    "$CN_INSTALLER" --version "$VERSION" --backend-image example.invalid/image

for document in "$REPO_ROOT/DEPLOY.md" "$REPO_ROOT/README.md" "$REPO_ROOT/README.zh-CN.md"; do
    awk '
        /^```bash[[:space:]]*$/ { in_block = 1; pipefail = 0; curl_seen = 0; curl_bash = 0; next }
        /^```[[:space:]]*$/ {
            if (curl_bash && !pipefail) exit 1
            in_block = 0
            next
        }
        in_block && /set -o pipefail/ { pipefail = 1 }
        in_block && /curl[[:space:]]/ { curl_seen = 1 }
        in_block && curl_seen && /\|[[:space:]]*(sudo[[:space:]]+)?bash/ { curl_bash = 1 }
    ' "$document" || fail "curl|bash block without set -o pipefail: ${document}"
done

echo "[OK] bootstrap shell contract checks passed"
