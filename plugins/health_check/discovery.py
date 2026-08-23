#!/usr/bin/env python3
"""
Health Check — Resource Discovery Engine
=========================================
Auto-discover project modules, endpoints, database tables, and plugins.

The discovery engine scans the project filesystem and runtime state to detect:
  - Modules:    directories containing app.py / routes.py / models.py
  - Endpoints:  Flask blueprints and their registered routes
  - Tables:     SQLite database schema (all tables, row counts, columns)
  - Plugins:    installed plugins via PluginRegistry

All user-facing strings use English as source for i18n _().

@package health_check
"""

from i18n import _
import os
import sys
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(BASE_DIR, '..'))
sys.path.append(os.path.join(BASE_DIR, '..', 'auth-center'))
sys.path.append(os.path.join(BASE_DIR, '..'))

_t = lambda s: s
def init_i18n(t_func):
    global _t
    _t = t_func


# ────────────────────────────────────────────────────────────────
# Modules to exclude from auto-discovery
# ────────────────────────────────────────────────────────────────
EXCLUDED_DIRS = {
    '__pycache__(', ')__init__(', ').git', '.trae',
    'node_modules', 'venv', '.venv', 'env',
    'tmp', 'tools', 'prompts', 'themes',
    _('Internationalization Plan'),
}

MODULE_INDICATORS = {'app.py', 'routes.py', 'models.py'}


# ════════════════════════════════════════════════════════════════
# Module Discovery
# ════════════════════════════════════════════════════════════════

def scan_modules(project_root: str = PROJECT_ROOT) -> List[dict]:
    """
    Scan project root directory for modules.

    A module is a subdirectory containing at least one of:
      app.py, routes.py, models.py

    Returns list of dicts:
      {
        'name': 'auth-center',
        'path': '/abs/path/to/auth-center',
        'indicators': ['app.py', 'routes.py', 'models.py'],
        'has_app': True,
        'has_routes': True,
        'has_models': True,
      }
    """
    modules = []
    if not os.path.isdir(project_root):
        return modules

    for item in sorted(os.listdir(project_root)):
        item_path = os.path.join(project_root, item)
        if not os.path.isdir(item_path):
            continue
        if item.startswith('.') or item in EXCLUDED_DIRS:
            continue

        found = []
        for indicator in MODULE_INDICATORS:
            if os.path.isfile(os.path.join(item_path, indicator)):
                found.append(indicator)

        if found:
            modules.append({
                'name': item,
                'path': item_path,
                'indicators': found,
                'has_app': 'app.py' in found,
                'has_routes': 'routes.py' in found,
                'has_models': 'models.py' in found,
            })

    return modules


# ════════════════════════════════════════════════════════════════
# Endpoint Discovery
# ════════════════════════════════════════════════════════════════

