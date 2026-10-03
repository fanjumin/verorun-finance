# providers/tushare_provider.py — 授权主源（Tushare Pro，authorized=True）
# 命名避开官方包名 tushare；类名 TushareProvider。积分探针驱动 supports()，无权限类别自动裁剪。
import json
import logging
import time
from datetime import datetime, timedelta

import pandas as pd

from .. import tushare_client as tsc
from .base import DataCategory, ProviderError
from .base_v2 import BaseProviderV2, FetchResult

_log = logging.getLogger("stock_analysis.providers.tushare")

# K 线周期 → tushare 接口名（分钟线另有接口，P1 再评估）
_FREQ_API = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}
# 每根 bar 覆盖的自然日（含节假日余量），用于按周期放大回溯窗口
_LOOKBACK_PER_BAR = {"daily": 2.2, "weekly": 9.0, "monthly": 33.0}


def to_ts_code(symbol: str) -> str:
    """symbol → tushare ts_code。

    - 600519 / sh600519 → 600519.SH
    - sh000001 → 000001.SH（上证指数）；sz000001 / 000001 → 000001.SZ（平安银行）
    - 8xxxxx / bj920xxx → *.BJ（北交所）
    """
    s = symbol.strip().upper()
    pref = None
    if len(s) > 6 and s[:2] in ("SH", "SZ", "BJ"):
        pref, s = s[:2], s[2:]
    if not s.isdigit() or len(s) != 6:
        raise ProviderError("tushare", "symbol", f"invalid symbol: {symbol}")
    if pref == "BJ":
        return f"{s}.BJ"
    if pref == "SH":
        return f"{s}.SH"
    if pref == "SZ":
        return f"{s}.SZ"
    if s[0] in ("6", "5"):
        return f"{s}.SH"
    if s[0] in ("8", "4"):
        return f"{s}.BJ"
    return f"{s}.SZ"


