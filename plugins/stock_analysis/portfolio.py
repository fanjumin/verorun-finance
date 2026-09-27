# portfolio.py — 组合分析：持仓导入、行业暴露、集中度、Beta/VaR、Brinson 归因
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np
import pandas as pd

_log = logging.getLogger("stock_analysis.portfolio")


@dataclass
class Holding:
    symbol: str
    weight: float
    shares: int = 0
    cost: float = 0.0
    market_value: float = 0.0


@dataclass
class Portfolio:
    name: str = ""
    holdings: list[Holding] = field(default_factory=list)
    benchmark: str = ""
    cash_weight: float = 0.0

    @property
    def symbols(self) -> list[str]:
        return [h.symbol for h in self.holdings]

    @property
    def weights(self) -> np.ndarray:
        return np.array([h.weight for h in self.holdings])

    @property
    def total_weight(self) -> float:
        return sum(h.weight for h in self.holdings) + self.cash_weight


def parse_holdings(raw: list[dict]) -> list[Holding]:
    """从 dict 列表解析持仓。支持 {symbol, weight} 或 {symbol, shares, price} 格式。"""
    holdings = []
    for item in raw:
        sym = item.get("symbol", "").strip()
        if not sym:
            continue
        w = item.get("weight")
        if w is None and "shares" in item and "price" in item:
            mv = item["shares"] * item["price"]
            total_mv = sum(r.get("shares", 0) * r.get("price", 0) for r in raw)
            w = mv / total_mv if total_mv > 0 else 0
        if w is None:
            continue
        holdings.append(Holding(
            symbol=sym,
            weight=float(w),
            shares=int(item.get("shares", 0)),
            cost=float(item.get("cost", 0)),
            market_value=float(item.get("market_value", 0)),
        ))
    total = sum(h.weight for h in holdings)
    if total > 0 and abs(total - 1.0) > 0.01:
        for h in holdings:
            h.weight /= total
    return holdings


def industry_exposure(portfolio: Portfolio,
                      industry_map: dict[str, str]) -> pd.DataFrame:
    """行业暴露表。industry_map: {symbol: industry_name}。

    返回 DataFrame(columns=[industry, weight, count])，按 weight 降序。
    """
    rows = []
    for h in portfolio.holdings:
        ind = industry_map.get(h.symbol, "未知")
        rows.append({"industry": ind, "weight": h.weight, "symbol": h.symbol})
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["industry", "weight", "count"])
    grouped = df.groupby("industry").agg(
        weight=("weight", "sum"),
        count=("symbol", "count"),
    ).sort_values("weight", ascending=False).reset_index()
    return grouped


def concentration_metrics(weights: np.ndarray) -> dict:
    """集中度指标：HHI、前 N 大权重、有效持仓数。"""
    w = np.array(weights, dtype=float)
    w = w[w > 0]
    if len(w) == 0:
        return {"hhi": 0, "top5": 0, "top10": 0, "effective_n": 0, "max_weight": 0}
    w_sorted = np.sort(w)[::-1]
    hhi = float(np.sum(w_sorted ** 2))
    effective_n = float(1.0 / hhi) if hhi > 0 else 0
    return {
        "hhi": round(hhi, 6),
        "top5": round(float(w_sorted[:5].sum()), 4),
        "top10": round(float(w_sorted[:10].sum()), 4),
        "effective_n": round(effective_n, 1),
        "max_weight": round(float(w_sorted[0]), 4),
        "max_symbol_idx": int(np.argmax(w_sorted)),
        "n_holdings": len(w),
    }