def scan_endpoints(app) -> List[dict]:
    """
    Scan a Flask application for all registered routes.

    Traverses app.url_map and app.blueprints to extract:
      - URL rule
      - HTTP methods
      - Endpoint name
      - Blueprint name (if any)
      - View function name

    Returns list of dicts:
      {
        'url': '/admin/health/api/status',
        'methods': ['GET', 'POST'],
        'endpoint': 'health.api_status',
        'blueprint': 'health',
        'view_name': 'api_status',
      }

    Returns empty list if app is None or has no url_map.
    """
    if app is None:
        return []

    endpoints = []
    seen = set()

    for rule in app.url_map.iter_rules():
        # Skip static file routes
        if rule.endpoint == 'static' or 'static' in rule.endpoint:
            continue

        # Deduplicate by rule + methods
        key = (rule.rule, tuple(sorted(rule.methods)))
        if key in seen:
            continue
        seen.add(key)

        # Extract blueprint name from endpoint string
        bp_name = None
        view_name = rule.endpoint
        if '.' in rule.endpoint:
            bp_name, view_name = rule.endpoint.split('.', 1)

        # Filter out HEAD and OPTIONS (auto-added by Flask)
        methods = [m for m in rule.methods if m in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH')]

        if methods:
            endpoints.append({
                'url': rule.rule,
                'methods': methods,
                'endpoint': rule.endpoint,
                'blueprint': bp_name,
                'view_name': view_name,
            })

    return sorted(endpoints, key=lambda e: e['url'])


def get_default_app() -> Optional[object]:
    """
    Try to get the default Flask app for endpoint scanning.
    This is best-effort — returns None if no app is available.
    """
    try:
        # Try admin app first (where health_bp is registered)
        from admin import app as admin_app
        return admin_app.app
    except (ImportError, AttributeError):
        pass

    try:
        from flask import current_app
        return current_app._get_current_object()
    except (RuntimeError, AttributeError):
        pass

    return None


# ════════════════════════════════════════════════════════════════
# Database Table Discovery
# ════════════════════════════════════════════════════════════════

def scan_tables(db_path: Optional[str] = None) -> List[dict]:
    """
    Scan a PostgreSQL database for all tables and their metadata.

    For each table, extracts:
      - name
      - column names and types
      - row count

    Returns list of dicts:
      {
        'name': 'users',
        'columns': [{'name': 'id', 'type': 'bigint'}, ...],
        'row_count': 100,
        'auto_increment_id': None,
      }
    """
    import psycopg2

    tables = []
    try:
        conn = get_raw_connection()
        cursor = conn.cursor()
        cursor.execute("CREATE SCHEMA IF NOT EXISTS health")
        cursor.execute("SET search_path TO health")

        # Get all user tables
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY table_name"
        )
        all_tables = [r[0] for r in cursor.fetchall()]

        for tname in all_tables:
            # Columns
            cursor.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = %s ORDER BY ordinal_position",
                (tname,)
            )
            columns = [{'name': r[0], 'type': r[1]} for r in cursor.fetchall()]

            # Row count
            try:
                cursor.execute(f'SELECT COUNT(*) FROM "{tname}"')
                row_count = cursor.fetchone()[0]
            except psycopg2.Error:
                conn.rollback()
                row_count = -1

            tables.append({
                'name': tname,
                'columns': columns,
                'column_count': len(columns),
                'row_count': row_count,
                'auto_increment_id': None,
            })

        conn.close()
    except Exception as e:
        return [{'error': str(e)}]

    return tables


# ════════════════════════════════════════════════════════════════
# Plugin Discovery
# ════════════════════════════════════════════════════════════════

def scan_plugins() -> List[dict]:
    """
    Scan the plugins directory for installed plugins.

    Uses PluginRegistry.discover() to find available plugins,
    then reads metadata and status for each.

    Returns list of dicts:
      {
        'name': 'my_plugin',
        'version': '1.0.0',
        'description': 'Does something',
        'author': 'developer',
        'status': 'enabled' | 'disabled' | 'not_loaded',
        'has_health_checks': True,
        'health_check_count': 3,
      }
    """
    try:
        from plugin_manager.discovery import PluginDiscovery

        discovery = PluginDiscovery(plugins_dir=os.path.join(PROJECT_ROOT, 'plugins'))
        discovered = discovery.discover()

        results = []
        for plugin_info in discovered:
            meta = getattr(plugin_info, 'metadata', None) or getattr(plugin_info, 'descriptor', None) or {}
            results.append({
                'name': plugin_info.identifier,
                'version': meta.get('version', '0.1.0'),
                'description': meta.get('description', ''),
                'author': meta.get('author', ''),
                'status': 'discovered',
                'has_health_checks': False,
                'health_check_count': 0,
                'depends_on': meta.get('depends_on', []),
                'enabled': meta.get('enabled', True),
            })

        return results

    except ImportError:
        return [{'error': _t('Plugin system not available')}]
    except Exception as e:
        return [{'error': str(e)}]


# ════════════════════════════════════════════════════════════════
# Discovery Reporter (aggregates all scans)
# ════════════════════════════════════════════════════════════════

class DiscoveryReporter:
    """
    Aggregate discovery results from all scanners.

    Usage:
        reporter = DiscoveryReporter()
        report = reporter.run(flask_app=app)
        print(json.dumps(report, indent=2))
    """

    def __init__(self):
        self.start_time = None

    def run(self, flask_app=None) -> dict:
        """Run all discovery scans and return aggregated report."""
        self.start_time = time.time()

        modules = scan_modules()
        endpoints = scan_endpoints(flask_app) if flask_app else []
        tables = scan_tables()
        plugins = scan_plugins()

        elapsed = int((time.time() - self.start_time) * 1000)

        return {
            'discovered_at': datetime.now().isoformat(),
            'elapsed_ms': elapsed,
            'summary': {
                'modules': len(modules),
                'endpoints': len(endpoints),
                'tables': len(tables),
                'plugins': len(plugins),
            },
            'modules': modules,
            'endpoints': endpoints,
            'tables': tables,
            'plugins': plugins,
        }

    def summary_text(self, result: dict) -> str:
        """
        Generate a human-readable summary of discovery results.
        Uses English as source language for i18n.
        """
        s = result.get('summary', {})
        parts = []
        parts.append(_t('{n} modules discovered').format(n=s.get('modules', 0)))
        parts.append(_t('{n} endpoints discovered').format(n=s.get('endpoints', 0)))
        parts.append(_t('{n} database tables discovered').format(n=s.get('tables', 0)))
        parts.append(_t('{n} plugins discovered').format(n=s.get('plugins', 0)))
        return ' | '.join(parts)


