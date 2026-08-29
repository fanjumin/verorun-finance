"""plugins/_base/ratelimit.py — 跨 worker 频控公共组件（Q4 批准项）

PG 窗口计数（public.rate_limit_events），多 gunicorn worker 全局生效；
DB 异常时降级进程内计数（fail-open 保可用）。

用法：
    from plugins._base.ratelimit import check_rate_limit
    if not check_rate_limit("stock_analysis:" + user_key, limit=30, window=60):
        return jsonify({"error": "Too Many Requests"}), 429

实现说明：
- 表/索引幂等创建，首次调用自动建表
- 窗口滑动删除 + 计数 + 插入在同一事务语义内（逐语句 commit，避免长事务）
- 占位符注意：INTERVAL '%s seconds' 会被 psycopg2 转义成 ''60'' 导致语法错误，
  必须使用 INTERVAL '1 second' * %s（2026-08-25 实测踩坑）
"""
import threading
import time

_PROCESS_LIMITS: dict = {}
_PROCESS_LOCK = threading.Lock()


def check_rate_limit(key: str, limit: int, window: int = 60) -> bool:
    """返回 True 表示允许放行；False 表示超限。"""
    from plugins._base.db import get_raw_connection
    try:
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS rate_limit_events (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            rate_key TEXT NOT NULL,
            ts TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rle_key_ts ON rate_limit_events(rate_key, ts)")
        conn.commit()
        cur.execute(
            "DELETE FROM rate_limit_events WHERE ts < NOW() - INTERVAL '1 second' * %s",
            (int(window),))
        cur.execute("SELECT COUNT(*) FROM rate_limit_events WHERE rate_key=%s", (key,))
        if cur.fetchone()[0] >= limit:
            conn.commit()
            cur.close()
            conn.close()
            return False
        cur.execute("INSERT INTO rate_limit_events (rate_key) VALUES (%s)", (key,))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception:
        # DB 异常降级：进程内滑动窗口（单 worker 内仍限流）
        now = time.time()
        with _PROCESS_LOCK:
            window_start, hits = _PROCESS_LIMITS.get(key, (now, 0))
            if now - window_start > window:
                window_start, hits = now, 0
            hits += 1
            _PROCESS_LIMITS[key] = (window_start, hits)
        return hits <= limit
