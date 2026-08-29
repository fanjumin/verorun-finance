-- v1.5.0 — Sedimentation queue: high-value memories awaiting admin review
-- before being promoted into the shared knowledge_blocks table.
SET search_path TO memory_engine, public;

CREATE TABLE IF NOT EXISTS sedimentation_queue (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_schema varchar(32)      NOT NULL,          -- memory_engine | cogevolution_substrate
    memory_id     varchar(64)      NOT NULL,          -- source memory id (uuid::text)
    content_hash  varchar(64)      NOT NULL,          -- sha256(owner_id|content), idempotency
    title         varchar(200)     NOT NULL DEFAULT '',
    content       text             NOT NULL,
    keywords      text             NOT NULL DEFAULT '',
    category      varchar(32)      NOT NULL DEFAULT 'general',
    owner_id      varchar(64)      NOT NULL DEFAULT '',
    memory_type   varchar(20)      NOT NULL DEFAULT 'fact',
    confidence    real             NOT NULL DEFAULT 0.5,
    quality_score real             NOT NULL DEFAULT 0.5,
    status        varchar(16)      NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
    note          text,
    kb_id         varchar(64),                        -- knowledge_blocks.id after approval
    created_at    timestamptz      NOT NULL DEFAULT now(),
    reviewed_at   timestamptz,
    UNIQUE (source_schema, memory_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_sed_queue_status
    ON sedimentation_queue (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sed_queue_owner
    ON sedimentation_queue (owner_id);
