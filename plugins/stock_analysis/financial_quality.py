"""
financial_quality.py — 财务标准化 + TTM + 杜邦 + 盈利质量 + 舞弊筛查（P0）

解决的问题
----------
v1.7.1 的 evidence.py 把财报四表原始数据塞进 prompt，模型看到的是
"营业收入 3.27e9" 这种没有语义的数字。分析师真正要看的是：
    毛利率环比 +180bp、应收周转天数增加 12 天、OCF/NI 连续两年 <0.6
这些必须由确定性代码算出来，而不是让模型"读表"。

设计约束
--------
1. 全部函数纯计算、无 IO、可单测。
2. 所有比率对分母为 0 / 缺失返回 NaN，**绝不返回 0**（0 会被误读为"极好"）。
3. 金融行业（银行/券商/保险）走独立分支，屏蔽周转率、Altman Z、M-Score。
4. 周期行业（钢铁/化工/养殖/煤炭）标记 cyclical，估值端禁用 PE。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

FINANCIALS = {"银行", "证券", "保险", "多元金融", "BANK", "INSURANCE", "DIVERSIFIED_FINANCIALS"}
CYCLICALS = {"钢铁", "化工", "煤炭", "有色金属", "养殖", "农林牧渔", "建筑材料", "航运", "石油石化"}
NAN = float("nan")


def _d(x) -> float:
    """安全除法：分母为 0/None → NaN。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return NAN
    return v


def _r(num, den) -> float:
    n, d = _d(num), _d(den)
    if d == 0 or np.isnan(n) or np.isnan(d):
        return NAN
    return n / d


# ================================================================== TTM


def ttm(quarterly: pd.DataFrame, col: str, periods: int = 4) -> pd.Series:
    """
    滚动 TTM。quarterly 需按报告期升序，index 为 period（如 2025Q2）。
    利润表/现金流量表科目用滚动 4 季求和。
    """
    s = pd.to_numeric(quarterly[col], errors="coerce")
    return s.rolling(periods, min_periods=periods).sum()


def ttm_from_cumulative(rows: pd.DataFrame, col: str) -> pd.Series:
    """
    从累计值推导单季，再滚动 TTM（A 股财报是累计口径！）。
    单季 Q_n = 累计_n − 累计_{n-1}（同一年内）；Q1 = 累计 Q1。
    例：Q3 单季 = 三季报累计 − 中报累计。
    """
    df = rows.copy()
    df["_year"] = df["period"].astype(str).str[:4]
    df["_q"] = df["period"].astype(str).str[-1]
    df["_single"] = np.nan
    for y, g in df.groupby("_year"):
        g = g.sort_values("_q")
        prev = 0.0
        for i, (idx, r) in enumerate(g.iterrows()):
            cur = _d(r[col])
            df.at[idx, "_single"] = cur - prev if i > 0 else cur
            prev = cur
    return ttm(df.assign(**{col: df["_single"]}).sort_values("period"), col)


# ================================================================== 杜邦


def dupont(f: Dict[str, float]) -> Dict[str, float]:
    """五层杜邦拆解：ROE = 税负 × 利息负担 × 营业利润率 × 资产周转 × 权益乘数。"""
    ni = _d(f.get("net_profit"))
    rev = _d(f.get("revenue"))
    ta = _d(f.get("total_assets"))
    eq = _d(f.get("equity"))
    ebit = _d(f.get("ebit")) or _d(f.get("operating_profit")) or NAN
    ebt = _d(f.get("ebt")) or (ni + _d(f.get("tax") or 0.0))

    tax_burden = _r(ni, ebt)
    int_burden = _r(ebt, ebit)
    margin = _r(ebit, rev)
    turnover = _r(rev, ta)
    leverage = _r(ta, eq)
    roe = _r(ni, eq)

    return {
        "roe": roe,
        "tax_burden": tax_burden,
        "interest_burden": int_burden,
        "operating_margin": margin,
        "asset_turnover": turnover,
        "leverage": leverage,
        # 交叉校验：五项乘积应等于 ROE（容差 1e-6，写进单测）
        "check_product": (tax_burden * int_burden * margin * turnover * leverage
                          if not any(np.isnan(x) for x in
                                     (tax_burden, int_burden, margin, turnover, leverage)) else NAN),
    }


# ================================================================== 质量指标


@dataclass
class QualityReport:
    metrics: Dict[str, float] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)
    industry_note: str = ""


