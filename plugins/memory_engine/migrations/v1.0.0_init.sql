CREATE SCHEMA IF NOT EXISTS memory_engine;

-- pgvector 为可选平台能力（方案 A / CE-D1 修复）：
-- 迁移器探测 pg_available_extensions；可用时建 vector(1536) 列，不可用时建 TEXT 列降级为关键词检索。
-- 生产环境如需向量检索：apt install postgresql-14-pgvector 后重跑迁移即可自动升级。
SET search_path TO memory_engine, public;

-- Plugin degrades gracefully to keyword search when the column is not usable.
CREATE TABLE IF NOT EXISTS memories (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_type    varchar(20)  NOT NULL DEFAULT 'user',
    owner_id      varchar(64)  NOT NULL DEFAULT '',
    agent_id      varchar(64)  NOT NULL DEFAULT '',
    memory_type   varchar(20)  NOT NULL DEFAULT 'fact',
    content       text         NOT NULL,
    keywords      text[]       NOT NULL DEFAULT '{}',
    confidence    real         NOT NULL DEFAULT 0.5,
    hit_count     integer      NOT NULL DEFAULT 0,
    last_hit_at   timestamptz,
    quality_score real         NOT NULL DEFAULT 0.5,
    importance    real         NOT NULL DEFAULT 0.5,
    source        varchar(20)  NOT NULL DEFAULT 'auto',
    content_hash  varchar(64)  UNIQUE,
    status        varchar(16)  NOT NULL DEFAULT 'active',
    meta          jsonb        NOT NULL DEFAULT '{}',
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memories_owner
    ON memories (owner_type, owner_id, status);
CREATE INDEX IF NOT EXISTS idx_memories_type
    ON memories (memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_keywords
    ON memories USING gin (keywords);

CREATE TABLE IF NOT EXISTS reflexion_logs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id    varchar(64) NOT NULL,
    task_id     varchar(64) NOT NULL DEFAULT '',
    trigger     varchar(32) NOT NULL DEFAULT 'task_completed',
    success     boolean     NOT NULL DEFAULT true,
    user_query  text,
    issue       text,
    lesson      text,
    action      text,
    rating      smallint,
    tokens_used integer     NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reflexion_agent_time
    ON reflexion_logs (agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS prompt_metrics (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      varchar(64) NOT NULL,
    prompt_hash   varchar(64) NOT NULL,
    prompt_version varchar(16) NOT NULL DEFAULT '1.0.0',
    sample_count  integer     NOT NULL DEFAULT 0,
    success_count integer     NOT NULL DEFAULT 0,
    success_rate  real        NOT NULL DEFAULT 0,
    avg_rating    real        NOT NULL DEFAULT 0,
    total_tokens  integer     NOT NULL DEFAULT 0,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_id, prompt_hash)
);

CREATE INDEX IF NOT EXISTS idx_prompt_metrics_agent
    ON prompt_metrics (agent_id, updated_at DESC);
