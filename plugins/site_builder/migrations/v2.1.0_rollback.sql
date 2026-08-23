-- v2.1.0_rollback.sql: 回滚 Site Builder 插件（删除独立库 site_builder schema）
-- 注意：仅在验证期后且确认不再需要独立库数据时执行；主库旧表不受影响。
BEGIN;

DROP SCHEMA IF EXISTS site_builder CASCADE;

COMMIT;
