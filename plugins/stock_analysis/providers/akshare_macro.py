# providers/akshare_macro.py — 中国宏观 EDB 免费源（akshare / 国家统计局口径）
#
# 背景：宏观 MACRO 类别原本只有 FMP `/economic`（需自备 key），未配置时
# `/api/macro` 恒 404。本 provider 用 akshare 免费源补齐 **中国**宏观，
# 使宏观页在零成本前提下出数。
#
# 红线（方案 §3）：
#   - 只新增 provider，不改 FMP 路径；country != CN 时本源明确不支持（不顶替他国数据）。
#   - 源没给的字段留空，不填假值；NaN 点直接丢弃而不是填 0。
#   - 不修改 K 线/财报等其它类别的任何行为。
#
# 实测基线（2026-09-19，akshare 1.18.94，本机）：
#   macro_china_cpi            224 行  最新 2026年08月份  全国-同比增长 0.8
#   macro_china_ppi            248 行  「当月同比增长」列
#   macro_china_pmi            224 行  「制造业-指数」列
#   macro_china_gdp             82 行  「国内生产总值-同比增长」列（季度）
#   macro_china_money_supply   224 行  「货币和准货币(M2)-同比增长」列
#   macro_china_shrzgm         136 行  「社会融资规模增量」列（月份形如 202602）
#   macro_china_gyzjz          205 行  「同比增长」列  最新 2026年08月份
#   macro_china_lpr           1575 行  「LPR1Y」列      最新 2026-08-20
#
# ★ 实测推翻了原方案的一条假设：方案写 CPI ← `macro_china_cpi_monthly`，
#   实测该接口是**金十事件流**（列：商品/日期/今值/预测值/前值），
#   最新仅到 2025-09-10 且「今值」为 NaN —— 源已停更，不能当 CPI 时序用。
#   真正的 CPI 月度过时序是 **`macro_china_cpi`**（无后缀，列：月份/全国-当月/全国-同比增长…）。
#   同理 `*_yearly`（cpi/ppi/pmi/gdp/m2）都是金十事件流，一律不用。
import re
import time

import pandas as pd

from .base import DataCategory, ProviderError
from .base_v2 import BaseProviderV2, FetchResult, ProviderUnavailable

import logging

_log = logging.getLogger("stock_analysis.akshare_macro")

# 本源只覆盖中国宏观
_CN_ALIASES = {"CN", "CHN", "CHINA", "ZH"}

# indicator → (akshare 函数名, 日期列, 值列, 单位, 频率)
# 值列一律取「同比/指数」等**可比口径**：宏观页看趋势，绝对值跨期不可比（如 GDP 绝对值是累计值）。
_SPEC = {
    "CPI": ("macro_china_cpi", "月份", "全国-同比增长", "%", "月"),
    "PPI": ("macro_china_ppi", "月份", "当月同比增长", "%", "月"),
    "PMI": ("macro_china_pmi", "月份", "制造业-指数", "指数", "月"),
    "GDP": ("macro_china_gdp", "季度", "国内生产总值-同比增长", "%", "季"),
    "M2": ("macro_china_money_supply", "月份", "货币和准货币(M2)-同比增长", "%", "月"),
    "SHRZGM": ("macro_china_shrzgm", "月份", "社会融资规模增量", "亿元", "月"),
    "INDUSTRIAL": ("macro_china_gyzjz", "月份", "同比增长", "%", "月"),
    "LPR": ("macro_china_lpr", "TRADE_DATE", "LPR1Y", "%", "日"),
}

_QTR_FULL = re.compile(r"(\d{4})年.*?第(\d)\s*-\s*(\d)季度")
_QTR_ONE = re.compile(r"(\d{4})年.*?第(\d)季度")
_YM_CN = re.compile(r"(\d{4})年(\d{1,2})月")
_YM_NUM = re.compile(r"^(\d{4})(\d{2})$")


def _unsupported(msg: str) -> ProviderUnavailable:
    """确定性失败（国家/指标不支持、源结构变了）：重试无意义，**且不可计入冷却**。

    ★ 必须抛 ProviderUnavailable 而非 ProviderError：BaseProviderV2.fetch 对前者
      原样透传、对后者会包装成新的 ProviderUnavailable 并丢掉 retryable 属性，
      gateway 侧 `getattr(err, 'retryable', True)` 会退回 True —— 于是「连切 3 次
      美国」就会把本源误冷却 300s，再切回中国也取不到数。
    """
    err = ProviderUnavailable(f"[akshare_macro/macro] {msg}")
    err.retryable = False
    return err


