"""
valuation_models.py — 估值引擎（P0）

四条腿并行，输出**区间**而不是点估计：
    1. DCF（FCFF 两阶段）+ WACC × g 敏感性矩阵
    2. 反向 DCF：当前股价隐含的长期增长率  ★差异化功能
    3. 可比公司 Comps：PE / PB / PS / EV-EBITDA 四条隐含股价
    4. PB-ROE 与剩余收益（RI / EVA）

纪律（代码与 prompt 同步约束）
------------------------------
- 周期股禁用 PE 目标价 → 自动切 EV/EBITDA + PB 分位
- 金融股禁用 DCF 的 ΔNWC / Capex 假设 → 走 PB-ROE 与 DDM
- 亏损企业 PE = NaN → UI 显示 "—（亏损）"，绝不显示 0 或 -1
- 所有假设暴露在 key_assumptions，UI 可编辑，改动即重算
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .financial_quality import CYCLICALS, FINANCIALS

DISCLAIMER = "估值结果为模型输出，基于全部假设成立的前提；非投资建议，不构成收益承诺。"


def _safe(x, default=float("nan")) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


# ================================================================== WACC


def wacc(
    risk_free: float = 0.02,
    beta: float = 1.0,
    erp: float = 0.055,
    cost_of_debt: float = 0.035,
    tax_rate: float = 0.25,
    equity_value: float = 1.0,
    debt_value: float = 0.0,
    size_premium: float = 0.0,
) -> Dict[str, float]:
    """A 股 ERP 经验区间 5.0%–6.5%；无风险利率用 10Y 国债到期收益率。"""
    ke = risk_free + beta * erp + size_premium
    kd_after = cost_of_debt * (1 - tax_rate)
    total = max(equity_value + debt_value, 1e-9)
    we, wd = equity_value / total, debt_value / total
    return {
        "cost_of_equity": ke,
        "cost_of_debt_after_tax": kd_after,
        "wacc": ke * we + kd_after * wd,
        "weight_equity": we,
        "weight_debt": wd,
    }


# ================================================================== DCF


@dataclass
class DCFInputs:
    fcf0: float                      # 起始自由现金流（FCFF）
    growths: Sequence[float]         # 显式预测期逐年增速（长度 = 预测年数）
    wacc: float
    terminal_growth: float = 0.025
    net_debt: float = 0.0            # 有息负债 − 货币资金 − 交易性金融资产
    minority_interest: float = 0.0
    cash: float = 0.0                # 已包含在 net_debt 时置 0
    shares: float = 1.0


def fcff(ebit: float, tax_rate: float, da: float, capex: float, delta_nwc: float) -> float:
    """FCFF = EBIT×(1−t) + D&A − Capex − ΔNWC"""
    return ebit * (1 - tax_rate) + da - capex - delta_nwc


def dcf_two_stage(inp: DCFInputs) -> Dict[str, object]:
    if inp.wacc <= inp.terminal_growth:
        return {"error": f"WACC({inp.wacc:.2%}) 必须大于永续增长率({inp.terminal_growth:.2%})"}

    fcfs, pv_sum, fcf = [], 0.0, inp.fcf0
    for t, g in enumerate(inp.growths, start=1):
        fcf = fcf * (1 + g)
        disc = fcf / (1 + inp.wacc) ** t
        fcfs.append({"year": t, "growth": g, "fcff": fcf, "pv": disc})
        pv_sum += disc

    tv = fcf * (1 + inp.terminal_growth) / (inp.wacc - inp.terminal_growth)
    pv_tv = tv / (1 + inp.wacc) ** len(inp.growths)

    ev = pv_sum + pv_tv
    equity = ev - inp.net_debt - inp.minority_interest
    per_share = equity / inp.shares if inp.shares else float("nan")
    return {
        "explicit_fcff": fcfs,
        "pv_explicit": pv_sum,
        "terminal_value": tv,
        "pv_terminal": pv_tv,
        "pv_terminal_share": pv_tv / ev if ev else float("nan"),
        "enterprise_value": ev,
        "equity_value": equity,
        "value_per_share": per_share,
        "assumptions": {"wacc": inp.wacc, "terminal_growth": inp.terminal_growth,
                        "years": len(inp.growths)},
        "disclaimer": DISCLAIMER,
    }


def sensitivity_grid(
    inp: DCFInputs,
    wacc_range: Sequence[float],
    g_range: Sequence[float],
) -> pd.DataFrame:
    """WACC × 永续增长 敏感性矩阵。投委会唯一会盯的那张表。"""
    data = {}
    for g in g_range:
        row = []
        for w in wacc_range:
            mod = DCFInputs(**{**inp.__dict__, "wacc": w, "terminal_growth": g})
            r = dcf_two_stage(mod)
            row.append(r.get("value_per_share", float("nan")))
        data[f"g={g:.1%}"] = row
    return pd.DataFrame(data, index=[f"WACC={w:.2%}" for w in wacc_range]).T


# ================================================================== 反向 DCF


def reverse_dcf(
    price: float,
    inp: DCFInputs,
    lo: float = -0.10,
    hi: float = 0.40,
) -> Dict[str, object]:
    """
    当前股价隐含了多高的永续增长率？
    二分求解使 DCF 每股价值 == 当前价的 terminal_growth。
    输出"市场隐含预期"，比目标价更客观——这是专业与业余的分界线。
    """
    def value_at(g: float) -> float:
        mod = DCFInputs(**{**inp.__dict__, "terminal_growth": g})
        r = dcf_two_stage(mod)
        return _safe(r.get("value_per_share"), float("inf"))

    # 永续增长必须小于 WACC，否则模型无解；把上界收敛到 WACC 之内
    hi = min(hi, inp.wacc - 0.005)
    if hi <= lo:
        return {"error": f"WACC({inp.wacc:.2%}) 过低，反向 DCF 无解（请检查 WACC 与增长区间）"}

    f_lo, f_hi = value_at(lo), value_at(hi)
    if not (f_lo <= price <= f_hi):
        return {
            "implied_g": float("nan"),
            "note": f"当前价 {price:.2f} 不在 [{lo:.0%},{hi:.0%}] 增长区间对应的价值区间 "
                    f"[{f_lo:.2f}, {f_hi:.2f}] 内",
            "value_at_low": f_lo, "value_at_high": f_hi,
        }
    for _ in range(80):
        mid = (lo + hi) / 2
        if value_at(mid) < price:
            lo = mid
        else:
            hi = mid
    implied = (lo + hi) / 2
    return {"implied_g": implied, "price": price,
            "grid": {f"{g:.0%}": value_at(g) for g in (-0.05, 0.0, 0.05, 0.10, 0.15, 0.20)}}


# ================================================================== 可比公司


def comps(
    target: Dict[str, float],
    peers: pd.DataFrame,
    metrics: Sequence[str] = ("pe", "pb", "ps", "ev_ebitda"),
    use_median: bool = True,
) -> Dict[str, object]:
    """
    target: {"pe"/"pb"/... 对应的分子, 例如 {"eps": 2.5, "bps": 12.0, "sps": 30.0, "ebitda": 800}}
    peers: 列含 pe / pb / ps / ev_ebitda（可比公司，同申万二级行业）
    输出每组倍数对应的隐含股价，并用 中位数 ± 1×IQR 给区间。
    """
    out: Dict[str, object] = {}
    for m in metrics:
        if m not in peers.columns:
            continue
        s = pd.to_numeric(peers[m], errors="coerce")
        s = s[(s > 0) & (s < s.quantile(0.95) * 3)]      # 剔除负值（亏损企业）与极端值
        if len(s) < 3:
            continue
        med, q1, q3 = s.median(), s.quantile(0.25), s.quantile(0.75)
        centre = med if use_median else s.mean()
        num_key = {"pe": "eps", "pb": "bps", "ps": "sps", "ev_ebitda": "ebitda"}[m]
        num = _safe(target.get(num_key))
        if math.isnan(num):
            continue
        out[m] = {
            "peer_median": float(med),
            "peer_iqr": [float(q1), float(q3)],
            "implied_value_centre": float(centre * num),
            "implied_value_low": float(max(q1, med - (q3 - q1)) * num),
            "implied_value_high": float(min(q3, med + (q3 - q1)) * num),
            "peer_count": int(len(s)),
        }
    out["disclaimer"] = DISCLAIMER
    return out


# ================================================================== PB-ROE / 剩余收益


def implied_pb(roe: float, r: float, g: float) -> float:
    """理论 PB = (ROE − g) / (r − g)。用于快速交叉验证 Comps 的 PB。"""
    if r <= g:
        return float("nan")
    return (roe - g) / (r - g)


def residual_income(
    book_value: float,
    roe_forecasts: Sequence[float],
    r: float,
    terminal_growth: float = 0.02,
    fade_years: int = 10,
) -> Dict[str, object]:
    """
    RI 模型：V0 = BV0 + Σ (ROE_t − r) × BV_{t−1} / (1+r)^t
    相比 DCF 的优势：对短期现金流噪声不敏感，适合银行/保险与重资产。
    """
    bv, pv = book_value, 0.0
    rows = []
    for t, roe in enumerate(roe_forecasts, start=1):
        ni = bv * roe
        ri = ni - r * bv
        pv += ri / (1 + r) ** t
        rows.append({"year": t, "roe": roe, "net_income": ni, "ri": ri, "pv": ri / (1 + r) ** t})
        bv = bv + ni * (1 - 0.30)           # 简化：30% 分红率，剩余滚入净资产

    # 衰减：超出显式期后 RI 按 (1+g)/(1+r) 永续，并线性 fade
    last_roe = roe_forecasts[-1] if roe_forecasts else r
    cont_ri = bv * (last_roe - r)
    if r > terminal_growth:
        tv = cont_ri * (1 + terminal_growth) / (r - terminal_growth)
        pv += (tv / (1 + r) ** len(roe_forecasts)) / max(fade_years / 10.0, 0.5)
    return {"value": book_value + pv, "pv_ri": pv, "detail": rows,
            "disclaimer": DISCLAIMER}


# ================================================================== 统一入口


@dataclass
class ValuationContext:
    industry: str = ""
    price: float = float("nan")
    shares: float = 1.0
    book_value: float = float("nan")
    eps: float = float("nan")
    bps: float = float("nan")
    sps: float = float("nan")
    ebitda: float = float("nan")
    roe: float = float("nan")
    net_debt: float = 0.0
    minority: float = 0.0
    beta: float = 1.0
    risk_free: float = 0.02
    erp: float = 0.055
    cost_of_debt: float = 0.035
    tax_rate: float = 0.25
    equity_value: float = 1.0
    debt_value: float = 0.0


def run_all(
    ctx: ValuationContext,
    dcf_inp: Optional[DCFInputs] = None,
    peers: Optional[pd.DataFrame] = None,
    roe_forecasts: Optional[Sequence[float]] = None,
) -> Dict[str, object]:
    """四法并列 + 汇总区间。周期股/金融股自动切方法。"""
    cyclical = ctx.industry in CYCLICALS
    financial = ctx.industry in FINANCIALS
    res: Dict[str, object] = {"industry": ctx.industry,
                              "methods_used": [], "methods_skipped": []}

    w = wacc(ctx.risk_free, ctx.beta, ctx.erp, ctx.cost_of_debt, ctx.tax_rate,
             ctx.equity_value, ctx.debt_value)
    res["wacc"] = w

    # 1) DCF
    if dcf_inp is not None and not financial:
        res["dcf"] = dcf_two_stage(dcf_inp)
        res["sensitivity"] = sensitivity_grid(
            dcf_inp,
            [w["wacc"] - 0.02, w["wacc"] - 0.01, w["wacc"], w["wacc"] + 0.01, w["wacc"] + 0.02],
            [0.005, 0.015, 0.025, 0.035, 0.045],
        ).to_dict()
        res["reverse_dcf"] = reverse_dcf(ctx.price, dcf_inp)
        res["methods_used"].append("dcf")
    else:
        res["methods_skipped"].append("dcf（金融股或缺少现金流假设）")

    # 2) 可比
    if peers is not None and len(peers) > 0:
        metrics = ["ev_ebitda", "pb", "ps"] if cyclical else ["pe", "pb", "ps", "ev_ebitda"]
        res["comps"] = comps({"eps": ctx.eps, "bps": ctx.bps, "sps": ctx.sps,
                              "ebitda": ctx.ebitda}, peers, metrics)
        res["methods_used"].append(f"comps({','.join(metrics)})")
    if cyclical:
        res["methods_skipped"].append("PE 目标价（周期行业，改用 EV/EBITDA 与 PB 分位）")

    # 3) PB-ROE / RI
    if not math.isnan(ctx.roe) and not math.isnan(ctx.bps):
        res["pb_roe"] = {"implied_pb": implied_pb(ctx.roe, w["wacc"], 0.025),
                         "implied_value": implied_pb(ctx.roe, w["wacc"], 0.025) * ctx.bps}
        res["methods_used"].append("pb_roe")
    if roe_forecasts and not math.isnan(ctx.book_value):
        ri = residual_income(ctx.book_value, roe_forecasts, w["wacc"])
        # residual_income 返回的是权益总价值，统一折算为每股后再进入区间汇总
        ri["value_total"] = ri["value"]
        ri["value_per_share"] = ri["value"] / ctx.shares if ctx.shares else float("nan")
        res["residual_income"] = ri
        res["methods_used"].append("residual_income")

    # 4) 汇总区间
    vals: List[float] = []
    for k in ("dcf", "comps", "pb_roe", "residual_income"):
        node = res.get(k)
        if not isinstance(node, dict):
            continue
        if k == "dcf":
            vals.append(_safe(node.get("value_per_share")))
        elif k == "pb_roe":
            vals.append(_safe(node.get("implied_value")))
        elif k == "residual_income":
            vals.append(_safe(node.get("value_per_share")))
        elif k == "comps":
            for m, v in node.items():
                if isinstance(v, dict) and "implied_value_centre" in v:
                    vals.append(_safe(v["implied_value_centre"]))
    vals = [v for v in vals if not math.isnan(v) and v > 0]
    if vals:
        res["fair_value_range"] = [float(np.percentile(vals, 25)),
                                   float(np.median(vals)),
                                   float(np.percentile(vals, 75))]
        if not math.isnan(ctx.price) and ctx.price > 0:
            mid = float(np.median(vals))
            res["upside_to_mid"] = mid / ctx.price - 1.0
    res["disclaimer"] = DISCLAIMER
    res["caveats"] = _caveats(ctx, cyclical, financial)
    return res


def _caveats(ctx: ValuationContext, cyclical: bool, financial: bool) -> List[str]:
    c = []
    if financial:
        c.append("金融行业：DCF 的 ΔNWC/Capex 假设不适用，以 PB-ROE 与 DDM 为主")
    if cyclical:
        c.append("周期行业：盈利处周期位置时 PE 会严重失真，请以 EV/EBITDA 与 PB 历史分位为准")
    if ctx.beta > 1.5:
        c.append(f"Beta({ctx.beta:.2f}) 偏高，WACC 对 ERP 假设极敏感，请看敏感性矩阵")
    if not math.isnan(ctx.eps) and ctx.eps <= 0:
        c.append("每股收益为负，PE 无意义（UI 显示 —（亏损））")
    return c


if __name__ == "__main__":
    inp = DCFInputs(fcf0=1_000, growths=[0.15, 0.12, 0.10, 0.08, 0.06],
                    wacc=0.095, terminal_growth=0.025, net_debt=2_000, shares=500)
    ctx = ValuationContext(industry="电子", price=42.0, shares=500, book_value=9_000,
                           eps=2.4, bps=18.0, sps=24.0, ebitda=2_600, roe=0.133,
                           net_debt=2_000, beta=1.1, equity_value=21_000, debt_value=3_000)
    peers = pd.DataFrame({"pe": [18, 22, 25, 30, 16, 45], "pb": [2.2, 3.1, 2.8, 4.0, 1.9, 5.5],
                          "ps": [1.5, 2.0, 2.4, 3.0, 1.2, 3.6], "ev_ebitda": [9, 12, 14, 18, 8, 22]})
    out = run_all(ctx, inp, peers, roe_forecasts=[0.15, 0.14, 0.13, 0.12, 0.11])
    print("WACC:", round(out["wacc"]["wacc"], 4))
    print("DCF 每股:", round(out["dcf"]["value_per_share"], 2))
    print("反向DCF 隐含 g:", round(out["reverse_dcf"].get("implied_g", float('nan')), 4))
    print("汇总区间:", [round(v, 2) for v in out["fair_value_range"]])
    print("相对中值空间:", round(out["upside_to_mid"], 3))
    print("注意事项:", out["caveats"])
