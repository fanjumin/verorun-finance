#!/usr/bin/env python3
"""
visitor_profile/models.py — Visitor Profile Engine 数据库层
=============================================================
PostgreSQL 独立 Schema `visitor_profile`（单库多 Schema，插件标准 §9.1）。

设计约束：
  - 一律使用 plugins._base.db.get_pooled_connection() 从共享池借取连接
    （用完必须 close() 归还池，杜绝缓存连接永久持有）
  - 每个连接先执行 SET search_path TO visitor_profile 切换 Schema
  - CRUD 全部幂等、异常安全（with 退出时自动提交/回滚并归还池）

表结构（migrations/v1.0.0_initial.sql）：
  - visitors          访客基础信息
  - event_log         行为事件日志
  - memories          画像记忆（pgvector 向量）
  - extraction_tasks  画像提取任务队列
  - agent_registry    本地 Agent 注册（profiler）
  - schema_meta       Schema 版本追踪（§10.6）
"""
import json
import logging

import psycopg2
from plugins._base.db import get_pooled_connection

logger = logging.getLogger('visitor_profile.models')

SCHEMA = 'visitor_profile'


def get_db():
    """每次从共享池借取连接并设置 visitor_profile schema（用完必须 close() 归还池）"""
    conn = None
    try:
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS %s" % SCHEMA)
        conn.execute("SET search_path TO %s" % SCHEMA)
        conn.execute("SELECT 1").fetchone()
        return conn
    except psycopg2.DatabaseError as e:
        if conn is not None:
            try:
                conn.close()  # 归还（坏连接由池丢弃）
            except Exception:
                pass
        print(f'[VisitorProfile] ⚠️ Database damaged, reconnect: {e}')
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS %s" % SCHEMA)
        conn.execute("SET search_path TO %s" % SCHEMA)
        return conn


# ── Schema 版本（§10.6） ──────────────────────────────────────────────

def get_schema_version() -> str:
    """读取当前 schema 版本"""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            return row['value'] if row else '0.0.0'
    except Exception:
        return '0.0.0'


def set_schema_version(version: str):
    """写入 schema 版本"""
    with get_db() as conn:
        conn.execute('''
            INSERT INTO schema_meta (key, value, updated_at)
            VALUES ('schema_version', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        ''', (version,))
        conn.commit()


# ── VisitorModel — 访客 ──────────────────────────────────────────────

