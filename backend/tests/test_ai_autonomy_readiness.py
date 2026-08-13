# -*- coding: utf-8 -*-
"""M1/S2 dedicated Redis and Worker readiness contracts."""
from app.ai.autonomy import readiness
from app.core import config


def _configure(monkeypatch, *, enabled=True, host='192.0.2.10'):
    monkeypatch.setattr(config, 'AI_AUTONOMY_ENABLED', enabled)
    monkeypatch.setattr(config, 'AI_AUTONOMY_REDIS_HOST', host)
    monkeypatch.setattr(config, 'AI_AUTONOMY_REDIS_PORT', 6390)
    monkeypatch.setattr(config, 'AI_AUTONOMY_REDIS_PASSWORD', 'fake-pass')


def test_disabled_feature_does_not_probe_infrastructure(monkeypatch):
    _configure(monkeypatch, enabled=False)
    calls = []

    result = readiness.autonomy_readiness(
        checkpoint_probe=lambda: calls.append('checkpoint'),
        worker_probe=lambda: calls.append('worker'),
    )

    assert result == {
        'enabled': False,
        'configured': True,
        'checkpoint_ready': False,
        'worker_ready': False,
        'ready': False,
        'reason': 'feature_disabled',
    }
    assert calls == []


def test_enabled_feature_requires_structurally_valid_redis(monkeypatch):
    _configure(monkeypatch, host='')
    result = readiness.autonomy_readiness(enabled=True)
    assert result['configured'] is False
    assert result['ready'] is False
    assert result['reason'] == 'redis_not_configured'

    _configure(monkeypatch, host='redis.example.com/path')
    result = readiness.autonomy_readiness(enabled=True)
    assert result['configured'] is False
    assert result['reason'] == 'redis_not_configured'


def test_readiness_reports_fixed_reason_codes_only(monkeypatch):
    _configure(monkeypatch)

    checkpoint_down = readiness.autonomy_readiness(
        checkpoint_probe=lambda: False,
        worker_probe=lambda: True,
    )
    assert checkpoint_down['worker_ready'] is True
    assert checkpoint_down['reason'] == 'checkpoint_unavailable'

    worker_down = readiness.autonomy_readiness(
        checkpoint_probe=lambda: True,
        worker_probe=lambda: False,
    )
    assert worker_down['reason'] == 'worker_unavailable'

    ready = readiness.autonomy_readiness(
        checkpoint_probe=lambda: True,
        worker_probe=lambda: True,
    )
    assert ready == {
        'enabled': True,
        'configured': True,
        'checkpoint_ready': True,
        'worker_ready': True,
        'ready': True,
        'reason': 'ready',
    }


def test_probe_exceptions_never_escape_or_enter_reason(monkeypatch):
    _configure(monkeypatch)

    def fail():
        raise RuntimeError('redis://:private-secret@private-host:6379/0')

    result = readiness.autonomy_readiness(
        checkpoint_probe=fail, worker_probe=fail,
    )
    assert result['checkpoint_ready'] is False
    assert result['worker_ready'] is False
    assert result['reason'] == 'checkpoint_unavailable'
    assert 'private' not in str(result)


def test_checkpoint_probe_is_read_only_bounded_and_closes(monkeypatch):
    _configure(monkeypatch)
    observed = {'commands': [], 'closed': False}

    class FakeRedis:
        def ping(self):
            return True

        def execute_command(self, *args):
            observed['commands'].append(args)
            return [] if args[0] == 'FT._LIST' else None

        def close(self):
            observed['closed'] = True

    def fake_from_url(url, **kwargs):
        observed['url'] = url
        observed['kwargs'] = kwargs
        return FakeRedis()

    import redis

    monkeypatch.setattr(redis.Redis, 'from_url', fake_from_url)
    assert readiness.checkpoint_readiness(timeout=0.25) is True
    assert observed['url'].endswith('/0')
    assert observed['kwargs']['socket_connect_timeout'] == 0.25
    assert observed['kwargs']['socket_timeout'] == 0.25
    assert observed['commands'][0] == ('FT._LIST',)
    assert observed['commands'][1][0] == 'JSON.GET'
    assert observed['closed'] is True


def test_worker_probe_requires_registered_drive_task(monkeypatch):
    _configure(monkeypatch)
    from app.ai.autonomy import worker

    class Inspect:
        def __init__(self, response):
            self.response = response

        def registered(self):
            return self.response

    class Control:
        def __init__(self, response):
            self.response = response
            self.timeout = None
            self.connection = None

        def inspect(self, timeout, connection):
            self.timeout = timeout
            self.connection = connection
            return Inspect(self.response)

    class Connection:
        def __init__(self):
            self.ensure_kwargs = None
            self.released = False

        def ensure_connection(self, **kwargs):
            self.ensure_kwargs = kwargs

        def release(self):
            self.released = True

    class App:
        def __init__(self, response):
            self.control = Control(response)
            self.connection = Connection()
            self.connection_kwargs = None

        def connection_for_read(self, **kwargs):
            self.connection_kwargs = kwargs
            return self.connection

    app = App({'worker@example': [worker.DRIVE_RUN_TASK]})
    monkeypatch.setattr(worker, 'get_celery_app', lambda: app)
    assert readiness.worker_readiness(timeout=0.25) is True
    assert app.control.timeout == 0.25
    assert app.control.connection is app.connection
    assert app.connection_kwargs == {
        'connect_timeout': 0.25,
        'transport_options': {
            'socket_connect_timeout': 0.25,
            'socket_timeout': 0.25,
            'retry_on_timeout': False,
        },
    }
    assert app.connection.ensure_kwargs == {
        'max_retries': 0, 'timeout': 0.25,
    }
    assert app.connection.released is True

    app = App({'worker@example': ['unrelated.task']})
    monkeypatch.setattr(worker, 'get_celery_app', lambda: app)
    assert readiness.worker_readiness(timeout=0.25) is False
    assert app.connection.released is True
