# -*- coding: utf-8 -*-
"""Process enablement for autonomy APIs."""
from app.core import config


def is_autonomy_enabled():
    """True when ``OGS_AI_AUTONOMY_ENABLED`` is on.

    Bundled Compose sets this to true. There is no product UI switch.
    """
    return bool(config.AI_AUTONOMY_ENABLED)
