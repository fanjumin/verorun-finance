-- SMS Plugin — v1.0.0 → v1.1.0 迁移
-- 变更内容（对应 2026-08-06 代码审计修复）：
--   1. 新增 sms_rate_limits 表（频率限制迁入插件 schema，数据隔离 §11.2）
--   2. sms_templates/sms_logs 的时间列由 TEXT 升级为 TIMESTAMPTZ
-- 全部为幂等语句，可安全重复执行。
-- 运行时由 models.py 的 init_sms_db() / _ensure_time_columns() 幂等执行。

SET search_path TO sms;

CREATE TABLE IF NOT EXISTS sms_rate_limits (
    phone       TEXT NOT NULL,
    hour_bucket TEXT NOT NULL,
    count       BIGINT DEFAULT 1,
    PRIMARY KEY (phone, hour_bucket)
);

ALTER TABLE sms_templates ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at::timestamptz;
ALTER TABLE sms_templates ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING updated_at::timestamptz;
ALTER TABLE sms_logs ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at::timestamptz;
