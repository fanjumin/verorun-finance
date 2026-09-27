#!/usr/bin/env python3
"""agent_matrix 审批系统 — DB 访问层（系统级内核定域）。

自有数据写在独立 schema `agent_tools`（历史命名保留），
通过 get_pooled_connection() 借池 + SET search_path。

⚠️ schema 名保留 "agent_tools"，不迁移表数据。
"""
import glob
import os
import re

import psycopg2

from plugins._base.db import get_pooled_connection

SCHEMA_NAME = "agent_tools"

# 迁移 advisory lock 键（"AGT_MGR" 的 ASCII 十六进制），全局唯一
MIGRATION_LOCK_KEY = 0x4147545F4D4752


def get_approval_db():
    """借池连接并切换到审批系统独立 schema；调用方负责 close()。

    schema 由迁移 0001 的 CREATE SCHEMA 负责创建，这里不再每次执行 DDL，
    仅切 search_path（连接归还时由连接池重置回 public）。
    """
    conn = get_pooled_connection()
    conn.execute(f"SET search_path TO {SCHEMA_NAME}, public")
    # SET LOCAL 仅作用于当前事务，commit/rollback 后自动复位，
    # 避免 session 级 SET timezone 泄漏到连接池内其他模块。
    conn.execute("SET LOCAL timezone TO 'UTC'")
    return conn


def get_raw_connection():
    """迁移/卸载清理用：一次性裸连接，用完即关（不归还池）。"""
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

    保留美元引号支持：后续迁移若需 DO $$ ... $$ 块做条件 DDL，
    块内分号不会被误切。
    """
    out, buf, quote = [], [], None
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if quote and quote[0] == '$':
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


def run_approval_migrations(log=None):
    """串行化审批系统迁移：裸连接 + pg_try_advisory_xact_lock + schema_migrations。

    - pg_try_advisory_xact_lock：同一时刻仅一个 gunicorn worker 执行迁移，其余直接跳过；
      锁为事务级，随 commit/rollback 自动释放。
    - schema_migrations 记录已应用文件名，run-once 幂等，已应用的不再重跑。
    """
    # 内核定域：迁移 SQL 与本文件同级目录下的 approval_migrations/
    _HERE = os.path.dirname(os.path.abspath(__file__))
    migrations_dir = os.path.join(_HERE, 'approval_migrations')

    conn = get_raw_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SET search_path TO {SCHEMA_NAME}, public")
        cur.execute("SET LOCAL timezone TO 'UTC'")

        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        if not cur.fetchone()[0]:
            if log:
                log("approval migration skipped: another worker is migrating")
            conn.rollback()
            return

        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}")
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.schema_migrations ("
            "  id SERIAL PRIMARY KEY,"
            "  filename TEXT NOT NULL UNIQUE,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ")")

        for path in sorted(glob.glob(os.path.join(migrations_dir, '*.sql'))):
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
                log(f"approval migration applied: {filename}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
