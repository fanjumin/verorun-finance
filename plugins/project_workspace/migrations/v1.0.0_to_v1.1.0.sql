-- project_workspace v1.1.0: 适配 text-embedding-004 的 768 维向量。
-- 前置条件：document_chunks.embedding 列内无 1536 维历史数据
-- （Kernel Patch A 之前 UnifiedLLM 无 get_embedding，向量从未成功入库，列基本为空）。
-- 若存在历史 1536 维数据，本迁移将失败并回滚，需先人工清理/重建该列。
--
-- 【无 pgvector 环境补完】原写法是无条件 ALTER ... TYPE vector(768)，
-- 在捆绑 PG 无 vector 二进制时会抛 `type "vector" does not exist` 并使整个迁移事务回滚
-- → 连 projects/documents 等主表都建不出来（实测：project_workspace schema 全空）。
-- 这与 init.sql 的既定降级口径（§6.4：不阻断安装，检索走 keyword tier）不一致。
-- 现改为与 init.sql 同样的探测式写法：无 vector 扩展则跳过，仅留 NOTICE。

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        ALTER TABLE project_workspace.document_chunks
            ALTER COLUMN embedding TYPE vector(768) USING embedding::vector(768);
    ELSE
        RAISE NOTICE 'pgvector absent: skip embedding 1536 -> 768 alter (keyword tier)';
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'embedding dimension alter skipped: %', SQLERRM;
END
$$;
