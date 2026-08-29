-- ══════════════════════════════════════════════════════════════
-- Vault 2.0 — Database Migration 001
-- Creates backup tracking, scheduling, audit, and storage tables.
-- ══════════════════════════════════════════════════════════════

-- Schema isolation (plugin-standard v1.3 §9.1): every plugin uses its
-- own schema; system tables remain reachable via the trailing 'public'.
CREATE SCHEMA IF NOT EXISTS vault;
SET search_path TO vault, public;

-- 1. Backup job records
CREATE TABLE IF NOT EXISTS vault_backups (
    id              SERIAL PRIMARY KEY,
    label           VARCHAR(128) NOT NULL UNIQUE,
    backup_type     VARCHAR(32) NOT NULL DEFAULT 'full',   -- full, incremental, differential
    base_backup_id  INTEGER REFERENCES vault_backups(id),
    status          VARCHAR(32) NOT NULL DEFAULT 'running', -- running, success, failed, validating
    size_bytes      BIGINT,
    compressed_size BIGINT,
    encryption      VARCHAR(32) DEFAULT 'none',             -- none, aes256-gcm
    checksum_sha256 VARCHAR(64),
    storage_targets JSONB DEFAULT '[]',
    content_summary JSONB DEFAULT '{}',                     -- {"tables":123,"files":456,"plugins":["vault","ads"]}
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    error_message   TEXT,
    created_by      VARCHAR(128),
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 2. Backup schedules
CREATE TABLE IF NOT EXISTS vault_schedules (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(128) NOT NULL,
    cron_expression VARCHAR(64) NOT NULL,
    backup_type     VARCHAR(32) NOT NULL DEFAULT 'full',
    retention_days  INTEGER,
    retention_count INTEGER,
    storage_targets JSONB DEFAULT '[]',
    backup_window   JSONB DEFAULT NULL,                     -- {"start":"02:00","end":"06:00"}
    pre_hook        TEXT,
    post_hook       TEXT,
    enabled         BOOLEAN DEFAULT TRUE,
    last_run_at     TIMESTAMP,
    next_run_at     TIMESTAMP,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 3. Audit log
CREATE TABLE IF NOT EXISTS vault_audit_log (
    id              SERIAL PRIMARY KEY,
    action          VARCHAR(64) NOT NULL,                   -- backup.create, backup.delete, restore.execute, config.update
    resource_type   VARCHAR(64),
    resource_id     VARCHAR(128),
    operator        VARCHAR(128),
    ip_address      VARCHAR(45),
    details         JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 4. Storage target configs
CREATE TABLE IF NOT EXISTS vault_storage_targets (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(128) NOT NULL,
    storage_type    VARCHAR(32) NOT NULL,                   -- local, s3, oss, azure, gcs, sftp, webdav
    config          JSONB NOT NULL DEFAULT '{}',
    is_default      BOOLEAN DEFAULT FALSE,
    enabled         BOOLEAN DEFAULT TRUE,
    last_test_at    TIMESTAMP,
    last_test_ok    BOOLEAN,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_vault_backups_status ON vault_backups(status);
CREATE INDEX IF NOT EXISTS idx_vault_backups_created ON vault_backups(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vault_backups_type ON vault_backups(backup_type);
CREATE INDEX IF NOT EXISTS idx_vault_schedules_enabled ON vault_schedules(enabled);
CREATE INDEX IF NOT EXISTS idx_vault_schedules_next_run ON vault_schedules(next_run_at);
CREATE INDEX IF NOT EXISTS idx_vault_audit_created ON vault_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vault_audit_action ON vault_audit_log(action);
CREATE INDEX IF NOT EXISTS idx_vault_storage_enabled ON vault_storage_targets(enabled);
