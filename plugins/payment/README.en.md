# payment (Payment Gateway)

## Overview

The **payment** plugin is the unified payment gateway management module for the VeroRun platform. It provides centralized configuration and management for multiple payment providers including Alipay, WeChat Pay, Stripe, and PayPal. The plugin handles payment creation, confirmation, and notification verification through a consistent hook-based interface.

The plugin manages its own independent PostgreSQL schema `payment` with tables for `payment_logs` and `payment_configs` (connected via `plugins/_base/db.py` `get_raw_connection()`). It also supports migration from the main VeroRun database via the `migrate_from_main_db()` method.

| Property    | Value                |
|-------------|----------------------|
| Identifier  | `payment`            |
| Version     | 1.1.0                |
| Database    | PG schema `payment`  |
| Menu Group  | Business Center      |
| Menu Key    | `payment`            |

---

## Features

- **Multi-Provider Support** -- Manage configurations for Alipay, WeChat Pay, Stripe, and PayPal from a single admin interface.
- **Payment Lifecycle Hooks** -- Standardized hooks for creating payments, confirming payments, and verifying asynchronous notifications.
- **Payment Logging** -- All payment events are logged to `payment_logs` for audit and debugging.
- **Configuration Management** -- Store and manage provider-specific configurations (API keys, merchant IDs, webhook secrets) in `payment_configs`.
- **Database Migration** -- The `migrate_from_main_db()` method supports migrating payment data from the main VeroRun database.
- **Admin Dashboard** -- A dedicated admin panel for managing payment configurations and viewing payment logs.

---

## Architecture

The plugin follows a clean service-oriented architecture:

```
payment/
  __init__.py     -- Plugin entry point (PaymentPlugin)
  models.py       -- Data layer (PG schema, payment_logs, payment_configs)
  services.py     -- Payment processing logic (delegated to auth-center)
  migrations/     -- Schema migration scripts (§10.6)
  routes/
    admin.py      -- Admin routes (payment_admin_bp)
```

**Data Flow:**
1. Admin configures payment providers via the admin panel.
2. Configurations are stored in `payment_configs` table.
3. Other plugins call `payment/create` hook to initiate a payment.
4. `services.py` processes the payment through the appropriate provider.
5. Payment events are logged to `payment_logs`.
6. The `payment/confirm` hook finalizes the payment.
7. The `payment/verify_notify` hook handles asynchronous callbacks from providers.

---

## Directory Structure

```
plugins/payment/
  __init__.py
  models.py
  services.py
  migrations/
    v1.0.0_baseline.sql
  routes/
    admin.py
  README.en.md
```

---

## Installation & Activation

1. Ensure the `payment/` directory is present under `plugins/`.
2. The plugin is auto-discovered by the VeroRun plugin loader.
3. Verify activation in the admin panel under **Plugins**.
4. The PostgreSQL schema `payment` is automatically initialized on first load.
5. If migrating from the main database, the `migrate_from_main_db()` method is called automatically.
6. Configure payment provider credentials in the admin panel.

---

## Configuration

Configuration is managed through the admin panel. Each payment provider requires its own set of credentials:

| Provider     | Key                  | Description                          |
|--------------|----------------------|--------------------------------------|
| Alipay       | `alipay_app_id`      | Alipay application ID                |
| Alipay       | `alipay_private_key` | Alipay merchant private key          |
| Alipay       | `alipay_public_key`  | Alipay public key                    |
| WeChat Pay   | `wechat_app_id`      | WeChat Pay application ID            |
| WeChat Pay   | `wechat_mch_id`      | WeChat Pay merchant ID               |
| WeChat Pay   | `wechat_api_key`     | WeChat Pay API v2 key                |
| Stripe       | `stripe_secret_key`  | Stripe secret API key                |
| Stripe       | `stripe_webhook_secret` | Stripe webhook signing secret     |
| PayPal       | `paypal_client_id`   | PayPal REST API client ID            |
| PayPal       | `paypal_secret`      | PayPal REST API secret               |

---

## API Endpoints & Hooks

### Hooks Provided

| Hook                     | Description                                              |
|--------------------------|----------------------------------------------------------|
| `payment/create`         | Create a new payment with the specified provider         |
| `payment/confirm`        | Confirm and finalize a payment                           |
| `payment/verify_notify`  | Verify an asynchronous notification from a provider      |

### Hooks Listened

This plugin does not listen to any external hooks.

### Admin Routes

- `GET  /admin/payment/` -- Admin dashboard (payment configurations, logs)
- `POST /admin/payment/config` -- Create or update payment provider configuration
- `GET  /admin/payment/logs` -- View payment logs

### Public Routes

- `POST /api/payment/notify/alipay` -- Alipay asynchronous notification endpoint
- `POST /api/payment/notify/wechat` -- WeChat Pay asynchronous notification endpoint
- `POST /api/payment/notify/stripe` -- Stripe webhook endpoint
- `POST /api/payment/notify/paypal` -- PayPal webhook endpoint

---

## Dependencies

This plugin has no external third-party Python dependencies. It relies on:

- VeroRun core (hook system, plugin loader, template engine)
- PostgreSQL (via `plugins/_base/db.py` `get_raw_connection()` factory)
- External payment provider APIs (Alipay, WeChat Pay, Stripe, PayPal)

---

## Permissions

| Permission       | Description                              |
|------------------|------------------------------------------|
| `api:read`       | Read payment configurations and logs     |
| `api:write`      | Write payment configurations             |
| `admin:access`   | Access the admin payment page            |

---

## License

This plugin is part of the VeroRun platform and is distributed under the same license as the core platform.