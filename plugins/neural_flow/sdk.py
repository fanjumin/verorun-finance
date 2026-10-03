"""sdk.py — 埋点 SDK（平台级，行业无关，方案 v1.3 §6.6）。

三件套：
  emit_span    显式埋点（行业特有业务节点）
  span_timer   上下文管理器：start/end 自动成对，异常自动补 end(status='error')
               ——span 完整性的服务端半边兜底（前端半边 = 5×P95 超时收敛）

双通道落点：
  实时：sa_sse_events(topic='flow')（跨插件直写，单连接铁律，见 __init__ 模块注释）
  归档：nf_flow_spans（自有表，30 天回放）

纪律（方案 §5）：
  - 双写均静默失败——可视化是旁路，绝不影响主流程；
  - tokens 关联失败必须显式 None（禁 0）；cost_usd 恒为估算；
  - payload.ts = 进程本地时钟，仅显示用；排序/游标一律走表 id/created_at。
"""
from __future__ import annotations

import time
import uuid

from plugin_manager.logger import get_plugin_logger

_log = get_plugin_logger("neural_flow")

# 通用核 stage（方案 §5.1，冻结）；扩展 stage 须由 Domain Profile stage_dict 声明，
# 未声明 stage 前端灰态渲染（fail-safe），此处不做硬校验（旁路宽容）。
CORE_STAGES = ("data", "indicator", "evidence", "decision", "llm", "output", "error")


def _build_payload(trace_id, domain, stage, event, entity, decision_type, model,
                   tokens, cost_usd, latency_ms, cache_hit, confidence,
                   status, message, meta):
    return {
        "span_id": "sp_%s" % uuid.uuid4().hex[:10],
        "trace_id": str(trace_id),
        "domain": domain or "platform",
        "entity": entity or {},
        "ts": time.time(),
        "stage": stage,
        "event": event,
        "decision_type": decision_type,
        "model": model,
        "tokens": tokens,          # None=未关联；禁 0 冒充
        "cost_usd": round(float(cost_usd or 0.0), 6),
        "latency_ms": latency_ms,
        "cache_hit": bool(cache_hit),
        "confidence": confidence,
        "status": status or "ok",
        "message": message or "",
        "meta": meta or {},
    }


def emit_span(trace_id, domain="platform", stage="data", event="end", *,
              entity=None, decision_type=None, model=None, tokens=None,
              cost_usd=0.0, latency_ms=None, cache_hit=False, confidence=None,
              status="ok", message="", meta=None,
              source=None, source_id=None) -> dict:
    """发射一条 flow span（双写，静默失败）。返回 payload（供测试断言）。

    source/source_id：采集器幂等键——同一源行重放时归档去重（唯一索引）
    且不再重发实时帧；自然埋点不传（None），不受唯一约束限制。
    """
    payload = _build_payload(trace_id, domain, stage, event, entity, decision_type,
                             model, tokens, cost_usd, latency_ms, cache_hit,
                             confidence, status, message, meta)
    # 归档通道（自有表）；返回 None = 源行重复（唯一索引命中）
    duplicate = False
    try:
        from .models_nf import insert_span
        duplicate = insert_span(payload, source=source, source_id=source_id) is None
    except Exception as err:
        _log.debug("span archive write failed: %s", err)
    if duplicate:
        return payload  # 重复源行：实时帧同样不重发（避免前端重复动画）
    # 实时通道（跨插件直写 stock_analysis 出流表；表存在性由其插件保证）
    try:
        from plugins.stock_analysis import models_sa
        models_sa.insert_sse_event("flow", payload)
    except Exception as err:
        _log.debug("span realtime write failed: %s", err)
    return payload


class span_timer:
    """start/end 自动成对的上下文管理器。

    用法：
        with span_timer(trace_id=job_id, domain="medical", stage="triage",
                        entity=ent) as span:
            result = do_triage(...)
            span.confidence = result.score
    异常路径自动 emit end(status='error', message=异常摘要)，杜绝悬垂 start 帧。
    """

    def __init__(self, trace_id, domain="platform", stage="data", *, entity=None,
                 model=None):
        self._args = (trace_id, domain, stage, entity, model)
        self._t0 = None
        self.confidence = None
        self.tokens = None
        self.cost_usd = 0.0
        self.message = ""
        self.meta = None

    def __enter__(self):
        self._t0 = time.time()
        trace_id, domain, stage, entity, model = self._args
        emit_span(trace_id, domain, stage, "start", entity=entity, model=model,
                  message=self.message)
        return self

    def __exit__(self, exc_type, exc, tb):
        trace_id, domain, stage, entity, model = self._args
        latency = int((time.time() - self._t0) * 1000) if self._t0 else None
        if exc is not None:
            emit_span(trace_id, domain, stage, "end", entity=entity, model=model,
                      latency_ms=latency, status="error",
                      message="%s: %s" % (exc_type.__name__, exc)[:200],
                      meta=self.meta)
        else:
            emit_span(trace_id, domain, stage, "end", entity=entity, model=model,
                      tokens=self.tokens, cost_usd=self.cost_usd,
                      latency_ms=latency, confidence=self.confidence,
                      message=self.message, meta=self.meta)
        return False  # 不吞业务异常
