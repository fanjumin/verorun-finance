"""alert_engine.py — 告警规则评估引擎（D1-c，契约 §3 /api/alerts + SSE alert.triggered）

- type 6 类对齐桌面 ALERT_TYPE_META（price_above/price_below/change_pct/
  rsi_oversold/rsi_overbought/signal_change）；channel 持久化/协议统一语义码
  in_app/email/im，旧中文行值（站内信/邮件/IM）读取时经 normalize_channel 归一；
- 状态机：active →（条件满足）→ triggered →（条件解除）→ active，信号复位后再次穿越可重触发；
- scan_alerts() 单轮扫描返回本轮触发事件列表；
- scheduled_scan() 为框架 APScheduler job（60s 一轮，advisory lock 防多 worker 双跑）的
  驱动入口，调用 scan_alerts() 并把返回事件交回调度方（D1-d SSE 广播挂接此返回）。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

try:
    from . import models_sa as sa
    from .gateway import DataCategory, gateway
    from .indicators import compute_indicators
except ImportError:  # 顶层脚本运行兜底
    from plugins.stock_analysis import models_sa as sa
    from plugins.stock_analysis.gateway import DataCategory, gateway
    from plugins.stock_analysis.indicators import compute_indicators

_log = logging.getLogger("stock_analysis.alert_engine")

ALERT_TYPES = {
    "price_above", "price_below", "change_pct",
    "rsi_oversold", "rsi_overbought", "signal_change",
}
CHANNEL_CODES = ("in_app", "email", "im")
# 旧桌面中文行值 → 语义码（兼容存量数据，读/写统一归一）
_LEGACY_CHANNEL_TO_CODE = {"站内信": "in_app", "邮件": "email", "IM": "im"}
# label / unit 与桌面 ALERT_TYPE_META 文案对齐（消息生成用）
ALERT_META = {
    "price_above": ("价格突破（≥）", "元"),
    "price_below": ("价格跌破（≤）", "元"),
    "change_pct": ("涨跌幅超过", "%"),
    "rsi_oversold": ("RSI 超卖（≤）", ""),
    "rsi_overbought": ("RSI 超买（≥）", ""),
    "signal_change": ("信号发生变化", ""),
}
_NEED_THRESHOLD = ALERT_TYPES - {"signal_change"}

# 定时扫描 advisory lock key（scheduled_scan 跨 worker 单跑）
_SCAN_LOCK_KEY = "sa_alert_scan"


def normalize_channel(raw) -> str:
    """【读容错】存量行值 → 语义码；空值或未知值回落 in_app。

    仅用于序列化/展示既有数据（历史脏行不该让页面崩）。
    ⚠️ 不可用于写入校验：未知值被静默改写会让调用方的合法性判断变成死代码
    （见 coerce_channel）。
    """
    if not raw:
        return "in_app"
    if raw in _LEGACY_CHANNEL_TO_CODE:
        return _LEGACY_CHANNEL_TO_CODE[raw]
    return raw if raw in CHANNEL_CODES else "in_app"


def coerce_channel(raw):
    """【写校验】入参渠道 → 语义码；无法识别的值返回 None（调用方回 400）。

    与 normalize_channel 的分工：读可以容错、写必须严格。
    - None / 空串 / 纯空白      → "in_app"（默认渠道，契约允许省略）
    - in_app / email / im       → 原样
    - 站内信 / 邮件 / IM（旧行值）→ 归一为对应语义码
    - 其它任意值                → None（拒绝，不静默改写用户意图）
    """
    if raw is None:
        return "in_app"
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if not text:
        return "in_app"
    if text in CHANNEL_CODES:
        return text
    if text in _LEGACY_CHANNEL_TO_CODE:
        return _LEGACY_CHANNEL_TO_CODE[text]
    return None


def serialize_rule(row: dict) -> dict:
    """规则行 → 契约 DTO（threshold Decimal→float|null，id 转字符串对齐桌面）。"""
    th = row.get("threshold")
    return {
        "id": str(row["id"]),
        "symbol": row["symbol"],
        "name": row.get("name") or row["symbol"],
        "type": row["type"],
        "threshold": float(th) if th is not None else None,
        "channel": normalize_channel(row["channel"]),
        "status": row["status"],
        "silent_from": row.get("silent_from"),
        "silent_to": row.get("silent_to"),
        "last_triggered_at": row.get("last_triggered_at"),
        "created_at": row.get("created_at"),
    }


def lookup_name(symbol: str) -> str | None:
    """创建规则时尽力取证券名（腾讯快照），失败返回 None（回落 symbol 展示）。"""
    try:
        q = gateway.get_quote(symbol, DataCategory.QUOTE)
        name = (q.get("name") or "").strip()
        return name[:64] or None
    except Exception as err:
        _log.warning("alert name lookup failed symbol=%s: %s", symbol, err)
        return None


# ── 引擎 ──

def _fmt_num(v) -> str:
    """去尾零数值文本（1350.0→1350，3.12→3.12）。"""
    return f"{float(v):g}" if v is not None else ""


def _in_silent_window(rule: dict) -> bool:
    """静默窗口（HH:MM 每日重复；from<=to 为同日区间，否则跨午夜）。"""
    sf, st = rule.get("silent_from"), rule.get("silent_to")
    if not sf or not st:
        return False
    now = datetime.now().time()
    if sf <= st:
        return sf <= now < st
    return now >= sf or now < st


def _fetch_rsi(symbol: str):
    """日线后复权 RSI14 末值；数据不足/异常返回 None（该轮跳过评估）。"""
    try:
        frame = gateway.get_kline(symbol, datalen=120)
        if frame is None or frame.empty:
            return None
        ind = compute_indicators(frame, basis="hfq")
        vals = ind["rsi14"]
        return vals[-1] if vals else None
    except Exception as err:
        _log.warning("alert rsi fetch failed symbol=%s: %s", symbol, err)
        return None


def _fetch_signal(symbol: str):
    """技术面信号（buy/sell/hold）；失败返回 None。"""
    try:
        from .stock_skill import StockAnalysisSkill
        sig = StockAnalysisSkill().get_signal(symbol)
        s = (sig or {}).get("signal")
        return s if s in {"buy", "sell", "hold"} else None
    except Exception as err:
        _log.warning("alert signal fetch failed symbol=%s: %s", symbol, err)
        return None


def _condition(rule: dict, quote=None, rsi=None, signal=None):
    """评估单条规则的当前条件。返回 (met, observed, message)。

    observed/message 在 met=False 时可为 None（triggered 状态解除复位时只用 met）。
    """
    typ = rule["type"]
    th = rule["threshold"]
    th_f = float(th) if th is not None else None
    name = rule.get("name") or rule["symbol"]
    label, unit = ALERT_META[typ]
    observed = None

    if typ == "price_above":
        price = float(quote.get("price") or 0)
        observed = price
        met = th_f is not None and price >= th_f
    elif typ == "price_below":
        price = float(quote.get("price") or 0)
        observed = price
        met = th_f is not None and price <= th_f
    elif typ == "change_pct":
        change = float(quote.get("change_pct") or 0)
        observed = round(change, 4)
        met = th_f is not None and abs(change) >= th_f
    elif typ == "rsi_oversold":
        observed = rsi
        met = rsi is not None and th_f is not None and rsi <= th_f
    elif typ == "rsi_overbought":
        observed = rsi
        met = rsi is not None and th_f is not None and rsi >= th_f
    else:  # signal_change：与基线比较；首扫建档由调用方处理
        new_signal = signal
        base = rule.get("last_signal")
        if base is None:
            return False, None, None
        met = new_signal is not None and new_signal != base
        if not met:
            return False, None, None
        return True, None, f"{name} 信号变化：{base} → {new_signal}"

    if not met:
        return False, observed, None
    return True, observed, f"{name} {label} {_fmt_num(th_f)}{unit}"


def _trigger(rule: dict, observed, message: str, signal: str = None) -> dict:
    """落触发状态 + 事件流水，返回 SSE 事件体。"""
    sa.set_alert_triggered(rule["id"], observed, signal=signal)
    sa.insert_alert_event(rule["id"], rule["symbol"], rule["type"],
                          rule.get("threshold"), observed, message)
    return {
        "alert_id": str(rule["id"]),
        "symbol": rule["symbol"],
        "name": rule.get("name") or rule["symbol"],
        "type": rule["type"],
        "threshold": float(rule["threshold"]) if rule.get("threshold") is not None else None,
        "observed": observed,
        "message": message,
        "channels": [normalize_channel(rule["channel"])],
        "at": time.strftime("%H:%M:%S"),
    }


def scan_alerts() -> list:
    """单轮全量扫描：返回本轮触发事件列表（调用方负责 SSE 广播与心跳）。

    对 active 规则：条件满足 → 触发；对 triggered 规则：条件不再满足 → 复位 active。
    数据获取失败/静默窗口内的规则本轮跳过。
    """
    events = []
    rules = []
    try:
        rules = sa.evaluable_alerts()
    except Exception as err:
        _log.warning("scan_alerts load rules failed: %s", err)
        return events

    # 按规则各自取数（规则量小，逐条评估；大范围监控由 D1-d 决定扫描节奏）
    for rule in rules:
        if _in_silent_window(rule):
            continue
        typ = rule["type"]
        met = observed = message = None
        new_signal = None
        try:
            if typ in {"price_above", "price_below", "change_pct"}:
                quote = gateway.get_quote(rule["symbol"], DataCategory.QUOTE)
                if typ in {"price_above", "price_below"}:
                    # DEF-06：行情缺失/价格为 0 或负时本轮跳过（continue），
                    # 避免 price_below 以 0 元误触发；也避免数据源故障把
                    # 已 triggered 的规则误复位——与 rsi/signal 缺失即跳过语义对齐。
                    try:
                        px = float((quote or {}).get("price"))
                    except (TypeError, ValueError):
                        px = 0.0
                    if px <= 0:
                        continue
                met, observed, message = _condition(rule, quote=quote)
            elif typ in {"rsi_oversold", "rsi_overbought"}:
                rsi = _fetch_rsi(rule["symbol"])
                if rsi is None:
                    continue
                met, observed, message = _condition(rule, rsi=rsi)
            else:  # signal_change
                signal = _fetch_signal(rule["symbol"])
                if signal is None:
                    continue
                if rule.get("last_signal") is None:
                    sa.set_alert_baseline(rule["id"], signal)   # 首扫建档，不触发
                    continue
                met, observed, message = _condition(rule, signal=signal)
                new_signal = signal if met else None
        except Exception as err:
            _log.warning("scan alert id=%s symbol=%s type=%s skipped: %s",
                         rule.get("id"), rule.get("symbol"), typ, err)
            continue

        if rule["status"] == "active":
            if met:
                events.append(_trigger(rule, observed, message, signal=new_signal))
        elif rule["status"] == "triggered" and not met:
            sa.rearm_alert(rule["id"])
    return events


# ── 定时扫描驱动（D1-c：框架 APScheduler job，默认 60s 一轮）──

def scheduled_scan() -> dict:
    """框架注册的告警扫描入口（register_jobs → orchestrator SchedulerEngine）。

    - 会话级 advisory lock（sa_alert_scan）保证 gunicorn 多 worker（各持独立调度器）
      下每轮仅一方真正执行，未抢到锁/DB 不可用即跳过本轮，幂等可重复；
    - 返回 {skipped, events}；D1-d SSE 广播可挂接本入口：拿到 events 后推给订阅者。
    """
    lock_conn = None
    held = False
    try:
        from plugins._base.db import get_pooled_connection
        lock_conn = get_pooled_connection()
        row = lock_conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(?)) AS ok", (_SCAN_LOCK_KEY,)).fetchone()
        held = bool(row and row["ok"])
        if not held:
            return {"skipped": True, "reason": "lock-busy", "events": 0}
        try:
            sa.ensure_tables()
        except Exception as err:
            _log.warning("alert scan ensure_tables failed: %s", err)
        events = scan_alerts()
        if events:
            _log.info("alert scan triggered %d event(s)", len(events))
            for ev in events:
                try:
                    sa.insert_sse_event("alerts", ev)
                except Exception as err:
                    _log.warning("sse alert event insert failed: %s", err)
                try:
                    # B4：告警触发钩子——通知/审计/第三方推送以钩子消费者接入，内核零改动
                    from plugin_manager.hooks import get_hook_registry
                    get_hook_registry().do_action("stock.alert.triggered", ev)
                except Exception as err:
                    _log.warning("hook stock.alert.triggered dispatch failed: %s", err)
        try:
            sa.prune_sse_events()
        except Exception as err:
            _log.warning("sse outbox prune failed: %s", err)
        return {"skipped": False, "events": len(events)}
    except Exception as err:
        _log.warning("alert scan round skipped: %s", err)
        return {"skipped": True, "reason": "db-unavailable", "events": 0}
    finally:
        # 无论是否持锁都必须归还池连接，否则每分钟泄漏一条（见 _base/db 池语义）
        if lock_conn is not None:
            if held:
                try:
                    lock_conn.execute("SELECT pg_advisory_unlock(hashtext(?))", (_SCAN_LOCK_KEY,))
                except Exception:
                    pass
            try:
                lock_conn.close()
            except Exception:
                pass
