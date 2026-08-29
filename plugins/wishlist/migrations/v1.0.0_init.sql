-- Wishlist Plugin: Initial Schema v1.0.0
-- 创建时间: 2026-08-07
-- 独立 PostgreSQL schema，单库多 Schema 架构（插件标准 §9.1）

CREATE SCHEMA IF NOT EXISTS wishlist;

CREATE TABLE IF NOT EXISTS wishlist.wishlist (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    product_id  BIGINT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_wishlist_user ON wishlist.wishlist(user_id);
CREATE INDEX IF NOT EXISTS idx_wishlist_product ON wishlist.wishlist(product_id);

-- Schema 版本记录表
CREATE TABLE IF NOT EXISTS wishlist._schema_version (
    version     TEXT NOT NULL,
    applied_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO wishlist._schema_version (version) VALUES ('1.0.0');
