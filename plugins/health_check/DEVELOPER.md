# Health Check Center — Developer Guide for Adding New Checkers

## Architecture Overview

```
checkers.py                    routes.py                    admin/templates/health.html
┌──────────────────┐           ┌──────────────────┐         ┌──────────────────────┐
│  BaseHealthCheck │           │  GET /api/status  │         │  ⚙️ Manage Checkers Tab│
│  (Abstract Base) │           │  POST /api/run    │ ◄────── │  📦 Registry Display   │
│         ↑        │           │  GET /api/checks  │         │  ＋One-click Add       │
│  Subclass Impl   │           │  GET /api/checkers/registry│  🗑 Delete/Sort/Config │
│  @register Hook  │           │  POST /api/checkers/register│                      │
│         ↑        │           └──────────────────┘         └──────────────────────┘
│  CheckerRegistry │
│  (Global Registry)│
└──────────────────┘
```

**Core Concepts:**
- **BaseHealthCheck** — Abstract base class for all checkers
- **@register(check_key)** — Registers a checker class into the global registry
- **CheckerRegistry** — Manages all registered checkers, supports list / get / unregister
- **health_checks table** — Stores checker enable/disable, config, sort order, etc.
- **Health Check Runner** — Iterates enabled items in the health_checks table and executes them

---

## Method 1: Code Registration (Recommended)

### Step 1: Write a Checker Class in checkers.py

```python
from health_check.checkers import BaseHealthCheck, CheckResult, register

@register('my_business_api')          # ← Unique key, corresponds to DB check_key
class MyBusinessAPIHealthCheck(BaseHealthCheck):
    # ── Metadata (required) ──
    check_key = 'my_business_api'
    name = 'Business API Check'              # Name displayed in admin panel
    category = 'external'              # Category: system/external/workflow/agent/cms/ssl/error
    severity = 'warning'               # Severity: info/warning/critical
    description = 'Check availability and response time of a specific business API'
    sort_order = 55                    # Sort order (lower = higher priority)

    # ── Config Defaults (optional) ──
    config_defaults = {
        'timeout': 5,
        'base_url': 'https://api.example.com',
    }

    # ── Config JSON Schema (optional, visual editing in admin panel) ──
    config_schema = {
        'type': 'object',
        'properties': {
            'timeout': {'type': 'integer', 'default': 5, 'description': 'Timeout (seconds)'},
            'base_url': {'type': 'string', 'default': 'https://api.example.com', 'description': 'API Base URL'},
        }
    }

    def check(self) -> CheckResult:
        """
        Core check logic.
        Returns CheckResult(status, response_time_ms, message, detail)
        Where status: 'passed' | 'warning' | 'error'
        """
        start = time.time()
        try:
            url = self.config.get('base_url') + '/health'
            code, elapsed, body = self._http_get(url, self.config.get('timeout', 5))

            if code == 200:
                return CheckResult('passed', elapsed, f'API OK (HTTP {code})')
            else:
                return CheckResult('error', elapsed, f'API returned {code}')
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            return CheckResult('error', elapsed, str(e))
```

### Step 2: Enable in Admin Panel

1. After deployment, go to "Health Check" → "⚙️ Checker Config"
2. Find your new checker in the "📦 Available Checkers" section
3. Click the "＋Add" button
4. The new checker will appear in the configured list, enabled by default

---

## Method 2: DB-Only Record (No Python Code Needed)

Suitable for ad-hoc HTTP endpoint checks or scenarios that don't require complex logic.

### Operate in Admin Panel

1. Go to "Health Check" → "⚙️ Checker Config"
2. In the "➕ Manual Add Checker" section, fill in:
   - Key: `check_my_service`
   - Name: `My Service Check`
   - Category: `external`
   - Severity: `warning`
3. Click "Add"
4. The checker will appear in the list, with status `warning` on execution (no corresponding Python checker implementation)

---

## Method 3: Separate File Registration (Modular)

For complex checkers, place them in a separate file:

```python
# health_check/checkers/my_custom_checker.py
from ..checkers import BaseHealthCheck, CheckResult, register

@register('custom_check')
class CustomCheck(BaseHealthCheck):
    check_key = 'custom_check'
    name = 'Custom Check'
    category = 'system'
    severity = 'warning'

    def check(self) -> CheckResult:
        # ... implementation
        pass
```

Then import this file in `routes.py` or `__init__.py`:

```python
# At the end of checkers.py or the beginning of routes.py
from .checkers.my_custom_checker import CustomCheck  # Triggers @register
```

