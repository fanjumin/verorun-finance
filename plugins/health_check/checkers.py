#!/usr/bin/env python3
"""
Health Check — Checkers
=========================
All checkers inherit from BaseHealthCheck and auto-register via the @register
decorator.

Adding a new checker takes 3 steps:
  1. Inherit BaseHealthCheck in this file (or create a new checker_xxx.py)
  2. Decorate with @register
  3. Add a record to the health_checks table (ORM / API)

See DEVELOPER.md for detailed tutorial.
"""

import os, sys, json, re, time, socket, ssl, subprocess
from datetime import datetime, timedelta
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Type, Tuple
from services.deployment_config import deploy
_t = lambda s: s
def init_i18n(t_func):
    global _t
    _t = t_func

import urllib.request
import urllib.error
import ipaddress
import re

# Add project path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(BASE_DIR, '..', 'auth-center'))
sys.path.append(os.path.join(BASE_DIR, '..'))


# ═══════════════════════════════════════════════════════════════════════════
# Network Safety — Private IP Detection (§11.3)
# ═══════════════════════════════════════════════════════════════════════════

_PRIVATE_RANGES = [
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('::1/128'),
    ipaddress.ip_network('fc00::/7'),
]


def _is_private_host(host: str) -> bool:
    """Check if a hostname or IP resolves to a private/internal address.
    
    Returns True if the host should be blocked (private IP range).
    Returns False if the host is a public address or cannot be resolved.
    """
    import socket as _socket
    # Strip brackets from IPv6 addresses
    host = host.strip('[]')
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _PRIVATE_RANGES)
    except ValueError:
        pass  # Not a literal IP — could be a hostname
    # Resolve hostname and check all resolved IPs
    try:
        ips = _socket.getaddrinfo(host, None)
        for info in ips:
            ip_str = info[4][0]
            try:
                addr = ipaddress.ip_address(ip_str)
                if any(addr in net for net in _PRIVATE_RANGES):
                    return True
            except ValueError:
                continue
    except _socket.gaierror:
        return False  # Can't resolve — don't block (may be public hostname)
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Auto-Fix Safety — LLM 建议的修复动作执行前必须通过白名单校验
# ═══════════════════════════════════════════════════════════════════════════

_SQL_BLOCKED_KEYWORDS = re.compile(
    r'\b(DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|COPY|VACUUM|REINDEX|'
    r'COMMENT|ATTACH|DETACH)\b',
    re.IGNORECASE,
)


def _is_safe_run_sql(sql: str) -> bool:
    """run_sql 白名单校验：仅允许 SELECT / UPDATE / DELETE。

    - 拒绝 DROP/ALTER/TRUNCATE/CREATE/GRANT 等破坏性关键字
    - UPDATE/DELETE 强制携带 WHERE，防止全表误操作
    """
    if not sql or not isinstance(sql, str):
        return False
    if _SQL_BLOCKED_KEYWORDS.search(sql):
        return False
    head = sql.lstrip().upper()
    if head.startswith('SELECT'):
        return True
    if head.startswith(('UPDATE', 'DELETE')):
        return bool(re.search(r'\bWHERE\b', sql, re.IGNORECASE))
    return False


def _is_safe_http_url(url: str) -> bool:
    """SSRF 防护：仅允许 http/https，且目标主机非内网/环回/链路本地地址。

    复用 §11.3 的 _is_private_host 检查，防止 LLM 提供的 URL 指向内网资源。
    """
    from urllib.parse import urlparse
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return False
    return not _is_private_host(parsed.hostname)


def _extract_host_from_url(url: str) -> str:
    """Extract hostname from a URL string."""
    m = re.match(r'https?://([^/:]+)', url)
    return m.group(1) if m else ''


# ═══════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════

class CheckerRegistry:
    """
    Global checker registry.
    Register via @register(check_key) decorator or directly call register().
    """

    _checkers: Dict[str, Type['BaseHealthCheck']] = {}

    @classmethod
    def register(cls, check_key: str, checker_cls: Type['BaseHealthCheck'] = None):
        """
        Register a checker — can be used as decorator or direct call.

        Usage:
            @register('my_check')
            class MyCheck(BaseHealthCheck): ...
        """
        if checker_cls is not None:
            cls._checkers[check_key] = checker_cls
            return checker_cls

        def decorator(klass):
            cls._checkers[check_key] = klass
            # Set check_key on class if not explicitly defined
            if not hasattr(klass, 'check_key') or not klass.check_key:
                klass.check_key = check_key
            return klass
        return decorator

    @classmethod
    def get(cls, check_key: str) -> Optional[Type['BaseHealthCheck']]:
        """Get checker class by key."""
        return cls._checkers.get(check_key)

    @classmethod
    def get_instance(cls, check_key: str, config: dict = None) -> Optional['BaseHealthCheck']:
        """Get checker instance."""
        klass = cls.get(check_key)
        if not klass:
            return None
        return klass(config or {})

    @classmethod
    def list_registered(cls) -> List[Dict]:
        """List metadata for all registered checkers (used by admin UI)."""
        result = []
        for key, klass in sorted(cls._checkers.items()):
            # Instantiate to get metadata (may depend on config, use empty dict)
            try:
                inst = klass({})
                meta = {
                    'check_key': key,
                    'name': inst.get_name(),
                    'category': inst.get_category(),
                    'severity': inst.get_severity(),
                    'description': inst.get_description(),
                    'default_sort_order': inst.get_sort_order(),
                    'config_schema': inst.get_config_schema(),
                    'config_defaults': inst.get_config_defaults(),
                }
            except Exception as e:
                meta = {
                    'check_key': key,
                    'name': getattr(klass, 'check_key', key),
                    'error': str(e),
                }
            result.append(meta)
        return result

    @classmethod
    def unregister(cls, check_key: str):
        """Unregister a checker."""
        cls._checkers.pop(check_key, None)

    @classmethod
    def size(cls) -> int:
        return len(cls._checkers)


# Convenience alias
register = CheckerRegistry.register


# ═══════════════════════════════════════════════════════════════════════════
# Check Result
# ═══════════════════════════════════════════════════════════════════════════

class CheckResult:
    """Result of a single check."""
    def __init__(self, status: str = 'passed', response_time_ms: int = 0,
                 message: str = '', detail: dict = None):
        assert status in ('passed', 'warning', 'error'), f"Invalid status: {status}"
        self.status = status
        self.response_time_ms = response_time_ms
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {
            'status': self.status,
            'response_time_ms': self.response_time_ms,
            'message': self.message,
            'detail': json.dumps(self.detail, ensure_ascii=False),
        }

    def to_emoji(self) -> str:
        return '✅' if self.status == 'passed' else ('⚠️' if self.status == 'warning' else '❌')


# ═══════════════════════════════════════════════════════════════════════════
# Abstract Base HealthCheck
# ═══════════════════════════════════════════════════════════════════════════

class BaseHealthCheck(ABC):
    """
    Abstract base class for health checkers.
    All custom checkers must inherit from this class and implement check().

    Class attributes (overridable):
        check_key: str              — Unique key (defaults to registry key)
        name: str                   — Display name
        category: str               — Category (system/external/workflow/agent/cms/ssl/error)
        severity: str               — Severity (info/warning/critical)
        description: str            — Description
        sort_order: int             — Sort weight
        config_schema: dict         — JSON Schema for config
        config_defaults: dict       — Default config values

    Subclasses must implement:
        check(self) -> CheckResult  — Run the check
    """

    # ── Metadata (override in subclasses) ──
    check_key: str = ''
    name: str = 'Unnamed Check'
    category: str = 'system'
    severity: str = 'warning'
    description: str = ''
    sort_order: int = 50
    config_schema: dict = {}
    config_defaults: dict = {}

    def __init__(self, config: dict):
        """Initialize checker; config is read from health_checks.config JSON."""
        self.config = {**self.config_defaults, **config}

    # ── Metadata accessors (overridable) ──

    def get_name(self) -> str:
        return _t(self.name)

    def get_category(self) -> str:
        return self.category

    def get_severity(self) -> str:
        return self.severity

    def get_description(self) -> str:
        return _t(self.description)

    def get_sort_order(self) -> int:
        return self.sort_order

    def get_config_schema(self) -> dict:
        """Return JSON Schema for config (used by admin page config editor)."""
        return self.config_schema

    def get_config_defaults(self) -> dict:
        return self.config_defaults

    # ── Check entry point ──

    @abstractmethod
    def check(self) -> CheckResult:
        """
        The sole entry point for running a check.
        Subclasses must implement this and return a CheckResult.
        """
        pass

    # ── Legacy compatibility (called by routes.py) ──
    def run(self) -> CheckResult:
        """Legacy compatibility: delegates to check() by default."""
        return self.check()

    # ── Utility methods ──

    def _http_get(self, url: str, timeout: int = 5) -> Tuple[int, int, str]:
        """HTTP GET request, returns (status_code, elapsed_ms, body)."""
        start = time.time()
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                elapsed = int((time.time() - start) * 1000)
                body = resp.read().decode('utf-8', errors='replace')[:500]
                return resp.status, elapsed, body
        except urllib.error.HTTPError as e:
            elapsed = int((time.time() - start) * 1000)
            return e.code, elapsed, str(e)
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            return 0, elapsed, str(e)


