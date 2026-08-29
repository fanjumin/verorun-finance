# ali_api (1688 Supply Chain Sourcing)

## Overview

The **ali_api** plugin integrates the 1688 (Alibaba China) wholesale marketplace into the VeroRun platform. It enables merchants to search for products on 1688, apply AI-powered optimization to listings, and publish them to local marketplaces. The plugin also automates purchase order creation when orders are paid, streamlining the dropshipping and sourcing workflow.

The plugin manages its own independent SQLite database (`ali_api.db`) and includes a comprehensive service layer with caching, rate limiting, image search, and review capabilities.

| Property    | Value                    |
|-------------|--------------------------|
| Identifier  | `ali_api`                |
| Version     | 2.1.0                    |
| Database    | `ali_api.db`             |
| Menu Group  | Business Center          |
| Embed URL   | `/admin/ali-api/`        |

---

## Features

- **1688 Product Search** -- Search the 1688 marketplace by keyword, category, or image with full product details.
- **AI-Powered Optimization** -- Automatically optimize product titles, descriptions, and images for local marketplaces using AI.
- **Local Marketplace Publishing** -- Push optimized products directly to the VeroRun storefront.
- **Auto Purchase Order Creation** -- Listens to the `ORDER_PAID` event and automatically creates corresponding purchase orders on 1688.
- **Image Search** -- Reverse image search on 1688 to find identical or similar products.
- **Caching & Rate Limiting** -- Built-in caching layer and rate limiter to prevent API abuse and improve performance.
- **Review Service** -- Manage and moderate product reviews from sourced items.

---

## Architecture

The plugin follows a service-oriented architecture with a clear separation of concerns:

```
ali_api/
  __init__.py              -- Plugin entry point (AliApiPlugin)
  models.py                -- Data layer (ORM models, init_ali_api_db)
  routes/
    admin.py               -- Admin routes (ali_admin_bp)
  services/
    alibaba_client.py      -- 1688 API client (v1)
    alibaba_client_v2.py   -- 1688 API client (v2, enhanced)
    ai_processor.py        -- AI product optimization engine
    image_search_service.py -- Reverse image search logic
    cache_service.py       -- Response caching layer
    rate_limiter.py        -- API rate limiting
    review_service.py      -- Product review management
```

**Data Flow:**
1. Merchants search 1688 via the admin interface.
2. The `alibaba_client` queries the 1688 API (rate-limited and cached).
3. Results are displayed in the embedded admin panel.
4. AI processor optimizes selected product listings.
5. Optimized products are published to the local storefront.
6. When an order is paid, the plugin auto-creates a purchase order on 1688.

---

## Directory Structure

```
plugins/ali_api/
  __init__.py
  models.py
  routes/
    admin.py
  services/
    alibaba_client.py
    alibaba_client_v2.py
    ai_processor.py
    image_search_service.py
    cache_service.py
    rate_limiter.py
    review_service.py
  README.en.md
```

---

## Installation & Activation

1. Ensure the `ali_api/` directory is present under `plugins/`.
2. The plugin is auto-discovered by the VeroRun plugin loader.
3. Verify activation in the admin panel under **Plugins**.
4. The database `ali_api.db` is automatically initialized on first load.
5. Configure 1688 API credentials in the plugin settings.

No additional system dependencies are required beyond the core VeroRun platform.

---

## Configuration

Configuration is managed through the plugin settings panel. Required credentials for the 1688 API must be provided:

| Key                  | Type   | Required | Description                            |
|----------------------|--------|----------|----------------------------------------|
| `alibaba_app_key`    | string | Yes      | 1688 Open Platform application key     |
| `alibaba_app_secret` | string | Yes      | 1688 Open Platform application secret  |
| `alibaba_access_token` | string | Yes    | 1688 OAuth access token                |

Additional configuration options for rate limiting and caching are available in the service modules.

---

## API Endpoints & Hooks

### Hooks Listened

| Hook          | Description                                                 |
|---------------|-------------------------------------------------------------|
| `ORDER_PAID`  | Triggers automatic purchase order creation on 1688          |

### Hooks Provided

This plugin does not provide hooks for external consumption. All functionality is exposed via admin routes and the embedded panel.

### Admin Routes

- `GET  /admin/ali-api/` -- Embedded admin panel (search, product management)
- `POST /admin/ali-api/search` -- Search 1688 products
- `POST /admin/ali-api/optimize` -- AI-optimize a product listing
- `POST /admin/ali-api/publish` -- Publish optimized product to storefront
- `POST /admin/ali-api/image-search` -- Reverse image search on 1688

---

## Dependencies

This plugin has no external third-party Python dependencies. It relies on:

- VeroRun core (hook system, plugin loader, template engine)
- SQLite (via VeroRun's database abstraction layer)
- 1688 Open Platform API (external HTTP service)

---

## Permissions

| Permission             | Description                              |
|------------------------|------------------------------------------|
| `network.request`      | Make outbound HTTP requests to 1688 API  |
| `shop.product.write`   | Create and publish products to the store |

---

## License

This plugin is part of the VeroRun platform and is distributed under the same license as the core platform.