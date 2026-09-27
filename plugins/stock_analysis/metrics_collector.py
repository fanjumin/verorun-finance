#!/usr/bin/env python3
"""数据源调用指标采集（方案 §4.4）。

现状：/api/providers 只给静态元信息（是否配凭据、覆盖类别），**没有运行期指标**，
前端"数据源健康"卡的命中率/延迟一直是假数据。

设计：
  - 进程内滑窗计数，**线程安全**（gunicorn 多线程 + Flask 并发共享同一进程状态）。
  - 埋点位置只有一处：`providers/base_v2.py::BaseProviderV2.fetch()` 模板方法，
    所有 provider 自动被采集，无需逐个改。
  - 计数含 calls / ok / fail / 平均延迟 / P95（最近 200 个样本）/ 冷却剩余 / 最后成功时间。
  - **进程重启清零**（不落库）——这是刻意的：避免为展示指标引入写放大；
    产品若要跨重启的命中率，后续可落 PG 小表（见 snapshot() 的 note 字段说明）。

对外：record_call() / record_cooldown() / snapshot() / reset()

落盘吞吐（方案 §4.5）：
  - record_write() 由 models_sa 写路径埋点（连接代理，按 cursor.rowcount 计行数）；
  - sink_rate() 给近 60s 的 rows/s；从未落盘返回 None（不用 0 冒充"未采集"）。
  - 同样进程内计数、重启清零。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

# 每个 provider 保留的延迟样本数（用于 P95），限制内存占用
_SAMPLE_MAX = 200

# 触发"配额耗尽（令牌桶）"时按此秒数估算冷却；真实冷却由 provider 的限流策略决定，
# 这里只能给出保守估计，故 snapshot() 里用 cooldown_estimated 标注。
_COOLDOWN_ESTIMATE_SEC = 30

_lock = threading.RLock()

# provider -> {calls, ok, fail, lat_sum, samples, cool_until, last_ok, last_call, last_error}
_WIN: "defaultdict[str, dict]" = defaultdict(lambda: {
    "calls": 0,
    "ok": 0,
    "fail": 0,
    "lat_sum": 0.0,
    "samples": deque(maxlen=_SAMPLE_MAX),
    "cool_until": 0.0,
    "last_ok": 0.0,
    "last_call": 0.0,
    "last_error": "",
})

# 落盘吞吐（§4.5）：(ts, rows) 事件流 + 累计值。事件上限防内存膨胀。
_WRITE_MAX = 5000
_WRITE: dict = {
    "events": deque(maxlen=_WRITE_MAX),   # [(ts, rows), ...] 升序
    "total": 0,                            # 进程内累计写入行数
    "last": 0.0,                           # 最后一次写入时间（epoch）
}
_DEFAULT_WINDOW_SEC = 60.0


def _bucket(provider: str) -> dict:
    with _lock:
        return _WIN[provider]


def record_call(provider: str, ok: bool, latency_ms: float, error: str = "") -> None:
    """记录一次 provider 调用结果。

    ok 的定义：拿到非空数据（与 base_v2.fetch 的 `res.empty` 判定一致），
    即"空结果"算失败 —— 因为对投研来说空结果等于没命中，必须计入命中率。
    """
    provider = str(provider or "unknown")
    lat = max(0.0, float(latency_ms or 0.0))
    with _lock:
        w = _WIN[provider]
        w["calls"] += 1
        if ok:
            w["ok"] += 1
            w["last_ok"] = time.time()
        else:
            w["fail"] += 1
            if error:
                w["last_error"] = str(error)[:200]
        w["lat_sum"] += lat
        w["samples"].append(lat)
        w["last_call"] = time.time()


def record_cooldown(provider: str, seconds: float = _COOLDOWN_ESTIMATE_SEC) -> None:
    """记录一次冷却开始（令牌桶耗尽 / 上游限流）。"""
    provider = str(provider or "unknown")
    with _lock:
        _WIN[provider]["cool_until"] = max(
            _WIN[provider]["cool_until"], time.time() + max(0.0, float(seconds)),
        )


def _percentile(samples, pct: float):
    """最近样本的近似分位数（不引入 numpy，排序即可；样本 ≤200）。"""
    if not samples:
        return None
    ordered = sorted(samples)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    idx = (len(ordered) - 1) * pct
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 1)


def snapshot() -> list:
    """导出当前窗口快照，供 /api/gateway/health 使用。"""
    now = time.time()
    out = []
    with _lock:
        items = list(_WIN.items())
    for name, w in items:
        calls = w["calls"]
        samples = list(w["samples"])
        out.append({
            "name": name,
            "calls": calls,
            "ok": w["ok"],
            "fail": w["fail"],
            # 命中率：无调用时为 null（前端显示 --，不要用 0 冒充"0% 命中"）
            "hitRate": round(w["ok"] / calls, 4) if calls else None,
            "latencyMs": round(w["lat_sum"] / calls, 1) if calls else None,
            "latencyP95Ms": _percentile(samples, 0.95),
            "cooldown": max(0, int(w["cool_until"] - now)),
            "cooling": w["cool_until"] > now,
            "lastOk": w["last_ok"] or None,
            "lastCall": w["last_call"] or None,
            "lastError": w["last_error"] or None,
        })
    out.sort(key=lambda r: r["name"])
    return out


def record_write(rows: int = 1) -> None:
    """记录一次 PG 写入（行数按 cursor.rowcount）。

    埋点位置：`models_sa.get_db()` 返回的连接代理，凡 INSERT/UPDATE/DELETE 语句
    均被统计，只读语句不计。零副作用：调用方已包 try/except。
    """
    try:
        n = int(rows or 0)
    except Exception:
        n = 0
    if n <= 0:
        return
    now = time.time()
    with _lock:
        _WRITE["events"].append((now, n))
        _WRITE["total"] += n
        _WRITE["last"] = now


def sink_rate(window: float = _DEFAULT_WINDOW_SEC) -> "float | None":
    """近 window 秒的落盘吞吐（rows/s）。

    三种返回：
      - None：进程启动至今**从未**落盘（未采集，前端显示 --，不用 0 冒充）；
      - 0.0 ：有落盘，但近 window 秒内没有写入（真实为 0）；
      - >0  ：window 内写入行数 / 实际观测跨度（跨度至少 1s，避免单次写入被高估）。
    """
    win = max(1.0, float(window or _DEFAULT_WINDOW_SEC))
    now = time.time()
    with _lock:
        events = [(t, n) for (t, n) in _WRITE["events"] if t >= now - win]
        total = _WRITE["total"]
    if not events:
        return None if total == 0 else 0.0
    span = max(1.0, now - events[0][0])
    return round(sum(n for _, n in events) / span, 3)


def sink_meta(window: float = _DEFAULT_WINDOW_SEC) -> dict:
    """落盘吞吐的口径说明，让前端如实标注覆盖范围与局限。"""
    with _lock:
        total = _WRITE["total"]
        last = _WRITE["last"]
    return {
        "scope": "process",
        "persisted": False,
        "windowSec": window,
        "rowsTotal": total,
        "lastWrite": last or None,
        # 覆盖范围：models_sa 的全部写路径（连接代理统一埋点），不含 kline 缓存等
        # 走其他连接的写入；会话缓存落盘当前无此路径，故明确标注未采集。
        "coverage": "models_sa 写路径（INSERT/UPDATE/DELETE，按 cursor.rowcount 计）",
        "sessionCacheSink": False,
        "note": "进程内计数，重启清零；ON CONFLICT DO NOTHING 未实际写入时 rowcount=0，不计行数",
    }


def reset() -> None:
    """清空窗口（测试用；生产环境一般不调用）。"""
    with _lock:
        _WIN.clear()
        _WRITE["events"].clear()
        _WRITE["total"] = 0
        _WRITE["last"] = 0.0


def meta() -> dict:
    """采集层自身的口径说明，让前端能如实标注"进程内计数、重启清零"。"""
    return {
        "scope": "process",
        "persisted": False,
        "sampleMax": _SAMPLE_MAX,
        "cooldownEstimated": True,
        "cooldownEstimateSec": _COOLDOWN_ESTIMATE_SEC,
        "okDefinition": "非空结果视为命中；空结果与异常均计入失败",
    }