# ═══════════════════════════════════════════════════════════════════════════
# Concrete Checker Implementations
# ═══════════════════════════════════════════════════════════════════════════
#
# Each checker class below inherits BaseHealthCheck and is registered
# with @register. They are built-in system checkers.
#
# To add a new checker, scroll to the bottom and use the template.
# ═══════════════════════════════════════════════════════════════════════════

# ─── 1. Core API Check ──────────────────────────────────────────────────

@register('core_api')
class CoreAPIHealthCheck(BaseHealthCheck):
    check_key = 'core_api'
    name = 'Core API Check'
    category = 'system'
    severity = 'warning'
    description = 'Health endpoint check for all subsites (Site/Platform/Admin)'
    sort_order = 10
    config_defaults = {'timeout': 5, 'endpoints': ['/health']}
    config_schema = {
        'type': 'object',
        'properties': {
            'timeout': {'type': 'integer', 'default': 5, 'description': 'Timeout (seconds)'},
        }
    }

    def check(self) -> CheckResult:
        endpoints = self.config.get('endpoints', ['/health'])
        subdomains = {
            'Main Site': ('http://127.0.0.1:8081', 8081),
            deploy.server_name('platform'): ('http://127.0.0.1:8083', 8083),
            f'{deploy.server_name("agent")} (admin)': ('http://127.0.0.1:8084', 8084),
            'Health Service': ('http://127.0.0.1:8085', 8085),
        }
        results = {}
        all_ok = True
        max_time = 0

        for domain, (base, port) in subdomains.items():
            url = f'{base}{endpoints[0]}'
            code, elapsed, body = self._http_get(url, self.config.get('timeout', 5))
            max_time = max(max_time, elapsed)
            ok = code == 200
            results[domain] = {'code': code, 'ms': elapsed, 'ok': ok}
            if not ok:
                all_ok = False

        # 就绪探针：health-service /ready（DB 连通性）。
        # /health 存活探针在 DB 异常时仍返回 200，而 /ready 会返回 503。
        rcode, relapsed, _ = self._http_get(
            'http://127.0.0.1:8085/ready', self.config.get('timeout', 5)
        )
        max_time = max(max_time, relapsed)
        ready_ok = rcode == 200
        results['Health Service (readiness /ready)'] = {
            'code': rcode, 'ms': relapsed, 'ok': ready_ok
        }
        if not ready_ok:
            all_ok = False

        detail = {'endpoints': results}
        total = len(results)
        if all_ok:
            return CheckResult('passed', max_time, f'All {total} subsite endpoints OK', detail)
        failed = [k for k, v in results.items() if not v['ok']]
        status = 'warning' if len(failed) <= 2 else 'error'
        return CheckResult(status, max_time,
                           f'{len(failed)}/{total} subsite endpoints abnormal: {", ".join(failed)}', detail)


# ─── 2. Database Connection Check ───────────────────────────────────────

@register('database')
class DatabaseHealthCheck(BaseHealthCheck):
    check_key = 'database'
    name = 'Database Connection'
    category = 'system'
    severity = 'critical'
    description = 'PostgreSQL database connection status, table count, and schema size'
    sort_order = 20
    config_defaults = {'timeout': 3}
    config_schema = {
        'type': 'object',
        'properties': {
            'timeout': {'type': 'integer', 'default': 3, 'description': 'Timeout (seconds)'},
        }
    }

    def check(self) -> CheckResult:
        from models import get_db as main_db
        start = time.time()
        try:
            with main_db() as conn:
                conn.execute('SELECT 1')
                elapsed = int((time.time() - start) * 1000)
                tables = conn.execute(
                    "SELECT COUNT(*) as c FROM pg_catalog.pg_tables WHERE schemaname='public'"
                ).fetchone()['c']
                # Get PostgreSQL database size
                db_size = conn.execute(
                    "SELECT pg_database_size(current_database()) as sz"
                ).fetchone()['sz']
                # ── 连接数指标：总量 + 按应用分组 ─────────────────────────
                max_conn = int(conn.execute('SHOW max_connections').fetchone()['max_connections'])
                conn_row = conn.execute(
                    "SELECT COUNT(*) AS total FROM pg_stat_activity"
                ).fetchone()
                total_conn = conn_row['total']
                groups = conn.execute(
                    "SELECT COALESCE(application_name, '(none)') AS app, COUNT(*) AS cnt "
                    "FROM pg_stat_activity GROUP BY 1 ORDER BY 2 DESC"
                ).fetchall()
                group_detail = {g['app']: g['cnt'] for g in groups}
                app_breakdown = ', '.join(f"{k}:{v}" for k, v in group_detail.items() if v > 0)
            size_str = f'{db_size/1024/1024:.1f}MB' if db_size > 1024*1024 else f'{db_size/1024:.0f}KB'
            # 连接数超 max_connections 的 80% → warning（连接压力预警）
            warn_threshold = int(max_conn * 0.8)
            if total_conn > warn_threshold:
                msg = (f'High DB connections: {total_conn}/{max_conn} '
                       f'({tables} tables, {size_str}) [{app_breakdown}]')
                return CheckResult('warning', elapsed, msg, {
                    'tables': tables,
                    'db_size_bytes': db_size,
                    'type': 'PostgreSQL',
                    'connections': total_conn,
                    'max_connections': max_conn,
                    'connection_groups': group_detail,
                })
            return CheckResult('passed', elapsed,
                               f'Database OK ({tables} tables, {size_str}, '
                               f'{total_conn}/{max_conn} conns)',
                               {'tables': tables, 'db_size_bytes': db_size, 'type': 'PostgreSQL',
                                'connections': total_conn, 'max_connections': max_conn,
                                'connection_groups': group_detail})
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            return CheckResult('error', elapsed, f'Database connection failed: {e}', {'error': str(e)})


# ─── 3. Redis Cache Check ───────────────────────────────────────────────

@register('redis')
class RedisHealthCheck(BaseHealthCheck):
    check_key = 'redis'
    name = 'Redis Cache'
    category = 'system'
    severity = 'warning'
    description = 'Redis cache service connection status'
    sort_order = 25
    config_defaults = {'host': '127.0.0.1', 'port': 6379, 'timeout': 3}
    config_schema = {
        'type': 'object',
        'properties': {
            'host': {'type': 'string', 'default': '127.0.0.1', 'description': 'Redis host'},
            'port': {'type': 'integer', 'default': 6379, 'description': 'Port'},
            'timeout': {'type': 'integer', 'default': 3, 'description': 'Timeout (seconds)'},
        }
    }

    def check(self) -> CheckResult:
        try:
            import redis as redis_client
        except ImportError:
            return CheckResult('warning', 0, 'Redis client not available (pip install redis)')

        host = self.config.get('host', '127.0.0.1')
        port = self.config.get('port', 6379)
        start = time.time()
        try:
            r = redis_client.Redis(host=host, port=port, socket_timeout=3)
            r.ping()
            elapsed = int((time.time() - start) * 1000)
            # Extra: check connection pool info
            info = r.info()
            pool_info = {
                'connected_clients': info.get('connected_clients', 'N/A'),
                'used_memory_human': info.get('used_memory_human', 'N/A'),
                'uptime_in_days': info.get('uptime_in_days', 'N/A'),
            }
            return CheckResult('passed', elapsed,
                               f'Redis OK ({host}:{port}, connections:{pool_info["connected_clients"]})',
                               pool_info)
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            return CheckResult('warning', elapsed, f'Redis check failed: {e}')


# ─── 4. Server Resources Check ─────────────────────────────────────────

