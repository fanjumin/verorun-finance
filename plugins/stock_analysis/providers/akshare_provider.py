# providers/akshare_provider.py — K线备源（开源聚合，授权状态：非授权 authorized=False）
# 命名避开官方包名 akshare，类名用 AkshareProvider
import time

import pandas as pd

from .base import DataCategory, ProviderError
from .base_v2 import BaseProviderV2, FetchResult

import logging

_log = logging.getLogger("stock_analysis.akshare_provider")

# K 线周期 → akshare stock_zh_a_hist 的 period 取值
_HIST_PERIODS = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}
# P1 分钟线周期 → akshare stock_zh_a_hist_min_em 的 period 取值
# （period ≥ 5 时 akshare 忽略 start/end，返回全量，需自行 tail）
_MINUTE_PERIODS = {"5m": "5", "15m": "15", "30m": "30", "60m": "60"}


class AkshareProvider(BaseProviderV2):
    name = "akshare"
    authorized = False
    market = "CN"
    categories = frozenset({DataCategory.KLINE})
    kline_freqs = frozenset({"daily", "weekly", "monthly", "5m", "15m", "30m", "60m"})
    rate_per_min = 30
    burst = 5

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if cat is DataCategory.KLINE:
            data = self._fetch_kline(symbol, datalen=kw.get("datalen", 120),
                                     freq=kw.get("freq", "daily"))
        else:
            raise NotImplementedError(cat)
        return FetchResult(category=cat, data=data, source=self.name,
                           as_of=time.strftime("%Y-%m-%dT%H:%M:%S"))

    def _fetch_kline(self, symbol: str, datalen: int = 120,
                     freq: str = "daily") -> pd.DataFrame:
        period = _HIST_PERIODS.get(freq)
        minute_period = _MINUTE_PERIODS.get(freq)
        if period is None and minute_period is None:
            raise ProviderError(self.name, "kline", f"akshare 未接入周期 {freq}",
                                retryable=False)
        try:
            import akshare as ak
        except ImportError as err:
            raise ProviderError(self.name, "kline", f"akshare not installed: {err}",
                                retryable=False)
        code = symbol[2:] if len(symbol) > 6 and symbol[:2].isalpha() else symbol

        if minute_period is not None:
            # 分钟线：东财只取不复权口径（adjust=""），帧自报 price_basis="raw"；
            # 请求 hfq/qfq 时由 kline_service._resolve_basis 如实降级为 raw（不假称复权）。
            try:
                raw = ak.stock_zh_a_hist_min_em(symbol=code, period=minute_period,
                                                adjust="")
            except Exception as err:
                raise ProviderError(self.name, "kline", str(err))
            if raw is None or raw.empty:
                raise ProviderError(self.name, "kline", "empty", retryable=False)
            df = self._norm(raw)
            df["price_basis"] = "raw"
            return df.dropna(subset=["open", "high", "low", "close"]).tail(datalen)

        def _pull(adjust):
            try:
                return ak.stock_zh_a_hist(symbol=code, period=period,
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
            # _norm 产出「索引名=date + 列 date」，直接 merge(on="date") 会因歧义抛
            # ValueError: 'date' is both an index level and a column label → 两侧都丢弃索引
            df = self._norm(raw).reset_index(drop=True)
            df = df.merge(h[["date", "close"]].reset_index(drop=True)
                          .rename(columns={"close": "close_hfq"}),
                          on="date", how="left")
            df = df.set_index("date")
            df["close_hfq"] = df["close_hfq"].fillna(df["close"])
            ratio = (df["close_hfq"] / df["close"].replace(0, pd.NA))
            for c in ("open", "high", "low"):
                df[c + "_hfq"] = df[c] * ratio
            # 前复权（qfq）：锚定「本帧最新一根」，qfq = hfq × (最新 raw / 最新 hfq)。
            # 与 corporate_actions.AdjustmentEngine 的 qfq 口径一致（末根 factor=1）。
            _raw_last, _hfq_last = df["close"].iloc[-1], df["close_hfq"].iloc[-1]
            if pd.notna(_raw_last) and pd.notna(_hfq_last) and float(_hfq_last) != 0:
                _k = float(_raw_last) / float(_hfq_last)
                for c in ("open", "high", "low", "close"):
                    df[c + "_qfq"] = df[c + "_hfq"] * _k
                df["qfq_anchor"] = str(pd.Timestamp(df.index[-1]).date())
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
        # 「时间」为分钟线列名（含 HH:MM），「日期」为日/周/月线列名
        df = df.rename(columns={"日期": "day", "时间": "day", "开盘": "open", "收盘": "close",
                                "最高": "high", "最低": "low", "成交量": "volume",
                                "成交额": "amount"})
        df["date"] = pd.to_datetime(df["day"])
        df = df.set_index("date", drop=False)
        for c in ("open", "high", "low", "close", "volume", "amount"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    def fetch_kline(self, symbol: str, datalen: int = 120) -> pd.DataFrame:
        return self._fetch_kline(symbol, datalen=datalen)
