# research_dag.py — 多智能体研究流水线 DAG 节点处理器（P1 W12-13）
#
# 平台契约（对齐 deep_research.py 已验证模式）：
# - handler(node_def, input_data) 严格两参
# - 上游输出位于 input_data["node_<上游id>_output"]
# - LLM 走 UnifiedLLM.chat()，装配复刻 discuss_research._chat
# - 角色提示词读 agent_matrix/prompts/rs_*.md（单一事实源，零硬编码提示词）
#
# DAG 拓扑（A3.8A-4 映射）：
#   plan → collect → audit → [fundamental | quant | valuation](并行) → risk → pm
# 并行靠 DAG 依赖边 + worker 池（内核无 parallel 节点类型）。
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional

_log = logging.getLogger("stock_analysis.research_dag")

# 测试注入缝（生产保持 None）
LLM_FACTORY = None
EVIDENCE_FETCHER = None


# ================================================================ LLM 工具

def _chat(prompt: str, *, temperature: float = 0.3, max_tokens: int = 2048) -> str:
    if LLM_FACTORY is not None:
        return LLM_FACTORY([{"role": "user", "content": prompt}])
    from agent_matrix.engine import UnifiedLLM
    from agent_matrix.model_resolver import resolve_model_args
    cfg = resolve_model_args({"strategy": "tier", "tier": "standard"})
    if not cfg.get("model_name") and cfg.get("model"):
        cfg["model_name"] = cfg["model"]
    return UnifiedLLM(cfg).chat(
        [{"role": "user", "content": prompt}],
        temperature=temperature, max_tokens=max_tokens,
        module="stock_analysis",
    )


