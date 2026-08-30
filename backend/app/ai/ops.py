"""M2/S1 alert ingestion and bounded Prometheus observations."""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

import requests

from app.ai.autonomy.repository import sanitize_text
from app.core import config


ALERTMANAGER_MAX_BYTES = 256 * 1024
PROMETHEUS_RANGE_SECONDS = 15 * 60
PROMETHEUS_STEP_SECONDS = 30
PROMETHEUS_TIMEOUT_SECONDS = 5
PROMETHEUS_MAX_BYTES = 256 * 1024
PROMETHEUS_MAX_SERIES = 100
PROMETHEUS_MAX_SAMPLES = 1000


class OpsValidationError(ValueError):
    pass


class PrometheusQueryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AlertTrigger:
    status: str
    trigger_ref: str
    trigger_summary: str
    goal: str
    host_id: int
    system_user_id: int
    service: str
    instance: str
    job: str


def alertmanager_configured() -> bool:
    token = config.AI_ALERTMANAGER_TOKEN
    owner = config.AI_ALERTMANAGER_OWNER
    return bool(len(token) >= 32 and owner)


def prometheus_configured() -> bool:
    return bool(config.AI_PROMETHEUS_BASE_URL)


def verify_bearer(header: str) -> bool:
    if not alertmanager_configured():
        return False
    prefix = 'Bearer '
    if not isinstance(header, str) or not header.startswith(prefix):
        return False
    supplied = header[len(prefix):].strip()
    return bool(supplied) and hmac.compare_digest(
        supplied, config.AI_ALERTMANAGER_TOKEN,
    )


def _rfc3339(value: Any) -> str:
    text = sanitize_text(str(value or '')).strip()
    if not text or len(text) > 64:
        raise OpsValidationError('alert startsAt is required')
    try:
        parsed = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError as exc:
        raise OpsValidationError('alert startsAt must be RFC3339') from exc
    if parsed.tzinfo is None:
        raise OpsValidationError('alert startsAt must include timezone')
    return parsed.astimezone(datetime.timezone.utc).isoformat()


def _bounded_label(labels: Mapping[str, Any], name: str, limit: int) -> str:
    value = sanitize_text(str(labels.get(name) or '')).strip()
    if len(value) > limit:
        raise OpsValidationError('alert label %s is too long' % name)
    return value


def parse_alertmanager(payload: Any) -> AlertTrigger:
    if not isinstance(payload, Mapping):
        raise OpsValidationError('Alertmanager payload must be an object')
    status = sanitize_text(str(payload.get('status') or '')).strip().lower()
    if status not in {'firing', 'resolved'}:
        raise OpsValidationError('unsupported Alertmanager status')
    group_key = sanitize_text(str(payload.get('groupKey') or '')).strip()
    if not group_key or len(group_key) > 512:
        raise OpsValidationError('Alertmanager groupKey is required')
    alerts = payload.get('alerts')
    if not isinstance(alerts, list) or len(alerts) != 1:
        raise OpsValidationError('exactly one alert is supported per webhook')
    alert = alerts[0]
    if not isinstance(alert, Mapping):
        raise OpsValidationError('alert must be an object')
    alert_status = sanitize_text(
        str(alert.get('status') or status)
    ).strip().lower()
    if alert_status != status:
        raise OpsValidationError('group and alert status disagree')
    common_labels = payload.get('commonLabels') or {}
    alert_labels = alert.get('labels') or {}
    if not isinstance(common_labels, Mapping) or not isinstance(
        alert_labels, Mapping
    ):
        raise OpsValidationError('alert labels must be objects')
    labels = dict(common_labels)
    labels.update(alert_labels)
    try:
        host_id = int(labels.get('ogs_host_id'))
        system_user_id = int(labels.get('ogs_system_user_id'))
    except (TypeError, ValueError):
        raise OpsValidationError(
            'ogs_host_id and ogs_system_user_id must be positive integers'
        ) from None
    if host_id <= 0 or system_user_id <= 0:
        raise OpsValidationError(
            'ogs_host_id and ogs_system_user_id must be positive integers'
        )
    service = (
        _bounded_label(labels, 'service', 128)
        or _bounded_label(labels, 'alertname', 128)
    )
    if not service:
        raise OpsValidationError('service or alertname label is required')
    instance = _bounded_label(labels, 'instance', 255)
    job = _bounded_label(labels, 'job', 128)
    starts_at = _rfc3339(alert.get('startsAt'))
    trigger_ref = hashlib.sha256(
        (group_key + '\n' + starts_at).encode('utf-8')
    ).hexdigest()
    summary = '%s: %s on asset #%d' % (status, service, host_id)
    goal = (
        '调查资产 #%d 上服务 %s 不可用告警；确认影响和根因，所有写操作'
        '必须经过现有审批，并以独立验证结果收口。'
    ) % (host_id, service)
    return AlertTrigger(
        status=status,
        trigger_ref=trigger_ref,
        trigger_summary=summary,
        goal=goal[:512],
        host_id=host_id,
        system_user_id=system_user_id,
        service=service,
        instance=instance,
        job=job,
    )