def quality_metrics(
    f: Dict[str, float],
    prev: Optional[Dict[str, float]] = None,
    industry: str = "",
    avg_total_assets: Optional[float] = None,
) -> QualityReport:
    """
    f 至少需要：revenue, net_profit, ocf(经营现金流净额), total_assets, equity,
                 cogs(营业成本), inventory, ar(应收账款), ap(应付账款),
                 ebit, interest_exp, total_debt(有息负债), goodwill, capex, da
    """
    rep = QualityReport()
    m = rep.metrics
    fin = industry in FINANCIALS

    rev, ni, ocf = _d(f.get("revenue")), _d(f.get("net_profit")), _d(f.get("ocf"))
    ta, eq = _d(f.get("total_assets")), _d(f.get("equity"))
    ata = _d(avg_total_assets) if avg_total_assets is not None else ta

    # ---- 盈利 ----
    m["gross_margin"] = _r(rev - _d(f.get("cogs")), rev)
    m["net_margin"] = _r(ni, rev)
    m["ebit_margin"] = _r(_d(f.get("ebit")), rev)

    # ---- 成长 ----
    if prev:
        m["revenue_yoy"] = _r(rev - _d(prev.get("revenue")), abs(_d(prev.get("revenue"))))
        m["profit_yoy"] = _r(ni - _d(prev.get("net_profit")), abs(_d(prev.get("net_profit"))))
        pgm = _r(_d(prev.get("revenue")) - _d(prev.get("cogs")), _d(prev.get("revenue")))
        if not np.isnan(pgm) and not np.isnan(m["gross_margin"]):
            m["gross_margin_delta_bp"] = (m["gross_margin"] - pgm) * 10_000

    # ---- 盈利质量（核心！）----
    m["ocf_to_ni"] = _r(ocf, ni)
    m["accruals_ratio"] = _r(ni - ocf, ata)          # Sloan 应计率
    m["cash_recovery"] = _r(_d(f.get("cash_from_sales")), rev)   # 收现比
    m["goodwill_to_equity"] = _r(_d(f.get("goodwill")), eq)

    # ---- 运营效率（金融股不适用）----
    if not fin:
        m["ar_days"] = _r(_d(f.get("ar")) * 365, rev)
        m["inv_days"] = _r(_d(f.get("inventory")) * 365, _d(f.get("cogs")) or rev)
        m["ap_days"] = _r(_d(f.get("ap")) * 365, _d(f.get("cogs")) or rev)
        if not any(np.isnan(m.get(k, NAN)) for k in ("ar_days", "inv_days", "ap_days")):
            m["ccc_days"] = m["ar_days"] + m["inv_days"] - m["ap_days"]
        m["asset_turnover"] = _r(rev, ata)

    # ---- 财务健康 ----
    m["debt_to_asset"] = _r(_d(f.get("total_liab")), ta)
    m["interest_coverage"] = _r(_d(f.get("ebit")), _d(f.get("interest_exp")))
    m["net_cash"] = (_d(f.get("cash")) + _d(f.get("fin_assets") or 0.0)) - _d(f.get("total_debt"))

    # ---- 金融股专属 ----
    if fin:
        m["roa"] = _r(ni, ata)
        for k in ("nim", "npl_ratio", "provision_coverage", "cet1"):
            if f.get(k) is not None:
                m[k] = _d(f[k])
        rep.industry_note = "金融行业：已屏蔽周转率/Altman Z/M-Score，改用 ROA/NIM/不良率/拨备/资本充足率"

    # ---- 预警 ----
    flags = rep.flags
    if not np.isnan(m.get("ocf_to_ni", NAN)) and m["ocf_to_ni"] < 0.6:
        flags.append("OCF/NI < 0.6：利润含金量偏低")
    if not np.isnan(m.get("accruals_ratio", NAN)) and m["accruals_ratio"] > 0.10:
        flags.append("应计率 > 10%：Sloan 应计异常，警惕盈余质量")
    if not np.isnan(m.get("goodwill_to_equity", NAN)) and m["goodwill_to_equity"] > 0.30:
        flags.append("商誉/净资产 > 30%：减值风险高")
    if not np.isnan(m.get("interest_coverage", NAN)) and 0 < m["interest_coverage"] < 2:
        flags.append("利息保障倍数 < 2：偿债压力较大")
    if not np.isnan(m.get("ar_days", NAN)) and prev:
        p_ar = _r(_d(prev.get("ar")) * 365, _d(prev.get("revenue")))
        if not np.isnan(p_ar) and m["ar_days"] - p_ar > 15:
            flags.append("应收周转天数同比增加 > 15 天：需核查收入确认政策")
    if industry in CYCLICALS:
        rep.industry_note += " | 周期行业：估值禁用 PE，改用 EV/EBITDA 与 PB 分位"
    return rep


# ================================================================== 舞弊筛查


