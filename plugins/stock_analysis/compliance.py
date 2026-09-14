"""compliance.py — 合规与审计模块（P2 W19-20）

职责：
  1. 研究留痕 — 版本号自动生成（prompt/indicator/evidence/model/agent）
  2. 适当性 — 输出分级（普通/专业/合格投资者），高风险标的二次确认
  3. 静默期 — 持仓相关标的发布前后 N 日禁止出结论
  4. 利益冲突 — 报告尾部自动追加免责声明
  5. 审计日志 — who/when/symbol/evidence_hash/model/cost 全留痕
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime
from typing import Any

from . import models_sa as db

_log = logging.getLogger("stock_analysis.compliance")

HIGH_RISK_TAGS = {"ST", "*ST", "退市风险", "可转债", "衍生品", "期权", "期货"}

SUITABILITY_LEVELS = {
    "normal": "普通投资者",
    "professional": "专业投资者",
    "qualified": "合格投资者",
}

DEFAULT_DISCLAIMER = (
    "本系统及运营方不持有所述标的，本报告仅供参考，不构成投资建议。"
    "投资有风险，入市需谨慎。"
)


def compute_evidence_hash(data_sources: list[str] | None,
                          indicators: list[str] | None,
                          model: str | None = None) -> str:
    """根据数据来源+指标+模型生成证据哈希（用于留痕去重/追溯）。"""
    parts = sorted(data_sources or []) + sorted(indicators or [])
    if model:
        parts.append(model)
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def generate_version(*components: str | None) -> str:
    """根据组件版本号自动生成研究版本号（任一变更即新版本）。"""
    parts = [c or "unknown" for c in components]
    raw = ":".join(parts)
    return "v" + hashlib.md5(raw.encode()).hexdigest()[:8]


def classify_suitability(symbol: str, signal: str | None = None,
                         confidence: float | None = None,
                         tags: list[str] | None = None) -> dict:
    """评估输出适当性等级 + 是否需要二次确认。

    返回: {level, level_name, requires_confirmation, reasons}
    """
    reasons = []
    requires_confirmation = False
    level = "normal"

    all_tags = set(tags or [])
    if signal:
        all_tags.add(signal)

    for tag in all_tags:
        if tag in HIGH_RISK_TAGS or any(r in tag for r in ("ST", "退市")):
            requires_confirmation = True
            reasons.append(f"高风险标的: {tag}")
            level = "professional"

    if confidence is not None and confidence < 50:
        reasons.append(f"置信度偏低({confidence:.1f}%)")
        if level == "normal":
            level = "professional"

    if requires_confirmation and level == "normal":
        level = "professional"

    return {
        "level": level,
        "level_name": SUITABILITY_LEVELS.get(level, level),
        "requires_confirmation": requires_confirmation,
        "reasons": reasons,
    }


def check_silence(symbol: str, user_id: str, check_date: date | None = None) -> dict:
    """检查静默期约束。返回 {blocked, reason, silence_config}。"""
    is_active = db.check_silence_active(symbol, user_id, check_date)
    if is_active:
        return {
            "blocked": True,
            "reason": f"标的 {symbol} 处于静默期，禁止发布分析结论",
            "symbol": symbol,
        }
    return {"blocked": False, "symbol": symbol}


def generate_disclaimer(symbols: list[str] | None = None) -> str:
    """生成利益冲突声明。"""
    base = DEFAULT_DISCLAIMER
    if symbols:
        sym_str = "、".join(symbols[:10])
        return f"涉及标的：{sym_str}。{base}"
    return base


def log_analysis(who: str, symbol: str | None, action: str,
                 evidence_hash: str | None = None,
                 model: str | None = None,
                 prompt_version: str | None = None,
                 indicator_version: str | None = None,
                 agent_versions: dict | None = None,
                 cost: float | None = None,
                 result_summary: dict | None = None) -> None:
    """记录一条分析审计日志。"""
    db.insert_audit(
        who=who, action=action, symbol=symbol,
        evidence_hash=evidence_hash, model=model,
        prompt_version=prompt_version,
        indicator_version=indicator_version,
        agent_versions=agent_versions, cost=cost,
        result_summary=result_summary,
    )


def run_compliance_check(result: dict, user_context: dict | None = None) -> dict:
    """对分析结果执行完整合规检查。

    result: {symbol, signal, confidence, tags?, data_sources?, indicators?, model?}
    user_context: {user_id, suitability_level?}

    返回: {
      passed: bool,
      flags: [{type, message, severity}],
      disclaimer: str,
      evidence_hash: str,
      version: str,
      suitability: {...},
      silence: {...},
    }
    """
    flags = []
    symbol = result.get("symbol", "")
    signal = result.get("signal")
    confidence = result.get("confidence")
    tags = result.get("tags") or []
    data_sources = result.get("data_sources") or []
    indicators = result.get("indicators") or []
    model = result.get("model")
    user_id = (user_context or {}).get("user_id", "anonymous")
    user_level = (user_context or {}).get("suitability_level", "normal")

    evidence_hash = compute_evidence_hash(data_sources, indicators, model)
    version = generate_version(
        result.get("prompt_version"),
        result.get("indicator_version"),
        evidence_hash,
        model,
    )

    suitability = classify_suitability(symbol, signal, confidence, tags)
    if suitability["requires_confirmation"]:
        if user_level == "normal":
            flags.append({
                "type": "suitability",
                "message": f"该输出需要专业投资者确认: {'; '.join(suitability['reasons'])}",
                "severity": "warning",
            })
        for reason in suitability["reasons"]:
            flags.append({
                "type": "high_risk",
                "message": reason,
                "severity": "info",
            })

    silence = check_silence(symbol, user_id)
    if silence["blocked"]:
        flags.append({
            "type": "silence_period",
            "message": silence["reason"],
            "severity": "error",
        })

    if not data_sources:
        flags.append({
            "type": "missing_sources",
            "message": "分析结果缺少数据来源标注（合规要求）",
            "severity": "warning",
        })

    disclaimer = generate_disclaimer([symbol] if symbol else None)
    passed = all(f["severity"] != "error" for f in flags)

    return {
        "passed": passed,
        "flags": flags,
        "disclaimer": disclaimer,
        "evidence_hash": evidence_hash,
        "version": version,
        "suitability": suitability,
        "silence": silence,
    }


def submit_for_approval(result_type: str, payload: dict,
                        title: str | None = None,
                        result_id: int | None = None,
                        submitter: str | None = None) -> int:
    """创建并提交审批。"""
    approval_id = db.create_approval(
        result_type=result_type, payload=payload,
        title=title, result_id=result_id, submitter=submitter,
    )
    db.submit_approval(approval_id, submitter or "anonymous")
    _log.info("approval submitted: id=%d type=%s by=%s",
              approval_id, result_type, submitter)
    return approval_id


def list_pending_approvals() -> list:
    """获取待审批列表。"""
    return db.list_approvals(status="submitted")