---

## Checker API Reference

### BaseHealthCheck Class

| Method/Attribute | Type | Description |
|-----------|------|------|
| `check_key` | `str` (Class attr) | Unique identifier key |
| `name` | `str` (Class attr) | Display name |
| `category` | `str` (Class attr) | Category label |
| `severity` | `str` (Class attr) | Severity level |
| `description` | `str` (Class attr) | Description |
| `sort_order` | `int` (Class attr) | Sort weight |
| `config_defaults` | `dict` (Class attr) | Default config |
| `config_schema` | `dict` (Class attr) | JSON Schema |
| `check()` | Method (Required) | Execute check → CheckResult |
| `_http_get(url, timeout)` | Method | HTTP GET request → (status, ms, body) |

### CheckResult Class

```python
CheckResult(
    status='passed',         # 'passed' | 'warning' | 'error'
    response_time_ms=0,      # Response time (ms)
    message='Everything OK',    # Display message
    detail={'key': 'value'}  # JSON details (auto-serialized)
)
```

### Utility Methods

- `self._http_get(url, timeout=5)` → `(status_code, elapsed_ms, body)`

---

## Common Example Templates

### Check Agent Matrix Master/Child Agents

```python
@register('agent_status')
class AgentStatusHealthCheck(BaseHealthCheck):
    check_key = 'agent_status'
    name = 'Agent Online Status'
    category = 'agent'
    severity = 'critical'
    description = 'Health status of the master agent and all child agents'

    def check(self) -> CheckResult:
        start = time.time()
        try:
            from agent_matrix.models import get_db
            with get_db() as conn:
                agents = conn.execute(
                    'SELECT id, name, status, last_heartbeat FROM agent_matrix'
                ).fetchall()
            elapsed = int((time.time() - start) * 1000)
            online = sum(1 for a in agents if a.get('status') == 'online')
            total = len(agents)
            if online == total:
                return CheckResult('passed', elapsed, f'{online}/{total} Agents Online')
            return CheckResult('warning', elapsed, f'{online}/{total} Agents Online ({total-online} Offline)',
                               {'agents': [dict(a) for a in agents]})
        except Exception as e:
            return CheckResult('error', 0, str(e))
```

### Check Content Factory Collection Channels

```python
@register('content_pipeline')
class ContentPipelineCheck(BaseHealthCheck):
    check_key = 'content_pipeline'
    name = 'Content Pipeline Check'
    category = 'cms'
    severity = 'warning'
    description = 'Content factory collection channel status and processing queue depth'

    def check(self) -> CheckResult:
        start = time.time()
        try:
            from models import get_db
            with get_db() as conn:
                channels = conn.execute(
                    'SELECT name, status, last_run_at FROM collection_channels WHERE is_active=1'
                ).fetchall()
                queue_depth = conn.execute(
                    "SELECT COUNT(*) as c FROM content_items WHERE status='pending'"
                ).fetchone()['c']
            elapsed = int((time.time() - start) * 1000)
            return CheckResult('passed', elapsed,
                               f'{len(channels)} channels | Queue depth {queue_depth}',
                               {'channels': [dict(c) for c in channels], 'queue_depth': queue_depth})
        except Exception as e:
            return CheckResult('error', 0, str(e))
```

### Check Specific Workflow Recent Execution Status

```python
@register('workflow_recent')
class WorkflowRecentCheck(BaseHealthCheck):
    check_key = 'workflow_recent'
    name = 'Recent Workflow Status'
    category = 'workflow'
    severity = 'warning'
    description = 'Check the recent execution status of a specific Workflow'
    config_defaults = {'workflow_id': 1, 'max_failures': 3}

    def check(self) -> CheckResult:
        start = time.time()
        try:
            from orchestrator import models as om
            wf_id = self.config.get('workflow_id', 1)
            with om.get_db() as conn:
                recent = conn.execute(
                    "SELECT id, status, started_at, finished_at FROM workflow_instances "
                    "WHERE workflow_id=? ORDER BY started_at DESC LIMIT 10",
                    (wf_id,)
                ).fetchall()
                failures = sum(1 for r in recent if r['status'] == 'failed')
            elapsed = int((time.time() - start) * 1000)
            threshold = self.config.get('max_failures', 3)
            if failures > threshold:
                return CheckResult('warning', elapsed,
                                   f'{failures} failures in last {len(recent)} runs (threshold: {threshold})',
                                   {'instances': [dict(r) for r in recent]})
            return CheckResult('passed', elapsed,
                               f'All {len(recent)} recent runs OK')
        except Exception as e:
            return CheckResult('error', 0, str(e))
```