def _parse_date(v) -> str:
    """各种源日期 → YYYY-MM-DD；解析不了返回 ''。

    源格式混杂：'2026年08月份' / '2026年第1-3季度' / '202602' / date 对象 / '2026-08-20'。
    月频统一取 1 日，季度取末月 1 日（'1-3季度' → 09-01），保证可按日期升序排。
    """
    if v is None:
        return ""
    if hasattr(v, "isoformat") and not isinstance(v, str):   # date / datetime / Timestamp
        try:
            return str(v)[:10]
        except Exception:                                     # noqa: BLE001
            return ""
    txt = str(v).strip()
    if not txt:
        return ""
    m = _QTR_FULL.search(txt)
    if m:
        return f"{m.group(1)}-{int(m.group(3)) * 3:02d}-01"
    m = _QTR_ONE.search(txt)
    if m:
        return f"{m.group(1)}-{int(m.group(2)) * 3:02d}-01"
    m = _YM_CN.search(txt)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    m = _YM_NUM.match(txt)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    return txt[:10]


class AkshareMacroProvider(BaseProviderV2):
    name = "akshare_macro"
    authorized = False
    market = "CN"
    categories = frozenset({DataCategory.MACRO})
    rate_per_min = 20
    burst = 2

    # 前端下拉直接用（与 FMPProvider.MACRO_INDICATORS 同款约定）
    MACRO_INDICATORS = tuple(_SPEC.keys())

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if cat is not DataCategory.MACRO:
            raise NotImplementedError(cat)
        country = str(kw.get("country") or "CN").strip().upper()
        if country not in _CN_ALIASES:
            # 不是中国 → 本源明确不支持，留给 FMP 处理
            raise _unsupported(f"only CN supported (got {country})")
        indicator = str(kw.get("indicator") or "CPI").strip().upper()
        if indicator not in _SPEC:
            raise _unsupported(f"unsupported CN indicator: {indicator}")
        limit = int(kw.get("limit") or 240)

        df = self._fetch_macro(indicator, limit=limit)
        if df is None or df.empty:
            raise ProviderError(self.name, "macro",
                                f"empty series for {indicator}", retryable=False)
        return FetchResult(category=cat, data=df, source=self.name,
                           as_of=str(df.index.max().date()))

    # ── 内部实现 ────────────────────────────────────────
    @staticmethod
    def _import_ak():
        try:
            import akshare as ak
            return ak
        except ImportError as err:
            raise _unsupported(f"akshare not installed: {err}") from err

    def _fetch_macro(self, indicator: str, limit: int = 240) -> pd.DataFrame:
        """→ DataFrame(date 索引 + value 列)，升序；attrs 带溯源与口径。"""
        ak = self._import_ak()
        fn_name, date_col, val_col, unit, freq = _SPEC[indicator]
        fn = getattr(ak, fn_name, None)
        if fn is None:
            raise _unsupported(f"akshare missing {fn_name}")
        try:
            raw = fn()
        except Exception as err:                              # noqa: BLE001
            # 网络/上游抖动：可重试，计入冷却（与 K 线等类别同款处理）
            raise ProviderError(self.name, "macro",
                                f"{fn_name} failed: {type(err).__name__}: {err}")

        if raw is None or raw.empty or date_col not in raw.columns:
            raise _unsupported(
                f"{fn_name} bad frame: cols={list(getattr(raw, 'columns', []))[:6]}")
        if val_col not in raw.columns:
            # 值列名变了（上游改版）→ 明确报错，不猜列、不取第一列冒充
            raise _unsupported(f"{fn_name} value column '{val_col}' missing; "
                               f"got {list(raw.columns)[:8]}")

        recs = []
        for _, row in raw.iterrows():
            d = _parse_date(row.get(date_col))
            if len(d) != 10:
                continue
            v = row.get(val_col)
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v != v:                                        # NaN 点丢弃（不填 0）
                continue
            recs.append({"date": d, "value": v})

        # 源顺序不统一（CPI/PPI/PMI/GDP 倒序，社融/工业增加值/LPR 升序）→ 一律重排
        recs.sort(key=lambda x: x["date"])
        if limit and len(recs) > limit:
            recs = recs[-limit:]
        if not recs:
            raise _unsupported(f"no usable points for {indicator}")

        df = pd.DataFrame(recs)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        df.attrs.update({"source": self.name, "indicator": indicator,
                         "country": "CN", "unit": unit, "freq": freq,
                         "series": fn_name})
        _log.info("akshare_macro %s: %d pts, as_of=%s", indicator, len(df),
                  df.index.max().date())
        return df
