#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPOSITORY="OrangeServers/OrangeServer"
VERSION=""
INSTALL_DIR="/opt/orangeserver"
HTTP_PORT="8080"
PROJECT_NAME="orangeserver"
BUNDLE_FILE=""
CHECKSUM_FILE=""
BACKEND_IMAGE="ghcr.io/orangeservers/orangeserver-backend"
NGINX_IMAGE="nginx:1.25-alpine"
REDIS_IMAGE="redis:7.4-alpine"
MYSQL_IMAGE="mysql:8.0.42"

usage() {
    cat <<'EOF'
Usage:
  bootstrap-compose.sh --version vX.Y.Z [--install-dir DIR] [--port PORT] [--project-name NAME] [--backend-image IMAGE]
  bootstrap-compose.sh --version vX.Y.Z --bundle-file FILE [--checksum-file FILE] [--project-name NAME] [--backend-image IMAGE]

This is a thin installer for the Docker Compose bundled deployment. It downloads
and verifies a versioned release bundle, generates infrastructure credentials,
runs the repository preflight, and calls make docker-up-image.

Advanced registry overrides:
  --nginx-image REF --redis-image REF --mysql-image REF
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
            shift 2
            ;;
        --install-dir)
            INSTALL_DIR="${2:-}"
            shift 2
            ;;
        --port)
            HTTP_PORT="${2:-}"
            shift 2
            ;;
        --project-name)
            PROJECT_NAME="${2:-}"
            shift 2
            ;;
        --bundle-file)
            BUNDLE_FILE="${2:-}"
            shift 2
            ;;
        --checksum-file)
            CHECKSUM_FILE="${2:-}"
            shift 2
            ;;
        --backend-image)
            BACKEND_IMAGE="${2:-}"
            shift 2
            ;;
        --nginx-image)
            NGINX_IMAGE="${2:-}"
            shift 2
            ;;
        --redis-image)
            REDIS_IMAGE="${2:-}"
            shift 2
            ;;
        --mysql-image)
            MYSQL_IMAGE="${2:-}"
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
[[ "$HTTP_PORT" =~ ^[0-9]+$ ]] \
    && [ "$HTTP_PORT" -ge 1 ] && [ "$HTTP_PORT" -le 65535 ] \
    || fail "--port must be an integer between 1 and 65535"
