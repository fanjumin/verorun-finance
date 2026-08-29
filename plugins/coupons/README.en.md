# coupons (Smart Coupon Engine)

## Overview

The **coupons** plugin is a smart coupon engine for the VeroRun platform. It goes beyond simple discount codes by offering scenario-based coupon distribution, AI-powered coupon recommendations, and integration with the subscription system. The plugin validates, applies, and tracks coupon usage throughout the order lifecycle.

The plugin stores its data in the shared PostgreSQL database (via VeroRun's database abstraction layer) and uses a dedicated `CouponEngine` for business logic and an `AICouponRecommender` for personalized suggestions.

| Property     | Value               |
|--------------|---------------------|
| Identifier   | `coupons`           |
| Version      | 1.1.0               |
| Category     | shop                |
| Icon         | coupons             |
| Database     | PostgreSQL (shared) |
| Menu Group   | Business Center     |
| Menu Key     | `coupons_plugin`    |

---

## Features

- **Scenario-Based Coupons** -- Define coupon rules based on scenarios such as first purchase, cart value thresholds, product categories, or user segments.
- **AI-Powered Recommendations** -- The `AICouponRecommender` analyzes user behavior to suggest the most relevant coupons, maximizing conversion.
- **Subscription Integration** -- Coupons can be tied to subscription plans, offering discounts on recurring payments.
- **Coupon Validation & Application** -- The `CouponEngine` enforces all business rules: expiration, usage limits, minimum spend, product eligibility, and stacking rules, and applies coupons during checkout.
- **Admin Dashboard** -- Manage coupon campaigns, view redemption statistics, and configure rules.

---

## Architecture

The plugin follows a clean separation of core logic and presentation:

```
coupons/
  __init__.py         -- Plugin entry point (CouponPlugin)
  models.py           -- Data layer (coupon tables in the shared database)
  routes.py           -- Web layer (coupon_bp Blueprint)
  engine.py           -- Business logic (CouponEngine class)
  ai_recommender.py   -- AI recommendation engine (AICouponRecommender)
  scene.py            -- Scenario definitions and rules
  templates/          -- Admin dashboard UI (admin_coupons.html) + AI recommend partial
  i18n/               -- en.yml / zh-CN.yml translation files
  migrations/         -- Schema migration SQL files
```

**Data Flow:**
1. Admins create coupon campaigns via the admin panel.
2. The `CouponEngine` validates and applies coupons during checkout.
3. The `AICouponRecommender` suggests coupons to users based on behavior.

---

## Directory Structure

```
plugins/coupons/
  __init__.py
  models.py
  routes.py
  engine.py
  ai_recommender.py
  scene.py
  templates/
    admin_coupons.html
    _ai_recommend.html
  i18n/
    en.yml
    zh-CN.yml
  migrations/
  README.en.md
  README_CN.md
```

---

## Installation & Activation

1. Ensure the `coupons/` directory is present under `plugins/`.
2. The plugin is auto-discovered by the VeroRun plugin loader.
3. Verify activation in the admin panel under **Plugins**.
4. Tables are created automatically in the shared database on first load (`init_db()`).

No additional dependencies are required beyond the core VeroRun platform.

---

## Configuration

The plugin operates with sensible defaults. Configuration is managed through the admin panel.

| Key                     | Type    | Default | Description                                       |
|-------------------------|---------|---------|---------------------------------------------------|
| `max_coupons_per_user`  | integer | 5       | Maximum active coupons per user                   |
| `stacking_enabled`      | boolean | false   | Whether multiple coupons can be stacked           |
| `default_expiry_days`   | integer | 30      | Default expiration period for new coupons         |

---

## API Endpoints & Hooks

### Hooks Provided

| Hook              | Description                                              |
|-------------------|----------------------------------------------------------|
| `coupon/validate` | Validate a coupon code against business rules            |
| `coupon/apply`    | Apply a coupon to an order (deduct amount, mark as used) |

### Admin Routes

- `GET  /admin/coupons/` -- Admin dashboard (coupon list, statistics)
- `POST /admin/coupons/create` -- Create a new coupon campaign
- `POST /admin/coupons/update` -- Update an existing coupon
- `GET  /admin/coupons/stats` -- View coupon usage statistics

### Public Routes

- `POST /api/coupons/validate` -- Validate a coupon code
- `POST /api/coupons/apply` -- Apply a coupon to the current cart
- `GET  /api/coupons/recommend` -- Get AI-recommended coupons for the current user

---

## Dependencies

This plugin has no external third-party dependencies. It relies on:

- VeroRun core (hook system, plugin loader, template engine, i18n module)
- PostgreSQL (via VeroRun's database abstraction layer, shared with the main database)

---

## Permissions

| Permission     | Description                          |
|----------------|--------------------------------------|
| `order.read`   | Read order data for coupon validation|
| `order.write`  | Modify orders when applying coupons  |
| `user.read`    | Read user data for AI recommendations|
| `admin:access` | Access the admin coupon dashboard    |

---

## License

This plugin is part of the VeroRun platform and is distributed under the same license as the core platform.
