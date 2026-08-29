-- ali_api 插件 — Schema 基线迁移 v0.0.0 → v2.1.0
-- =====================================================
-- 目标: 精确记录 v2.1.0 完整基线 schema，与 models.py create_table() 逐表对齐。
-- 说明: 运行时迁移以 models.init_tables()（Python，幂等）为准，
--       本文件为版本化 SQL 文档，供审计与手动比对。
-- 执行前提: SET search_path TO ali_api;

-- 1. 商品缓存表（models.py: AliApiItem.create_table）
CREATE TABLE IF NOT EXISTS ali_api_items (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             BIGINT DEFAULT 0,
    product_id          TEXT UNIQUE NOT NULL,
    source_url          TEXT DEFAULT '',
    title               TEXT NOT NULL DEFAULT '',
    original_title      TEXT DEFAULT '',
    price               DECIMAL(10,2) DEFAULT 0,
    original_price      DECIMAL(10,2) DEFAULT 0,
    currency            TEXT DEFAULT 'CNY',
    category            TEXT DEFAULT '',
    images              TEXT DEFAULT '[]',
    specs               TEXT DEFAULT '{}',
    product_sku         TEXT DEFAULT '[]',
    description         TEXT DEFAULT '',
    -- B2B 增强字段
    moq                 BIGINT DEFAULT 0,              -- 最小起订量
    wholesale_price     TEXT DEFAULT '[]',              -- 批发阶梯价 JSON
    is_support_agent    BIGINT DEFAULT 0,              -- 是否支持一件代发
    seller_credit       BIGINT DEFAULT 0,              -- 卖家诚信通等级
    shop_level          BIGINT DEFAULT 0,              -- 店铺等级
    seller_name         TEXT DEFAULT '',                -- 卖家名称
    seller_id           TEXT DEFAULT '',                -- 卖家 ID
    location            TEXT DEFAULT '',                -- 所在地
    -- AI 处理结果
    ai_title            TEXT DEFAULT '',
    ai_title_options    TEXT DEFAULT '[]',
    selected_title      TEXT DEFAULT '',
    ai_description      TEXT DEFAULT '',
    -- 发布状态
    publish_status      TEXT DEFAULT 'draft',
    target_product_id   BIGINT DEFAULT NULL,
    -- 统计
    api_call_count      BIGINT DEFAULT 0,
    -- 原始API响应
    api_response        TEXT DEFAULT '{}',
    status              TEXT DEFAULT 'active',
    last_synced_at      TIMESTAMPTZ DEFAULT NULL,
    error_msg           TEXT DEFAULT '',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    processed_at        TIMESTAMPTZ DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_ali_items_product_id ON ali_api_items(product_id);
CREATE INDEX IF NOT EXISTS idx_ali_items_status ON ali_api_items(status);
CREATE INDEX IF NOT EXISTS idx_ali_items_category ON ali_api_items(category);
CREATE INDEX IF NOT EXISTS idx_ali_items_user ON ali_api_items(user_id);
CREATE INDEX IF NOT EXISTS idx_ali_items_publish ON ali_api_items(publish_status);

-- 2. 1688 商品评论表（models.py: AliApiReview.create_table）
CREATE TABLE IF NOT EXISTS ali_api_reviews (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id      TEXT NOT NULL,
    review_id       TEXT UNIQUE NOT NULL,
    user_id         BIGINT DEFAULT 0,
    buyer_name      TEXT DEFAULT '',
    rating          BIGINT DEFAULT 5,
    content         TEXT DEFAULT '',
    review_time     TEXT DEFAULT '',
    spec_info       TEXT DEFAULT '',
    images          TEXT DEFAULT '[]',
    is_anonymous    BIGINT DEFAULT 0,
    reply_content   TEXT DEFAULT '',
    reply_time      TEXT DEFAULT '',
    raw_data        TEXT DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reviews_product ON ali_api_reviews(product_id);
CREATE INDEX IF NOT EXISTS idx_reviews_rating ON ali_api_reviews(rating);

-- 3. API 调用日志表（models.py: AliApiLog.create_table）
CREATE TABLE IF NOT EXISTS ali_api_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT,
    api_key_id BIGINT,
    endpoint TEXT NOT NULL,
    params TEXT DEFAULT '{}',
    response_code BIGINT,
    response_time BIGINT,
    success BIGINT DEFAULT 0,
    error_msg TEXT,
    ip_address TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ali_logs_user ON ali_api_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_ali_logs_endpoint ON ali_api_logs(endpoint);
CREATE INDEX IF NOT EXISTS idx_ali_logs_created ON ali_api_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_ali_logs_success ON ali_api_logs(success);

-- 4. 用户调用统计表（models.py: AliApiUserStats.create_table）
CREATE TABLE IF NOT EXISTS ali_api_user_stats (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT UNIQUE,
    calls_today BIGINT DEFAULT 0,
    calls_total BIGINT DEFAULT 0,
    last_reset_date TEXT,
    last_call_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ali_user_stats_user ON ali_api_user_stats(user_id);

-- 5. 1688 OAuth Token 存储表（models.py: AliApiToken.create_table）
CREATE TABLE IF NOT EXISTS ali_api_tokens (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         BIGINT DEFAULT 0,
    app_key         TEXT NOT NULL DEFAULT '',
    access_token    TEXT NOT NULL DEFAULT '',
    refresh_token   TEXT NOT NULL DEFAULT '',
    ali_id          TEXT DEFAULT '',
    resource_owner  TEXT DEFAULT '',
    expires_in      BIGINT DEFAULT 0,
    token_type      TEXT DEFAULT 'bearer',
    scope           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 6. OAuth state 存储表（models.py: OAuthState.create_table）
CREATE TABLE IF NOT EXISTS ali_oauth_states (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    state       TEXT UNIQUE NOT NULL,
    redirect_uri TEXT NOT NULL DEFAULT '',
    user_id     BIGINT DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    used        BIGINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_oauth_state ON ali_oauth_states(state);

-- 7. 插件配置表（models.py: AliApiConfig.create_table）
CREATE TABLE IF NOT EXISTS ali_api_config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL DEFAULT '',
    description     TEXT DEFAULT '',
    encrypted       BIGINT DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 8. 1688 代发采购单表（models.py: AliPurchaseOrder.create_table）
CREATE TABLE IF NOT EXISTS ali_purchase_orders (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    local_order_id      TEXT NOT NULL,
    local_order_item_id BIGINT NOT NULL,
    product_id          BIGINT NOT NULL,
    ali_product_id      TEXT NOT NULL,
    ali_sku_id          TEXT DEFAULT '',
    quantity            BIGINT DEFAULT 1,
    price               DOUBLE PRECISION DEFAULT 0,
    total_fee           DOUBLE PRECISION DEFAULT 0,
    ali_order_id        TEXT DEFAULT '',
    ali_order_status    TEXT DEFAULT 'pending',
    supplier_name       TEXT DEFAULT '',
    supplier_id         TEXT DEFAULT '',
    tracking_company    TEXT DEFAULT '',
    tracking_number     TEXT DEFAULT '',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    ordered_at          TIMESTAMPTZ,
    shipped_at          TIMESTAMPTZ,
    remark              TEXT DEFAULT '',
    UNIQUE(local_order_item_id, ali_product_id)
);
CREATE INDEX IF NOT EXISTS idx_ali_po_order ON ali_purchase_orders(local_order_id);
CREATE INDEX IF NOT EXISTS idx_ali_po_status ON ali_purchase_orders(ali_order_status);
CREATE INDEX IF NOT EXISTS idx_ali_po_ali_order ON ali_purchase_orders(ali_order_id);

-- 9. 本地 Agent 注册表（models.py: AgentRegistry.create_table）
CREATE TABLE IF NOT EXISTS agent_registry (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            TEXT NOT NULL,
    identifier      TEXT DEFAULT '',
    role_type       TEXT DEFAULT 'sub',
    description     TEXT DEFAULT '',
    domain          TEXT DEFAULT 'supply_chain',
    provider        TEXT DEFAULT '',
    model_name      TEXT DEFAULT '',
    system_prompt   TEXT DEFAULT '',
    capabilities    TEXT DEFAULT '[]',
    is_active       BIGINT DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ali_agent_registry_id ON agent_registry(identifier);

-- 10. Schema 版本跟踪表（models.py: SchemaMeta.create_table）
CREATE TABLE IF NOT EXISTS schema_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT DEFAULT '',
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
