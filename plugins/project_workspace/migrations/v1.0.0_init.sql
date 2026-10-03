CREATE SCHEMA IF NOT EXISTS project_workspace;

-- pgvector 为平台能力（trusted extension）：迁移自建，不依赖部署脚本预建。
-- 显式 SCHEMA public：迁移执行器已预设 search_path 为插件 schema，若不显式指定，
-- 扩展会落入插件 schema（其他插件 search_path 不含它 → vector 类型不可见）。
-- 【科研版桌面降级 §6.4】捆绑 PostgreSQL 可能无 vector 二进制：创建失败不阻断安装，
-- document_chunks.embedding 列转为探测式建列（见文件末尾），
-- 检索层已有 keyword 路径自动降级（services/retriever.py）。
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pgvector unavailable, project_workspace degrades to keyword tier';
END
$$;

SET search_path TO project_workspace, public;

-- Plugin degrades gracefully to keyword search when the column is not usable.

-- -- Projects: top-level isolation unit ------------------------------------
-- Each project is fully isolated from other projects.
-- Cross-project search is opt-in via plugin config enable_cross_project_search.
CREATE TABLE IF NOT EXISTS projects (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_type    varchar(20)  NOT NULL DEFAULT 'user',
    owner_id      varchar(64)  NOT NULL,
    name          varchar(255) NOT NULL,
    description   text         NOT NULL DEFAULT '',
    tags          text[]       NOT NULL DEFAULT '{}',
    visibility    varchar(16)  NOT NULL DEFAULT 'private',
    status        varchar(16)  NOT NULL DEFAULT 'active',
    member_count  integer      NOT NULL DEFAULT 0,
    doc_count     integer      NOT NULL DEFAULT 0,
    metadata      jsonb        NOT NULL DEFAULT '{}',
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_projects_owner
    ON projects (owner_type, owner_id, status);
CREATE INDEX IF NOT EXISTS idx_projects_tags
    ON projects USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_projects_created
    ON projects (created_at DESC);

-- -- Project Members ------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_members (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id     varchar(64)  NOT NULL,
    role        varchar(16)  NOT NULL DEFAULT 'member',
    joined_at   timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (project_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_project_members_user
    ON project_members (user_id);

-- -- Documents: the core knowledge unit -----------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    uuid         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename      varchar(512) NOT NULL,
    original_name varchar(512) NOT NULL,
    file_ext      varchar(20)  NOT NULL DEFAULT '',
    file_size     integer      NOT NULL DEFAULT 0,
    mime_type     varchar(128) NOT NULL DEFAULT '',
    page_count    integer      DEFAULT 0,
    word_count    integer      DEFAULT 0,
    char_count    integer      DEFAULT 0,
    status        varchar(16)  NOT NULL DEFAULT 'pending',
    error_msg     text,
    summary       text,
    language      varchar(10),
    tags          text[]       NOT NULL DEFAULT '{}',
    source_url    varchar(1024),
    metadata      jsonb        NOT NULL DEFAULT '{}',
    uploaded_by   varchar(64)  NOT NULL,
    uploaded_at   timestamptz  NOT NULL DEFAULT now(),
    processed_at  timestamptz,
    updated_at    timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_project
    ON documents (project_id, status);
CREATE INDEX IF NOT EXISTS idx_documents_status
    ON documents (status);
CREATE INDEX IF NOT EXISTS idx_documents_tags
    ON documents USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_documents_uploaded
    ON documents (uploaded_at DESC);

-- -- Document Chunks: vectorized fragments for RAG ------------------------
CREATE TABLE IF NOT EXISTS document_chunks (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   uuid         NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    project_id    uuid         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    chunk_index   integer      NOT NULL,
    content       text         NOT NULL,
    token_count   integer      NOT NULL DEFAULT 0,
    section_title varchar(255),
    page_number   integer,
    metadata      jsonb        NOT NULL DEFAULT '{}',
    created_at    timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chunks_document
    ON document_chunks (document_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_chunks_project
    ON document_chunks (project_id);

-- pgvector 探测式建列：vector 类型可用时补 embedding 列；
-- 不可用时跳过（检索层 keyword 路径自动降级；veroscholar kb_sync 同样兼容）。
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding vector(1536);
    ELSE
        RAISE NOTICE 'vector type absent: document_chunks.embedding skipped';
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'embedding column bootstrap skipped';
END
$$;

-- -- Citations: extracted references --------------------------------------
CREATE TABLE IF NOT EXISTS citations (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   uuid         NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    project_id    uuid         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    citation_key  varchar(255) NOT NULL,
    title         varchar(512),
    authors       text[]       NOT NULL DEFAULT '{}',
    year          integer,
    journal       varchar(512),
    doi           varchar(255),
    url           varchar(1024),
    raw_text      text,
    confidence    real         NOT NULL DEFAULT 0.5,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (document_id, citation_key)
);

CREATE INDEX IF NOT EXISTS idx_citations_project
    ON citations (project_id, citation_key);

-- -- Q&A Logs: query history with sources for traceability ----------------
CREATE TABLE IF NOT EXISTS qa_logs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    uuid         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id       varchar(64)  NOT NULL,
    query         text         NOT NULL,
    answer        text,
    sources       jsonb        NOT NULL DEFAULT '[]',
    agent_id      varchar(64),
    model_used    varchar(128),
    tokens_used   integer      NOT NULL DEFAULT 0,
    latency_ms    integer      NOT NULL DEFAULT 0,
    feedback      smallint,
    created_at    timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_qa_project
    ON qa_logs (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qa_user
    ON qa_logs (user_id, created_at DESC);

-- -- Schema Version Tracking ----------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version     varchar(16) PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
