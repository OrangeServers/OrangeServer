"""Bounded read-only monitoring adapters for the existing chat Agent."""
from __future__ import annotations

import datetime
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlparse

from app.ai.autonomy.repository import sanitize_text
from app.ai.diagnostic_adapters import sanitize_evidence
from app.core.db.database import (
    db, t_ai_monitoring_host_mapping, t_ai_monitoring_source, t_host,
)
from app.tools.basesec import decrypt_secret, encrypt_secret


MAX_RESPONSE_BYTES = 256 * 1024
MAX_SERIES = 100
MAX_DISCOVERY_VALUES = 5000
MAX_SAMPLES = 1000
QUERY_TIMEOUT_SECONDS = 5
QUERY_BUDGET_SECONDS = 15
MAX_REQUESTS_PER_ANALYSIS = 12
MAX_TOTAL_RESPONSE_BYTES = 512 * 1024
MAX_SOURCES_PER_ANALYSIS = 100
LOOKBACK_MINUTES = frozenset({15, 60, 360, 1440})


class MonitoringError(RuntimeError):
    pass


class MonitoringValidationError(ValueError):
    pass


SOURCE_TYPES = frozenset({"prometheus", "grafana", "loki", "zabbix"})
LABEL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
METRIC_NAME_RE = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]{0,254}$")
GRAFANA_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SECRET_LABEL_RE = re.compile(
    r"(?:password|passwd|token|secret|authorization|cookie|api[_-]?key)", re.I,
)


def _safe_labels(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key)[:128]: sanitize_evidence(raw)[:255]
        for key, raw in list(value.items())[:20]
        if LABEL_NAME_RE.fullmatch(str(key)) and not SECRET_LABEL_RE.search(str(key))
    }


