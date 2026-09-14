# providers/sina.py — 新浪：日K + 个股新闻（非授权兜底源，合规标注 authorized=False）
# 逐字平移自 stock_skill._get_price_data / _sentiment_analysis 抓取段（v1.3.0）
import re
import time

import pandas as pd
import requests

from .base import DataCategory, ProviderError
from .base_v2 import BaseProviderV2, FetchResult

_UA = {"User-Agent": "VeroRun-StockAnalysis/1.0.0"}   # 与 v1.3.0 原值一致
_POS = ("利好", "增长", "回升", "突破", "增持", "盈利", "上涨")   # 原词表原样
_NEG = ("利空", "下滑", "亏损", "减持", "风险", "下跌", "处罚")


class SinaProvider(BaseProviderV2):
    name = "sina"
    authorized = False
    market = "CN"
    categories = frozenset({DataCategory.KLINE, DataCategory.NEWS, DataCategory.QUOTE})
    rate_per_min = 60
    burst = 10

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if cat is DataCategory.KLINE:
            data = self._fetch_kline(symbol, datalen=kw.get("datalen", 120))
        elif cat is DataCategory.NEWS:
            data = self._fetch_news(symbol)
        elif cat is DataCategory.QUOTE:
            data = self._fetch_quote(symbol)
        else:
            raise NotImplementedError(cat)
        return FetchResult(category=cat, data=data, source=self.name,
                           as_of=time.strftime("%Y-%m-%dT%H:%M:%S"))

    def _fetch_quote(self, symbol: str) -> dict:
        from .commons import market_symbol as _ms
        mkt = symbol.lower() if symbol.lower().startswith(("sh", "sz", "bj")) else _ms(symbol)
        headers = dict(_UA)
        headers["Referer"] = "https://finance.sina.com.cn/"
        url = "https://hq.sinajs.cn/list=" + mkt
        for attempt in range(2):
            try:
                response = requests.get(url, headers=headers, timeout=5)
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 1:
                    raise ProviderError(self.name, "quote", "request failed after retry")
                time.sleep(0.5)
        text = response.content.decode("gbk", errors="ignore")
        m = re.search(r'"([^"]*)"', text)
        if not m:
            raise ProviderError(self.name, "quote", "empty payload", retryable=False)
        parts = m.group(1).split(",")
        if len(parts) < 4:
            raise ProviderError(self.name, "quote", "empty payload", retryable=False)
        price = float(parts[3] or 0)
        prev_close = float(parts[2] or 0)
        change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else None
        return {"name": parts[0], "price": price, "prev_close": prev_close,
                "change_pct": change_pct}

    def _fetch_kline(self, market_symbol: str, datalen: int = 120) -> pd.DataFrame:
        for attempt in range(2):
            try:
                response = requests.get(
                    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
                    params={"symbol": market_symbol, "scale": "240", "ma": "no",
                            "datalen": str(datalen)},
                    headers=_UA, timeout=10)
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 1:
                    raise ProviderError(self.name, "kline", "request failed after retry")
                time.sleep(0.5)
        rows = response.json()
        if not rows:
            raise ProviderError(self.name, "kline", "empty payload", retryable=False)
        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame["day"])
        frame = frame.set_index("date")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        for column in ("open", "high", "low", "close"):
            frame[column + "_hfq"] = frame[column]
        frame["price_basis"] = "raw"
        return frame.dropna(subset=["open", "high", "low", "close"])

    def _fetch_news(self, market_symbol: str) -> list:
        url = ("https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/"
               f"symbol/{market_symbol}.phtml")
        for attempt in range(2):
            try:
                response = requests.get(url, headers=_UA, timeout=10)
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 1:
                    raise ProviderError(self.name, "news", "request failed")
                time.sleep(0.5)
        html = response.content.decode("gbk", errors="ignore")
        block = html
        container = re.search(
            r'<div class="datelist">(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
        if container:
            block = container.group(1)
        title_pattern = re.compile(
            r"<a[^>]+href=['\"][^'\"]+['\"][^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE)
        titles = []
        for raw_title in title_pattern.findall(block):
            title = re.sub(r"<[^>]+>", "", raw_title)
            title = re.sub(r"&nbsp;|\s+", " ", title).strip()
            if "'+" in title or "+'" in title:
                continue
            if 5 <= len(title) <= 120 and title not in titles:
                titles.append(title)
        return titles

    def fetch_kline(self, market_symbol: str, datalen: int = 120) -> pd.DataFrame:
        return self._fetch_kline(market_symbol, datalen=datalen)

    def fetch_news(self, market_symbol: str) -> list:
        return self._fetch_news(market_symbol)
