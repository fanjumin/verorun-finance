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

# ── 实时出流可用性（NF-03 收尾，2026-10-04 生产实测驱动）──
# 本端点的出流源是 stock_analysis 的 sa_sse_events。该插件缺席/未激活的 edition
# 上，原实现会「每秒一条 WARNING + 只发心跳」无限空转：既不下发数据又刷爆日志
# （生产实测：单连接 20 秒产生 35 条同因告警）。现在改为：
#   建流前探活 → 路由直接 503；流内出流失效 → 同因错误节流记录并有界退出，
#   把「静默空转」变成「显式降级」，交回客户端 EventSource 的退避重连。
OUTBOX_PROBE_TTL = 60.0     # 探活结果缓存（秒），避免每次建流都打库
OUTBOX_ERROR_THROTTLE = 300.0   # 同因 poll 错误的再记录间隔（秒）
OUTBOX_MAX_CONSECUTIVE = 3  # 连续失败多少轮判定出流失效并终止本流

_HEARTBEAT = ": heartbeat\n\n"

_probe_cache = {"ts": 0.0, "reason": None}
_poll_err = {"sig": None, "ts": 0.0, "suppressed": 0}


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    try:
        return float(obj)
    except (TypeError, ValueError):
        return str(obj)


def _list_sse_events(after_id=None, topics=None, limit=POLL_LIMIT):
    """只读借用 stock_analysis 的出流查询（依赖缺席时按原样抛异常）。"""
    from plugins.stock_analysis import models_sa
    return models_sa.list_sse_events(after_id=after_id, topics=topics, limit=limit)


def probe_outbox(force=False):
    """探测实时出流是否可用；返回 None=可用，否则返回不可用原因（仅服务端留痕）。

    探活就是一次 `limit=1` 的真实读取——同时覆盖「依赖模块缺席」与「表未建」
    两类失效，不做字符串猜测。结果按 TTL 缓存，防止探活本身变成新的库压力。
    """
    now = time.monotonic()
    if not force and now - _probe_cache["ts"] < OUTBOX_PROBE_TTL:
        return _probe_cache["reason"]
    reason = None
    try:
        _list_sse_events(after_id=0, topics=list(TOPICS), limit=1)
    except Exception as err:
        reason = "%s: %s" % (type(err).__name__, err)
    _probe_cache["ts"] = now
    _probe_cache["reason"] = reason
    return reason


def reset_outbox_probe():
    """清掉探活缓存（单测/依赖插件刚装好时用），不影响任何运行态。"""
    _probe_cache["ts"] = 0.0
    _probe_cache["reason"] = None


def _note_poll_error(err):
    """同因 poll 错误节流记录：首现即 WARNING，此后每 THROTTLE 秒汇总一次。"""
    sig = "%s: %s" % (type(err).__name__, err)
    now = time.monotonic()
    if sig != _poll_err["sig"]:
        _poll_err["sig"] = sig
        _poll_err["ts"] = now
        _poll_err["suppressed"] = 0
        _log.warning("sse poll failed: %s", sig)
        return
    _poll_err["suppressed"] += 1
    if now - _poll_err["ts"] >= OUTBOX_ERROR_THROTTLE:
        _log.warning("sse poll still failing: %s（%ds 内另有 %d 次同因失败已抑制）",
                     sig, int(OUTBOX_ERROR_THROTTLE), _poll_err["suppressed"])
        _poll_err["ts"] = now
        _poll_err["suppressed"] = 0


def parse_topics(raw):
    """解析 topics 参数 → (topics, error)。仅放行 flow。"""
    if not raw or not raw.strip():
        return list(TOPICS), None
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    # NF-14：保序去重——"flow,flow" 不得原样展开成 IN (?, ?) 的冗余占位。
    deduped = list(dict.fromkeys(tokens))
    unknown = [t for t in deduped if t not in TOPICS]
    if unknown:
        return None, "unknown topic: %s" % unknown[0]
    return deduped, None


def parse_last_id(raw):
    """解析 Last-Event-ID → (id, error)。

    空值 → (0, None)；纯数字 → (int, None)；
    非法（非数字 / 负数 / 小数）→ (None, "invalid Last-Event-ID")。
    在 SSE 响应建立前校验，避免坏 header 在 200 之后崩进生成器。
    """
    if raw is None:
        return 0, None
    text = str(raw).strip()
    if not text:
        return 0, None
    if not text.isdigit():
        return None, "invalid Last-Event-ID"
    return int(text), None


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
    poll = poll_fn or _list_sse_events
    cursor = int(last_id) if last_id else 0
    now = time.monotonic()
    last_send = now
    last_auth = now
    poll_errors = 0
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
            poll_errors = 0
        except Exception as err:
            _note_poll_error(err)
            poll_errors += 1
            if poll_errors >= OUTBOX_MAX_CONSECUTIVE:
                # 出流连续失效：显式告知并结束本流（客户端退避重连后会拿到 503），
                # 不再以「心跳 + 逐秒告警」的形式无限空转。
                yield format_frame(None, "system.notice",
                                   {"code": "OUTBOX_UNAVAILABLE"})
                return
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
    """兼容别名：出流读取已收敛到 `_list_sse_events`（探活与轮询共用一条路径）。"""
    return _list_sse_events(after_id=after_id, topics=topics, limit=limit)
