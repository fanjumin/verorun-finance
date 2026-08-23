#!/usr/bin/env python3
"""
analytics/models.py — Analytics 数据库 Schema + 完整 CRUD

共有 11 张表:
  1. analytics_logs           — 原始访问日志（短存，7-30天）
  2. analytics_hourly_stats   — 每小时聚合（实时概览使用）
  3. analytics_daily_stats    — 每日聚合（长期存储）
  4. analytics_visitor_sessions — 访客会话（每日 Visitor Hash）
  5. analytics_events         — 自定义业务事件
  6. analytics_page_stats     — 页面级统计
  7. analytics_source_stats   — 来源/Referer 统计
  8. analytics_geo_stats      — 地理分布统计
  9. analytics_device_stats   — 设备/浏览器统计
  10. analytics_alerts        — 告警规则
  11. analytics_privacy_config — 隐私配置
"""

from i18n import _
import os
import logging
import psycopg2
import psycopg2.extras
import sys
import json
import hashlib
import time
import re
import threading
from datetime import datetime, timedelta
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
from services.deployment_config import deploy
from plugins._base.db import get_raw_connection
from .ua_parser import BOT_PATTERNS  # BOT_PATTERNS 唯一真源（ua_parser.py）

logger = logging.getLogger('analytics.models')

# ─── 数据库（PG schema）─────────────────────────────────────────────────────

# analytics 使用 PG schema analytics，不依赖主库
_ANALYTICS_DB = None


def _to_pg_sql(sql: str) -> str:
    """将 SQLite 的 ? 占位符转换为 PG 的 %s，跳过字符串/标识符字面量内的 ?"""
    out = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in ('"', "'"):
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == '\\':
                    j += 2
                    continue
                if sql[j] == quote:
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
        elif ch == '?':
            out.append('%s')
            i += 1
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


class _PgConnection:
    """psycopg2 connection adapter with sqlite3-compatible interface."""
    def __init__(self, conn):
        self._conn = conn
    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if params is not None:
            cur.execute(_to_pg_sql(sql), params)
        else:
            cur.execute(sql)
        return cur
    def commit(self):
        self._conn.commit()
    def rollback(self):
        """公开回滚接口（替代外部直接访问私有 _conn）"""
        self._conn.rollback()
    def close(self):
        self._conn.close()


