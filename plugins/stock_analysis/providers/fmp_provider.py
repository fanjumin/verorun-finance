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
        DataCategory.MACRO,          # 方案 §4.6 宏观 EDB：/economic 指标时序
    })

    # FMP economic 端点支持的常用指标（前端下拉直接用；不在表里的也允许手填，
    # 由上游决定是否返回数据，不做客户端臆造）
    MACRO_INDICATORS = (
        "GDP", "realGDP", "CPI", "coreCPI", "inflationRate", "PPI",
        "unemploymentRate", "nonfarmPayrolls", "initialClaims",
        "federalFundsRate", "treasuryRate", "consumerSentiment",
        "retailSales", "industrialProduction", "housingStarts",
    )

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

    @staticmethod
    def _canonical_sym(symbol: Optional[str]) -> str:
        """网关符号 → FMP REST 路径代码（v2.1.0 港股适配）。

        - US:AAPL / AAPL → ``AAPL``（美股原样）
        - HK:00700 / 00700 → ``0700.HK``（FMP 港股代码为 **4 位数字 + .HK**，
          与 Yahoo 同构；插件内部 UID 统一存 5 位，此处收敛为 FMP 形态）
        - 0700.HK 已是 FMP 形态，原样返回

        市场判定以 secmaster 为唯一权威；非数字代码或无法识别时退回大写裸码，
        绝不臆造后缀（6 位 A 股在 supports_category 已被拦截，不会进入取数）。
        """
        code = str(symbol or "").split(":")[-1].upper()
        if not code:
            return ""
        if code.isdigit():
            try:
                from ..secmaster import resolve_symbol
                sid = resolve_symbol(symbol)
            except Exception:                  # noqa: BLE001 — 解析失败按非港股处理
                sid = None
            if sid is not None and sid.market == "HK":
                return str(int(code)).zfill(4) + ".HK"
        return code

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
        # 宏观指标**无 symbol**，必须在下面的 symbol 校验之前分流（否则会被"需要 symbol"误杀）
        if cat == DataCategory.MACRO:
            return self._macro(kw.get("indicator") or "CPI",
                               country=kw.get("country") or "US",
                               start=kw.get("start"), end=kw.get("end"),
                               limit=int(kw.get("limit") or 240))
        sym = self._canonical_sym(symbol)
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

    # ---- 宏观 EDB（方案 §4.6）----

    def _macro(self, indicator: str, country: str = "US",
               start: Optional[str] = None, end: Optional[str] = None,
               limit: int = 240) -> FetchResult:
        """宏观经济指标时序：`GET /economic?name=<indicator>&country=<CC>`。

        与方案示例的差异（按 FMP 官方文档修正）：方案写 `/economic/{indicator}`，
        官方稳定形态是 `/economic?name=...`（带 name 参数并支持 from/to 日期区间），
        路径式端点不在 v3 文档内。这里按官方形态实现。

        返回统一为 DataFrame（date 索引 + value 列），空数据走 FetchResult.empty
        → gateway 不缓存、计入失败，前端显示"该指标无数据"。
        """
        p = {"apikey": self.secret(), "name": indicator, "country": country}
        if start:
            p["from"] = start
        if end:
            p["to"] = end
        try:
            raw = self._get_json(f"{BASE}/economic", p, timeout=20)
        except Exception as err:                       # noqa: BLE001 —— 上游异常统一包装
            raise ProviderUnavailable(f"FMP macro upstream: {type(err).__name__}: {err}")
        if isinstance(raw, dict) and raw.get("Error Message"):
            raise ProviderUnavailable(f"FMP: {raw['Error Message']}")

        # 兼容三种形态：顶层 list / {"data": [...]} / 单条指标记录 {date, value}
        if isinstance(raw, list):
            rows = raw
        elif isinstance(raw, dict):
            if isinstance(raw.get("data"), list) or isinstance(raw.get("historical"), list):
                rows = raw.get("data") or raw.get("historical")
            elif "date" in raw or "value" in raw:      # 单条记录（无 data 包装）
                rows = [raw]
            else:
                rows = []
        else:
            rows = []
        recs = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            d = r.get("date") or r.get("datetime") or r.get("period")
            v = r.get("value", r.get(indicator))
            if d is None or v is None:
                continue
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue
            recs.append({"date": str(d)[:10], "value": val})
        recs.sort(key=lambda x: x["date"])
        if limit and len(recs) > limit:
            recs = recs[-limit:]

        if not recs:
            return FetchResult(DataCategory.MACRO, pd.DataFrame(), "fmp", "",
                               warnings=["empty"], url=self._redact(f"{BASE}/economic?name={indicator}"),
                               params={"indicator": indicator, "country": country})
        df = pd.DataFrame(recs)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        # 溯源：gateway 只往外传 DataFrame，来源随数据走 attrs（路由读取后如实标注；
        # 若跨缓存回读丢失 attrs，路由会退化成 unknown，而不是编造来源）
        df.attrs.update({"source": self.name, "indicator": indicator, "country": country})
        return FetchResult(DataCategory.MACRO, df, "fmp", as_of=str(df.index.max().date()),
                           delay_seconds=86400,
                           url=self._redact(f"{BASE}/economic?name={indicator}"),
                           params={"indicator": indicator, "country": country},
                           cost_units=1.0)

    def get_macro(self, indicator: str, country: str = "US", **kw) -> FetchResult:
        """公开入口（方案 §4.6）：宏观指标时序，走统一的 fetch 模板（含埋点/令牌桶）。"""
        return self.fetch(DataCategory.MACRO, indicator=indicator, country=country, **kw)

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
