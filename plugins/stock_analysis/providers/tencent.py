# providers/tencent.py — 腾讯：实时快照/指数（免费稳定，公开行情）
# 逐字平移自 stock_skill._get_latest_price（v1.3.0），字段下标保持不变
import time

import requests

from .base import DataCategory, ProviderError
from .base_v2 import BaseProviderV2, FetchResult
from .commons import market_symbol as commons_market_symbol


class TencentProvider(BaseProviderV2):
    name = "tencent"
    authorized = False
    market = "CN"
    categories = frozenset({DataCategory.QUOTE, DataCategory.INDEX})
    rate_per_min = 120
    burst = 20

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if cat is DataCategory.QUOTE:
            data = self._fetch_quote(symbol)
        elif cat is DataCategory.INDEX:
            from .commons import index_symbol
            data = self._fetch_quote(index_symbol(symbol))
        else:
            raise NotImplementedError(cat)
        return FetchResult(category=cat, data=data, source=self.name,
                           as_of=time.strftime("%Y-%m-%dT%H:%M:%S"))

    def _fetch_quote(self, symbol: str) -> dict:
        market_symbol = symbol.lower() if symbol.lower().startswith(("sh", "sz", "bj")) \
            else commons_market_symbol(symbol)
        for attempt in range(2):
            try:
                response = requests.get("https://qt.gtimg.cn/q=" + market_symbol, timeout=5)
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 1:
                    raise ProviderError(self.name, "quote", "request failed")
                time.sleep(0.5)
        parts = response.text.strip().strip(";").split("~")
        if len(parts) < 40:
            raise ProviderError(self.name, "quote", "实时行情返回数据不完整")
        return {"name": parts[1], "price": float(parts[3] or 0),
                "prev_close": float(parts[4] or 0), "change_pct": float(parts[32] or 0),
                "pe_ttm": float(parts[39] or 0),
                "pb": float(parts[46] or 0) if len(parts) > 46 else 0,
                "turnover_rate": float(parts[38] or 0) if len(parts) > 38 else 0}

    def fetch_quote(self, symbol: str) -> dict:
        return self._fetch_quote(symbol)