@register('server_resources')
class ServerHealthCheck(BaseHealthCheck):
    check_key = 'server_resources'
    name = 'Server Resources'
    category = 'system'
    severity = 'warning'
    description = 'CPU / Memory / Disk usage monitoring'
    sort_order = 30
    config_defaults = {'cpu_threshold': 90, 'mem_threshold': 85, 'disk_threshold': 85}
    config_schema = {
        'type': 'object',
        'properties': {
            'cpu_threshold': {'type': 'integer', 'default': 90, 'description': 'CPU alert threshold (%)'},
            'mem_threshold': {'type': 'integer', 'default': 85, 'description': 'Memory alert threshold (%)'},
            'disk_threshold': {'type': 'integer', 'default': 85, 'description': 'Disk alert threshold (%)'},
        }
    }

    def check(self) -> CheckResult:
        start = time.time()
        detail = {}
        warnings = []

        # CPU
        try:
            with open('/proc/stat') as f:
                parts = list(map(int, f.readline().split()[1:]))
            total, idle = sum(parts), parts[3]
            time.sleep(0.1)
            with open('/proc/stat') as f:
                parts2 = list(map(int, f.readline().split()[1:]))
            total2, idle2 = sum(parts2), parts2[3]
            diff_total = total2 - total
            cpu_usage = round(100 - (idle2 - idle) * 100 / diff_total, 1) if diff_total > 0 else 0
            detail['cpu_usage_pct'] = cpu_usage
            if cpu_usage > self.config.get('cpu_threshold', 90):
                warnings.append(f'CPU {cpu_usage}% > threshold')
        except Exception as e:
            cpu_usage = -1
            detail['cpu_error'] = str(e)

        # Memory
        try:
            with open('/proc/meminfo') as f:
                mem = {}
                for line in f:
                    p = line.split(':')
                    if len(p) == 2:
                        try: mem[p[0].strip()] = int(p[1].strip().replace(' kB', ''))
                        except: pass
            mt, ma = mem.get('MemTotal', 0), mem.get('MemAvailable', 0)
            mem_usage = round(100 - ma * 100 / mt, 1) if mt > 0 else 0
            detail['mem_usage_pct'] = mem_usage
            if mem_usage > self.config.get('mem_threshold', 85):
                warnings.append(f'Memory {mem_usage}% > threshold')
        except Exception as e:
            mem_usage = -1
            detail['mem_error'] = str(e)

        # Disk
        try:
            s = os.statvfs('/')
            du = round(100 - s.f_bfree * 100 / s.f_blocks, 1)
            detail['disk_usage_pct'] = du
            if du > self.config.get('disk_threshold', 85):
                warnings.append(f'Disk {du}% > threshold')
        except Exception as e:
            du = -1
            detail['disk_error'] = str(e)

        elapsed = int((time.time() - start) * 1000)
        detail['elapsed_ms'] = elapsed
        if not warnings:
            return CheckResult('passed', elapsed,
                               f'CPU {cpu_usage}% | Memory {mem_usage}% | Disk {du}%', detail)
        return CheckResult('warning', elapsed, '; '.join(warnings), detail)


# ─── 5. SSL Certificate Check ──────────────────────────────────────────

@register('ssl_cert')
class SSLHealthCheck(BaseHealthCheck):
    check_key = 'ssl_cert'
    name = 'SSL Certificate'
    category = 'ssl'
    severity = 'warning'
    description = 'SSL certificate expiry check for all subdomains'
    sort_order = 50
    config_defaults = {
        'domains': [deploy.server_name(),
                     deploy.server_name('platform'), deploy.server_name('agent')],
        'expire_warn_days': 30,
    }
    config_schema = {
        'type': 'object',
        'properties': {
            'domains': {'type': 'array', 'items': {'type': 'string'},
                        'description': 'Domains to check'},
            'expire_warn_days': {'type': 'integer', 'default': 30,
                                 'description': 'Days before expiry to start warning'},
        }
    }

    def check(self) -> CheckResult:
        domains = self.config.get('domains', [])
        warn_days = self.config.get('expire_warn_days', 30)
        results = {}
        all_ok = True
        any_warning = False
        max_time = 0

        for domain in domains:
            start = time.time()
            try:
                ctx = ssl.create_default_context()
                with socket.create_connection((domain, 443), timeout=10) as sock:
                    with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                        cert = ssock.getpeercert()
                        elapsed = int((time.time() - start) * 1000)
                        expire = datetime.strptime(cert['notAfter'], '%b %d %H:%M:%S %Y %Z')
                        days = (expire - datetime.now()).days
                        results[domain] = {'days_left': days, 'expire': cert['notAfter']}
                        if days <= 0:
                            all_ok = False
                            results[domain]['status'] = 'expired'
                        elif days <= warn_days:
                            any_warning = True
                            results[domain]['status'] = 'expiring_soon'
                        else:
                            results[domain]['status'] = 'ok'
                        max_time = max(max_time, elapsed)
            except Exception as e:
                results[domain] = {'status': 'error', 'error': str(e)[:50]}

        elapsed = int((time.time() - start) * 1000) if 'start' in dir() else 0
        ok_count = sum(1 for r in results.values() if r.get('status') == 'ok')
        detail = {'domains': results}

        if all_ok and not any_warning:
            return CheckResult('passed', max_time,
                               f'{ok_count}/{len(domains)} SSL certificates valid', detail)
        elif all_ok:
            expiring = [d for d, r in results.items() if r.get('status') == 'expiring_soon']
            return CheckResult('warning', max_time,
                               f'{len(expiring)} domain(s) expiring soon', detail)
        else:
            return CheckResult('error', max_time, 'Some SSL certificates abnormal', detail)


# ─── 6. External Dependencies Check ────────────────────────────────────

@register('external_apis')
class ExternalAPIHealthCheck(BaseHealthCheck):
    check_key = 'external_apis'
    name = 'External Dependencies Check'
    category = 'external'
    severity = 'warning'
    description = 'Stock/AI/Payment dependencies'
    sort_order = 40
    config_defaults = {'timeout': 10, 'endpoints': []}
    config_schema = {
        'type': 'object',
        'properties': {
            'endpoints': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'External API URLs to check'
            },
            'timeout': {'type': 'integer', 'default': 10, 'description': 'Timeout (seconds)'},
        }
    }

    def check(self) -> CheckResult:
        start = time.time()
        endpoints = self.config.get('endpoints', [])
        if not endpoints:
            return CheckResult('passed', 0, 'No external API endpoints configured (can configure in admin)')

        results = {}
        max_time = 0
        timeout = self.config.get('timeout', 10)

        for url in endpoints:
            host = _extract_host_from_url(url)
            if host and _is_private_host(host):
                results[host] = {'code': 0, 'ms': 0, 'status': 'blocked', 'reason': 'private_ip'}
                continue
            code, elapsed, body = self._http_get(url, timeout)
            max_time = max(max_time, elapsed)
            ok = (code == 200)
            results[host or url] = {'code': code, 'ms': elapsed, 'status': 'ok' if ok else 'fail'}

        elapsed = int((time.time() - start) * 1000)
        failed = [f'{k}({v["code"]})' for k, v in results.items() if v['status'] not in ('ok', 'blocked')]
        blocked = [k for k, v in results.items() if v.get('reason') == 'private_ip']
        if not failed and not blocked:
            return CheckResult('passed', max_time,
                               f'All {len(endpoints)} external APIs OK')
        msg_parts = []
        if failed:
            msg_parts.append(f'{len(failed)}/{len(endpoints)} abnormal: {", ".join(failed)}')
        if blocked:
            msg_parts.append(f'{len(blocked)} blocked (private IP)')
        return CheckResult('warning', max_time,
                           '; '.join(msg_parts),
                           {'endpoints': results})


# ─── 7. Workflow Engine Check ─────────────────────────────────────────

@register('workflow_engine')
class WorkflowHealthCheck(BaseHealthCheck):
    check_key = 'workflow_engine'
    name = 'Workflow Engine'
    category = 'workflow'
    severity = 'warning'
    description = 'Cron / Workflow scheduler running status and recent execution records'
    sort_order = 60
    config_defaults = {
        'timeout': 5,
        # orchestrator /health（无需认证）：报告 APScheduler 与 worker pool 进程级活性
        'automation_url': 'http://127.0.0.1:8084/admin/automation/health',
    }

    def check(self) -> CheckResult:
        start = time.time()
        try:
            from orchestrator import models as om
        except ImportError:
            return CheckResult('warning', 0, 'Orchestrator module not available')

        try:
            with om.get_db() as conn:
                try: cron_total = conn.execute('SELECT COUNT(*) as c FROM cron_jobs').fetchone()['c']
                except: cron_total = 0
                try: cron_active = conn.execute('SELECT COUNT(*) as c FROM cron_jobs WHERE is_active=1').fetchone()['c']
                except: cron_active = 0
                try: wf_total = conn.execute('SELECT COUNT(*) as c FROM workflow_definitions').fetchone()['c']
                except: wf_total = 0
                try: recent_failed = conn.execute(
                    "SELECT COUNT(*) as c FROM workflow_instances "
                    "WHERE status='failed' AND created_at>=NOW() - INTERVAL '1 day'"
                ).fetchone()['c']
                except: recent_failed = 0

            # 真实活性探测：DB 正常但调度进程崩溃时，仅查表无法发现。
            # 通过 orchestrator /health 获取 APScheduler / worker pool 运行状态。
            automation_url = self.config.get(
                'automation_url', 'http://127.0.0.1:8084/admin/automation/health'
            )
            sched_running = None
            worker_active = None
            code, _, body = self._http_get(automation_url, self.config.get('timeout', 5))
            if code == 200:
                try:
                    proc = json.loads(body)
                    sched_running = bool(proc.get('scheduler_running'))
                    worker_active = bool(proc.get('worker_pool_active'))
                except (json.JSONDecodeError, TypeError):
                    sched_running = None

            elapsed = int((time.time() - start) * 1000)
            detail = {'cron_total': cron_total, 'cron_active': cron_active,
                      'workflows': wf_total, 'recent_failures_24h': recent_failed,
                      'scheduler_running': sched_running, 'worker_pool_active': worker_active}
            warnings = []
            if recent_failed > 5:
                warnings.append(f'{recent_failed} workflow failures in 24h')
            if sched_running is False:
                warnings.append('APScheduler is not running')
            if worker_active is False:
                warnings.append('Worker pool is not active')
            if sched_running is None:
                warnings.append('Orchestrator /health endpoint unreachable')
            status = 'passed' if not warnings else 'warning'
            msg = f'{cron_active}/{cron_total} Cron active | {wf_total} workflows'
            if warnings:
                msg += ' | ' + '; '.join(warnings)
            return CheckResult(status, elapsed, msg, detail)
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            return CheckResult('warning', elapsed, f'Check failed: {e}')


