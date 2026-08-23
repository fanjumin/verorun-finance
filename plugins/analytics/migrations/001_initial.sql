-- ══════════════════════════════════════════════════════════════
-- Analytics Plugin — Database Migration 001 (Initial Schema)
-- Creates the 11 analytics tables inside the isolated 'analytics' schema.
-- Idempotent: safe to run multiple times (CREATE ... IF NOT EXISTS).
-- Schema isolation (plugin-standard v1.4 §9.1): every plugin uses its
-- own schema; system tables remain reachable via the trailing 'public'.
-- ══════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS analytics;
SET search_path TO analytics, public;

-- ===================================================================
-- 1. 原始访问日志（短存 — 默认保留 7 天）
-- ===================================================================
CREATE TABLE IF NOT EXISTS analytics_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    timestamp BIGINT NOT NULL,          -- 请求时间戳（秒）
    visitor_hash TEXT NOT NULL,          -- 匿名访客哈希
    session_hash TEXT,                   -- 会话哈希
    ip_prefix TEXT,                      -- 匿名 IP（前三段，如 "192.168.1.x"）
    country TEXT,                        -- 国家代码（如 "CN"）
    city TEXT,                           -- 城市
    user_agent TEXT,                     -- 原始 UA（用于解析，不存储精确IP）
    browser TEXT,                        -- 浏览器名
    browser_version TEXT,                -- 浏览器版本
    os_name TEXT,                        -- 操作系统
    device_type TEXT DEFAULT 'desktop',  -- desktop / mobile / tablet / bot
    is_bot BIGINT DEFAULT 0,           -- 是否爬虫/搜索引擎
    path TEXT NOT NULL,                  -- 请求路径
    query_string TEXT,                   -- 查询参数
    referer TEXT,                        -- 来源
    referer_domain TEXT,                 -- 来源域名（归一化）
    utm_source TEXT,                     -- UTM 参数
    utm_medium TEXT,
    utm_campaign TEXT,
    language TEXT,                       -- 浏览器语言
    status_code BIGINT DEFAULT 200,     -- HTTP 状态码
    response_time BIGINT DEFAULT 0,     -- 响应耗时（毫秒）
    request_method TEXT DEFAULT 'GET',   -- 请求方法
    service_name TEXT DEFAULT '',        -- 来源服务名（platform / admin）
    full_url TEXT,                       -- 完整 URL（脱敏后）
    content_type TEXT DEFAULT 'text/html' -- 内容类型
);
CREATE INDEX IF NOT EXISTS idx_analytics_logs_ts ON analytics_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_analytics_logs_hash ON analytics_logs(visitor_hash);
CREATE INDEX IF NOT EXISTS idx_analytics_logs_path ON analytics_logs(path);
CREATE INDEX IF NOT EXISTS idx_analytics_logs_bot ON analytics_logs(is_bot);

-- ===================================================================
-- 2. 每小时聚合 — 用于实时概览
-- ===================================================================
CREATE TABLE IF NOT EXISTS analytics_hourly_stats (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    date TEXT NOT NULL,            -- "2026-05-09"
    hour BIGINT NOT NULL,         -- 0-23
    service_name TEXT DEFAULT '',
    pv BIGINT DEFAULT 0,
    uv BIGINT DEFAULT 0,
    ipv BIGINT DEFAULT 0,         -- 独立 IP 数
    new_visitors BIGINT DEFAULT 0,
    bounce_count BIGINT DEFAULT 0,   -- 跳出次数（仅1次PV的会话）
    total_time BIGINT DEFAULT 0,     -- 总停留时间（秒）
    session_count BIGINT DEFAULT 0,  -- 会话数
    bot_count BIGINT DEFAULT 0,      -- 爬虫请求数
    error_count BIGINT DEFAULT 0,    -- 4xx/5xx 错误数
    avg_response_time BIGINT DEFAULT 0,  -- 平均响应时间（ms）
    UNIQUE(date, hour, service_name)
);
CREATE INDEX IF NOT EXISTS idx_analytics_hourly_date ON analytics_hourly_stats(date, hour);

