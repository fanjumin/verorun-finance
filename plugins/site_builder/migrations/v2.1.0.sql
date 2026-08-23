-- v2.1.0: Site Builder plugin initial schema (independent database site_builder)
-- 独立库初始 schema：由插件安装时自动执行（幂等）。
BEGIN;

CREATE SCHEMA IF NOT EXISTS site_builder;

CREATE TABLE IF NOT EXISTS site_builder.site_builder_prompts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identifier      TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    icon            TEXT DEFAULT '📄',
    industry        TEXT DEFAULT '',
    tags_json       TEXT DEFAULT '[]',
    is_builtin      BIGINT DEFAULT 1,
    is_active       BIGINT DEFAULT 1,
    defaults_json   TEXT DEFAULT '{}',
    pages_json      TEXT DEFAULT '[]',
    documents_json  TEXT DEFAULT '[]',
    prompts_json    TEXT DEFAULT '{}',
    created_by      BIGINT DEFAULT 0,
    created_at      TEXT DEFAULT (NOW()),
    updated_at      TEXT DEFAULT (NOW())
);

CREATE INDEX IF NOT EXISTS idx_sbp_identifier ON site_builder.site_builder_prompts(identifier);
CREATE INDEX IF NOT EXISTS idx_sbp_industry ON site_builder.site_builder_prompts(industry);

CREATE TABLE IF NOT EXISTS site_builder.site_builder_tasks (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id         TEXT UNIQUE NOT NULL,
    user_id         BIGINT NOT NULL,
    site_config_id  BIGINT DEFAULT 1,
    prompt_id       BIGINT,
    user_input      TEXT DEFAULT '',
    status          TEXT DEFAULT 'pending',
    plan_json       TEXT DEFAULT '{}',
    result_json     TEXT DEFAULT '{}',
    current_step    TEXT DEFAULT '',
    error_message   TEXT DEFAULT '',
    created_at      TEXT DEFAULT (NOW()),
    updated_at      TEXT DEFAULT (NOW()),
    finished_at     TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sbt_user ON site_builder.site_builder_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_sbt_status ON site_builder.site_builder_tasks(status);

CREATE TABLE IF NOT EXISTS site_builder.design_tokens (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_key     TEXT NOT NULL DEFAULT 'platform',
    token_json   TEXT DEFAULT '{}',
    draft_json   TEXT DEFAULT '{}',
    generated_by TEXT DEFAULT 'manual',
    prompt_id    INTEGER DEFAULT NULL,
    version      INTEGER DEFAULT 1,
    created_at   TEXT DEFAULT NOW(),
    updated_at   TEXT DEFAULT NOW(),
    UNIQUE(site_key)
);

CREATE INDEX IF NOT EXISTS idx_dt_site_key ON site_builder.design_tokens(site_key);

CREATE TABLE IF NOT EXISTS site_builder.site_versions (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_key      TEXT NOT NULL DEFAULT 'platform',
    version_label TEXT NOT NULL,
    snapshot_json TEXT DEFAULT '{}',
    blocks_json   TEXT DEFAULT '[]',
    is_current    INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sv_site_key ON site_builder.site_versions(site_key);

COMMIT;
