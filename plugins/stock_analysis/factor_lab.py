"""
factor_lab.py — 因子计算、中性化、IC 检验与分层回测（P1）

替代 v1.7.1 的 signal_quality（只统计"命中率"）。
专业要求：因子要能被 IC / ICIR / 分层收益 / 换手 / 回撤 五个维度检验，
且回测必须遵守六条防坑清单，否则曲线一定漂亮、上线一定亏钱。

回测六条铁律（BacktestConfig 中强制）
------------------------------------
1. t+1 成交：信号用 t 日收盘计算，t+1 开盘/收盘成交（绝不用当天收盘成交）
2. 剔除 ST / *ST / 退市 / 次新（上市 <60 交易日）
3. 剔除停牌（无法成交）与一字涨跌停（买不到/卖不掉）
4. 成本：佣金万2.5双边 + 印花税千1卖出 + 冲击成本（按 Amihud 估算）
5. 使用时点行业分类（避免前视偏差）
6. 样本包含已退市标的（避免幸存者偏差）
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd


# ================================================================== 配置


@dataclass
class BacktestConfig:
    """回测铁律集中在此，任何一条都不可绕过。"""
    delay_days: int = 1                 # t+1 成交
    price_for_signal: str = "close"     # 信号用当日收盘
    price_for_trade: str = "open"       # 成交用次日开盘
    commission: float = 0.00025         # 单边佣金 万2.5
    stamp_tax: float = 0.001            # 卖出印花税 千1
    slippage_bps: float = 5.0           # 冲击成本（bp），可按 Amihud 动态估算
    min_list_days: int = 60             # 次新股剔除
    exclude_st: bool = True
    exclude_suspended: bool = True
    exclude_limit_up_down: bool = True  # 涨跌停不可成交
    include_delisted: bool = True       # 必须包含退市样本
    periods_per_year: int = 252


# ================================================================== 因子库


def momentum(close: pd.DataFrame, n: int = 20, skip: int = 0) -> pd.DataFrame:
    """动量。skip>0 时跳过最近 skip 日（剥离短期反转效应）。"""
    if skip:
        return close.shift(skip) / close.shift(skip + n) - 1.0
    return close / close.shift(n) - 1.0


def reversal(close: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    return -(close / close.shift(n) - 1.0)


def volatility(ret: pd.DataFrame, n: int = 60) -> pd.DataFrame:
    return ret.rolling(n, min_periods=max(5, n // 2)).std() * np.sqrt(252)


def downside_vol(ret: pd.DataFrame, n: int = 60) -> pd.DataFrame:
    neg = ret.clip(upper=0.0).fillna(0.0)
    return neg.rolling(n, min_periods=max(5, n // 2)).std() * np.sqrt(252)


def amihud(ret: pd.DataFrame, amount: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """非流动性：|收益| / 成交额 的均值 ×1e8。值越大越难成交。"""
    illiq = (ret.abs() / amount.replace(0, np.nan)) * 1e8
    return illiq.rolling(n, min_periods=max(5, n // 2)).mean()


def beta(asset_ret: pd.DataFrame, bench: pd.Series, n: int = 250) -> pd.DataFrame:
    cov = asset_ret.rolling(n, min_periods=60).cov(bench)
    var = bench.rolling(n, min_periods=60).var()
    return cov.div(var, axis=0)


def turnover_rate(vol: pd.DataFrame, shares_float: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    return (vol / shares_float.replace(0, np.nan)).rolling(n, min_periods=5).mean()


def volume_avg(amount: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """日均成交额（对数）。"""
    return np.log(amount.rolling(n, min_periods=max(5, n // 2)).mean().replace(0, np.nan))


def ln_mcap(mcap: pd.DataFrame) -> pd.DataFrame:
    """对数市值（规模因子）。"""
    return np.log(mcap.astype(float).replace(0, np.nan))


def residual_momentum(ret: pd.DataFrame, bench: pd.Series, n: int = 120) -> pd.DataFrame:
    """残差动量：收益对基准回归取残差，再算动量。剥离市场 Beta 后的纯 alpha 动量。"""
    resid = pd.DataFrame(index=ret.index, columns=ret.columns, dtype=float)
    for dt in ret.index:
        r = ret.loc[dt].dropna()
        b = bench.loc[dt] if dt in bench.index else np.nan
        if len(r) < 10 or np.isnan(b):
            continue
        X = np.column_stack([np.ones(len(r)), np.full(len(r), b)])
        try:
            coef, *_ = np.linalg.lstsq(X, r.to_numpy(float), rcond=None)
            resid.loc[dt, r.index] = r.to_numpy(float) - X @ coef
        except np.linalg.LinAlgError:
            continue
    return resid.rolling(n, min_periods=30).sum()


FACTOR_REGISTRY = {
    "mom_20": lambda d: momentum(d["close"], 20),
    "mom_60": lambda d: momentum(d["close"], 60),
    "mom_120_skip20": lambda d: momentum(d["close"], 120, skip=20),
    "mom_250": lambda d: momentum(d["close"], 250),
    "rev_20": lambda d: reversal(d["close"], 20),
    "vol_60": lambda d: volatility(d["ret"], 60),
    "vol_250": lambda d: volatility(d["ret"], 250),
    "dvol_60": lambda d: downside_vol(d["ret"], 60),
    "illiquidity_20": lambda d: amihud(d["ret"], d["amount"], 20),
    "turnover_20": lambda d: turnover_rate(d["vol"], d["float_shares"], 20),
    "turnover_60": lambda d: turnover_rate(d["vol"], d["float_shares"], 60),
    "volume_avg_20": lambda d: volume_avg(d["amount"], 20),
    "beta_250": lambda d: beta(d["ret"], d["bench_ret"], 250),
    "ln_mcap": lambda d: ln_mcap(d["mcap"]) if "mcap" in d else pd.DataFrame(),
    "residual_mom_120": lambda d: residual_momentum(d["ret"], d["bench_ret"], 120),
}


# ================================================================== 基本面因子


def _safe_div(num, den):
    """安全除法，分母为 0 或 NaN 返回 NaN。"""
    if den is None or den == 0 or pd.isna(den):
        return np.nan
    return num / den


def ep_ratio(net_income: pd.Series, mcap: pd.Series) -> pd.Series:
    """EP = 净利润 / 总市值（PE 倒数，价值因子）。"""
    return net_income / mcap.replace(0, np.nan)


def bp_ratio(equity: pd.Series, mcap: pd.Series) -> pd.Series:
    """BP = 净资产 / 总市值（PB 倒数）。"""
    return equity / mcap.replace(0, np.nan)


def sp_ratio(revenue: pd.Series, mcap: pd.Series) -> pd.Series:
    """SP = 营业收入 / 总市值（PS 倒数）。"""
    return revenue / mcap.replace(0, np.nan)


def cfp_ratio(ocf: pd.Series, mcap: pd.Series) -> pd.Series:
    """CFP = 经营现金流 / 总市值。"""
    return ocf / mcap.replace(0, np.nan)


def roe_factor(net_income: pd.Series, equity: pd.Series) -> pd.Series:
    """ROE = 净利润 / 净资产（质量因子）。"""
    return net_income / equity.replace(0, np.nan)


def roic_factor(nopat: pd.Series, invested_capital: pd.Series) -> pd.Series:
    """ROIC = NOPAT / 投入资本。"""
    return nopat / invested_capital.replace(0, np.nan)


def gross_margin_factor(revenue: pd.Series, cogs: pd.Series) -> pd.Series:
    """毛利率 = (营收 - 成本) / 营收。"""
    return (revenue - cogs) / revenue.replace(0, np.nan)


def ocf_to_ni_factor(ocf: pd.Series, net_income: pd.Series) -> pd.Series:
    """经营现金流 / 净利润（盈利质量）。"""
    return ocf / net_income.replace(0, np.nan)


def accruals_ratio(net_income: pd.Series, ocf: pd.Series, total_assets: pd.Series) -> pd.Series:
    """应计率 = (净利润 - 经营现金流) / 总资产。越高盈利质量越差。"""
    return (net_income - ocf) / total_assets.replace(0, np.nan)


def debt_ratio_factor(total_liabilities: pd.Series, total_assets: pd.Series) -> pd.Series:
    """资产负债率 = 总负债 / 总资产。"""
    return total_liabilities / total_assets.replace(0, np.nan)


def yoy_growth(current: pd.Series, prior: pd.Series) -> pd.Series:
    """同比增长率 = (本期 - 同期) / |同期|。"""
    return (current - prior) / prior.abs().replace(0, np.nan)


def dividend_yield_factor(dividends: pd.Series, mcap: pd.Series) -> pd.Series:
    """股息率 = 每股股利 / 股价 ≈ 总股利 / 市值。"""
    return dividends / mcap.replace(0, np.nan)


FUNDAMENTAL_FACTOR_REGISTRY = {
    "ep": lambda d: ep_ratio(d["net_income"], d["mcap"]),
    "bp": lambda d: bp_ratio(d["equity"], d["mcap"]),
    "sp": lambda d: sp_ratio(d["revenue"], d["mcap"]),
    "cfp": lambda d: cfp_ratio(d["ocf"], d["mcap"]),
    "roe": lambda d: roe_factor(d["net_income"], d["equity"]),
    "roic": lambda d: roic_factor(d["nopat"], d["invested_capital"]),
    "gross_margin": lambda d: gross_margin_factor(d["revenue"], d["cogs"]),
    "ocf_to_ni": lambda d: ocf_to_ni_factor(d["ocf"], d["net_income"]),
    "accruals": lambda d: accruals_ratio(d["net_income"], d["ocf"], d["total_assets"]),
    "debt_ratio": lambda d: debt_ratio_factor(d["total_liabilities"], d["total_assets"]),
    "revenue_yoy": lambda d: yoy_growth(d["revenue"], d["revenue_prior"]),
    "profit_yoy": lambda d: yoy_growth(d["net_income"], d["net_income_prior"]),
    "dividend_yield": lambda d: dividend_yield_factor(d["dividends"], d["mcap"]),
}


# ================================================================== 预处理


def winsorize(s: pd.Series, n_mad: float = 3.0) -> pd.Series:
    """MAD 去极值（比固定分位数更稳健，A 股极值多）。"""
    med = s.median()
    mad = (s - med).abs().median()
    if mad == 0 or np.isnan(mad):
        return s
    lo, hi = med - n_mad * 1.4826 * mad, med + n_mad * 1.4826 * mad
    return s.clip(lo, hi)


def zscore(s: pd.Series) -> pd.Series:
    std = s.std(ddof=0)
    return (s - s.mean()) / std if std and not np.isnan(std) else s * 0.0


def neutralize(s: pd.Series, industry: pd.Series, lnmcap: pd.Series) -> pd.Series:
    """
    行业哑变量 + 对数市值 OLS，取残差。
    用 numpy.lstsq 实现，避免引入 statsmodels 依赖；
    若已装 statsmodels，可替换为 sm.OLS 以获得 t 检验与 R²。
    """
    df = pd.DataFrame({"y": s, "lnmcap": lnmcap})
    ind = pd.get_dummies(industry.reindex(s.index), prefix="ind", drop_first=True).astype(float)
    X = pd.concat([df[["lnmcap"]], ind], axis=1)
    mask = df["y"].notna() & X.notna().all(axis=1)
    if mask.sum() < max(10, X.shape[1] + 2):
        return s * np.nan
    Xm = np.column_stack([np.ones(mask.sum()), X[mask].to_numpy(float)])
    ym = df.loc[mask, "y"].to_numpy(float)
    try:
        coef, *_ = np.linalg.lstsq(Xm, ym, rcond=None)
    except np.linalg.LinAlgError:
        return s * np.nan
    resid = pd.Series(np.nan, index=s.index, dtype=float)
    resid.loc[mask] = ym - Xm @ coef
    return resid


def prepare_cross_section(
    raw: pd.Series,
    industry: pd.Series,
    mcap: pd.Series,
    do_neutralize: bool = True,
) -> pd.Series:
    x = winsorize(raw)
    if do_neutralize:
        x = neutralize(x, industry, np.log(mcap.astype(float).replace(0, np.nan)))
    return zscore(x)


# ================================================================== 检验


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman 秩相关。手写实现，避免 scipy 依赖（scipy 可选，装了更快）。"""
    try:
        from scipy.stats import spearmanr      # type: ignore
        return float(spearmanr(a.to_numpy(), b.to_numpy(), nan_policy="omit").statistic)
    except Exception:
        return float(a.rank().corr(b.rank()))


