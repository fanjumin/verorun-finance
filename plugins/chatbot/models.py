#!/usr/bin/env python3
"""
AI Advisor (Chatbot) Plugin — PostgreSQL schema: chatbot
=========================================================
- plugin_configs: 插件配置（替代主库 plugin_configs）
- agent_registry: 本地 Agent 注册（替代主库 agent_matrix 写入）
- chatbot_sessions: 对话统计/日志（替代主库 chatbot_sessions）
"""
import psycopg2
from plugins._base.db import get_pooled_connection


def get_chatbot_db():
    """每次从共享池借取连接并设置 chatbot schema（用完必须 close() 归还池）"""
    conn = None
    try:
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS chatbot")
        conn.execute("SET search_path TO chatbot")
        conn.execute("SELECT 1").fetchone()
        return conn
    except psycopg2.DatabaseError as e:
        if conn is not None:
            try:
                conn.close()  # 归还（坏连接由池丢弃）
            except Exception:
                pass
        print(f'[ChatbotPlugin] ⚠️ Database damaged, reconnect: {e}')
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS chatbot")
        conn.execute("SET search_path TO chatbot")
        return conn


def init_chatbot_tables():
    """创建所有 chatbot 插件表（幂等）"""
    with get_chatbot_db() as conn:
        # 0. Schema 版本追踪表（§10.6）
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_meta (
            key         TEXT PRIMARY KEY,
            value       TEXT DEFAULT '',
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )''')

        # 1. 插件配置表
        conn.execute('''CREATE TABLE IF NOT EXISTS plugin_configs (
            plugin_name TEXT NOT NULL,
            key         TEXT NOT NULL,
            value       TEXT DEFAULT '',
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (plugin_name, key)
        )''')

        # 2. Agent 注册表（本地，替代主库 agent_matrix 写入）
        conn.execute('''CREATE TABLE IF NOT EXISTS agent_registry (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name            TEXT NOT NULL,
            identifier      TEXT DEFAULT '',
            role_type       TEXT DEFAULT 'sub',
            description     TEXT DEFAULT '',
            domain          TEXT DEFAULT 'chatbot',
            provider        TEXT DEFAULT 'dashscope',
            model_name      TEXT DEFAULT 'qwen-turbo',
            system_prompt   TEXT DEFAULT '',
            capabilities    TEXT DEFAULT '[]',
            is_active       BIGINT DEFAULT 1,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )''')
        # 幂等添加 identifier 列（兼容旧表；PG 的 ADD COLUMN IF NOT EXISTS 不报错，
        # 不再因列已存在而 abort/rollback 导致同事务建表被撤销）
        conn.execute("ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS identifier TEXT DEFAULT ''")

        # 3. 对话会话日志表
        conn.execute('''CREATE TABLE IF NOT EXISTS chatbot_sessions (
            id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            session_id  TEXT NOT NULL,
            user_query  TEXT DEFAULT '',
            ai_reply    TEXT DEFAULT '',
            escalated   BIGINT DEFAULT 0,
            csat_score  BIGINT DEFAULT 0,
            source      TEXT DEFAULT 'chatbot',
            intent      TEXT DEFAULT '',
            sentiment   TEXT DEFAULT '',
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_cs_created ON chatbot_sessions(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_cs_session ON chatbot_sessions(session_id)')

        conn.commit()
        print('[ChatbotPlugin] PG schema chatbot is ready')


# ── Schema 版本迁移（§10.6） ──

def get_schema_version() -> str:
    """从 schema_meta 表读取当前 schema 版本（§10.6）"""
    try:
        with get_chatbot_db() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            return row['value'] if row else '0.0.0'
    except Exception:
        return '0.0.0'


def set_schema_version(version: str):
    """写入当前 schema 版本（§10.6）"""
    with get_chatbot_db() as conn:
        conn.execute('''
            INSERT INTO schema_meta (key, value, updated_at)
            VALUES ('schema_version', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        ''', (version,))
        conn.commit()


# ── 配置读写 ──

def get_config(plugin_name: str, key: str, default=''):
    """读取单条配置"""
    with get_chatbot_db() as conn:
        r = conn.execute(
            'SELECT value FROM plugin_configs WHERE plugin_name=%s AND key=%s',
            (plugin_name, key)
        ).fetchone()
        return r['value'] if r else default


def set_config(plugin_name: str, key: str, value: str):
    """保存单条配置"""
    with get_chatbot_db() as conn:
        conn.execute('''
            INSERT INTO plugin_configs (plugin_name, key, value, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT(plugin_name, key) DO UPDATE SET
                value=excluded.value,
                updated_at=NOW()
        ''', (plugin_name, key, str(value)))
        conn.commit()


def get_all_configs(plugin_name: str) -> dict:
    """读取某插件全部配置"""
    with get_chatbot_db() as conn:
        rows = conn.execute(
            'SELECT key, value FROM plugin_configs WHERE plugin_name=%s',
            (plugin_name,)
        ).fetchall()
        return {r['key']: r['value'] for r in rows}


def seed_defaults(plugin_name: str, defaults: dict):
    """仅当 DB 中无该配置行时写入默认值"""
    with get_chatbot_db() as conn:
        existing_keys = {
            r['key'] for r in conn.execute(
                'SELECT key FROM plugin_configs WHERE plugin_name=%s',
                (plugin_name,)
            ).fetchall()
        }
    for key, value in defaults.items():
        if key not in existing_keys:
            set_config(plugin_name, key, str(value))


# ── Agent 注册 ──

def upsert_agent(name: str, role_type: str, description: str, domain: str,
                 provider: str, model_name: str, system_prompt: str,
                 capabilities: str, is_active: int = 1, identifier: str = ''):
    """注册或更新 Agent"""
    with get_chatbot_db() as conn:
        try:
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
                    (name, identifier, role_type, description, domain, provider, model_name,
                     system_prompt, capabilities, is_active)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ''', (name, identifier, role_type, description, domain, provider, model_name,
                      system_prompt, capabilities, is_active))
            conn.commit()
        except Exception:
            # 注册失败不能留下 aborted 连接（曾导致 PG 连接池耗尽）
            conn.rollback()
            raise


def get_agent(agent_id: str):
    """按 name 或 identifier 查询 Agent"""
    with get_chatbot_db() as conn:
        row = conn.execute(
            'SELECT * FROM agent_registry WHERE (name=%s OR identifier=%s) AND is_active=1 LIMIT 1',
            (agent_id, agent_id)
        ).fetchone()
        return dict(row) if row else None


def unregister_agents():
    """清空本地 agent_registry 表（插件禁用/卸载时调用，实现"零残留"）"""
    with get_chatbot_db() as conn:
        cur = conn.execute('DELETE FROM agent_registry')
        conn.commit()
        return cur.rowcount


# ── 从主库迁移已有数据（幂等，首次运行自动执行） ──

def migrate_from_main():
    """从主库迁移 plugin_configs / agent_matrix / chatbot_sessions 到独立库（幂等）。

    每个分支独立 try/except：主库缺少某张表（如 plugin_configs）不会中断其余迁移。
    """
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(__file__), '..', '..', 'auth-center'))
        from models import get_db as get_main_db
    except Exception as e:
        print(f'[ChatbotPlugin] Main DB import failed, skip migration: {e}')
        return

    with get_main_db() as mc:
        # 迁移 plugin_configs（仅迁移独立库中不存在的键，不覆盖已有值）
        try:
            main_rows = mc.execute(
                "SELECT key, value FROM plugin_configs WHERE plugin_name='chatbot'"
            ).fetchall()
            migrated = 0
            if main_rows:
                with get_chatbot_db() as conn:
                    local_keys = {
                        r['key'] for r in conn.execute(
                            'SELECT key FROM plugin_configs WHERE plugin_name=%s', ('chatbot',)
                        ).fetchall()
                    }
                for r in main_rows:
                    if r['key'] not in local_keys:
                        set_config('chatbot', r['key'], r['value'])
                        migrated += 1
                if migrated:
                    print(f'[ChatbotPlugin] Migrated {migrated}/{len(main_rows)} plugin_configs (skipped existing)')
                else:
                    print(f'[ChatbotPlugin] Skipped migration, all {len(main_rows)} plugin_configs already exist')
        except Exception as e:
            print(f'[ChatbotPlugin] plugin_configs migration skipped: {e}')

        # 迁移 agent（仅迁移 chatbot 相关的 agent_matrix 记录）
        try:
            agent_rows = mc.execute(
                "SELECT * FROM agent_matrix WHERE domain='chatbot' OR managed_modules LIKE '%chatbot%'"
            ).fetchall()
            if agent_rows:
                for r in agent_rows:
                    upsert_agent(
                        name=r['name'], role_type=r.get('role_type', 'sub'),
                        description=r.get('description', ''), domain=r.get('domain', 'chatbot'),
                        provider=r.get('provider', 'dashscope'), model_name=r.get('model_name', 'qwen-turbo'),
                        system_prompt=r.get('system_prompt', ''),
                        capabilities=r.get('capabilities', '[]'),
                        is_active=r.get('is_active', 1)
                    )
                print(f'[ChatbotPlugin] Migrated {len(agent_rows)} agent_registry entries')
        except Exception as e:
            print(f'[ChatbotPlugin] agent migration skipped: {e}')

        # 迁移 chatbot_sessions（仅最近 30 天）
        try:
            session_rows = mc.execute(
                "SELECT * FROM chatbot_sessions WHERE created_at >= NOW() - INTERVAL '30 days'"
            ).fetchall()
            if session_rows:
                with get_chatbot_db() as conn:
                    for r in session_rows:
                        conn.execute(
                            '''INSERT INTO chatbot_sessions
                               (session_id, user_query, ai_reply, escalated, csat_score,
                                source, intent, sentiment, created_at)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT DO NOTHING''',
                            (r['session_id'], r.get('user_query', ''), r.get('ai_reply', ''),
                             r.get('escalated', 0), r.get('csat_score', 0),
                             r.get('source', 'chatbot'), r.get('intent', ''),
                             r.get('sentiment', ''), r.get('created_at', ''))
                        )
                    conn.commit()
                print(f'[ChatbotPlugin] Migrated {len(session_rows)} chatbot_sessions entries')
        except Exception as e:
            print(f'[ChatbotPlugin] chatbot_sessions migration skipped: {e}')