def altman_z(f: Dict[str, float], is_private: bool = False) -> Optional[float]:
    """
    Altman Z-Score（仅非金融、非公用事业）。
    Z < 1.8 高危区；1.8–2.99 灰色区；> 2.99 安全区。
    """
    wc = _d(f.get("working_capital"))
    ta = _d(f.get("total_assets"))
    re_ = _d(f.get("retained_earnings"))
    ebit = _d(f.get("ebit"))
    eq = _d(f.get("equity"))
    liab = _d(f.get("total_liab"))
    rev = _d(f.get("revenue"))
    if ta == 0 or np.isnan(ta):
        return None
    z = (1.2 * _r(wc, ta) + 1.4 * _r(re_, ta) + 3.3 * _r(ebit, ta)
         + 0.6 * _r(eq, liab) + 1.0 * _r(rev, ta))
    return float(z)


def beneish_m(cur: Dict[str, float], prev: Dict[str, float]) -> Optional[Dict[str, float]]:
    """
    Beneish M-Score（8 变量）。M > -1.78 → 盈余操纵嫌疑。
    A 股建议：M > -1.5 **且** 连续两年上升 才报警，否则误报率极高。
    """
    def g(d, k):
        return _d(d.get(k))

    rev, rev_p = g(cur, "revenue"), g(prev, "revenue")
    ar, ar_p = g(cur, "ar"), g(prev, "ar")
    if rev_p == 0 or np.isnan(rev_p):
        return None

    dsri = _r(_r(ar, rev), _r(ar_p, rev_p))                      # 应收占收入比
    gm_c = _r(rev - g(cur, "cogs"), rev)
    gm_p = _r(rev_p - g(prev, "cogs"), rev_p)
    gmi = _r(gm_p, gm_c)                                          # 毛利率恶化
    ta_c, ta_p = g(cur, "total_assets"), g(prev, "total_assets")
    aqi = _r(_r(ta_c - g(cur, "ppe") - g(cur, "cash"), ta_c),
             _r(ta_p - g(prev, "ppe") - g(prev, "cash"), ta_p))   # 资产质量
    sgi = _r(rev, rev_p)                                          # 收入增长
    dep_c = _r(g(cur, "depreciation"), g(cur, "depreciation") + g(cur, "ppe"))
    dep_p = _r(g(prev, "depreciation"), g(prev, "depreciation") + g(prev, "ppe"))
    depi = _r(dep_p, dep_c)
    sgai = _r(_r(g(cur, "sga"), rev), _r(g(prev, "sga"), rev_p))
    lvgi = _r(_r(g(cur, "total_liab"), ta_c), _r(g(prev, "total_liab"), ta_p))
    tata = _r(g(cur, "net_profit") - g(cur, "ocf"), ta_c)

    vals = [dsri, gmi, aqi, sgi, depi, sgai, lvgi, tata]
    if any(np.isnan(v) for v in vals):
        return None
    m = (-4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
         + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)
    return {
        "m_score": float(m),
        "components": {"dsri": dsri, "gmi": gmi, "aqi": aqi, "sgi": sgi,
                       "depi": depi, "sgai": sgai, "lvgi": lvgi, "tata": tata},
        "verdict": high_risk_verdict(float(m)),
    }


def high_risk_verdict(m: float) -> str:
    if m > -1.50:
        return "高风险：存在盈余操纵嫌疑，建议回避或重点核查"
    if m > -1.78:
        return "偏警戒：接近 Beneish 阈值，需结合应计率与审计意见复核"
    return "未见明显操纵迹象"


# ================================================================== 标准化输出


def standardize(f: Dict[str, float], prev: Optional[Dict] = None,
                industry: str = "") -> Dict[str, object]:
    """对外唯一入口：返回可直接进证据包的标准化财务视图。"""
    q = quality_metrics(f, prev, industry)
    out: Dict[str, object] = {"metrics": q.metrics, "flags": q.flags, "dupont": {}}
    if industry not in FINANCIALS:
        out["dupont"] = dupont(f)
        z = altman_z(f)
        if z is not None:
            out["altman_z"] = z
        if prev:
            ms = beneish_m(f, prev)
            if ms:
                out["beneish"] = ms
    if q.industry_note:
        out["industry_note"] = q.industry_note
    return out


if __name__ == "__main__":
    cur = {
        "revenue": 12_000, "cogs": 7_200, "net_profit": 1_500, "ocf": 900,
        "total_assets": 20_000, "equity": 10_000, "total_liab": 10_000,
        "ebit": 1_900, "ebt": 1_800, "tax": 300, "ar": 3_000, "inventory": 2_000,
        "ap": 1_500, "cash": 2_000, "total_debt": 3_000, "goodwill": 500,
        "ppe": 6_000, "depreciation": 800, "sga": 1_200, "retained_earnings": 4_000,
        "interest_exp": 200,
        "working_capital": 3_500,           # 流动资产 − 流动负债（Altman Z 需要）
    }
    prev = {k: (v * 0.9 if isinstance(v, (int, float)) else v) for k, v in cur.items()}
    import json
    print(json.dumps(standardize(cur, prev, "电子"), ensure_ascii=False, indent=2, default=str))
