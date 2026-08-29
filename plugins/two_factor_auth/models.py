"""2FA plugin — DB access layer.

遵循插件标准 §9.1 / §11.2：
- 自有数据写在独立 schema `two_factor_auth`，通过 get_pooled_connection() 借池 + SET search_path。
- 读/写主库 public（如 user_sessions 签发 SSO 会话）通过 models.get_db 显式 schema 限定。
"""
from plugins._base.db import get_pooled_connection
import os
import re
import psycopg2

SCHEMA_NAME = "two_factor_auth"


def get_two_factor_db():
    """借池连接并切换到插件独立 schema；调用方用 `with` 归还。

    schema 由迁移 0001 的 CREATE SCHEMA 负责创建，这里不再每次执行 DDL，
    仅切 search_path（连接归还时由连接池重置回 public）。
    """
    conn = get_pooled_connection()
    conn.execute(f"SET search_path TO {SCHEMA_NAME}, public")
    # B3 修复：SET LOCAL 仅作用于当前事务，commit/rollback 后自动复位，
    # 避免 session 级 SET timezone 泄漏到连接池内其他模块（NOW() 时间语义被污染）。
    conn.execute("SET LOCAL timezone TO 'UTC'")
    return conn


def get_raw_connection():
    """卸载清理用：一次性裸连接，用完即关（不归还池）。"""
    return psycopg2.connect(
        host=os.environ.get('PG_HOST', ''),
        port=int(os.environ.get('PG_PORT', 5432)),
        dbname=os.environ.get('PG_DB', 'appdb'),
        user=os.environ.get('PG_USER', 'app'),
        password=os.environ.get('PG_PASSWORD', ''),
        connect_timeout=10,
    )


def _split_statements(sql: str):
    """按分号切分 SQL，忽略单引号/双引号/美元引号（$$ / $tag$）内的分号。

    B2 修复需要 0002 使用 DO $$ ... $$ 块做条件 DDL，分句器必须支持美元引号，
    否则块内分号会被误切。
    """
    out, buf, quote = [], [], None
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if quote and quote[0] == '$':
            # 美元引号块内：直到匹配到结束标记
            if sql.startswith(quote, i):
                buf.append(quote)
                i += len(quote)
                quote = None
                continue
            buf.append(ch)
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == '$':
            # 识别 $$ 或 $tag$（tag 以字母/下划线开头）
            m = re.match(r'\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$', sql[i:])
            if m:
                quote = m.group(0)
                buf.append(quote)
                i += len(quote)
                continue
        if ch == ';':
            stmt = ''.join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = ''.join(buf).strip()
    if tail:
        out.append(tail)
    return out


def run_migration(conn, sql: str):
    """在已切换 search_path 的连接上，逐条执行迁移语句。

    psycopg2 的 execute 不支持多语句，故必须逐条执行。
    """
    for stmt in _split_statements(sql):
        # 跳过 SET search_path（连接已设置），避免重复解析
        if stmt.upper().startswith('SET SEARCH_PATH'):
            continue
        conn.execute(stmt)
    conn.commit()


# B1 修复：迁移 advisory lock 键（"2FA_MGR"），与 im_gateway scheduler 同款模式
MIGRATION_LOCK_KEY = 0x3246415F4D4752


def run_migrations_with_lock(plugin_dir, log=None):
    """串行化迁移：裸连接 + pg_try_advisory_xact_lock + schema_migrations 版本记录。

    修复 B1（多 gunicorn worker 并发迁移 → PG 并发 DDL 互持 AccessExclusiveLock 死锁）
    与 B2（0002 重复执行把既有 timestamptz 按服务器本地时区二次解释 → 数据偏移）：
    - pg_try_advisory_xact_lock：同一时刻仅一个 worker 执行迁移，其余直接跳过；
      锁为事务级，随 commit/rollback 自动释放。
    - schema_migrations 记录已应用文件名，run-once 幂等，已应用的不再重跑。
    """
    import glob
    conn = get_raw_connection()
    try:
        cur = conn.cursor()
        # 迁移执行环境：schema 与 UTC 时间语义
        cur.execute(f"SET search_path TO {SCHEMA_NAME}, public")
        cur.execute("SET LOCAL timezone TO 'UTC'")

        # 获取事务级 advisory lock（开始事务；未抢到则跳过，由其他 worker 迁移）
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        if not cur.fetchone()[0]:
            if log:
                log("2FA migration skipped: another worker is migrating")
            conn.rollback()
            return

        # 迁移版本记录表（幂等创建）
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}")
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.schema_migrations ("
            "  id SERIAL PRIMARY KEY,"
            "  filename TEXT NOT NULL UNIQUE,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ")")

        # 仅执行未应用过的迁移文件（0001 → 0002 …）
        for path in sorted(glob.glob(os.path.join(plugin_dir, 'migrations', '*.sql'))):
            filename = os.path.basename(path)
            cur.execute(
                f"SELECT 1 FROM {SCHEMA_NAME}.schema_migrations WHERE filename=%s",
                (filename,))
            if cur.fetchone():
                continue
            with open(path, 'r', encoding='utf-8') as f:
                sql = f.read()
            for stmt in _split_statements(sql):
                if stmt.upper().startswith('SET SEARCH_PATH'):
                    continue
                cur.execute(stmt)
            cur.execute(
                f"INSERT INTO {SCHEMA_NAME}.schema_migrations (filename) VALUES (%s)",
                (filename,))
            if log:
                log(f"2FA migration applied: {filename}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