def portfolio_beta(returns: pd.DataFrame, weights: np.ndarray,
                   benchmark_returns: pd.Series,
                   window: int = 252) -> dict:
    """组合 Beta 与系统性风险指标。

    returns: DataFrame(index=date, columns=symbols) 个股日收益率
    weights: 等长 array 各标的权重
    benchmark_returns: 基准日收益率序列
    """
    aligned = returns.dropna(axis=1, how="all").tail(window)
    symbols = aligned.columns.tolist()
    w = np.zeros(len(returns.columns))
    for i, sym in enumerate(returns.columns):
        if sym in symbols:
            idx = symbols.index(sym) if sym in symbols else -1

    w_map = {sym: weights[i] for i, sym in enumerate(returns.columns)}
    w_aligned = np.array([w_map.get(s, 0) for s in aligned.columns])
    if w_aligned.sum() > 0:
        w_aligned /= w_aligned.sum()

    port_ret = (aligned * w_aligned).sum(axis=1)
    bench = benchmark_returns.reindex(port_ret.index).dropna()
    port_ret = port_ret.reindex(bench.index)

    cov = np.cov(port_ret.values, bench.values)
    beta = cov[0, 1] / cov[1, 1] if cov[1, 1] > 0 else np.nan
    alpha_annual = (port_ret.mean() - beta * bench.mean()) * 252
    tracking_error = (port_ret - bench).std() * np.sqrt(252)
    information_ratio = (port_ret.mean() - bench.mean()) * 252 / tracking_error if tracking_error > 0 else np.nan

    return {
        "beta": round(float(beta), 4),
        "alpha_annual": round(float(alpha_annual), 4),
        "tracking_error": round(float(tracking_error), 4),
        "information_ratio": round(float(information_ratio), 4),
        "port_vol_annual": round(float(port_ret.std() * np.sqrt(252)), 4),
        "bench_vol_annual": round(float(bench.std() * np.sqrt(252)), 4),
    }


# ── 正态分布分位/密度：手写实现，去掉 scipy 硬依赖 ─────────────────────
# 设计理由：scipy 在本项目中始终是"可选加速件"（对照 factor_lab._spearman 的
# try/except 降级）。而 parametric / cornish_fisher VaR 此前硬 import scipy，
# 使随包 venv（未装 scipy）的 /api/portfolio/analyze 整体 500 —— 功能全废。

def _norm_pdf(x: float) -> float:
    """标准正态概率密度 φ(x)。"""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_ppf(p: float) -> float:
    """标准正态分位函数 Φ⁻¹(p)。Acklam 有理逼近，绝对误差 < 1.15e-9。

    覆盖 p ∈ (0,1)，采用 p_low/p_high 三段式（尾部用有理式避免溢出）。
    """
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
          1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
          6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    p_low, p_high = 0.02425, 1.0 - 0.02425
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p > p_high:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


def portfolio_var(returns: pd.DataFrame, weights: np.ndarray,
                  confidence: float = 0.95,
                  method: str = "historical") -> dict:
    """组合 VaR / CVaR(ES)。method: historical / parametric / cornish_fisher。

    口径（写进 docstring 以便审计）：
    - ``var``  : 左尾分位数（负值 = 损失）
    - ``cvar`` : 左尾条件均值（Expected Shortfall），**不再等于 var**
    - ``degenerate``: 样本 < 30 或 σ ≤ 0 时为 True，此时 var=cvar=μ（如实降级，
      不返回 NaN，也不伪造尾部估计）
    """
    w = np.array(weights, dtype=float)
    aligned = returns.dropna(axis=1, how="all")
    w_aligned = np.array([w[i] for i, sym in enumerate(returns.columns)
                          if sym in aligned.columns])
    clean = aligned[[s for s in aligned.columns if s in returns.columns]]
    alpha = 1.0 - confidence
    if clean.empty or len(w_aligned) == 0:
        return {"var": np.nan, "cvar": np.nan, "method": method,
                "confidence": confidence, "n_obs": 0, "degenerate": True}

    port_ret = (clean.values * w_aligned[:len(clean.columns)]).sum(axis=1)
    port_ret = port_ret[np.isfinite(port_ret)]
    mu = float(port_ret.mean()) if port_ret.size else 0.0
    sigma = float(port_ret.std(ddof=1)) if port_ret.size > 1 else 0.0
    degenerate = (port_ret.size < 30) or (not np.isfinite(sigma)) or sigma <= 0.0

    if method == "historical":
        var = float(np.percentile(port_ret, alpha * 100)) if port_ret.size else mu
        tail = port_ret[port_ret <= var]
        cvar = float(tail.mean()) if tail.size else var
    elif method in ("parametric", "cornish_fisher"):
        if degenerate:
            var = cvar = mu
        else:
            z = _norm_ppf(alpha)
            if method == "cornish_fisher":
                zs = (port_ret - mu) / sigma
                s = float((zs ** 3).mean())          # 偏度
                k = float((zs ** 4).mean()) - 3.0    # 超额峰度

                # Cornish-Fisher 展开：把正态分位修正到偏度/峰度后的真实分位
                def _cf(v):
                    return (v + (v ** 2 - 1) * s / 6 + (v ** 3 - 3 * v) * k / 24
                            - (2 * v ** 3 - 5 * v) * s ** 2 / 36)

                z_cf = float(_cf(z))
                # CVaR 用同一 CF 变换后的经验分位尾部均值，保证与 VaR 自洽
                z_cf_all = np.sort(_cf(zs))
                tail = z_cf_all[z_cf_all <= z_cf]
                cvar = float(mu + sigma * (tail.mean() if tail.size else z_cf))
                z = z_cf
            else:
                cvar = float(mu - sigma * _norm_pdf(z) / alpha)
            var = float(mu + z * sigma)
    else:
        raise ValueError(f"unknown VaR method: {method}")

    return {"var": round(var, 6), "cvar": round(cvar, 6), "method": method,
            "confidence": confidence, "n_obs": int(port_ret.size),
            "degenerate": degenerate}


