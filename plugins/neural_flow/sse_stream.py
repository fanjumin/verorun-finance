"""sse_stream.py — SSE 帧生成器（纯函数，范式同 stock_analysis/sse_stream.py）。

本插件的 events 端点为**备用实时通道**（finance 版桌面端默认仍走 stock_analysis
单连接，见 __init__ 模块注释的 B3 约束）；无 stock_analysis 的 edition 用本端点。

出流源复用 sa_sse_events(topic='flow')（跨插件只读，与实时写入同一时序表，
游标口径一致）；断线续传 = Last-Event-ID → 出流表自增 id 补发。
"""
from __future__ import annotations

import json
import time

from plugin_manager.logger import get_plugin_logger

_log = get_plugin_logger("neural_flow")

TOPICS = ("flow",)
POLL_INTERVAL = 1.0
HEARTBEAT_INTERVAL = 15.0
REAUTH_INTERVAL = 30.0
POLL_LIMIT = 200

_HEARTBEAT = ": heartbeat\n\n"


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    try:
        return float(obj)
    except (TypeError, ValueError):
        return str(obj)


def parse_topics(raw):
    """解析 topics 参数 → (topics, error)。仅放行 flow。"""
    if not raw or not raw.strip():
        return list(TOPICS), None
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    unknown = [t for t in tokens if t not in TOPICS]
    if unknown:
        return None, "unknown topic: %s" % unknown[0]
    return tokens, None


def event_name(topic: str, payload: dict) -> str:
    """flow 主题帧名统一 flow.span（前端按 payload.stage/domain 分发渲染）。"""
    if topic == "flow":
        return "flow.span"
    return "system.notice"


def format_frame(event_id, event: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, default=_json_default)
    if event_id is None:
        return "event: %s\ndata: %s\n\n" % (event, data)
    return "id: %d\nevent: %s\ndata: %s\n\n" % (event_id, event, data)


def stream_events(topics, last_id, token, auth_check,
                  poll_fn=None, poll_interval=POLL_INTERVAL,
                  heartbeat_interval=HEARTBEAT_INTERVAL,
                  reauth_interval=REAUTH_INTERVAL):
    """SSE 帧生成器（游标轮询 + 心跳 + 周期重验；与 stock_analysis 同构，
    poll_fn 可注入供单测缩时）。"""
    if auth_check(token) is None:
        yield format_frame(None, "system.notice", {"code": "AUTH_EXPIRED"})
        return
    poll = poll_fn or _poll_outbox
    cursor = int(last_id) if last_id else 0
    now = time.monotonic()
    last_send = now
    last_auth = now
    while True:
        now = time.monotonic()
        if now - last_auth >= reauth_interval:
            if auth_check(token) is None:
                yield format_frame(None, "system.notice", {"code": "AUTH_EXPIRED"})
                return
            last_auth = now
        sent = False
        try:
            rows = poll(after_id=cursor, topics=topics, limit=POLL_LIMIT)
        except Exception as err:
            _log.warning("sse poll failed: %s", err)
            rows = []
        for row in rows:
            cursor = row["id"]
            yield format_frame(cursor, event_name(row["topic"], row["payload"]),
                               row["payload"])
            sent = True
        if sent:
            last_send = time.monotonic()
        elif time.monotonic() - last_send >= heartbeat_interval:
            yield _HEARTBEAT
            last_send = time.monotonic()
        time.sleep(poll_interval)


def _poll_outbox(after_id=None, topics=None, limit=POLL_LIMIT):
    from plugins.stock_analysis import models_sa
    return models_sa.list_sse_events(after_id=after_id, topics=topics, limit=limit)
