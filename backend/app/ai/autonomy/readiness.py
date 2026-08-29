# -*- coding: utf-8 -*-
"""M1/S2 autonomy infrastructure readiness.

The public status response contains booleans, fixed machine-readable reason
codes, and bounded worker capacity fields only.  Connection errors, Redis
URLs, credentials, worker names and private host details must never cross the
API boundary.
"""
from collections.abc import Mapping
from urllib.parse import quote
import uuid

from app.core import config


READINESS_TIMEOUT_SECONDS = 0.75

REASON_READY = 'ready'
REASON_FEATURE_DISABLED = 'feature_disabled'
REASON_REDIS_NOT_CONFIGURED = 'redis_not_configured'
REASON_CHECKPOINT_UNAVAILABLE = 'checkpoint_unavailable'
REASON_WORKER_UNAVAILABLE = 'worker_unavailable'
WORKER_POOL = 'prefork'


def autonomy_redis_url(database: int) -> str:
    """Build a credential-safe URL for the configured autonomy Redis.

    Passwords are encoded as URI user-info.  The host is configuration rather
    than model input, but rejecting URL delimiters prevents it from changing
    the URL authority or path accidentally.
    """
    host = str(config.AI_AUTONOMY_REDIS_HOST or '').strip()
    if not host or any(char.isspace() for char in host):
        raise ValueError('autonomy Redis host is not configured')
    if any(char in host for char in '/@?#'):
        raise ValueError('autonomy Redis host is invalid')

    port = int(config.AI_AUTONOMY_REDIS_PORT)
    if not 1 <= port <= 65535:
        raise ValueError('autonomy Redis port is invalid')
    db = int(database)
    if db < 0:
        raise ValueError('autonomy Redis database is invalid')

    # Bracket a bare IPv6 address before adding the port.  Existing brackets
    # are retained for environments that already provide them.
    authority_host = host
    if ':' in host and not (host.startswith('[') and host.endswith(']')):
        authority_host = '[%s]' % host

    password = str(config.AI_AUTONOMY_REDIS_PASSWORD or '')
    auth = ':%s@' % quote(password, safe='') if password else ''
    return 'redis://%s%s:%d/%d' % (auth, authority_host, port, db)


def autonomy_redis_configured() -> bool:
    """Return whether a structurally valid Redis target exists."""
    try:
        autonomy_redis_url(0)
    except (TypeError, ValueError):
        return False
    return True


def checkpoint_readiness(timeout: float = READINESS_TIMEOUT_SECONDS) -> bool:
    """Probe DB 0 plus the JSON/Search commands required by the saver.

    The probe is read-only: it does not create indices or checkpoint keys.
    Both socket connection and command reads are bounded by a short timeout.
    """
    client = None
    try:
        from redis import Redis

        client = Redis.from_url(
            autonomy_redis_url(0),
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
            retry_on_timeout=False,
            decode_responses=True,
        )
        if not client.ping():
            return False
        # LangGraph's Redis saver uses RediSearch and RedisJSON.  These two
        # commands prove both capabilities without writing probe data.
        client.execute_command('FT._LIST')
        client.execute_command(
            'JSON.GET', '__ogs_autonomy_readiness__:%s' % uuid.uuid4().hex,
        )
        return True
    except Exception:
        return False
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def _configured_worker_concurrency() -> int | None:
    try:
        value = int(config.AI_AUTONOMY_WORKER_CONCURRENCY)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _normalise_worker_pool(value) -> str | None:
    """Map Celery's implementation string to a non-sensitive pool name."""
    text = str(value or '').lower()
    for name in ('prefork', 'gevent', 'eventlet', 'threads', 'solo'):
        if name in text:
            return name
    return None


def _worker_stats_summary(stats) -> tuple[str | None, int | None]:
    """Extract only pool type and slot counts from inspect.stats()."""
    if not isinstance(stats, Mapping):
        return None, None

    pools = set()
    observed = []
    for details in stats.values():
        if not isinstance(details, Mapping):
            continue
        pool = details.get('pool')
        if not isinstance(pool, Mapping):
            continue
        pool_name = _normalise_worker_pool(
            pool.get('implementation') or pool.get('pool')
        )
        if pool_name:
            pools.add(pool_name)
        raw_count = pool.get('max-concurrency')
        if raw_count is None:
            raw_count = pool.get('max_concurrency')
        if raw_count is None and isinstance(
            pool.get('processes'), (list, tuple, set)
        ):
            raw_count = len(pool['processes'])
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        if count > 0:
            observed.append(count)

    pool_name = next(iter(pools)) if len(pools) == 1 else (
        'mixed' if pools else None
    )
    return pool_name, sum(observed) if observed else None


