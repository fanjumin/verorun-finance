-- chatbot 插件 — Schema 基线迁移 v0.0.0 → v1.2.0
-- =====================================================
-- 目标: 精确记录 v1.2.0 完整基线 schema，与 models.py init_chatbot_tables() 逐表对齐。
-- 说明: 运行时迁移以 models.init_chatbot_tables()（Python，幂等）为准，
--       本文件为版本化 SQL 文档，供审计与手动比对。
-- 执行前提: SET search_path TO chatbot;

-- 1. Schema 版本跟踪表（models.py: schema_meta）
CREATE TABLE IF NOT EXISTS schema_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT DEFAULT '',
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 2. 插件配置表（models.py: plugin_configs）
CREATE TABLE IF NOT EXISTS plugin_configs (
    plugin_name TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT DEFAULT '',
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (plugin_name, key)
);

-- 3. Agent 注册表（models.py: agent_registry，本地注册，替代主库 agent_matrix 写入）
CREATE TABLE IF NOT EXISTS agent_registry (
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
);
CREATE INDEX IF NOT EXISTS idx_agent_registry_identifier ON agent_registry(identifier);

-- 4. 对话会话日志表（models.py: chatbot_sessions）
CREATE TABLE IF NOT EXISTS chatbot_sessions (
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
);
CREATE INDEX IF NOT EXISTS idx_cs_created ON chatbot_sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_cs_session ON chatbot_sessions(session_id);
