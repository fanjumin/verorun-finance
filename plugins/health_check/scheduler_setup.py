#!/usr/bin/env python3
"""
Health Check — Scheduled Task Setup
====================================
Register automated health check schedules into the orchestrator cron_jobs table.

This module seeds the cron_jobs DB table with predefined schedules that call
the health check internal API. It relies on the existing orchestrator SchedulerEngine
for execution, not creating a separate scheduler.

Usage (called from admin/app.py on startup):
    from health_check.scheduler_setup import seed_health_schedules
    seed_health_schedules()

All user-facing strings use English as source for i18n _().

@package health_check
"""

import os
import sys
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(BASE_DIR, '..', 'auth-center'))
sys.path.append(os.path.join(BASE_DIR, '..'))

_t = lambda s: s
def init_i18n(t_func):
    global _t
    _t = t_func

try:
    from plugin_manager.logger import get_plugin_logger
    _logger = get_plugin_logger('health_check')
except ImportError:
    import logging
    _logger = logging.getLogger('health_check')


# ─── Health Check Schedule Definitions ─────────────────────────

# Each schedule defines a cron job that calls the health check internal API.
# The internal API uses X-Health-Secret header for auth.
# 审计 D5：HEALTH_SECRET 未配置时为空（fail-closed），不再使用源码公开默认值
HEALTH_SECRET = os.environ.get('HEALTH_SECRET', '')
HEALTH_INTERNAL_URL = os.environ.get(
    'HEALTH_INTERNAL_URL',
    'http://127.0.0.1:8085/admin/health/api/internal/run'
)

SCHEDULES = [
    {
        'name': _t('Health Check — Quick Scan'),
        'description': _t('Quick health scan every 5 minutes: system-level checks only'),
        'frequency': 'interval',
        'interval_seconds': 300,
        'checks': ['core_api', 'database', 'redis', 'server_resources', 'veroguard'],
        'trigger_info': 'cron:quick',
    },
    {
        'name': _t('Health Check — Standard Scan'),
        'description': _t('Standard health scan every 30 minutes: system + external + workflow'),
        'frequency': 'interval',
        'interval_seconds': 1800,
        'checks': [
            'core_api', 'database', 'redis', 'server_resources',
            'external_apis', 'workflow_engine', 'error_logs',
            'discovery_modules', 'discovery_endpoints',
            'discovery_tables', 'discovery_plugins',
            'agent_matrix', 'content_factory', 'ssl_cert',
            'internal_links', 'ai_gateway', 'plugin_store',
        ],
        'trigger_info': 'cron:standard',
    },
    {
        'name': _t('Health Check — Full Scan'),
        'description': _t('Full health scan every 6 hours: all checks + deep database scan'),
        'frequency': 'interval',
        'interval_seconds': 21600,
        'checks': [],  # All active checks
        'trigger_info': 'cron:full',
    },
    {
        'name': _t('Health Check — Deep DB Scan'),
        'description': _t('Deep database table integrity scan daily at 03:00'),
        'frequency': 'cron',
        'cron_expr': '0 0 3 * * *',
        'checks': ['discovery_tables', 'database', 'media_integrity'],
        'trigger_info': 'cron:deep_db',
    },
]


def seed_health_schedules():
    """
    Seed health check cron schedules into the orchestrator cron_jobs table.
    Idempotent: checks by name before inserting.
    """
    try:
        from orchestrator.models import get_db as orch_db
    except ImportError:
        _logger.warning('Orchestrator module not available, skipping schedule seeding')
        return

    registered = 0
    skipped = 0
    migrated = 0

    for schedule in SCHEDULES:
        name = schedule['name']

        # Build the API call config as target JSON
        target_config = json.dumps({
            'url': HEALTH_INTERNAL_URL,
            'method': 'POST',
            'headers': {
                'Content-Type': 'application/json',
                'X-Health-Secret': HEALTH_SECRET,
            },
            'body': {
                'trigger_type': 'scheduled',
                'trigger_info': schedule['trigger_info'],
                'checks': schedule['checks'],
            },
        }, ensure_ascii=False)

        try:
            with orch_db() as conn:
                # Check if already exists by name
                # orchestrator 的 get_db() 返回裸 cursor，其 execute() 返回 None，
                # 必须先 execute 再 fetchone，不能链式调用。
                conn.execute(
                    'SELECT id, target_config FROM cron_jobs WHERE name=%s', (name,)
                )
                existing = conn.fetchone()

                if existing:
                    # 幂等迁移：老库的定时巡检可能仍指向 8084（admin 进程）。
                    # 统一迁移到 8085（health service 独立进程），保证 admin 宕机时巡检仍可运行。
                    try:
                        old_cfg = json.loads(existing['target_config'] or '{}')
                        old_url = old_cfg.get('url') or ''
                    except (json.JSONDecodeError, TypeError):
                        old_url = ''
                    if old_url and old_url != HEALTH_INTERNAL_URL:
                        conn.execute(
                            'UPDATE cron_jobs SET target_config=%s, updated_at=NOW() '
                            'WHERE id=%s',
                            (target_config, existing['id'])
                        )
                        migrated += 1
                        _logger.info(
                            'Migrated schedule "%s" target URL → %s',
                            name, HEALTH_INTERNAL_URL
                        )
                    else:
                        skipped += 1
                    continue

                # Determine job type and trigger expression
                if schedule['frequency'] == 'interval':
                    job_type = 'interval'
                    interval_sec = schedule['interval_seconds']
                    cron_expr = ''
                else:
                    job_type = 'cron'
                    interval_sec = 0
                    cron_expr = schedule.get('cron_expr', '0 0 * * * *')

                conn.execute("""
                    INSERT INTO cron_jobs
                        (name, description, job_type, cron_expr, natural_expr,
                         interval_seconds, is_active, target_type, target_config,
                         priority, max_retries, retry_delay, max_runs)
                    VALUES (%s,%s,%s,%s,%s,%s,1,'api',%s,%s,3,10,0)
                """, (
                    name,
                    schedule['description'],
                    job_type,
                    cron_expr,
                    '',  # natural_expr not used
                    interval_sec,
                    target_config,
                    'normal',
                ))
                registered += 1

        except Exception as e:
            _logger.error('Failed to register schedule "%s": %s', name, str(e))

    if registered > 0:
        _logger.info('Registered %d new health check schedules', registered)
    if migrated > 0:
        _logger.info('Migrated %d existing schedules to %s', migrated, HEALTH_INTERNAL_URL)
    if skipped > 0:
        _logger.info('%d schedules already exist (skipped)', skipped)
