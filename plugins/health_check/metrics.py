#!/usr/bin/env python3
"""
Health Check — Prometheus Metrics Endpoint
===========================================
Exposes Prometheus-compatible text metrics at /admin/health/api/metrics.

No third-party dependencies required — generates Prometheus exposition format
directly. Designed to be scraped by Prometheus or compatible aggregators.

Metrics exposed:
  app_health_status         — Latest check result (1=pass, 2=warning, 3=error) per check_key
  app_health_score          — Current health score 0-100
  app_health_passed_total   — Passed count (latest run)
  app_health_warnings_total — Warning count (latest run)
  app_health_errors_total   — Error count (latest run)
  app_health_response_ms    — Response time per check_key (latest run)
  app_system_cpu_usage      — CPU usage %
  app_system_memory_usage   — Memory usage %
  app_system_disk_usage     — Disk usage %
  app_db_size_bytes         — Database estimated size (bytes)
  app_db_table_count        — Number of tables in health schema
"""

from i18n import _
import os, sys, time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)

from .models import get_db


def _get_system_metrics():
    """Collect CPU / memory / disk usage. Returns dict or empty on failure."""
    metrics = {}

    # CPU via /proc/stat (Linux only)
    try:
        with open('/proc/stat') as f:
            parts = list(map(int, f.readline().split()[1:]))
        total, idle = sum(parts), parts[3]
        time.sleep(0.05)
        with open('/proc/stat') as f:
            parts2 = list(map(int, f.readline().split()[1:]))
        total2, idle2 = sum(parts2), parts2[3]
        diff = total2 - total
        metrics['cpu_usage'] = round(100 - (idle2 - idle) * 100 / diff, 1) if diff > 0 else 0
    except Exception:
        metrics['cpu_usage'] = -1

    # Memory via /proc/meminfo (Linux only)
    try:
        with open('/proc/meminfo') as f:
            mem = {}
            for line in f:
                p = line.split(':')
                if len(p) == 2:
                    try:
                        mem[p[0].strip()] = int(p[1].strip().replace(' kB', ''))
                    except ValueError:
                        pass
        mt = mem.get('MemTotal', 0)
        ma = mem.get('MemAvailable', 0)
        metrics['mem_usage'] = round(100 - ma * 100 / mt, 1) if mt > 0 else 0
    except Exception:
        metrics['mem_usage'] = -1

    # Disk via os.statvfs (cross-platform)
    try:
        s = os.statvfs('/')
        metrics['disk_usage'] = round(100 - s.f_bfree * 100 / s.f_blocks, 1)
    except Exception:
        metrics['disk_usage'] = -1

    return metrics


def _get_db_metrics():
    """Collect database-level metrics (PostgreSQL health schema)."""
    metrics = {}
    try:
        with get_db() as conn:
            r = conn.execute(
                "SELECT COUNT(*) as c FROM pg_catalog.pg_tables WHERE schemaname='health'"
            ).fetchone()
            metrics['db_table_count'] = r['c'] if r else 0
            # Estimate DB size via relation sizes
            r2 = conn.execute(
                "SELECT COALESCE(SUM(pg_total_relation_size(quote_ident(schemaname)||'.'||quote_ident(tablename))),0) as sz "
                "FROM pg_catalog.pg_tables WHERE schemaname='health'"
            ).fetchone()
            metrics['db_size_bytes'] = r2['sz'] if r2 else 0
    except Exception:
        metrics['db_size_bytes'] = 0
        metrics['db_table_count'] = 0

    return metrics


def _get_health_metrics():
    """Collect health check result metrics from latest run."""
    metrics = {}

    try:
        with get_db() as conn:
            run = conn.execute(
                "SELECT * FROM check_runs WHERE status='completed' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if not run:
                return metrics

            run = dict(run)
            metrics['_score'] = run.get('passed', 0) + run.get('warnings', 0) + run.get('errors', 0)
            if metrics['_score'] > 0:
                metrics['health_score'] = round(
                    (run.get('passed', 0) + run.get('warnings', 0) * 0.5) * 100 / metrics['_score'], 1
                )
            else:
                metrics['health_score'] = 100.0

            items = conn.execute(
                'SELECT check_key, check_name, status, response_time_ms FROM check_history WHERE run_id=?',
                (run['id'],)
            ).fetchall()

            for item in items:
                key = item['check_key'].replace('-', '_(').replace(').', '_(')
                status_val = {')passed': 1, 'warning': 2, 'error': 3}.get(item['status'], 0)
                metrics[f'status_{key}'] = status_val
                metrics[f'resp_ms_{key}'] = item['response_time_ms'] or 0
    except Exception:
        pass

    return metrics


def generate_metrics():
    """Generate Prometheus text format metrics string."""
    lines = []

    # ── System metrics ──
    sys_m = _get_system_metrics()
    lines.append(f'# HELP app_system_cpu_usage CPU usage percentage')
    lines.append(f'# TYPE app_system_cpu_usage gauge')
    lines.append(f'app_system_cpu_usage {sys_m.get("cpu_usage", -1)}')

    lines.append(f'# HELP app_system_memory_usage Memory usage percentage')
    lines.append(f'# TYPE app_system_memory_usage gauge')
    lines.append(f'app_system_memory_usage {sys_m.get("mem_usage", -1)}')

    lines.append(f'# HELP app_system_disk_usage Disk usage percentage')
    lines.append(f'# TYPE app_system_disk_usage gauge')
    lines.append(f'app_system_disk_usage {sys_m.get("disk_usage", -1)}')

    # ── Database metrics ──
    db_m = _get_db_metrics()
    lines.append(f'# HELP app_db_size_bytes PostgreSQL health schema size in bytes')
    lines.append(f'# TYPE app_db_size_bytes gauge')
    lines.append(f'app_db_size_bytes {db_m.get("db_size_bytes", 0)}')

    lines.append(f'# HELP app_db_table_count Number of tables in database')
    lines.append(f'# TYPE app_db_table_count gauge')
    lines.append(f'app_db_table_count {db_m.get("db_table_count", 0)}')

    # ── Health metrics ──
    h_m = _get_health_metrics()

    lines.append(f'# HELP app_health_score Current health score 0-100')
    lines.append(f'# TYPE app_health_score gauge')
    lines.append(f'app_health_score {h_m.get("health_score", 0)}')

    for k, v in sorted(h_m.items()):
        if k.startswith('status_'):
            check_key = k[7:]
            lines.append(f'# HELP app_health_status Status of {check_key} (1=pass,2=warning,3=error)')
            lines.append(f'# TYPE app_health_status gauge')
            lines.append(f'app_health_status{{check_key="{check_key}"}} {v}')
        elif k.startswith('resp_ms_'):
            check_key = k[8:]
            lines.append(f'# HELP app_health_response_ms Response time of {check_key} in ms')
            lines.append(f'# TYPE app_health_response_ms gauge')
            lines.append(f'app_health_response_ms{{check_key="{check_key}"}} {v}')

    # ── Meta ──
    import time as _time
    lines.append(f'# HELP app_metrics_scrape_duration_seconds Time to generate this metrics page')
    lines.append(f'# TYPE app_metrics_scrape_duration_seconds gauge')
    lines.append(f'# EOF')

    return '\n'.join(lines) + '\n'