def _strict_bool(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise MonitoringValidationError(name + " must be boolean")


def _source_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlparse(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise MonitoringValidationError("invalid monitoring base URL")
    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        raise MonitoringValidationError("loopback monitoring URL is forbidden")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    ):
        raise MonitoringValidationError("unsafe monitoring destination")
    return text


def _external_ref(source_type: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MonitoringValidationError("external_ref must be an object")
    if source_type in {"prometheus", "loki"}:
        labels = value.get("labels")
        if not isinstance(labels, Mapping) or not 1 <= len(labels) <= 8:
            raise MonitoringValidationError("external_ref labels are required")
        normalized = {}
        for key, raw in labels.items():
            if not isinstance(key, str) or not LABEL_NAME_RE.fullmatch(key):
                raise MonitoringValidationError("external_ref label is invalid")
            text = sanitize_text(str(raw)).strip()
            if not text or len(text) > 255:
                raise MonitoringValidationError("external_ref label value is invalid")
            normalized[key] = text
        return {"labels": normalized}
    if source_type == "grafana":
        uid = sanitize_text(str(value.get("datasource_uid") or "")).strip()
        dashboard_uid = sanitize_text(
            str(value.get("dashboard_uid") or "")
        ).strip()
        datasource_type = str(value.get("datasource_type") or "").strip()
        if (
            not GRAFANA_UID_RE.fullmatch(uid)
            or not GRAFANA_UID_RE.fullmatch(dashboard_uid)
        ):
            raise MonitoringValidationError("Grafana UID is invalid")
        if datasource_type not in {"prometheus", "loki"}:
            raise MonitoringValidationError("Grafana datasource type is invalid")
        labels = _external_ref(datasource_type, {"labels": value.get("labels")})
        return {
            "datasource_uid": uid,
            "datasource_type": datasource_type,
            "dashboard_uid": dashboard_uid,
            "labels": labels["labels"],
        }
    if source_type == "zabbix":
        hostid = str(value.get("hostid") or "")
        if not hostid.isdigit() or len(hostid) > 20:
            raise MonitoringValidationError("Zabbix hostid is invalid")
        return {"hostid": hostid}
    raise MonitoringValidationError("unsupported monitoring source type")


class MonitoringConfigService:
    def __init__(self, session=None):
        self.session = session or db.session

    @staticmethod
    def _source_dict(row: t_ai_monitoring_source) -> dict[str, Any]:
        return {
            "id": int(row.id),
            "name": str(row.name),
            "source_type": str(row.source_type),
            "base_url": str(row.base_url),
            "token_configured": bool(row.token_ciphertext),
            "verify_tls": bool(row.verify_tls),
            "enabled": bool(row.enabled),
            "created_by": str(row.created_by),
        }

    def list_sources(self) -> list[dict[str, Any]]:
        return [
            self._source_dict(row)
            for row in self.session.query(t_ai_monitoring_source)
            .order_by(t_ai_monitoring_source.id.asc()).all()
        ]

    def get_source(self, source_id: int) -> t_ai_monitoring_source:
        row = self.session.query(t_ai_monitoring_source).filter_by(
            id=int(source_id),
        ).first()
        if row is None:
            raise MonitoringValidationError("monitoring source not found")
        return row

    def create_source(self, actor: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        name = sanitize_text(str(payload.get("name") or "")).strip()
        source_type = str(payload.get("source_type") or "").strip().lower()
        if not name or len(name) > 64:
            raise MonitoringValidationError("name must contain 1 to 64 characters")
        if source_type not in SOURCE_TYPES:
            raise MonitoringValidationError("unsupported monitoring source type")
        if self.session.query(t_ai_monitoring_source).filter_by(name=name).first():
            raise MonitoringValidationError("monitoring source name already exists")
        token = payload.get("token")
        if token is not None and (not isinstance(token, str) or len(token) > 1024):
            raise MonitoringValidationError("invalid monitoring token")
        row = t_ai_monitoring_source(
            name=name,
            source_type=source_type,
            base_url=_source_url(payload.get("base_url")),
            token_ciphertext=encrypt_secret(token) if token else None,
            verify_tls=_strict_bool(payload.get("verify_tls"), "verify_tls", True),
            enabled=_strict_bool(payload.get("enabled"), "enabled", True),
            created_by=sanitize_text(str(actor or ""))[:24],
        )
        self.session.add(row)
        self.session.commit()
        return self._source_dict(row)

    def update_source(
        self, source_id: int, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        row = self.get_source(source_id)
        if "name" in payload:
            name = sanitize_text(str(payload.get("name") or "")).strip()
            if not name or len(name) > 64:
                raise MonitoringValidationError("name must contain 1 to 64 characters")
            clash = self.session.query(t_ai_monitoring_source).filter(
                t_ai_monitoring_source.name == name,
                t_ai_monitoring_source.id != row.id,
            ).first()
            if clash:
                raise MonitoringValidationError("monitoring source name already exists")
            row.name = name
        if "base_url" in payload:
            row.base_url = _source_url(payload.get("base_url"))
        if "verify_tls" in payload:
            row.verify_tls = _strict_bool(payload.get("verify_tls"), "verify_tls", True)
        if "enabled" in payload:
            row.enabled = _strict_bool(payload.get("enabled"), "enabled", True)
        if payload.get("clear_token") is True:
            row.token_ciphertext = None
        elif "token" in payload and payload.get("token"):
            token = payload.get("token")
            if not isinstance(token, str) or len(token) > 1024:
                raise MonitoringValidationError("invalid monitoring token")
            row.token_ciphertext = encrypt_secret(token)
        self.session.commit()
        return self._source_dict(row)

    def delete_source(self, source_id: int) -> None:
        self.session.delete(self.get_source(source_id))
        self.session.commit()

    def save_mapping(
        self, actor: str, source_id: int, host_id: int,
        external_ref: Mapping[str, Any],
    ) -> dict[str, Any]:
        source = self.session.query(t_ai_monitoring_source).filter_by(
            id=int(source_id),
        ).first()
        if source is None:
            raise MonitoringValidationError("monitoring source not found")
        external_ref = _external_ref(source.source_type, external_ref)
        encoded = json.dumps(
            dict(external_ref), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > 4096:
            raise MonitoringValidationError("external_ref is too large")
        row = self.session.query(t_ai_monitoring_host_mapping).filter_by(
            source_id=int(source_id), host_id=int(host_id),
        ).first()
        if row is None:
            row = t_ai_monitoring_host_mapping(
                source_id=int(source_id), host_id=int(host_id),
                external_ref_json=encoded,
                confirmed_by=sanitize_text(str(actor or ""))[:24],
            )
            self.session.add(row)
        else:
            row.external_ref_json = encoded
            row.confirmed_by = sanitize_text(str(actor or ""))[:24]
        self.session.commit()
        return {
            "id": int(row.id),
            "source_id": int(row.source_id),
            "host_id": int(row.host_id),
            "external_ref": json.loads(row.external_ref_json),
            "confirmed_by": str(row.confirmed_by),
        }

    def list_mappings(self, host_id: int | None = None) -> list[dict[str, Any]]:
        query = self.session.query(t_ai_monitoring_host_mapping)
        if host_id is not None:
            query = query.filter_by(host_id=int(host_id))
        return [{
            "id": int(row.id),
            "source_id": int(row.source_id),
            "host_id": int(row.host_id),
            "external_ref": json.loads(row.external_ref_json),
            "confirmed_by": str(row.confirmed_by),
        } for row in query.order_by(t_ai_monitoring_host_mapping.id.asc()).all()]

    def sources_for_host(self, host_id: int) -> list[dict[str, Any]]:
        rows = (
            self.session.query(t_ai_monitoring_source)
            .join(
                t_ai_monitoring_host_mapping,
                t_ai_monitoring_host_mapping.source_id == t_ai_monitoring_source.id,
            )
            .filter(
                t_ai_monitoring_source.enabled.is_(True),
                t_ai_monitoring_host_mapping.host_id == int(host_id),
            )
            .order_by(t_ai_monitoring_source.id.asc())
            .all()
        )
        return [{
            "id": int(row.id),
            "name": str(row.name),
            "source_type": str(row.source_type),
        } for row in rows]

    def test_source(self, source_id: int, adapter=None) -> dict[str, Any]:
        adapter = adapter or MonitoringAdapter()
        return adapter.test_connection(self.runtime_source(self.get_source(source_id)))

    def discover(self, source_id: int, host_id: int, adapter=None) -> list[dict[str, Any]]:
        source = self.get_source(source_id)
        host = self.session.query(t_host).filter_by(
            id=int(host_id), is_deleted=False,
        ).first()
        if host is None:
            raise MonitoringValidationError("host not found")
        adapter = adapter or MonitoringAdapter()
        return adapter.discover(
            self.runtime_source(source),
            {"id": int(host.id), "alias": str(host.alias), "ip": str(host.host_ip)},
        )

    @staticmethod
    def runtime_source(row: t_ai_monitoring_source) -> dict[str, Any]:
        token = ""
        if row.token_ciphertext:
            token = str(decrypt_secret(row.token_ciphertext) or "")
        return {
            **MonitoringConfigService._source_dict(row),
            "token": token,
        }


class MonitoringAnalysisService:
    def __init__(self, session, platform, adapter: MonitoringAdapter | None = None):
        self.session = session
        self.platform = platform
        self.adapter = adapter or MonitoringAdapter()

    def _mapped_sources(
        self, host_id: int, *, selected: Any = None, source_id: int | None = None,
    ) -> list[tuple[Any, Any]]:
        host_id = int(host_id)
        if not self.platform.validate_asset_ids([host_id]):
            raise MonitoringValidationError("host_id is not authorized")
        query = (
            self.session.query(t_ai_monitoring_source, t_ai_monitoring_host_mapping)
            .join(
                t_ai_monitoring_host_mapping,
                t_ai_monitoring_host_mapping.source_id == t_ai_monitoring_source.id,
            )
            .filter(
                t_ai_monitoring_source.enabled.is_(True),
                t_ai_monitoring_host_mapping.host_id == host_id,
            )
        )
        if selected:
            query = query.filter(t_ai_monitoring_source.source_type.in_(selected))
        if source_id is not None:
            query = query.filter(t_ai_monitoring_source.id == int(source_id))
        rows = query.order_by(t_ai_monitoring_source.id.asc()).limit(
            MAX_SOURCES_PER_ANALYSIS + 1,
        ).all()
        if not rows:
            raise MonitoringValidationError(
                "asset has no enabled confirmed monitoring mapping"
            )
        if len(rows) > MAX_SOURCES_PER_ANALYSIS:
            raise MonitoringValidationError("asset has too many monitoring mappings")
        return rows

    def discover(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        host_id = int(arguments["host_id"])
        rows = self._mapped_sources(
            host_id, selected=arguments.get("source_types"),
        )
        observations = []
        failed = 0
        for source_row, mapping_row in rows:
            public_source = MonitoringConfigService._source_dict(source_row)
            try:
                observation = self.adapter.discover_capabilities(
                    source=MonitoringConfigService.runtime_source(source_row),
                    mapping=json.loads(mapping_row.external_ref_json),
                    search=str(arguments.get("search") or "").strip(),
                )
                observations.append(observation)
            except MonitoringError as exc:
                failed += 1
                observations.append({
                    "source_id": int(source_row.id),
                    "source_name": public_source["name"],
                    "source_type": public_source["source_type"],
                    "status": "failed",
                    "error": sanitize_text(str(exc))[:256],
                    "observations": [],
                })
        return {
            "kind": "monitoring_catalog",
            "rows": observations,
            "summary": {
                "title": "monitoring_catalog",
                "total": len(observations),
                "source_count": len(observations),
                "succeeded": len(observations) - failed,
                "failed": failed,
                "partial": 0 < failed < len(observations),
            },
        }

    def query(self, operation: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        expected_types = {
            "query_prometheus": {"prometheus"},
            "query_loki": {"loki"},
            "query_grafana_panel": {"grafana"},
            "query_zabbix_history": {"zabbix"},
        }
        if operation not in expected_types:
            raise MonitoringValidationError("unsupported monitoring operation")
        host_id = int(arguments["host_id"])
        rows = self._mapped_sources(
            host_id, source_id=int(arguments["source_id"]),
        )
        source_row, mapping_row = rows[0]
        if str(source_row.source_type) not in expected_types[operation]:
            raise MonitoringValidationError("monitoring source type does not match tool")
        source = MonitoringConfigService.runtime_source(source_row)
        mapping = json.loads(mapping_row.external_ref_json)
        methods = {
            "query_prometheus": self.adapter.query_prometheus,
            "query_loki": self.adapter.query_loki,
            "query_grafana_panel": self.adapter.query_grafana_panel,
            "query_zabbix_history": self.adapter.query_zabbix_history,
        }
        observation = methods[operation](
            source=source, mapping=mapping, arguments=arguments,
        )
        if not _observations_have_data(observation.get("observations")):
            observation["status"] = "no_data"
        return {
            "kind": "monitoring_observation",
            "rows": [observation],
            "summary": {
                "title": "monitoring_observation",
                "total": 1,
                "source_count": 1,
                "succeeded": 1,
                "no_data": int(observation["status"] == "no_data"),
                "failed": 0,
                "partial": False,
            },
        }


def _observations_have_data(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, Mapping)
        and any(_positive_count(item.get(key)) for key in (
            "sample_count", "line_count", "count",
        ))
        for item in value
    )


def _positive_count(value: Any) -> bool:
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _escape_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _selector(mapping: Mapping[str, Any]) -> str:
    labels = mapping.get("labels") or {}
    if not isinstance(labels, Mapping) or not labels:
        raise MonitoringError("monitoring mapping has no labels")
    items = []
    for key in sorted(labels):
        if not isinstance(key, str) or not LABEL_NAME_RE.fullmatch(key):
            raise MonitoringError("monitoring mapping contains an invalid label")
        value = sanitize_text(str(labels[key])).strip()
        if not value or len(value) > 255:
            raise MonitoringError("monitoring mapping contains an invalid value")
        items.append('%s="%s"' % (key, _escape_label(value)))
    return "{" + ",".join(items) + "}"


def _bounded_json_value(response: Any) -> Any:
    try:
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > MAX_RESPONSE_BYTES:
            raise MonitoringError("monitoring response is too large")
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=16 * 1024):
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise MonitoringError("monitoring response is too large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MonitoringError("monitoring response is invalid") from exc
    finally:
        if hasattr(response, "close"):
            response.close()


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float):
        super().__init__(hostname, port, timeout=timeout)
        self.address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self.address, self.port), self.timeout, self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self, hostname: str, address: str, port: int,
        timeout: float, context: ssl.SSLContext,
    ):
        super().__init__(hostname, port, timeout=timeout, context=context)
        self.address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self.address, self.port), self.timeout, self.source_address,
        )
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


class _PinnedResponse:
    def __init__(self, response: Any, connection: Any):
        self.response = response
        self.connection = connection
        self.headers = response.headers
        self.closed = False

    def raise_for_status(self) -> None:
        if int(self.response.status) >= 400:
            raise MonitoringError("monitoring request failed")

    def iter_content(self, chunk_size: int):
        try:
            while True:
                chunk = self.response.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.response.close()
            self.connection.close()


class MonitoringAdapter:
    def __init__(
        self,
        request: Callable[..., Any] | None = None,
        resolver: Callable[..., Any] = socket.getaddrinfo,
    ):
        self.request = request
        self.resolver = resolver
        self.started_at = time.monotonic()
        self.request_count = 0
        self.response_bytes = 0

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        remaining = QUERY_BUDGET_SECONDS - (time.monotonic() - self.started_at)
        if self.request_count >= MAX_REQUESTS_PER_ANALYSIS or remaining <= 0:
            raise MonitoringError("monitoring request budget exhausted")
        self.request_count += 1
        kwargs["timeout"] = min(float(kwargs.get("timeout", remaining)), remaining)
        parsed = urlparse(url)
        try:
            addresses = self.resolver(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise MonitoringError("monitoring destination cannot be resolved") from exc
        if not addresses:
            raise MonitoringError("monitoring destination cannot be resolved")
        for address_info in addresses:
            try:
                address = ipaddress.ip_address(str(address_info[4][0]).split("%", 1)[0])
            except (IndexError, ValueError, TypeError) as exc:
                raise MonitoringError("monitoring destination cannot be resolved") from exc
            if (
                address.is_loopback
                or address.is_link_local
                or address.is_unspecified
                or address.is_multicast
            ):
                raise MonitoringError("unsafe monitoring destination")
        if self.request is not None:
            return self.request(method, url, **kwargs)
        return self._pinned_request(method, url, str(addresses[0][4][0]), **kwargs)

    @staticmethod
    def _pinned_request(method: str, url: str, address: str, **kwargs: Any) -> Any:
        parsed = urlparse(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        headers = dict(kwargs.get("headers") or {})
        headers["Host"] = parsed.netloc
        path = parsed.path or "/"
        query = urlencode(kwargs.get("params") or {}, doseq=True)
        if query:
            path += "?" + query
        body = None
        if kwargs.get("json") is not None:
            body = json.dumps(
                kwargs["json"], ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
        timeout = float(kwargs.get("timeout", QUERY_TIMEOUT_SECONDS))
        if parsed.scheme == "https":
            verify = bool(kwargs.get("verify", True))
            context = (
                ssl.create_default_context()
                if verify else ssl._create_unverified_context()
            )
            connection = _PinnedHTTPSConnection(
                str(parsed.hostname), address, port, timeout, context,
            )
        else:
            connection = _PinnedHTTPConnection(
                str(parsed.hostname), address, port, timeout,
            )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
        except Exception:
            connection.close()
            raise
        return _PinnedResponse(response, connection)

    def _json_value(self, response: Any) -> Any:
        payload = _bounded_json_value(response)
        self.response_bytes += len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if self.response_bytes > MAX_TOTAL_RESPONSE_BYTES:
            raise MonitoringError("monitoring response budget exhausted")
        return payload

    def _json(self, response: Any) -> Mapping[str, Any]:
        payload = self._json_value(response)
        if not isinstance(payload, Mapping):
            raise MonitoringError("monitoring response is invalid")
        return payload

    def test_connection(self, source: Mapping[str, Any]) -> dict[str, Any]:
        source_type = str(source.get("source_type") or "")
        if source_type == "prometheus":
            payload = self._http_json(source, "GET", "/api/v1/status/buildinfo")
            if not isinstance(payload, Mapping) or payload.get("status") != "success":
                raise MonitoringError("Prometheus connection test failed")
            version = ((payload.get("data") or {}).get("version") or "")
        elif source_type == "loki":
            payload = self._http_json(source, "GET", "/loki/api/v1/labels")
            if not isinstance(payload, Mapping) or payload.get("status") != "success":
                raise MonitoringError("Loki connection test failed")
            version = ""
        elif source_type == "grafana":
            payload = self._http_json(source, "GET", "/api/health")
            if not isinstance(payload, Mapping) or str(payload.get("database") or "").lower() != "ok":
                raise MonitoringError("Grafana connection test failed")
            ping = self._http_json(source, "GET", "/api/login/ping")
            if not isinstance(ping, Mapping) or str(ping.get("message") or "").lower() != "logged in":
                raise MonitoringError("Grafana authentication test failed")
            datasources = self._http_json(source, "GET", "/api/datasources")
            if not isinstance(datasources, list) or len(datasources) > 5000:
                raise MonitoringError("Grafana datasource test failed")
            query_verified = False
            for datasource in datasources[:8]:
                if not isinstance(datasource, Mapping):
                    continue
                uid = str(datasource.get("uid") or "")
                datasource_type = str(datasource.get("type") or "")
                if not GRAFANA_UID_RE.fullmatch(uid):
                    continue
                if datasource_type == "prometheus":
                    suffix = "/api/v1/labels"
                elif datasource_type == "loki":
                    suffix = "/loki/api/v1/labels"
                else:
                    continue
                probe = self._http_json(
                    source, "GET",
                    "/api/datasources/proxy/uid/%s%s" % (uid, suffix),
                )
                if isinstance(probe, Mapping) and probe.get("status") == "success":
                    query_verified = True
                    break
            if not query_verified:
                raise MonitoringError("Grafana datasource query test failed")
            version = payload.get("version") or ""
        elif source_type == "zabbix":
            payload = self._zabbix_rpc(source, 1, "apiinfo.version", {})
            if not isinstance(payload, str) or not payload:
                raise MonitoringError("Zabbix connection test failed")
            self._zabbix_call(
                source, 2, "host.get", {"output": ["hostid"], "limit": 1},
            )
            version = payload
        else:
            raise MonitoringError("unsupported monitoring source")
        return {
            "ok": True,
            "source_type": source_type,
            "version": sanitize_text(str(version))[:64],
        }

    def discover(
        self, source: Mapping[str, Any], host: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        source_type = str(source.get("source_type") or "")
        alias = sanitize_text(str(host.get("alias") or "")).strip()
        address = sanitize_text(str(host.get("ip") or "")).strip()
        if source_type == "prometheus":
            payload = self._http_json(
                source, "GET", "/api/v1/label/instance/values",
            )
            values = payload.get("data") if isinstance(payload, Mapping) else None
            return self._label_candidates(values, alias, address, "instance")
        if source_type == "loki":
            payload = self._http_json(
                source, "GET", "/loki/api/v1/label/host/values",
            )
            values = payload.get("data") if isinstance(payload, Mapping) else None
            return self._label_candidates(values, alias, address, "host")
        if source_type == "grafana":
            datasources = self._http_json(source, "GET", "/api/datasources")
            if not isinstance(datasources, list) or len(datasources) > 5000:
                raise MonitoringError("Grafana discovery failed")
            candidates = []
            for datasource in datasources[:8]:
                if not isinstance(datasource, Mapping):
                    continue
                datasource_type = str(datasource.get("type") or "")
                uid = sanitize_text(str(datasource.get("uid") or "")).strip()
                if (
                    datasource_type not in {"prometheus", "loki"}
                    or not GRAFANA_UID_RE.fullmatch(uid)
                ):
                    continue
                if datasource_type == "prometheus":
                    suffix = "/api/v1/label/instance/values"
                    label_name = "instance"
                else:
                    suffix = "/loki/api/v1/label/host/values"
                    label_name = "host"
                payload = self._http_json(
                    source, "GET", "/api/datasources/proxy/uid/%s%s" % (uid, suffix),
                )
                values = payload.get("data") if isinstance(payload, Mapping) else None
                for candidate in self._label_candidates(
                    values, alias, address, label_name,
                ):
                    candidate["source_name"] = sanitize_text(
                        str(datasource.get("name") or uid)
                    )[:128]
                    candidate["external_ref"].update({
                        "datasource_uid": uid,
                        "datasource_type": datasource_type,
                    })
                    candidates.append(candidate)
                    if len(candidates) == 20:
                        return candidates
            return candidates
        if source_type == "zabbix":
            values = self._zabbix_rpc(source, 1, "host.get", {
                "output": ["hostid", "host", "name"],
                "selectInterfaces": ["ip", "dns"],
                "monitored_hosts": True,
                "limit": 100,
            })
            if not isinstance(values, list) or len(values) > 100:
                raise MonitoringError("Zabbix discovery failed")
            candidates = []
            needles = {alias.lower(), address.lower()} - {""}
            for item in values:
                if not isinstance(item, Mapping):
                    continue
                haystack = {
                    str(item.get("host") or "").lower(),
                    str(item.get("name") or "").lower(),
                }
                for interface in item.get("interfaces") or []:
                    if isinstance(interface, Mapping):
                        haystack.update({
                            str(interface.get("ip") or "").lower(),
                            str(interface.get("dns") or "").lower(),
                        })
                if needles and not any(
                    needle == candidate or needle in candidate
                    for needle in needles for candidate in haystack if candidate
                ):
                    continue
                hostid = str(item.get("hostid") or "")
                if hostid.isdigit():
                    candidates.append({
                        "label": sanitize_text(str(item.get("name") or item.get("host") or hostid))[:128],
                        "match": "host",
                        "external_ref": {"hostid": hostid},
                    })
            return candidates[:20]
        raise MonitoringError("unsupported monitoring source")

    def _http_json(
        self, source: Mapping[str, Any], method: str, path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._request(
                method,
                str(source.get("base_url") or "").rstrip("/") + path,
                params=dict(params or {}),
                headers=self._headers(source),
                timeout=QUERY_TIMEOUT_SECONDS,
                verify=bool(source.get("verify_tls", True)),
                stream=True,
                allow_redirects=False,
            )
            return self._json_value(response)
        except MonitoringError:
            raise
        except Exception as exc:
            raise MonitoringError("monitoring request failed") from exc

    @staticmethod
    def _label_candidates(
        values: Any, alias: str, address: str, label_name: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(values, list) or len(values) > 1000:
            raise MonitoringError("monitoring discovery failed")
        needles = {alias.lower(), address.lower()} - {""}
        rows = []
        for raw in values:
            value = sanitize_text(str(raw)).strip()
            lowered = value.lower()
            if not value or len(value) > 255:
                continue
            if needles and not any(
                needle == lowered or needle in lowered for needle in needles
            ):
                continue
            rows.append({
                "label": value,
                "match": label_name,
                "external_ref": {"labels": {label_name: value}},
            })
            if len(rows) == 20:
                break
        return rows

    def discover_capabilities(
        self, *, source: Mapping[str, Any], mapping: Mapping[str, Any], search: str,
    ) -> dict[str, Any]:
        """Return bounded source-native choices; the model decides what to query."""
        source_type = str(source.get("source_type") or "")
        base = {
            "source_id": int(source["id"]),
            "source_name": sanitize_text(str(source.get("name") or ""))[:64],
            "source_type": source_type,
            "status": "ok",
        }
        needle = sanitize_text(search).strip().lower()[:128]
        if source_type in {"prometheus", "loki"}:
            end = datetime.datetime.now(datetime.timezone.utc)
            start = end - datetime.timedelta(hours=1)
            if source_type == "prometheus":
                payload = self._http_json(
                    source, "GET", "/api/v1/label/__name__/values",
                    params={
                        "match[]": _selector(mapping),
                        "start": start.timestamp(), "end": end.timestamp(),
                    },
                )
                values = payload.get("data") if isinstance(payload, Mapping) else None
                if not isinstance(values, list) or len(values) > MAX_DISCOVERY_VALUES:
                    raise MonitoringError("Prometheus metric discovery is too large")
                metrics = sorted({
                    str(value) for value in values
                    if METRIC_NAME_RE.fullmatch(str(value))
                    and (not needle or needle in str(value).lower())
                })
                return {
                    **base, "metrics": metrics[:200],
                    "metric_count": len(metrics), "truncated": len(metrics) > 200,
                }
            payload = self._http_json(
                source, "GET", "/loki/api/v1/series",
                params={
                    "match[]": _selector(mapping),
                    "start": str(int(start.timestamp() * 1_000_000_000)),
                    "end": str(int(end.timestamp() * 1_000_000_000)),
                },
            )
            series = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(series, list) or len(series) > MAX_DISCOVERY_VALUES:
                raise MonitoringError("Loki label discovery is too large")
            labels: dict[str, set[str]] = {}
            for item in series:
                if not isinstance(item, Mapping):
                    continue
                for key, value in item.items():
                    if LABEL_NAME_RE.fullmatch(str(key)):
                        labels.setdefault(str(key), set()).add(
                            sanitize_evidence(value)[:255]
                        )
            return {
                **base,
                "labels": {
                    key: sorted(value for value in values if not needle or needle in value.lower())[:20]
                    for key, values in sorted(labels.items())[:20]
                },
            }
        if source_type == "grafana":
            dashboard_uid = str(mapping.get("dashboard_uid") or "")
            if not GRAFANA_UID_RE.fullmatch(dashboard_uid):
                raise MonitoringError("Grafana mapping is invalid")
            payload = self._http_json(
                source, "GET", "/api/dashboards/uid/" + dashboard_uid,
            )
            dashboard = payload.get("dashboard") if isinstance(payload, Mapping) else None
            if not isinstance(dashboard, Mapping):
                raise MonitoringError("Grafana dashboard response is invalid")
            panels = []
            pending = list(dashboard.get("panels") or [])
            while pending and len(panels) < 100:
                panel = pending.pop(0)
                if not isinstance(panel, Mapping):
                    continue
                if isinstance(panel.get("panels"), list):
                    pending[0:0] = panel["panels"]
                title = sanitize_text(str(panel.get("title") or ""))[:128]
                panel_id = panel.get("id")
                if (
                    isinstance(panel_id, int)
                    and panel_id > 0
                    and (not needle or needle in title.lower())
                    and any(isinstance(target, Mapping) and not target.get("hide")
                            for target in panel.get("targets") or [])
                ):
                    panels.append({"panel_id": panel_id, "title": title})
            return {
                **base, "dashboard_uid": dashboard_uid,
                "dashboard_title": sanitize_text(str(dashboard.get("title") or ""))[:128],
                "panels": panels,
            }
        if source_type == "zabbix":
            hostid = str(mapping.get("hostid") or "")
            if not hostid.isdigit():
                raise MonitoringError("Zabbix mapping has no valid hostid")
            params: dict[str, Any] = {
                "output": [
                    "itemid", "name", "key_", "lastvalue", "lastclock",
                    "units", "value_type",
                ],
                "hostids": [hostid], "monitored": True,
                "sortfield": "name", "limit": 100,
            }
            if needle:
                params.update({"search": {"name": search[:128]}, "searchWildcardsEnabled": True})
            items = self._zabbix_call(source, 1, "item.get", params)
            problems = self._zabbix_call(source, 2, "problem.get", {
                "output": ["eventid", "name", "severity", "clock", "acknowledged"],
                "hostids": [hostid], "recent": True,
                "sortfield": ["eventid"], "sortorder": "DESC", "limit": 100,
            })
            return {
                **base,
                "items": [self._select_fields(item, (
                    "itemid", "name", "key_", "lastvalue", "lastclock", "units",
                    "value_type",
                )) for item in items],
                "problems": [self._select_fields(item, (
                    "eventid", "name", "severity", "clock", "acknowledged",
                )) for item in problems],
            }
        raise MonitoringError("unsupported monitoring source")

    def query_prometheus(
        self, *, source: Mapping[str, Any], mapping: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        metric = str(arguments.get("metric") or "")
        calculation = str(arguments.get("calculation") or "raw")
        aggregation = str(arguments.get("aggregation") or "none")
        range_minutes = int(arguments.get("range_minutes", 5))
        lookback = int(arguments.get("lookback_minutes", 60))
        step = int(arguments.get("step_seconds", 30))
        group_by = arguments.get("group_by") or []
        if not METRIC_NAME_RE.fullmatch(metric):
            raise MonitoringError("invalid Prometheus metric")
        if calculation not in {
            "raw", "rate", "irate", "increase", "delta", "avg_over_time",
            "min_over_time", "max_over_time", "sum_over_time",
        } or aggregation not in {"none", "sum", "avg", "min", "max", "count"}:
            raise MonitoringError("unsupported Prometheus calculation")
        if range_minutes not in {1, 5, 15, 60} or lookback not in LOOKBACK_MINUTES:
            raise MonitoringError("unsupported Prometheus time range")
        if step not in {15, 30, 60, 300} or lookback * 60 // step > MAX_SAMPLES:
            raise MonitoringError("Prometheus step would exceed sample budget")
        if (
            not isinstance(group_by, list) or len(group_by) > 4
            or len(set(group_by)) != len(group_by)
            or any(not isinstance(key, str) or not LABEL_NAME_RE.fullmatch(key)
                   for key in group_by)
        ):
            raise MonitoringError("invalid Prometheus group_by")
        expression = metric + _selector(mapping)
        if calculation != "raw":
            expression = "%s(%s[%sm])" % (calculation, expression, range_minutes)
        if aggregation != "none":
            suffix = " by (%s)" % ",".join(group_by) if group_by else ""
            expression = "%s%s (%s)" % (aggregation, suffix, expression)
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(minutes=lookback)
        payload = self._http_json(source, "GET", "/api/v1/query_range", params={
            "query": expression, "start": start.timestamp(),
            "end": end.timestamp(), "step": step, "timeout": "5s",
        })
        if payload.get("status") != "success":
            raise MonitoringError("Prometheus query failed")
        result = (payload.get("data") or {}).get("result")
        series, sample_count = self._prometheus_series(result)
        return {
            "source_id": int(source["id"]),
            "source_name": sanitize_text(str(source.get("name") or ""))[:64],
            "source_type": "prometheus", "status": "ok",
            "lookback_minutes": lookback,
            "observations": [{
                "template": "prometheus_query", "query": expression,
                "step_seconds": step, "sample_count": sample_count, "series": series,
            }],
        }

    @staticmethod
    def _prometheus_series(result: Any) -> tuple[list[dict[str, Any]], int]:
        if not isinstance(result, list) or len(result) > MAX_SERIES:
            raise MonitoringError("Prometheus result is invalid")
        rows = []
        total = 0
        for raw_series in result:
            if not isinstance(raw_series, Mapping):
                raise MonitoringError("Prometheus result is invalid")
            raw_samples = raw_series.get("values") or []
            if not isinstance(raw_samples, list):
                raise MonitoringError("Prometheus result is invalid")
            samples = []
            for raw in raw_samples:
                if not isinstance(raw, list) or len(raw) != 2:
                    raise MonitoringError("Prometheus sample is invalid")
                try:
                    timestamp, value = float(raw[0]), float(raw[1])
                except (TypeError, ValueError) as exc:
                    raise MonitoringError("Prometheus sample is invalid") from exc
                if not math.isfinite(timestamp) or not math.isfinite(value):
                    raise MonitoringError("Prometheus sample is invalid")
                samples.append([timestamp, value])
                total += 1
                if total > MAX_SAMPLES:
                    raise MonitoringError("Prometheus sample limit exceeded")
            rows.append({
                "labels": _safe_labels(raw_series.get("metric")),
                "samples": samples,
            })
        return rows, total

    def query_loki(
        self, *, source: Mapping[str, Any], mapping: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        contains = sanitize_text(str(arguments.get("contains") or "")).strip()
        lookback = int(arguments.get("lookback_minutes", 60))
        limit = int(arguments.get("limit", 100))
        regex = bool(arguments.get("regex", False))
        if not contains or len(contains) > 256 or lookback not in LOOKBACK_MINUTES:
            raise MonitoringError("invalid Loki query")
        if limit not in {20, 50, 100}:
            raise MonitoringError("unsupported Loki line limit")
        escaped = contains.replace("\\", "\\\\").replace('"', '\\"')
        query = "%s %s \"%s\"" % (_selector(mapping), "|~" if regex else "|=", escaped)
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(minutes=lookback)
        payload = self._http_json(source, "GET", "/loki/api/v1/query_range", params={
            "query": query,
            "start": str(int(start.timestamp() * 1_000_000_000)),
            "end": str(int(end.timestamp() * 1_000_000_000)),
            "limit": limit, "direction": "backward",
        })
        if payload.get("status") != "success":
            raise MonitoringError("Loki query failed")
        result = (payload.get("data") or {}).get("result")
        if not isinstance(result, list) or len(result) > MAX_SERIES:
            raise MonitoringError("Loki result is invalid")
        items = []
        for stream in result:
            if not isinstance(stream, Mapping) or not isinstance(stream.get("values"), list):
                raise MonitoringError("Loki result is invalid")
            safe_labels = _safe_labels(stream.get("stream"))
            for value in stream["values"]:
                if not isinstance(value, list) or len(value) != 2:
                    raise MonitoringError("Loki log line is invalid")
                items.append({
                    "timestamp": sanitize_text(str(value[0]))[:32],
                    "line": sanitize_evidence(value[1])[:2048],
                    "labels": safe_labels,
                })
                if len(items) > limit:
                    raise MonitoringError("Loki line limit exceeded")
        return {
            "source_id": int(source["id"]),
            "source_name": sanitize_text(str(source.get("name") or ""))[:64],
            "source_type": "loki", "status": "ok",
            "lookback_minutes": lookback,
            "observations": [{
                "template": "loki_query", "query": query,
                "line_count": len(items), "items": items,
            }],
        }

    def query_grafana_panel(
        self, *, source: Mapping[str, Any], mapping: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        panel_id = int(arguments.get("panel_id") or 0)
        lookback = int(arguments.get("lookback_minutes", 60))
        if panel_id <= 0 or lookback not in LOOKBACK_MINUTES:
            raise MonitoringError("invalid Grafana panel query")
        return self._grafana(
            source, mapping, lookback, panel_id=panel_id,
        )

    def query_zabbix_history(
        self, *, source: Mapping[str, Any], mapping: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        hostid = str(mapping.get("hostid") or "")
        item_ids = arguments.get("item_ids")
        lookback = int(arguments.get("lookback_minutes", 60))
        if (
            not hostid.isdigit() or lookback not in LOOKBACK_MINUTES
            or not isinstance(item_ids, list) or not 1 <= len(item_ids) <= 16
            or len(set(item_ids)) != len(item_ids)
            or any(not isinstance(value, str) or not value.isdigit() or len(value) > 20
                   for value in item_ids)
        ):
            raise MonitoringError("invalid Zabbix history query")
        items = self._zabbix_call(source, 1, "item.get", {
            "output": ["itemid", "name", "key_", "units", "value_type"],
            "hostids": [hostid], "itemids": item_ids, "monitored": True,
            "limit": 16,
        })
        if {str(item.get("itemid")) for item in items} != set(item_ids):
            raise MonitoringError("Zabbix item is not mapped to this asset")
        end = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        start = end - lookback * 60
        by_type: dict[int, list[str]] = {}
        for item in items:
            value_type = str(item.get("value_type") or "")
            if value_type not in {"0", "3"}:
                raise MonitoringError("Zabbix item is not numeric")
            by_type.setdefault(int(value_type), []).append(str(item["itemid"]))
        samples = []
        request_id = 2
        for history_type, selected_ids in sorted(by_type.items()):
            values = self._zabbix_rpc(source, request_id, "history.get", {
                "history": history_type, "itemids": selected_ids,
                "time_from": start, "time_till": end,
                "sortfield": "clock", "sortorder": "ASC",
                "output": ["itemid", "clock", "value"], "limit": MAX_SAMPLES,
            })
            request_id += 1
            if not isinstance(values, list):
                raise MonitoringError("Zabbix history result is invalid")
            samples.extend(values)
            if len(samples) > MAX_SAMPLES:
                raise MonitoringError("Zabbix sample limit exceeded")
        metadata = {str(item["itemid"]): item for item in items}
        grouped = []
        for item_id in item_ids:
            points = []
            for sample in samples:
                if not isinstance(sample, Mapping) or str(sample.get("itemid")) != item_id:
                    continue
                try:
                    point = [int(sample.get("clock")), float(sample.get("value"))]
                except (TypeError, ValueError) as exc:
                    raise MonitoringError("Zabbix history value is invalid") from exc
                if not math.isfinite(point[1]):
                    raise MonitoringError("Zabbix history value is invalid")
                points.append(point)
            item = metadata[item_id]
            grouped.append({
                "itemid": item_id,
                "name": sanitize_evidence(item.get("name"))[:256],
                "key": sanitize_evidence(item.get("key_"))[:256],
                "units": sanitize_evidence(item.get("units"))[:32],
                "samples": points,
            })
        return {
            "source_id": int(source["id"]),
            "source_name": sanitize_text(str(source.get("name") or ""))[:64],
            "source_type": "zabbix", "status": "ok",
            "lookback_minutes": lookback,
            "observations": [{
                "template": "zabbix_history", "sample_count": len(samples),
                "items": grouped,
            }],
        }

    def _headers(self, source: Mapping[str, Any]) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        token = str(source.get("token") or "")
        if token:
            headers["Authorization"] = "Bearer " + token
        return headers

    def _grafana(
        self, source: Mapping[str, Any], mapping: Mapping[str, Any],
        lookback_minutes: int, panel_id: int | None = None,
    ) -> dict[str, Any]:
        uid = sanitize_text(str(mapping.get("datasource_uid") or "")).strip()
        dashboard_uid = sanitize_text(
            str(mapping.get("dashboard_uid") or "")
        ).strip()
        datasource_type = str(mapping.get("datasource_type") or "")
        if (
            not GRAFANA_UID_RE.fullmatch(uid)
            or not GRAFANA_UID_RE.fullmatch(dashboard_uid)
            or datasource_type not in {"prometheus", "loki"}
        ):
            raise MonitoringError("Grafana mapping is invalid")
        dashboard_payload = self._http_json(
            source, "GET", "/api/dashboards/uid/" + dashboard_uid,
        )
        dashboard = (
            dashboard_payload.get("dashboard")
            if isinstance(dashboard_payload, Mapping) else None
        )
        if not isinstance(dashboard, Mapping):
            raise MonitoringError("Grafana dashboard response is invalid")
        labels = mapping.get("labels")
        if not isinstance(labels, Mapping):
            raise MonitoringError("Grafana mapping is invalid")
        queries, panels = self._grafana_panel_queries(
            dashboard, uid, datasource_type, labels, lookback_minutes,
            panel_id=panel_id,
        )
        if not queries:
            raise MonitoringError("Grafana dashboard has no mapped panel queries")
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(minutes=lookback_minutes)
        try:
            response = self._request(
                "POST",
                str(source.get("base_url") or "").rstrip("/") + "/api/ds/query",
                json={
                    "queries": queries,
                    "from": str(int(start.timestamp() * 1000)),
                    "to": str(int(end.timestamp() * 1000)),
                },
                headers={**self._headers(source), "Content-Type": "application/json"},
                timeout=QUERY_TIMEOUT_SECONDS,
                verify=bool(source.get("verify_tls", True)),
                stream=True,
                allow_redirects=False,
            )
            payload = self._json(response)
        except MonitoringError:
            raise
        except Exception as exc:
            raise MonitoringError("Grafana panel query failed") from exc
        observations = self._summarize_grafana_results(payload, panels)
        return {
            "source_id": int(source["id"]),
            "source_name": sanitize_text(str(source.get("name") or ""))[:64],
            "source_type": "grafana",
            "datasource_type": datasource_type,
            "dashboard_uid": dashboard_uid,
            "status": "ok",
            "lookback_minutes": lookback_minutes,
            "observations": observations,
        }

    @classmethod
    def _grafana_panel_queries(
        cls, dashboard: Mapping[str, Any], datasource_uid: str,
        datasource_type: str, labels: Mapping[str, Any], lookback_minutes: int,
        panel_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[int, str]]]:
        pending = list(dashboard.get("panels") or [])
        queries: list[dict[str, Any]] = []
        panels: dict[str, tuple[int, str]] = {}
        while pending and len(queries) < 8:
            panel = pending.pop(0)
            if not isinstance(panel, Mapping):
                continue
            nested = panel.get("panels")
            if isinstance(nested, list):
                pending[0:0] = nested
            raw_panel_id = panel.get("id")
            current_panel_id = (
                raw_panel_id if isinstance(raw_panel_id, int)
                else int(raw_panel_id) if isinstance(raw_panel_id, str)
                and raw_panel_id.isdigit() else 0
            )
            if panel_id is not None and current_panel_id != panel_id:
                continue
            panel_source = panel.get("datasource")
            panel_uid = (
                str(panel_source.get("uid") or "")
                if isinstance(panel_source, Mapping) else str(panel_source or "")
            )
            for raw_target in panel.get("targets") or []:
                if len(queries) == 8 or not isinstance(raw_target, Mapping):
                    break
                target_source = raw_target.get("datasource")
                target_uid = (
                    str(target_source.get("uid") or "")
                    if isinstance(target_source, Mapping) else str(target_source or "")
                )
                if (target_uid or panel_uid) != datasource_uid or raw_target.get("hide"):
                    continue
                expression = raw_target.get("expr")
                if not isinstance(expression, str) or not expression.strip():
                    continue
                ref_id = chr(ord("A") + len(queries))
                query = dict(raw_target)
                query.update({
                    "refId": ref_id,
                    "expr": cls._grafana_expand(
                        expression, labels, lookback_minutes,
                    ),
                    "datasource": {
                        "uid": datasource_uid, "type": datasource_type,
                    },
                    "maxDataPoints": 1000,
                    "intervalMs": 30000,
                })
                query.pop("hide", None)
                queries.append(query)
                panels[ref_id] = (
                    current_panel_id,
                    sanitize_text(str(panel.get("title") or ""))[:128],
                )
        return queries, panels

    @staticmethod
    def _grafana_expand(
        expression: str, labels: Mapping[str, Any], lookback_minutes: int,
    ) -> str:
        result = expression
        macros = {
            "$__interval": "30s",
            "$__rate_interval": "2m",
            "$__range": "%sm" % lookback_minutes,
        }
        for key, value in macros.items():
            result = result.replace(key, value)
        for key, raw in labels.items():
            if not isinstance(key, str) or not LABEL_NAME_RE.fullmatch(key):
                continue
            value = str(raw).replace("\\", "\\\\").replace('"', '\\"')
            result = result.replace("${%s}" % key, value).replace("$" + key, value)
        return result[:8192]

    @staticmethod
    def _summarize_grafana_results(
        payload: Mapping[str, Any], panels: Mapping[str, tuple[int, str]],
    ) -> list[dict[str, Any]]:
        results = payload.get("results")
        if not isinstance(results, Mapping):
            raise MonitoringError("Grafana panel query response is invalid")
        observations = []
        total_samples = 0
        for ref_id, (panel_id, panel_title) in panels.items():
            result = results.get(ref_id)
            frames = result.get("frames") if isinstance(result, Mapping) else None
            if not isinstance(frames, list) or len(frames) > MAX_SERIES:
                continue
            numeric: list[float] = []
            safe_frames = []
            total_cells = 0
            for frame in frames:
                data = frame.get("data") if isinstance(frame, Mapping) else None
                values = data.get("values") if isinstance(data, Mapping) else None
                if not isinstance(values, list):
                    continue
                schema = frame.get("schema") if isinstance(frame, Mapping) else None
                fields = schema.get("fields") if isinstance(schema, Mapping) else None
                safe_columns = []
                for column_index, column in enumerate(values):
                    if not isinstance(column, list):
                        continue
                    safe_column = []
                    for raw in column:
                        total_cells += 1
                        if total_cells > MAX_SAMPLES:
                            raise MonitoringError("Grafana sample limit exceeded")
                        safe_column.append(MonitoringAdapter._safe_monitoring_value(raw))
                        if column_index == 0:
                            continue
                        try:
                            value = float(raw)
                        except (TypeError, ValueError):
                            continue
                        if math.isfinite(value):
                            numeric.append(value)
                    safe_columns.append(safe_column)
                safe_frames.append({
                    "fields": [
                        {
                            "name": sanitize_text(str(field.get("name") or ""))[:128],
                            "type": sanitize_text(str(field.get("type") or ""))[:32],
                            "labels": _safe_labels(field.get("labels")),
                        }
                        for field in (fields or [])[:20] if isinstance(field, Mapping)
                    ],
                    "values": safe_columns,
                })
            observations.append({
                "template": "dashboard_panel",
                "panel_id": panel_id,
                "panel_title": panel_title,
                "sample_count": len(numeric),
                "latest": numeric[-1] if numeric else None,
                "minimum": min(numeric) if numeric else None,
                "maximum": max(numeric) if numeric else None,
                "frames": safe_frames,
            })
            total_samples += len(numeric)
        return observations

    @staticmethod
    def _safe_monitoring_value(value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                return None
            return value
        return sanitize_evidence(value)[:2048]

    def _zabbix_call(
        self, source: Mapping[str, Any], request_id: int,
        method: str, params: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        result = self._zabbix_rpc(source, request_id, method, params)
        if not isinstance(result, list) or len(result) > 100:
            raise MonitoringError("Zabbix result is invalid")
        if any(not isinstance(item, Mapping) for item in result):
            raise MonitoringError("Zabbix result is invalid")
        return result

    def _zabbix_rpc(
        self, source: Mapping[str, Any], request_id: int,
        method: str, params: Mapping[str, Any],
    ) -> Any:
        if method not in {
            "apiinfo.version", "host.get", "problem.get", "item.get", "history.get",
        }:
            raise MonitoringError("Zabbix method is not allowed")
        try:
            response = self._request(
                "POST", str(source.get("base_url") or ""),
                json={
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": dict(params),
                    "id": request_id,
                },
                headers={
                    **self._headers(source),
                    "Content-Type": "application/json-rpc",
                },
                timeout=QUERY_TIMEOUT_SECONDS,
                verify=bool(source.get("verify_tls", True)),
                stream=True,
                allow_redirects=False,
            )
            payload = self._json(response)
        except MonitoringError:
            raise
        except Exception as exc:
            raise MonitoringError("Zabbix query failed") from exc
        if payload.get("error"):
            raise MonitoringError("Zabbix query failed")
        return payload.get("result")

    @staticmethod
    def _select_fields(item: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, str]:
        return {
            field: sanitize_evidence(item.get(field))[:512]
            for field in fields
        }