# ─── 8. Agent Matrix Check ────────────────────────────────────────────

@register('agent_matrix')
class AgentMatrixHealthCheck(BaseHealthCheck):
    check_key = 'agent_matrix'
    name = 'Agent Matrix'
    category = 'agent'
    severity = 'warning'
    description = 'Agent matrix (main config + system agents + user agents + running tasks) status'
    sort_order = 70
    config_defaults = {'timeout': 10}

    def check(self) -> CheckResult:
        start = time.time()
        try:
            from agent_matrix.models import get_db as am_get_db
        except ImportError:
            return CheckResult('warning', 0, 'Agent Matrix module not available')

        try:
            with am_get_db() as conn:
                tables = [t['tablename'] for t in conn.execute(
                    "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'"
                ).fetchall()]
                detail = {'tables_found': [t for t in tables if 'agent' in t.lower()]}

                am_count = conn.execute('SELECT COUNT(*) as c FROM agent_matrix').fetchone()['c'] if 'agent_matrix' in tables else 0
                sa_count = conn.execute('SELECT COUNT(*) as c FROM system_agents WHERE is_active=1').fetchone()['c'] if 'system_agents' in tables else 0
                ap_count = conn.execute('SELECT COUNT(*) as c FROM agent_profiles').fetchone()['c'] if 'agent_profiles' in tables else 0
                task_count = conn.execute("SELECT COUNT(*) as c FROM agent_tasks WHERE status='running'").fetchone()['c'] if 'agent_tasks' in tables else 0

                detail.update({'agent_matrix': am_count, 'system_agents_active': sa_count,
                               'agent_profiles': ap_count, 'running_tasks': task_count})

            elapsed = int((time.time() - start) * 1000)
            return CheckResult('passed', elapsed,
                               f'{am_count} matrix configs | {sa_count} system agents | {ap_count} user agents | {task_count} running',
                               detail)
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            return CheckResult('warning', elapsed, f'Check failed: {e}')


# ─── 9. Content Factory Check ─────────────────────────────────────────

@register('content_factory')
class ContentFactoryHealthCheck(BaseHealthCheck):
    check_key = 'content_factory'
    name = 'Content Factory'
    category = 'cms'
    severity = 'warning'
    description = 'Content factory collection channel status, processing queue, pending review'
    sort_order = 80
    config_defaults = {'timeout': 5}

    def check(self) -> CheckResult:
        start = time.time()
        try:
            from models import get_db as main_db
            with main_db() as conn:
                tables = [t['tablename'] for t in conn.execute(
                    "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'"
                ).fetchall()]
                detail = {}
                channels = conn.execute('SELECT COUNT(*) as c FROM collection_channels WHERE is_active=1').fetchone()['c'] if 'collection_channels' in tables else 0
                processing = conn.execute("SELECT COUNT(*) as c FROM content_items WHERE status='processing'").fetchone()['c'] if 'content_items' in tables else 0
                pending = conn.execute("SELECT COUNT(*) as c FROM content_items WHERE status='pending_review'").fetchone()['c'] if 'content_items' in tables else 0
                detail.update({'active_channels': channels, 'processing': processing, 'pending_review': pending})

            elapsed = int((time.time() - start) * 1000)
            return CheckResult('passed', elapsed,
                               f'{channels} channels | {processing} processing | {pending} pending review', detail)
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            return CheckResult('warning', elapsed, f'Check failed: {e}')


# ─── 10. SSE / WebSocket Connection Check ─────────────────────────────

@register('sse_ws')
class SSEWebSocketHealthCheck(BaseHealthCheck):
    check_key = 'sse_ws'
    name = 'SSE/WebSocket'
    category = 'system'
    severity = 'warning'
    description = 'SSE push / WebSocket connection status'
    sort_order = 95
    config_defaults = {'timeout': 5}
    config_schema = {
        'type': 'object',
        'properties': {
            'endpoints': {'type': 'array', 'items': {'type': 'string'},
                          'description': 'SSE endpoint list'},
        }
    }

    def check(self) -> CheckResult:
        start = time.time()
        endpoints = self.config.get('endpoints', [])
        detail = {}
        warnings = []

        for url in endpoints:
            try:
                code, elapsed, body = self._http_get(url, timeout=5)
                if code in (200, 404, 405):
                    detail[url] = f'HTTP {code} (service running)'
                else:
                    warnings.append(f'{url} returned {code}')
                    detail[url] = f'Abnormal: {code}'
            except Exception as e:
                warnings.append(f'{url} unreachable')
                detail[f'{url}_error'] = str(e)[:50]

        elapsed = int((time.time() - start) * 1000)
        if not warnings:
            return CheckResult('passed', elapsed, 'SSE/WS connections OK', detail)
        return CheckResult('warning', elapsed, '; '.join(warnings), detail)


# ─── 11. Error Log Stats ──────────────────────────────────────────────

@register('error_logs')
class ErrorLogHealthCheck(BaseHealthCheck):
    check_key = 'error_logs'
    name = 'Error Logs'
    category = 'error'
    severity = 'warning'
    description = 'Error log count in the last 24 hours'
    sort_order = 100
    config_defaults = {'hours': 24, 'threshold': 50}
    config_schema = {
        'type': 'object',
        'properties': {
            'hours': {'type': 'integer', 'default': 24, 'description': 'Statistics window (hours)'},
            'threshold': {'type': 'integer', 'default': 50, 'description': 'Alert threshold (error count)'},
        }
    }

    def check(self) -> CheckResult:
        start = time.time()
        hours = self.config.get('hours', 24)
        threshold = self.config.get('threshold', 50)

        try:
            from models import get_db as main_db
            with main_db() as conn:
                if conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='health' AND tablename='admin_logs'").fetchone():
                    errors = conn.execute(
                        "SELECT COUNT(*) as c FROM admin_logs WHERE (action LIKE '%error%' OR action LIKE '%fail%') "
                        "AND created_at>=NOW() - INTERVAL '{} hours'".format(hours)
                    ).fetchone()['c']
                else:
                    errors = 0
            elapsed = int((time.time() - start) * 1000)
            detail = {'recent_errors_24h': errors, 'threshold': threshold}
            if errors > threshold:
                return CheckResult('warning', elapsed,
                                   f'{errors} errors in last {hours}h (threshold: {threshold})', detail)
            return CheckResult('passed', elapsed, f'{errors} error logs in last {hours}h', detail)
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            return CheckResult('passed', elapsed, f'Check skipped: {e}')


# ═══════════════════════════════════════════════════════════════════════════
# Fix Suggestions
# ═══════════════════════════════════════════════════════════════════════════
# Checkers can attach fix_suggestions to their CheckResult.detail,
# so that administrators or auto-repair workflows can execute fixes.
# ═══════════════════════════════════════════════════════════════════════════

# ─── Supported fix actions ────────────────────────────────────────────────

FIX_ACTION_MARK_DELETED   = 'mark_deleted'     # Set status='deleted'
FIX_ACTION_CLEAR_FIELD    = 'clear_field'       # Clear a text field to ''
FIX_ACTION_DELETE_RECORD  = 'delete_record'     # DELETE FROM table WHERE id=?
FIX_ACTION_UPDATE_URL     = 'update_url'        # Update a URL field to new value
FIX_ACTION_MARK_DISABLED  = 'mark_disabled'     # Set is_enabled=0
FIX_ACTION_RUN_SQL        = 'run_sql'           # Execute arbitrary SQL
FIX_ACTION_NOTIFY_ADMIN   = 'notify_admin'      # Send a notification to admin

