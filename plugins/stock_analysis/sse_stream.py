"""sse_stream.py — 桌面端 SSE 事件流（D1-d，契约 §4 /api/events）

- sa_sse_events 出流表为唯一时序源：写方（告警触发/任务流转）插行，
  SSE 连接按 id 游标轮询，跨 gunicorn worker 顺序一致；
- 事件 id = 出流表自增 id：断线重连按 Last-Event-ID → DB 补发最近 POLL_LIMIT 条；
- 空闲 15s 发心跳注释行；每 30s 重验 JWT，失效发 system.notice(AUTH_EXPIRED) 后关闭；
- quotes 主题暂缓（待 watchlist 语义确定），parse_topics 放行 alerts/jobs/discuss（discuss
  承载对辩逐轮过程帧，阶段 B）。
"""
from __future__ import annotations

import json
import logging
import time

_log = logging.getLogger("stock_analysis.sse_stream")

TOPICS = ("alerts", "jobs", "discuss")    # quotes 暂缓（D1-d）；discuss = 对辩逐轮过程（阶段 B）
DEFAULT_TOPICS = "alerts,jobs"
POLL_INTERVAL = 1.0               # 出流轮询间隔（秒）
HEARTBEAT_INTERVAL = 15.0         # 心跳注释行间隔（秒）
REAUTH_INTERVAL = 30.0            # JWT 周期重验间隔（秒）
POLL_LIMIT = 200                  # 每轮拉取上限 = 断线补发上限

_HEARTBEAT = ": heartbeat\n\n"


def _json_default(obj):
    """json.dumps 兜底：Decimal/时间对象 → 可 JSON 化标量。"""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    try:
        return float(obj)
    except (TypeError, ValueError):
        return str(obj)


# ── 解析与帧格式化（纯函数，便于单测）──

def parse_topics(raw):
    """解析 topics 参数 → (topics, error)。

    raw 为空/全空 → 默认 alerts,jobs；含 quotes 或未知主题 → 返回 400 文案。
    """
    if not raw:
        return list(DEFAULT_TOPICS.split(",")), None
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if not tokens:
        return list(DEFAULT_TOPICS.split(",")), None
    unknown = [t for t in tokens if t not in TOPICS]
    if unknown:
        if "quotes" in unknown:
            return None, "quotes topic is not available yet"
        return None, "unknown topic: %s" % unknown[0]
    return tokens, None


_JOB_EVENT = {"running": "job.running", "done": "job.completed", "failed": "job.failed"}


def event_name(topic: str, payload: dict) -> str:
    """出流行 → SSE 命名帧事件名（契约 §4.1）。"""
    if topic == "alerts":
        return "alert.triggered"
    if topic == "discuss":
        return "discuss.round"
    return _JOB_EVENT.get((payload or {}).get("status"), "job.status")


def format_frame(event_id, event: str, payload: dict) -> str:
    """SSE 帧（id: 事件序号 / event: 命名 / data: JSON 负载）。"""
    data = json.dumps(payload, ensure_ascii=False, default=_json_default)
    if event_id is None:
        return "event: %s\ndata: %s\n\n" % (event, data)
    return "id: %d\nevent: %s\ndata: %s\n\n" % (event_id, event, data)


def _system_notice() -> str:
    return format_frame(None, "system.notice", {"code": "AUTH_EXPIRED"})


# ── 事件流生成器 ──

def stream_events(topics, last_id, token, auth_check,
                  poll_fn=None,
                  poll_interval=POLL_INTERVAL,
                  heartbeat_interval=HEARTBEAT_INTERVAL,
                  reauth_interval=REAUTH_INTERVAL):
    """SSE 帧生成器。auth_check(token) -> payload|None。

    首次即验权（失效发 AUTH_EXPIRED 后结束）；游标自 last_id 起步周期拉取出流事件；
    空闲达 heartbeat_interval 发心跳；达 reauth_interval 重验 token。
    poll_fn 可注入（默认 models_sa.list_sse_events），间隔可注入供单测缩时。
    """
    if auth_check(token) is None:
        yield _system_notice()
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
                yield _system_notice()
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
    from . import models_sa as sa
    return sa.list_sse_events(after_id=after_id, topics=topics, limit=limit)
