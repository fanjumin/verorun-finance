"""
providers/polygon_provider.py — Polygon.io Provider（美股 K 线 / 报价 / 公司概况）

选型理由
--------
- Polygon 是美股历史 K 线最稳定的 REST 源之一，免费档 5 calls/min、延迟 15 min，
  付费档提供实时 + 全量历史。与 FMP 互补：FMP 强在基本面 + 一致预期，
  Polygon 强在高频 K 线 + 期权/外汇/加密。
- 一份凭据覆盖 KLINE / QUOTE / PROFILE / NEWS。

端点（v2，稳定）：
   /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
   /v2/reference/tickers/{ticker}
   /v1/open-close/{ticker}/{date}
   /v2/reference/news?ticker={ticker}
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

try:
    from .base_v2 import (BaseProviderV2, DataCategory, FetchResult,
                          ProviderUnavailable, SecretResolver)
except ImportError:
    from base_v2 import (BaseProviderV2, DataCategory, FetchResult,
                         ProviderUnavailable, SecretResolver)

BASE = "https://api.polygon.io"


class PolygonProvider(BaseProviderV2):
    name = "polygon"
    market = "US"
    required_secret = "api_key"
    rate_per_min = 5                 # 免费档 5/min；付费按套餐调高
    burst = 3
    categories = frozenset({
        DataCategory.KLINE, DataCategory.QUOTE, DataCategory.PROFILE,
        DataCategory.NEWS,
    })

    def supports_category(self, cat, symbol: Optional[str] = None) -> bool:
        if not super().supports_category(cat, symbol):
            return False
        if symbol:
            code = symbol.split(":")[-1]
            if code.isdigit() and len(code) == 6:
                return False
        return True

    def _url(self, path: str, **params) -> str:
        p = {"apiKey": self.secret()}
        p.update({k: v for k, v in params.items() if v is not None})
        qs = "&".join(f"{k}={v}" for k, v in p.items())
        return self._redact(f"{BASE}/{path}?{qs}")

    def _call(self, path: str, **params) -> Any:
        p = {"apiKey": self.secret()}
        p.update({k: v for k, v in params.items() if v is not None})
        data = self._get_json(f"{BASE}/{path}", p, timeout=20)
        if isinstance(data, dict) and data.get("error"):
            raise ProviderUnavailable(f"Polygon: {data['error']}")
        return data

    def _do_fetch(self, cat: DataCategory, *, symbol: Optional[str] = None, **kw) -> FetchResult:
        sym = (symbol or "").split(":")[-1].upper()
        if not sym:
            raise ProviderUnavailable("Polygon 需要 symbol")

        if cat == DataCategory.KLINE:
            return self._kline(sym, **kw)
        if cat == DataCategory.QUOTE:
            return self._quote(sym)
        if cat == DataCategory.PROFILE:
            return self._profile(sym)
        if cat == DataCategory.NEWS:
            return self._news(sym, kw.get("limit", 20))
        raise ProviderUnavailable(f"Polygon 不支持 {cat.value}")

    def _kline(self, sym: str, start: Optional[str] = None, end: Optional[str] = None,
               limit: int = 1000, **_) -> FetchResult:
        from_d = (start or "2020-01-01").replace("-", "")
        to_d = (end or pd.Timestamp.now().strftime("%Y-%m-%d")).replace("-", "")
        raw = self._call(
            f"v2/aggs/ticker/{sym}/range/1/day/{from_d}/{to_d}",
            adjusted="true", sort="asc", limit=limit,
        )
        results = (raw or {}).get("results") or []
        if not results:
            return FetchResult(DataCategory.KLINE, pd.DataFrame(), "polygon", "", warnings=["empty"])
        df = pd.DataFrame(results)
        df = df.rename(columns={
            "o": "open", "h": "high", "l": "low", "c": "close", "v": "vol", "t": "date",
        })
        df["date"] = pd.to_datetime(df["date"], unit="ms")
        df = df.set_index("date").sort_index()[["open", "high", "low", "close", "vol"]].astype(float)
        return FetchResult(DataCategory.KLINE, df, "polygon",
                           as_of=str(df.index.max()), delay_seconds=900,
                           url=self._url(f"v2/aggs/ticker/{sym}/range/1/day/{from_d}/{to_d}"),
                           params={"symbol": sym}, cost_units=1.0)

    def _quote(self, sym: str) -> FetchResult:
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        data = self._call(f"v1/open-close/{sym}/{today}")
        if not data or data.get("status") == "NOT_FOUND":
            return FetchResult(DataCategory.QUOTE, {}, "polygon", "", warnings=["not_found"])
        row = {
            "symbol": sym, "open": data.get("open"), "high": data.get("high"),
            "low": data.get("low"), "close": data.get("close"),
            "volume": data.get("volume"), "preMarket": data.get("preMarket"),
            "afterHours": data.get("afterHours"),
        }
        return FetchResult(DataCategory.QUOTE, row, "polygon",
                           as_of=today, delay_seconds=900,
                           url=self._url(f"v1/open-close/{sym}/{today}"),
                           params={"symbol": sym}, cost_units=1.0)

    def _profile(self, sym: str) -> FetchResult:
        data = self._call(f"v3/reference/tickers/{sym}")
        row = (data or {}).get("results", {}) if isinstance(data, dict) else {}
        return FetchResult(DataCategory.PROFILE, row, "polygon", as_of="",
                           delay_seconds=86400,
                           url=self._url(f"v3/reference/tickers/{sym}"),
                           params={"symbol": sym}, cost_units=1.0)

    def _news(self, sym: str, limit: int = 20) -> FetchResult:
        data = self._call("v2/reference/news", ticker=sym, limit=limit)
        items = (data or {}).get("results") or []
        rows = [{"title": d.get("title"), "published": d.get("published_utc"),
                 "source": d.get("publisher", {}).get("name"),
                 "url": d.get("article_url"),
                 "text": (d.get("description") or "")[:500]} for d in items]
        return FetchResult(DataCategory.NEWS, rows, "polygon",
                           as_of=(rows[0]["published"] if rows else ""),
                           delay_seconds=3600,
                           url=self._url("v2/reference/news", ticker=sym),
                           params={"symbol": sym}, cost_units=1.0)

    def health(self) -> dict:
        base = super().health()
        if not self.secret():
            base.update({"ok": False, "reason": "未配置 api_key"})
            return base
        try:
            d = self._call("v3/reference/tickers/AAPL")
            base.update({"ok": bool(d and d.get("results")), "latency_probe": "ticker/AAPL"})
        except Exception as e:
            base.update({"ok": False, "reason": str(e)})
        return base


if __name__ == "__main__":
    p = PolygonProvider(secrets=SecretResolver(lambda n: None))
    print("health:", p.health())
    print("supports KLINE(AAPL):", p.supports(DataCategory.KLINE, "AAPL"))
    print("supports KLINE(600519):", p.supports(DataCategory.KLINE, "600519"))
