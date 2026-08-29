# providers/sina.py — 新浪：日K + 个股新闻（非授权兜底源，合规标注 authorized=False）
# 逐字平移自 stock_skill._get_price_data / _sentiment_analysis 抓取段（v1.3.0）
import re
import time

import pandas as pd
import requests

from .base import BaseProvider, DataCategory, ProviderError

_UA = {"User-Agent": "VeroRun-StockAnalysis/1.0.0"}   # 与 v1.3.0 原值一致
_POS = ("利好", "增长", "回升", "突破", "增持", "盈利", "上涨")   # 原词表原样
_NEG = ("利空", "下滑", "亏损", "减持", "风险", "下跌", "处罚")


class SinaProvider(BaseProvider):
    name = "sina"
    authorized = False                                 # 显式标注：非授权兜底

    @classmethod
    def supports(cls) -> set:
        return {DataCategory.KLINE, DataCategory.NEWS}

    def fetch_kline(self, market_symbol: str, datalen: int = 120) -> pd.DataFrame:
        # 逐字平移自 _get_price_data（v1.3.0），仅去掉函数名前缀
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
                    raise ProviderError(self.name, "kline", str(response.status_code))
                time.sleep(0.5)
        rows = response.json()
        if not rows:
            raise ProviderError(self.name, "kline", "empty payload")
        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame["day"])
        frame = frame.set_index("date")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.dropna(subset=["open", "high", "low", "close"])

    def fetch_news(self, market_symbol: str) -> list:
        # 逐字平移自 _sentiment_analysis 抓取段（v1.3.0）
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
        html = response.content.decode("gbk", errors="ignore")      # GBK 解码原样
        title_pattern = re.compile(
            r"<a[^>]+href=['\"][^'\"]+['\"][^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE)
        titles = []
        for raw_title in title_pattern.findall(html):
            title = re.sub(r"<[^>]+>", "", raw_title)
            title = re.sub(r"&nbsp;|\s+", " ", title).strip()
            if 5 <= len(title) <= 120 and title not in titles:
                titles.append(title)
        return titles
