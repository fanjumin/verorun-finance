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

        # 腾讯 ~ 分隔字段位（实测 88 字段，v_sh600519）：
        #   [1]名称 [3]现价 [4]昨收 [5]今开 [6]成交量(手) [30]时间戳
        #   [31]涨跌额 [32]涨跌幅% [33]最高 [34]最低 [37]成交额(万元)
        #   [38]换手率% [39]PE(TTM) [43]振幅% [44]流通市值(亿) [45]总市值(亿) [46]PB
        # 历史缺陷：只取 7 个字段，open/high/low/volume/amount 从未填充 →
        # 前端 quotes 这些列恒为 null（K线页与行情条缺开高低量）。
        def _f(idx: int) -> float:
            if idx >= len(parts):
                return 0.0
            try:
                return float(parts[idx] or 0)
            except (TypeError, ValueError):
                return 0.0

        return {"name": parts[1],
                "price": _f(3),
                "prev_close": _f(4),
                "open": _f(5),
                "high": _f(33),
                "low": _f(34),
                "volume": _f(6),
                # 腾讯成交额单位为万元 → 统一为元，与其他 provider 对齐
                "amount": _f(37) * 10000,
                "change_pct": _f(32),
                "pe_ttm": _f(39),
                "pb": _f(46),
                "turnover_rate": _f(38)}

    def fetch_quote(self, symbol: str) -> dict:
        return self._fetch_quote(symbol)
