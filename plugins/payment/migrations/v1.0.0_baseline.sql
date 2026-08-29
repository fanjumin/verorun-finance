-- Payment Plugin — v1.0.0 基线 Schema（PostgreSQL）
-- 与 plugins/payment/models.py 中 init_payment_tables() 的定义保持一致。
-- 后续版本迁移按 §10.6 约定命名为 v<from>_to_v<to>.sql，逐条事务执行。

CREATE SCHEMA IF NOT EXISTS payment;

SET search_path TO payment;

CREATE TABLE IF NOT EXISTS payment_logs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id        TEXT NOT NULL,
    subject         TEXT DEFAULT '',
    amount          REAL DEFAULT 0,
    provider        TEXT DEFAULT '',
    status          TEXT DEFAULT 'pending',
    raw_response    TEXT DEFAULT '',
    created_at      TEXT DEFAULT NOW(),
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_payment_logs_order ON payment_logs(order_id);
CREATE INDEX IF NOT EXISTS idx_payment_logs_created ON payment_logs(created_at);

CREATE TABLE IF NOT EXISTS payment_configs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider    TEXT NOT NULL,
    config_key  TEXT NOT NULL,
    config_value TEXT NOT NULL DEFAULT '',
    updated_at  TEXT DEFAULT NOW(),
    UNIQUE(provider, config_key)
);
