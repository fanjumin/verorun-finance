-- migrations/v1.0.0_initial.sql
-- Visitor Profile Engine — 初始化 visitor_profile Schema
-- 幂等：可安全重复执行（CREATE IF NOT EXISTS）

-- 创建独立 Schema 并启用 pgvector 扩展
CREATE SCHEMA IF NOT EXISTS visitor_profile;
-- 显式 SCHEMA public：迁移执行器已预设 search_path 为插件 schema，若不显式指定，
-- 扩展会落入插件 schema（其他插件 search_path 不含它 → vector 类型不可见）。
CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;

-- ============================================
-- 0. Schema 版本追踪表（插件标准 §10.6）
-- ============================================
CREATE TABLE IF NOT EXISTS visitor_profile.schema_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT DEFAULT '',
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- 1. 访客基础信息表
-- ============================================
CREATE TABLE IF NOT EXISTS visitor_profile.visitors (
    id              SERIAL PRIMARY KEY,
    visitor_id      VARCHAR(128) NOT NULL UNIQUE,
    -- 身份关联
    user_id         INTEGER REFERENCES public.users(id) ON DELETE SET NULL,
    -- 设备与环境
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    device_fingerprint VARCHAR(128),
    user_agent      TEXT,
    -- 地理信息
    country         VARCHAR(64),
    region          VARCHAR(128),
    city            VARCHAR(128),
    -- 来源
    referrer        TEXT,
    utm_source      VARCHAR(256),
    utm_medium      VARCHAR(256),
    utm_campaign    VARCHAR(256),
    -- 聚合指标
    total_visits    INTEGER NOT NULL DEFAULT 1,
    total_events    INTEGER NOT NULL DEFAULT 0,
    avg_session_sec INTEGER DEFAULT 0,
    -- 画像摘要（JSON，由 profiler Agent 周期性更新）
    profile_summary JSONB DEFAULT '{}',
    -- 标签
    tags            JSONB DEFAULT '[]',
    -- 元数据
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_visitors_user_id ON visitor_profile.visitors(user_id);
CREATE INDEX IF NOT EXISTS idx_visitors_last_seen ON visitor_profile.visitors(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_visitors_tags ON visitor_profile.visitors USING gin(tags);

-- ============================================
-- 2. 行为事件日志表
-- ============================================
CREATE TABLE IF NOT EXISTS visitor_profile.event_log (
    id              BIGSERIAL PRIMARY KEY,
    visitor_id      VARCHAR(128) NOT NULL,
    event_type      VARCHAR(64) NOT NULL,
    -- event_type: page_view, click, form_submit, chat_message,
    --             search, download, purchase, custom
    -- 事件上下文
    page_url        TEXT,
    page_title      VARCHAR(512),
    element_id      VARCHAR(256),
    element_text    TEXT,
    -- 自定义数据
    event_data      JSONB DEFAULT '{}',
    -- 会话
    session_id      VARCHAR(128),
    -- 时间
    client_ts       TIMESTAMPTZ,
    server_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 处理状态
    processed       BOOLEAN NOT NULL DEFAULT false,
    processed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_event_visitor ON visitor_profile.event_log(visitor_id, server_ts DESC);
CREATE INDEX IF NOT EXISTS idx_event_type ON visitor_profile.event_log(event_type);
CREATE INDEX IF NOT EXISTS idx_event_unprocessed ON visitor_profile.event_log(processed, server_ts)
    WHERE NOT processed;

-- ============================================
-- 3. 画像记忆表（核心：向量化 + 语义检索）
-- ============================================
CREATE TABLE IF NOT EXISTS visitor_profile.memories (
    id              BIGSERIAL PRIMARY KEY,
    visitor_id      VARCHAR(128) NOT NULL,
    -- 画像分类
    memory_type     VARCHAR(64) NOT NULL,
    -- memory_type: behavior_profile, intent_tag, sentiment,
    --              interest_cluster, purchase_intent, tech_skill
    -- 结构化画像内容
    content         JSONB NOT NULL,
    -- content 示例：
    -- {"intent":"评估私有化部署","tech_tags":["LLM","Docker"],
    --  "sentiment":"positive","confidence":0.92,
    --  "summary":"访客反复浏览私有化部署方案页并下载技术白皮书"}
    -- 向量（pgvector）
    embedding       vector(1536),
    -- 元数据
    source_event_id BIGINT REFERENCES visitor_profile.event_log(id),
    confidence      FLOAT DEFAULT 0.0,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    expired_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memories_visitor ON visitor_profile.memories(visitor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_type ON visitor_profile.memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_active ON visitor_profile.memories(visitor_id, is_active)
    WHERE is_active;
-- pgvector IVFFlat 索引（用于语义检索）
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON visitor_profile.memories
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================
-- 4. 画像提取任务队列（异步处理）
-- ============================================
CREATE TABLE IF NOT EXISTS visitor_profile.extraction_tasks (
    id              BIGSERIAL PRIMARY KEY,
    visitor_id      VARCHAR(128) NOT NULL,
    event_ids       JSONB NOT NULL,
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',
    -- status: pending, processing, completed, failed
    result_memory_ids JSONB,
    error_message   TEXT,
    processing_time_ms INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON visitor_profile.extraction_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_visitor ON visitor_profile.extraction_tasks(visitor_id);

-- ============================================
-- 5. Agent 注册表（本地，替代主库 agent_matrix 写入）
-- ============================================
CREATE TABLE IF NOT EXISTS visitor_profile.agent_registry (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            TEXT NOT NULL,
    identifier      TEXT DEFAULT '',
    role_type       TEXT DEFAULT 'sub',
    description     TEXT DEFAULT '',
    domain          TEXT DEFAULT 'visitor_profiling',
    provider        TEXT DEFAULT 'dashscope',
    model_name      TEXT DEFAULT 'qwen-turbo',
    system_prompt   TEXT DEFAULT '',
    capabilities    TEXT DEFAULT '[]',
    is_active       BIGINT DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
