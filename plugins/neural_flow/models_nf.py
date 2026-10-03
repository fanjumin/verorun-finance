"""models_nf.py — nf_flow_spans 归档表（跨域共表，方案 v1.3 §6.2）。

连接统一经 plugins._base.db.get_pooled_connection()（禁自建连接工厂，
与 stock_analysis/models_sa.py:13 同口径）。
Schema 隔离（标准 v1.8 §9.1/§11.2）：自有表落独立 schema `neural_flow`，
借池 → CREATE SCHEMA IF NOT EXISTS → SET search_path TO neural_flow, public
（public 兜底：采集器需读主库 agent_token_logs）。
"""
from __future__ import annotations

import json
from contextlib import contextmanager

from plugin_manager.logger import get_plugin_logger
from plugins._base.db import get_pooled_connection

_log = get_plugin_logger("neural_flow")

SCHEMA = "neural_flow"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nf_flow_spans (
    id         BIGSERIAL PRIMARY KEY,
    domain     VARCHAR(32)  NOT NULL DEFAULT 'platform',
    trace_id   VARCHAR(64)  NOT NULL,
    entity     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    payload    JSONB        NOT NULL,
    source     VARCHAR(32),
    source_id  BIGINT,
    created_at TIMESTAMPTZ  DEFAULT now()
)
""",
"""
CREATE INDEX IF NOT EXISTS idx_nfs_domain_created ON nf_flow_spans (domain, created_at)
""",
"""
CREATE INDEX IF NOT EXISTS idx_nfs_trace ON nf_flow_spans (trace_id)
""",
"""
ALTER TABLE nf_flow_spans ADD COLUMN IF NOT EXISTS source VARCHAR(32)
""",
"""
ALTER TABLE nf_flow_spans ADD COLUMN IF NOT EXISTS source_id BIGINT
""",
# 采集幂等：游标为进程内内存 + advisory lock 仅在轮次间互斥，锁在 worker 间轮转时
# 落后 worker 可能重放同一源行 → 用 (source, source_id) 唯一索引兜底；
# source_id 为 NULL 的自然埋点不参与唯一约束（PostgreSQL 中 NULL 不冲突）。
"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_nfs_source ON nf_flow_spans (source, source_id)
""",


@contextmanager
def get_nf_db():
    """借池 → 建 schema + 设 search_path → 退出归还（§11.2 连接生命周期）。

    - 借与归还成对：with 退出即 close 归还池，严禁模块级缓存连接；
    - 归还前 commit（或异常 rollback），池侧统一重置 search_path 保证跨插件干净；
    - search_path 含 public 兜底：采集器需读主库 agent_token_logs（只读）。
    """
    conn = get_pooled_connection()
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS %s" % SCHEMA)
        conn.execute("SET search_path TO %s, public" % SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def ensure_tables() -> None:
    with get_nf_db() as conn:
        for ddl in _SCHEMA:
            conn.execute(ddl)


def insert_span(payload: dict, source: str | None = None,
                source_id: int | None = None) -> int | None:
    """归档写入（nf_flow_spans）。

    返回自增 id；源行重复（source/source_id 命中唯一索引）返回 None；
    失败抛异常由调用方决定吞掉。
    """
    domain = payload.get("domain") or "platform"
    trace_id = str(payload.get("trace_id") or "unknown")
    entity_json = json.dumps(payload.get("entity") or {}, ensure_ascii=False)
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    with get_nf_db() as conn:
        if source_id is None:
            cur = conn.execute(
                "INSERT INTO nf_flow_spans (domain, trace_id, entity, payload) "
                "VALUES (%s, %s, %s::jsonb, %s::jsonb) RETURNING id",
                (domain, trace_id, entity_json, payload_json),
            )
        else:
            cur = conn.execute(
                "INSERT INTO nf_flow_spans "
                "(domain, trace_id, entity, payload, source, source_id) "
                "VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s) "
                "ON CONFLICT (source, source_id) DO NOTHING RETURNING id",
                (domain, trace_id, entity_json, payload_json, source, source_id),
            )
        row = cur.fetchone()
        return row["id"] if row else None


def list_spans(from_ts=None, to_ts=None, domain=None, trace_id=None,
               limit: int = 500, order: str = "desc"):
    """回放查询：排序/过滤一律用 created_at/id（双时基纪律，payload.ts 仅显示）。

    默认 id 倒序（最新优先）——升序 + LIMIT 会把窗口固定在最旧的一段，
    行数超限时最新 span 永远查不到；需要时序回放时显式传 order='asc'。
    from_ts/to_ts 为 epoch 秒。
    """
    sql = "SELECT id, domain, trace_id, entity, payload, created_at FROM nf_flow_spans WHERE 1=1"
    args: list = []
    if from_ts is not None:
        sql += " AND created_at >= to_timestamp(%s)"
        args.append(float(from_ts))
    if to_ts is not None:
        sql += " AND created_at < to_timestamp(%s)"
        args.append(float(to_ts))
    if domain:
        sql += " AND domain = %s"
        args.append(domain)
    if trace_id:
        sql += " AND trace_id = %s"
        args.append(trace_id)
    order_dir = "ASC" if str(order).lower() == "asc" else "DESC"
    sql += " ORDER BY id " + order_dir + " LIMIT %s"
    args.append(max(1, min(int(limit), 2000)))
    with get_nf_db() as conn:
        cur = conn.execute(sql, args)
        rows = cur.fetchall()
    out = []
    for r in rows:
        item = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        item["_id"] = r["id"]
        item["_created_at"] = r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"])
        out.append(item)
    return out


def drop_schema() -> None:
    """卸载清理：删除本插件自有 schema 及其全部对象（零残留，标准 §12.5）。

    仅作用于 SCHEMA 常量所指 schema，不触碰 public 与其他插件 schema；
    走裸池连接（卸载语义下不应再先 CREATE SCHEMA），commit 后归还池。
    """
    conn = get_pooled_connection()
    try:
        conn.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def prune_spans(retention_days: int = 30) -> int:
    with get_nf_db() as conn:
        cur = conn.execute(
            "DELETE FROM nf_flow_spans WHERE created_at < now() - make_interval(days => %s)",
            (int(retention_days),),
        )
        return cur.rowcount if cur.rowcount is not None else 0
