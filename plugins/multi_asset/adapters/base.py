"""assets adapters --- reuse the stock_analysis provider contract (no copy).

Review P1-3 requires the adapter layer to match the *real* BaseProviderV2 contract:

  * hook signature ``_do_fetch(self, cat, *, symbol=None, **kw) -> FetchResult``
    (keyword-only ``symbol``, single symbol, FetchResult return);
  * category declaration via the ``categories`` frozenset + ``supports()`` /
    ``supports_category()``;
  * credentials via ``SecretResolver`` (never a bare ``cls()`` instantiation, so
    providers that need a token still receive one).

The base class is obtained by importing the stock_analysis contract module
(plugin-standard-v1.8 §10.4 guarantees load order via the declared dependency;
§12.7 is the runtime data path, see adapters/__init__.py). When the import is not
possible the plugin still loads, but fetching degrades: ``CONTRACT_AVAILABLE`` is
False and every provider raises ``ProviderUnavailable``.
"""
from __future__ import annotations

from datetime import datetime, timezone

try:
    from plugins.stock_analysis.providers.base import DataCategory
    from plugins.stock_analysis.providers.base_v2 import (
        BaseProviderV2, FetchResult, ProviderUnavailable, SecretResolver)
    CONTRACT_AVAILABLE = True
except Exception:      # pragma: no cover - only when stock_analysis is absent
    from abc import ABC

    BaseProviderV2 = ABC
    FetchResult = None
    DataCategory = None
    SecretResolver = None
    CONTRACT_AVAILABLE = False

    class ProviderUnavailable(RuntimeError):
        """Degraded fallback used only when the contract cannot be imported."""


__all__ = ["AssetProviderBase", "DataCategory", "FetchResult", "ProviderUnavailable",
           "SecretResolver", "CONTRACT_AVAILABLE", "make_result", "secret_resolver",
           "cats"]


def cats(*names):
    """Build a category set from ``DataCategory`` member names.

    ``categories`` is not decoration: ``BaseProviderV2.fetch()`` rejects a request
    whose category is not declared (``supports_category``), so a provider with an
    empty set can never fetch anything. Members are resolved by name so the same
    declaration also works in degraded mode, where the stock_analysis contract
    could not be imported and ``DataCategory`` is ``None``.
    """
    if DataCategory is None:
        return frozenset(str(n).lower() for n in names)
    out = set()
    for name in names:
        member = getattr(DataCategory, name, None)
        if member is not None:
            out.add(member)
    return frozenset(out)


def make_result(category, data, source: str, url: str = "", params: dict = None,
                warnings=None, delay_seconds: int = 0):
    """Build a FetchResult, or a plain dict when the contract is unavailable."""
    if FetchResult is None:
        return {"category": getattr(category, "value", category), "data": data,
                "source": source, "url": url, "params": params or {},
                "warnings": list(warnings or []), "delay_seconds": delay_seconds}
    return FetchResult(category=category, data=data, source=source,
                       as_of=datetime.now(timezone.utc).astimezone().isoformat(),
                       url=url, params=params or {},
                       warnings=list(warnings or []), delay_seconds=delay_seconds)


def secret_resolver(cfg_reader=None):
    """SecretResolver bound to the plugin config -> env -> config.yaml chain."""
    if SecretResolver is None:
        return None
    if cfg_reader is None:
        cfg_reader = _plugin_config
    return SecretResolver.from_plugin_config(cfg_reader)


def _plugin_config() -> dict:
    """Read this plugin's persisted config (empty outside a Flask context)."""
    try:
        from flask import current_app
        pm = current_app.extensions.get("plugin_manager")
        if pm is not None and pm.is_enabled("multi_asset"):
            return pm.get_config("multi_asset") or {}
    except Exception:
        pass
    return {}


class AssetProviderBase(BaseProviderV2 if CONTRACT_AVAILABLE else object):
    """Common base for every asset adapter.

    ``market = "GLOBAL"`` on purpose: the parent ``supports_category`` narrows by
    market through stock_analysis' secmaster, which only knows equities. Since
    multi_asset resolves its own symbols (asset_symbol.py) we must not let that
    check reject futures/fund/bond codes.
    """

    name = "multi_asset_base"
    market = "GLOBAL"
    asset_type = ""
    categories: frozenset = frozenset()
    kline_freqs: frozenset = frozenset({"daily", "weekly", "monthly"})
    rate_per_min = 120
    burst = 10

    def __init__(self, secrets=None, session=None, asset_type: str = None):
        if asset_type:
            self.asset_type = str(asset_type).upper()
        if CONTRACT_AVAILABLE:
            super().__init__(secrets=secrets, session=session)

    # -- contract surface when imported in degraded mode --------------------- #

    @classmethod
    def supports(cls) -> set:
        try:
            return set(super().supports())
        except Exception:
            return set(cls.categories)

    def health(self) -> dict:
        return {"provider": self.name, "asset_type": self.asset_type,
                "categories": sorted(getattr(c, "value", str(c))
                                     for c in self.categories),
                "market": self.market}

    def _unavailable(self, reason: str):
        return ProviderUnavailable("%s/%s: %s" % (self.name, self.asset_type, reason))
