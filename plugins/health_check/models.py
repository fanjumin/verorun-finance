#!/usr/bin/env python3
"""
Health Check — Database Models
============================
All health check related database tables. Uses PostgreSQL health schema.

Table structure:
  health_checks     — Check item definitions (registered checks, configuration, enable/disable)
  check_history     — Detailed results of each check item per inspection run
  check_runs        — Batch information for each inspection run
  alert_config      — Alert rule configuration
  alert_history     — Sent alert records
  health_trend      — Daily aggregated statistics (for trend charts)

@package health_monitor
"""

import json, time, psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from contextlib import contextmanager
from collections import defaultdict
from plugins._base.db import PgConnection
from plugins._base.db import get_raw_connection

try:
    from plugin_manager.logger import get_plugin_logger
    _logger = get_plugin_logger('health_check')
except ImportError:
    import logging
    _logger = logging.getLogger('health_check')


@contextmanager
def get_db():
    conn = get_raw_connection()
    conn.autocommit = False
    try:
        wrapped = PgConnection(conn)
        wrapped.execute("CREATE SCHEMA IF NOT EXISTS health")
        wrapped.execute("SET search_path TO health")
        yield wrapped
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_health_tables():
    """Initialize all health check tables (idempotent: IF NOT EXISTS)"""
    with get_db() as conn:
        conn.execute("""
            -- =============================================
            -- 1. Check item definition table
            -- =============================================
            CREATE TABLE IF NOT EXISTS health_checks (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL,                    -- Check item name (e.g. "Core API Check")
                check_key       TEXT NOT NULL UNIQUE,             -- Unique key (e.g. "core_api")
                category        TEXT NOT NULL DEFAULT 'system',   -- Category: system/external/workflow/agent/cms/ssl/error
                description     TEXT DEFAULT '',                  -- Description
                config          TEXT DEFAULT '{}',                -- JSON config (timeout, URLs, etc.)
                is_active       BIGINT DEFAULT 1,                -- Whether enabled
                severity        TEXT DEFAULT 'warning'            -- Severity level: info/warning/critical
                    CHECK(severity IN ('info','warning','critical')),
                sort_order      BIGINT DEFAULT 0,                -- Sort order
                created_at      TEXT DEFAULT NOW(),
                updated_at      TEXT DEFAULT NOW()
            );

            -- =============================================
            -- 2. Check run table (each triggered inspection is one batch)
            -- =============================================
            CREATE TABLE IF NOT EXISTS check_runs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                trigger_type    TEXT NOT NULL DEFAULT 'manual',   -- manual/scheduled/workflow
                trigger_info    TEXT DEFAULT '',                  -- Trigger details (e.g. cron job id)
                total_checks    BIGINT DEFAULT 0,                -- Total check items
                passed          BIGINT DEFAULT 0,                -- Passed count
                warnings        BIGINT DEFAULT 0,                -- Warning count
                errors          BIGINT DEFAULT 0,                -- Error count
                duration_ms     BIGINT DEFAULT 0,                -- Total duration (ms)
                status          TEXT DEFAULT 'completed'          -- completed/running/failed
                    CHECK(status IN ('running','completed','failed')),
                summary         TEXT DEFAULT '',                  -- Run summary
                created_at      TEXT DEFAULT NOW()
            );

            -- =============================================
            -- 3. Check history/details table
            -- =============================================
            CREATE TABLE IF NOT EXISTS check_history (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                run_id          BIGINT NOT NULL,                 -- References check_runs.id
                check_id        BIGINT NOT NULL,                 -- References health_checks.id
                check_key       TEXT NOT NULL,                    -- Redundant, for convenient querying
                check_name      TEXT NOT NULL,                    -- Redundant
                category        TEXT NOT NULL,                    -- Redundant
                status          TEXT NOT NULL DEFAULT 'passed'    -- passed/warning/error
                    CHECK(status IN ('passed','warning','error')),
                response_time_ms BIGINT DEFAULT 0,               -- Response time (ms)
                message         TEXT DEFAULT '',                  -- Result message
                detail          TEXT DEFAULT '{}',                -- JSON details
                checked_at      TEXT DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_check_history_run
                ON check_history(run_id);
            CREATE INDEX IF NOT EXISTS idx_check_history_key
                ON check_history(check_key);
            CREATE INDEX IF NOT EXISTS idx_check_history_time
                ON check_history(checked_at);

            -- =============================================
            -- 4. Alert rule configuration table
            -- =============================================
            CREATE TABLE IF NOT EXISTS alert_config (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL,                    -- Rule name
                check_key       TEXT DEFAULT '*',                 -- Associated check item ('*' means all)
                severity        TEXT DEFAULT 'warning',           -- Trigger severity: warning/critical
                consecutive     BIGINT DEFAULT 1,                -- Alert after N consecutive failures
                notify_method   TEXT DEFAULT 'email',             -- email/internal message/webhook/all
                webhook_url     TEXT DEFAULT '',                  -- Webhook URL
                alert_level     TEXT DEFAULT 'P3',                -- P0(critical)/P1(major)/P2(minor)/P3(info)
                aggregation_window BIGINT DEFAULT 300,          -- Aggregation window in seconds (0=instant)
                cooldown_minutes   BIGINT DEFAULT 60,           -- Min minutes between same-check alerts
                is_active       BIGINT DEFAULT 1,
                created_at      TEXT DEFAULT NOW(),
                updated_at      TEXT DEFAULT NOW()
            );

            -- =============================================
            -- 5. Alert history table
            -- =============================================
            CREATE TABLE IF NOT EXISTS alert_history (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                alert_config_id BIGINT DEFAULT 0,
                check_key       TEXT NOT NULL,
                check_name      TEXT NOT NULL,
                run_id          BIGINT DEFAULT 0,
                status          TEXT NOT NULL,                    -- Status at trigger time
                alert_level     TEXT DEFAULT 'P3',                -- P0(critical)/P1(major)/P2(minor)/P3(info)
                message         TEXT DEFAULT '',
                notify_method   TEXT DEFAULT '',
                is_read         BIGINT DEFAULT 0,
                created_at      TEXT DEFAULT NOW()
            );

            -- =============================================
            -- 6a. Alert silences table
            -- =============================================
            CREATE TABLE IF NOT EXISTS alert_silences (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                check_key       TEXT DEFAULT '*',                 -- '*' means all checks
                starts_at       TEXT NOT NULL,                    -- Silence start time (ISO format)
                ends_at         TEXT NOT NULL,                    -- Silence end time (ISO format)
                reason          TEXT DEFAULT '',                  -- Reason for silence
                created_by      TEXT DEFAULT 'system',            -- Who created the silence
                created_at      TEXT DEFAULT NOW()
            );

            -- =============================================
            -- 6. Daily health trend table
            -- =============================================
            CREATE TABLE IF NOT EXISTS health_trend (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                date            TEXT NOT NULL,                    -- '2026-05-10'
                total_checks    BIGINT DEFAULT 0,
                passed          BIGINT DEFAULT 0,
                warnings        BIGINT DEFAULT 0,
                errors          BIGINT DEFAULT 0,
                avg_response_ms BIGINT DEFAULT 0,
                health_score    REAL DEFAULT 100.0,              -- Health score 0-100
                created_at      TEXT DEFAULT NOW()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_health_trend_date
                ON health_trend(date);

            -- =============================================
            -- 7. Fix audit log table (for rollback)
            -- =============================================
            CREATE TABLE IF NOT EXISTS fix_audit_log (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                run_id          BIGINT DEFAULT 0,
                check_key       TEXT NOT NULL,
                action          TEXT NOT NULL,                    -- FIX_ACTION_* constant
                params_json     TEXT DEFAULT '{}',               -- Parameters used
                undo_params_json TEXT DEFAULT '{}',              -- Parameters needed to undo
                status          TEXT DEFAULT 'applied'           -- applied / rolled_back
                    CHECK(status IN ('applied','rolled_back')),
                admin_user      TEXT DEFAULT '',
                created_at      TEXT DEFAULT NOW()
            );
        """)
        _logger.info('Database tables initialized')

    # Idempotent migration: add missing columns from schema updates
    migrate_alert_schema()


