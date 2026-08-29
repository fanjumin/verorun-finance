# gateway.py — 数据网关：类别路由 + 源前缀缓存。阶段1 基态；后续阶段在 ROUTE 上扩源
from __future__ import annotations

import time

import pandas as pd

from .providers.base import BaseProvider, DataCategory, ProviderError
from .providers.commons import index_symbol, market_symbol
from .providers.sina import SinaProvider
from .providers.tencent import TencentProvider

# 阶段1 路由表：kline/news→sina，quote/index→tencent（与 v1.3.0 数据流等价）
ROUTE: dict = {
    DataCategory.KLINE: [SinaProvider],
    DataCategory.NEWS: [SinaProvider],
    DataCategory.QUOTE: [TencentProvider],
    DataCategory.INDEX: [TencentProvider],
}
TTL = {DataCategory.KLINE: 300, DataCategory.QUOTE: 60, DataCategory.INDEX: 60,
       DataCategory.NEWS: 600}     # TTL 维持 v1.3.0 现值；不落盘（裁定归后续阶段）


class DataGateway:
    def __init__(self):
        self._instances = {cls: cls() for chain in ROUTE.values() for cls in chain}
        self._cache: dict[str, tuple[float, object]] = {}   # {key: (expire_at, value)}

    # ── 对外入口（后续阶段增 get_fundamental/get_moneyflow）──
    def get_kline(self, symbol: str, datalen: int = 120) -> pd.DataFrame:
        return self._dispatch(DataCategory.KLINE, symbol, datalen=datalen)

    def get_quote(self, symbol: str, category: DataCategory = DataCategory.QUOTE) -> dict:
        return self._dispatch(category, symbol)

    def get_news(self, symbol: str) -> list:
        return self._dispatch(DataCategory.NEWS, symbol)

    # ── 内部：缓存（key 带源前缀）→ 沿路由链取数 ──
    def _dispatch(self, category: DataCategory, symbol: str, **kwargs):
        chain = ROUTE[category]
        cache_key = f"{chain[0].name}:{category.value}:{symbol}:{kwargs.get('datalen', '')}"
        hit = self._cache.get(cache_key)
        if hit and hit[0] > time.time():
            return hit[1]
        last_err: Exception | None = None
        for provider_cls in chain:                        # 阶段1 链长=1；后续阶段多源 failover
            provider = self._instances[provider_cls]
            try:
                value = self._fetch(provider, category, symbol, **kwargs)
                self._cache[cache_key] = (time.time() + TTL[category], value)
                return value
            except (ProviderError, NotImplementedError) as err:
                last_err = err
        raise ProviderError("gateway", category.value, f"all sources failed: {last_err}")

    @staticmethod
    def _fetch(provider: BaseProvider, category: DataCategory, symbol: str, **kwargs):
        if category is DataCategory.KLINE:
            return provider.fetch_kline(market_symbol(symbol), **kwargs)
        if category in (DataCategory.QUOTE, DataCategory.INDEX):
            return provider.fetch_quote(symbol if category is DataCategory.QUOTE
                                        else index_symbol(symbol))
        if category is DataCategory.NEWS:
            return provider.fetch_news(market_symbol(symbol))
        raise NotImplementedError(category)


gateway = DataGateway()   # 模块级单例：stock_skill 各调用点直接 import 使用