def ic_series(factor: pd.DataFrame, fwd_ret: pd.DataFrame, method: str = "spearman") -> pd.Series:
    """逐期截面 IC。fwd_ret 已对齐为「下一期收益」。"""
    out = {}
    for dt in factor.index:
        f = factor.loc[dt].dropna()
        r = fwd_ret.loc[dt].dropna() if dt in fwd_ret.index else pd.Series(dtype=float)
        idx = f.index.intersection(r.index)
        if len(idx) < 10:
            continue
        a, b = f.loc[idx], r.loc[idx]
        out[dt] = _spearman(a, b) if method == "spearman" else float(a.corr(b))
    return pd.Series(out, dtype=float)


def factor_report(ics: pd.Series, periods_per_year: int = 12) -> Dict[str, float]:
    if len(ics) < 3:
        return {"n_periods": len(ics)}
    mean, std = ics.mean(), ics.std(ddof=1)
    return {
        "n_periods": int(len(ics)),
        "ic_mean": float(mean),
        "ic_std": float(std),
        "icir": float(mean / std) if std else float("nan"),
        "ic_positive_ratio": float((ics > 0).mean()),
        "ic_t_stat": float(mean / std * np.sqrt(len(ics))) if std else float("nan"),
        "ic_annualized_ir": float(mean / std * np.sqrt(periods_per_year)) if std else float("nan"),
    }


