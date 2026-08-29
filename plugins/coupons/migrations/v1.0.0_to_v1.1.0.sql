-- Coupons Plugin — v1.0.0 → v1.1.0 迁移基线
-- 新增：Schema 版本跟踪表（§10.6）
-- 全部为 CREATE TABLE IF NOT EXISTS，幂等可重复执行。

SET search_path TO coupons;

CREATE TABLE IF NOT EXISTS schema_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT DEFAULT '',
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
