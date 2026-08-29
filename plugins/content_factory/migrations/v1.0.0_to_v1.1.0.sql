-- Content Factory Plugin — v1.0.0 → v1.1.0 迁移基线
-- 新增：Agent 注册表（§4.1）+ Schema 版本跟踪表（§10.6）
-- 全部为 CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS，幂等可重复执行。

SET search_path TO content_factory;

CREATE TABLE IF NOT EXISTS agent_registry (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            TEXT NOT NULL,
    identifier      TEXT DEFAULT '',
    role_type       TEXT DEFAULT 'sub',
    description     TEXT DEFAULT '',
    domain          TEXT DEFAULT 'content',
    provider        TEXT DEFAULT '',
    model_name      TEXT DEFAULT '',
    system_prompt   TEXT DEFAULT '',
    capabilities    TEXT DEFAULT '[]',
    is_active       BIGINT DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cf_agent_registry_identifier ON agent_registry(identifier);

CREATE TABLE IF NOT EXISTS schema_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT DEFAULT '',
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