### Check Redis Connection Pool Status

```python
@register('redis_pool')
class RedisPoolHealthCheck(BaseHealthCheck):
    check_key = 'redis_pool'
    name = 'Redis Connection Pool Status'
    category = 'system'
    severity = 'warning'
    description = 'Redis connection pool usage, memory, and hit rate'
    config_defaults = {'host': '127.0.0.1', 'port': 6379, 'max_clients_warn': 100}

    def check(self) -> CheckResult:
        start = time.time()
        try:
            import redis
            r = redis.Redis(host=self.config['host'], port=self.config['port'],
                           socket_timeout=3, decode_responses=True)
            info = r.info()
            elapsed = int((time.time() - start) * 1000)
            detail = {
                'connected_clients': info.get('connected_clients'),
                'used_memory_human': info.get('used_memory_human'),
                'uptime_in_days': info.get('uptime_in_days'),
                'keyspace_hit_ratio': round(
                    info.get('keyspace_hits', 0) * 100 / max(
                        info.get('keyspace_hits', 0) + info.get('keyspace_misses', 1), 1), 2
                ),
            }
            clients = detail['connected_clients']
            threshold = self.config.get('max_clients_warn', 100)
            if clients > threshold:
                return CheckResult('warning', elapsed,
                                   f'Connections {clients} exceed threshold {threshold}', detail)
            return CheckResult('passed', elapsed,
                               f'Redis OK | {clients} connections | {detail["used_memory_human"]}', detail)
        except Exception as e:
            return CheckResult('error', 0, str(e))
```

---

## Deployment Verification

```bash
# 1. Deploy code to server
cd ~/projects/your-project
scp -r health_check/ your-user@your-server:/path/to/deployment/

# 2. Restart admin service
ssh your-user@your-server "cd /path/to/deployment && find health_check -name __pycache__ -exec rm -rf {} + && fuser -k 8084/tcp && sleep 1 && cd admin && python3 -B app.py 8084 &"

# 3. Verify new checker is visible
# Visit admin panel → Health Check → ⚙️ Checker Config
# Click "＋Add" in "📦 Available Checkers"
```

---

## Important Notes

1. **check_key must be unique** — Corresponds to health_checks.check_key in the DB
2. **Restart after deployment** — Newly registered checker classes take effect after the admin service restarts
3. **Existing DB record** — If the check_key already exists in the health_checks table, clicking "＋Add" will prompt that it already exists
4. **Checker unavailable** — If @register is declared but no corresponding Python implementation exists, execution will be marked as warning
5. **Async execution** — Manual checks are async; wait a few seconds after clicking "⚡ Run Now" for the refresh

---

## Resource Auto-Discovery Engine (Resource Discovery)

### Overview

`discovery.py` provides four auto-discovery capabilities, displayed in the "Inventory" tab:

| Scanner | Discovery Content | Purpose |
|--------|----------|------|
| `scan_modules()` | All subdirectories containing `app.py`/`routes.py`/`models.py` in the project | Understand module distribution |
| `scan_endpoints(app)` | All Blueprint routes registered in the Flask app | View full API endpoint list |
| `scan_tables(db_path)` | All table names, column counts, row counts, auto-increment ID watermark in SQLite DB | Full database scan |
| `scan_plugins()` | All installed plugins in the plugin directory with their status/health check count | Plugin registration status |

### API Endpoints

```
GET /admin/health/api/discovery/status  — Triggers a full scan and returns JSON
```

### 4 Built-in Discovery Checkers

| check_key | Name | Function |
|-----------|------|------|
| `discovery_modules` | Module Discovery | Scan module directories, track added/removed modules |
| `discovery_endpoints` | Endpoint Discovery | Scan Flask routes, group by Blueprint |
| `discovery_tables` | Database Table Discovery | Scan DB tables, track added/removed/changed tables |
| `discovery_plugins` | Plugin Discovery | Scan plugin directory, report load status and health check registration |

These checkers are automatically registered by `import health_check.discovery` when `admin/app.py` starts.

### Usage

1. After deployment, go to "Health Check" → "📦 Inventory" tab
2. Click "⚡ Run Now" to trigger a full scan (includes discovery checkers)
3. Enable/disable each discovery checker in "⚙️ Checker Config"

### State Persistence

- `discovery_modules` and `discovery_tables` save last scan results as JSON files
- Path: `health_check/data/discovery_*.json`
- Used to detect added/removed resource changes