def brinson_attribution(port_returns: pd.Series, port_industries: pd.Series,
                        bench_returns: pd.Series, bench_industries: pd.Series,
                        port_weights: Optional[pd.Series] = None,
                        bench_weights: Optional[pd.Series] = None) -> dict:
    """Brinson-Fachler 归因。

    将超额收益分解为：
    - 配置效应 (Allocation)：行业权重偏离 × 行业基准收益
    - 选择效应 (Selection)：行业权重 × (组合行业收益 - 基准行业收益)
    - 交互效应 (Interaction)：权重偏离 × 行业收益偏离

    port_returns / bench_returns: Series(index=symbol, values=return)
    port_industries / bench_industries: Series(index=symbol, values=industry)
    port_weights / bench_weights: Series(index=symbol, values=weight)
    """
    if port_weights is None:
        port_weights = pd.Series(1.0 / len(port_returns), index=port_returns.index)
    if bench_weights is None:
        bench_weights = pd.Series(1.0 / len(bench_returns), index=bench_returns.index)

    industries = sorted(set(port_industries.unique()) | set(bench_industries.unique()))

    sector_port_ret = {}
    sector_bench_ret = {}
    sector_port_w = {}
    sector_bench_w = {}

    for ind in industries:
        p_syms = port_industries[port_industries == ind].index
        b_syms = bench_industries[bench_industries == ind].index

        pw = port_weights.reindex(p_syms).fillna(0)
        bw = bench_weights.reindex(b_syms).fillna(0)
        pr = port_returns.reindex(p_syms).fillna(0)
        br = bench_returns.reindex(b_syms).fillna(0)

        sector_port_w[ind] = pw.sum()
        sector_bench_w[ind] = bw.sum()
        sector_port_ret[ind] = (pw * pr).sum() / pw.sum() if pw.sum() > 0 else 0
        sector_bench_ret[ind] = (bw * br).sum() / bw.sum() if bw.sum() > 0 else 0

    total_port_ret = sum(sector_port_w.get(ind, 0) * sector_port_ret.get(ind, 0) for ind in industries)
    total_bench_ret = sum(sector_bench_w.get(ind, 0) * sector_bench_ret.get(ind, 0) for ind in industries)

    rows = []
    total_alloc = 0
    total_select = 0
    total_interact = 0

    for ind in industries:
        wp = sector_port_w.get(ind, 0)
        wb = sector_bench_w.get(ind, 0)
        rp = sector_port_ret.get(ind, 0)
        rb = sector_bench_ret.get(ind, 0)

        alloc = (wp - wb) * rb
        select = wb * (rp - rb)
        interact = (wp - wb) * (rp - rb)

        total_alloc += alloc
        total_select += select
        total_interact += interact

        rows.append({
            "industry": ind,
            "port_weight": round(wp, 4),
            "bench_weight": round(wb, 4),
            "port_return": round(float(rp), 6),
            "bench_return": round(float(rb), 6),
            "allocation": round(float(alloc), 6),
            "selection": round(float(select), 6),
            "interaction": round(float(interact), 6),
            "total_effect": round(float(alloc + select + interact), 6),
        })

    return {
        "sectors": rows,
        "summary": {
            "total_allocation": round(float(total_alloc), 6),
            "total_selection": round(float(total_select), 6),
            "total_interaction": round(float(total_interact), 6),
            "total_active": round(float(total_alloc + total_select + total_interact), 6),
            "port_return": round(float(total_port_ret), 6),
            "bench_return": round(float(total_bench_ret), 6),
        },
    }


