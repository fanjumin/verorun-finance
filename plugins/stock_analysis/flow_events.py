"""flow_events.py — 神经中枢 flow span 发射器（P1，方案 §6.1.1）

双写：
  1) sa_sse_events(topic='flow')  —— 实时通道（SSE 消费，保留 1 天，够用）
  2) sa_flow_spans                —— 归档通道（回放/钻取，保留 30 天）

span 语义（方案 §5）：
  - span_id = 前端幂等去重唯一依据（Last-Event-ID 补发/轮询重复投递时丢弃）；
  - ts = 进程本地 time.time()，仅前端显示，禁止作排序/过滤依据；
  - 帧序即时序一律用出流/归档表 id 或 created_at。

双写均静默失败：可视化是旁路，绝不影响主流程。
"""
from __future__ import annotations

import logging
import time
import uuid

_log = logging.getLogger("stock_analysis.flow_events")


def emit_flow_span(trace_id, domain: str, stage: str, event: str = "end", *,
                   symbol=None, decision_type=None, model=None, tokens=None,
                   cost_usd=0.0, latency_ms=None, cache_hit=False,
                   confidence=None, status: str = "ok", message: str = "",
                   meta=None) -> dict | None:
    """发射一条 flow span（实时出流 + 归档双写）。

    任一写失败仅告警并返回 None（调用方记录，不向上抛）；
    成功返回 span 信元，供调用方复用（span_id 做幂等/关联）。
    """
    payload: dict = {
        "span_id": "sp_%s" % uuid.uuid4().hex[:10],
        "trace_id": str(trace_id),
        "domain": domain,
        "symbol": symbol,
        "ts": time.time(),
        "stage": stage,
        "event": event,
        "decision_type": decision_type,
        "model": model,
        "tokens": tokens or {},
        "cost_usd": round(float(cost_usd or 0.0), 6),
        "latency_ms": latency_ms,
        "cache_hit": cache_hit,
        "confidence": confidence,
        "status": status,
        "message": message,
        "meta": meta or {},
    }
    try:
        from . import models_sa as sa
        sa.ensure_tables()
        sa.insert_sse_event("flow", payload)     # 实时通道（topic='flow'）
        sa.insert_flow_span(payload)             # 归档通道
    except Exception as err:
        _log.warning("flow span emit failed trace=%s stage=%s: %s",
                     trace_id, stage, err)
        return None
    return payload