"""Event emission for multi_asset.

Channel discipline (the D10 lesson): plugin.json ``hooks.provides`` is served by the
**hook registry** (``do_action``), while ``hooks.listens`` is served by
``get_event_handlers``. The two are independent channels — never mix them, or the
subscriber silently receives nothing.

This plugin provides ``asset.data.ready`` and emits it whenever a batch of bars is
persisted (daily reference/bars sync), so downstream consumers that subscribe via
``get_event_handlers()`` are notified.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("multi_asset.events")

EVENT_DATA_READY = "asset.data.ready"

__all__ = ["EVENT_DATA_READY", "emit_data_ready"]


def emit_data_ready(payload: dict) -> bool:
    """Dispatch the provided hook. Never raises; returns True when dispatched."""
    try:
        from plugin_manager.hooks import get_hook_registry
        get_hook_registry().do_action(EVENT_DATA_READY, dict(payload or {}))
        return True
    except Exception as err:
        _log.warning("asset.data.ready dispatch failed: %s", err)
        return False