def migrate_alert_schema():
    """Idempotent migration: add new alert columns to existing tables if missing."""
    migrations = [
        ("alert_config", "alert_level", "TEXT DEFAULT 'P3'"),
        ("alert_config", "aggregation_window", "INTEGER DEFAULT 300"),
        ("alert_config", "cooldown_minutes", "INTEGER DEFAULT 60"),
        ("alert_history", "alert_level", "TEXT DEFAULT 'P3'"),
    ]
    with get_db() as conn:
        for table, column, col_def in migrations:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
                _logger.info('Migrated: %s.%s', table, column)
            except Exception:
                pass  # Column already exists
        conn.commit()


# ─── Seed data: register default check items ───────────────────────────────────────────────

DEFAULT_CHECKS = [
    # (check_key, name, category, description, config, severity, sort)
    ('core_api',       'Core API Check',        'system',    'Health endpoint check for all subsites (Site/Platform/Admin/Health)', '{"timeout":5,"endpoints":["/health"]}', 'warning', 10),
    # ── VeroGuard ──
    ('veroguard',      'VeroGuard Guardian',    'system',    'VeroGuard daemon running status, self-protect, and heartbeat health',
     '{"guardian_status_url":"http://127.0.0.1:8085/api/guardian/status","timeout":5}', 'critical', 15),
    ('database',       'Database Connection',   'system',    'PostgreSQL connection status, table count, and schema size',  '{"timeout":3}',                                    'critical', 20),
    ('redis',          'Redis Cache',           'system',    'Redis cache service connection status', '{"timeout":3}',                                    'warning', 25),
    ('server_resources','Server Resources',     'system',    'CPU/Memory/Disk usage monitoring',     '{"cpu_threshold":90,"mem_threshold":85,"disk_threshold":85,"timeout":10}', 'warning', 30),
    ('external_apis',  'External Dependencies', 'external',  'Stock quotes/AI API/Payment dependencies', '{"timeout":10,"endpoints":[]}', 'warning', 40),
    ('ssl_cert',       'SSL Certificate',       'ssl',       'SSL certificate expiry check for all subdomains', '{"domains":[],"expire_warn_days":30}', 'warning', 50),
    ('workflow_engine','Workflow Engine',       'workflow',  'Cron/Workflow scheduler running status', '{"timeout":5}',                                   'warning', 60),
    ('agent_matrix',   'Agent Matrix',          'agent',     'Primary agent + sub-agent online status','{"timeout":10}',                                   'warning', 70),
    ('content_factory','Content Factory',       'cms',       'Collection channels / processing queue status', '{"timeout":5}',                                    'warning', 80),
    ('media_integrity','Media Integrity',       'cms',       'Scan media files/avatars referenced in DB and verify disk existence',
     '{"dry_run":true,"max_fixes_per_run":20}', 'warning', 85),
    ('sse_ws',         'SSE/WebSocket',         'system',    'SSE push / WebSocket connection status','{"timeout":5}',                                    'warning', 95),
    ('error_logs',     'Error Logs',            'error',     'Error log count in the last 24 hours',  '{"hours":24,"threshold":50}',                      'warning', 100),
    # ── Discovery checkers ──
    ('discovery_modules',   'Module Discovery',           'system',   'Auto-discover project modules and detect changes',               '{}', 'info', 5),
    ('discovery_endpoints', 'Endpoint Discovery',         'system',   'Discover Flask endpoints and detect route changes',               '{}', 'info', 6),
    ('discovery_tables',    'Database Table Discovery',   'database', 'Auto-discover database tables, row counts, and column changes',    '{}', 'info', 7),
    ('discovery_plugins',   'Plugin Discovery',           'system',   'Auto-discover plugins and their health check registration status', '{}', 'info', 8),
    # ── Internal Link Check ──
    ('internal_links',  'Internal Link Check',  'cms',  'Scan all internal links for broken, redirected, or problematic URLs',
     '{"max_urls":50,"timeout":5,"check_redirects":true}', 'warning', 42),
    # ── P1: AI & Plugin Store ──
    ('ai_gateway',     'AI Gateway',            'system',    'AI budget gate status, token usage, and provider model availability',
     '{"timeout":10}', 'warning', 32),
    ('plugin_store',   'Plugin Store & License','external',  'Plugin store connectivity and license service availability',
     '{"timeout":10}', 'warning', 45),
]