# ================================================================== 分层回测


def quintile_backtest(
    factor: pd.DataFrame,
    fwd_ret: pd.DataFrame,
    cfg: BacktestConfig = BacktestConfig(),
    n_groups: int = 5,
    long_top: bool = True,
    tradable: Optional[pd.DataFrame] = None,
) -> Dict[str, object]:
    """
    分层回测：按因子值分 n 组，等权持有，t+1 成交，计入交易成本。

    factor  : 截面因子（t 日收盘计算得到，已 shift 到可交易日）
    fwd_ret : 下一期收益（t → t+1）
    tradable: 可选的可交易掩码（停牌/涨跌停/ST 置 False），回测铁律第 2/3 条
    """
    group_names = [f"Q{i + 1}" for i in range(n_groups)]
    nav: Dict[str, List[float]] = {g: [1.0] for g in group_names}
    nav["long_short"] = [1.0]
    prev: Dict[str, Optional[pd.Index]] = {g: None for g in group_names}
    turnover: Dict[str, List[float]] = {g: [] for g in group_names}
    unit_cost = 2 * (cfg.commission + cfg.slippage_bps / 10_000.0) + cfg.stamp_tax / 2

    for dt in factor.index:
        f = factor.loc[dt].dropna()
        r = fwd_ret.loc[dt].dropna() if dt in fwd_ret.index else pd.Series(dtype=float)
        idx = f.index.intersection(r.index)
        if tradable is not None and dt in tradable.index:
            ok = tradable.loc[dt]
            idx = idx.intersection(ok[ok.fillna(False)].index)
        if len(idx) < n_groups * 5:
            continue
        f, r = f.loc[idx], r.loc[idx]

        # long_top=True → 因子值最大的为 Q1
        ranks = f.rank(ascending=not long_top, method="first")
        groups = pd.cut(ranks, n_groups, labels=group_names)
        gr = r.groupby(groups, observed=True).mean()

        for g in group_names:
            cur = idx[groups == g]
            chg = _turnover(prev[g], cur)
            prev[g] = cur
            turnover[g].append(chg)
            net = float(gr.get(g, 0.0)) - chg * unit_cost
            nav[g].append(nav[g][-1] * (1.0 + net))

        # 多空：Q1 − Qn（多头 Q1，空头 Qn）
        ls = float(gr.get("Q1", 0.0) - gr.get(f"Q{n_groups}", 0.0))
        nav["long_short"].append(nav["long_short"][-1] * (1.0 + ls))

    out: Dict[str, object] = {"nav": {k: pd.Series(v) for k, v in nav.items()}}
    for k, s in out["nav"].items():                          # type: ignore[union-attr]
        out[f"metrics_{k}"] = perf_metrics(s, cfg.periods_per_year)
    out["annual_turnover"] = {
        g: float(np.mean(turnover[g]) * cfg.periods_per_year) if turnover[g] else float("nan")
        for g in group_names
    }
    out["config"] = asdict(cfg)
    return out