class TushareProvider(BaseProviderV2):
    name = "tushare"
    authorized = True
    market = "CN"
    categories = frozenset({DataCategory.KLINE, DataCategory.FUNDAMENTAL,
                            DataCategory.MONEYFLOW, DataCategory.CONSENSUS,
                            DataCategory.TOPLIST, DataCategory.MARGIN,
                            DataCategory.NORTHBOUND, DataCategory.SHAREFLOAT,
                            DataCategory.HOLDERNUMBER})
    kline_freqs = frozenset({"daily", "weekly", "monthly"})
    rate_per_min = 120
    burst = 4
    required_secret = "token"

    @classmethod
    def supports(cls) -> set:
        """动态叠加积分探针结果：无 token / 无权限类别自动裁剪，gateway 据此跳过。"""
        caps = tsc.probe_capabilities()
        mapping = {
            "kline": DataCategory.KLINE,
            "fundamental": DataCategory.FUNDAMENTAL,
            "moneyflow": DataCategory.MONEYFLOW,
            "consensus": DataCategory.CONSENSUS,
            "toplist": DataCategory.TOPLIST,
            "margin": DataCategory.MARGIN,
            "northbound": DataCategory.NORTHBOUND,
            "sharefloat": DataCategory.SHAREFLOAT,
            "holdernumber": DataCategory.HOLDERNUMBER,
        }
        return {mapping[k] for k in caps if k in mapping}

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if cat is DataCategory.KLINE:
            data = self._fetch_kline(symbol, datalen=kw.get("datalen", 120),
                                     freq=kw.get("freq", "daily"))
        elif cat is DataCategory.FUNDAMENTAL:
            data = self._fetch_fundamental(symbol, periods=kw.get("periods", 8))
        elif cat is DataCategory.MONEYFLOW:
            data = self._fetch_moneyflow(symbol, days=kw.get("days", 5))
        elif cat is DataCategory.CONSENSUS:
            data = self._fetch_consensus(symbol)
        elif cat is DataCategory.TOPLIST:
            data = self._fetch_toplist(symbol, **kw)
        elif cat is DataCategory.MARGIN:
            data = self._fetch_margin(symbol, **kw)
        elif cat is DataCategory.NORTHBOUND:
            data = self._fetch_northbound(symbol, **kw)
        elif cat is DataCategory.SHAREFLOAT:
            data = self._fetch_sharefloat(symbol, **kw)
        elif cat is DataCategory.HOLDERNUMBER:
            data = self._fetch_holdernumber(symbol, **kw)
        else:
            raise NotImplementedError(cat)
        return FetchResult(category=cat, data=data, source=self.name,
                           as_of=time.strftime("%Y-%m-%dT%H:%M:%S"))

    def _pro(self):
        try:
            return tsc.get_pro()
        except Exception as err:
            raise ProviderError(self.name, "token", str(err))

    def _fetch_kline(self, symbol: str, datalen: int = 120,
                     freq: str = "daily") -> pd.DataFrame:
        api = _FREQ_API.get(freq)
        if api is None:
            raise ProviderError(self.name, "kline", f"tushare 未接入周期 {freq}",
                                retryable=False)
        code = to_ts_code(symbol)
        end = datetime.now()
        # 周/月线每根覆盖多个自然日，窗口须按周期放大，否则返回 bars 远少于 datalen
        start = end - timedelta(days=int(datalen * _LOOKBACK_PER_BAR[freq]) + 90)
        try:
            df = getattr(self._pro(), api)(
                ts_code=code,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"))
        except Exception as err:
            raise ProviderError(self.name, "kline", str(err))
        if df is None or df.empty:
            raise ProviderError(self.name, "kline", "empty", retryable=False)
        df = df.sort_values("trade_date")
        df = df.rename(columns={"vol": "volume"})
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")

        adj = self._adj_factor(code, start, end)
        if adj is not None and not adj.empty:
            df = df.merge(adj, on="trade_date", how="left")
            df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce").bfill()
            for c in ("open", "high", "low", "close"):
                df[c + "_hfq"] = df[c] * df["adj_factor"]
            df["price_basis"] = "hfq"
        else:
            _log.warning("adj_factor unavailable for %s, indicators on unadjusted prices", code)
            for c in ("open", "high", "low", "close"):
                df[c + "_hfq"] = df[c]
            df["price_basis"] = "raw"

        df["day"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        df["date"] = pd.to_datetime(df["day"])
        df = df.set_index("date")
        return df.dropna(subset=["open", "high", "low", "close"]).tail(datalen)

    def _adj_factor(self, code: str, start, end):
        try:
            adj = self._pro().adj_factor(
                ts_code=code,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"))
            if adj is None or adj.empty:
                return None
            adj = adj.sort_values("trade_date")
            return adj[["trade_date", "adj_factor"]]
        except Exception as err:
            _log.warning("adj_factor fetch failed %s: %s", code, err)
            return None

    def _fetch_fundamental(self, symbol: str, periods: int = 8) -> dict:
        code = to_ts_code(symbol)
        pro = self._pro()
        calls = (
            ("income", lambda: pro.income(ts_code=code, report_type=1)),
            ("balance", lambda: pro.balancesheet(ts_code=code, report_type=1)),
            ("cashflow", lambda: pro.cashflow(ts_code=code, report_type=1)),
            ("fina_indicator", lambda: pro.fina_indicator(ts_code=code)),
        )
        out = {}
        for name, call in calls:
            try:
                df = call()
            except Exception as err:
                raise ProviderError(self.name, "fundamental", f"{name} failed: {err}")
            out[name] = (json.loads(df.head(periods).to_json(orient="records"))
                         if df is not None and not df.empty else [])
        return out

    def _fetch_moneyflow(self, symbol: str, days: int = 5) -> pd.DataFrame:
        code = to_ts_code(symbol)
        end = datetime.now()
        start = end - timedelta(days=days * 2 + 30)
        try:
            df = self._pro().moneyflow(
                ts_code=code,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"))
        except Exception as err:
            raise ProviderError(self.name, "moneyflow", str(err))
        if df is None or df.empty:
            raise ProviderError(self.name, "moneyflow", "empty", retryable=False)
        df = df.sort_values("trade_date").tail(days)
        df["day"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        df["date"] = pd.to_datetime(df["day"])
        return df.set_index("date")

    def _fetch_consensus(self, symbol: str) -> dict:
        """A 股业绩预告（业绩预告 → 一致预期的 A 股等价物）。

        Tushare 接口：forecast_vip（高积分）。
        返回 dict：{period, net_profit_min, net_profit_max, summary, ...}。
        """
        code = to_ts_code(symbol)
        pro = self._pro()
        try:
            df = pro.forecast_vip(ts_code=code)
        except AttributeError:
            raise ProviderError(self.name, "consensus",
                                "forecast_vip 接口不可用（积分不足）", retryable=False)
        except Exception as err:
            raise ProviderError(self.name, "consensus", str(err))
        if df is None or df.empty:
            raise ProviderError(self.name, "consensus", "empty", retryable=False)
        latest = df.sort_values("end_date", ascending=False).iloc[0]
        result = {"source": "tushare", "ts_code": code}
        for col in ("end_date", "ann_date", "net_profit_min", "net_profit_max",
                     "net_profit", "basic_eps_min", "basic_eps_max",
                     "summary", "change_reason"):
            val = latest.get(col)
            if val is not None and str(val) != "nan":
                result[col] = val
        if not any(k in result for k in ("net_profit_min", "net_profit_max", "basic_eps_min")):
            raise ProviderError(self.name, "consensus", "no estimate fields", retryable=False)
        return result

    def _fetch_toplist(self, symbol: str, **kw) -> list:
        """龙虎榜。"""
        from ..ashare_special import fetch_top_list
        data = fetch_top_list(symbol, kw.get("start_date"), kw.get("end_date"))
        if not data:
            raise ProviderError(self.name, "toplist", "empty", retryable=False)
        return data

    def _fetch_margin(self, symbol: str, **kw) -> dict:
        """融资融券。"""
        from ..ashare_special import fetch_margin, fetch_margin_detail
        records = fetch_margin(symbol, kw.get("start_date"), kw.get("end_date"))
        if not records:
            raise ProviderError(self.name, "margin", "empty", retryable=False)
        latest = fetch_margin_detail(symbol)
        return {"records": records, "latest": latest}

    def _fetch_northbound(self, symbol: str, **kw) -> list:
        """北向资金（个股不在北向接口中，返回市场级别数据）。"""
        from ..ashare_special import fetch_northbound_flow
        data = fetch_northbound_flow(kw.get("start_date"), kw.get("end_date"))
        if not data:
            raise ProviderError(self.name, "northbound", "empty", retryable=False)
        return data

    def _fetch_sharefloat(self, symbol: str, **kw) -> list:
        """限售股解禁。"""
        from ..ashare_special import fetch_share_float
        data = fetch_share_float(symbol, kw.get("start_date"), kw.get("end_date"))
        if not data:
            raise ProviderError(self.name, "sharefloat", "empty", retryable=False)
        return data

    def _fetch_holdernumber(self, symbol: str, **kw) -> list:
        """股东户数。"""
        from ..ashare_special import fetch_holder_number
        data = fetch_holder_number(symbol, kw.get("start_date"), kw.get("end_date"))
        if not data:
            raise ProviderError(self.name, "holdernumber", "empty", retryable=False)
        return data

    def fetch_kline(self, symbol: str, datalen: int = 120) -> pd.DataFrame:
        return self._fetch_kline(symbol, datalen=datalen)

    def fetch_fundamental(self, symbol: str, periods: int = 8) -> dict:
        return self._fetch_fundamental(symbol, periods=periods)

    def fetch_moneyflow(self, symbol: str, days: int = 5, **kwargs) -> pd.DataFrame:
        return self._fetch_moneyflow(symbol, days=days)
