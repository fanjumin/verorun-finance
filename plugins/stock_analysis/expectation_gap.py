"""
expectation_gap.py — 预期差计算模块（P1 W13-14）

比较实际业绩 vs 一致预期（分析师估计 / 业绩预告），输出结构化预期差。
两条数据通路：
  - FMP（美股）：analyst-estimates → estimatedRevenue / estimatedEps / estimatedNetIncome
  - Tushare（A 股）：forecast_vip → net_profit_min/max, basic_eps_min/max
  - akshare/同花顺（A 股，免 key）：stock_profit_forecast_ths → 年度 EPS 预测 + 预测机构数
    ★ 只有**前瞻年度**口径，与定期报告累计/TTM 实际值不同口径，不计算超预期。

核心函数：
  compute_gap(consensus, actuals) → ExpectationGapResult
  format_gap_card(result) → str  （供 LLM / UI 消费）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)


@dataclass
class GapItem:
    """单项预期差。"""
    metric: str
    label: str
    actual: Optional[float] = None
    estimate: Optional[float] = None
    estimate_low: Optional[float] = None
    estimate_high: Optional[float] = None
    surprise_pct: Optional[float] = None
    unit: str = ""

    @property
    def beat(self) -> Optional[str]:
        if self.surprise_pct is None:
            return None
        if self.surprise_pct > 5:
            return "beat"
        if self.surprise_pct < -5:
            return "miss"
        return "inline"


@dataclass
class ExpectationGapResult:
    """预期差汇总。"""
    symbol: str
    period: str = ""
    items: List[GapItem] = field(default_factory=list)
    source: str = ""
    overall_surprise: Optional[float] = None
    narrative: str = ""

    @property
    def verdict(self) -> str:
        if self.overall_surprise is None:
            return "unknown"
        if self.overall_surprise > 5:
            return "超预期"
        if self.overall_surprise < -5:
            return "低于预期"
        return "符合预期"


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _pct_surprise(actual: float, estimate: float) -> Optional[float]:
    if estimate == 0:
        return None
    return (actual - estimate) / abs(estimate) * 100


def _extract_fmp_actuals(actuals: dict) -> dict:
    """从 fundamental income 表提取最近一期 FMP 可比字段。"""
    income_list = actuals.get("income", [])
    if not income_list:
        return {}
    latest = income_list[0] if isinstance(income_list[0], dict) else {}
    return {
        "revenue": _safe_float(latest.get("revenue") or latest.get("total_revenue")),
        "net_income": _safe_float(latest.get("net_income")),
        "eps": _safe_float(latest.get("eps") or latest.get("basic_eps")),
    }


def _extract_ts_actuals(actuals: dict) -> dict:
    """从 Tushare fina_indicator / income 提取最近一期 A 股可比字段。"""
    fina = actuals.get("fina_indicator", [])
    income = actuals.get("income", [])
    latest_fina = fina[0] if fina and isinstance(fina[0], dict) else {}
    latest_income = income[0] if income and isinstance(income[0], dict) else {}
    return {
        "revenue": _safe_float(latest_income.get("revenue") or latest_fina.get("revenue")),
        "net_income": _safe_float(
            latest_income.get("n_income") or latest_fina.get("net_profit")
        ),
        "eps": _safe_float(latest_fina.get("eps") or latest_fina.get("basic_eps")),
    }


def _compute_from_fmp(consensus: list | dict, actuals: dict, symbol: str) -> ExpectationGapResult:
    """FMP analyst-estimates：列表，每条含 estimatedRevenue / estimatedEps / estimatedNetIncome。"""
    items: List[GapItem] = []
    period = ""

    if isinstance(consensus, list) and consensus:
        est = consensus[0] if isinstance(consensus[0], dict) else {}
    elif isinstance(consensus, dict):
        est = consensus
    else:
        return ExpectationGapResult(symbol=symbol, source="fmp", narrative="无一致预期数据")

    period = str(est.get("date", ""))
    act = _extract_fmp_actuals(actuals)

    est_rev = _safe_float(est.get("estimatedRevenue"))
    act_rev = act.get("revenue")
    if est_rev and act_rev is not None:
        items.append(GapItem(
            metric="revenue", label="营业收入",
            actual=act_rev, estimate=est_rev,
            surprise_pct=_pct_surprise(act_rev, est_rev),
            unit="USD",
        ))

    est_ni = _safe_float(est.get("estimatedNetIncome"))
    act_ni = act.get("net_income")
    if est_ni and act_ni is not None:
        items.append(GapItem(
            metric="net_income", label="净利润",
            actual=act_ni, estimate=est_ni,
            surprise_pct=_pct_surprise(act_ni, est_ni),
            unit="USD",
        ))

    est_eps = _safe_float(est.get("estimatedEps"))
    act_eps = act.get("eps")
    if est_eps and act_eps is not None:
        items.append(GapItem(
            metric="eps", label="每股收益(EPS)",
            actual=act_eps, estimate=est_eps,
            surprise_pct=_pct_surprise(act_eps, est_eps),
            unit="USD/share",
        ))

    surprises = [it.surprise_pct for it in items if it.surprise_pct is not None]
    overall = sum(surprises) / len(surprises) if surprises else None

    return ExpectationGapResult(
        symbol=symbol, period=period, items=items,
        source="fmp", overall_surprise=overall,
    )


def _compute_from_tushare(consensus: dict, actuals: dict, symbol: str) -> ExpectationGapResult:
    """Tushare forecast_vip：业绩预告，含区间估计。"""
    items: List[GapItem] = []
    period = str(consensus.get("end_date", ""))
    act = _extract_ts_actuals(actuals)

    ni_low = _safe_float(consensus.get("net_profit_min"))
    ni_high = _safe_float(consensus.get("net_profit_max"))
    act_ni = act.get("net_income")
    if ni_low is not None and ni_high is not None:
        mid = (ni_low + ni_high) / 2
        surprise = None
        if act_ni is not None and mid != 0:
            surprise = _pct_surprise(act_ni, mid)
        items.append(GapItem(
            metric="net_income", label="净利润(预告)",
            actual=act_ni, estimate=mid,
            estimate_low=ni_low, estimate_high=ni_high,
            surprise_pct=surprise,
            unit="CNY",
        ))

    eps_low = _safe_float(consensus.get("basic_eps_min"))
    eps_high = _safe_float(consensus.get("basic_eps_max"))
    act_eps = act.get("eps")
    if eps_low is not None and eps_high is not None:
        mid = (eps_low + eps_high) / 2
        surprise = None
        if act_eps is not None and mid != 0:
            surprise = _pct_surprise(act_eps, mid)
        items.append(GapItem(
            metric="eps", label="每股收益(预告)",
            actual=act_eps, estimate=mid,
            estimate_low=eps_low, estimate_high=eps_high,
            surprise_pct=surprise,
            unit="CNY/share",
        ))

    surprises = [it.surprise_pct for it in items if it.surprise_pct is not None]
    overall = sum(surprises) / len(surprises) if surprises else None

    summary = consensus.get("summary", "")
    change_reason = consensus.get("change_reason", "")

    narrative_parts = []
    if summary:
        narrative_parts.append(f"业绩预告摘要：{summary}")
    if change_reason:
        narrative_parts.append(f"变动原因：{change_reason}")

    return ExpectationGapResult(
        symbol=symbol, period=period, items=items,
        source="tushare", overall_surprise=overall,
        narrative="；".join(narrative_parts),
    )


def _eps_by_period(actuals: dict) -> Dict[str, float]:
    """end_date(YYYYMMDD) → 基本每股收益。

    akshare 财报源的两处落点都读：income 的 `basic_eps`（新浪利润表「基本每股收益」）
    与 fina_indicator 的中文键「基本每股收益」（东财摘要）。两者都是**年初至今累计**
    口径 —— 计算 TTM 时要用「本期累计 + 上年全年 − 上年同期累计」。
    """
    out: Dict[str, float] = {}
    for key in ("income", "fina_indicator"):
        for row in actuals.get(key) or []:
            if not isinstance(row, dict):
                continue
            d = str(row.get("end_date") or "")
            if len(d) != 8 or not d.isdigit():
                continue
            val = _safe_float(row.get("basic_eps"))
            if val is None:
                val = _safe_float(row.get("基本每股收益"))
            if val is not None and d not in out:
                out[d] = val
    return out


def _ttm_eps(eps_map: Dict[str, float]) -> tuple:
    """累计口径 EPS → 滚动 12 个月 EPS，返回 (值, 口径说明)。

    年报（1231）本身就是全年值；季报/半年报用 本期 + 上年全年 − 上年同期。
    上年全年或上年同期缺失时退化为最近一期累计值（并在说明里标注，不静默当 TTM）。
    """
    if not eps_map:
        return None, ""
    periods = sorted(eps_map)
    latest = periods[-1]
    year = int(latest[:4])
    mmdd = latest[4:]
    if mmdd == "1231":
        return eps_map[latest], f"{latest} 年报"
    prev_fy = f"{year - 1}1231"
    prev_same = f"{year - 1}{mmdd}"
    if prev_fy in eps_map and prev_same in eps_map:
        val = eps_map[latest] + eps_map[prev_fy] - eps_map[prev_same]
        return val, f"{latest} 滚动12月"
    return eps_map[latest], f"{latest} 年初至今（缺上年同期，未折算TTM）"


def _compute_from_akshare(consensus: dict, actuals: dict, symbol: str) -> ExpectationGapResult:
    """akshare（同花顺）一致预期：**只有年度 EPS 预测 + 预测机构数**。

    ★ 口径纪律：这里的预期是**前瞻年度值**（FY1~FY3），而可拿到的实际值是
    **定期报告累计/滚动 12 月值**，两者不可比 —— 硬算"超预期"会系统性偏向
    "不及预期"（TTM 永远是滞后于全年预测的）。因此：
      - **不产出带 surprise 的 GapItem**（避免 UI 显示假的"不及预期"）；
      - 实际值、达成度只写进 narrative，供阅读，不算作预期差。
    只有当已披露**年报**的年度恰好等于某预测年度时（基本不会遇到，源只给未来三年），
    才按同口径产出可比项。
    """
    forecasts = [f for f in (consensus.get("forecasts") or []) if isinstance(f, dict)]
    years = [int(f["year"]) for f in forecasts if f.get("year") is not None]
    fy1 = min(years) if years else None

    eps_map = _eps_by_period(actuals)
    ttm, ttm_note = _ttm_eps(eps_map)

    items: List[GapItem] = []
    # 同口径可比：已披露年报 EPS 落在某个预测年度上
    for f in forecasts:
        y = f.get("year")
        annual = eps_map.get(f"{int(y)}1231") if y is not None else None
        if annual is None:
            continue
        est = _safe_float(f.get("eps_mean"))
        items.append(GapItem(
            metric=f"eps_fy{int(y)}", label=f"{int(y)}年 每股收益",
            actual=annual, estimate=est,
            estimate_low=_safe_float(f.get("eps_min")),
            estimate_high=_safe_float(f.get("eps_max")),
            surprise_pct=_pct_surprise(annual, est) if est else None,
            unit="CNY/share",
        ))

    parts = []
    fy1_row = next((f for f in forecasts if f.get("year") == fy1), {}) or {}
    est = _safe_float(fy1_row.get("eps_mean"))
    lo = _safe_float(fy1_row.get("eps_min"))
    hi = _safe_float(fy1_row.get("eps_max"))
    inst = fy1_row.get("institutions")
    if ttm is not None and fy1 is not None:
        parts.append(f"实际 EPS {ttm:.2f} 元（{ttm_note}）")
        head = f"FY{int(fy1)} 一致预期"
        if est is None:
            parts.append(f"{head} 缺失")
        elif inst and lo is not None and hi is not None:
            parts.append(f"{head} {est:.2f} 元（{inst} 家机构，区间 {lo:.2f}~{hi:.2f}）")
        elif inst:
            parts.append(f"{head} {est:.2f} 元（{inst} 家机构）")
        else:
            parts.append(f"{head} {est:.2f} 元")
        if est:
            parts.append(f"达成度 {ttm / est * 100:.1f}%")
    elif ttm is not None:
        parts.append(f"实际 EPS {ttm:.2f} 元（{ttm_note}）")
    parts.append("口径：滚动12月实际 vs 前瞻年度预测，二者不可比，故不计超预期")

    return ExpectationGapResult(
        symbol=symbol,
        period=f"FY{int(fy1)}E" if fy1 else "",
        items=items,
        source="akshare_consensus",
        overall_surprise=None,
        narrative="｜".join(parts),
    )


def compute_gap(consensus: Any, actuals: dict, symbol: str = "") -> ExpectationGapResult:
    """计算预期差。

    Parameters
    ----------
    consensus : dict | list
        FMP 返回列表（analyst-estimates），Tushare 返回 dict（forecast_vip）。
    actuals : dict
        fundamental 四表数据（income / balance / cashflow / fina_indicator）。
    symbol : str
        标的代码（用于结果标识）。
    """
    if not consensus:
        return ExpectationGapResult(symbol=symbol, narrative="无一致预期数据")
    if not actuals:
        # akshare 源给的是**前瞻**预测，没有实际业绩时预测本身仍然有效，
        # 只是无从比较 —— 不整条判死，交给 _compute_from_akshare 输出口径说明。
        if not (isinstance(consensus, dict)
                and consensus.get("source") == "akshare_consensus"):
            return ExpectationGapResult(symbol=symbol, narrative="无实际业绩数据")

    if isinstance(consensus, list):
        return _compute_from_fmp(consensus, actuals, symbol)
    if isinstance(consensus, dict) and consensus.get("source") == "akshare_consensus":
        return _compute_from_akshare(consensus, actuals, symbol)
    if isinstance(consensus, dict) and consensus.get("source") == "tushare":
        return _compute_from_tushare(consensus, actuals, symbol)
    if isinstance(consensus, dict) and any(
        k in consensus for k in ("estimatedRevenue", "estimatedEps", "estimatedNetIncome")
    ):
        return _compute_from_fmp(consensus, actuals, symbol)
    if isinstance(consensus, dict) and any(
        k in consensus for k in ("net_profit_min", "net_profit_max", "basic_eps_min")
    ):
        return _compute_from_tushare(consensus, actuals, symbol)

    return ExpectationGapResult(symbol=symbol, narrative="无法识别一致预期数据格式")


def format_gap_card(result: ExpectationGapResult) -> str:
    """格式化为可读文本，供 LLM 上下文或 UI 卡片消费。"""
    if not result.items:
        return f"## 预期差\n{result.narrative or '无数据'}"

    lines = [f"## 预期差（{result.verdict}）"]
    if result.period:
        lines.append(f"报告期：{result.period}")

    for it in result.items:
        parts = [f"**{it.label}**"]
        if it.actual is not None:
            parts.append(f"实际 {it.actual:,.2f}")
        if it.estimate_low is not None and it.estimate_high is not None:
            parts.append(f"预告区间 [{it.estimate_low:,.2f}, {it.estimate_high:,.2f}]")
        elif it.estimate is not None:
            parts.append(f"预期 {it.estimate:,.2f}")
        if it.surprise_pct is not None:
            direction = "↑" if it.surprise_pct > 0 else "↓" if it.surprise_pct < 0 else "→"
            parts.append(f"偏差 {direction}{abs(it.surprise_pct):.1f}%")
        lines.append(" | ".join(parts))

    if result.overall_surprise is not None:
        lines.append(f"\n综合偏差：{result.overall_surprise:+.1f}%（{result.verdict}）")
    if result.narrative:
        lines.append(result.narrative)

    return "\n".join(lines)
