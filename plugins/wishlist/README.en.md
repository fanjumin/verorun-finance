# Wishlist (Favorites/Wishlist)

## Overview

The **wishlist** plugin provides product favorites and wishlist management for the VeroRun platform. It allows users to save products they are interested in for later purchase. The plugin is lightweight, with routes defined inline within the `__init__.py` module as a Blueprint.

The plugin manages its own independent PostgreSQL schema (`wishlist`) while performing read-only cross-reads from the main VeroRun database for product information.

| Property    | Value                         |
|-------------|-------------------------------|
| Identifier  | `wishlist`                    |
| Version     | 1.2.0                         |
| Database    | PostgreSQL schema `wishlist`  |
| Menu Group  | None (no admin menu)          |

---

## Features

- **Product Favorites** -- Users can add products to their wishlist with a single toggle action.
- **Wishlist Management** -- View, add, and remove items from the wishlist via simple API endpoints.
- **Toggle Functionality** -- The `POST /api/toggle` endpoint adds or removes a product in one call.
- **Bulk Check** -- The `POST /api/check` endpoint allows checking wishlist status for multiple products at once.
- **Item Count** -- The `GET /api/count` endpoint returns the total number of wishlist items for the current user.
- **Cross-Schema Reads** -- Reads product information read-only from the main VeroRun database (`public.products`) while storing wishlist data in the `wishlist` schema.

---

## Architecture

The plugin defines all routes inline within the `__init__.py` module as a Blueprint:

```
wishlist/
  __init__.py    -- Plugin entry point, Blueprint definition, and all routes
  models.py      -- Data layer (PG schema access, table initialization)
  migrations/    -- Versioned schema migration scripts
```

**Data Flow:**
1. User browses a product and toggles it into their wishlist.
2. The wishlist entry is stored in the `wishlist` PostgreSQL schema.
3. Product details are cross-read (read-only) from the main VeroRun database.
4. The `wishlist/sync` hook allows other plugins to synchronize wishlist data.
5. Users can view their wishlist and remove items at any time.

---

## Directory Structure

```
plugins/wishlist/
  __init__.py
  models.py
  migrations/v1.0.0_init.sql
  i18n/en.yml
  i18n/zh-CN.yml
  README.en.md
  README_CN.md
```

---

## Installation & Activation

1. Ensure the `wishlist/` directory is present under `plugins/`.
2. The plugin is auto-discovered by the VeroRun plugin loader.
3. Verify activation in the admin panel under **Plugins**.
4. The `wishlist` PostgreSQL schema is automatically initialized on first load (`init_db()`).

No additional dependencies are required beyond the core VeroRun platform.

---

## Configuration

The plugin exposes a `max_page_size` setting (default 50) via `settings_schema`, adjustable through the admin panel.

Permissions (plugin standard §10.2):

| Permission    | Description                                             |
|---------------|---------------------------------------------------------|
| `api:read`    | Read business data (products, wishlist items)           |
| `user:profile`| Read basic user identity from JWT                       |

---

## API Endpoints & Hooks

### Hooks Provided

| Hook              | Description                                              |
|-------------------|----------------------------------------------------------|
| `wishlist/sync`   | Synchronize wishlist data (used by other plugins)        |

### API Endpoints (Authenticated)

| Method | Endpoint          | Description                                              |
|--------|-------------------|----------------------------------------------------------|
| GET    | `/api/list`       | List all wishlist items for the current user             |
| POST   | `/api/toggle`     | Toggle (add/remove) a product in the wishlist            |
| POST   | `/api/check`      | Check wishlist status for one or more products           |
| GET    | `/api/count`      | Get the total count of wishlist items for the current user|

### Admin Routes

This plugin does not register any admin routes.

---

## Dependencies

This plugin has no external third-party dependencies. It relies on:

- VeroRun core (plugin loader, JWT service, i18n module)
- PostgreSQL (via `plugins/_base/db.py` `get_raw_connection()`)
- Main VeroRun database (read-only cross-reads for product information)

---

## Dashboard Stats

The plugin exposes dashboard statistics via `get_dashboard_stats()`:

| Key               | Description                  | Type    |
|-------------------|------------------------------|---------|
| `total_favorites` | Total number of favorites    | counter |
| `active_users`    | Users with favorites         | counter |

---

## License

This plugin is part of the VeroRun platform and is distributed under the same license as the core platform.
