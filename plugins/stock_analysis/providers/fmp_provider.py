"""
providers/fmp_provider.py — Financial Modeling Prep Provider（国际基本面参考实现）

选型理由
--------
- FMP 是少数同时提供 **三表原始 + 标准化 ratios(TTM) + 分析师一致预期 + 目标价**
  的公网 REST 源，一份凭据覆盖 v2 的 FUNDAMENTAL / CONSENSUS / PROFILE / NEWS。
- 免费档 250 calls/day，付费档按套餐提升；配额用令牌桶收敛。
- 兜底链：FMP → Tiingo（价格）→ Finnhub（报价/新闻）→ 免费档。

落地步骤
--------
1. 放到 plugins/stock_analysis/providers/fmp_provider.py
2. gateway.ROUTE 增加：
       DataCategory.FUNDAMENTAL: ["tushare", "fmp"]        # A股走 tushare，美股走 fmp
       DataCategory.CONSENSUS:   ["fmp"]
       DataCategory.PROFILE:     ["fmp", "polygon"]
3. plugin.json settings 增加 fmp.api_key（password 类型，加密存储）
4. 数据源配置页"测试连接"调用 health() → GET /profile/AAPL

端点（v3，稳定）：
   /historical-price-full/{sym}?serietype=line
   /income-statement/{sym}?period=quarter&limit=8
   /balance-sheet-statement/{sym}?period=quarter&limit=8
   /cash-flow-statement/{sym}?period=quarter&limit=8
   /ratios-ttm/{sym}
   /analyst-estimates/{sym}?period=annual&limit=4
   /price-target-summary/{sym}
   /profile/{sym}
   /stock_news?tickers={sym}&limit=20
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

try:                                  # 作为插件模块加载
    from .base_v2 import (BaseProviderV2, DataCategory, FetchResult,
                          ProviderUnavailable, SecretResolver)
except ImportError:                   # 独立运行/单测
    from base_v2 import (BaseProviderV2, DataCategory, FetchResult,
                         ProviderUnavailable, SecretResolver)

BASE = "https://financialmodelingprep.com/api/v3"


class FMPProvider(BaseProviderV2):
    name = "fmp"
    market = "GLOBAL"                # 覆盖美股为主，也含部分港股/ADR
    required_secret = "api_key"
    rate_per_min = 60                # 免费档建议 30；付费按套餐调高
    burst = 8
    categories = frozenset({
        DataCategory.KLINE, DataCategory.FUNDAMENTAL, DataCategory.CONSENSUS,
        DataCategory.PROFILE, DataCategory.NEWS, DataCategory.QUOTE,
        DataCategory.FORECAST,
    })

    # ------------------------------------------------------------ 契约微调

    def supports_category(self, cat, symbol: Optional[str] = None) -> bool:
        """FMP 主要覆盖美股；A 股（6 位数字）应走 tushare，显式排除避免误路由。"""
        if not super().supports_category(cat, symbol):
            return False
        if symbol:
            code = symbol.split(":")[-1]
            if code.isdigit() and len(code) == 6:      # A 股代码
                return False
        return True

    # ------------------------------------------------------------ 工具

    def _url(self, path: str, **params) -> str:
        p = {"apikey": self.secret()}
        p.update({k: v for k, v in params.items() if v is not None})
        qs = "&".join(f"{k}={v}" for k, v in p.items())
        return self._redact(f"{BASE}/{path}?{qs}")

    def _call(self, path: str, **params) -> Any:
        """真实调用（内部保留密钥），回执里 url 已脱敏。"""
        p = {"apikey": self.secret()}
        p.update({k: v for k, v in params.items() if v is not None})
        data = self._get_json(f"{BASE}/{path}", p, timeout=20)
        if isinstance(data, dict) and data.get("Error Message"):
            raise ProviderUnavailable(f"FMP: {data['Error Message']}")
        return data

    # ------------------------------------------------------------ 实现

    def _do_fetch(self, cat: DataCategory, *, symbol: Optional[str] = None, **kw) -> FetchResult:
        sym = (symbol or "").split(":")[-1].upper()
        if not sym:
            raise ProviderUnavailable("FMP 需要 symbol")

        if cat == DataCategory.KLINE:
            return self._kline(sym, **kw)
        if cat == DataCategory.FUNDAMENTAL:
            return self._fundamental(sym, **kw)
        if cat == DataCategory.CONSENSUS:
            return self._consensus(sym, **kw)
        if cat == DataCategory.FORECAST:
            return self._price_target(sym)
        if cat == DataCategory.PROFILE:
            return self._profile(sym)
        if cat == DataCategory.NEWS:
            return self._news(sym, kw.get("limit", 20))
        if cat == DataCategory.QUOTE:
            return self._quote(sym)
        raise ProviderUnavailable(f"FMP 不支持 {cat.value}")

    # ---- K 线 ----

    def _kline(self, sym: str, start: Optional[str] = None, end: Optional[str] = None,
               limit: int = 1000, **_) -> FetchResult:
        # 注意：FMP 的历史端点用 `from` / `to`，不是 from_ / to_
        p = {"serietype": "line", "limit": limit}
        if start:
            p["from"] = start
        if end:
            p["to"] = end
        raw = self._call(f"historical-price-full/{sym}", **p)
        hist = (raw or {}).get("historical") or []
        if not hist:
            return FetchResult(DataCategory.KLINE, pd.DataFrame(), "fmp", "", warnings=["empty"])
        df = pd.DataFrame(hist)[["date", "open", "high", "low", "close", "volume"]]
        df = df.rename(columns={"volume": "vol"})
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index().astype(float)
        return FetchResult(DataCategory.KLINE, df, "fmp",
                           as_of=str(df.index.max()), delay_seconds=86400,
                           url=self._url(f"historical-price-full/{sym}"),
                           params={"symbol": sym}, cost_units=1.0)

    # ---- 财务三表 + TTM 比率 ----

    def _fundamental(self, sym: str, period: str = "quarter", limit: int = 12, **_) -> FetchResult:
        out: Dict[str, Any] = {}
        for key, path in (("income", "income-statement"),
                          ("balance", "balance-sheet-statement"),
                          ("cashflow", "cash-flow-statement")):
            try:
                out[key] = self._call(f"{path}/{sym}", period=period, limit=limit)
            except ProviderUnavailable:
                out[key] = []
        try:
            out["ratios_ttm"] = (self._call(f"ratios-ttm/{sym}") or [{}])[0]
        except ProviderUnavailable:
            out["ratios_ttm"] = {}

        as_of = ""
        inc = out.get("income") or []
        if inc and isinstance(inc[0], dict):
            as_of = inc[0].get("date", "")
        return FetchResult(DataCategory.FUNDAMENTAL, out, "fmp", as_of=as_of,
                           delay_seconds=86400, url=self._url(f"income-statement/{sym}"),
                           params={"symbol": sym, "period": period}, cost_units=3.0)

    # ---- 一致预期（预期差的核心）----

    def _consensus(self, sym: str, period: str = "annual", limit: int = 4, **_) -> FetchResult:
        data = self._call(f"analyst-estimates/{sym}", period=period, limit=limit)
        return FetchResult(DataCategory.CONSENSUS, data or [], "fmp",
                           as_of=(data[0].get("date", "") if data else ""),
                           delay_seconds=86400, url=self._url(f"analyst-estimates/{sym}"),
                           params={"symbol": sym}, cost_units=2.0)

    def _price_target(self, sym: str) -> FetchResult:
        data = self._call(f"price-target-summary/{sym}")
        return FetchResult(DataCategory.FORECAST, data or {}, "fmp", as_of="",
                           delay_seconds=86400, url=self._url(f"price-target-summary/{sym}"),
                           params={"symbol": sym}, cost_units=1.0)

    # ---- 公司概况 / 新闻 / 报价 ----

    def _profile(self, sym: str) -> FetchResult:
        data = self._call(f"profile/{sym}")
        row = (data or [{}])[0] if isinstance(data, list) else (data or {})
        return FetchResult(DataCategory.PROFILE, row, "fmp", as_of="",
                           delay_seconds=86400, url=self._url(f"profile/{sym}"),
                           params={"symbol": sym}, cost_units=1.0)

    def _news(self, sym: str, limit: int = 20) -> FetchResult:
        data = self._call("stock_news", tickers=sym, limit=limit)
        rows = [{"title": d.get("title"), "published": d.get("publishedDate"),
                 "source": d.get("site"), "url": d.get("url"),
                 "text": (d.get("text") or "")[:500]} for d in (data or [])]
        return FetchResult(DataCategory.NEWS, rows, "fmp",
                           as_of=(rows[0]["published"] if rows else ""),
                           delay_seconds=3600, url=self._url("stock_news", tickers=sym),
                           params={"symbol": sym}, cost_units=1.0)

    def _quote(self, sym: str) -> FetchResult:
        data = self._call(f"quote/{sym}")
        row = (data or [{}])[0] if isinstance(data, list) else (data or {})
        return FetchResult(DataCategory.QUOTE, row, "fmp",
                           as_of=str(row.get("timestamp", "")), delay_seconds=900,
                           url=self._url(f"quote/{sym}"), params={"symbol": sym},
                           cost_units=0.5)

    # ---- 健康探测 ----

    def health(self) -> dict:
        base = super().health()
        if not self.secret():
            base.update({"ok": False, "reason": "未配置 api_key"})
            return base
        try:
            d = self._call("profile/AAPL")
            base.update({"ok": bool(d), "latency_probe": "profile/AAPL"})
        except Exception as e:
            base.update({"ok": False, "reason": str(e)})
        return base


# ------------------------------------------------------------------ 字段映射


FMP_TO_CANONICAL = {
    # 用于 financial_quality.standardize 的字段映射（美股口径）
    "revenue": "revenue", "netIncome": "net_profit", "ebitda": "ebitda",
    "operatingIncome": "ebit", "incomeBeforeTax": "ebt", "incomeTaxExpense": "tax",
    "costOfRevenue": "cogs", "totalAssets": "total_assets",
    "totalStockholdersEquity": "equity", "totalLiabilities": "total_liab",
    "netCashProvidedByOperatingActivities": "ocf",
    "netReceivables": "ar", "inventory": "inventory",
    "accountPayables": "ap", "cashAndCashEquivalents": "cash",
    "totalDebt": "total_debt", "goodwill": "goodwill",
    "propertyPlantEquipmentNet": "ppe",
    "depreciationAndAmortization": "depreciation",
    "sellingGeneralAndAdministrativeExpenses": "sga",
    "retainedEarnings": "retained_earnings", "interestExpense": "interest_exp",
    "capital Expenditure": "capex",
}


def to_canonical(row: Dict[str, Any]) -> Dict[str, float]:
    """FMP 单期财报 → 标准化字段（供 financial_quality 消费）。"""
    out: Dict[str, float] = {}
    for src, dst in FMP_TO_CANONICAL.items():
        v = row.get(src)
        try:
            out[dst] = float(v) if v is not None else float("nan")
        except (TypeError, ValueError):
            out[dst] = float("nan")
    wc = row.get("totalCurrentAssets"), row.get("totalCurrentLiabilities")
    if all(x is not None for x in wc):
        out["working_capital"] = float(wc[0]) - float(wc[1])
    return out


if __name__ == "__main__":
    p = FMPProvider(secrets=SecretResolver(lambda n: None))
    print("health:", p.health())          # 无凭据时应返回 ok=False
    print("supports FUNDAMENTAL(AAPL):", p.supports(DataCategory.FUNDAMENTAL, "AAPL"))
    print("supports FUNDAMENTAL(600519):", p.supports(DataCategory.FUNDAMENTAL, "600519"))
