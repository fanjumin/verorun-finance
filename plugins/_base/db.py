#!/usr/bin/env python3
"""Plugin shared DB connection utilities.

为各插件提供统一的 psycopg2 连接包装类，替代各插件重复定义的内联包装类。
"""
import psycopg2
import os
import re
import threading
from psycopg2 import pool
from psycopg2.extras import RealDictCursor


# 匹配单引号字符串字面量（含 '' 转义）或单个 ? 占位符。
# 仅替换字面量之外的 ?，避免 SQL 字符串内的 ?（如 LIKE 模式）被误替换。
_PLACEHOLDER_RE = re.compile(r"'(''|[^'])*'|\?")


def _replace_placeholders(sql: str) -> str:
    """将 SQL 中的 ? 占位符替换为 %s，跳过单引号字符串字面量内的 ?。"""
    def _repl(m):
        return '%s' if m.group(0) == '?' else m.group(0)
    return _PLACEHOLDER_RE.sub(_repl, sql)


class PgConnection:
    """psycopg2 connection adapter with sqlite3-compatible interface.

    提供 `.execute()` / `.commit()` / `.rollback()` / `.close()` 方法和
    context manager 支持，兼容从 SQLite 迁移到 PG 的插件代码。
    """
    def __init__(self, conn):
        self._conn = conn
        self._cur = None

    def cursor(self):
        return self._conn.cursor()

    def execute(self, sql, params=None):
        if self._cur is None:
            self._cur = self._conn.cursor(cursor_factory=RealDictCursor)
        self._cur.execute(_replace_placeholders(sql), params or ())
        return self._cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._cur:
            self._cur.close()
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self.close()


def get_raw_connection():
    """Return a raw psycopg2 connection using env-configured PG credentials.

    All plugins and modules should use this single factory instead of
    inlining psycopg2.connect() calls with repeated env var lookups.
    """
    return psycopg2.connect(
        host=os.environ.get('PG_HOST', ''),
        port=int(os.environ.get('PG_PORT', 5432)),
        dbname=os.environ.get('PG_DB', 'appdb'),
        user=os.environ.get('PG_USER', 'app'),
        password=os.environ.get('PG_PASSWORD', ''),
        connect_timeout=10,  # 建连最多等 10 秒，避免低配机器上无限挂死
    )


# ── 共享连接池（ThreadedConnectionPool，线程安全） ──────────────────────────
# 根治插件"缓存连接永久持有"导致的 PG 连接数打满问题。
# 机制：每进程一个懒创建池；get_pooled_connection() 从池借连接，调用方用完
#       close() 归还池。连接总数被 maxconn 锁死，杜绝单调累积。
# 懒创建：每个 gunicorn worker fork 后首次调用才建池 → 进程间独立，避免共享 fd。

_POOL_MIN = int(os.environ.get('PLUGIN_POOL_MIN', 1))
_POOL_MAX = int(os.environ.get('PLUGIN_POOL_MAX', 10))
_pool = None
_pool_lock = threading.Lock()


def _pool_config():
    return dict(
        host=os.environ.get('PG_HOST', ''),
        port=int(os.environ.get('PG_PORT', 5432)),
        dbname=os.environ.get('PG_DB', 'appdb'),
        user=os.environ.get('PG_USER', 'app'),
        password=os.environ.get('PG_PASSWORD', ''),
        connect_timeout=10,
    )


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                try:
                    _pool = pool.ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, **_pool_config())
                except Exception:
                    _pool = None  # DB 未就绪 → 后续回退直连，不阻塞
    return _pool


class PooledPgConnection(PgConnection):
    """池化连接：close() 归还池而非真正关闭底层连接。"""
    def __init__(self, raw, pool):
        super().__init__(raw)
        self._pool = pool
        self._returned = False

    def _return(self):
        if self._returned:
            return
        self._returned = True
        if self._cur is not None:
            try:
                self._cur.close()
            except Exception:
                pass
            self._cur = None
        # 归还前回滚未提交事务，并重置 search_path，保证池内连接干净
        # （防止 payment 等插件设置过 search_path 后，其他插件借到同一连接查错 schema）
        try:
            self._conn.rollback()
        except Exception:
            pass
        try:
            _cur = self._conn.cursor()
            _cur.execute("SET search_path TO public")
            _cur.close()
        except Exception:
            pass
        try:
            self._pool.putconn(self._conn)
        except Exception:
            try:
                self._conn.close()  # 归还失败（连接损坏）→ 丢弃
            except Exception:
                pass

    def close(self):
        self._return()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            try:
                self._conn.rollback()
            except Exception:
                pass
        else:
            try:
                self._conn.commit()
            except Exception:
                pass
        self._return()


def get_pooled_connection():
    """从共享池借一条连接；调用方用完必须 close()（归还池）。

    池满/池不可用时回退直连（用完即关，不累积）。
    """
    p = _get_pool()
    if p is not None:
        try:
            return PooledPgConnection(p.getconn(), p)
        except (pool.PoolError, Exception):
            pass  # 池满或异常 → 回退直连
    return PgConnection(psycopg2.connect(**_pool_config()))