# ── Auto-exec whitelist (safe, reversible actions) ──
FIX_ACTION_CLEAN_TEMP     = 'clean_temp'         # Delete temp files / cache
FIX_ACTION_RESTART_WORKER = 'restart_worker'     # HUP a non-gunicorn worker
FIX_ACTION_SET_LOG_LEVEL  = 'set_log_level'      # Change log level in system_config
FIX_ACTION_FLUSH_CDN      = 'flush_cdn'          # Trigger CDN cache refresh

WHITELIST_FIX_ACTIONS = {
    FIX_ACTION_CLEAN_TEMP, FIX_ACTION_RESTART_WORKER,
    FIX_ACTION_SET_LOG_LEVEL, FIX_ACTION_FLUSH_CDN,
}

ALL_FIX_ACTIONS = {
    FIX_ACTION_MARK_DELETED, FIX_ACTION_CLEAR_FIELD, FIX_ACTION_DELETE_RECORD,
    FIX_ACTION_UPDATE_URL, FIX_ACTION_MARK_DISABLED, FIX_ACTION_RUN_SQL,
    FIX_ACTION_NOTIFY_ADMIN,
    FIX_ACTION_CLEAN_TEMP, FIX_ACTION_RESTART_WORKER,
    FIX_ACTION_SET_LOG_LEVEL, FIX_ACTION_FLUSH_CDN,
}


class FixSuggestion:
    """
    A single fix suggestion describing an executable repair operation.

    Core fields:
        action:    One of FIX_ACTION_* constants
        reason:    Human-readable reason for the fix
        params:    Dict of action-specific parameters (see below)

    Action-specific params:
        mark_deleted:   {'table': str, 'record_id': int}
        clear_field:    {'table': str, 'record_id': int, 'field': str}
        delete_record:  {'table': str, 'record_id': int}
        update_url:     {'table': str, 'record_id': int, 'field': str, 'new_value': str}
        mark_disabled:  {'table': str, 'record_id': int}
        run_sql:        {'sql': str, 'params': list|None}
        notify_admin:   {'message': str, 'level': str}
    """

    def __init__(self, action: str, reason: str = '',
                 params: dict = None, record_type: str = ''):
        self.action = action
        self.reason = reason
        self.params = params or {}
        self.record_type = record_type

    def to_dict(self) -> dict:
        return {
            'action': self.action,
            'reason': self.reason,
            'params': self.params,
            'record_type': self.record_type,
        }

    @staticmethod
    def apply_fix(conn, suggestion: 'FixSuggestion') -> bool:
        """
        Apply a fix using an existing DB connection. Returns True on success.

        For run_sql/notify_admin actions, conn may be None — the caller
        (api_fix) should handle these cases separately.
        """
        params = suggestion.params
        try:
            if suggestion.action == FIX_ACTION_MARK_DELETED:
                if conn and 'table' in params and 'record_id' in params:
                    conn.execute(
                        f"UPDATE {params['table']} SET status='deleted' WHERE id=%s",
                        (params['record_id'],)
                    )
                    return True

            elif suggestion.action == FIX_ACTION_CLEAR_FIELD:
                if conn and 'table' in params and 'record_id' in params and 'field' in params:
                    conn.execute(
                        f"UPDATE {params['table']} SET {params['field']}=%s WHERE id=%s",
                        ('', params['record_id'])
                    )
                    return True

            elif suggestion.action == FIX_ACTION_DELETE_RECORD:
                if conn and 'table' in params and 'record_id' in params:
                    conn.execute(
                        f"DELETE FROM {params['table']} WHERE id=%s",
                        (params['record_id'],)
                    )
                    return True

            elif suggestion.action == FIX_ACTION_UPDATE_URL:
                if conn and 'table' in params and 'record_id' in params \
                        and 'field' in params and 'new_value' in params:
                    conn.execute(
                        f"UPDATE {params['table']} SET {params['field']}=%s WHERE id=%s",
                        (params['new_value'], params['record_id'])
                    )
                    return True

            elif suggestion.action == FIX_ACTION_MARK_DISABLED:
                if conn and 'table' in params and 'record_id' in params:
                    # Try is_enabled first, fall back to is_active
                    try:
                        conn.execute(
                            f"UPDATE {params['table']} SET is_enabled=0 WHERE id=%s",
                            (params['record_id'],)
                        )
                    except Exception:
                        conn.execute(
                            f"UPDATE {params['table']} SET is_active=0 WHERE id=%s",
                            (params['record_id'],)
                        )
                    return True

            elif suggestion.action == FIX_ACTION_RUN_SQL:
                if conn and 'sql' in params:
                    if not _is_safe_run_sql(params['sql']):
                        return False  # SQL 未通过白名单校验，拒绝执行
                    sql_params = params.get('params') or []
                    conn.execute(params['sql'], sql_params)
                    return True

            elif suggestion.action == FIX_ACTION_CLEAN_TEMP:
                # Delete temp files and cache dirs
                import glob, shutil
                temp_patterns = params.get('patterns', ['logs/*.temp', 'data/cache/*'])
                for pattern in temp_patterns:
                    for fp in glob.glob(os.path.join(PROJECT_ROOT, pattern)):
                        try:
                            if os.path.isdir(fp):
                                shutil.rmtree(fp, ignore_errors=True)
                            else:
                                os.remove(fp)
                        except Exception:
                            pass
                return True

            elif suggestion.action == FIX_ACTION_RESTART_WORKER:
                # HUP a non-gunicorn worker process
                worker_name = params.get('worker_name', '')
                # 白名单约束：仅允许进程名（字母/数字/下划线/连字符/点，1-64 字符），
                # 防止 AI 建议的异常 pattern（含正则元字符）注入 pkill -f
                if worker_name and re.fullmatch(r'[A-Za-z0-9_.\-]{1,64}', worker_name):
                    subprocess.run(['pkill', '-HUP', '-f', worker_name],
                                   capture_output=True, timeout=5)
                return True

            elif suggestion.action == FIX_ACTION_SET_LOG_LEVEL:
                # Change log_level in system_config
                if conn and 'level' in params:
                    conn.execute(
                        "INSERT INTO system_config (key, value) VALUES ('log_level', %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                        (params['level'],)
                    )
                    return True

            elif suggestion.action == FIX_ACTION_FLUSH_CDN:
                # POST to CDN refresh URL (SSRF 防护：仅允许 http/https 且目标非内网地址)
                cdn_url = os.environ.get('CDN_REFRESH_URL', params.get('url', ''))
                if cdn_url and _is_safe_http_url(cdn_url):
                    import urllib.request
                    try:
                        urllib.request.urlopen(cdn_url, timeout=10)
                    except Exception:
                        pass
                return True

        except Exception as e:
            # Log and re-raise so caller can catch
            raise RuntimeError(f"Fix failed [{suggestion.action}]: {e}") from e

        return False


# ═══════════════════════════════════════════════════════════════════════════
# Media Integrity Checker
# ═══════════════════════════════════════════════════════════════════════════
# Scans media files/avatars referenced in the database and verifies
# they exist on disk. Reports warnings with fix suggestions for missing files.
# ═══════════════════════════════════════════════════════════════════════════

# Project root for resolving media file disk paths
PROJECT_ROOT = os.path.normpath(os.path.join(BASE_DIR, '..'))

# URL path → disk path mapping rules (highest priority first)
_PATH_MAP = [
    ('/static/media/',     os.path.join(PROJECT_ROOT, 'admin', 'static', 'media')),
    ('/static/avatars/',   os.path.join(PROJECT_ROOT, 'admin', 'static', 'avatars')),
    ('/admin/static/media/', os.path.join(PROJECT_ROOT, 'admin', 'static', 'media')),
    ('/admin/static/avatars/', os.path.join(PROJECT_ROOT, 'admin', 'static', 'avatars')),
]


def _url_to_fs_path(url_path: str) -> str:
    """Convert a URL path (e.g. /static/media/xxx.jpg) to a local filesystem path."""
    for url_prefix, fs_dir in _PATH_MAP:
        if url_path.startswith(url_prefix):
            rel = url_path[len(url_prefix):]
            # Strip extraneous static/ prefix
            if rel.startswith('static/'):
                rel = rel[7:]
            return os.path.normpath(os.path.join(fs_dir, rel)).replace('\\', '/')
    # Fallback: try direct join
    fname = os.path.basename(url_path)
    return os.path.join(PROJECT_ROOT, 'admin', 'static', 'media', fname).replace('\\', '/')