def full_report(portfolio: Portfolio, gateway, datalen: int = 250) -> dict:
    """一键生成组合分析报告。

    包含：
    1. 持仓概览 + 集中度
    2. 行业暴露
    3. Beta / 跟踪误差
    4. VaR / CVaR (historical + parametric + cornish_fisher)
    5. Brinson 归因（vs benchmark）
    """
    from .gateway import DataGateway
    from .models_sa import get_classification

    symbols = portfolio.symbols
    panels = {}
    for sym in symbols:
        try:
            df = gateway.get_kline(sym, datalen=datalen)
            if df is not None and not df.empty:
                panels[sym] = df
        except Exception as err:
            _log.warning("kline fetch failed for %s: %s", sym, err)

    if not panels:
        return {"error": "no data fetched for any holding"}

    ret_series = {}
    for sym, df in panels.items():
        col = "close_hfq" if "close_hfq" in df.columns else "close"
        s = pd.to_numeric(df[col], errors="coerce")
        ret_series[sym] = s.pct_change()
    returns = pd.DataFrame(ret_series).sort_index()

    weights = np.array([portfolio.holdings[i].weight
                        for i, sym in enumerate(portfolio.symbols)
                        if sym in returns.columns])
    valid_syms = [sym for sym in portfolio.symbols if sym in returns.columns]
    if weights.sum() > 0:
        weights = weights / weights.sum()

    ind_map = {}
    for sym in valid_syms:
        cls = get_classification(sym, standard="sw")
        ind_map[sym] = cls.get("industry_l2", "未知") if cls else "未知"

    bench_symbol = portfolio.benchmark or "000300"
    try:
        bench_df = gateway.get_kline(bench_symbol, datalen=datalen)
        bench_col = "close_hfq" if "close_hfq" in bench_df.columns else "close"
        bench_ret = pd.to_numeric(bench_df[bench_col], errors="coerce").pct_change()
    except Exception:
        bench_ret = returns.mean(axis=1)

    port_ret = (returns[valid_syms] * weights).sum(axis=1)

    concentration = concentration_metrics(weights)
    exposure = industry_exposure(portfolio, ind_map)
    beta_info = portfolio_beta(returns[valid_syms], weights, bench_ret)
    var_hist = portfolio_var(returns[valid_syms], weights, method="historical")
    var_param = portfolio_var(returns[valid_syms], weights, method="parametric")
    var_cf = portfolio_var(returns[valid_syms], weights, method="cornish_fisher")

    bench_ind_map = {}
    try:
        from .providers.commons import market_symbol
        bench_syms = valid_syms
        bench_ind_map = ind_map
    except Exception:
        bench_ind_map = ind_map

    port_w_series = pd.Series({sym: w for sym, w in zip(valid_syms, weights)})
    bench_w_series = pd.Series(1.0 / len(valid_syms), index=valid_syms)

    port_ret_last = returns[valid_syms].iloc[-1] if not returns.empty else pd.Series(dtype=float)
    bench_ret_last = bench_ret.reindex(returns.index).iloc[-1] if not bench_ret.empty else 0

    brinson = brinson_attribution(
        port_returns=port_ret_last,
        port_industries=pd.Series(ind_map),
        bench_returns=pd.Series(bench_ret_last, index=valid_syms),
        bench_industries=pd.Series(bench_ind_map),
        port_weights=port_w_series,
        bench_weights=bench_w_series,
    )

    cum_port = (1 + port_ret).cumprod()
    cum_bench = (1 + bench_ret.reindex(port_ret.index).fillna(0)).cumprod()

    return {
        "n_holdings": len(valid_syms),
        "total_weight": round(float(weights.sum()), 4),
        "concentration": concentration,
        "industry_exposure": exposure.to_dict("records"),
        "risk": {
            "beta": beta_info,
            "var_historical": var_hist,
            "var_parametric": var_param,
            "var_cornish_fisher": var_cf,
        },
        "brinson": brinson,
        "nav": {
            "portfolio": cum_port.dropna().tolist(),
            "benchmark": cum_bench.dropna().tolist(),
        },
    }
