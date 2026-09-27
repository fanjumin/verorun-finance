-- v1.8.0 — A/B 注入收益对照 + 检索回归评测（P2）。
SET search_path TO memory_engine, public;

-- ① A/B 结局事件：每个已完成任务一行（含对照臂未注入任务）。
--    分流键 = user_id 确定性哈希（K2 约束：注入点无 session_id）。
CREATE TABLE IF NOT EXISTS ab_events (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id       varchar(64) NOT NULL DEFAULT '',
    user_id       varchar(64) NOT NULL,
    agent_id      varchar(64) NOT NULL DEFAULT '',
    arm           varchar(16) NOT NULL,              -- treatment / control（16 容纳 'treatment'）
    injected      boolean     NOT NULL DEFAULT false,
    block_len     integer     NOT NULL DEFAULT 0,    -- 实际注入的字符数
    failed        boolean     NOT NULL DEFAULT false,
    confidence    real        NOT NULL DEFAULT 0,
    retries       integer     NOT NULL DEFAULT 0,
    total_tokens  bigint      NOT NULL DEFAULT 0,    -- agent_token_logs 按 task_id 回查
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_ab_events_user   ON ab_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ab_events_window ON ab_events (created_at);

-- ② 检索回归评测集：给定 query + 期望命中的记忆（零 LLM 成本）。
CREATE TABLE IF NOT EXISTS eval_cases (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          varchar(128) NOT NULL,
    owner_id      varchar(64)  NOT NULL,             -- 检索作用域（记忆属主）
    agent_id      varchar(64)  NOT NULL DEFAULT '',
    query         text         NOT NULL,
    expected_memory_id uuid,                         -- 期望 top-k 命中的记忆行
    expected_keywords text[]  NOT NULL DEFAULT '{}', -- 兜底判据（命中内容含任一关键词）
    active        boolean      NOT NULL DEFAULT true,
    created_at    timestamptz  NOT NULL DEFAULT now()
);

-- ③ 评测运行记录：每次跑 harness 留档，供变更前后对比。
CREATE TABLE IF NOT EXISTS eval_runs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_count    integer     NOT NULL,
    hit_at_k      real        NOT NULL DEFAULT 0,
    mrr           real        NOT NULL DEFAULT 0,
    metrics       jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- 分解指标 + 失败 case 明细
    created_at    timestamptz NOT NULL DEFAULT now()
);
