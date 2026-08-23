SET search_path TO memory_engine, public;

CREATE TABLE IF NOT EXISTS evolution_rounds (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     varchar(64) NOT NULL,
    round_seq    integer     NOT NULL,
    status       varchar(16) NOT NULL DEFAULT 'open',   -- open / closed
    window_start timestamptz NOT NULL,
    window_end   timestamptz,
    mem_count    integer     NOT NULL DEFAULT 0,
    ref_count    integer     NOT NULL DEFAULT 0,
    prompt_from  varchar(16),                            -- prompt_version at round start
    prompt_to    varchar(16),                            -- prompt_version at round end
    meta         jsonb       NOT NULL DEFAULT '{}',      -- e.g. {"optimization_notes": [...]}
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_id, round_seq)
);

CREATE INDEX IF NOT EXISTS idx_rounds_agent_seq
    ON evolution_rounds (agent_id, round_seq DESC);
