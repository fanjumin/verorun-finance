SET search_path TO memory_engine, public;

-- Optional: HNSW index for vector search at scale (requires pgvector extension).
-- CREATE INDEX IF NOT EXISTS idx_memories_embedding
--     ON memories USING hnsw (embedding vector_cosine_ops);