def seed_default_checks():
    """Initialize default check items (only when the table is empty)"""
    with get_db() as conn:
        count = conn.execute('SELECT COUNT(*) as c FROM health_checks').fetchone()['c']
        if count > 0:
            # 幂等迁移：历史安装的 external_apis 默认端点指向 httpbin.org
            # （不可靠第三方依赖，与系统端点无关）。仅当 config 仍为旧默认值时替换为空。
            try:
                conn.execute(
                    "UPDATE health_checks SET config=%s "
                    "WHERE check_key='external_apis' AND config LIKE %s",
                    ('{"timeout":10,"endpoints":[]}', '%httpbin%')
                )
                conn.commit()
            except Exception as e:
                _logger.warning('Failed to migrate external_apis default config: %s', e)
            return
        for ck, name, cat, desc, cfg, sev, sort in DEFAULT_CHECKS:
            conn.execute(
                'INSERT INTO health_checks (check_key, name, category, description, config, severity, sort_order) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (ck, name, cat, desc, cfg, sev, sort)
            )
        conn.commit()
        _logger.info('Registered %d default check items', len(DEFAULT_CHECKS))


# ─── Query helpers ──────────────────────────────────────────────────────────────

def get_active_checks():
    """Retrieve all enabled check items"""
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM health_checks WHERE is_active=1 ORDER BY sort_order'
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_runs(limit=20):
    """Retrieve recent inspection batches"""
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM check_runs ORDER BY created_at DESC LIMIT %s',
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_history_for_run(run_id):
    """Retrieve detailed results for a specific inspection run"""
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM check_history WHERE run_id=%s ORDER BY id',
            (run_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_status():
    """Retrieve status statistics from the most recent completed inspection run"""
    with get_db() as conn:
        run = conn.execute(
            "SELECT * FROM check_runs WHERE status='completed' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not run:
            return None
        run = dict(run)
        history = conn.execute(
            'SELECT * FROM check_history WHERE run_id=%s ORDER BY category, id',
            (run['id'],)
        ).fetchall()
        run['items'] = [dict(h) for h in history]
        return run


def get_health_trend(days=7):
    """Retrieve health trend data for the last N days"""
    with get_db() as conn:
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        rows = conn.execute(
            'SELECT * FROM health_trend WHERE date>=%s ORDER BY date',
            (since,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_alerts(limit=50):
    """Retrieve alert history"""
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM alert_history ORDER BY created_at DESC LIMIT %s',
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_unread_alert_count():
    with get_db() as conn:
        r = conn.execute(
            "SELECT COUNT(*) as c FROM alert_history WHERE is_read=0"
        ).fetchone()
    return r['c'] if r else 0
