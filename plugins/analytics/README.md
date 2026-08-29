# Analytics (analytics)

## Overview

Analytics is VeroRun's server-side cookie-less analytics middleware plugin, providing complete website traffic data collection, storage, aggregation, and visualization. The plugin uses a cookie-less lightweight tracking approach to deliver PV/UV statistics, visitor session identification, page-level behavior analysis, geolocation resolution, and trend analysis without relying on client-side cookies.

Version: **1.7.0**

## Features

- **Cookie-less Tracking**: Visitor identification based on server-side fingerprints (IP + User-Agent combined hash), no client cookies required, privacy-compliant
- **PV/UV Statistics**: Accurate page view (PV) and unique visitor (UV) tracking
- **Visitor Session Management**: Automatic session identification and merging based on time windows
- **Page-level Statistics**: Fine-grained analysis by page path, referrer, device type, etc.
- **Geolocation**: ip2region-based IP geolocation (country/province/city), supports local offline `.xdb` upload (China-friendly)
- **User-Agent Parsing**: Built-in UA parser for browser, OS, and device type detection
- **Trend Analysis**: Hourly/daily/weekly/monthly access trends
- **Real-time Dashboard**: Embedded analytics dashboard in the admin panel
- **Background Aggregation**: Dedicated aggregation thread every 60 seconds
- **Workflow Integration**: `workflow_nodes` module for calling analytics data in workflows

## Architecture

### Database Strategy

The plugin uses a **dedicated database**, storing data in the PostgreSQL `analytics` schema.

### Module Structure

```
┌─────────────────────────────────────────────────┐
│                  middleware.py                    │
│        (request interception & raw capture)       │
└─────────────────────┬───────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────┐
│                   tracker.py                     │
│         (behavior tracking & event logging)       │
└─────────────────────┬───────────────────────────┘
          ┌───────────┼───────────┐
          ▼           ▼           ▼
┌──────────────┐ ┌──────────┐ ┌──────────────┐
│  ua_parser   │ │  geoip   │ │  models.py   │
│  (UA parsing)│ │(IP lookup)│ │ (11 tables)  │
└──────────────┘ └──────────┘ └──────┬───────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────┐
│                 processor.py                     │
│      (background aggregation / every 60s)        │
└─────────────────────┬───────────────────────────┘
          ┌───────────┼───────────┐
          ▼           ▼           ▼
┌──────────────┐ ┌──────────┐ ┌──────────────┐
│   routes     │ │   cli    │ │ workflow_    │
│ (dashboard   │ │ (command │ │ nodes        │
│  Blueprint)  │ │  line)   │ │ (workflow)   │
└──────────────┘ └──────────┘ └──────────────┘
```

### 11 Analytics Tables

The plugin maintains the following tables in the PostgreSQL `analytics` schema:

| Table | Purpose |
|------|------|
| `analytics_logs` | Raw access logs |
| `analytics_visitor_sessions` | Visitor session aggregation |
| `analytics_hourly_stats` | Hourly aggregated stats |
| `analytics_daily_stats` | Daily aggregated stats |
| `analytics_page_stats` | Page-level stats |
| `analytics_source_stats` | Referrer/source stats |
| `analytics_geo_stats` | Geolocation stats |
| `analytics_device_stats` | Device/browser/OS stats |
| `analytics_events` | Custom event records |
| `analytics_alerts` | Alert records |
| `analytics_privacy_config` | Privacy configuration |

## Directory Structure

```
analytics/
├── __init__.py              # Plugin entry, registers hooks and middleware
├── models.py                # 11 analytics table models
├── middleware.py            # Server-side cookie-less analytics middleware
├── processor.py             # Background aggregation thread (every 60s)
├── tracker.py               # Event tracker, records raw behavior data
├── geoip.py                 # IP geolocation (ip2region-based)
├── ua_parser.py             # User-Agent parser
├── routes.py                # Dashboard Blueprint routes (register_routes)
├── cli.py                   # Command-line tools
├── workflow_nodes.py        # Workflow engine integration nodes
├── migrate_analytics.py     # Database migration script
├── plugin.json              # Plugin metadata configuration
├── data/
│   └── ip2region_v4.xdb     # ip2region IP geolocation database
├── ip2region/
│   ├── __init__.py
│   ├── searcher.py          # ip2region search engine
│   └── util.py              # ip2region utilities
├── i18n/
│   ├── en.yml               # English internationalization
│   └── zh-CN.yml            # Simplified Chinese internationalization
├── migrations/
│   └── 001_initial.sql      # Database version migration (initial schema)
├── static/
│   ├── js/                  # Localized frontend dependencies (echarts/chart.js/tsparticles) and dashboard JS
│   ├── china.json           # China map data
│   └── world.json           # World map data
└── templates/
    └── analytics.html       # Admin dashboard template
```

## Installation & Activation

### Installation

The plugin is included in VeroRun's default plugin directory; no separate installation is required.

### Activation

1. Ensure the `analytics` schema exists in PostgreSQL
2. Run the database migration script:

```bash
python -m plugins.analytics.migrate_analytics
```

3. Enable the Analytics plugin in the VeroRun admin "Plugin Management" page
4. The middleware will automatically start intercepting requests and collecting data after activation

### Local Development

The plugin uses the PostgreSQL `analytics` schema; tables are created automatically on initialization (see `migrations/001_initial.sql`).

## Configuration

The following parameters are configured in `plugin.json`:

```json
{
  "name": "analytics",
  "version": "1.7.0",
  "database": {
    "type": "postgresql",
    "schema": "analytics"
  },
  "aggregation": {
    "interval_seconds": 60
  },
  "middleware": {
    "enabled": true,
    "exclude_paths": ["/admin/*", "/static/*", "/api/health"]
  },
  "ip2region": {
    "db_path": "data/ip2region_v4.xdb"
  }
}
```

| Config Key | Description | Default |
|--------|------|--------|
| `database.schema` | PostgreSQL schema name | `analytics` |
| `aggregation.interval_seconds` | Aggregation thread interval (seconds) | `60` |
| `middleware.enabled` | Whether the middleware is enabled | `true` |
| `middleware.exclude_paths` | Excluded path patterns | Admin panel and static assets |
| `ip2region.db_path` | ip2region database file path | `data/ip2region_v4.xdb` |

## API Endpoints

### Hooks Provided

| Hook Identifier | Type | Description |
|-------------|------|------|
| `analytics/track_event` | Hook | Manually record custom analytics events |
| `analytics/get_realtime` | Hook | Fetch real-time analytics (online visitors, today's PV/UV) |
| `analytics/get_trend` | Hook | Fetch analytics trends for a specified time range |

### Admin Panel

| Path | Description |
|------|------|
| `/admin/analytics/` | Analytics dashboard (embedded page) |

### Filters Registered

| Filter Identifier | Description |
|---------------|------|
| `dashboard.data` | Module-level registration; injects analytics summaries into the admin dashboard |

## Dependencies

### Internal Dependencies

- VeroRun core framework: middleware registration, hook system, event bus
- Admin panel (auth-center): dashboard embedding and menu rendering

### External Dependencies

- **ip2region**: IP geolocation library using the offline `data/ip2region_v4.xdb` database
- **PostgreSQL**: production data storage (`analytics` schema)

### Dependents

- **health_check** plugin: fetches access trends via the `analytics/get_trend` hook for health analysis
- **Workflow engine**: calls analytics data via `workflow_nodes.py`

### Menu

- **Menu group**: `Monitoring & Data`
- **Embedded URL**: `/admin/analytics/`

## Changelog

### v1.7.0 (2026-08-18)

**System style integration**

- **Rendered natively in the admin shell**: the Analytics dashboard no longer loads in an isolated iframe. `l_analytics()` now injects a plain HTML fragment directly into the main content area, fully inheriting the system component styles (`.cd/.st/.ai-tab-bar/.ai-tab/.btn/.in/.sl/.ta/.bdg/.modal-box/.toast`).
- **Removed the standalone style**: deleted the custom neon theme (particles, scanline, hard-coded colors); the dashboard now uses system CSS variables end-to-end.
- **Frontend hardening**: event bindings centralized into `initAnalyticsDashboard()` (safe re-entry on every navigation), toast renamed to `analyticsToast` to avoid polluting the admin global scope; the full standalone page still works as a fallback.

### v1.6.2 (2026-08-18)

**Stability & UX fixes**

- **Self-healing PG schema**: the plugin now idempotently adds missing columns/tables (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`) for all 11 tables at startup/first use, fixing dashboard API 500s on servers upgraded from older versions.
- **JSON error responses**: dashboard/API exceptions now return JSON and are logged to `data/logs/plugins/analytics.log`, instead of the default HTML 500 page that broke the frontend JSON parser.
- **Frontend hardening**: `api()` now guards against non-JSON/HTTP errors and falls back to the parent page JWT token (fixes first-frame 401s in the srcdoc iframe).
- **ip2region offline upload**: Settings → GeoIP now has an "Upload .xdb" button to install the ip2region database locally (China-friendly, no GitHub/Gitee access required).
- **Style alignment**: the dashboard now uses the system design-system variables; removed the custom particle/scanline neon decorations.

### v1.5.2 (2026-08-06)

**Bug fix: analytics dashboard self-polling counted as PV**

The dashboard's own `/admin/analytics/api/v1/*` polling endpoints were being recorded as page views (~1300 requests/hour while the dashboard is open), inflating PV/sessions.

- Added `/admin/analytics/*` to the default `exclude_paths` so the dashboard's own API traffic is no longer tracked.
- If the database already has a privacy config row, update `analytics_privacy_config.exclude_paths` manually and restart the service (existing rows are not overwritten on re-init).

### v1.5.1 (2026-08-06)

**Bug fix: aggregation inflation**

Fixed a critical bug where the background aggregation thread re-added the entire current hour's statistics every 60 seconds, inflating PV, session counts and other metrics by up to ~50-60x.

- `upsert_hourly`, `upsert_page_stat`, `upsert_source`, `upsert_geo`, `upsert_device` now use **overwrite semantics** instead of additive: each run fully recomputes the aggregation window so repeated runs converge to the true value.
- Page/source/geo/device statistics are now aggregated at the **daily level** (full-day recompute + overwrite) instead of the hourly pass.
- `AnalyticsProcessor._aggregate_daily()` accepts an optional `date_str` parameter for historical recomputation.

> **Upgrade note**: statistics accumulated before v1.5.1 are inflated. After upgrading, clear the aggregation tables (`analytics_hourly_stats`, `analytics_daily_stats`, `analytics_page_stats`, `analytics_source_stats`, `analytics_geo_stats`, `analytics_device_stats`) so real data rebuilds going forward. Raw logs are preserved.

## License

This plugin is part of the VeroRun project and follows the overall license of the VeroRun project.