def worker_readiness_details(
    timeout: float = READINESS_TIMEOUT_SECONDS,
) -> dict:
    """Return safe worker readiness and runtime capacity details."""
    connection = None
    result = {
        'ready': False,
        'worker_pool': WORKER_POOL,
        'worker_concurrency_configured': _configured_worker_concurrency(),
        'worker_concurrency_observed': None,
    }
    try:
        from app.ai.autonomy.worker import DRIVE_RUN_TASK, get_celery_app

        celery_app = get_celery_app()
        if celery_app is None:
            return result
        configured_pool = _normalise_worker_pool(
            getattr(getattr(celery_app, 'conf', None), 'worker_pool', None)
        )
        if configured_pool:
            result['worker_pool'] = configured_pool
        connection = celery_app.connection_for_read(
            connect_timeout=timeout,
            transport_options={
                'socket_connect_timeout': timeout,
                'socket_timeout': timeout,
                'retry_on_timeout': False,
            },
        )
        # Inspect's timeout bounds the reply wait, not an initial broker
        # connection.  Establish that connection explicitly with no retries so
        # an unavailable Redis cannot hold the status request open.
        connection.ensure_connection(max_retries=0, timeout=timeout)
        registered = celery_app.control.inspect(
            timeout=timeout, connection=connection,
        )
        registered_tasks = registered.registered()
        if not isinstance(registered_tasks, dict):
            return result
        result['ready'] = any(
            isinstance(tasks, (list, tuple, set))
            and DRIVE_RUN_TASK in tasks
            for tasks in registered_tasks.values()
        )
        stats = getattr(registered, 'stats', None)
        if callable(stats):
            try:
                pool_name, observed = _worker_stats_summary(stats())
                if pool_name:
                    result['worker_pool'] = pool_name
                if observed is not None:
                    result['worker_concurrency_observed'] = observed
            except Exception:
                # Registration is sufficient for worker readiness; stats are
                # supplementary and must not make a healthy worker unavailable.
                pass
        return result
    except Exception:
        return result
    finally:
        if connection is not None:
            try:
                connection.release()
            except Exception:
                pass


def worker_readiness(timeout: float = READINESS_TIMEOUT_SECONDS) -> bool:
    """Confirm a worker on the configured broker registered drive_run."""
    return bool(worker_readiness_details(timeout)['ready'])


def autonomy_readiness(
    *, enabled=None, checkpoint_probe=None, worker_probe=None,
) -> dict:
    """Return the stable public readiness contract.

    Probes are injectable so API and unit tests never need a live broker.
    """
    feature_enabled = (
        bool(config.AI_AUTONOMY_ENABLED) if enabled is None else bool(enabled)
    )
    configured = autonomy_redis_configured()
    result = {
        'enabled': feature_enabled,
        'configured': configured,
        'checkpoint_ready': False,
        'worker_ready': False,
        'worker_pool': WORKER_POOL,
        'worker_concurrency_configured': _configured_worker_concurrency(),
        'worker_concurrency_observed': None,
        'ready': False,
        'reason': REASON_FEATURE_DISABLED,
    }
    if not feature_enabled:
        return result
    if not configured:
        result['reason'] = REASON_REDIS_NOT_CONFIGURED
        return result

    checkpoint_probe = checkpoint_probe or checkpoint_readiness
    worker_probe = worker_probe or worker_readiness_details
    try:
        result['checkpoint_ready'] = bool(checkpoint_probe())
    except Exception:
        result['checkpoint_ready'] = False
    try:
        worker_status = worker_probe()
        if isinstance(worker_status, Mapping):
            result['worker_ready'] = bool(worker_status.get('ready'))
            pool_name = _normalise_worker_pool(
                worker_status.get('worker_pool') or worker_status.get('pool')
            )
            if pool_name:
                result['worker_pool'] = pool_name
            configured = worker_status.get(
                'worker_concurrency_configured',
                worker_status.get('configured_concurrency'),
            )
            try:
                configured = int(configured)
            except (TypeError, ValueError):
                configured = None
            if configured is not None and configured > 0:
                result['worker_concurrency_configured'] = configured
            observed = worker_status.get(
                'worker_concurrency_observed',
                worker_status.get('observed_concurrency'),
            )
            try:
                observed = int(observed)
            except (TypeError, ValueError):
                observed = None
            if observed is not None and observed > 0:
                result['worker_concurrency_observed'] = observed
        else:
            result['worker_ready'] = bool(worker_status)
    except Exception:
        result['worker_ready'] = False

    if not result['checkpoint_ready']:
        result['reason'] = REASON_CHECKPOINT_UNAVAILABLE
    elif not result['worker_ready']:
        result['reason'] = REASON_WORKER_UNAVAILABLE
    else:
        result['ready'] = True
        result['reason'] = REASON_READY
    return result
