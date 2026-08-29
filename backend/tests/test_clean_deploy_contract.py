# -*- coding: utf-8 -*-
"""Cold-start deployment contracts exercised by the real deployment artifacts."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
DEPLOY = REPO_ROOT / "deploy"
OPS = REPO_ROOT / "ops"


def test_docker_health_reads_non_default_port_from_root_env():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("docker-health:", 1)[1].split("\n\n", 1)[0]
    assert "OGS_HTTP_PORT" in target
    assert "/.env" in target
    assert "${port:-8080}" in target


def test_private_repository_does_not_allocate_github_hosted_runners():
    workflows = (
        REPO_ROOT / ".github" / "workflows" / "ci.yml",
        REPO_ROOT / ".github" / "workflows" / "deploy-site.yml",
    )
    for path in workflows:
        source = path.read_text(encoding="utf-8")
        jobs_source = source.split("\njobs:\n", 1)[1]
        job_count = len(
            re.findall(r"^  [a-zA-Z][\w-]*:\s*$", jobs_source, re.MULTILINE)
        )
        guard_count = source.count("if: ${{ github.event.repository.private == false }}")
        assert guard_count == job_count, f"{path.name} has an unguarded job"


def test_backend_image_publish_is_guarded_while_repository_is_private():
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "publish-backend-image.yml"
    ).read_text(encoding="utf-8")
    assert "packages: write" in workflow
    assert "contents: write" in workflow
    assert (
        "github.event.repository.private == false"
        " && github.repository == 'OrangeServers/OrangeServer'"
    ) in workflow
    assert "secrets.GITHUB_TOKEN" in workflow
    assert "linux/amd64" in workflow
    assert "release:" not in workflow.split("permissions:", 1)[0]
    assert "workflow_dispatch:" in workflow
    assert "context: ." in workflow
    assert "ops/build-deploy-bundle.sh" in workflow
    assert "gh release upload" in workflow
    assert "gh release view" in workflow
    assert "--json tagName" in workflow
    assert "packages/container/orangeserver-backend/versions" in workflow
    assert "GHCR tag $RELEASE_TAG already exists" in workflow
    assert "could not verify GHCR tag immutability" in workflow
    assert '"release-assets/bootstrap-compose.sh"' in workflow
    assert "--clobber" not in workflow
    assert ":latest" not in workflow


def test_prebuilt_image_path_is_explicit_and_local_build_remains_default():
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    expected_image = (
        "${OGS_BACKEND_IMAGE:-orangeserver-backend}:"
        "${OGS_BACKEND_TAG:-latest}"
    )
    assert expected_image in compose
    assert "build:" in compose
    local_target = makefile.split("docker-up:", 1)[1].split("\n\n", 1)[0]
    assert "--build" in local_target
    image_target = makefile.split("docker-up-image:", 1)[1].split("\n\n", 1)[0]
    assert "OGS_BACKEND_IMAGE" in image_target
    assert "OGS_BACKEND_TAG" in image_target
    assert "禁止 latest" in image_target
    assert "--no-build" in image_target
    assert "env -u OGS_BACKEND_IMAGE -u OGS_BACKEND_TAG" in makefile
    assert "# OGS_BACKEND_IMAGE=ghcr.io/orangeservers/orangeserver-backend" in env_example


def test_compose_bootstrap_is_a_versioned_checksumming_thin_wrapper():
    bootstrap = (OPS / "bootstrap-compose.sh").read_text(encoding="utf-8")
    docs = (REPO_ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "--version must use stable SemVer" in bootstrap
    assert "--project-name" in bootstrap
    assert '"${PROJECT_NAME,,}"' in bootstrap
    assert '"${PROJECT_NAME}_mysql-data"' in bootstrap
    assert 'set_key .env COMPOSE_PROJECT_NAME "$PROJECT_NAME"' in bootstrap
    assert "releases/download/${VERSION}" in bootstrap
    assert "sha256sum -c" in bootstrap
    assert "bash ops/preflight-compose.sh bundled" in bootstrap
    assert "make docker-up-image" in bootstrap
    assert "openssl rand -hex" in bootstrap
    assert "OGS_FLASK_SECRET_KEY \"\"" in bootstrap
    assert "OGS_FERNET_KEYS \"\"" in bootstrap
    assert 'set_key .env OGS_AI_AUTONOMY_ENABLED true' in bootstrap
    assert 'set_key .env OGS_AI_AUTONOMY_REDIS_HOST redis' in bootstrap
    assert (
        'set_key .env OGS_AI_AUTONOMY_REDIS_PASSWORD '
        '"$redis_password"'
    ) in bootstrap
    assert 'set_key .env OGS_AUTONOMY_WORKER_CONCURRENCY 2' in bootstrap
    assert "docker volume inspect" in bootstrap
    assert "docker build" not in bootstrap
    assert "down -v" not in bootstrap
    assert (
        "github.com/OrangeServers/OrangeServer/releases/download/"
        "vX.Y.Z/bootstrap-compose.sh"
    ) in docs
    assert "raw.githubusercontent.com" not in docs


def test_compose_does_not_force_global_container_names():
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    assert "container_name:" not in compose


def test_standard_compose_is_four_containers_with_one_redis():
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    redis_service = compose.split("  redis:\n", 1)[1].split("\n  mysql:\n", 1)[0]
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    backend_env = (REPO_ROOT / "backend" / ".env.example").read_text(
        encoding="utf-8",
    )
    assert all(f"  {service}:" in compose for service in ("app", "worker", "redis", "mysql"))
    assert "  frontend:" not in compose
    assert "  autonomy-redis:" not in compose
    assert "redis:8.10.0" in compose
    assert "redis:8.10.0-alpine" not in compose
    assert "docker-entrypoint.sh redis-server --appendonly yes" in redis_service
    assert "--maxmemory-policy volatile-lru" in redis_service
    assert '"$$OGS_REDIS_PASSWORD"' in redis_service
    assert "--requirepass\n      - ${OGS_REDIS_PASSWORD:-}" not in redis_service
    assert "app.ai.autonomy.celery_entry:celery_app" in compose
    assert compose.count(
        "OGS_AI_AUTONOMY_ENABLED: ${OGS_AI_AUTONOMY_ENABLED:-true}",
    ) == 2
    assert compose.count("OGS_AI_AUTONOMY_REDIS_HOST: ${OGS_AI_AUTONOMY_REDIS_HOST:-redis}") == 2
    assert compose.count("OGS_REDIS_DB: ${OGS_REDIS_DB:-2}") == 2
    assert compose.count(
        "OGS_REDIS_MAX_CONNECTIONS: ${OGS_REDIS_MAX_CONNECTIONS:-256}",
    ) == 2
    assert 'OGS_PROXY_LAYERS: "0"' in compose
    assert "OGS_AUTONOMY_WORKER_CONCURRENCY: ${OGS_AUTONOMY_WORKER_CONCURRENCY:-2}" in compose
    assert "autonomy-redis-data:" in compose
    assert "OGS_AI_AUTONOMY_ENABLED=" in env_example
    assert "OGS_REDIS_MAX_CONNECTIONS=256" in env_example
    assert "OGS_AI_AUTONOMY_REDIS_PASSWORD=" not in env_example
    assert "OGS_AI_AUTONOMY_ENABLED=" in backend_env


def test_release_bundle_contains_all_compose_runtime_inputs():
    builder = (OPS / "build-deploy-bundle.sh").read_text(encoding="utf-8")
    for path in (
        ".env.example",
        "Makefile",
        "backend/.env.example",
        "backend/mysqldir/orange.sql",
        "deploy/docker-compose.yml",
        "ops/preflight-compose.sh",
        "ops/bootstrap-compose.sh",
    ):
        assert f'"{path}"' in builder
    assert "sha256sum" in builder
    assert "frontend/dist" not in builder
    assert "deploy/nginx" not in builder
    assert '"CHANGELOG.md"' in builder
    assert '"docs/operations/UPGRADE.md"' in builder
    assert '"${ROOT}/backend/mysqldir/"*.sql' in builder
    assert '"${OUTPUT_DIR}/bootstrap-compose.sh"' in builder
    assert "install -m 0755" in builder


def test_docker_daemon_example_contains_only_supported_documented_keys():
    config = json.loads(
        (DEPLOY / "daemon.json.example").read_text(encoding="utf-8")
    )
    assert set(config) == {
        "registry-mirrors",
        "max-concurrent-downloads",
        "log-driver",
        "log-opts",
    }


def test_preflight_allows_absent_optional_env_key(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required to execute the deployment preflight probe")
    source = (OPS / "preflight-compose.sh").read_text(encoding="utf-8")
    match = re.search(r"load_env_val\(\) \{.*?\n\}", source, re.DOTALL)
    assert match, "preflight must define load_env_val"
    env_file = tmp_path / "backend.env"
    env_file.write_text("OGS_FERNET_KEYS=\n", encoding="utf-8")
    probe = (
        "set -uo pipefail\n"
        f"{match.group(0)}\n"
        f"load_env_val '{env_file.as_posix()}' OGS_FERNET_KEY >/dev/null\n"
    )
    result = subprocess.run(
        ["bash", "-c", probe], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_preflight_does_not_create_a_false_shell_port_override():
    source = (OPS / "preflight-compose.sh").read_text(encoding="utf-8")
    assert "HTTP_PORT_VALUE=$(load_env_val" in source
    assert "\nOGS_HTTP_PORT=$(load_env_val" not in source
    assert "OGS_BACKEND_IMAGE OGS_BACKEND_TAG" in source
    assert "ip_local_port_range" in source
    assert "ip_local_reserved_ports" in source


def test_container_nginx_config_is_valid_for_non_default_port():
    common = (DEPLOY / "nginx" / "ogs_proxy_common.conf").read_text(encoding="utf-8")
    frontend = (DEPLOY / "nginx" / "frontend_container.conf").read_text(encoding="utf-8")
    assert "proxy_set_header Host $http_host;" in common
    assert "proxy_set_header Host $host;" not in common
    assert "proxy_read_timeout" not in common
    assert "proxy_send_timeout" not in common
    assert frontend.count("proxy_set_header Host $http_host;") == 2
    assert "proxy_set_header Host $host;" not in frontend


def test_anonymous_routes_explicitly_skip_session_csrf():
    from app.api import account_api, local_api

    anonymous = [
        rule
        for module in (account_api, local_api)
        for rule in module.ROUTES
        if not rule.need_auth
    ]
    assert anonymous
    assert all(rule.skip_csrf for rule in anonymous), [
        rule.url for rule in anonymous if not rule.skip_csrf
    ]


def test_frontend_does_not_probe_authenticated_status_without_session_cookie():
    app_vue = (REPO_ROOT / "frontend" / "src" / "App.vue").read_text(encoding="utf-8")
    assert "hasSessionCookie" in app_vue
    assert "startsWith('csrf_token=')" in app_vue
    assert "if (hasSessionCookie) appInit()" in app_vue


def test_fresh_schema_matches_password_version_model():
    schema = (BACKEND / "mysqldir" / "orange.sql").read_text(encoding="utf-8")
    table = re.search(
        r"CREATE TABLE `t_acc_user` \(.*?\n\) ENGINE=", schema, re.DOTALL,
    )
    assert table, "orange.sql must define t_acc_user"
    ddl = table.group(0)
    assert "`password_version` int" in ddl
    assert "DEFAULT '2'" in ddl or "DEFAULT 2" in ddl
    assert "idx_acc_user_password_version" in ddl
    seed_lines = [
        line for line in schema.splitlines()
        if line.startswith("INSERT INTO `t_acc_user`")
    ]
    assert seed_lines
    assert all("`password_version`" in line for line in seed_lines)


def test_baseline_tables_contain_every_current_orm_column():
    """Existing baseline tables are not altered by SQLAlchemy db.create_all()."""
    from app.core.db.database import db

    schema = (BACKEND / "mysqldir" / "orange.sql").read_text(encoding="utf-8")
    ddl_columns = {}
    for match in re.finditer(
        r"CREATE TABLE `([^`]+)` \((.*?)\)\s*ENGINE=",
        schema,
        re.IGNORECASE | re.DOTALL,
    ):
        ddl_columns[match.group(1)] = set(
            re.findall(r"^\s*`([^`]+)`\s+", match.group(2), re.MULTILINE)
        )

    missing = {}
    for table_name, table in db.metadata.tables.items():
        if table_name not in ddl_columns:
            continue
        absent = set(table.columns.keys()) - ddl_columns[table_name]
        if absent:
            missing[table_name] = sorted(absent)

    assert missing == {}, f"orange.sql is behind ORM columns: {missing}"


def test_baseline_varchar_lengths_match_current_orm():
    from app.core.db.database import db

    schema = (BACKEND / "mysqldir" / "orange.sql").read_text(encoding="utf-8")
    ddl_lengths = {}
    for match in re.finditer(
        r"CREATE TABLE `([^`]+)` \((.*?)\)\s*ENGINE=",
        schema,
        re.IGNORECASE | re.DOTALL,
    ):
        ddl_lengths[match.group(1)] = {
            name: int(length)
            for name, length in re.findall(
                r"^\s*`([^`]+)`\s+varchar\((\d+)\)",
                match.group(2),
                re.IGNORECASE | re.MULTILINE,
            )
        }

    mismatches = {}
    for table_name, table in db.metadata.tables.items():
        if table_name not in ddl_lengths:
            continue
        for column in table.columns:
            orm_length = getattr(column.type, "length", None)
            ddl_length = ddl_lengths[table_name].get(column.name)
            if orm_length and ddl_length and orm_length != ddl_length:
                mismatches[f"{table_name}.{column.name}"] = (ddl_length, orm_length)

    assert mismatches == {}, f"orange.sql varchar length != ORM: {mismatches}"


def test_rev53_autonomy_migration_matches_baseline_and_orm():
    """M1/S1: rev53 列集合仍是 orange.sql 基线与 ORM 的子集。

    rev53 迁移文件本身不可变；后续迁移（rev54+）向同表追加列，
    完整三方一致由最新迁移的契约测试断言。"""
    from app.core.db.database import db

    rev53 = (
        BACKEND / "mysqldir" / "rev53_ai_autonomy_baseline.sql"
    ).read_text(encoding="utf-8")
    schema = (BACKEND / "mysqldir" / "orange.sql").read_text(encoding="utf-8")

    def columns(source, table):
        match = re.search(
            r"CREATE TABLE(?: IF NOT EXISTS)? `" + table
            + r"` \((.*?)\)\s*ENGINE=",
            source,
            re.IGNORECASE | re.DOTALL,
        )
        assert match, f"{table} is missing from the schema source"
        return set(
            re.findall(r"^\s*`([^`]+)`\s+", match.group(1), re.MULTILINE)
        )

    tables = [
        "t_ai_autonomous_run",
        "t_ai_autonomous_step",
        "t_ai_autonomous_event",
        "t_ai_autonomous_artifact",
    ]
    for table in tables:
        rev53_columns = columns(rev53, table)
        baseline_columns = columns(schema, table)
        orm_columns = set(db.metadata.tables[table].columns.keys())
        assert rev53_columns <= baseline_columns, table
        assert rev53_columns <= orm_columns, table

    assert (
        "ADD COLUMN `ai_environment` VARCHAR(10) NOT NULL "
        "DEFAULT ''production''"
    ) in rev53
    host_ddl = re.search(
        r"CREATE TABLE `t_host` \((.*?)\)\s*ENGINE=", schema, re.DOTALL,
    )
    assert host_ddl, "orange.sql must define t_host"
    assert (
        "`ai_environment` varchar(10) NOT NULL DEFAULT 'production'"
    ) in host_ddl.group(1)
    assert "ai_environment" in db.metadata.tables["t_host"].columns


def test_rev54_autonomy_migration_matches_baseline_and_orm():
    """M1/S2: rev54 追加列仍是 orange.sql 基线与 ORM 的子集。

    rev54 迁移文件本身不可变；后续迁移（rev55+）向同表追加列，
    完整三方一致由最新迁移的契约测试断言。"""
    from app.core.db.database import db

    rev53 = (
        BACKEND / "mysqldir" / "rev53_ai_autonomy_baseline.sql"
    ).read_text(encoding="utf-8")
    rev54 = (
        BACKEND / "mysqldir" / "rev54_ai_autonomy_lease.sql"
    ).read_text(encoding="utf-8")
    schema = (BACKEND / "mysqldir" / "orange.sql").read_text(encoding="utf-8")

    def create_columns(source, table):
        match = re.search(
            r"CREATE TABLE(?: IF NOT EXISTS)? `" + table
            + r"` \((.*?)\)\s*ENGINE=",
            source,
            re.IGNORECASE | re.DOTALL,
        )
        assert match, f"{table} is missing from the schema source"
        return set(
            re.findall(r"^\s*`([^`]+)`\s+", match.group(1), re.MULTILINE)
        )

    added = set(re.findall(
        r"ADD COLUMN `(\w+)`", rev54,
    ))
    assert added == {
        "lease_owner", "lease_token", "lease_expires_at", "heartbeat_at",
        "graph_version", "active_host_id",
    }, added
    # 每个 ALTER 都有幂等守卫，脚本可重复执行。
    assert rev54.count("information_schema.COLUMNS") == 6
    assert rev54.count("PREPARE stmt FROM @sql;") == 8

    rev53_columns = create_columns(rev53, "t_ai_autonomous_run")
    baseline_columns = create_columns(schema, "t_ai_autonomous_run")
    orm_columns = set(
        db.metadata.tables["t_ai_autonomous_run"].columns.keys()
    )
    assert rev53_columns | added <= baseline_columns
    assert rev53_columns | added <= orm_columns

    run_ddl = re.search(
        r"CREATE TABLE `t_ai_autonomous_run` \((.*?)\)\s*ENGINE=",
        schema,
        re.DOTALL,
    )
    assert run_ddl, "orange.sql must define t_ai_autonomous_run"
    assert "`lease_owner` varchar(64) DEFAULT NULL" in run_ddl.group(1)
    assert "`lease_token` varchar(64) DEFAULT NULL" in run_ddl.group(1)
    assert "`lease_expires_at` datetime DEFAULT NULL" in run_ddl.group(1)
    assert "`heartbeat_at` datetime DEFAULT NULL" in run_ddl.group(1)
    assert (
        "`graph_version` varchar(32) NOT NULL DEFAULT 'v1'"
    ) in run_ddl.group(1)
    assert (
        "KEY `idx_ai_auto_run_lease_expires` (`lease_expires_at`)"
    ) in run_ddl.group(1)
    assert (
        "ADD KEY `idx_ai_auto_run_lease_expires` (`lease_expires_at`)"
    ) in rev54
    assert "`active_host_id` int GENERATED ALWAYS AS" in run_ddl.group(1)
    assert "UNIQUE KEY `uq_ai_auto_run_active_host` (`active_host_id`)" in (
        run_ddl.group(1)
    )
    assert "ADD UNIQUE KEY `uq_ai_auto_run_active_host`" in rev54
    assert "HAVING COUNT(*) > 1" in rev54

    # Generated-expression string literals must not inherit the caller's
    # connection charset. The old dump begins with SET NAMES utf8 (utf8mb3),
    # while migrations use utf8mb4; explicit introducers keep fresh and
    # upgraded information_schema expressions identical.
    for status in ("completed", "failed", "cancelled", "expired"):
        assert f"_utf8mb4'{status}'" in run_ddl.group(1)
        assert f"_utf8mb4''{status}''" in rev54


def test_autonomy_run_migrations_match_baseline_and_orm():
    """rev53+rev54+rev55+rev57、orange.sql 与 ORM 列集合一致。"""
    from app.core.db.database import db

    def migration_columns(path):
        text = (BACKEND / "mysqldir" / path).read_text(encoding="utf-8")
        return set(re.findall(r"ADD COLUMN `(\w+)`", text))

    rev53 = (
        BACKEND / "mysqldir" / "rev53_ai_autonomy_baseline.sql"
    ).read_text(encoding="utf-8")
    rev55 = (
        BACKEND / "mysqldir" / "rev55_ai_autonomy_custom_profile.sql"
    ).read_text(encoding="utf-8")
    schema = (BACKEND / "mysqldir" / "orange.sql").read_text(encoding="utf-8")

    def create_columns(source, table):
        match = re.search(
            r"CREATE TABLE(?: IF NOT EXISTS)? `" + table
            + r"` \((.*?)\)\s*ENGINE=",
            source,
            re.IGNORECASE | re.DOTALL,
        )
        assert match, f"{table} is missing from the schema source"
        return set(
            re.findall(r"^\s*`([^`]+)`\s+", match.group(1), re.MULTILINE)
        )

    added = set(re.findall(r"ADD COLUMN `(\w+)`", rev55))
    assert added == {"custom_profile_json"}, added
    # 每个 ALTER 都有幂等守卫，脚本可重复执行。
    assert rev55.count("information_schema.COLUMNS") == 1
    assert rev55.count("PREPARE stmt FROM @sql;") == 1

    rev53_columns = create_columns(rev53, "t_ai_autonomous_run")
    rev54_added = migration_columns("rev54_ai_autonomy_lease.sql")
    rev57_added = migration_columns("rev57_ai_ops_trigger.sql")
    assert rev57_added == {
        "trigger_type", "trigger_ref", "trigger_summary",
    }
    baseline_columns = create_columns(schema, "t_ai_autonomous_run")
    orm_columns = set(
        db.metadata.tables["t_ai_autonomous_run"].columns.keys()
    )
    assert (
        rev53_columns | rev54_added | added | rev57_added
        == baseline_columns == orm_columns
    )

    run_ddl = re.search(
        r"CREATE TABLE `t_ai_autonomous_run` \((.*?)\)\s*ENGINE=",
        schema,
        re.DOTALL,
    )
    assert run_ddl, "orange.sql must define t_ai_autonomous_run"
    assert "`custom_profile_json` text DEFAULT NULL" in run_ddl.group(1)
    assert "UNIQUE KEY `uq_ai_auto_run_trigger`" in run_ddl.group(1)

    rev57 = (
        BACKEND / "mysqldir" / "rev57_ai_ops_trigger.sql"
    ).read_text(encoding="utf-8")
    assert rev57.count("information_schema.COLUMNS") == 3
    assert rev57.count("information_schema.STATISTICS") == 1
    assert rev57.count("PREPARE stmt FROM @sql;") == 4


def test_rev56_autonomy_evidence_table_matches_baseline_and_orm():
    """M1/S3 切片 4：rev56 新增 Evidence 表后，orange.sql 与 ORM 一致。"""
    from app.core.db.database import db

    rev56 = (
        BACKEND / "mysqldir" / "rev56_ai_autonomy_evidence.sql"
    ).read_text(encoding="utf-8")
    schema = (BACKEND / "mysqldir" / "orange.sql").read_text(encoding="utf-8")

    def create_columns(source, table):
        match = re.search(
            r"CREATE TABLE(?: IF NOT EXISTS)? `" + table
            + r"` \((.*?)\)\s*ENGINE=",
            source,
            re.IGNORECASE | re.DOTALL,
        )
        assert match, f"{table} is missing from the schema source"
        return set(
            re.findall(r"^\s*`([^`]+)`\s+", match.group(1), re.MULTILINE)
        )

    # 守卫式 CREATE：表已存在时不重复建表，脚本可重复执行。
    assert rev56.count("information_schema.TABLES") == 1
    assert rev56.count("PREPARE stmt FROM @sql;") == 1
    assert "t_ai_autonomous_evidence" in rev56

    baseline_columns = create_columns(schema, "t_ai_autonomous_evidence")
    orm_columns = set(
        db.metadata.tables["t_ai_autonomous_evidence"].columns.keys()
    )
    migration_columns = create_columns(rev56, "t_ai_autonomous_evidence")
    assert baseline_columns == orm_columns == migration_columns

    evidence_ddl = re.search(
        r"CREATE TABLE `t_ai_autonomous_evidence` \((.*?)\)\s*ENGINE=",
        schema,
        re.DOTALL,
    )
    assert evidence_ddl, "orange.sql must define t_ai_autonomous_evidence"
    # Evidence 永远标记不可信：默认 0，无其它默认可意外置 1。
    assert "`trusted` tinyint(1) NOT NULL DEFAULT 0" in evidence_ddl.group(1)
    assert (
        "REFERENCES `t_ai_autonomous_run` (`id`) ON DELETE CASCADE"
        in evidence_ddl.group(1)
    )
    # 全新基线的 DROP 顺序必须先于父表之外引用它的表。
    assert schema.index(
        "DROP TABLE IF EXISTS `t_ai_autonomous_evidence`;"
    ) < schema.index("DROP TABLE IF EXISTS `t_ai_autonomous_run`;")


def test_rev58_knowledge_tables_match_fresh_schema_and_orm():
    from app.core.db.database import db

    migration = (
        BACKEND / "mysqldir" / "rev58_ai_knowledge.sql"
    ).read_text(encoding="utf-8")
    schema = (BACKEND / "mysqldir" / "orange.sql").read_text(encoding="utf-8")

    def columns(source, table):
        match = re.search(
            r"CREATE TABLE(?: IF NOT EXISTS)? `" + table
            + r"` \((.*?)\)\s*ENGINE=",
            source,
            re.IGNORECASE | re.DOTALL,
        )
        assert match, table
        return set(re.findall(r"^\s*`([^`]+)`\s+", match.group(1), re.MULTILINE))

    for table in ("t_ai_embedding_config", "t_ai_knowledge_document"):
        assert columns(migration, table) == columns(schema, table)
        assert columns(schema, table) == set(db.metadata.tables[table].columns.keys())
    assert migration.count("CREATE TABLE IF NOT EXISTS") == 2
    assert "INSERT IGNORE INTO `t_ai_embedding_config`" in migration
    assert "BAAI/bge-small-zh-v1.5" in migration
    assert "09b6e5dccb3cf9c17b68c4493ceb1cf6eb4c6980e8a429a8c3343d46932e75ec" in migration
    assert "`content` longtext NOT NULL" in migration


def test_dockerfile_pins_and_verifies_local_embedding_model():
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    requirements = (BACKEND / "requirements.txt").read_text(encoding="utf-8")
    assert "fastembed==0.8.0" in requirements
    assert "langchain-text-splitters==1.1.2" in requirements
    assert "fast-bge-small-zh-v1.5.tar.gz" in dockerfile
    assert "bf023219b6029148fddf764d248808816c0ca1f107f058231bb1ae0fa526f83f" in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "libgomp1" in dockerfile
    assert "OGS_AI_EMBEDDING_MODEL_PATH=/opt/orangeserver/models/fast-bge-small-zh-v1.5" in dockerfile
    assert "PIP_EXTRA_INDEX_URL" not in dockerfile
    compose = (REPO_ROOT / "deploy" / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    assert compose.count(
        "OGS_AI_EMBEDDING_MODEL_PATH: "
        "/opt/orangeserver/models/fast-bge-small-zh-v1.5"
    ) == 2


def test_dockerfile_builds_from_committed_requirements_without_resolving_lock():
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    assert "pip-compile" not in dockerfile
    assert "COPY backend/requirements.txt" in dockerfile
    assert "pip wheel" in dockerfile
    assert "FROM base AS runtime" in dockerfile
    assert "FROM node:22.22.1-alpine AS frontend-builder" in dockerfile
    assert "npm ci --no-audit --no-fund" in dockerfile
    assert "COPY --from=frontend-builder" in dockerfile
    assert "/app/.gunicorn" in dockerfile


def test_full_container_dev_is_isolated_and_source_mapped():
    compose = (DEPLOY / "docker-compose.dev.yml").read_text(encoding="utf-8")
    assert "host.docker.internal" not in compose
    assert "dev-mysql-data:/var/lib/mysql" in compose
    assert "dev-redis-data:/data" in compose
    assert "../backend:/app" in compose
    assert "../frontend:/app" in compose
    assert "--reload" in compose
    assert "npm run dev" in compose
    assert "VITE_API_TARGET: http://backend:28000" in compose
    assert "fetch('http://127.0.0.1:5173/')" in compose


def test_autonomy_dev_overlay_uses_dedicated_redis8_and_worker():
    overlay = (
        DEPLOY / "docker-compose.dev-autonomy.yml"
    ).read_text(encoding="utf-8")
    autonomy_redis = overlay.split(
        "  autonomy-redis:\n", 1
    )[1].split("\n  backend:\n", 1)[0]
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "redis:8.0-alpine" in overlay
    assert "dev-autonomy-redis-data:/data" in overlay
    assert '--appendonly\n      - "yes"' in autonomy_redis
    assert "--appendfsync\n      - everysec" in autonomy_redis
    assert "--maxmemory-policy\n      - noeviction" in autonomy_redis
    assert "command:\n      - redis-server" in autonomy_redis
    assert "exec redis-server" not in autonomy_redis
    assert "\n      - sh\n" not in autonomy_redis
    assert (
        "--requirepass\n"
        "      - ${OGS_AI_AUTONOMY_REDIS_PASSWORD:"
        "?Set OGS_AI_AUTONOMY_REDIS_PASSWORD in .env.dev}"
    ) in autonomy_redis
    assert "OGS_AI_AUTONOMY_REDIS_HOST: autonomy-redis" in overlay
    assert "OGS_AI_AUTONOMY_REDIS_PORT: 6379" in overlay
    assert overlay.count("OGS_AI_AUTONOMY_REDIS_PASSWORD: ${") == 3
    # Three service environments plus the redis-server command argument.
    assert overlay.count(":?Set OGS_AI_AUTONOMY_REDIS_PASSWORD") == 4
    assert overlay.count(
        "OGS_AI_AUTONOMY_ENABLED: ${OGS_AI_AUTONOMY_ENABLED:-false}"
    ) == 2
    assert "autonomy-worker:" in overlay
    image = (
        "${OGS_DEV_AUTONOMY_BACKEND_IMAGE:-orangeserver-autonomy-dev}:"
        "${OGS_DEV_AUTONOMY_BACKEND_TAG:-local}"
    )
    assert overlay.count("image: " + image) == 2
    assert overlay.count("context: ..") == 2
    assert overlay.count("dockerfile: backend/Dockerfile") == 2
    assert overlay.count("pull_policy: never") == 2
    assert "app.ai.autonomy.celery_entry:celery_app" in overlay
    assert "--concurrency=1" not in overlay
    assert "OGS_AUTONOMY_WORKER_CONCURRENCY: ${OGS_AUTONOMY_WORKER_CONCURRENCY:-2}" in overlay
    health_probe = (
        "from app.ai.autonomy.readiness import checkpoint_readiness, "
        "worker_readiness; raise SystemExit(0 if checkpoint_readiness() "
        "and worker_readiness() else 1)"
    )
    assert health_probe in overlay
    assert "inspect ping" not in overlay
    assert health_probe in (
        REPO_ROOT / "deploy" / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    assert health_probe in (
        REPO_ROOT / "deploy" / "docker-compose.s2-smoke.yml"
    ).read_text(encoding="utf-8")
    assert "../backend:/app" in overlay
    assert overlay.count("condition: service_healthy") == 4
    assert "docker-compose.dev-autonomy.yml" in makefile
    assert "docker-dev-autonomy-up:" in makefile
    assert "OGS_AI_AUTONOMY_ENABLED=true" in makefile
    assert "-u OGS_AI_AUTONOMY_ENABLED" in makefile
    assert "-u OGS_AI_AUTONOMY_REDIS_PASSWORD" in makefile
    assert "[ -z \"$$password\" ]" in makefile
    assert "make docker-dev-autonomy-ps" in makefile
    assert "--build" in makefile


def test_dev_env_is_generated_and_not_committed():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    init_script = (OPS / "init-dev-env.sh").read_text(encoding="utf-8")
    assert "docker-dev-init:" in makefile
    assert "docker-dev-reset:" in makefile
    assert "up -d --wait --wait-timeout 180" in makefile
    assert ".env.dev" in gitignore
    assert "umask 077" in init_script
    assert "OGS_AI_AUTONOMY_ENABLED=false" in init_script
    assert "OGS_AI_AUTONOMY_REDIS_PASSWORD=$(random_hex 24)" in init_script