-- ===================================================================
-- 3. 每日聚合 — 长期存储
-- ===================================================================
CREATE TABLE IF NOT EXISTS analytics_daily_stats (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    date TEXT NOT NULL UNIQUE,     -- "2026-05-09"
    pv BIGINT DEFAULT 0,
    uv BIGINT DEFAULT 0,
    ipv BIGINT DEFAULT 0,
    new_visitors BIGINT DEFAULT 0,
    returning_visitors BIGINT DEFAULT 0,
    bounce_rate DOUBLE PRECISION DEFAULT 0.0,
    avg_session_duration DOUBLE PRECISION DEFAULT 0.0,  -- 平均会话时长（秒）
    avg_depth DOUBLE PRECISION DEFAULT 0.0,            -- 平均访问深度（PV/会话）
    bot_pv BIGINT DEFAULT 0,
    error_pv BIGINT DEFAULT 0,
    avg_response_time BIGINT DEFAULT 0,
    total_sessions BIGINT DEFAULT 0,
    peak_concurrent BIGINT DEFAULT 0,     -- 当天最高同时在线
    peak_concurrent_time TEXT DEFAULT '',  -- 峰值时间
    last_calculated BIGINT DEFAULT 0     -- 最后一次计算时间戳
);

-- ===================================================================
-- 4. 访客会话
-- ===================================================================
CREATE TABLE IF NOT EXISTS analytics_visitor_sessions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_hash TEXT NOT NULL UNIQUE,     -- 会话标识
    visitor_hash TEXT NOT NULL,            -- 访客标识
    date TEXT NOT NULL,                    -- 会话日期
    start_time BIGINT NOT NULL,          -- 开始时间戳
    end_time BIGINT NOT NULL DEFAULT 0,  -- 最后活动时间
    duration BIGINT DEFAULT 0,           -- 会话时长（秒）
    page_views BIGINT DEFAULT 1,         -- 浏览页数
    entry_path TEXT,                      -- 入口页面
    exit_path TEXT,                       -- 退出页面
    referer TEXT,                         -- 来源
    browser TEXT,                         -- 浏览器
    os_name TEXT,
    device_type TEXT DEFAULT 'desktop',
    country TEXT,
    city TEXT,
    is_bot BIGINT DEFAULT 0,
    is_new_visitor BIGINT DEFAULT 0      -- 当日新访客
);
CREATE INDEX IF NOT EXISTS idx_analytics_sessions_hash ON analytics_visitor_sessions(session_hash);
CREATE INDEX IF NOT EXISTS idx_analytics_sessions_vhash ON analytics_visitor_sessions(visitor_hash);
CREATE INDEX IF NOT EXISTS idx_analytics_sessions_date ON analytics_visitor_sessions(date);
CREATE INDEX IF NOT EXISTS idx_analytics_sessions_bot ON analytics_visitor_sessions(is_bot);

-- ===================================================================
-- 5. 自定义业务事件
-- ===================================================================
CREATE TABLE IF NOT EXISTS analytics_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    timestamp BIGINT NOT NULL,
    visitor_hash TEXT NOT NULL,
    event_name TEXT NOT NULL,              -- "launch_agent", "view_stock", "create_workflow" 等
    event_category TEXT DEFAULT '',        -- 事件分类
    event_label TEXT DEFAULT '',           -- 事件标签
    event_value BIGINT DEFAULT 0,         -- 事件数值（可选）
    path TEXT DEFAULT '',
    service_name TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}'             -- JSON 额外数据
);
CREATE INDEX IF NOT EXISTS idx_analytics_events_ts ON analytics_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_analytics_events_name ON analytics_events(event_name);
CREATE INDEX IF NOT EXISTS idx_analytics_events_cat ON analytics_events(event_category);

-- ===================================================================
-- 6. 页面级统计
-- ===================================================================
CREATE TABLE IF NOT EXISTS analytics_page_stats (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    date TEXT NOT NULL,
    path TEXT NOT NULL,
    pv BIGINT DEFAULT 0,
    uv BIGINT DEFAULT 0,
    unique_entries BIGINT DEFAULT 0,     -- 作为入口次数
    unique_exits BIGINT DEFAULT 0,       -- 作为退出次数
    avg_time_on_page BIGINT DEFAULT 0,   -- 平均停留（秒）
    exit_rate DOUBLE PRECISION DEFAULT 0.0,           -- 退出率
    total_time BIGINT DEFAULT 0,
    UNIQUE(date, path)
);
CREATE INDEX IF NOT EXISTS idx_analytics_page_date ON analytics_page_stats(date);
CREATE INDEX IF NOT EXISTS idx_analytics_page_path ON analytics_page_stats(path);