@register('media_integrity')
class MediaIntegrityChecker(BaseHealthCheck):
    check_key = 'media_integrity'
    name = 'Media Integrity'
    category = 'cms'
    severity = 'warning'
    description = 'Scan media files/avatars referenced in DB and verify disk existence'
    sort_order = 85
    config_defaults = {
        'dry_run': True,           # Report only, no fixes by default
        'max_fixes_per_run': 20,   # Max fixes per run
    }
    config_schema = {
        'type': 'object',
        'properties': {
            'dry_run': {'type': 'boolean', 'default': True,
                        'description': 'Dry-run mode: report only, do not execute fixes'},
            'max_fixes_per_run': {'type': 'integer', 'default': 20,
                                  'description': 'Max records to fix per run'},
        }
    }

    def _check_paths(self, records: list, path_field: str, record_type: str,
                     table: str) -> list:
        """Check if files for a batch of records exist on disk. Returns list of missing items."""
        missing = []
        for rec in records:
            raw_path = rec.get(path_field, '')
            if not raw_path:
                continue
            fs_path = _url_to_fs_path(raw_path)
            if not os.path.exists(fs_path):
                missing.append({
                    'record': rec,
                    'fs_path': fs_path,
                    'raw_path': raw_path,
                    'record_type': record_type,
                    'table': table,
                })
        return missing

    def _build_fix_suggestions(self, missing_items: list) -> list:
        """Generate fix suggestions from missing file records."""
        suggestions = []
        for item in missing_items:
            rec = item['record']
            action = 'clear_field'
            reason = f'File not found: {item["raw_path"]}'
            if item['table'] == 'media_files':
                action = 'mark_deleted'
            suggestions.append(FixSuggestion(
                action=action,
                reason=reason,
                params={
                    'table': item['table'],
                    'record_id': rec['id'],
                    'field': item.get('field', ''),
                    'missing_path': item['fs_path'],
                },
                record_type=item['record_type'],
            ))
        return suggestions

    def check(self) -> CheckResult:
        start = time.time()
        missing_all = []
        dry_run = self.config.get('dry_run', True)
        max_fixes = self.config.get('max_fixes_per_run', 20)

        try:
            from models import get_db as main_db
        except ImportError:
            return CheckResult('warning', 0, 'Main DB models not available, skipping media check')

        with main_db() as conn:
            tables_found = [t['tablename'] for t in conn.execute(
                "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'"
            ).fetchall()]

            # ── 1. media_files table ──
            if 'media_files' in tables_found:
                # 只检查未被软删除的记录（deleted_at IS NULL）
                # 缺失文件通过 AI Fix 标记 deleted_at 后不再重复报告
                # 兼容旧 schema：deleted_at 列可能不存在，先探测再决定是否过滤
                try:
                    _cols = [r['column_name'] for r in conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='media_files'"
                    ).fetchall()]
                    _has_deleted_at = 'deleted_at' in _cols
                except Exception:
                    _has_deleted_at = False
                _filter_sql = ' WHERE deleted_at IS NULL' if _has_deleted_at else ''
                rows = conn.execute(
                    "SELECT id, file_path, thumb_path, original_name FROM media_files"
                    + _filter_sql
                ).fetchall()
                for field in ('file_path', 'thumb_path'):
                    missing_all.extend(self._check_paths(
                        [dict(r) for r in rows], field, 'media_file', 'media_files'
                    ))

            # ── 2. users table: avatars ──
            if 'users' in tables_found:
                rows = conn.execute(
                    "SELECT id, avatar_url FROM users "
                    "WHERE avatar_url IS NOT NULL AND avatar_url != ''"
                ).fetchall()
                missing_all.extend(self._check_paths(
                    [dict(r) for r in rows], 'avatar_url', 'avatar', 'users'
                ))

            # ── 3. brand_settings: logo + favicon ──
            if 'brand_settings' in tables_found:
                row = conn.execute(
                    "SELECT id, logo_url, favicon_url FROM brand_settings WHERE id=1"
                ).fetchone()
                if row:
                    r = dict(row)
                    missing_all.extend(self._check_paths(
                        [r], 'logo_url', 'brand_logo', 'brand_settings'
                    ))
                    missing_all.extend(self._check_paths(
                        [r], 'favicon_url', 'brand_favicon', 'brand_settings'
                    ))

            # ── 4. social_links: icons ──
            if 'social_links' in tables_found:
                rows = conn.execute(
                    "SELECT id, icon_url, name FROM social_links "
                    "WHERE icon_url IS NOT NULL AND icon_url != ''"
                ).fetchall()
                missing_all.extend(self._check_paths(
                    [dict(r) for r in rows], 'icon_url', 'social_icon', 'social_links'
                ))

        # Deduplicate by file path + record ID + field
        seen_paths = set()
        unique_missing = []
        for item in missing_all:
            key = (item['fs_path'], item['record']['id'], item.get('field', ''))
            if key not in seen_paths:
                seen_paths.add(key)
                unique_missing.append(item)

        fix_suggestions = self._build_fix_suggestions(unique_missing[:max_fixes])
        total_missing = len(unique_missing)
        limited = total_missing > max_fixes

        elapsed = int((time.time() - start) * 1000)
        detail = {
            'total_missing': total_missing,
            'max_fixes': max_fixes,
            'limited': limited,
            'dry_run': dry_run,
            'items': [{
                'record_type': item['record_type'],
                'table': item['table'],
                'id': item['record']['id'],
                'field': item.get('field', ''),
                'raw_path': item['raw_path'],
                'fs_path': item['fs_path'],
                'original_name': item['record'].get('original_name', ''),
            } for item in unique_missing[:max_fixes]],
            'fix_suggestions': [s.to_dict() for s in fix_suggestions],
        }

        if total_missing == 0:
            return CheckResult('passed', elapsed, 'All media files exist', detail)

        msg = f'Found {total_missing} missing files'
        if limited:
            msg += f' (showing first {max_fixes})'
        if dry_run:
            msg += ' (Dry-run, no fixes applied)'
        return CheckResult('warning', elapsed, msg, detail)


# ═══════════════════════════════════════════════════════════════════════════
# Internal Link Checker
# ═══════════════════════════════════════════════════════════════════════════
# Scans all structured link records (navigation, footer, social, blocks, etc.)
# and CMS article content for broken, redirected, or problematic URLs.
# ═══════════════════════════════════════════════════════════════════════════

# ─── Link source definitions: (table, url_field, id_field, title_field, source_type)
_LINK_SOURCES = [
    # Structured navigation links
    ('header_nav',       'url', 'id', 'title', 'header_nav'),
    ('footer_nav',       'url', 'id', 'title', 'footer_nav'),
    ('footer_links',     'url', 'id', 'title', 'footer_links'),
    ('footer_articles',  'url', 'id', 'title', 'footer_articles'),
    ('partner_links',    'url', 'id', 'name', 'partner_links'),
    ('social_media_links', 'url', 'id', 'platform_name', 'social_media'),
    # Block / content links
    ('cms_blocks',       'link_url', 'id', 'title', 'cms_block'),
    ('site_blocks',      'link_url', 'id', 'title', 'site_block'),
    ('ad_placements',    'link_url', 'id', 'title', 'ad_placement'),
    # Notification / download links
    ('downloads',        'download_url', 'id', 'title', 'download'),
    ('downloads',        'repo_url', 'id', 'title', 'download'),
    ('downloads',        'docs_url', 'id', 'title', 'download'),
]

_LINK_CHECK_RESULT_STATUS = {
    200: 'healthy', 301: 'redirect', 302: 'redirect',
    303: 'redirect', 307: 'redirect', 308: 'redirect',
    400: 'bad_request', 401: 'unauthorized', 403: 'forbidden',
    404: 'broken', 410: 'broken', 500: 'server_error',
    502: 'server_error', 503: 'server_error', 504: 'server_error',
}


def _resolve_redirect_chain(url: str, max_hops: int = 5, timeout: int = 5) -> dict:
    """Follow redirect chain and return final status and final URL."""
    import urllib.request
    import urllib.error
    import ssl

    hops = []
    current_url = url
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for _ in range(max_hops + 1):
        try:
            req = urllib.request.Request(current_url, method='HEAD')
            req.add_header('User-Agent', 'VeroRun-HealthCheck/1.0')
            resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
            hops.append({
                'url': current_url,
                'status': resp.getcode(),
            })
            final_url = resp.geturl()
            return {
                'final_status': resp.getcode(),
                'final_url': final_url,
                'hops': hops,
                'redirect_count': len(hops) - 1,
            }
        except urllib.error.HTTPError as e:
            status = e.getcode() or 0
            hops.append({'url': current_url, 'status': status})
            redir_url = e.headers.get('Location')
            if redir_url and status in (301, 302, 303, 307, 308):
                current_url = redir_url if redir_url.startswith('http') else \
                    url.rstrip('/') + '/' + redir_url.lstrip('/')
                continue
            return {
                'final_status': status,
                'final_url': current_url,
                'hops': hops,
                'redirect_count': len(hops) - 1,
            }
        except (urllib.error.URLError, OSError, socket.timeout) as e:
            return {
                'final_status': 0,
                'final_url': current_url,
                'hops': hops,
                'redirect_count': len(hops) - 1,
                'error': str(e),
            }
    # Exceeded max hops
    return {
        'final_status': 0,
        'final_url': current_url,
        'hops': hops,
        'redirect_count': max_hops,
        'error': 'Max redirect hops exceeded',
    }