# ════════════════════════════════════════════════════════════════
# Health Checkers using the Discovery Engine
# ════════════════════════════════════════════════════════════════

# These checkers are registered via @register decorator so they
# automatically appear in the health_checks registry for the admin UI.
#
# When a discovery checker runs, it scans the current state and reports
# changes from previous runs (newly added or removed resources).

from .checkers import BaseHealthCheck, CheckResult, register
from plugins._base.db import get_raw_connection


@register('discovery_modules')
class ModuleDiscoveryCheck(BaseHealthCheck):
    """Check for newly added or removed modules compared to last run."""
    check_key = 'discovery_modules'
    name = 'Module Discovery'
    category = 'system'
    severity = 'info'
    description = 'Auto-discover project modules and detect changes'
    sort_order = 5
    config_defaults = {'data_dir': os.path.join(BASE_DIR, 'data')}
    config_schema = {
        'type': 'object',
        'properties': {
            'data_dir': {'type': 'string', 'description': 'Directory to store discovery state'},
        }
    }

    def _state_path(self) -> str:
        data_dir = self.config.get('data_dir', os.path.join(BASE_DIR, 'data'))
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, 'discovery_modules.json')

    def _load_prev(self) -> set:
        path = self._state_path()
        if os.path.isfile(path):
            try:
                with open(path, 'r') as f:
                    return set(json.load(f))
            except (json.JSONDecodeError, IOError):
                pass
        return set()

    def _save_prev(self, names: set):
        path = self._state_path()
        try:
            with open(path, 'w') as f:
                json.dump(sorted(names), f)
        except IOError:
            pass

    def check(self) -> CheckResult:
        start = time.time()
        modules = scan_modules()
        current_names = {m['name'] for m in modules}
        prev_names = self._load_prev()

        new_modules = current_names - prev_names
        removed_modules = prev_names - current_names

        self._save_prev(current_names)

        elapsed = int((time.time() - start) * 1000)
        detail = {
            'total': len(modules),
            'modules': [{'name': m['name'], 'indicators': m['indicators']} for m in modules],
            'new_since_last_check': sorted(new_modules),
            'removed_since_last_check': sorted(removed_modules),
        }

        if new_modules:
            return CheckResult(
                'warning', elapsed,
                _t('{n} new module(s) detected: {names}').format(
                    n=len(new_modules), names=', '.join(sorted(new_modules))),
                detail
            )
        return CheckResult(
            'passed', elapsed,
            _t('{n} modules stable').format(n=len(modules)),
            detail
        )


@register('discovery_endpoints')
class EndpointDiscoveryCheck(BaseHealthCheck):
    """Discover and track Flask endpoint changes."""
    check_key = 'discovery_endpoints'
    name = 'Endpoint Discovery'
    category = 'system'
    severity = 'info'
    description = 'Discover Flask endpoints and detect route changes'
    sort_order = 6
    config_defaults = {}

    def check(self) -> CheckResult:
        start = time.time()
        app = get_default_app()
        if app is None:
            return CheckResult('passed', 0, _t('Endpoint discovery: health service runs independently, routes not scannable'))

        endpoints = scan_endpoints(app)
        elapsed = int((time.time() - start) * 1000)

        # Group by blueprint
        by_blueprint = {}
        for ep in endpoints:
            bp = ep['blueprint'] or '(root)'
            if bp not in by_blueprint:
                by_blueprint[bp] = []
            by_blueprint[bp].append({
                'url': ep['url'],
                'methods': ep['methods'],
            })

        detail = {
            'total': len(endpoints),
            'by_blueprint': {bp: {'count': len(eps), 'routes': eps}
                             for bp, eps in sorted(by_blueprint.items())},
        }

        return CheckResult(
            'passed', elapsed,
            _t('{n} endpoints across {b} blueprints').format(
                n=len(endpoints), b=len(by_blueprint)),
            detail
        )


