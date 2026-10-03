"""Lazy loader for the plugin's ``data/*.json`` reference assets.

Why a data directory instead of inline Python literals: the i18n standard
(``docs/i18n-standard.md``) requires English source strings in code, and
``scripts/i18n_check.py --check-cn`` flags CJK literals in ``*.py``. Two kinds of
Chinese text are unavoidable in a market-data plugin and are therefore kept as
JSON assets under ``data/`` (which sits in the scanner's ``SKIP_DIRS``):

  * vendor (akshare) Chinese column names / API argument values;
  * Chinese variety display names.

Nothing here is UI copy — UI strings go through ``i18n/*.yml`` + ``self.t()``.
"""
from __future__ import annotations

import json
import os
import threading

__all__ = ["load_data", "data_path"]

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CACHE: dict = {}
_LOCK = threading.Lock()


def data_path(name: str) -> str:
    """Absolute path of a bundled data asset."""
    return os.path.join(_DATA_DIR, name)


def load_data(name: str) -> dict:
    """Load a bundled JSON asset once; returns ``{}`` when missing or malformed."""
    key = str(name)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        loaded = {}
        try:
            with open(data_path(key), encoding="utf-8") as fh:
                parsed = json.load(fh)
            if isinstance(parsed, dict):
                loaded = parsed
        except Exception:
            loaded = {}
        _CACHE[key] = loaded
    return loaded
