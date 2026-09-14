# providers/akshare_provider.py — K线备源（开源聚合，授权状态：非授权 authorized=False）
# 命名避开官方包名 akshare，类名用 AkshareProvider
import time

import pandas as pd

from .base import DataCategory, ProviderError
from .base_v2 import BaseProviderV2, FetchResult

import logging

_log = logging.getLogger("stock_analysis.akshare_provider")


class AkshareProvider(BaseProviderV2):
    name = "akshare"
    authorized = False
    market = "CN"
    categories = frozenset({DataCategory.KLINE})
    rate_per_min = 30
    burst = 5

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if cat is DataCategory.KLINE:
            data = self._fetch_kline(symbol, datalen=kw.get("datalen", 120))
        else:
            raise NotImplementedError(cat)
        return FetchResult(category=cat, data=data, source=self.name,
                           as_of=time.strftime("%Y-%m-%dT%H:%M:%S"))

    def _fetch_kline(self, symbol: str, datalen: int = 120) -> pd.DataFrame:
        try:
            import akshare as ak
        except ImportError as err:
            raise ProviderError(self.name, "kline", f"akshare not installed: {err}",
                                retryable=False)
        code = symbol[2:] if len(symbol) > 6 and symbol[:2].isalpha() else symbol

        def _pull(adjust):
            try:
                return ak.stock_zh_a_hist(symbol=code, period="daily",
                                          adjust=adjust, timeout=15)
            except Exception as err:
                raise ProviderError(self.name, "kline", str(err))

        try:
            hfq = _pull("hfq")
        except ProviderError:
            hfq = None
        if hfq is None or hfq.empty:
            raise ProviderError(self.name, "kline", "empty", retryable=False)
        h = self._norm(hfq)

        try:
            raw = _pull("")
        except ProviderError:
            raw = None
        if raw is not None and not raw.empty:
            r = self._norm(raw)
            df = r
            df = df.merge(h[["date", "close"]].rename(columns={"close": "close_hfq"}),
                          on="date", how="left")
            df = df.set_index("date")
            df["close_hfq"] = df["close_hfq"].fillna(df["close"])
            ratio = (df["close_hfq"] / df["close"].replace(0, pd.NA))
            for c in ("open", "high", "low"):
                df[c + "_hfq"] = df[c] * ratio
            df["price_basis"] = "hfq"
        else:
            _log.warning("akshare raw unavailable for %s, using hfq-only frame (display = hfq)", code)
            df = h.copy()
            for c in ("open", "high", "low", "close"):
                df[c + "_hfq"] = df[c]
            df["price_basis"] = "hfq"
            df["_raw_unavailable"] = True
        return df.dropna(subset=["open", "high", "low", "close"]).tail(datalen)

    @staticmethod
    def _norm(df: pd.DataFrame) -> pd.DataFrame:
        df = df.rename(columns={"日期": "day", "开盘": "open", "收盘": "close",
                                "最高": "high", "最低": "low", "成交量": "volume"})
        df["date"] = pd.to_datetime(df["day"])
        df = df.set_index("date", drop=False)
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    def fetch_kline(self, symbol: str, datalen: int = 120) -> pd.DataFrame:
        return self._fetch_kline(symbol, datalen=datalen)
