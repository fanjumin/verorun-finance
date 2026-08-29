# providers/tencent.py — 腾讯：实时快照/指数（免费稳定，公开行情）
# 逐字平移自 stock_skill._get_latest_price（v1.3.0），字段下标保持不变
import time

import requests

from .base import BaseProvider, DataCategory, ProviderError
from .commons import market_symbol as commons_market_symbol


class TencentProvider(BaseProvider):
    name = "tencent"
    authorized = False

    @classmethod
    def supports(cls) -> set:
        return {DataCategory.QUOTE, DataCategory.INDEX}

    def fetch_quote(self, symbol: str) -> dict:
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
        # 字段位与 v1.3.0 完全一致（parts[3]现价 / [4]昨收 / [32]涨幅 / [39]PE-TTM
        # / [47]每股净资产 / [38]换手率），任何下标改动都是行为变化，禁止
        return {"name": parts[1], "price": float(parts[3] or 0),
                "prev_close": float(parts[4] or 0), "change_pct": float(parts[32] or 0),
                "pe_ttm": float(parts[39] or 0),
                "net_asset_per_share": float(parts[47] or 0) if len(parts) > 47 else 0,
                "turnover_rate": float(parts[38] or 0) if len(parts) > 38 else 0}