@register('discovery_tables')
class TableDiscoveryCheck(BaseHealthCheck):
    """Discover and monitor database tables."""
    check_key = 'discovery_tables'
    name = 'Database Table Discovery'
    category = 'database'
    severity = 'info'
    description = 'Auto-discover database tables, row counts, and column changes'
    sort_order = 7
    config_defaults = {'data_dir': os.path.join(BASE_DIR, 'data')}

    def _state_path(self) -> str:
        data_dir = self.config.get('data_dir', os.path.join(BASE_DIR, 'data'))
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, 'discovery_tables.json')

    def _load_prev(self) -> dict:
        path = self._state_path()
        if os.path.isfile(path):
            try:
                with open(path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save_prev(self, data: dict):
        path = self._state_path()
        try:
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
        except IOError:
            pass

    def check(self) -> CheckResult:
        start = time.time()
        tables = scan_tables()
        elapsed = int((time.time() - start) * 1000)

        if not tables:
            return CheckResult('error', elapsed, _t('No database tables found or DB unavailable'))

        if 'error' in tables[0]:
            return CheckResult('warning', elapsed,
                               _t('Database scan error: {err}').format(err=tables[0]['error']),
                               tables[0])

        # Check for new/removed tables vs previous state
        prev_state = self._load_prev()
        current_map = {t['name']: t['row_count'] for t in tables}
        prev_names = set(prev_state.keys())
        current_names = set(current_map.keys())

        new_tables = current_names - prev_names
        removed_tables = prev_names - current_names
        changed_tables = {
            name for name in (current_names & prev_names)
            if current_map[name] != prev_state.get(name)
        }

        # Save current state
        self._save_prev(current_map)

        # Build summary
        total_rows = sum(t['row_count'] for t in tables if t['row_count'] >= 0)
        column_counts = {t['name']: t['column_count'] for t in tables}

        detail = {
            'total_tables': len(tables),
            'total_rows': total_rows,
            'tables': [{
                'name': t['name'],
                'columns': t['column_count'],
                'rows': t['row_count'],
                'auto_increment_id': t.get('auto_increment_id'),
            } for t in tables],
            'new_since_last_check': sorted(new_tables),
            'removed_since_last_check': sorted(removed_tables),
            'row_count_changed': sorted(changed_tables),
        }

        warnings = []
        if new_tables:
            warnings.append(_t('{n} new table(s): {names}').format(
                n=len(new_tables), names=', '.join(sorted(new_tables))))
        if removed_tables:
            warnings.append(_t('{n} table(s) removed').format(n=len(removed_tables)))

        status = 'passed'
        message = _t('{n} tables, {r} total rows').format(n=len(tables), r=total_rows)

        if warnings:
            status = 'warning'
            message += ' | ' + '; '.join(warnings)

        return CheckResult(status, elapsed, message, detail)


@register('discovery_plugins')
class PluginDiscoveryCheck(BaseHealthCheck):
    """Discover and monitor installed plugins."""
    check_key = 'discovery_plugins'
    name = 'Plugin Discovery'
    category = 'system'
    severity = 'info'
    description = 'Auto-discover plugins and their health check registration status'
    sort_order = 8
    config_defaults = {}

    def check(self) -> CheckResult:
        start = time.time()
        plugins = scan_plugins()
        elapsed = int((time.time() - start) * 1000)

        if not plugins:
            return CheckResult('passed', elapsed, _t('No plugins discovered'))

        if 'error' in plugins[0]:
            return CheckResult('warning', elapsed,
                               _t('Plugin scan: {msg}').format(msg=plugins[0].get('error', '')))

        total = len(plugins)
        enabled = sum(1 for p in plugins if p.get('status') == 'enabled')
        with_health = sum(1 for p in plugins if p.get('has_health_checks'))
        errors = [p['name'] for p in plugins if p.get('status') in ('load_error', 'error')]

        detail = {
            'total': total,
            'enabled': enabled,
            'disabled': total - enabled,
            'with_health_checks': with_health,
            'plugins': plugins,
        }

        status = 'passed'
        message = _t('{n} plugins ({e} enabled, {h} with health checks)').format(
            n=total, e=enabled, h=with_health)

        if errors:
            status = 'warning'
            message += _t(' | {n} plugin(s) have errors: {names}').format(
                n=len(errors), names=', '.join(errors))

        return CheckResult(status, elapsed, message, detail)
