"""
evidence_bundle.py — 证据包 v2（P0，第 3 周）

从 v1.7.1 的"把数据塞进 prompt"升级为**带溯源、可防幻觉**的证据结构。
三重作用：
    1. 给 LLM：渲染成紧凑 markdown 表，每条带 [id]，模型只能引用这些 id
    2. 防幻觉：verify_against_evidence() 扫描报告，未在证据中出现的数字 → 打标
    3. 给审计：provenance_id 落库，任何结论可回溯到具体 URL 与时间戳

核心原则
--------
    Numbers come from code. Words come from models.
    模型可以做归纳、归因、风险清单、反方论证、行文；
    但报告里的每个数字，都必须能在证据包里找到 provenance_id。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    import pandas as pd
except ImportError:      # 证据包在纯 dict 场景下可无 pandas
    pd = None            # type: ignore


DISCLAIMER = "研究信息，不构成投资建议；数据可能延迟或缺失，请以原始来源为准。"


# ================================================================== 条目


@dataclass
class EvidenceItem:
    key: str                      # "income.revenue_ttm"
    label: str                    # "营业收入(TTM)"
    value: Any
    unit: str = ""                # CNY / x / % / 股
    as_of: str = ""               # 数据归属期或时点，如 "2025Q3" / "2026-09-06T15:00:00+08:00"
    source: str = ""              # tushare / fmp / user_supplied
    provenance_id: str = ""
    freshness: str = "unknown"    # realtime / delayed / eod / stale / unknown
    delay_seconds: int = 0
    url: str = ""
    confidence: float = 1.0       # 源级别可信度（非结论置信度）
    note: str = ""

    def __post_init__(self):
        if not self.provenance_id:
            raw = f"{self.source}|{self.key}|{self.as_of}|{self.value}"
            self.provenance_id = "ev_" + hashlib.sha1(raw.encode()).hexdigest()[:10]

    def numeric(self) -> Optional[float]:
        try:
            return float(self.value)
        except (TypeError, ValueError):
            return None

    def fmt(self) -> str:
        v = self.value
        n = self.numeric()
        if n is not None:
            if self.unit == "%":
                v = f"{n * 100:.2f}%"
            elif abs(n) >= 1e8:
                v = f"{n / 1e8:.2f}亿"
            elif abs(n) >= 1e4:
                v = f"{n / 1e4:.2f}万"
            else:
                v = f"{n:,.4g}"
        return f"{v}{self.unit if self.unit not in ('%', '') else ''}"


# ================================================================== 包


@dataclass
class EvidenceBundle:
    uid: str                                  # "CN:600519"
    as_of: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    items: List[EvidenceItem] = field(default_factory=list)
    missing: List[Dict[str, str]] = field(default_factory=list)   # 降级留痕
    warnings: List[str] = field(default_factory=list)

    # ---------------- 构建 ----------------

    def add(self, **kw) -> "EvidenceBundle":
        self.items.append(EvidenceItem(**kw))
        return self

    def add_missing(self, key: str, reason: str, source: str = "") -> None:
        """
        逐源降级留痕。v1.7.1 已有此机制，保留并结构化。
        关键：失败源必须写明原因，避免模型在无数据支撑时臆测。
        """
        self.missing.append({"key": key, "source": source, "reason": reason})
        self.items.append(EvidenceItem(
            key=key, label=key, value=None, source=source or "unavailable",
            as_of=self.as_of, freshness="unavailable", confidence=0.0,
            note=f"未获得（{reason}）",
        ))

    # ---------------- 质量 ----------------

    def coverage(self) -> float:
        """证据覆盖率：有值条目 / 总条目。"""
        if not self.items:
            return 0.0
        ok = sum(1 for i in self.items if i.value is not None and i.freshness != "unavailable")
        return ok / len(self.items)

    def freshness_score(self) -> float:
        weights = {"realtime": 1.0, "delayed": 0.85, "eod": 0.7,
                   "stale": 0.35, "unavailable": 0.0, "unknown": 0.5}
        if not self.items:
            return 0.0
        return sum(weights.get(i.freshness, 0.5) for i in self.items) / len(self.items)

    def numeric_values(self) -> List[float]:
        return [n for n in (i.numeric() for i in self.items) if n is not None]

    def by_key(self, key: str) -> Optional[EvidenceItem]:
        for i in self.items:
            if i.key == key:
                return i
        return None

    # ---------------- 渲染 ----------------

    def to_prompt_block(self, max_items: int = 80, group_by_prefix: bool = True) -> str:
        """
        渲染给 LLM 的证据块。要求模型以 [id] 形式引用。
        """
        rows: List[str] = [f"# 证据包（标的 {self.uid}，生成于 {self.as_of}）",
                           "规则：你只能使用下列证据中的数值；需要引用时写 [id]；",
                           "      不得推断、外推或计算证据中不存在的数字；",
                           "      数据缺失时明确写『未获得』，不得猜测。", ""]
        items = [i for i in self.items if i.value is not None][:max_items]
        if group_by_prefix:
            items.sort(key=lambda i: i.key)
        for i in items:
            tail = []
            if i.as_of:
                tail.append(i.as_of)
            if i.freshness in ("stale", "delayed"):
                tail.append(i.freshness)
            if i.source:
                tail.append(i.source)
            rows.append(f"- [{i.provenance_id}] {i.label} = {i.fmt()}"
                        + (f"  ({', '.join(tail)})" if tail else ""))
        if self.missing:
            rows += ["", "## 未获得的数据（禁止臆测）"]
            for m in self.missing[:20]:
                rows.append(f"- {m['key']}：未获得（{m['reason']}）")
        return "\n".join(rows)

    # ---------------- 序列化 ----------------

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "as_of": self.as_of,
            "items": [asdict(i) for i in self.items],
            "missing": self.missing,
            "warnings": self.warnings,
            "quality": {"coverage": self.coverage(), "freshness": self.freshness_score()},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @property
    def hash(self) -> str:
        """证据指纹：用于幂等与研究留痕版本管理。"""
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]


# ================================================================== 防幻觉


_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?%?")

# 报告中常见的"结构性数字"，不要求出现在证据里（如"3 个理由""第 2 季度"）
_ALLOW_LITERALS = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 20, 30, 50, 100,
                   0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0}


def verify_against_evidence(
    report: str,
    bundle: EvidenceBundle,
    rel_tol: float = 1e-4,
    abs_tol: float = 1e-6,
    allow_derived: bool = True,
) -> Dict[str, object]:
    """
    扫描报告，找出证据包中不存在的数字（疑似幻觉）。

    allow_derived=True 时，允许"证据值的简单派生"：
      - 百分比形式（×100）与原始值互认
      - 亿/万 单位换算
      - 证据值之间的加减组合（如两个季度相减）
    """
    allowed = bundle.numeric_values()
    # 扩展允许的派生值
    derived: List[float] = []
    for v in allowed:
        derived += [v * 100, v / 100, v * 1e8, v / 1e8, v * 1e4, v / 1e4, abs(v), -v]
    if allow_derived and len(allowed) <= 60:
        arr = np.array(allowed) if (np := _np()) is not None else None
        if arr is not None and len(arr) > 1:
            # 两两差值（同比/环比增量常由两个证据值相减得到）
            d = (arr[:, None] - arr[None, :]).ravel()
            derived.extend(d.tolist())
    pool = set(allowed) | set(derived) | _ALLOW_LITERALS

    suspects: List[str] = []
    for tok in _NUM_RE.findall(report or ""):
        clean = tok.replace(",", "")
        is_pct = clean.endswith("%")
        try:
            x = float(clean.rstrip("%"))
        except ValueError:
            continue
        if is_pct:
            x = x / 100.0
        if any(abs(x - a) <= max(abs_tol, abs(a) * rel_tol) for a in pool):
            continue
        suspects.append(tok)

    return {
        "clean": len(suspects) == 0,
        "suspect_count": len(suspects),
        "suspects": suspects[:30],
        "checked_tokens": len(_NUM_RE.findall(report or "")),
        "evidence_values": len(allowed),
    }


def _np():
    try:
        import numpy as np
        return np
    except ImportError:
        return None


# ================================================================== 置信度（代码计算）


def compute_confidence(
    bundle: EvidenceBundle,
    agreement: float = 0.5,
    strategy_hit_rate: float = 0.5,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    """
    置信度改由代码计算，**不再让模型自报**。
    返回带解释的结果，UI 上置信度环可点击展开 why。
    """
    w = {"coverage": 0.35, "freshness": 0.20, "agreement": 0.25, "history": 0.20}
    if weights:
        w.update(weights)

    cov, fresh = bundle.coverage(), bundle.freshness_score()
    agree = min(max(agreement, 0.0), 1.0)
    hist = min(max(strategy_hit_rate, 0.0), 1.0)

    score = w["coverage"] * cov + w["freshness"] * fresh + w["agreement"] * agree + w["history"] * hist
    score = round(min(max(score, 0.0), 1.0), 3)

    why = (f"证据覆盖 {cov:.0%}（权重 {w['coverage']:.0%}）· "
           f"数据新鲜度 {fresh:.0%}（{w['freshness']:.0%}）· "
           f"多角色一致度 {agree:.0%}（{w['agreement']:.0%}）· "
           f"该策略历史命中 {hist:.0%}（{w['history']:.0%}）")

    grade = "高" if score >= 0.7 else "中" if score >= 0.5 else "低"
    return {
        "confidence": score,
        "grade": grade,
        "why": why,
        "components": {"coverage": cov, "freshness": fresh,
                       "agreement": agree, "history": hist},
        "caveat": "启发式信号强度，非统计置信度，请勿据此重仓决策。",
        "disclaimer": DISCLAIMER,
    }


# ================================================================== 结论结构


@dataclass
class ResearchConclusion:
    """强制每个角色输出可证伪的结论。invalidation 与 verify_by 是本方案最重要的字段。"""
    view: str = "neutral"                       # constructive / neutral / cautious
    claims: List[Dict[str, Any]] = field(default_factory=list)
    invalidation: str = ""                      # 证伪条件
    verify_by: List[str] = field(default_factory=list)   # 验证指标
    horizon: str = "3-6个月"
    evidence_refs: List[str] = field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def audit(self, bundle: EvidenceBundle) -> Dict[str, object]:
        """结论自检：引用是否存在、证伪条件是否给出。"""
        ids = {i.provenance_id for i in bundle.items}
        bad_refs = [r for r in self.evidence_refs if r not in ids]
        text = " ".join(c.get("text", "") for c in self.claims)
        halluc = verify_against_evidence(text, bundle)
        return {
            "valid": not bad_refs and bool(self.invalidation) and halluc["clean"],
            "bad_refs": bad_refs,
            "has_invalidation": bool(self.invalidation),
            "has_verify_by": bool(self.verify_by),
            "hallucination": halluc,
        }


# ================================================================== 构建


def build_research_bundle(symbol: str) -> EvidenceBundle:
    """通过 gateway 收集全部可用证据，打包为 EvidenceBundle。

    供 research_dag 的 stock.collect 节点调用。每个类别独立 try/except，
    单类失败不影响其他；缺失类别走 add_missing 留痕。
    """
    uid = symbol
    try:
        from .secmaster import resolve_symbol
        sid = resolve_symbol(symbol)
        if sid and getattr(sid, "uid", None):
            uid = sid.uid
    except Exception:
        pass

    bundle = EvidenceBundle(uid=uid)

    try:
        from .gateway import gateway
    except Exception:
        bundle.warnings.append("gateway unavailable")
        return bundle

    # 1) 实时快照
    try:
        q = gateway.get_quote(symbol)
        if isinstance(q, dict):
            for k in ("price", "close", "last"):
                if k in q and q[k] is not None:
                    bundle.add(key=f"quote.{k}", label="最新价", value=q[k],
                               unit="CNY", source=q.get("source", ""),
                               freshness="realtime", as_of=q.get("time", ""))
                    break
    except Exception as err:
        bundle.add_missing("quote.price", reason=str(err)[:80])

    # 2) 财报
    fund_data = None
    try:
        fund = gateway.get_fundamental(symbol)
        if isinstance(fund, dict):
            fund_data = fund
            for tbl_name in ("income", "balance", "cashflow", "fina_indicator"):
                rows = fund.get(tbl_name, [])
                if rows and isinstance(rows, list):
                    latest = rows[0] if rows else {}
                    for field_key in ("revenue", "net_profit", "total_assets",
                                      "roe", "eps", "gross_margin"):
                        if field_key in latest and latest[field_key] is not None:
                            bundle.add(
                                key=f"{tbl_name}.{field_key}",
                                label=f"{tbl_name}.{field_key}",
                                value=latest[field_key],
                                source="tushare", freshness="eod",
                                as_of=str(latest.get("end_date", "")),
                            )
    except Exception as err:
        bundle.add_missing("fundamental", reason=str(err)[:80])

    # 3) 资金流
    try:
        mf = gateway.get_moneyflow(symbol)
        if mf is not None and hasattr(mf, "columns") and "net_mf_amount" in mf.columns:
            net = float(mf["net_mf_amount"].tail(1).iloc[0]) if len(mf) > 0 else 0
            bundle.add(key="flow.net_mf", label="主力净流入(最近)", value=net,
                       unit="万元", source="tushare", freshness="eod")
    except Exception as err:
        bundle.add_missing("moneyflow", reason=str(err)[:80])

    # 4) 估值分位
    try:
        from .valuation import pe_pb_percentile, valuation_line
        val = pe_pb_percentile(symbol)
        if val:
            bundle.add(key="valuation.pe_ttm", label="PE(TTM)", value=val.get("pe_ttm"),
                       source="tushare", freshness="eod")
            bundle.add(key="valuation.pe_pct_5y", label="PE 5年分位",
                       value=val.get("pe_percentile_5y"), unit="%",
                       source="tushare", freshness="eod")
            bundle.add(key="valuation.pb", label="PB", value=val.get("pb"),
                       source="tushare", freshness="eod")
    except Exception as err:
        bundle.add_missing("valuation", reason=str(err)[:80])

    # 5) 一致预期（可选，FMP 凭据存在时）
    consensus_data = None
    try:
        consensus = gateway.get_consensus(symbol)
        if isinstance(consensus, (dict, list)):
            consensus_data = consensus
            if isinstance(consensus, dict):
                for k, v in consensus.items():
                    if v is not None:
                        bundle.add(key=f"consensus.{k}", label=f"一致预期.{k}",
                                   value=v, source="fmp", freshness="eod")
    except Exception as err:
        bundle.add_missing("consensus", reason=str(err)[:80])

    # 5.5) 预期差（一致预期 vs 实际业绩）
    if consensus_data and fund_data:
        try:
            from .expectation_gap import compute_gap, format_gap_card
            gap_result = compute_gap(consensus_data, fund_data, symbol)
            if gap_result.items:
                gap_text = format_gap_card(gap_result)
                bundle.add(key="gap.verdict", label="预期差判定",
                           value=gap_result.verdict, source=gap_result.source,
                           freshness="eod")
                bundle.add(key="gap.card", label="预期差卡片",
                           value=gap_text, source=gap_result.source,
                           freshness="eod")
                if gap_result.overall_surprise is not None:
                    bundle.add(key="gap.surprise_pct", label="综合偏差%",
                               value=gap_result.overall_surprise, unit="%",
                               source=gap_result.source, freshness="eod")
        except Exception as err:
            bundle.add_missing("expectation_gap", reason=str(err)[:80])

    # 6) 新闻
    try:
        news = gateway.get_news(symbol)
        if isinstance(news, list) and news:
            bundle.add(key="news.headlines", label="近期新闻",
                       value="; ".join(str(t)[:60] for t in news[:5]),
                       source="sina", freshness="delayed")
    except Exception as err:
        bundle.add_missing("news", reason=str(err)[:80])

    return bundle


if __name__ == "__main__":
    b = EvidenceBundle(uid="CN:600519")
    b.add(key="quote.price", label="最新价", value=1684.0, unit="CNY",
          as_of="2026-09-06T15:00:00+08:00", source="tencent", freshness="realtime")
    b.add(key="income.revenue_ttm", label="营业收入(TTM)", value=1.742e11, unit="CNY",
          as_of="2025Q3", source="tushare", freshness="eod")
    b.add(key="ratio.gross_margin", label="毛利率", value=0.918, unit="%",
          as_of="2025Q3", source="tushare", freshness="eod")
    b.add_missing("consensus.eps_fy2", reason="未配置 FMP 凭据")

    print(b.to_prompt_block())
    print("\n质量:", {"coverage": round(b.coverage(), 3), "freshness": round(b.freshness_score(), 3)})
    print("置信度:", compute_confidence(b, agreement=0.66, strategy_hit_rate=0.57))

    good = "毛利率 91.8%，营收规模 1742.0亿，估值处于历史中位。"
    bad = "预计明年净利润增长 37.5%，目标价 2450 元。"
    print("\n合规报告校验:", verify_against_evidence(good, b))
    print("含幻觉报告校验:", verify_against_evidence(bad, b))
