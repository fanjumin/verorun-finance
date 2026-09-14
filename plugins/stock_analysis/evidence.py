# evidence.py — 证据打包（A1）+ 结构化输出解析（A4）
# 约束：
# - 每个证据子源独立 try/except，任一缺失降级并留痕（可审计）；
# - 输出纯文本块注入 prompt 的"证据材料"段，总长受 max_chars 预算控制；
# - tushare 字段全部 .get() 容错：不同 token 权限返回字段集不同，缺字段只跳过不报错。
from __future__ import annotations

import json
import re
from typing import Optional


def _fmt_yi(v) -> Optional[str]:
    """元 → 亿元，保留 2 位；非法值 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return "%.2f亿" % (f / 1e8)


def _fmt_pct(v) -> Optional[str]:
    try:
        return "%.1f%%" % float(v)
    except (TypeError, ValueError):
        return None


def digest_fundamental(fund: dict, periods: int = 4) -> list:
    """四表记录 → 关键科目摘要行（最近 periods 期）。返回 [] 表示无可用数据。"""
    lines = []
    income = (fund.get("income") or [])[:periods]
    cashflow = (fund.get("cashflow") or [])[:periods]
    balance = (fund.get("balance") or [])[:periods]
    fina = (fund.get("fina_indicator") or [])[:periods]

    cf_by_period = {r.get("end_date"): r for r in cashflow if isinstance(r, dict)}
    bl_by_period = {r.get("end_date"): r for r in balance if isinstance(r, dict)}

    for rec in income:
        if not isinstance(rec, dict):
            continue
        period = rec.get("end_date") or "N/A"
        parts = ["- %s 营收 %s" % (period, _fmt_yi(rec.get("revenue")) or "N/A")]
        ni = _fmt_yi(rec.get("n_income"))
        if ni:
            parts.append("净利润 %s" % ni)
        cf = cf_by_period.get(period)
        if cf:
            ncf = _fmt_yi(cf.get("n_cashflow_act"))
            if ncf:
                parts.append("经营现金流 %s" % ncf)
        bl = bl_by_period.get(period)
        if bl:
            debt_ratio = None
            ta, tl = bl.get("total_assets"), bl.get("total_liab")
            try:
                if float(ta) > 0:
                    debt_ratio = "%.1f%%" % (float(tl) / float(ta) * 100)
            except (TypeError, ValueError):
                pass
            if debt_ratio:
                parts.append("负债率 %s" % debt_ratio)
        lines.append(" ".join(p for p in parts if "None" not in p))

    # 最近一期财务指标（ROE/毛利率/净利同比）
    for rec in fina[:1]:
        if not isinstance(rec, dict):
            continue
        seg = []
        roe = _fmt_pct(rec.get("roe"))
        gm = _fmt_pct(rec.get("grossprofit_margin"))
        yoy = _fmt_pct(rec.get("netprofit_yoy"))
        if roe:
            seg.append("ROE %s" % roe)
        if gm:
            seg.append("毛利率 %s" % gm)
        if yoy:
            seg.append("净利同比 %s" % yoy)
        if seg:
            lines.append("- 财务指标(%s): %s" % (rec.get("end_date") or "N/A", " / ".join(seg)))

    # 营收同比（由最近两期 income 推算，避免依赖不一定存在的 yoy 字段）
    if (len(income) >= 2 and isinstance(income[0], dict) and isinstance(income[1], dict)):
        try:
            cur, prev = float(income[0].get("revenue")), float(income[1].get("revenue"))
            if prev != 0:
                lines.append("- 营收同比(推算): %+.1f%%" % ((cur - prev) / abs(prev) * 100))
        except (TypeError, ValueError):
            pass
    return [l for l in lines if l.strip()]


def _short_err(err: Exception, limit: int = 60) -> str:
    text = str(err).replace("\n", " ")
    return text[:limit]


def build_evidence_context(fetch_fundamental, fetch_moneyflow, fetch_news,
                           valuation_text: str = "", symbol: str = "",
                           max_chars: int = 2600) -> str:
    """证据打包（A1）。三个取数回调分别对应 gateway.get_fundamental/get_moneyflow/get_news，
    传回调而非直接依赖 gateway，便于测试与未来替换数据源。
    """
    blocks = []

    # 1) 财报四表关键科目
    try:
        fund = fetch_fundamental(symbol) or {}
        lines = digest_fundamental(fund, periods=4)
        if lines:
            blocks.append("## 财报证据（最近4期）\n" + "\n".join(lines))
        else:
            blocks.append("## 财报证据\n未获得（数据源返回为空）")
    except Exception as err:
        blocks.append("## 财报证据\n未获得（%s）" % _short_err(err))

    # 2) 资金流（近 5 日净额合计，单位万元）
    try:
        mf = fetch_moneyflow(symbol)
        if mf is not None and len(mf) > 0 and "net_mf_amount" in mf.columns:
            net = mf["net_mf_amount"].astype(float).fillna(0)
            tail3 = net.tail(3).sum()
            blocks.append("## 资金流证据\n近%d日主力净流入合计 %.0f 万元（最近3日 %.0f 万元）"
                          % (len(net), float(net.sum()), float(tail3)))
        else:
            blocks.append("## 资金流证据\n未获得（数据源无 net_mf_amount 字段或为空）")
    except Exception as err:
        blocks.append("## 资金流证据\n未获得（%s）" % _short_err(err))

    # 3) 新闻标题（最多 5 条原文标题，供 LLM 自行判断情绪）
    try:
        titles = (fetch_news(symbol) or [])[:5]
        if titles:
            blocks.append("## 近期新闻标题\n" + "\n".join("- %s" % t[:80] for t in titles))
        else:
            blocks.append("## 近期新闻标题\n未获得（无新闻返回）")
    except Exception as err:
        blocks.append("## 近期新闻标题\n未获得（%s）" % _short_err(err))

    # 4) 估值分位（可选；调用方已格式化成一行文本）
    if valuation_text:
        blocks.append("## 估值分位\n" + valuation_text)

    text = "\n\n".join(blocks)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n（证据材料超长，已截断）"
    return text


# ============================================================
# A4：结构化输出解析（严格 JSON，"正则+关键词"兜底链的第一层）
# ============================================================

_VALID_SIGNALS = {"buy", "sell", "hold"}
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_structured_output(report: str) -> Optional[dict]:
    """解析 LLM 研判输出中的结构化 JSON。

    返回与 _extract_signal 兼容的 dict（signal/confidence/reasons，附 summary/
    evidence_refs 可选字段）；无法解析或校验失败返回 None（调用方走旧兜底链）。
    """
    if not report or not isinstance(report, str):
        return None

    candidates = []
    fence = _FENCE_RE.search(report)
    if fence:
        candidates.append(fence.group(1))
    # 裸 JSON：匹配所有含 "signal" 键的花括号块，取最后一个可解析的
    for mo in re.finditer(r"\{[^{}]*\"signal\"[^{}]*\}", report, re.DOTALL):
        candidates.append(mo.group(0))

    for text in reversed(candidates):
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        signal = obj.get("signal")
        if signal not in _VALID_SIGNALS:
            continue
        try:
            confidence = float(obj.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        reasons_raw = obj.get("reasons") or []
        reasons = [str(r) for r in reasons_raw if str(r).strip()][:5]
        out = {"signal": signal, "confidence": round(confidence, 2), "reasons": reasons}
        if isinstance(obj.get("summary"), str) and obj["summary"].strip():
            out["summary"] = obj["summary"].strip()[:400]
        refs = obj.get("evidence_refs") or []
        out["evidence_refs"] = [str(r) for r in refs if str(r).strip()][:8]
        return out
    return None
