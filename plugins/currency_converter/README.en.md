# currency_converter (Currency Converter)

## Overview

The **currency_converter** plugin provides multi-currency support for the VeroRun platform. It enables real-time currency conversion, automatic GeoIP-based currency detection, and user currency preferences. The plugin displays prices in the user's preferred currency across the storefront and syncs exchange rates periodically via a scheduled job.

The plugin stores its data in a dedicated PostgreSQL schema (`currency_converter`) on the VeroRun instance (single-database, multi-schema isolation) and is not enabled by default. Administrators must explicitly enable it before it becomes active.

| Property    | Value                                   |
|-------------|-----------------------------------------|
| Identifier  | `currency_converter`                    |
| Version     | 1.1.0                                   |
| Database    | PostgreSQL schema: `currency_converter` |
| Menu Group  | Business Center                         |
| Embed URL   | `/admin/currency/`                      |
| Default     | Disabled until admin enables             |

---

## Features

- **Multi-Currency Display** -- Show product prices in any of 15 supported currencies across the entire storefront.
- **Real-Time Exchange Rates** -- Periodically sync exchange rates from an external provider via a scheduled job.
- **User Preference** -- Authenticated users can set their preferred currency, persisted to the database.
- **GeoIP Auto-Detection** -- Automatically detect new visitors' country and suggest the appropriate currency on first visit.
- **Automatic Conversion** -- The `currency/convert` hook lets other plugins convert amounts on demand.
- **Frontend Widget** -- A `currency_widget.js` script provides a currency selector UI for the storefront.
- **Scheduled Rate Sync** -- The `register_jobs()` method registers a periodic job to refresh exchange rates.

---

## Architecture

The plugin follows a modular architecture with a background scheduler:

```
currency_converter/
  __init__.py            -- Plugin entry point (CurrencyConverterPlugin)
  models.py              -- Data layer (ORM models, rate storage)
  routes.py              -- Web layer (currency_bp Blueprint)
  services.py            -- Currency conversion and rate fetching logic
  scheduler.py           -- Scheduled job for periodic rate sync
  static/
    currency_widget.js   -- Frontend currency selector widget
```

**Data Flow:**
1. `scheduler.py` periodically fetches latest exchange rates from the provider.
2. Rates are stored in the `currency_converter` PostgreSQL schema.
3. `services.py` handles conversion requests using cached rates.
4. On user login/registration, GeoIP detection suggests a currency.
5. The frontend widget lets users manually switch currencies.
6. Product prices are converted on-the-fly based on the active currency.

---

## Directory Structure

```
plugins/currency_converter/
  __init__.py
  models.py
  routes.py
  services.py
  scheduler.py
  plugin.json
  static/
    currency_widget.js
  templates/
    admin_currency.html
  i18n/
    en.yml
    zh-CN.yml
```

---

## Installation & Activation

1. Ensure the `currency_converter/` directory is present under `plugins/`.
2. The plugin is auto-discovered by the VeroRun plugin loader.
3. The plugin is not enabled by default. Enable it in the admin panel under **Plugins**.
4. The `currency_converter` PostgreSQL schema is initialized automatically when the plugin is enabled.
5. Configure an exchange rate provider API key in the plugin settings.

---

## Configuration

| Key                        | Type    | Default | Description                                                  |
|----------------------------|---------|---------|--------------------------------------------------------------|
| `base_currency`            | string  | `CNY`   | The base currency for all conversions                        |
| `refresh_interval_minutes`  | integer | 60      | How often to refresh exchange rates (in minutes)             |
| `cache_ttl_minutes`        | integer | 60      | Cache time-to-live for converted amounts (in minutes)        |
| `default_currency`         | string  | `CNY`   | Default currency for users without a preference              |
| `enable_geoip`             | boolean | true    | Enable GeoIP-based currency auto-detection                   |

### Supported Currencies

The plugin supports 15 currencies out of the box, configurable via the admin panel.

---

## API Endpoints & Hooks

### Hooks Provided

| Hook                   | Description                                              |
|------------------------|----------------------------------------------------------|
| `currency/convert`     | Convert an amount from one currency to another           |
| `currency/rates`       | Get current exchange rates for all supported currencies  |
| `currency/preference`  | Get or set a user's currency preference                  |

### Hooks Listened

| Hook               | Description                                              |
|--------------------|----------------------------------------------------------|
| `user/login`       | Detect user's currency preference or suggest via GeoIP   |
| `user/registered`  | Set initial currency preference for new users            |

### Admin Routes

- `GET  /admin/currency/` -- Embedded admin panel (rate management, configuration)

### Public Routes

- `GET  /api/currency/rates` -- Get current exchange rates
- `GET  /api/currency/convert` -- Convert an amount
- `POST /api/currency/preference` -- Set user currency preference

### Scheduled Jobs

The `register_jobs()` method registers a periodic exchange rate sync job that runs every `refresh_interval_minutes` minutes.

---

## Dependencies

This plugin has no external third-party Python dependencies. It relies on:

- VeroRun core (hook system, plugin loader, template engine, job scheduler)
- PostgreSQL (dedicated schema `currency_converter`, via VeroRun's database abstraction layer)
- External exchange rate API (configurable provider)

---

## Permissions

The plugin declares the following permissions in `plugin.json`:

| Permission          | Description                                              |
|---------------------|----------------------------------------------------------|
| `api:read`          | Read business data                                       |
| `user:profile`      | Read/write user currency preferences                     |
| `network:request`   | Make outbound HTTP requests to exchange-rate APIs        |

## Dashboard Stats

The `dashboard.stats` declaration in `plugin.json` exposes the following metrics to the admin dashboard (§2.3):

| Key              | Title          | Type    | Description                                    |
|------------------|----------------|---------|------------------------------------------------|
| `currency_rates` | Currency Rates | counter | Number of exchange-rate currencies synced      |
| `last_sync`      | Last Sync      | gauge   | Unix timestamp (seconds) of the last rate sync |

---

## License

This plugin is part of the VeroRun platform and is distributed under the same license as the core platform.