def _load_prompt(slug: str) -> str:
    """从 agent_matrix/prompts/ 加载角色提示词。"""
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..",
                     "agent_matrix", "prompts", f"{slug}.md"),
        os.path.join("agent_matrix", "prompts", f"{slug}.md"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
    _log.warning("prompt file not found for %s, using fallback", slug)
    return ""


def _parse_json(text: str) -> dict:
    """从 LLM 输出中提取 JSON（兼容 ```json 包裹和尾部杂文）。"""
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return {}


def _cfg(node_def: dict) -> dict:
    return (node_def or {}).get("config") or {}


def _upstream(input_data: dict, *keys: str) -> Dict[str, Any]:
    """从 input_data 中提取上游节点输出。"""
    out = {}
    for k, v in (input_data or {}).items():
        if k.endswith("_output") and isinstance(v, dict):
            out[k] = v
    for key in keys:
        if key in (input_data or {}):
            out[key] = input_data[key]
    return out


def _find_upstream_field(input_data: dict, field: str, default=None):
    """在所有上游输出中搜索指定字段。"""
    for k, v in (input_data or {}).items():
        if isinstance(v, dict) and field in v:
            return v[field]
    return (input_data or {}).get(field, default)


# ================================================================ 节点处理器

def handle_rs_planner(node_def, input_data):
    """stock.rs_planner — 研究规划：分解假设 + 数据清单。"""
    cfg = _cfg(node_def)
    symbol = cfg.get("symbol") or _find_upstream_field(input_data, "symbol")
    question = cfg.get("question") or _find_upstream_field(input_data, "question")
    if not symbol:
        return {"success": False, "error": "symbol required"}

    prompt_base = _load_prompt("rs_planner")
    prompt = (f"{prompt_base}\n\nResearch question: {question or f'对 {symbol} 进行深度研判'}\n"
              f"Symbol: {symbol}")
    try:
        raw = _chat(prompt)
        parsed = _parse_json(raw)
        return {
            "success": True,
            "symbol": symbol,
            "hypotheses": parsed.get("hypotheses", []),
            "data_checklist": parsed.get("data_checklist", []),
            "raw": raw,
        }
    except Exception as err:
        return {"success": False, "symbol": symbol, "error": str(err)}


def handle_collect(node_def, input_data):
    """stock.collect — 证据收集：通过 gateway 打包 EvidenceBundle。"""
    symbol = _cfg(node_def).get("symbol") or _find_upstream_field(input_data, "symbol")
    if not symbol:
        return {"success": False, "error": "symbol required"}

    fetcher = EVIDENCE_FETCHER
    try:
        if fetcher:
            bundle = fetcher(symbol)
        else:
            from .evidence_bundle import build_research_bundle
            bundle = build_research_bundle(symbol)
        return {
            "success": True,
            "symbol": symbol,
            "bundle": bundle,
            "evidence_text": bundle.to_prompt_block() if hasattr(bundle, "to_prompt_block") else str(bundle),
            "coverage": bundle.coverage() if hasattr(bundle, "coverage") else 0,
            "item_count": len(bundle.items) if hasattr(bundle, "items") else 0,
        }
    except Exception as err:
        return {"success": False, "symbol": symbol, "error": str(err)}


def handle_audit(node_def, input_data):
    """stock.audit — 数据质量审计（确定性代码节点）。"""
    bundle = _find_upstream_field(input_data, "bundle")
    symbol = _find_upstream_field(input_data, "symbol") or ""

    report = {
        "symbol": symbol,
        "coverage": 0.0,
        "freshness": 0.0,
        "item_count": 0,
        "missing_keys": [],
        "warnings": [],
        "quality_grade": "低",
    }
    if bundle is None:
        report["warnings"].append("no evidence bundle found upstream")
        return {"success": True, **report}

    try:
        report["coverage"] = round(bundle.coverage(), 3) if hasattr(bundle, "coverage") else 0
        report["freshness"] = round(bundle.freshness_score(), 3) if hasattr(bundle, "freshness_score") else 0
        report["item_count"] = len(bundle.items) if hasattr(bundle, "items") else 0
        report["missing_keys"] = [m.get("key", "") for m in (bundle.missing if hasattr(bundle, "missing") else [])]
        report["warnings"] = list(bundle.warnings) if hasattr(bundle, "warnings") else []

        score = report["coverage"] * 0.5 + report["freshness"] * 0.5
        report["quality_grade"] = "高" if score >= 0.7 else "中" if score >= 0.4 else "低"
    except Exception as err:
        report["warnings"].append(f"audit partial: {err}")

    return {"success": True, **report}


def _analyst_node(node_def, input_data, slug: str, view_key: str):
    """分析师节点通用模板（fundamental / quant 共用）。"""
    symbol = _find_upstream_field(input_data, "symbol") or ""
    evidence_text = _find_upstream_field(input_data, "evidence_text") or ""
    audit_report = _find_upstream_field(input_data, "quality_grade") or ""

    if not evidence_text:
        return {"success": False, "symbol": symbol, "error": "no evidence_text from upstream"}

    prompt_base = _load_prompt(slug)
    prompt = (f"{prompt_base}\n\nSymbol: {symbol}\n"
              f"Data quality: {audit_report}\n\n{evidence_text}")
    try:
        raw = _chat(prompt)
        parsed = _parse_json(raw)
        return {
            "success": True,
            "symbol": symbol,
            "view": parsed.get("view", "neutral"),
            view_key: parsed,
            "raw": raw,
        }
    except Exception as err:
        return {"success": False, "symbol": symbol, "error": str(err)}


def handle_rs_fundamental(node_def, input_data):
    """stock.rs_fundamental — 基本面分析师。"""
    return _analyst_node(node_def, input_data, "rs_fundamental", "fundamental_view")


def handle_rs_quant(node_def, input_data):
    """stock.rs_quant — 量价分析师。"""
    return _analyst_node(node_def, input_data, "rs_quant", "technical_view")


def handle_valuation(node_def, input_data):
    """stock.valuation — PE/PB 分位估值（确定性代码节点）。"""
    symbol = _find_upstream_field(input_data, "symbol") or ""
    try:
        from .valuation import pe_pb_percentile, valuation_line
        val = pe_pb_percentile(symbol)
        if not val:
            return {"success": True, "symbol": symbol, "valuation_range": None,
                    "note": "valuation data unavailable"}
        return {
            "success": True,
            "symbol": symbol,
            "valuation_range": {
                "pe_pb": val,
                "valuation_text": valuation_line(val),
            },
        }
    except Exception as err:
        return {"success": False, "symbol": symbol, "error": str(err)}


def handle_rs_risk(node_def, input_data):
    """stock.rs_risk — 反方风险官：专门挑刺。"""
    symbol = _find_upstream_field(input_data, "symbol") or ""
    evidence_text = _find_upstream_field(input_data, "evidence_text") or ""

    prior_views = []
    for key in ("fundamental_view", "technical_view", "valuation_range"):
        v = _find_upstream_field(input_data, key)
        if v:
            prior_views.append(f"[{key}]: {json.dumps(v, ensure_ascii=False, default=str)[:500]}")

    if not evidence_text:
        return {"success": False, "symbol": symbol, "error": "no evidence_text from upstream"}

    prompt_base = _load_prompt("rs_risk")
    prompt = (f"{prompt_base}\n\nSymbol: {symbol}\n\n"
              f"Prior analyst views:\n" + "\n".join(prior_views) + "\n\n"
              f"{evidence_text}")
    try:
        raw = _chat(prompt)
        parsed = _parse_json(raw)
        return {
            "success": True,
            "symbol": symbol,
            "bear_cases": parsed.get("bear_cases", []),
            "stress_tests": parsed.get("stress_tests", []),
            "raw": raw,
        }
    except Exception as err:
        return {"success": False, "symbol": symbol, "error": str(err)}


def handle_rs_pm(node_def, input_data):
    """stock.rs_pm — 组合经理：收敛仓位 + 最终研判。"""
    symbol = _find_upstream_field(input_data, "symbol") or ""
    evidence_text = _find_upstream_field(input_data, "evidence_text") or ""

    context_parts = []
    for key in ("fundamental_view", "technical_view", "valuation_range",
                "bear_cases", "stress_tests"):
        v = _find_upstream_field(input_data, key)
        if v:
            context_parts.append(f"[{key}]: {json.dumps(v, ensure_ascii=False, default=str)[:600]}")

    if not evidence_text:
        return {"success": False, "symbol": symbol, "error": "no evidence_text from upstream"}

    prompt_base = _load_prompt("rs_pm")
    prompt = (f"{prompt_base}\n\nSymbol: {symbol}\n\n"
              f"Analyst views and risk assessment:\n" + "\n".join(context_parts) + "\n\n"
              f"{evidence_text}")
    try:
        raw = _chat(prompt)
        parsed = _parse_json(raw)

        from .evidence_bundle import ResearchConclusion
        conclusion = ResearchConclusion(
            view=parsed.get("view", "neutral"),
            claims=parsed.get("claims", []),
            invalidation=parsed.get("invalidation", ""),
            verify_by=parsed.get("verify_by", []),
            horizon=parsed.get("horizon", "3-6个月"),
            evidence_refs=[c.get("evidence_refs", []) for c in parsed.get("claims", [])
                           if isinstance(c, dict) for _ in c.get("evidence_refs", [])],
        )

        bundle = _find_upstream_field(input_data, "bundle")
        audit_result = conclusion.audit(bundle) if bundle else {}

        return {
            "success": True,
            "symbol": symbol,
            "position_view": parsed.get("position_view", {}),
            "conclusion": parsed,
            "self_audit": audit_result,
            "raw": raw,
        }
    except Exception as err:
        return {"success": False, "symbol": symbol, "error": str(err)}


# ================================================================ 注册

def get_dag_nodes() -> dict:
    """返回全部投研 DAG 节点处理器，供 __init__.register_dag_nodes() 合并。"""
    return {
        "stock.rs_planner": handle_rs_planner,
        "stock.collect": handle_collect,
        "stock.audit": handle_audit,
        "stock.rs_fundamental": handle_rs_fundamental,
        "stock.rs_quant": handle_rs_quant,
        "stock.valuation": handle_valuation,
        "stock.rs_risk": handle_rs_risk,
        "stock.rs_pm": handle_rs_pm,
    }