-- ===================================================================
-- 7. 来源统计（Referer / UTM）
-- ===================================================================
CREATE TABLE IF NOT EXISTS analytics_source_stats (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    date TEXT NOT NULL,
    source_type TEXT NOT NULL,             -- "direct", "search", "social", "referral", "email", "utm"
    source_name TEXT NOT NULL,             -- 来源名（如 "google", "wechat", "direct"）
    pv BIGINT DEFAULT 0,
    uv BIGINT DEFAULT 0,
    UNIQUE(date, source_type, source_name)
);
CREATE INDEX IF NOT EXISTS idx_analytics_source_date ON analytics_source_stats(date);

-- ===================================================================
-- 8. 地理分布统计
-- ===================================================================
CREATE TABLE IF NOT EXISTS analytics_geo_stats (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    date TEXT NOT NULL,
    country TEXT NOT NULL,
    city TEXT DEFAULT '',
    pv BIGINT DEFAULT 0,
    uv BIGINT DEFAULT 0,
    UNIQUE(date, country, city)
);
CREATE INDEX IF NOT EXISTS idx_analytics_geo_date ON analytics_geo_stats(date);

-- ===================================================================
-- 9. 设备/浏览器统计
-- ===================================================================
CREATE TABLE IF NOT EXISTS analytics_device_stats (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    date TEXT NOT NULL,
    device_type TEXT NOT NULL,             -- "desktop", "mobile", "tablet", "bot"
    browser TEXT DEFAULT '',
    os_name TEXT DEFAULT '',
    pv BIGINT DEFAULT 0,
    uv BIGINT DEFAULT 0,
    UNIQUE(date, device_type, browser, os_name)
);
CREATE INDEX IF NOT EXISTS idx_analytics_device_date ON analytics_device_stats(date);

-- ===================================================================
-- 10. 告警规则
-- ===================================================================
CREATE TABLE IF NOT EXISTS analytics_alerts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,                    -- 告警名称
    enabled BIGINT DEFAULT 1,
    metric TEXT NOT NULL,                  -- "uv", "pv", "bounce_rate", "error_rate", "response_time"
    operator TEXT NOT NULL,                -- "gt", "lt", "gte", "lte", "eq", "change_pct"
    threshold DOUBLE PRECISION NOT NULL,              -- 阈值
    time_window TEXT DEFAULT '1h',        -- 时间窗口: "1h", "24h", "7d"
    comparison TEXT DEFAULT 'absolute',    -- "absolute" / "change_pct"
    channels TEXT DEFAULT '["notification"]',  -- 通知渠道
    last_triggered BIGINT DEFAULT 0,
    created_at BIGINT DEFAULT 0,
    updated_at BIGINT DEFAULT 0
);

-- ===================================================================
-- 11. 隐私配置
-- ===================================================================
CREATE TABLE IF NOT EXISTS analytics_privacy_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at BIGINT DEFAULT 0
);

-- 默认隐私设置
INSERT INTO analytics_privacy_config (key, value, updated_at) VALUES
    ('ip_anonymization', 'true', 0),
    ('geo_analysis_enabled', 'true', 0),
    ('ua_parsing_enabled', 'true', 0),
    ('log_retention_days', '30', 0),
    ('aggregation_retention_days', '365', 0),
    ('track_bots', 'true', 0),
    ('exclude_internal_ips', 'true', 0),
    ('internal_ip_ranges', '["127.0.0.0/8","10.0.0.0/8","172.16.0.0/12","192.168.0.0/16"]', 0),
    ('exclude_paths', '["/static/*","/favicon.ico","/robots.txt","/health","/admin/automation/*","/admin/analytics/*"]', 0),
    ('anonymize_query_params', '["token","password","key","secret","auth"]', 0)
    ON CONFLICT(key) DO NOTHING;
