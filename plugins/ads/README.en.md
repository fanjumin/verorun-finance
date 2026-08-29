# ads (Ad Management)

## Overview

The **ads** plugin is the central ad management module for the VeroRun platform. It provides a complete system for creating, placing, and tracking advertisements across the site. With built-in AI-Ready APIs, the plugin enables intelligent ad placement and optimization, making it suitable for both manual and automated advertising workflows.

The plugin stores its data in the main PostgreSQL database under a dedicated `ads` schema (four tables: `ad_placements`, `ad_zones`, `ad_stats`, `ad_clicks`), and integrates with the platform's plugin manager for lifecycle management.

| Property    | Value             |
|-------------|-------------------|
| Identifier  | `ads`             |
| Version     | 1.2.0             |
| Database    | PostgreSQL, schema `ads` |
| Menu Group  | AI & Content      |
| Menu Key    | `ads`             |

---

## Features

- **Ad Placement Management** -- Create, update, and delete ad placements with configurable dimensions, positions, targeting rules, scheduling, weight, and frequency cap.
- **Zone Management** -- Group placements into named zones (`ad_zones`), with reference checks before zone deletion.
- **Impression & Click Statistics** -- Track every ad impression and click (aggregate counters + daily stats + sampled click details).
- **AI-Ready APIs** -- `ai_tools.py` exposes a standardized interface for AI agents (list/get/create/update/delete, stats, analysis, snippet generation).
- **Template Rendering** -- Server-side rendering of ad units via Jinja2 macros (`render_ads.html`) with async client rendering (`ads.js`).
- **Admin Dashboard** -- A dedicated admin panel (`admin_ads.html`) for managing placements, zones, reviewing statistics, and configuring ad parameters.
- **Multi-site Support** -- `site_key`/`site_domains` based multi-tenant targeting.
- **i18n** -- English and Simplified Chinese translation files under `i18n/`.

---

## Architecture

The plugin follows a layered architecture:

```
ads/
  __init__.py       -- Plugin entry point (AdsPlugin class, lifecycle)
  models.py         -- Data layer (connection mgmt, schema init, shared CRUD, stats)
  routes.py         -- Web layer (ads_bp Blueprint, admin & public routes)
  ai_tools.py       -- AI integration layer (AI function tools)
  templates/
    admin_ads.html  -- Admin dashboard template (inline JS)
    render_ads.html -- Ad unit rendering macros
  static/
    ads.js          -- Frontend ad rendering and tracking
  i18n/
    en.yml          -- English translations
    zh-CN.yml       -- Simplified Chinese translations
```

**Database (PostgreSQL `ads` schema):**

| Table          | Purpose                                                   |
|----------------|-----------------------------------------------------------|
| `ad_placements`| Ad placements (name, site_key, zone, position, page, type, targeting, schedule, weight, freq_cap, counters) |
| `ad_zones`     | Ad placement zones (site_key, identifier, size, status)   |
| `ad_stats`     | Daily impressions/clicks per ad (unique `(ad_id, stat_date)`) |
| `ad_clicks`    | Sampled click details (hashed IP, user-agent, referrer) for fraud review |

**Data Flow:**
1. Admins create/configure placements and zones via the admin dashboard.
2. Placements are stored in the `ads` schema.
3. `render_ads.html` macros emit ad slots; `ads.js` fetches active ads via the public API and renders them.
4. `ads.js` records impressions/clicks via public tracking endpoints (rate-limited per IP).
5. Statistics are aggregated in the admin panel; AI tools read them for automated optimization.

---

## Directory Structure

```
plugins/ads/
  __init__.py
  models.py
  routes.py
  ai_tools.py
  plugin.json
  README.en.md
  README_CN.md
  templates/
    admin_ads.html
    render_ads.html
  static/
    ads.js
  i18n/
    en.yml
    zh-CN.yml
```

---

## Installation & Activation

1. Ensure the `ads/` directory is present under `plugins/`.
2. The plugin is auto-discovered by the VeroRun plugin loader.
3. Verify activation in the admin panel under **Plugins**.
4. The `ads` schema and tables are created idempotently on first load by `models.init_ad_db()` (uses `IF NOT EXISTS` + dynamic column additions for smooth upgrades).

Database connectivity uses the platform's shared PG credentials (`PG_HOST` / `PG_PORT` / `PG_DB` / `PG_USER` / `PG_PASSWORD` environment variables). Connections are managed per-thread with liveness checks (gunicorn pre-fork compatible).

---

## Configuration

The following configuration keys are available in the plugin's configuration:

| Key               | Type    | Default | Description                                        |
|-------------------|---------|---------|----------------------------------------------------|
| `default_width`   | integer | 320     | Default width (in pixels) for new ad placements    |
| `default_height`  | integer | 0       | Default height (in pixels). 0 means auto-height.   |
| `max_placements`  | integer | 50      | Maximum number of concurrent ad placements allowed |

Configuration can be set via the admin panel (**Ad Statistics** tab > **Settings**) or `plugin.json`.

---

## API Endpoints & Hooks

### Hooks Provided

| Hook                        | Description                                      |
|-----------------------------|--------------------------------------------------|
| `ads/get_placements`        | Retrieve ad placements (via AI tools)            |
| `ads/render_ad`             | Render an ad unit as HTML                        |
| `ads/get_stats`             | Get impression and click statistics              |
| `ads/record_impression`     | Record an ad impression event                    |
| `ads/record_click`          | Record an ad click event                         |

### Hooks Listened

This plugin does not listen to any external hooks.

### Admin Routes (require admin auth via `_require_admin`)

- `GET    /admin/ads/`        -- List placements (paginated)
- `POST   /admin/ads/`        -- Create a placement
- `PUT    /admin/ads/<id>`    -- Update a placement (dynamic fields)
- `DELETE /admin/ads/<id>`    -- Delete a placement (cascades stats/clicks)
- `GET|POST /admin/ads/zones` -- List / create zones
- `PUT|DELETE /admin/ads/zones/<id>` -- Update / delete a zone (delete blocked while referenced)
- `GET    /admin/ads/api/v1/stats` -- Stats query (admin only)
- `GET|POST /admin/ads/settings`   -- Plugin settings

### Public Routes

- `GET  /admin/ads/api/v1/ads?page=&position=&site_key=&zone_id=&limit=` -- Active ads for client rendering (limit default 50, max 200)
- `POST /admin/ads/api/v1/stats/impression` -- Record impression (rate-limited: 60/min/IP)
- `POST /admin/ads/api/v1/stats/click`      -- Record click (rate-limited: 30/min/IP)

---

## Dependencies

- VeroRun core (plugin manager, `i18n`, template engine)
- `psycopg2` (PostgreSQL driver, part of the platform dependencies)
- Shared DB factory `plugins/_base/db.get_raw_connection()`

---

## Permissions

| Permission     | Description                                     |
|----------------|-------------------------------------------------|
| `api:read`     | View ad placements and statistics               |
| `api:write`    | Create, update, and delete ad placements        |
| `admin:access` | Access the plugin admin panel (admin dashboard) |

> Note: Access to the admin routes is enforced through the main system's admin authentication (`_require_admin`).

---

## Privacy Notes

- Click records store a **SHA-256 hash** of the client IP (not the raw IP) to reduce PII exposure while still allowing same-source fraud detection.
- If you serve EU users, document ad click data collection in your privacy policy and provide a deletion mechanism, per GDPR.

---

## License

This plugin is part of the VeroRun platform and is distributed under the same license as the core platform.
