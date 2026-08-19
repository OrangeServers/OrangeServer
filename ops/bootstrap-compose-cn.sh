#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPOSITORY_URL="${OGS_CN_REPOSITORY_URL:-https://gitee.com/orangeservers/OrangeServer.git}"
BACKEND_IMAGE="${OGS_CN_BACKEND_IMAGE:-ccr.ccs.tencentyun.com/xuwei777/orangeserver-backend}"
NGINX_IMAGE="${OGS_CN_NGINX_IMAGE:-m.daocloud.io/docker.io/library/nginx@sha256:516475cc129da42866742567714ddc681e5eed7b9ee0b9e9c015e464b4221a00}"
REDIS_IMAGE="${OGS_CN_REDIS_IMAGE:-m.daocloud.io/docker.io/library/redis@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2}"
MYSQL_IMAGE="${OGS_CN_MYSQL_IMAGE:-m.daocloud.io/docker.io/library/mysql@sha256:63823b8e2cbe4ae0c558155e02d00beba56130fbc3d147efccbdb328ae2dbb9e}"
AUTONOMY_REDIS_IMAGE="${OGS_CN_AUTONOMY_REDIS_IMAGE:-m.daocloud.io/docker.io/redis/redis-stack-server@sha256:7d8e657d60d525534d6abe96e7645ab30ef1b4f1ffedc6eeb2d4d4283b3a49b9}"
VERSION=""
FORWARD_ARGS=()

usage() {
    cat <<'EOF'
Usage:
  bootstrap-compose-cn.sh --version vX.Y.Z [--install-dir DIR] [--port PORT] [--project-name NAME]

This is the China mainland entry point for the same bundled Docker Compose
installer. It clones the fixed release tag from the Gitee mirror, builds the
deployment bundle locally, and delegates to bootstrap-compose.sh with the
Tencent Cloud TCR backend image and digest-pinned DaoCloud public mirrors for
Nginx, Redis, MySQL and the autonomy Redis Stack. OGS_CN_*_IMAGE environment
variables can override them.
EOF
}

fail() {
    echo "[FAIL] $*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version)
            VERSION="${2:-}"
            FORWARD_ARGS+=("$1" "${2:-}")
            shift 2
            ;;
        --install-dir|--port|--project-name)
            FORWARD_ARGS+=("$1" "${2:-}")
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || fail "--version must use stable SemVer, for example v1.2.3"

for command in git mktemp bash; do
    command -v "$command" >/dev/null 2>&1 \
        || fail "required command not found: $command"
done

WORK_DIR="$(mktemp -d)"
cleanup() {
    rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT

repository_dir="${WORK_DIR}/repository"
assets_dir="${WORK_DIR}/release-assets"
git clone \
    --quiet \
    --depth 1 \
    --branch "$VERSION" \
    --single-branch \
    "$REPOSITORY_URL" \
    "$repository_dir" \
    || fail "failed to clone ${VERSION} from the Gitee mirror"

cd "$repository_dir"
[ -x ops/build-deploy-bundle.sh ] \
    || fail "release tag does not contain ops/build-deploy-bundle.sh"
[ -x ops/bootstrap-compose.sh ] \
    || fail "release tag does not contain ops/bootstrap-compose.sh"

bash ops/build-deploy-bundle.sh \
    --version "$VERSION" \
    --output-dir "$assets_dir"

bash ops/bootstrap-compose.sh \
    "${FORWARD_ARGS[@]}" \
    --backend-image "$BACKEND_IMAGE" \
    --nginx-image "$NGINX_IMAGE" \
    --redis-image "$REDIS_IMAGE" \
    --mysql-image "$MYSQL_IMAGE" \
    --autonomy-redis-image "$AUTONOMY_REDIS_IMAGE" \
    --bundle-file "${assets_dir}/orangeserver-deploy-${VERSION}.tar.gz" \
    --checksum-file "${assets_dir}/orangeserver-deploy-${VERSION}.tar.gz.sha256"