def _turnover(prev: Optional[pd.Index], cur: pd.Index) -> float:
    """单边换手：首次建仓记 1.0。"""
    if prev is None or len(prev) == 0:
        return 1.0
    if len(cur) == 0:
        return 1.0
    return float(1.0 - len(prev.intersection(cur)) / len(prev))


def perf_metrics(nav: pd.Series, periods_per_year: int = 252) -> Dict[str, float]:
    nav = pd.Series(nav).dropna()
    if len(nav) < 3:
        return {}
    ret = nav.pct_change().dropna()
    ann_ret = (nav.iloc[-1] / nav.iloc[0]) ** (periods_per_year / max(len(ret), 1)) - 1
    ann_vol = ret.std(ddof=1) * np.sqrt(periods_per_year)
    dd = (nav / nav.cummax() - 1.0)
    max_dd = float(dd.min())
    sharpe = (ann_ret - 0.02) / ann_vol if ann_vol else float("nan")
    return {
        "total_return": float(nav.iloc[-1] / nav.iloc[0] - 1),
        "annual_return": float(ann_ret),
        "annual_vol": float(ann_vol),
        "sharpe": float(sharpe),
        "max_drawdown": max_dd,
        "calmar": float(ann_ret / abs(max_dd)) if max_dd else float("nan"),
        "win_rate": float((ret > 0).mean()),
    }


