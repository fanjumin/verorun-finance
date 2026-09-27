-- VeroRun net_proxy v1.0.0 初始迁移（幂等）
-- ---------------------------------------------------------------------------
-- 约定（plugin-standard-v1.8 §9.1 / §11.2）：
--   * 单库多 Schema：本文件建独立 PG schema `net_proxy`，禁止独立数据库；
--   * 全部 DDL 幂等（IF NOT EXISTS），可安全重复执行；
--   * 本文件由 models.run_migrations() 执行一次，文件名记入
--     net_proxy.schema_migrations（advisory 锁 + run-once）。
--
-- 代码侧 SQL 一律使用 ? 占位符（由 plugins/_base/db.py::_replace_placeholders
-- 垫片转换为 %s）；**禁止** jsonb ? 运算符（标准 §18.6.2，2026-09-17 商店
-- 500 事故根因）——本文件的 json 列一律用 TEXT 存 JSON 文本，判定走 LIKE。
--
-- 枚举合法性（protocol / region / action）在应用层校验（channels.py /
-- rules.py），DDL 不加 CHECK：保持迁移最小化，与既有插件迁移风格一致。
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS net_proxy;

-- ── 6.1 proxy_channels：出口通道注册表 ──────────────────────────────────────
-- 含端点、加密凭据、区域/画像/用途标签、权重、并发上限与运行态。
-- 端点与凭据全部运行时登记，代码零预置。
CREATE TABLE IF NOT EXISTS net_proxy.proxy_channels (
    id                   BIGSERIAL   PRIMARY KEY,
    name                 TEXT        NOT NULL,
    protocol             TEXT        NOT NULL DEFAULT 'http',
    host                 TEXT        NOT NULL,
    port                 INTEGER     NOT NULL,
    auth_username        TEXT        NOT NULL DEFAULT '',
    auth_password_enc    TEXT        NOT NULL DEFAULT '',
    region               TEXT        NOT NULL DEFAULT 'any',
    profile_tags         TEXT        NOT NULL DEFAULT '[]',
    usage_tags           TEXT        NOT NULL DEFAULT '[]',
    weight               INTEGER     NOT NULL DEFAULT 1,
    max_concurrent       INTEGER     NOT NULL DEFAULT 0,
    enabled              BOOLEAN     NOT NULL DEFAULT TRUE,
    fused_until          TIMESTAMPTZ,
    consecutive_failures INTEGER     NOT NULL DEFAULT 0,
    last_latency_ms      INTEGER,
    last_probe_at        TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 按名称唯一，避免重复登记同一逻辑通道
CREATE UNIQUE INDEX IF NOT EXISTS idx_proxy_channels_name
    ON net_proxy.proxy_channels (name);

-- ── 6.2 proxy_rules：路由规则表 ─────────────────────────────────────────────
-- priority 升序首个 enabled=1 命中者为胜（RuleEngine L2）。
CREATE TABLE IF NOT EXISTS net_proxy.proxy_rules (
    id             BIGSERIAL   PRIMARY KEY,
    priority       INTEGER     NOT NULL DEFAULT 100,
    target_pattern TEXT        NOT NULL DEFAULT '*',
    caller         TEXT        NOT NULL DEFAULT '*',
    region         TEXT        NOT NULL DEFAULT '',
    profile_tags   TEXT        NOT NULL DEFAULT '[]',
    usage_tags     TEXT        NOT NULL DEFAULT '[]',
    action         TEXT        NOT NULL DEFAULT 'DIRECT',
    note           TEXT        NOT NULL DEFAULT '',
    enabled        BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_proxy_rules_match
    ON net_proxy.proxy_rules (enabled, priority);

-- ── 6.3 proxy_request_log：出站审计（脱敏） ─────────────────────────────────
-- 仅记目标 host（不记完整 URL 与 query），凭据绝不落库。
CREATE TABLE IF NOT EXISTS net_proxy.proxy_request_log (
    id          BIGSERIAL   PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    caller      TEXT        NOT NULL DEFAULT '',
    target_host TEXT        NOT NULL DEFAULT '',
    scheme      TEXT        NOT NULL DEFAULT '',
    channel_id  BIGINT,
    action      TEXT        NOT NULL DEFAULT '',
    status_code INTEGER,
    latency_ms  INTEGER,
    bytes_up    BIGINT      NOT NULL DEFAULT 0,
    bytes_down  BIGINT      NOT NULL DEFAULT 0,
    error       TEXT        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_prl_ts
    ON net_proxy.proxy_request_log (ts);
CREATE INDEX IF NOT EXISTS idx_prl_channel_ts
    ON net_proxy.proxy_request_log (channel_id, ts);

-- ── 6.4 proxy_probe_log：探活日志 ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS net_proxy.proxy_probe_log (
    id         BIGSERIAL   PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    channel_id BIGINT      NOT NULL,
    ok         BOOLEAN     NOT NULL,
    latency_ms INTEGER,
    detail     TEXT        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ppl_channel_ts
    ON net_proxy.proxy_probe_log (channel_id, ts);
