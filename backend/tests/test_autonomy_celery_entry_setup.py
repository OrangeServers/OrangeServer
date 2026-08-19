# -*- coding: utf-8 -*-
"""celery_entry 遵守 setup 三态：配置未就绪前等待，不 import 业务配置。"""
import importlib
import sys


_MODULE = 'app.ai.autonomy.celery_entry'


def _reimport():
    sys.modules.pop(_MODULE, None)
    return importlib.import_module(_MODULE)


def test_celery_entry_builds_app_in_normal_mode(monkeypatch):
    from setup import state as state_mod
    from app.ai.autonomy import worker as worker_mod

    monkeypatch.setattr(state_mod, 'resolve_mode', lambda: 'normal')
    sentinel = object()
    monkeypatch.setattr(worker_mod, 'get_celery_app', lambda: sentinel)

    ce = _reimport()
    assert ce.celery_app is sentinel


def test_celery_entry_waits_until_configured(monkeypatch):
    import time

    from setup import state as state_mod
    from app.ai.autonomy import worker as worker_mod

    calls = []

    def resolve_mode():
        calls.append(1)
        return 'normal' if len(calls) > 1 else 'setup'

    monkeypatch.setattr(state_mod, 'resolve_mode', resolve_mode)
    sleeps = []
    monkeypatch.setattr(time, 'sleep', sleeps.append)
    sentinel = object()
    monkeypatch.setattr(worker_mod, 'get_celery_app', lambda: sentinel)

    ce = _reimport()
    assert ce.celery_app is sentinel
    assert len(calls) >= 2  # 轮询到 normal 才继续
    assert sleeps == [5]  # 确实等待了一个周期
