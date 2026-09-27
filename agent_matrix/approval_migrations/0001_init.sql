-- 系统级 Agent Tools 审批表（内核定域 v1.1）
--
-- 时间列一律 TIMESTAMPTZ：本库既有表大量使用 TEXT DEFAULT NOW()，
-- 直接比较会报「操作符不存在: text > timestamp with time zone」，新表不沿用该写法。
--
-- ⚠️ schema 名保留 "agent_tools"（历史命名），不迁移已有生产数据。
CREATE SCHEMA IF NOT EXISTS agent_tools;
SET search_path TO agent_tools, public;

CREATE TABLE IF NOT EXISTS agent_tools_approvals (
    id            bigserial PRIMARY KEY,
    task_id       varchar(64)  NOT NULL DEFAULT '',
    agent_id      varchar(64)  NOT NULL DEFAULT '',
    tool_name     varchar(128) NOT NULL,
    args_digest   text         NOT NULL DEFAULT '',   -- 仅存摘要，不入全量入参
    status        varchar(16)  NOT NULL DEFAULT 'pending',
    reason        text         NOT NULL DEFAULT '',
    requested_at  timestamptz  NOT NULL DEFAULT now(),
    decided_at    timestamptz,
    decided_by    varchar(64)  NOT NULL DEFAULT '',
    expires_at    timestamptz
);

CREATE INDEX IF NOT EXISTS idx_agt_approvals_pending
    ON agent_tools_approvals (status, requested_at DESC);
