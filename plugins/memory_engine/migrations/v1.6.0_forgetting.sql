-- v1.6.0 — Forgetting（记忆遗忘/整理闭环）: 为「软归档 + 质量衰减」提供表达式索引。
--
-- 背景: services/forgetting.py 的两条更新语句均以
--     COALESCE(last_hit_at, created_at) < now() - make_interval(days => ?)
-- 为判龄条件，且只作用于 status = 'active' 的行。
--
-- 说明:
--   * now() 非 IMMUTABLE，故 `now() - interval` 整体无法建索引；
--     可索引的只有 COALESCE(last_hit_at, created_at) 这一表达式本身。
--   * 采用部分索引（WHERE status = 'active'）——归档与衰减都只看活跃行，
--     同时使索引体积随归档进展自然收缩。
--   * 不新增 CONCURRENTLY: migrate() 在事务内执行整文件，CONCURRENTLY 不允许。
SET search_path TO memory_engine, public;

CREATE INDEX IF NOT EXISTS idx_memories_forgetting
    ON memories (COALESCE(last_hit_at, created_at))
    WHERE status = 'active';
