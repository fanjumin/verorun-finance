-- VeroRun neural_flow v1.0.0 初始迁移（幂等）
-- ---------------------------------------------------------------------------
-- 约定（plugin-standard-v1.8 §9.1 / §11.2 / §10.6）：
--   * 单库多 Schema：本文件只建**本插件自有** schema `neural_flow`，禁止独立数据库；
--   * 全部 DDL 幂等（IF NOT EXISTS），可安全重复执行；
--   * 本文件为等价固化档：运行期由 models_nf.ensure_tables() 执行同一组 DDL
--     （_SCHEMA 元组与下方语句一一对应），本文件供审计/人工比对与手工补建；
--   * 索引名不带 schema 前缀（PostgreSQL 语法约束），归属由表的 schema 决定。
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS neural_flow;

-- ── nf_flow_spans：跨域 span 归档（30 天保留，回放/钻取）──────────────────────
CREATE TABLE IF NOT EXISTS neural_flow.nf_flow_spans (
    id         BIGSERIAL PRIMARY KEY,
    domain     VARCHAR(32)  NOT NULL DEFAULT 'platform',
    trace_id   VARCHAR(64)  NOT NULL,
    entity     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    payload    JSONB        NOT NULL,
    source     VARCHAR(32),
    source_id  BIGINT,
    created_at TIMESTAMPTZ  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_nfs_domain_created
    ON neural_flow.nf_flow_spans (domain, created_at);

CREATE INDEX IF NOT EXISTS idx_nfs_trace
    ON neural_flow.nf_flow_spans (trace_id);

-- 旧库补列（新库已在 CREATE TABLE 内声明，此处幂等收敛）
ALTER TABLE neural_flow.nf_flow_spans ADD COLUMN IF NOT EXISTS source VARCHAR(32);
ALTER TABLE neural_flow.nf_flow_spans ADD COLUMN IF NOT EXISTS source_id BIGINT;

-- 采集幂等：游标为进程内内存 + advisory lock 仅在轮次间互斥，锁在 worker 间
-- 轮转时落后 worker 可能重放同一源行 → (source, source_id) 唯一索引兜底；
-- source_id 为 NULL 的自然埋点不参与唯一约束（PostgreSQL 中 NULL 不冲突）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_nfs_source
    ON neural_flow.nf_flow_spans (source, source_id);