@register('internal_links')
class InternalLinkChecker(BaseHealthCheck):
    """
    Scan all internal links (navigation, footer, blocks, CMS articles) for:
      - Broken links (404/410)
      - Redirect chains (301/302 → could be updated to final URL)
      - Server errors
      - Unreachable / timeout
    """
    check_key = 'internal_links'
    name = 'Internal Link Check'
    category = 'cms'
    severity = 'warning'
    description = 'Scan all internal links for broken, redirected, or problematic URLs'
    sort_order = 40  # default

    config_defaults = {
        'max_urls': 50,
        'timeout': 5,
        'check_redirects': True,
    }
    config_schema = {
        'type': 'object',
        'properties': {
            'max_urls': {'type': 'integer', 'default': 50, 'description': 'Max URLs to check per run'},
            'timeout': {'type': 'integer', 'default': 5, 'description': 'Timeout per URL (seconds)'},
            'check_redirects': {'type': 'boolean', 'default': True, 'description': 'Follow/check redirect chains'},
        },
    }

    # ── helpers ──────────────────────────────────────────────────────────

    def _collect_urls(self, conn) -> list:
        """Collect all URLs from database sources, return list of dicts."""
        urls = []
        seen_tables = set()

        # Check which tables exist
        existing = conn.execute(
            "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'"
        ).fetchall()
        for r in existing:
            seen_tables.add(r['tablename'])

        for table, field, id_field, title_field, source_type in _LINK_SOURCES:
            if table not in seen_tables:
                continue
            try:
                rows = conn.execute(
                    f"SELECT {id_field} AS id, {title_field} AS title, "
                    f"{field} AS url FROM {table} "
                    f"WHERE {field} IS NOT NULL AND {field} != ''"
                ).fetchall()
                for r in rows:
                    url = r['url'].strip()
                    if url and url.startswith(('http://', 'https://', '/')):
                        urls.append({
                            'table': table,
                            'field': field,
                            'record_id': r['id'],
                            'title': r['title'] or '',
                            'url': url,
                            'source_type': source_type,
                        })
            except Exception as e:
                continue

        # CMS article content — extract <a href> links
        if 'cms_posts' in seen_tables:
            try:
                import re
                posts = conn.execute(
                    "SELECT id, title, content FROM cms_posts "
                    "WHERE content IS NOT NULL AND content != ''"
                ).fetchall()
                for post in posts:
                    hrefs = re.findall(
                        r'<a\s+(?:[^>]*?\s+)?href="([^"]+)"',
                        post['content'], re.IGNORECASE
                    )
                    for href in hrefs:
                        h = href.strip()
                        if h and h.startswith(('http://', 'https://', '/')):
                            urls.append({
                                'table': 'cms_posts',
                                'field': 'content',
                                'record_id': post['id'],
                                'title': post['title'] or '',
                                'url': h,
                                'source_type': 'cms_article',
                            })
            except Exception:
                pass

        return urls

    def _check_url(self, url_info: dict, timeout: int) -> dict:
        """Check a single URL, return diagnostic result."""
        url = url_info['url']
        if url.startswith('/'):
            # Relative URL — skip HTTP check, mark as internal reference
            return {
                **url_info,
                'status': 'internal_path',
                'http_status': None,
                'message': 'Internal relative path (no HTTP check)',
            }

        result = _resolve_redirect_chain(url, max_hops=5, timeout=timeout)

        final_status = result.get('final_status', 0)
        status_label = _LINK_CHECK_RESULT_STATUS.get(final_status, 'unknown')
        redirect_count = result.get('redirect_count', 0)
        hops = result.get('hops', [])
        error = result.get('error', '')

        message_parts = []
        if status_label == 'healthy':
            message_parts.append('OK')
        elif status_label == 'redirect':
            message_parts.append(f'Redirects to: {result.get("final_url", "")}')
        elif status_label == 'broken':
            message_parts.append('Broken link (404/410)')
        elif status_label == 'server_error':
            message_parts.append(f'Server error ({final_status})')
        else:
            message_parts.append(error or f'HTTP {final_status}')

        return {
            **url_info,
            'status': status_label,
            'http_status': final_status,
            'redirect_count': redirect_count,
            'final_url': result.get('final_url') if redirect_count > 0 else None,
            'hops': hops,
            'message': ' — '.join(message_parts),
        }

    def _build_fix_suggestions(self, checked_urls: list) -> list:
        """Generate fix suggestions from checked URLs."""
        suggestions = []
        processed_ids = set()

        for item in checked_urls:
            status = item.get('status', '')
            table = item['table']
            record_id = item['record_id']
            field = item['field']

            # Deduplicate by table+record_id+field
            dedup_key = f'{table}:{record_id}:{field}'
            if dedup_key in processed_ids:
                continue
            processed_ids.add(dedup_key)

            if status == 'broken':
                suggestions.append(FixSuggestion(
                    action=FIX_ACTION_MARK_DISABLED,
                    reason=f'Broken link returned 404/410: {item["url"]}',
                    params={'table': table, 'record_id': record_id},
                    record_type=item.get('source_type', ''),
                ))
            elif status == 'redirect' and item.get('final_url'):
                suggestions.append(FixSuggestion(
                    action=FIX_ACTION_UPDATE_URL,
                    reason=f'Redirect chain: {item["url"]} → {item["final_url"]}',
                    params={
                        'table': table,
                        'record_id': record_id,
                        'field': field,
                        'new_value': item['final_url'],
                    },
                    record_type=item.get('source_type', ''),
                ))

        return suggestions

    # ── main check ────────────────────────────────────────────────────────

    def check(self) -> CheckResult:
        start = time.time()
        max_urls = self.config.get('max_urls', 50)
        timeout = self.config.get('timeout', 5)

        try:
            from models import get_db as main_db
        except ImportError:
            return CheckResult('warning', 0, 'Main DB not available, skip internal link check')

        try:
            with main_db() as conn:
                # Collect URLs
                all_urls = self._collect_urls(conn)
                if not all_urls:
                    elapsed = int((time.time() - start) * 1000)
                    return CheckResult('passed', elapsed, 'No links to check', {'total_urls': 0})

                # Check (limit to max_urls)
                urls_to_check = all_urls[:max_urls]
                checked = []
                for u in urls_to_check:
                    try:
                        result = self._check_url(u, timeout)
                        checked.append(result)
                    except Exception as e:
                        checked.append({**u, 'status': 'error', 'message': str(e)})

                # Summary stats
                total = len(checked)
                healthy = sum(1 for c in checked if c['status'] == 'healthy')
                broken = sum(1 for c in checked if c['status'] == 'broken')
                redirects = sum(1 for c in checked if c['status'] == 'redirect')
                internal_paths = sum(1 for c in checked if c['status'] == 'internal_path')
                errors = sum(1 for c in checked if c['status'] in ('server_error', 'unknown', 'error'))

                # Build fix suggestions
                fix_suggestions = self._build_fix_suggestions(checked)

                elapsed = int((time.time() - start) * 1000)
                detail = {
                    'total_urls': len(all_urls),
                    'checked': total,
                    'healthy': healthy,
                    'broken': broken,
                    'redirects': redirects,
                    'internal_paths': internal_paths,
                    'errors': errors,
                    'items': [{
                        'table': c.get('table', ''),
                        'record_id': c.get('record_id', 0),
                        'field': c.get('field', ''),
                        'url': c.get('url', ''),
                        'status': c.get('status', ''),
                        'http_status': c.get('http_status'),
                        'message': c.get('message', ''),
                        'final_url': c.get('final_url'),
                        'title': c.get('title', ''),
                        'source_type': c.get('source_type', ''),
                    } for c in checked],
                    'fix_suggestions': [s.to_dict() for s in fix_suggestions],
                }

                if broken == 0 and errors == 0 and redirects == 0:
                    return CheckResult('passed', elapsed, f'All {total} links healthy', detail)

                parts = []
                if broken:
                    parts.append(f'{broken} broken')
                if errors:
                    parts.append(f'{errors} errors')
                if redirects:
                    parts.append(f'{redirects} redirects')
                msg = ', '.join(parts) + f' of {total} links'
                return CheckResult('warning', elapsed, msg, detail)
        except Exception as e:
            return CheckResult('warning', 0, f'Internal link check failed: {e}')