def _promql_value(value: str) -> str:
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')


class PrometheusClient:
    """One fixed service-availability query; callers cannot provide PromQL."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        bearer_token: Optional[str] = None,
        request_get: Callable[..., Any] = requests.get,
    ) -> None:
        self.base_url = (base_url or config.AI_PROMETHEUS_BASE_URL).rstrip('/')
        self.bearer_token = (
            config.AI_PROMETHEUS_BEARER_TOKEN
            if bearer_token is None else bearer_token
        )
        self.request_get = request_get
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme not in {'http', 'https'}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise PrometheusQueryError('invalid Prometheus base URL')

    def service_availability(
        self, *, instance: str, job: str,
        now: Optional[datetime.datetime] = None,
    ) -> dict[str, Any]:
        if not instance or not job:
            raise PrometheusQueryError('instance and job labels are required')
        end = now or datetime.datetime.now(datetime.timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=datetime.timezone.utc)
        end = end.astimezone(datetime.timezone.utc)
        start = end - datetime.timedelta(seconds=PROMETHEUS_RANGE_SECONDS)
        query = 'up{instance="%s",job="%s"}' % (
            _promql_value(instance), _promql_value(job),
        )
        headers = {'Accept': 'application/json'}
        if self.bearer_token:
            headers['Authorization'] = 'Bearer %s' % self.bearer_token
        try:
            response = self.request_get(
                self.base_url + '/api/v1/query_range',
                params={
                    'query': query,
                    'start': start.timestamp(),
                    'end': end.timestamp(),
                    'step': PROMETHEUS_STEP_SECONDS,
                },
                headers=headers,
                timeout=PROMETHEUS_TIMEOUT_SECONDS,
                stream=True,
            )
            response.raise_for_status()
            length = response.headers.get('Content-Length')
            if length is not None and int(length) > PROMETHEUS_MAX_BYTES:
                raise PrometheusQueryError('Prometheus response is too large')
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=16 * 1024):
                raw.extend(chunk)
                if len(raw) > PROMETHEUS_MAX_BYTES:
                    raise PrometheusQueryError('Prometheus response is too large')
            payload = json.loads(raw.decode('utf-8'))
        except PrometheusQueryError:
            raise
        except Exception as exc:
            raise PrometheusQueryError('Prometheus query failed') from exc
        if not isinstance(payload, Mapping) or payload.get('status') != 'success':
            raise PrometheusQueryError('Prometheus query failed')
        result = (payload.get('data') or {}).get('result')
        if not isinstance(result, list):
            raise PrometheusQueryError('Prometheus result is invalid')
        if len(result) > PROMETHEUS_MAX_SERIES:
            raise PrometheusQueryError('Prometheus series limit exceeded')
        values: list[list[Any]] = []
        for series in result:
            if not isinstance(series, Mapping):
                raise PrometheusQueryError('Prometheus result is invalid')
            series_values = series.get('values') or []
            if not isinstance(series_values, list):
                raise PrometheusQueryError('Prometheus result is invalid')
            for sample in series_values:
                if not isinstance(sample, list) or len(sample) != 2:
                    raise PrometheusQueryError('Prometheus result is invalid')
                if isinstance(sample[0], bool) or not isinstance(
                    sample[0], (int, float)
                ) or not math.isfinite(float(sample[0])):
                    raise PrometheusQueryError('Prometheus timestamp is invalid')
                sample_value = sanitize_text(str(sample[1])).strip()
                try:
                    numeric_value = float(sample_value)
                except ValueError as exc:
                    raise PrometheusQueryError(
                        'Prometheus sample is invalid'
                    ) from exc
                if len(sample_value) > 32 or not math.isfinite(numeric_value):
                    raise PrometheusQueryError('Prometheus sample is invalid')
                values.append([sample[0], sample_value])
                if len(values) > PROMETHEUS_MAX_SAMPLES:
                    raise PrometheusQueryError('Prometheus sample limit exceeded')
        return {
            'query_template': 'service_availability',
            'range_seconds': PROMETHEUS_RANGE_SECONDS,
            'step_seconds': PROMETHEUS_STEP_SECONDS,
            'sample_count': len(values),
            'values': values,
        }
