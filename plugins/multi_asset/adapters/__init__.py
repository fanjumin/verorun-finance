"""Adapter registry, per-asset dual-source chains and failover (audit V-11).

The registry maps a ``(name, asset_type)`` pair to a provider class; the chains
declare which sources serve which asset class. Fetching walks the chain and
applies the same cooldown semantics the stock_analysis gateway uses (3 consecutive
failures -> 300s removal), so a flapping free source does not stall every request.
"""
from __future__ import annotations

import logging
import threading
import time

from .base import (CONTRACT_AVAILABLE, DataCategory, FetchResult,
                   ProviderUnavailable, make_result, secret_resolver)
from .providers import AkshareProvider, SaGatewayProvider, SinaFinanceProvider

_log = logging.getLogger("multi_asset.adapters")

__all__ = ["build_provider", "provider_chain", "chain_sources", "fetch",
           "reset_cooldown", "cooldown_state", "CONTRACT_AVAILABLE",
           "DataCategory", "FetchResult", "ProviderUnavailable", "make_result"]

# Registry: name -> (class, asset_type)
REGISTRY = {
    ("akshare", "FUTURE"): AkshareProvider,
    ("akshare", "OPTION"): AkshareProvider,
    ("akshare", "FUND"): AkshareProvider,
    ("akshare", "BOND"): AkshareProvider,
    ("sina", "FUTURE"): SinaFinanceProvider,
    ("sina", "OPTION"): SinaFinanceProvider,
    ("sa_gateway", "FUND"): SaGatewayProvider,
    ("sa_gateway", "BOND"): SaGatewayProvider,
}

# Per-asset source chains (>= 2 real sources each; akshare is the free primary).
CHAINS = {
    "FUTURE": [("akshare", "FUTURE"), ("sina", "FUTURE")],
    "OPTION": [("akshare", "OPTION"), ("sina", "OPTION")],
    "FUND": [("akshare", "FUND"), ("sa_gateway", "FUND")],
    "BOND": [("akshare", "BOND"), ("sa_gateway", "BOND")],
}

# MA-2：冷却语义直接复用 stock_analysis gateway 的常量，避免两套口径漂移；
# stock_analysis 不可用时（插件缺失）回落到同值字面量，保持行为一致。
try:
    from plugins.stock_analysis.gateway import COOLDOWN as COOLDOWN_SECONDS
    from plugins.stock_analysis.gateway import (
        COOLDOWN_FAIL_STREAK as COOLDOWN_FAIL_STREAK)
except Exception:      # noqa: BLE001 —— 契约/插件缺失时降级为字面量
    COOLDOWN_SECONDS = 300
    COOLDOWN_FAIL_STREAK = 3

_LOCK = threading.Lock()
_FAIL_STREAK: dict = {}
_COOLDOWN_UNTIL: dict = {}
_INSTANCES: dict = {}


def build_provider(asset_type: str, name: str):
    """Instantiate a provider with a SecretResolver injected (never a bare cls())."""
    at = str(asset_type or "").upper()
    key = (str(name), at)
    cls = REGISTRY.get(key)
    if cls is None:
        raise ProviderUnavailable("no provider registered for %s/%s" % (name, at))
    return cls(secrets=secret_resolver(), asset_type=at)


def provider_chain(asset_type: str) -> list:
    """Instantiate (and cache) the provider chain for an asset class.

    Honours the user's ``data_provider`` preference (mirrors the gateway's
    ``apply_preference``): when the configured source serves this asset class it is
    moved to the head of the chain, otherwise the default order is kept.
    """
    at = str(asset_type or "").upper()
    chain = list(CHAINS.get(at) or [])
    preferred = _configured_preference()
    if preferred:
        chain.sort(key=lambda item: 0 if item[0] == preferred else 1)
    with _LOCK:
        out = []
        for name, _at in chain:
            inst = _INSTANCES.get((name, at))
            if inst is None:
                inst = build_provider(at, name)
                _INSTANCES[(name, at)] = inst
            out.append(inst)
    return out


def _configured_preference() -> str:
    """Read the persisted ``data_provider`` setting (empty outside a Flask context)."""
    try:
        from flask import current_app
        pm = current_app.extensions.get("plugin_manager")
        if pm is not None and pm.is_enabled("multi_asset"):
            cfg = pm.get_config("multi_asset") or {}
            return str(cfg.get("data_provider") or "").strip()
    except Exception:
        pass
    return ""


def chain_sources(asset_type: str) -> list:
    """Declared source names for an asset class (used by tests / diagnostics)."""
    return [name for name, _at in CHAINS.get(str(asset_type or "").upper(), [])]


def cooldown_state() -> dict:
    return {"fail_streak": dict(_FAIL_STREAK),
            "cooldown_until": dict(_COOLDOWN_UNTIL)}


def reset_cooldown():
    with _LOCK:
        _FAIL_STREAK.clear()
        _COOLDOWN_UNTIL.clear()


def _key(asset_type: str, name: str):
    return "%s/%s" % (str(asset_type).upper(), name)


def _available(asset_type: str, providers: list) -> list:
    now = time.time()
    out = []
    for p in providers:
        if _COOLDOWN_UNTIL.get(_key(asset_type, p.name), 0) <= now:
            out.append(p)
    return out


def _record_failure(asset_type: str, name: str):
    k = _key(asset_type, name)
    with _LOCK:
        streak = _FAIL_STREAK.get(k, 0) + 1
        _FAIL_STREAK[k] = streak
        if streak >= COOLDOWN_FAIL_STREAK:
            _COOLDOWN_UNTIL[k] = time.time() + COOLDOWN_SECONDS
            _log.warning("provider %s cooled down %ss after %d failures",
                         k, COOLDOWN_SECONDS, streak)


def _record_success(asset_type: str, name: str):
    with _LOCK:
        _FAIL_STREAK[_key(asset_type, name)] = 0


def fetch(asset_type: str, category, symbol: str, **kw):
    """Fetch through the asset chain with cooldown-aware failover.

    Returns the ``FetchResult`` from the first source that yields data; raises
    ``ProviderUnavailable`` when the whole chain fails.
    """
    if not CONTRACT_AVAILABLE:
        raise ProviderUnavailable(
            "stock_analysis provider contract unavailable; cannot fetch")
    at = str(asset_type or "").upper()
    providers = _available(at, provider_chain(at))
    if not providers:
        raise ProviderUnavailable("all %s sources are cooling down" % at)
    last_err = None
    for provider in providers:
        try:
            result = provider.fetch(category, symbol=symbol, **kw)
        except ProviderUnavailable as err:
            _record_failure(at, provider.name)
            last_err = err
            continue
        except Exception as err:      # upstream unexpected: treat as source failure
            _record_failure(at, provider.name)
            last_err = err
            continue
        empty = getattr(result, "empty", None)
        if empty is None:
            empty = not (result or {}).get("data") if isinstance(result, dict) else False
        if empty:
            _record_failure(at, provider.name)
            last_err = ProviderUnavailable("%s returned empty" % provider.name)
            continue
        _record_success(at, provider.name)
        return result
    raise ProviderUnavailable("all %s sources failed: %s" % (at, last_err))
