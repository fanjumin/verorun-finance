# discuss_research.py — 多空对辩研判（接线点① · 阶段 B，四阶段协议，插件侧实现）
#
# 协议形状对齐内核 Agent Discussion v2.0（agent_matrix/orchestrator.discuss_and_execute）：
#   Planner(多头) → Reviewer(风控空头) → Revise(多头修订) → Decider(决策委员)
# 生成与评审分离：Reviewer 只审查不参与生成；Decider 只依据双方材料定稿、不新增论据。
# 语义归属修正（方案 §2.1 结论）：内核 discuss_and_execute 按 domain 硬编码查角色
# （site_builder/ops/finance），直接复用会把多头研判交给建站角色——故插件侧复刻协议形状、
# 角色替换为多头分析师/风控空头/决策委员，LLM 仍走内核 UnifiedLLM，内核零改动。
#
# i18n 约定（用户既定标准：提示词以英文呈现）：四阶段模板为英文；Decider 要求输出
# 中文研判正文（200-400 字）+ 尾部严格 JSON（键保持英文），JSON 键与 _extract_signal /
# parse_structured_output 兼容。
#
# LLM 装配复刻 deep_research.py 已验证范式：resolve_model_args(standard tier) +
# H-1 model→model_name 映射 + UnifiedLLM.chat(messages, temperature=0.3,
# max_tokens=_LLM_MAX_TOKENS, module="stock_analysis")；保留 LLM_FACTORY 测试注入缝（生产 None）。
from __future__ import annotations

import os

# SAU-1：对辩终稿较长，2048 易被 max_tokens 截断成空/半截响应。提到 8192 并支持
# SA_LLM_MAX_TOKENS 覆盖（上限而非目标，未截断时不会增加生成量）。
_LLM_MAX_TOKENS = int(os.environ.get("SA_LLM_MAX_TOKENS") or 8192)

# 四阶段提示词（英文；{} 占位符在调用处 format）
PROMPTS = {
    "planner": (
        "You are the bullish research analyst. Build a long thesis for {symbol} from the "
        "evidence chain below.\n\nEvidence:\n{evidence}\n\nRequirements:\n"
        "1. Cite the evidence source for every claim (e.g. \"fundamental#2\"); never "
        "fabricate data.\n"
        "2. Structure your answer into three parts: core arguments, upside logic, and key "
        "assumptions."
    ),
    "reviewer": (
        "You are the risk-focused bearish researcher. You do NOT generate bullish "
        "arguments; you only review the plan and the evidence chain below.\n\n"
        "Bullish thesis:\n{plan_v1}\n\nEvidence chain:\n{evidence}\n\nFocus on:\n"
        "1) holes or misreads in the evidence chain;\n"
        "2) ignored counter-evidence and downside risks;\n"
        "3) missing data items.\n\n"
        "Output a numbered issues list and a revised_steps list of suggested amendments."
    ),
    "revise": (
        "You are the bullish analyst. Answer each risk objection and revise your thesis.\n\n"
        "Original thesis:\n{plan_v1}\n\nRisk objections:\n{review}\n\n"
        "Output revised plan_v2; explicitly mark which objections were adopted and which "
        "were rejected, with reasons."
    ),
    "decider": (
        "You are the investment decision committee. You do NOT create new arguments; "
        "decide solely from the materials below.\n\n"
        "Revised bullish thesis:\n{plan_v2}\n\nRisk objections:\n{review}\n\n"
        "Write a {symbol} verdict of 200-400 Chinese characters that shows how the bearish "
        "view was weighted. Then output strict JSON only:\n"
        "{{\"signal\":\"buy|sell|hold\",\"confidence\":0 to 1,\"reasons\":[\"...\"],"
        "\"summary\":\"...\",\"evidence_refs\":[\"...\"],"
        "\"dissent\":\"unresolved bearish reservation\"}}"
    ),
}

# 测试注入缝（生产保持 None）：LLM_FACTORY(messages: list) -> str（对齐 deep_research 缝）。
LLM_FACTORY = None


def _chat(prompt: str) -> str:
    """一次模型调用。装配与 deep_research._chat 完全一致；异常上抛由调用方归类。"""
    if LLM_FACTORY is not None:                     # 测试注入缝
        return LLM_FACTORY([{"role": "user", "content": prompt}])
    from agent_matrix.engine import UnifiedLLM
    from agent_matrix.model_resolver import resolve_model_args
    cfg = resolve_model_args({"strategy": "tier", "tier": "standard"})
    if not cfg.get("model_name") and cfg.get("model"):     # H-1 同款映射
        cfg["model_name"] = cfg["model"]
    return UnifiedLLM(cfg).chat([{"role": "user", "content": prompt}],
                                temperature=0.3, max_tokens=_LLM_MAX_TOKENS,
                                module="stock_analysis")


def _parse_final(final: str) -> dict:
    """Decider 输出结构化解析；失败保守兜底 hold/0.5（对齐 deep_research 兜底语义）。"""
    try:
        try:
            from .evidence import parse_structured_output
        except ImportError:          # 顶层脚本运行兜底
            from evidence import parse_structured_output
        sig = parse_structured_output(final)
        if sig:
            return sig
    except Exception:
        pass
    return {"signal": "hold", "confidence": 0.5,
            "reasons": ["structured output unavailable, conservative stance"]}


def run_discussed_research(symbol: str, evidence_text: str, emit=None) -> dict:
    """执行四阶段对辩研判。

    symbol / evidence_text 由调用方准备（jobs 分支或路由取证据，本函数不取数）；
    emit(phase: str, content: str) 可选回调，供 SSE discuss 主题逐轮出流（B-3），
    回调自身异常被吞，不影响对辩主链路。

    返回 {"report": Decider 原文, "signal": dict, "rounds": {四轮原文}}。
    任一阶段模型调用异常整体上抛（调用方归类任务失败并回置/终止）。
    """
    def _emit(phase, content):
        if emit is None:
            return
        try:
            emit(phase, content)
        except Exception:
            pass

    plan_v1 = _chat(PROMPTS["planner"].format(symbol=symbol, evidence=evidence_text))
    _emit("planner", plan_v1)

    review = _chat(PROMPTS["reviewer"].format(plan_v1=plan_v1, evidence=evidence_text))
    _emit("reviewer", review)

    plan_v2 = _chat(PROMPTS["revise"].format(plan_v1=plan_v1, review=review))
    _emit("revise", plan_v2)

    final = _chat(PROMPTS["decider"].format(symbol=symbol, plan_v2=plan_v2, review=review))
    _emit("decider", final)

    return {
        "report": final,
        "signal": _parse_final(final),
        "rounds": {"planner": plan_v1, "reviewer": review,
                   "revise": plan_v2, "decider": final},
    }