shopt -u nocasematch
[ "$PROJECT_NAME" = "${PROJECT_NAME,,}" ] \
    && [[ "$PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] \
    || fail "--project-name must start with a lowercase letter or digit and contain only lowercase letters, digits, hyphens, or underscores"
[[ "$INSTALL_DIR" = /* ]] && [ "$INSTALL_DIR" != "/" ] \
    || fail "--install-dir must be an absolute path other than /"
[[ "$BACKEND_IMAGE" =~ ^[a-z0-9][a-z0-9./:_-]*[a-z0-9]$ ]] \
    && [[ "$BACKEND_IMAGE" == */* ]] \
    && [[ "${BACKEND_IMAGE#*/}" != *":"* ]] \
    && [[ "$BACKEND_IMAGE" != *"//"* ]] \
    && [[ "$BACKEND_IMAGE" != *".."* ]] \
    || fail "--backend-image must be an untagged lowercase container image path"
validate_image_ref() {
    local option="$1"
    local image_ref="$2"
    local image_tail="${image_ref##*/}"
    [[ "$image_ref" =~ ^[a-z0-9][a-z0-9./:_@-]*[a-z0-9]$ ]] \
        && [[ "$image_ref" != *"//"* ]] \
        && [[ "$image_ref" != *".."* ]] \
        && {
            [[ "$image_ref" =~ @sha256:[0-9a-f]{64}$ ]] \
                || { [[ "$image_ref" != *"@"* ]] && [[ "$image_tail" =~ ^[^:]+:[^:]+$ ]]; }
        } \
        || fail "${option} must be a lowercase tagged or digest-pinned container image reference"
}
validate_image_ref --nginx-image "$NGINX_IMAGE"
validate_image_ref --redis-image "$REDIS_IMAGE"
validate_image_ref --mysql-image "$MYSQL_IMAGE"
[ "$(id -u)" -eq 0 ] || fail "run this installer as root (for example through sudo)"

for command in docker find make openssl sed tar sha256sum mktemp; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done
docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is unavailable"

if [ -e "$INSTALL_DIR" ]; then
    fail "$INSTALL_DIR already exists; use the documented upgrade flow instead"
fi
for volume in "${PROJECT_NAME}_mysql-data" "${PROJECT_NAME}_backend-data"; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then
        fail "existing volume $volume found; refusing to treat an existing deployment as fresh"
    fi
done

WORK_DIR="$(mktemp -d)"
cleanup() {
    rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT

archive_name="orangeserver-deploy-${VERSION}.tar.gz"
if [ -n "$BUNDLE_FILE" ]; then
    [ -f "$BUNDLE_FILE" ] || fail "bundle file not found: $BUNDLE_FILE"
    cp "$BUNDLE_FILE" "${WORK_DIR}/${archive_name}"
    if [ -z "$CHECKSUM_FILE" ]; then
        CHECKSUM_FILE="${BUNDLE_FILE}.sha256"
    fi
    [ -f "$CHECKSUM_FILE" ] || fail "checksum file not found: $CHECKSUM_FILE"
    cp "$CHECKSUM_FILE" "${WORK_DIR}/${archive_name}.sha256"
else
    command -v curl >/dev/null 2>&1 || fail "required command not found: curl"
    release_url="https://github.com/${REPOSITORY}/releases/download/${VERSION}"
    curl -fsSL --retry 3 -o "${WORK_DIR}/${archive_name}" \
        "${release_url}/${archive_name}"
    curl -fsSL --retry 3 -o "${WORK_DIR}/${archive_name}.sha256" \
        "${release_url}/${archive_name}.sha256"
fi

(
    cd "$WORK_DIR"
    sha256sum -c "${archive_name}.sha256"
)
if tar -tzf "${WORK_DIR}/${archive_name}" \
    | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
    fail "release bundle contains an unsafe path"
fi
tar -C "$WORK_DIR" -xzf "${WORK_DIR}/${archive_name}"
[ -d "${WORK_DIR}/orangeserver" ] || fail "release bundle has an unexpected layout"

bundle_root="${WORK_DIR}/orangeserver"
cd "$bundle_root"

# The China entry point clones under umask 077. Static files are public assets,
# so normalize only frontend/dist for the unprivileged Nginx worker. Secrets and
# runtime data keep their restrictive permissions.
find frontend/dist -type d -exec chmod 755 {} +
find frontend/dist -type f -exec chmod 644 {} +

cp .env.example .env
cp backend/.env.example backend/.env
chmod 600 .env backend/.env

set_key() {
    local file="$1"
    local key="$2"
    local value="$3"
    if grep -q "^${key}=" "$file"; then
        sed -i "s|^${key}=.*$|${key}=${value}|" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

mysql_root_password="$(openssl rand -hex 24)"
mysql_app_password="$(openssl rand -hex 24)"
redis_password="$(openssl rand -hex 24)"
autonomy_redis_password="$(openssl rand -hex 24)"

set_key .env COMPOSE_PROJECT_NAME "$PROJECT_NAME"
set_key .env OGS_BACKEND_IMAGE "$BACKEND_IMAGE"
set_key .env OGS_BACKEND_TAG "$VERSION"
set_key .env OGS_NGINX_IMAGE "$NGINX_IMAGE"
set_key .env OGS_REDIS_IMAGE "$REDIS_IMAGE"
set_key .env OGS_MYSQL_IMAGE "$MYSQL_IMAGE"
set_key .env OGS_HTTP_PORT "$HTTP_PORT"
set_key .env MYSQL_ROOT_PASSWORD "$mysql_root_password"
set_key .env OGS_MYSQL_HOST mysql
set_key .env OGS_MYSQL_PORT 3306
set_key .env OGS_MYSQL_DBNAME orange
set_key .env OGS_MYSQL_USER app_user
set_key .env OGS_MYSQL_PASSWORD "$mysql_app_password"
set_key .env OGS_REDIS_HOST redis
set_key .env OGS_REDIS_PORT 6379
set_key .env OGS_REDIS_PASSWORD "$redis_password"
set_key .env OGS_AI_AUTONOMY_ENABLED true
set_key .env OGS_AI_AUTONOMY_REDIS_HOST autonomy-redis
set_key .env OGS_AI_AUTONOMY_REDIS_PORT 6379
set_key .env OGS_AI_AUTONOMY_REDIS_PASSWORD "$autonomy_redis_password"
set_key .env OGS_HTTPS false

set_key backend/.env OGS_ENV prod
set_key backend/.env OGS_FLASK_SECRET_KEY ""
set_key backend/.env OGS_FERNET_KEYS ""
set_key backend/.env OGS_MYSQL_HOST mysql
set_key backend/.env OGS_MYSQL_PORT 3306
set_key backend/.env OGS_MYSQL_DBNAME orange
set_key backend/.env OGS_MYSQL_USER app_user
set_key backend/.env OGS_MYSQL_PASSWORD "$mysql_app_password"
set_key backend/.env OGS_REDIS_HOST redis
set_key backend/.env OGS_REDIS_PORT 6379
set_key backend/.env OGS_REDIS_PASSWORD "$redis_password"
set_key backend/.env OGS_AI_AUTONOMY_ENABLED true
set_key backend/.env OGS_AI_AUTONOMY_REDIS_HOST autonomy-redis
set_key backend/.env OGS_AI_AUTONOMY_REDIS_PORT 6379
set_key backend/.env OGS_AI_AUTONOMY_REDIS_PASSWORD "$autonomy_redis_password"
set_key backend/.env OGS_CSRF_ALLOWED_ORIGINS ""
set_key backend/.env OGS_CORS_ORIGINS ""
set_key backend/.env OGS_HTTPS false

# Finish all read-only validation before making the installation directory visible.
bash ops/preflight-compose.sh bundled
printf '%s\n' "$VERSION" > .orangeserver-version
chmod 600 .orangeserver-version

parent_dir="$(dirname "$INSTALL_DIR")"
mkdir -p "$parent_dir"
mv "$bundle_root" "$INSTALL_DIR"
cd "$INSTALL_DIR"

if ! make docker-up-image; then
    cat >&2 <<EOF
[FAIL] The verified deployment files remain at ${INSTALL_DIR}.
After fixing registry/network availability, retry safely with:
  cd ${INSTALL_DIR} && make docker-up-image
No data volume was removed.
EOF
    exit 1
fi

server_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
server_ip="${server_ip:-SERVER_IP}"
cat <<EOF

[OK] OrangeServer ${VERSION} started.
Open: http://${server_ip}:${HTTP_PORT}/setup
To read the one-time Setup Token:
  cd ${INSTALL_DIR} && make setup-token
EOF