class VisitorModel:
    """访客基础信息 CRUD"""

    @staticmethod
    def upsert_from_event(event_data: dict):
        """事件落库时 upsert 访客记录（首次建档 / 更新活跃度）。

        单条 UPSERT：首次插入时建档，再次出现时刷新 last_seen_at、
        累加 total_events，并用 COALESCE 保留已有环境信息。
        """
        visitor_id = event_data.get('visitor_id')
        if not visitor_id:
            return None
        with get_db() as conn:
            conn.execute('''
                INSERT INTO visitors
                    (visitor_id, last_seen_at, device_fingerprint, user_agent,
                     country, region, city, referrer,
                     utm_source, utm_medium, utm_campaign, user_id)
                VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (visitor_id) DO UPDATE SET
                    last_seen_at = NOW(),
                    total_events = visitors.total_events + 1,
                    device_fingerprint = COALESCE(EXCLUDED.device_fingerprint,
                                                  visitors.device_fingerprint),
                    user_agent = COALESCE(EXCLUDED.user_agent, visitors.user_agent),
                    country = COALESCE(EXCLUDED.country, visitors.country),
                    region = COALESCE(EXCLUDED.region, visitors.region),
                    city = COALESCE(EXCLUDED.city, visitors.city),
                    referrer = COALESCE(EXCLUDED.referrer, visitors.referrer),
                    utm_source = COALESCE(EXCLUDED.utm_source, visitors.utm_source),
                    utm_medium = COALESCE(EXCLUDED.utm_medium, visitors.utm_medium),
                    utm_campaign = COALESCE(EXCLUDED.utm_campaign, visitors.utm_campaign),
                    user_id = COALESCE(EXCLUDED.user_id, visitors.user_id)
            ''', (
                visitor_id,
                event_data.get('device_fingerprint'),
                event_data.get('user_agent'),
                event_data.get('country'),
                event_data.get('region'),
                event_data.get('city'),
                event_data.get('referrer'),
                event_data.get('utm_source'),
                event_data.get('utm_medium'),
                event_data.get('utm_campaign'),
                event_data.get('user_id'),
            ))
            conn.commit()
        return visitor_id

    @staticmethod
    def get_by_id(visitor_id: str):
        with get_db() as conn:
            row = conn.execute('''
                SELECT * FROM visitors WHERE visitor_id = %s
            ''', (visitor_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_by_user_id(user_id):
        with get_db() as conn:
            rows = conn.execute('''
                SELECT * FROM visitors WHERE user_id = %s
                ORDER BY last_seen_at DESC
            ''', (user_id,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def list_visitors(limit=50, offset=0, keyword=''):
        """分页查询访客列表，支持 visitor_id 模糊搜索。"""
        sql = '''
            SELECT visitor_id, user_id, first_seen_at, last_seen_at,
                   country, city, total_visits, total_events,
                   profile_summary, tags
            FROM visitors
        '''
        params = []
        if keyword:
            sql += ' WHERE visitor_id ILIKE %s OR tags::text ILIKE %s'
            like = '%%%s%%' % keyword
            params.extend([like, like])
        sql += ' ORDER BY last_seen_at DESC LIMIT %s OFFSET %s'
        params.extend([limit, offset])
        with get_db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def update_summary(visitor_id: str, summary: dict, tags: list):
        """更新 profile_summary 与 tags（profiler 提取后调用）。"""
        with get_db() as conn:
            conn.execute('''
                UPDATE visitors
                SET profile_summary = %s,
                    tags = %s,
                    updated_at = NOW()
                WHERE visitor_id = %s
            ''', (json.dumps(summary, ensure_ascii=False),
                  json.dumps(tags, ensure_ascii=False), visitor_id))
            conn.commit()

    @staticmethod
    def count():
        with get_db() as conn:
            row = conn.execute('SELECT COUNT(*) AS n FROM visitors').fetchone()
        return row['n'] or 0

    @staticmethod
    def count_events_24h():
        with get_db() as conn:
            row = conn.execute('''
                SELECT COUNT(*) AS n FROM event_log
                WHERE server_ts > NOW() - INTERVAL '24 hours'
            ''').fetchone()
        return row['n'] or 0


# ── EventLogModel — 行为事件 ─────────────────────────────────────────

class EventLogModel:
    """行为事件日志 CRUD"""

    @staticmethod
    def insert(event_data: dict):
        """写入一条原始事件，返回新 id。"""
        with get_db() as conn:
            cur = conn.execute('''
                INSERT INTO event_log
                    (visitor_id, event_type, page_url, page_title,
                     element_id, element_text, event_data,
                     session_id, client_ts)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (
                event_data.get('visitor_id'),
                event_data.get('event_type', 'custom'),
                event_data.get('page_url'),
                event_data.get('page_title'),
                event_data.get('element_id'),
                event_data.get('element_text'),
                json.dumps(event_data.get('event_data', {}), ensure_ascii=False),
                event_data.get('session_id'),
                event_data.get('timestamp'),
            ))
            row = cur.fetchone()
            conn.commit()
        return row['id'] if row else None

    @staticmethod
    def get_by_visitor(visitor_id: str, limit=50):
        with get_db() as conn:
            rows = conn.execute('''
                SELECT id, event_type, page_url, page_title, element_id,
                       element_text, event_data, session_id, client_ts, server_ts
                FROM event_log
                WHERE visitor_id = %s
                ORDER BY server_ts DESC
                LIMIT %s
            ''', (visitor_id, limit)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def count_unprocessed(visitor_id: str) -> int:
        with get_db() as conn:
            row = conn.execute('''
                SELECT COUNT(*) AS n FROM event_log
                WHERE visitor_id = %s AND NOT processed
            ''', (visitor_id,)).fetchone()
        return row['n'] or 0

    @staticmethod
    def get_unprocessed_event_ids(visitor_id: str, max_events=20) -> list:
        with get_db() as conn:
            rows = conn.execute('''
                SELECT id FROM event_log
                WHERE visitor_id = %s AND NOT processed
                ORDER BY server_ts
                LIMIT %s
            ''', (visitor_id, max_events)).fetchall()
        return [r['id'] for r in rows]

    @staticmethod
    def mark_processed(event_ids: list):
        if not event_ids:
            return
        with get_db() as conn:
            conn.execute('''
                UPDATE event_log
                SET processed = true, processed_at = NOW()
                WHERE id = ANY(%s)
            ''', (event_ids,))
            conn.commit()

    @staticmethod
    def get_events_by_ids(event_ids: list) -> list:
        if not event_ids:
            return []
        with get_db() as conn:
            rows = conn.execute('''
                SELECT event_type, page_url, page_title, element_text,
                       event_data, client_ts, server_ts
                FROM event_log
                WHERE id = ANY(%s)
                ORDER BY server_ts
            ''', (event_ids,)).fetchall()
        return [dict(r) for r in rows]


# ── MemoryModel — 画像记忆 ───────────────────────────────────────────

class MemoryModel:
    """画像记忆 CRUD（含 pgvector 语义检索）"""

    @staticmethod
    def _embedding_literal(vec) -> str:
        """将 list[float] 转成 PG vector 字面量 '[1.0,2.0,...]'。"""
        return '[' + ','.join(repr(float(v)) for v in vec) + ']'

    @staticmethod
    def insert(visitor_id: str, memory_type: str, content: dict,
               embedding=None, confidence=0.0, source_event_id=None,
               retention_days=365):
        """插入画像记忆；embedding 缺失时降级为纯文本存储。"""
        expired_at = "NOW() + INTERVAL '%s days'" % int(retention_days or 365)

        with get_db() as conn:
            if embedding is not None:
                cur = conn.execute('''
                    INSERT INTO memories
                        (visitor_id, memory_type, content, embedding,
                         confidence, source_event_id, expired_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (visitor_id, memory_type,
                      json.dumps(content, ensure_ascii=False),
                      MemoryModel._embedding_literal(embedding),
                      confidence, source_event_id))
            else:
                cur = conn.execute('''
                    INSERT INTO memories
                        (visitor_id, memory_type, content,
                         confidence, source_event_id, expired_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (visitor_id, memory_type,
                      json.dumps(content, ensure_ascii=False),
                      confidence, source_event_id))
            row = cur.fetchone()
            conn.commit()
        return row['id'] if row else None

    @staticmethod
    def get_active_by_visitor(visitor_id: str, limit=20):
        with get_db() as conn:
            rows = conn.execute('''
                SELECT id, memory_type, content, confidence, created_at
                FROM memories
                WHERE visitor_id = %s AND is_active = true
                ORDER BY created_at DESC
                LIMIT %s
            ''', (visitor_id, limit)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def semantic_search(query_embedding, visitor_id=None, top_k=5):
        """pgvector 余弦相似度检索（query_embedding 为 list[float]）。"""
        literal = MemoryModel._embedding_literal(query_embedding)
        with get_db() as conn:
            if visitor_id:
                rows = conn.execute('''
                    SELECT id, visitor_id, memory_type, content, confidence,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM memories
                    WHERE visitor_id = %s AND is_active = true
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                ''', (literal, visitor_id, literal, top_k)).fetchall()
            else:
                rows = conn.execute('''
                    SELECT id, visitor_id, memory_type, content, confidence,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM memories
                    WHERE is_active = true AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                ''', (literal, literal, top_k)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def deactivate_expired():
        """将过期的画像记忆标记为 is_active=false（日常维护任务调用）。"""
        with get_db() as conn:
            cur = conn.execute('''
                UPDATE memories SET is_active = false
                WHERE is_active = true AND expired_at IS NOT NULL
                  AND expired_at < NOW()
            ''')
            conn.commit()
        return cur.rowcount

    @staticmethod
    def count_created_24h():
        with get_db() as conn:
            row = conn.execute('''
                SELECT COUNT(*) AS n FROM memories
                WHERE created_at > NOW() - INTERVAL '24 hours'
            ''').fetchone()
        return row['n'] or 0


# ── ExtractionTaskModel — 提取任务 ───────────────────────────────────

class ExtractionTaskModel:
    """画像提取任务队列 CRUD"""

    @staticmethod
    def create(visitor_id: str, event_ids: list):
        with get_db() as conn:
            cur = conn.execute('''
                INSERT INTO extraction_tasks (visitor_id, event_ids)
                VALUES (%s, %s)
                RETURNING id
            ''', (visitor_id, json.dumps(event_ids)))
            row = cur.fetchone()
            conn.commit()
        return row['id'] if row else None

    @staticmethod
    def get_latest(visitor_id: str, event_ids: list):
        with get_db() as conn:
            row = conn.execute('''
                SELECT * FROM extraction_tasks
                WHERE visitor_id = %s AND event_ids = %s
                ORDER BY created_at DESC LIMIT 1
            ''', (visitor_id, json.dumps(event_ids))).fetchone()
        return dict(row) if row else None

    @staticmethod
    def mark_processing(task_id):
        with get_db() as conn:
            conn.execute('''
                UPDATE extraction_tasks
                SET status = 'processing', started_at = NOW()
                WHERE id = %s
            ''', (task_id,))
            conn.commit()

    @staticmethod
    def mark_completed(task_id, memory_ids: list, elapsed_ms: int):
        with get_db() as conn:
            conn.execute('''
                UPDATE extraction_tasks
                SET status = 'completed',
                    result_memory_ids = %s,
                    processing_time_ms = %s,
                    completed_at = NOW()
                WHERE id = %s
            ''', (json.dumps(memory_ids), elapsed_ms, task_id))
            conn.commit()

    @staticmethod
    def mark_failed(task_id, error_message: str):
        with get_db() as conn:
            conn.execute('''
                UPDATE extraction_tasks
                SET status = 'failed',
                    error_message = %s,
                    completed_at = NOW()
                WHERE id = %s
            ''', (str(error_message)[:500], task_id))
            conn.commit()

    @staticmethod
    def avg_processing_time_24h() -> int:
        with get_db() as conn:
            row = conn.execute('''
                SELECT COALESCE(AVG(processing_time_ms), 0)::INTEGER AS ms
                FROM extraction_tasks
                WHERE completed_at > NOW() - INTERVAL '24 hours'
            ''').fetchone()
        return row['ms'] or 0

    @staticmethod
    def stats() -> dict:
        """提取任务总体统计（dashboard 使用）。"""
        with get_db() as conn:
            rows = conn.execute('''
                SELECT status, COUNT(*) AS n FROM extraction_tasks
                GROUP BY status
            ''').fetchall()
        out = {'pending': 0, 'processing': 0, 'completed': 0, 'failed': 0}
        for r in rows:
            out[r['status']] = r['n'] or 0
        return out


# ── AgentRegistry — profiler Agent 注册（本地表，插件标准参考） ──────────

def upsert_agent(name: str, identifier: str, role_type: str, description: str,
                 domain: str, provider: str, model_name: str,
                 system_prompt: str, capabilities: str, is_active: int = 1):
    """注册或更新 profiler Agent（幂等）。

    with 退出时自动回滚/提交，异常不会在池中留下 aborted 连接。
    """
    with get_db() as conn:
        exists = conn.execute(
            'SELECT id FROM agent_registry WHERE name=%s AND role_type=%s',
            (name, role_type)
        ).fetchone()
        if exists:
            conn.execute('''
                UPDATE agent_registry
                SET description=%s, domain=%s, provider=%s, model_name=%s,
                    system_prompt=%s, capabilities=%s, is_active=%s,
                    identifier=%s, updated_at=NOW()
                WHERE id=%s
            ''', (description, domain, provider, model_name,
                  system_prompt, capabilities, is_active, identifier, exists['id']))
        else:
            conn.execute('''
                INSERT INTO agent_registry
                (name, identifier, role_type, description, domain, provider,
                 model_name, system_prompt, capabilities, is_active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ''', (name, identifier, role_type, description, domain, provider,
                  model_name, system_prompt, capabilities, is_active))
        conn.commit()


def get_agent(identifier: str):
    """按 identifier 查询 Agent 配置（profiler）。"""
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM agent_registry WHERE identifier=%s AND is_active=1 LIMIT 1',
            (identifier,)
        ).fetchone()
    return dict(row) if row else None


def unregister_agents():
    """清空本地 agent_registry（插件禁用/卸载时调用，零残留）。"""
    with get_db() as conn:
        cur = conn.execute('DELETE FROM agent_registry')
        conn.commit()
    return cur.rowcount
