-- v1.7.0 — HNSW 向量索引（仅当 embedding 列为 vector 类型时创建；
-- 无 pgvector 环境静默跳过，与 CE-D1 降级策略一致）。
SET search_path TO memory_engine, public;

DO $$
DECLARE col_type text;
BEGIN
    -- pgvector 列的 data_type 是 USER-DEFINED，必须看 udt_name
    SELECT udt_name INTO col_type FROM information_schema.columns
     WHERE table_schema = 'memory_engine'
       AND table_name = 'memories' AND column_name = 'embedding';
    IF col_type = 'vector' THEN
        CREATE INDEX IF NOT EXISTS idx_memories_embedding
            ON memories USING hnsw (embedding vector_cosine_ops);
    END IF;
END $$;
