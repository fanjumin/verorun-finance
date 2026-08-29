# logistics (Logistics Express)

## Overview

The **logistics** plugin provides express shipment tracking for the VeroRun platform. It integrates with the Kdniao (快递鸟) API to support tracking across 600+ carriers worldwide. The plugin offers both a query interface for admins and hook-based tracking for automated order workflows.

The plugin manages its own independent PostgreSQL schema (`logistics`) and requires API credentials from Kdniao.

| Property    | Value                |
|-------------|----------------------|
| Identifier  | `logistics`          |
| Version     | 0.2.0                |
| Database    | `logistics` (PG schema) |
| Menu Group  | Business Center      |
| Menu Key    | `logistics`          |

---

## Features

- **Kdniao API Integration** -- Full integration with the Kdniao (快递鸟) express tracking API, supporting 600+ domestic and international carriers.
- **Shipment Tracking** -- Query real-time shipment status with detailed tracking events and timestamps.
- **Status Text Mapping** -- Convert raw carrier status codes into human-readable status text for display.
- **Hook-Based Tracking** -- Other plugins can query tracking information via the `logistics/query_track` hook.
- **Admin Dashboard** -- View and manage all tracked shipments from the admin panel.
- **Environment Variable Support** -- Credentials can be configured via environment variables for security in production.

---

## Architecture

The plugin follows a straightforward architecture:

```
logistics/
  __init__.py    -- Plugin entry point (LogisticsPlugin)
  models.py      -- Data layer (ORM models, tracking records)
  routes.py      -- Web layer (logistics_bp Blueprint)
  services.py    -- Kdniao API integration and tracking logic
```

**Data Flow:**
1. Admins or automated workflows query a tracking number.
2. `services.py` calls the Kdniao API with the configured credentials.
3. The tracking result is parsed and stored in the `logistics` schema.
4. Results are returned via the hook or displayed in the admin panel.
5. Status text is mapped via `logistics/get_shipping_status_text`.

---

## Directory Structure

```
plugins/logistics/
  __init__.py
  models.py
  routes.py
  services.py
  README.en.md
```

---

## Installation & Activation

1. Ensure the `logistics/` directory is present under `plugins/`.
2. The plugin is auto-discovered by the VeroRun plugin loader.
3. Verify activation in the admin panel under **Plugins**.
4. The `logistics` schema is automatically initialized on first load (via `CREATE SCHEMA IF NOT EXISTS` + `CREATE TABLE IF NOT EXISTS`).
5. **Required:** Configure Kdniao API credentials before use.

---

## Configuration

The following configuration keys are required for operation:

| Key             | Type   | Env Variable Override    | Description                        |
|-----------------|--------|--------------------------|------------------------------------|
| `kdniao_eid`    | string | `KDNIAO_EBUSINESS_ID`    | Kdniao e-business ID (EID)         |
| `kdniao_api_key`| string | `KDNIAO_API_KEY`         | Kdniao API key for authentication  |

For production environments, it is recommended to use environment variable overrides rather than storing credentials in the configuration file.

---

## API Endpoints & Hooks

### Hooks Provided

| Hook                               | Description                                              |
|------------------------------------|----------------------------------------------------------|
| `logistics/query_track`            | Query tracking information for a given tracking number   |
| `logistics/get_shipping_status_text`| Convert a carrier status code to human-readable text    |

### Hooks Listened

This plugin does not listen to any external hooks.

### Admin Routes

- `GET  /admin/logistics/` -- Admin dashboard (tracking query, shipment list)
- `POST /admin/logistics/query` -- Query a tracking number
- `GET  /admin/logistics/history` -- View tracking query history

### Public Routes

- `GET  /api/logistics/track` -- Public tracking lookup by tracking number

---

## Dependencies

This plugin has no external third-party Python dependencies. It relies on:

- VeroRun core (hook system, plugin loader, template engine)
- SQLite (via VeroRun's database abstraction layer)
- Kdniao (快递鸟) API (external HTTP service)

---

## Permissions

| Permission          | Description                          |
|---------------------|--------------------------------------|
| `logistics.query`   | Query shipment tracking information  |
| `network:request`   | External API call to Kdniao          |
| `admin:access`      | Admin panel access for logistics     |

---

## License

This plugin is part of the VeroRun platform and is distributed under the same license as the core platform.