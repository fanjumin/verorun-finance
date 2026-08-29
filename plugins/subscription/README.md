# subscription (Unified Subscription Management)

## Overview

The **subscription** plugin provides a unified subscription management system for the VeroRun platform. It implements a pay-as-you-go feature marketplace with per-SKU billing, enabling merchants to monetize premium features. The plugin supports dual-environment payment routing: Alipay/WeChat Pay for the Chinese (CN) market and Stripe/PayPal for international (INTL) customers.

The plugin includes scheduled jobs for subscription expiry checks and auto-renewal, and uses dedicated gateway modules for each supported payment provider.

| Property    | Value                              |
|-------------|------------------------------------|
| Identifier  | `subscription`                     |
| Version     | 1.1.0                              |
| Database    | Main DB `subscription` schema (PostgreSQL) |
| Menu Group  | Business Center (admin)             |

---

## Features

- **Feature Marketplace** -- Define and sell premium features as subscription SKUs with per-SKU billing.
- **Pay-as-You-Go** -- Flexible billing model where customers pay only for the features they need.
- **Dual-Environment Routing** -- Automatic payment routing: CN users get Alipay/WeChat Pay; INTL users get Stripe/PayPal.
- **Multi-Gateway Support** -- Dedicated gateway modules for Alipay, WeChat Pay, Stripe, and PayPal.
- **Subscription Lifecycle** -- Full lifecycle management: subscribe, cancel, renew, and check subscription status.
- **Scheduled Jobs** -- Expiry checks and auto-renewal retries registered via `register_jobs()`.
- **Trial & Grace Periods** -- Configurable trial days and grace periods for subscription management.
- **Concurrency-Safe Callbacks** -- Payment callbacks use row-level locking (`SELECT ... FOR UPDATE`) to prevent duplicate processing.
- **Gateway Config Fallback** -- Gateway credentials are read from environment variables with a `system_config` table fallback.
- **Full i18n** -- User portal and admin panel are fully translatable (en / zh-CN).

---

## Architecture

The plugin follows a gateway-based architecture with a scheduler:

```
subscription/
  __init__.py     -- Plugin entry point (SubscriptionPlugin)
  models.py       -- Data layer (models, DDL, seed data)
  routes.py       -- Web layer (sub_bp Blueprint)
  services.py     -- Subscription business logic
  scheduler.py    -- Scheduled jobs for expiry and auto-renewal
  gateways/
    __init__.py   -- Payment routing and shared helpers
    alipay.py     -- Alipay payment gateway
    paypal.py     -- PayPal payment gateway
    stripe.py     -- Stripe payment gateway
    wechat.py     -- WeChat Pay payment gateway
  templates/      -- User portal + admin panel templates
  i18n/           -- en.yml / zh-CN.yml translations
  migrations/     -- Reserved for future DB migrations
  screenshots/    -- Store page screenshots
```

**Data Flow:**
1. Admin defines subscription SKUs and features in the marketplace.
2. Users browse and subscribe to features.
3. `services.py` routes the payment to the appropriate gateway based on environment (CN/INTL).
4. The gateway module processes the payment and returns confirmation.
5. `scheduler.py` runs periodic jobs to check for expiring subscriptions.
6. Auto-renewal is triggered if enabled and payment succeeds.
7. The `subscription/has` hook lets other plugins check feature access.

---

## Directory Structure

```
plugins/subscription/
  __init__.py
  models.py
  routes.py
  services.py
  scheduler.py
  gateways/
    __init__.py
    alipay.py
    paypal.py
    stripe.py
    wechat.py
  templates/
    subscribe.html
    subscribe_admin.html
  i18n/
    en.yml
    zh-CN.yml
  migrations/
  screenshots/
  README.en.md
```

---

## Installation & Activation

1. Ensure the `subscription/` directory is present under `plugins/`.
2. The plugin is auto-discovered by the VeroRun plugin loader.
3. Verify activation in the admin panel under **Plugins**.
4. The database schema is automatically initialized on first load.
5. Configure payment gateway credentials for each supported provider (environment variables or `system_config`).
6. The `register_jobs()` method registers scheduled jobs for expiry checks and auto-renewal.

---

## Configuration

| Key                  | Type    | Default | Description                                      |
|----------------------|---------|---------|--------------------------------------------------|
| `trial_days`         | integer | 0       | Number of free trial days for new subscriptions  |
| `grace_days`         | integer | 3       | Grace period after expiry before access is revoked|
| `auto_renew_default` | boolean | true    | Default auto-renewal setting for new subscriptions|

Each payment gateway also requires its own provider-specific credentials (API keys, secrets, merchant IDs). Credentials are resolved from environment variables first, then fall back to the `system_config` table.

---

## API Endpoints & Hooks

### Hooks Provided

| Hook                    | Description                                              |
|-------------------------|----------------------------------------------------------|
| `subscription/has`      | Check if a user has an active subscription to a feature  |
| `subscription/list`     | List all subscriptions for a user                        |
| `subscription/subscribe`| Subscribe a user to a feature                            |
| `subscription/cancel`   | Cancel an active subscription                            |
| `subscription/renew`    | Renew an expiring or expired subscription                |

### Hooks Listened

This plugin listens to no external events; all lifecycle behavior is driven by the provided hooks and scheduled jobs.

### Admin Routes

- `GET  /plugin/subscription/admin/` -- Subscription management dashboard
- `GET/POST /plugin/subscription/admin/items` -- SKU catalog management
- `DELETE /plugin/subscription/admin/items/<item_key>` -- Delete a SKU
- `GET  /plugin/subscription/admin/users` -- View user subscriptions
- `GET  /plugin/subscription/admin/orders` -- View orders
- `POST /plugin/subscription/admin/orders/<order_no>/refund` -- Refund an order

### Public Routes

- `GET  /plugin/subscription/portal` -- User subscription portal page
- `GET  /plugin/subscription/api/items` -- List available subscription plans
- `GET  /plugin/subscription/api/my` -- Get current user's subscriptions
- `GET  /plugin/subscription/api/check/<item_key>` -- Check feature access
- `POST /plugin/subscription/api/subscribe` -- Subscribe to a plan
- `POST /plugin/subscription/api/cancel` -- Cancel a subscription
- `POST /plugin/subscription/api/renew` -- Renew a subscription
- `GET  /plugin/subscription/api/orders` -- List current user's orders
- `POST /plugin/subscription/api/notify/{alipay,wechat,stripe,paypal}` -- Payment gateway callbacks

### Scheduled Jobs

The `register_jobs()` method registers scheduled jobs:
1. **Expiry Check** -- Runs periodically to identify and process expired subscriptions.
2. **Auto-Renewal Retry** -- Retries renewal for suspended subscriptions.

---

## Dependencies

This plugin has no external third-party Python dependencies. It relies on:

- VeroRun core (hook system, plugin loader, template engine, job scheduler, i18n)
- PostgreSQL (main VeroRun database, dedicated `subscription` schema)
- External payment gateway APIs (Alipay, WeChat Pay, Stripe, PayPal)

---

## Permissions

| Permission   | Description                            |
|--------------|----------------------------------------|
| `api:read`   | View subscription plans and status     |
| `api:write`  | Subscribe, cancel, and renew           |
| `admin:access` | Manage subscription SKUs and all users |

---

## License

This plugin is part of the VeroRun platform and is distributed under the same license as the core platform.
