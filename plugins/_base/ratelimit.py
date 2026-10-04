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

# P1-2 修复：DDL 每进程只执行一次（否则每次 check_rate_limit 在热路径重复建表
# 触发 DDL 锁竞争 + 裸物理建连，批量/并发场景放大 PG 连接压力）。
_DDL_DONE = False
_DDL_LOCK = threading.Lock()
# ★ 2026-10-04 生产实证修复：表名必须**显式限定 public**。原先写成裸名
# `rate_limit_events`，而本组件借用的池化连接可能带着别的插件 SET 过的
# search_path（plugins/_base/db.py 的 _return() 是 rollback → SET public →
# putconn，那条 SET 处在未提交事务里、可被回滚，重置并不可靠），于是这张
# 平台共享表被分别建进了 4 个插件的 schema（实测：analytics / im_gateway /
# neural_flow / site_builder）。后果有二：① 宿主插件卸载时
# `DROP SCHEMA … CASCADE` 会连带删掉这张正在用的频控表；② 同一 rate_key 的
# 计数被按 schema/进程切碎，"跨 worker 全局生效"的前提失效，限额可被放大。
_TABLE = "public.rate_limit_events"
_DDL_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS public.rate_limit_events (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        rate_key TEXT NOT NULL,
        ts TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    "CREATE INDEX IF NOT EXISTS idx_rle_key_ts ON public.rate_limit_events(rate_key, ts)",
)


def _ensure_schema(conn) -> None:
    """进程内一次性建表/建索引；失败不置标记，下次调用重试。"""
    global _DDL_DONE
    if _DDL_DONE:
        return
    with _DDL_LOCK:
        if _DDL_DONE:
            return
        cur = conn.cursor()
        try:
            for stmt in _DDL_STATEMENTS:
                cur.execute(stmt)
            conn.commit()
            _DDL_DONE = True
        finally:
            cur.close()


# SAU-3：删除按 key 收窄后，不再活跃的键的行不会被回收 —— 低频全局兜底清理
_LAST_GC = 0.0
_GC_INTERVAL = 300  # 秒


def _maybe_gc(conn, window: int) -> None:
    """全局回收过期行（低频；失败不影响主流程）。"""
    global _LAST_GC
    now = time.time()
    if now - _LAST_GC < _GC_INTERVAL:
        return
    _LAST_GC = now
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM %s WHERE ts < NOW() - INTERVAL '1 second' * %%s" % _TABLE,
            (max(int(window), 3600),))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        cur.close()


def _is_missing_relation(err) -> bool:
    """是否为「表不存在」类错误（SQLSTATE 42P01）。"""
    if getattr(err, "pgcode", None) == "42P01":
        return True
    text = str(err)
    return "rate_limit_events" in text and "does not exist" in text


def _count_and_record(conn, key: str, limit: int, window: int) -> bool:
    """一次窗口计数：清过期 → 兜底 GC → 计数 → 记一次命中。返回是否放行。"""
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM %s"
            " WHERE rate_key = %%s AND ts < NOW() - INTERVAL '1 second' * %%s" % _TABLE,
            (key, int(window)))
        _maybe_gc(conn, window)
        cur.execute("SELECT COUNT(*) FROM %s WHERE rate_key=%%s" % _TABLE, (key,))
        if cur.fetchone()[0] >= limit:
            conn.commit()
            return False
        cur.execute("INSERT INTO %s (rate_key) VALUES (%%s)" % _TABLE, (key,))
        conn.commit()
        return True
    finally:
        cur.close()


def check_rate_limit(key: str, limit: int, window: int = 60) -> bool:
    """返回 True 表示允许放行；False 表示超限。"""
    from plugins._base.db import get_pooled_connection
    try:
        conn = get_pooled_connection()
        try:
            _ensure_schema(conn)
            try:
                return _count_and_record(conn, key, limit, window)
            except Exception as err:
                # 历史版本把这张表建到了别的 schema（或本机尚未建表）：
                # 清掉一次性标记，在 public 下补建一次并重试。仍失败才降级。
                if not _is_missing_relation(err):
                    raise
                global _DDL_DONE
                try:
                    conn.rollback()
                except Exception:
                    pass
                _DDL_DONE = False
                _ensure_schema(conn)
                return _count_and_record(conn, key, limit, window)
        finally:
            conn.close()   # ★ 原实现在异常路径上不归还连接（泄漏池槽），此处收口
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