# ─── Schema ───────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
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
"""

# ─── Schema 升级（幂等自愈）────────────────────────────────────────────────────

# CREATE TABLE IF NOT EXISTS 不会修改已存在的旧表。服务器从旧版本升级时，
# 新列/新表可能缺失，导致查询抛 PG 异常（表现为仪表盘全部 500）。
# 下表按当前 SCHEMA_SQL 逐列列出（名称 + 类型 + 可选默认值，不携带 NOT NULL，
# 避免旧表已有行数据时 ADD COLUMN NOT NULL 因空值而失败），用于幂等补齐。
_SCHEMA_UPGRADE_COLUMNS = {
    'analytics_logs': [
        "timestamp BIGINT", "visitor_hash TEXT", "session_hash TEXT", "ip_prefix TEXT",
        "country TEXT", "city TEXT", "user_agent TEXT", "browser TEXT",
        "browser_version TEXT", "os_name TEXT", "device_type TEXT DEFAULT 'desktop'",
        "is_bot BIGINT DEFAULT 0", "path TEXT", "query_string TEXT", "referer TEXT",
        "referer_domain TEXT", "utm_source TEXT", "utm_medium TEXT", "utm_campaign TEXT",
        "language TEXT", "status_code BIGINT DEFAULT 200", "response_time BIGINT DEFAULT 0",
        "request_method TEXT DEFAULT 'GET'", "service_name TEXT DEFAULT ''",
        "full_url TEXT", "content_type TEXT DEFAULT 'text/html'",
    ],
    'analytics_hourly_stats': [
        "date TEXT", "hour BIGINT", "service_name TEXT DEFAULT ''", "pv BIGINT DEFAULT 0",
        "uv BIGINT DEFAULT 0", "ipv BIGINT DEFAULT 0", "new_visitors BIGINT DEFAULT 0",
        "bounce_count BIGINT DEFAULT 0", "total_time BIGINT DEFAULT 0",
        "session_count BIGINT DEFAULT 0", "bot_count BIGINT DEFAULT 0",
        "error_count BIGINT DEFAULT 0", "avg_response_time BIGINT DEFAULT 0",
    ],
    'analytics_daily_stats': [
        "date TEXT", "pv BIGINT DEFAULT 0", "uv BIGINT DEFAULT 0", "ipv BIGINT DEFAULT 0",
        "new_visitors BIGINT DEFAULT 0", "returning_visitors BIGINT DEFAULT 0",
        "bounce_rate DOUBLE PRECISION DEFAULT 0.0", "avg_session_duration DOUBLE PRECISION DEFAULT 0.0",
        "avg_depth DOUBLE PRECISION DEFAULT 0.0", "bot_pv BIGINT DEFAULT 0",
        "error_pv BIGINT DEFAULT 0", "avg_response_time BIGINT DEFAULT 0",
        "total_sessions BIGINT DEFAULT 0", "peak_concurrent BIGINT DEFAULT 0",
        "peak_concurrent_time TEXT DEFAULT ''", "last_calculated BIGINT DEFAULT 0",
    ],
    'analytics_visitor_sessions': [
        "session_hash TEXT", "visitor_hash TEXT", "date TEXT", "start_time BIGINT",
        "end_time BIGINT DEFAULT 0", "duration BIGINT DEFAULT 0", "page_views BIGINT DEFAULT 1",
        "entry_path TEXT", "exit_path TEXT", "referer TEXT", "browser TEXT", "os_name TEXT",
        "device_type TEXT DEFAULT 'desktop'", "country TEXT", "city TEXT",
        "is_bot BIGINT DEFAULT 0", "is_new_visitor BIGINT DEFAULT 0",
    ],
    'analytics_events': [
        "timestamp BIGINT", "visitor_hash TEXT", "event_name TEXT",
        "event_category TEXT DEFAULT ''", "event_label TEXT DEFAULT ''",
        "event_value BIGINT DEFAULT 0", "path TEXT DEFAULT ''",
        "service_name TEXT DEFAULT ''", "metadata TEXT DEFAULT '{}'",
    ],
    'analytics_page_stats': [
        "date TEXT", "path TEXT", "pv BIGINT DEFAULT 0", "uv BIGINT DEFAULT 0",
        "unique_entries BIGINT DEFAULT 0", "unique_exits BIGINT DEFAULT 0",
        "avg_time_on_page BIGINT DEFAULT 0", "exit_rate DOUBLE PRECISION DEFAULT 0.0",
        "total_time BIGINT DEFAULT 0",
    ],
    'analytics_source_stats': [
        "date TEXT", "source_type TEXT", "source_name TEXT", "pv BIGINT DEFAULT 0",
        "uv BIGINT DEFAULT 0",
    ],
    'analytics_geo_stats': [
        "date TEXT", "country TEXT", "city TEXT DEFAULT ''", "pv BIGINT DEFAULT 0",
        "uv BIGINT DEFAULT 0",
    ],
    'analytics_device_stats': [
        "date TEXT", "device_type TEXT", "browser TEXT DEFAULT ''", "os_name TEXT DEFAULT ''",
        "pv BIGINT DEFAULT 0", "uv BIGINT DEFAULT 0",
    ],
    'analytics_alerts': [
        "name TEXT", "enabled BIGINT DEFAULT 1", "metric TEXT", "operator TEXT",
        "threshold DOUBLE PRECISION", "time_window TEXT DEFAULT '1h'",
        "comparison TEXT DEFAULT 'absolute'", "channels TEXT DEFAULT '[\"notification\"]'",
        "last_triggered BIGINT DEFAULT 0", "created_at BIGINT DEFAULT 0",
        "updated_at BIGINT DEFAULT 0",
    ],
    'analytics_privacy_config': [
        "key TEXT", "value TEXT", "updated_at BIGINT DEFAULT 0",
    ],
}


def _upgrade_schema(conn):
    """幂等补齐旧表缺失列（ALTER TABLE ... ADD COLUMN IF NOT EXISTS）。

    安全：可重复执行、不丢数据、失败会抛出并回滚（由调用方处理）。
    """
    for table, cols in _SCHEMA_UPGRADE_COLUMNS.items():
        for col_sql in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_sql}")
    conn.commit()


# 惰性 schema 自愈：每个进程首次 get_db() 时触发一次，避免服务器旧 schema 持续 500
_schema_ready = False
_schema_lock = threading.Lock()
_schema_last_attempt = 0.0


def _ensure_schema():
    """进程内惰性确保 schema 与当前代码一致（幂等；失败降级为普通 500 并可见）"""
    global _schema_ready, _schema_last_attempt
    if _schema_ready:
        return
    now = time.time()
    with _schema_lock:
        if _schema_ready:
            return
        if now - _schema_last_attempt < 60:
            return  # 重试频率限制：失败后每分钟最多重试一次
        _schema_last_attempt = now
        # 先置位防递归：init_analytics_tables() 内部会再次调用 get_db()
        _schema_ready = True
        try:
            init_analytics_tables()
        except Exception:
            _schema_ready = False
            logger.warning('Lazy schema ensure failed, will retry later', exc_info=True)


# ─── 连接管理 ──────────────────────────────────────────────────────────────────

_get_db = None

def set_db_func(func):
    """设置数据库连接获取函数，方便集成到主应用"""
    global _get_db
    _get_db = func

def get_db():
    """获取数据库连接（PG schema: analytics）"""
    _ensure_schema()
    if _get_db:
        return _get_db()
    raw = get_raw_connection()
    raw.autocommit = False
    with raw.cursor() as cur:  # 上下文管理器自动关闭游标，避免游标泄漏
        cur.execute("CREATE SCHEMA IF NOT EXISTS analytics")
    raw.commit()
    with raw.cursor() as cur:
        cur.execute("SET search_path TO analytics")
    raw.commit()
    return _PgConnection(raw)


# ─── 初始化 ─────────────────────────────────────────────────────────────────────

def _load_schema_sql() -> str:
    """优先从 migrations/001_initial.sql 加载 schema，缺失时回退内置 SCHEMA_SQL"""
    migration_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'migrations', '001_initial.sql')
    try:
        if os.path.exists(migration_path):
            with open(migration_path, 'r', encoding='utf-8') as f:
                return f.read()
    except Exception as e:
        logger.warning('Failed to read migration file %s: %s', migration_path, e, exc_info=True)
    return SCHEMA_SQL


def init_analytics_tables(db_path=None):
    """创建所有分析表（幂等，schema 唯一真源为 migrations/001_initial.sql）

    CREATE TABLE IF NOT EXISTS 仅建缺失表；对已存在的旧表追加
    ALTER TABLE ADD COLUMN IF NOT EXISTS 补齐新列（旧 schema 自愈）。
    """
    global _ANALYTICS_DB, _schema_ready
    if db_path:
        _ANALYTICS_DB = db_path
    conn = get_db()
    sql = _load_schema_sql()
    try:
        # psycopg2 原生支持一次执行多条语句，切勿用 ';' 手动切分：
        # 迁移文件注释里包含分号（见 001_initial.sql）时会被切出非法 SQL，
        # 导致语法错误 + 事务污染，后续语句全部报 transaction aborted。
        conn.execute(sql)
        # 幂等补齐旧表缺失列（不丢数据）
        _upgrade_schema(conn)
        _schema_ready = True
    except Exception as e:
        conn.rollback()
        _schema_ready = False
        logger.warning('Schema error: %s', e, exc_info=True)
    conn.commit()
    logger.info('PG schema analytics initialized (11 tables)')


# ─── 哈希与匿名化工具 ──────────────────────────────────────────────────────────

def hash_ip(ip: str) -> str:
    """IP 匿名化: 保留前三段，最后一段替换为 'x'"""
    if not ip or ip == '':
        return '0.0.0.x'
    parts = ip.split('.')
    if len(parts) == 4:
        return '.'.join(parts[:3]) + '.x'
    return ip

def make_visitor_hash(ip_prefix: str, user_agent: str, date_str: str) -> str:
    """
    生成每日访客哈希: SHA256(ip_prefix + '|' + ua + '|' + date)
    每天自动重置 — 同一用户明天生成不同 hash
    """
    raw = f"{ip_prefix}|{user_agent}|{date_str}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def make_session_hash(visitor_hash: str, timestamp: int, session_window: int = 1800) -> str:
    """
    生成会话哈希
    session_window: 会话超时窗口（秒），默认 30 分钟
    如果两次请求间隔 > session_window，视为新会话
    """
    window_key = timestamp // session_window
    raw = f"{visitor_hash}|{window_key}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]

def anonymize_query_string(qs: str, sensitive_params: list = None) -> str:
    """脱敏查询字符串中的敏感参数"""
    if not qs:
        return ''
    if sensitive_params is None:
        sensitive_params = ['token', 'password', 'key', 'secret', 'auth', 'code']
    import urllib.parse as up
    params = up.parse_qs(qs, keep_blank_values=True)
    for key in list(params.keys()):
        kl = key.lower()
        for sp in sensitive_params:
            if sp in kl:
                params[key] = ['[REDACTED]']
                break
    return up.urlencode(params, doseq=True)

def ip_in_ranges(ip: str, ranges: list) -> bool:
    """检查 IP 是否在内部网段中"""
    import ipaddress
    try:
        ip_obj = ipaddress.ip_address(ip)
        for r in ranges:
            try:
                net = ipaddress.ip_network(r, strict=False)
                if ip_obj in net:
                    return True
            except:
                continue
    except:
        return False
    return False

def is_bot(ua: str) -> bool:
    """检测是否是爬虫/搜索引擎（BOT_PATTERNS 定义于 ua_parser.py）"""
    if not ua:
        return True
    ua_lower = ua.lower()
    for pattern in BOT_PATTERNS:
        if pattern in ua_lower:
            return True
    return False

def normalize_referer(referer: str) -> str:
    """归一化 Referer 域名"""
    if not referer:
        return ''
    from urllib.parse import urlparse
    try:
        parsed = urlparse(referer)
        domain = parsed.netloc.lower()
        # 去掉 www. 前缀
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain
    except:
        return referer

def classify_source(referer: str, utm_source: str = '') -> tuple:
    """分类来源类型和名称"""
    if utm_source:
        return ('utm', utm_source)

    if not referer:
        return ('direct', 'direct')

    domain = normalize_referer(referer)

    # 搜索引擎
    SEARCH_DOMAINS = {
        'google.com', 'google.cn', 'bing.com', 'baidu.com', 'sogou.com',
        'so.com', 'yandex.com', 'duckduckgo.com'
    }
    if domain in SEARCH_DOMAINS:
        return ('search', domain.replace('.com', '').replace('.cn', ''))

    # 社交媒体
    SOCIAL_DOMAINS = {
        'weixin.qq.com', 'mp.weixin.qq.com', 'weibo.com', 'zhihu.com',
        'douyin.com', 'xiaohongshu.com', 'tieba.baidu.com',
        'twitter.com', 'facebook.com', 'linkedin.com', 'reddit.com'
    }
    if domain in SOCIAL_DOMAINS:
        return ('social', domain)

    # 同域（站内来源）
    OWN_DOMAINS = {deploy.DOMAIN, deploy.server_name('tm'), deploy.server_name('platform'),
                   deploy.server_name('agent')}
    if domain in OWN_DOMAINS:
        return ('internal', domain)

    return ('referral', domain)


# ─── 写操作 ───────────────────────────────────────────────────────────────────

def insert_log(conn, data: dict) -> int:
    """
    插入一条原始访问日志
    data 包含: timestamp, visitor_hash, session_hash, ip_prefix, user_agent, path,
               query_string, referer, status_code, response_time, ...
    """
    now = int(time.time())
    fields = {
        'timestamp': data.get('timestamp', now),
        'visitor_hash': data.get('visitor_hash', ''),
        'session_hash': data.get('session_hash', ''),
        'ip_prefix': data.get('ip_prefix', ''),
        'country': data.get('country', ''),
        'city': data.get('city', ''),
        'user_agent': data.get('user_agent', '')[:512],
        'browser': data.get('browser', ''),
        'browser_version': data.get('browser_version', ''),
        'os_name': data.get('os_name', ''),
        'device_type': data.get('device_type', 'desktop'),
        'is_bot': 1 if data.get('is_bot') else 0,
        'path': data.get('path', '/'),
        'query_string': data.get('query_string', '')[:512],
        'referer': (data.get('referer', '') or '')[:1024],
        'referer_domain': data.get('referer_domain', ''),
        'utm_source': data.get('utm_source', ''),
        'utm_medium': data.get('utm_medium', ''),
        'utm_campaign': data.get('utm_campaign', ''),
        'language': data.get('language', '')[:64],
        'status_code': data.get('status_code', 200),
        'response_time': data.get('response_time', 0),
        'request_method': data.get('request_method', 'GET'),
        'service_name': data.get('service_name', ''),
        'full_url': data.get('full_url', '')[:2048],
        'content_type': data.get('content_type', ''),
    }
    cols = ', '.join(fields.keys())
    vals = ', '.join('%s' for _ in fields)
    cursor = conn.execute(
        f'INSERT INTO analytics_logs ({cols}) VALUES ({vals}) RETURNING id',
        list(fields.values())
    )
    return cursor.fetchone()['id']


def insert_event(conn, data: dict) -> int:
    """插入一条自定义事件"""
    now = int(time.time())
    fields = {
        'timestamp': data.get('timestamp', now),
        'visitor_hash': data.get('visitor_hash', ''),
        'event_name': data.get('event_name', ''),
        'event_category': data.get('event_category', ''),
        'event_label': data.get('event_label', ''),
        'event_value': data.get('event_value', 0),
        'path': data.get('path', ''),
        'service_name': data.get('service_name', ''),
        'metadata': json.dumps(data.get('metadata', {}), ensure_ascii=False),
    }
    cols = ', '.join(fields.keys())
    vals = ', '.join('%s' for _ in fields)
    cursor = conn.execute(
        f'INSERT INTO analytics_events ({cols}) VALUES ({vals}) RETURNING id',
        list(fields.values())
    )
    return cursor.fetchone()['id']


# ─── 聚合写入 ──────────────────────────────────────────────────────────────────

def upsert_hourly(conn, date_str: str, hour: int, delta: dict, service: str = ''):
    """更新或插入小时聚合（覆盖语义：每次调用全量重算整个小时，避免重复累加导致数据虚高）"""
    conn.execute("""
        INSERT INTO analytics_hourly_stats (date, hour, service_name, pv, uv, ipv,
            new_visitors, bounce_count, total_time, session_count, bot_count,
            error_count, avg_response_time)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(date, hour, service_name) DO UPDATE SET
            pv = %s,
            uv = GREATEST(analytics_hourly_stats.uv, %s),
            ipv = GREATEST(analytics_hourly_stats.ipv, %s),
            new_visitors = %s,
            bounce_count = %s,
            total_time = %s,
            session_count = %s,
            bot_count = %s,
            error_count = %s,
            avg_response_time = %s
    """, (
        date_str, hour, service,
        delta.get('pv', 0), delta.get('uv', 0), delta.get('ipv', 0),
        delta.get('new_visitors', 0), delta.get('bounce_count', 0),
        delta.get('total_time', 0), delta.get('session_count', 0),
        delta.get('bot_count', 0), delta.get('error_count', 0),
        delta.get('avg_response_time', 0),
        # UPDATE part
        delta.get('pv', 0),
        delta.get('uv', 0),
        delta.get('ipv', 0),
        delta.get('new_visitors', 0),
        delta.get('bounce_count', 0),
        delta.get('total_time', 0),
        delta.get('session_count', 0),
        delta.get('bot_count', 0),
        delta.get('error_count', 0),
        delta.get('avg_response_time', 0),
    ))
    conn.commit()


def upsert_daily(conn, date_str: str, stats: dict):
    """更新或插入每日聚合"""
    conn.execute("""
        INSERT INTO analytics_daily_stats (date, pv, uv, ipv, new_visitors,
            returning_visitors, bounce_rate, avg_session_duration, avg_depth,
            bot_pv, error_pv, avg_response_time, total_sessions, last_calculated)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(date) DO UPDATE SET
            pv = %s, uv = %s, ipv = %s, new_visitors = %s, returning_visitors = %s,
            bounce_rate = %s, avg_session_duration = %s, avg_depth = %s,
            bot_pv = %s, error_pv = %s, avg_response_time = %s,
            total_sessions = %s, last_calculated = %s
    """, (
        date_str,
        stats['pv'], stats['uv'], stats['ipv'], stats['new_visitors'],
        stats['returning_visitors'], stats['bounce_rate'],
        stats['avg_session_duration'], stats['avg_depth'],
        stats['bot_pv'], stats['error_pv'], stats['avg_response_time'],
        stats['total_sessions'], int(time.time()),
        # UPDATE
        stats['pv'], stats['uv'], stats['ipv'], stats['new_visitors'],
        stats['returning_visitors'], stats['bounce_rate'],
        stats['avg_session_duration'], stats['avg_depth'],
        stats['bot_pv'], stats['error_pv'], stats['avg_response_time'],
        stats['total_sessions'], int(time.time()),
    ))
    conn.commit()


def upsert_page_stat(conn, date_str: str, path: str, delta: dict):
    """更新或插入页面统计（覆盖语义：全量重算整个日期）"""
    pv = delta.get('pv', 0)
    uv = delta.get('uv', 0)
    entries = delta.get('entries', 0)
    exits = delta.get('exits', 0)
    total_time = delta.get('total_time', 0)
    avg_time_on_page = round(total_time / pv, 1) if pv else 0.0
    exit_rate = round(exits * 1.0 / entries, 4) if entries else 0.0
    conn.execute("""
        INSERT INTO analytics_page_stats (date, path, pv, uv, unique_entries,
            unique_exits, avg_time_on_page, exit_rate, total_time)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(date, path) DO UPDATE SET
            pv = %s, uv = GREATEST(analytics_page_stats.uv, %s),
            unique_entries = %s,
            unique_exits = %s,
            avg_time_on_page = %s,
            exit_rate = %s,
            total_time = %s
    """, (
        date_str, path,
        pv, uv, entries, exits, avg_time_on_page, exit_rate, total_time,
        pv, uv, entries, exits, avg_time_on_page, exit_rate, total_time,
    ))
    conn.commit()


def upsert_source(conn, date_str: str, source_type: str, source_name: str, delta: dict):
    """更新或插入来源统计（覆盖语义：全量重算整个日期）"""
    conn.execute("""
        INSERT INTO analytics_source_stats (date, source_type, source_name, pv, uv)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT(date, source_type, source_name) DO UPDATE SET
            pv = %s, uv = GREATEST(analytics_source_stats.uv, %s)
    """, (
        date_str, source_type, source_name,
        delta.get('pv', 0), delta.get('uv', 0),
        delta.get('pv', 0), delta.get('uv', 0),
    ))
    conn.commit()


def upsert_geo(conn, date_str: str, country: str, city: str, delta: dict):
    """更新或插入地理统计（覆盖语义：全量重算整个日期）"""
    conn.execute("""
        INSERT INTO analytics_geo_stats (date, country, city, pv, uv)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT(date, country, city) DO UPDATE SET
            pv = %s, uv = GREATEST(analytics_geo_stats.uv, %s)
    """, (
        date_str, country, city,
        delta.get('pv', 0), delta.get('uv', 0),
        delta.get('pv', 0), delta.get('uv', 0),
    ))
    conn.commit()


def upsert_device(conn, date_str: str, device_type: str, browser: str, os_name: str, delta: dict):
    """更新或插入设备统计（覆盖语义：全量重算整个日期）"""
    conn.execute("""
        INSERT INTO analytics_device_stats (date, device_type, browser, os_name, pv, uv)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT(date, device_type, browser, os_name) DO UPDATE SET
            pv = %s, uv = GREATEST(analytics_device_stats.uv, %s)
    """, (
        date_str, device_type, browser, os_name,
        delta.get('pv', 0), delta.get('uv', 0),
        delta.get('pv', 0), delta.get('uv', 0),
    ))
    conn.commit()


# ─── 查询接口 ──────────────────────────────────────────────────────────────────

def get_realtime(conn) -> dict:
    """获取实时概览（当前小时）"""
    today = datetime.now().strftime('%Y-%m-%d')
    hour = datetime.now().hour

    # 当前小时
    row = conn.execute("""
        SELECT COALESCE(SUM(pv),0) pv, COALESCE(SUM(uv),0) uv,
               COALESCE(SUM(session_count),0) sessions,
               COALESCE(SUM(bot_count),0) bots,
               COALESCE(SUM(error_count),0) errors,
               COALESCE(SUM(new_visitors),0) new_visitors,
               COALESCE(AVG(avg_response_time),0) avg_response_time
        FROM analytics_hourly_stats
        WHERE date=%s AND hour=%s
    """, (today, hour)).fetchone()

    # 当前在线：最近 5 分钟独立访客
    five_min_ago = int(time.time()) - 300
    online = conn.execute("""
        SELECT COUNT(DISTINCT visitor_hash) cnt FROM analytics_logs
        WHERE timestamp >= %s AND is_bot=0
    """, (five_min_ago,)).fetchone()['cnt']

    # 今日汇总
    daily = conn.execute("""
        SELECT COALESCE(SUM(pv),0) pv, COALESCE(SUM(uv),0) uv,
               COALESCE(SUM(session_count),0) sessions
        FROM analytics_hourly_stats WHERE date=%s
    """, (today,)).fetchone()

    # 今日热门页面
    hot_pages = conn.execute("""
        SELECT path, COUNT(*) cnt FROM analytics_logs
        WHERE to_timestamp(timestamp)::date=%s AND is_bot=0
        GROUP BY path ORDER BY cnt DESC LIMIT 10
    """, (today,)).fetchall()

    return {
        'current_hour': {
            'pv': row['pv'], 'uv': row['uv'], 'sessions': row['sessions'],
            'bots': row['bots'], 'errors': row['errors'],
            'new_visitors': row['new_visitors'],
            'avg_response_time': round(row['avg_response_time'], 1),
        },
        'online': {'count': online, 'time': datetime.now().strftime('%H:%M:%S')},
        'today': {
            'pv': daily['pv'], 'uv': daily['uv'], 'sessions': daily['sessions'],
        },
        'hot_pages': [dict(r) for r in hot_pages],
    }


def get_trend(conn, days: int = 30) -> list:
    """获取流量趋势"""
    rows = conn.execute("""
        SELECT date, pv, uv, total_sessions, bounce_rate, avg_session_duration,
               bot_pv, error_pv
        FROM analytics_daily_stats
        ORDER BY date DESC LIMIT %s
    """, (days,)).fetchall()
    result = []
    for r in reversed(rows):
        result.append({
            'date': r['date'],
            'pv': r['pv'], 'uv': r['uv'],
            'sessions': r['total_sessions'],
            'bounce_rate': round(r['bounce_rate'], 2),
            'avg_duration': round(r['avg_session_duration'], 1),
            'bots': r['bot_pv'],
            'errors': r['error_pv'],
        })
    return result


def get_event_stats(conn, days: int = 30, category: str = '') -> list:
    """获取事件统计"""
    query = """
        SELECT event_name, event_category, COUNT(*) count,
               COALESCE(SUM(event_value), 0) total_value
        FROM analytics_events
        WHERE timestamp >= %s
    """
    params = [int(time.time()) - days * 86400]
    if category:
        query += ' AND event_category = %s'
        params.append(category)
    query += ' GROUP BY event_name, event_category ORDER BY count DESC LIMIT 20'
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_page_rank(conn, days: int = 30, limit: int = 20) -> list:
    """获取页面热度排行"""
    since = int(time.time()) - days * 86400
    rows = conn.execute("""
        SELECT path, COUNT(*) pv, COUNT(DISTINCT visitor_hash) uv,
               ROUND(AVG(response_time), 1) avg_time
        FROM analytics_logs
        WHERE timestamp >= %s AND is_bot=0
        GROUP BY path ORDER BY pv DESC LIMIT %s
    """, (since, limit)).fetchall()
    return [dict(r) for r in rows]


def get_geo_distribution(conn, days: int = 30) -> list:
    """获取地理分布"""
    since = datetime.now() - timedelta(days=days)
    rows = conn.execute("""
        SELECT country, SUM(pv) pv, SUM(uv) uv
        FROM analytics_geo_stats
        WHERE date >= %s
        GROUP BY country ORDER BY pv DESC LIMIT 20
    """, (since.strftime('%Y-%m-%d'),)).fetchall()
    return [dict(r) for r in rows]


def get_city_distribution(conn, days: int = 30, country: str = ''):
    """获取城市分布（可按国家筛选）"""
    since = datetime.now() - timedelta(days=days)
    params = [since.strftime('%Y-%m-%d')]
    where = "WHERE date >= %s"
    if country:
        where += " AND country = %s"
        params.append(country)
    rows = conn.execute(f"""
        SELECT country, city, SUM(pv) pv, SUM(uv) uv
        FROM analytics_geo_stats
        {where} AND city != ''
        GROUP BY country, city ORDER BY pv DESC LIMIT 50
    """, params).fetchall()
    return [dict(r) for r in rows]


# 拼音城市名 → 中文映射（ip2region未覆盖时的回退）
# 注意：键为拼音标识符（不翻译，保证与 API 返回的城市名匹配）；
# 值直接硬编码中文，不调用 _() —— 否则 i18n 表若翻译拼音键会导致"键=值同时被翻译"而映射失效
_PINYIN_CITY_MAP = {
    'Beijing': '北京', 'Shanghai': '上海', 'Guangzhou': '广州', 'Shenzhen': '深圳',
    'Hangzhou': '杭州', 'Nanjing': '南京', 'Chengdu': '成都', 'Wuhan': '武汉',
    'Tianjin': '天津', 'Chongqing': '重庆', "Xi'an": '西安', 'Xian': '西安',
    'Suzhou': '苏州', 'Changsha': '长沙', 'Zhengzhou': '郑州', 'Qingdao': '青岛',
    'Dalian': '大连', 'Xiamen': '厦门', 'Fuzhou': '福州', 'Hefei': '合肥',
    'Jinan': '济南', 'Shenyang': '沈阳', 'Kunming': '昆明', 'Harbin': '哈尔滨',
    'Changchun': '长春', 'Taiyuan': '太原', 'Guiyang': '贵阳', 'Nanning': '南宁',
    'Shijiazhuang': '石家庄', 'Nanchang': '南昌', 'Lanzhou': '兰州', 'Urumqi': '乌鲁木齐',
    'Foshan': '佛山', 'Dongguan': '东莞', 'Wuxi': '无锡', 'Ningbo': '宁波',
    'Wenzhou': '温州', 'Zhuhai': '珠海', 'Haikou': '海口', 'Sanya': '三亚',
}

def _to_chinese_city(name: str) -> str:
    """拼音城市名转中文（查表，找不到原样返回）"""
    return _PINYIN_CITY_MAP.get(name, name)


def get_china_city_distribution(conn, days: int = 30):
    """获取中国城市分布（主视图），自动转拼音为中文"""
    since = datetime.now() - timedelta(days=days)
    rows = conn.execute("""
        SELECT city, SUM(pv) pv, SUM(uv) uv
        FROM analytics_geo_stats
        WHERE date >= %s AND country = 'CN' AND city != ''
        GROUP BY city ORDER BY pv DESC LIMIT 30
    """, (since.strftime('%Y-%m-%d'),)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['city'] = _to_chinese_city(d['city'])
        result.append(d)
    return result


def get_device_distribution(conn, days: int = 30) -> dict:
    """获取设备分布"""
    since = datetime.now() - timedelta(days=days)
    ds = since.strftime('%Y-%m-%d')

    by_device = conn.execute("""
        SELECT device_type, SUM(pv) pv, SUM(uv) uv
        FROM analytics_device_stats
        WHERE date >= %s GROUP BY device_type ORDER BY pv DESC
    """, (ds,)).fetchall()

    by_browser = conn.execute("""
        SELECT browser, SUM(pv) pv, SUM(uv) uv
        FROM analytics_device_stats
        WHERE date >= %s AND browser != '' GROUP BY browser ORDER BY pv DESC LIMIT 10
    """, (ds,)).fetchall()

    by_os = conn.execute("""
        SELECT os_name, SUM(pv) pv, SUM(uv) uv
        FROM analytics_device_stats
        WHERE date >= %s AND os_name != '' GROUP BY os_name ORDER BY pv DESC LIMIT 10
    """, (ds,)).fetchall()

    return {
        'by_device': [dict(r) for r in by_device],
        'by_browser': [dict(r) for r in by_browser],
        'by_os': [dict(r) for r in by_os],
    }


def get_source_analysis(conn, days: int = 30) -> list:
    """获取来源分析"""
    since = datetime.now() - timedelta(days=days)
    rows = conn.execute("""
        SELECT source_type, source_name, SUM(pv) pv, SUM(uv) uv,
               ROUND(SUM(pv) * 100.0 / (SELECT SUM(pv) FROM analytics_source_stats WHERE date >= %s), 1) pct
        FROM analytics_source_stats
        WHERE date >= %s
        GROUP BY source_type, source_name ORDER BY pv DESC LIMIT 15
    """, (since.strftime('%Y-%m-%d'), since.strftime('%Y-%m-%d'))).fetchall()
    return [dict(r) for r in rows]


def get_hourly_breakdown(conn, date_str: str = None) -> list:
    """获取指定日期的小时级数据"""
    if not date_str:
        date_str = datetime.now().strftime('%Y-%m-%d')
    rows = conn.execute("""
        SELECT hour, pv, uv, session_count, bot_count, error_count, avg_response_time
        FROM analytics_hourly_stats
        WHERE date=%s ORDER BY hour
    """, (date_str,)).fetchall()
    result = []
    for r in rows:
        result.append(dict(r))
    return result


def get_event_report(conn, days: int = 7) -> dict:
    """获取事件报告（用于日报/周报）"""
    result = {}
    result['overall'] = get_event_stats(conn, days)
    result['page_rank'] = get_page_rank(conn, days, 10)
    result['sources'] = get_source_analysis(conn, days)
    result['devices'] = get_device_distribution(conn, days)
    result['geo'] = get_geo_distribution(conn, days)
    return result


# ─── 数据维护 ──────────────────────────────────────────────────────────────────

def cleanup_old_logs(conn, retention_days: int = 30):
    """清理过期原始日志"""
    cutoff = int(time.time()) - retention_days * 86400
    cur1 = conn.execute("DELETE FROM analytics_logs WHERE timestamp < %s", (cutoff,))
    cur2 = conn.execute("DELETE FROM analytics_visitor_sessions WHERE end_time < %s", (cutoff,))
    conn.commit()
    return cur1.rowcount + cur2.rowcount


def get_privacy_config(conn, key: str = None) -> dict:
    """获取隐私配置"""
    if key:
        row = conn.execute(
            "SELECT value FROM analytics_privacy_config WHERE key=%s",
            (key,)
        ).fetchone()
        return row['value'] if row else None
    rows = conn.execute("SELECT key, value FROM analytics_privacy_config").fetchall()
    return {r['key']: r['value'] for r in rows}


def update_privacy_config(conn, key: str, value: str):
    """更新隐私配置"""
    conn.execute("""
        INSERT INTO analytics_privacy_config (key, value, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT(key) DO UPDATE SET value=%s, updated_at=%s
    """, (key, value, int(time.time()), value, int(time.time())))
    conn.commit()


# ─── 告警引擎 ──────────────────────────────────────────────────────────────────

def check_alerts(conn) -> list:
    """
    检查所有启用的告警规则
    返回触发的告警列表
    """
    alerts = conn.execute(
        "SELECT * FROM analytics_alerts WHERE enabled=1"
    ).fetchall()
    triggered = []
    for alert in alerts:
        alert = dict(alert)
        try:
            current_value = _get_metric_value(conn, alert['metric'], alert['time_window'])
            if current_value is None:
                continue
            threshold = alert['threshold']
            op = alert['operator']

            # 比较逻辑
            matched = False
            if op == 'gt':
                matched = current_value > threshold
            elif op == 'lt':
                matched = current_value < threshold
            elif op == 'gte':
                matched = current_value >= threshold
            elif op == 'lte':
                matched = current_value <= threshold
            elif op == 'eq':
                matched = abs(current_value - threshold) < 0.001

            if matched:
                triggered.append({
                    'alert_id': alert['id'],
                    'name': alert['name'],
                    'metric': alert['metric'],
                    'current_value': current_value,
                    'threshold': threshold,
                    'operator': op,
                    'time_window': alert['time_window'],
                })
                conn.execute(
                    "UPDATE analytics_alerts SET last_triggered=%s WHERE id=%s",
                    (int(time.time()), alert['id'])
                )
                conn.commit()
        except Exception as e:
            logger.warning('Alert check failed [%s]: %s', alert['name'], e, exc_info=True)
    return triggered


def _get_metric_value(conn, metric: str, time_window: str) -> float:
    """获取告警指标的当前值"""
    seconds = _parse_time_window(time_window)
    since = int(time.time()) - seconds

    if metric == 'uv':
        row = conn.execute(
            "SELECT COUNT(DISTINCT visitor_hash) v FROM analytics_logs WHERE timestamp>=%s AND is_bot=0",
            (since,)
        ).fetchone()
        return row['v']
    elif metric == 'pv':
        row = conn.execute(
            "SELECT COUNT(*) v FROM analytics_logs WHERE timestamp>=%s AND is_bot=0",
            (since,)
        ).fetchone()
        return row['v']
    elif metric == 'error_rate':
        total = conn.execute(
            "SELECT COUNT(*) v FROM analytics_logs WHERE timestamp>=%s",
            (since,)
        ).fetchone()['v']
        if total == 0:
            return 0
        err = conn.execute(
            "SELECT COUNT(*) v FROM analytics_logs WHERE timestamp>=%s AND status_code>=400",
            (since,)
        ).fetchone()['v']
        return round(err * 100.0 / total, 2)
    elif metric == 'bounce_rate':
        sessions = conn.execute(
            "SELECT COUNT(*) v FROM analytics_visitor_sessions WHERE start_time>=%s",
            (since,)
        ).fetchone()['v']
        if sessions == 0:
            return 0
        bounced = conn.execute(
            "SELECT COUNT(*) v FROM analytics_visitor_sessions WHERE start_time>=%s AND page_views<=1",
            (since,)
        ).fetchone()['v']
        return round(bounced * 100.0 / sessions, 2)
    elif metric == 'avg_response_time':
        row = conn.execute(
            "SELECT ROUND(AVG(response_time), 1) v FROM analytics_logs WHERE timestamp>=%s",
            (since,)
        ).fetchone()
        return row['v'] or 0
    return 0


def _parse_time_window(tw: str) -> int:
    """解析时间窗口字符串为秒数"""
    unit = tw[-1]
    num = int(tw[:-1])
    if unit == 'h':
        return num * 3600
    elif unit == 'd':
        return num * 86400
    elif unit == 'm':
        return num * 60
    return 300  # default 5min


# ─── 会话管理 ──────────────────────────────────────────────────────────────────

def track_session(conn, session_hash: str, visitor_hash: str, date_str: str,
                   start_time: int, entry_path: str, referer: str,
                   browser: str, os_name: str, device_type: str,
                   country: str, city: str, is_bot: int, is_new: int):
    """创建新会话"""
    conn.execute("""
        INSERT INTO analytics_visitor_sessions
        (session_hash, visitor_hash, date, start_time, end_time, entry_path, referer,
         browser, os_name, device_type, country, city, is_bot, is_new_visitor)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(session_hash) DO NOTHING
    """, (
        session_hash, visitor_hash, date_str, start_time, start_time,
        entry_path, referer, browser, os_name, device_type,
        country, city, is_bot, is_new
    ))
    conn.commit()


def update_session(conn, session_hash: str, exit_path: str, page_views: int, duration: int):
    """更新会话（追加浏览记录）"""
    conn.execute("""
        UPDATE analytics_visitor_sessions
        SET end_time=%s, page_views=%s, duration=%s, exit_path=%s
        WHERE session_hash=%s
    """, (int(time.time()), page_views, duration, exit_path, session_hash))
    conn.commit()