# ═══════════════════════════════════════════════════════════════════════════
# VeroGuard Guardian Check
# ═══════════════════════════════════════════════════════════════════════════
# Monitors the verorun-guardian systemd daemon via the Health Service
# /api/guardian/status endpoint (port 8085). Returns critical if guardian
# is not running, since it is the last line of defense for service recovery.
# ═══════════════════════════════════════════════════════════════════════════

@register('veroguard')
class VeroGuardHealthCheck(BaseHealthCheck):
    check_key = 'veroguard'
    name = 'VeroGuard Guardian'
    category = 'system'
    severity = 'critical'
    description = 'VeroGuard daemon running status, self-protect, and heartbeat health'
    sort_order = 15
    config_defaults = {
        'guardian_status_url': 'http://127.0.0.1:8085/api/guardian/status',
        'timeout': 5,
    }
    config_schema = {
        'type': 'object',
        'properties': {
            'guardian_status_url': {'type': 'string', 'description': 'Guardian status endpoint URL'},
            'timeout': {'type': 'integer', 'default': 5, 'description': 'Timeout (seconds)'},
        }
    }

    def check(self) -> CheckResult:
        start = time.time()
        url = self.config.get('guardian_status_url', 'http://127.0.0.1:8085/api/guardian/status')
        code, elapsed, body = self._http_get(url, self.config.get('timeout', 5))

        if code == 200:
            try:
                data = json.loads(body)
                detail = data.get('data', {})
                return CheckResult('passed', elapsed,
                    'VeroGuard running', detail)
            except json.JSONDecodeError:
                return CheckResult('passed', elapsed, 'VeroGuard running (HTTP 200)')
        elif code == 503:
            return CheckResult('error', elapsed,
                'VeroGuard not running or status file missing',
                {'http_status': 503})
        elif code == 0:
            return CheckResult('error', elapsed,
                'Health Service (8085) unreachable — cannot check VeroGuard',
                {'error': 'connection_failed'})
        else:
            return CheckResult('warning', elapsed,
                f'VeroGuard status unknown (HTTP {code})')


# ═══════════════════════════════════════════════════════════════════════════
# AI Gateway Check
# ═══════════════════════════════════════════════════════════════════════════
# Checks AI infrastructure health: budget gate status, token usage today,
# and provider model configuration. Gracefully degrades if agent_matrix is
# not installed (returns 'passed' with note).
# ═══════════════════════════════════════════════════════════════════════════

@register('ai_gateway')
class AIGatewayHealthCheck(BaseHealthCheck):
    check_key = 'ai_gateway'
    name = 'AI Gateway'
    category = 'system'
    severity = 'warning'
    description = 'AI budget gate status, token usage, and provider model availability'
    sort_order = 32
    config_defaults = {'timeout': 10}

    def check(self) -> CheckResult:
        start = time.time()
        detail = {}
        warnings = []

        # 1. Check AI budget gate
        try:
            from agent_matrix.engine import check_ai_budget
            allowed, reason = check_ai_budget(scene='health_check')
            detail['budget_gate'] = {'allowed': allowed, 'reason': reason}
            if not allowed:
                warnings.append(f'AI budget gate blocked: {reason}')
        except ImportError:
            detail['budget_gate'] = 'agent_matrix not installed'
        except Exception as e:
            detail['budget_gate_error'] = str(e)

        # 2. Check token usage today
        try:
            from models import get_db as main_db
            with main_db() as conn:
                today = datetime.now().strftime('%Y-%m-%d')
                usage = conn.execute(
                    "SELECT COALESCE(SUM(total_tokens), 0) as total "
                    "FROM agent_token_daily WHERE stat_date=%s",
                    (today,)
                ).fetchone()
                if usage:
                    detail['tokens_today'] = float(usage['total'])
        except Exception:
            pass

        # 3. Check provider_models table has configured models
        try:
            from models import get_db as main_db
            with main_db() as conn:
                tables = [t['tablename'] for t in conn.execute(
                    "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'"
                ).fetchall()]
                if 'provider_models' in tables:
                    count = conn.execute(
                        'SELECT COUNT(*) as c FROM provider_models WHERE is_active=1'
                    ).fetchone()['c']
                    detail['active_models'] = count
                    if count == 0:
                        warnings.append('No active AI provider models configured')
        except Exception:
            pass

        elapsed = int((time.time() - start) * 1000)
        if warnings:
            return CheckResult('warning', elapsed, '; '.join(warnings), detail)
        return CheckResult('passed', elapsed, 'AI Gateway operational', detail)


# ═══════════════════════════════════════════════════════════════════════════
# Plugin Store & License Check
# ═══════════════════════════════════════════════════════════════════════════
# Pings the plugin store and license service APIs. Uses plugin_manager.region
# for region-aware API base URL resolution.
# ═══════════════════════════════════════════════════════════════════════════

@register('plugin_store')
class PluginStoreHealthCheck(BaseHealthCheck):
    check_key = 'plugin_store'
    name = 'Plugin Store & License'
    category = 'external'
    severity = 'warning'
    description = 'Plugin store connectivity and license service availability'
    sort_order = 45
    config_defaults = {'timeout': 10}

    def check(self) -> CheckResult:
        start = time.time()
        detail = {}
        warnings = []
        timeout = self.config.get('timeout', 10)

        # Resolve API base URL (region-aware)
        try:
            from plugin_manager.region import get_api_base
            api_base = get_api_base()
        except (ImportError, Exception):
            api_base = os.environ.get('APP_API_URL', 'https://api.verorun.com/v1')

        # 1. Plugin store ping
        store_url = f'{api_base}/store/ping'
        store_host = _extract_host_from_url(store_url)
        if store_host and _is_private_host(store_host):
            detail['store'] = {'url': store_url, 'code': 0, 'ms': 0, 'status': 'blocked', 'reason': 'private_ip'}
            warnings.append(f'Plugin store blocked (private IP: {store_host})')
        else:
            code1, elapsed1, _ = self._http_get(store_url, timeout)
            detail['store'] = {'url': store_url, 'code': code1, 'ms': elapsed1}
            if code1 != 200:
                warnings.append(f'Plugin store unreachable (HTTP {code1})')

        # 2. License service ping
        license_url = f'{api_base}/license/ping'
        license_host = _extract_host_from_url(license_url)
        if license_host and _is_private_host(license_host):
            detail['license'] = {'url': license_url, 'code': 0, 'ms': 0, 'status': 'blocked', 'reason': 'private_ip'}
            warnings.append(f'License service blocked (private IP: {license_host})')
        else:
            code2, elapsed2, _ = self._http_get(license_url, timeout)
            detail['license'] = {'url': license_url, 'code': code2, 'ms': elapsed2}
            if code2 != 200:
                warnings.append(f'License service unreachable (HTTP {code2})')

        elapsed = int((time.time() - start) * 1000)
        if warnings:
            return CheckResult('warning', elapsed, '; '.join(warnings), detail)
        return CheckResult('passed', elapsed, 'Plugin store & license service OK', detail)


# ═══════════════════════════════════════════════════════════════════════════
# Template for Adding New Checkers
# ═══════════════════════════════════════════════════════════════════════════
#
# Usage:
#   1. Add a record to the health_checks table (check_key must match the
#      registration key)
#   2. Restart the admin service for it to take effect
#
# No other code changes required.
#
# ─── Template Start ────────────────────────────────────────────────────
#
# @register('your_check_key')           # ← Unique key, matches health_checks.check_key
# class YourCheck(BaseHealthCheck):
#     check_key = 'your_check_key'
#     name = 'Your Check Name'            # ← Display name in admin UI
#     category = 'system'                 # ← Category: system/external/workflow/agent/cms/ssl/error
#     severity = 'warning'                # ← Severity: info/warning/critical
#     description = 'Describe what this check does'
#     sort_order = 55                     # ← Sort order (lower = higher priority)
#
#     # Config defaults (optional)
#     config_defaults = {
#         'timeout': 5,
#         'some_option': 'default_value',
#     }
#
#     # Config JSON Schema (optional, for admin page visual editing)
#     config_schema = {
#         'type': 'object',
#         'properties': {
#             'timeout': {'type': 'integer', 'default': 5, 'description': 'Timeout (seconds)'},
#             'some_option': {'type': 'string', 'default': 'default_value', 'description': 'Option description'},
#         }
#     }
#
#     def check(self) -> CheckResult:
#         """Implement check logic, return CheckResult."""
#         start = time.time()
#         try:
#             # ... your check logic ...
#             elapsed = int((time.time() - start) * 1000)
#             return CheckResult('passed', elapsed, 'Everything OK', {'key': 'value'})
#         except Exception as e:
#             elapsed = int((time.time() - start) * 1000)
#             return CheckResult('error', elapsed, f'Error: {e}')
#
# ─── Template End ──────────────────────────────────────────────────────