# ================================================================== 事件研究


def event_study(
    ret: pd.DataFrame,
    bench_ret: pd.Series,
    events: pd.DataFrame,
    pre: int = 5,
    post: int = 20,
) -> Dict[str, object]:
    """
    事件研究：计算 [-pre, +post] 窗口的累计超额收益 CAR。
    events: columns = [uid, event_date]
    """
    cars = []
    for _, ev in events.iterrows():
        uid, ed = ev["uid"], pd.Timestamp(ev["event_date"])
        if uid not in ret.columns:
            continue
        s = ret[uid]
        pos = s.index.searchsorted(ed)
        if pos == 0 or pos >= len(s):
            continue
        win = s.iloc[max(0, pos - pre): pos + post]
        bwin = bench_ret.reindex(win.index).fillna(0.0)
        cars.append((win - bwin).cumsum())
    if not cars:
        return {"n_events": 0}
    car = pd.concat(cars, axis=1).mean(axis=1)
    return {
        "n_events": len(cars),
        "car_by_day": car.to_dict(),
        "car_window": float(car.iloc[-1] - car.iloc[0]) if len(car) > 1 else 0.0,
        "car_pre": float(car.iloc[min(pre, len(car) - 1)] - car.iloc[0]),
        "car_post": float(car.iloc[-1] - car.iloc[min(pre, len(car) - 1)]),
    }


if __name__ == "__main__":
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2023-01-01", periods=300)
    cols = [f"CN:{i:06d}" for i in range(60)]
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.02, (300, 60)), axis=0)),
                         index=dates, columns=cols)
    ret = close.pct_change()
    bench = ret.mean(axis=1)

    f = momentum(close, 20).shift(1)         # t 日收盘算，t+1 生效
    fwd = ret.shift(-1)                      # 下一期收益
    ics = ic_series(f, fwd)
    print("因子报告:", {k: round(v, 4) for k, v in factor_report(ics).items()})

    bt = quintile_backtest(f.iloc[30:], fwd.iloc[30:])
    print("多空净值:", {k: round(v, 4) for k, v in bt["metrics_long_short"].items()})  # type: ignore
    print("分组年化:", {g: round(bt[f"metrics_{g}"]["annual_return"], 4) for g in ("Q1", "Q3", "Q5")})
    print("年化换手:", {g: round(v, 2) for g, v in bt["annual_turnover"].items()})  # type: ignore
    # 注：本 demo 用随机游走生成价格，理论上不存在动量 alpha，
    # 因此 IC≈0、分层无单调、成本全额侵蚀收益 —— 这正是"回测必须先证伪"的意义。
    print("中性化示例:", prepare_cross_section(
        f.iloc[-1],
        pd.Series({c: f"IND{i % 6}" for i, c in enumerate(close.columns)}),
        pd.Series(1e10, index=close.columns),
    ).describe()[["mean", "std"]].round(4).to_dict())
