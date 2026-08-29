# reviews (Product Reviews)

## Overview

The **reviews** plugin provides a product review and rating system for the VeroRun platform. It allows verified purchasers to rate products, write reviews, and upload photos. The plugin enforces that only customers who have completed a purchase can leave a review, ensuring authenticity and trustworthiness.

The plugin stores review data in the dedicated PostgreSQL `reviews` schema of the VeroRun main database, while performing cross-reads from the `public` schema for user and product information. Routes are defined in a dedicated `routes.py` module as a Blueprint.

| Property    | Value                |
|-------------|----------------------|
| Identifier  | `reviews`            |
| Version     | 1.1.0                |
| Database    | `reviews` schema     |
| Menu Group  | None (no admin menu) |

---

## Features

- **Product Rating** -- Users can rate products on a configurable scale (e.g., 1-5 stars).
- **Review Writing** -- Verified purchasers can write detailed text reviews.
- **Photo Upload** -- Users can attach photos to their reviews for richer feedback.
- **Verified Purchase Enforcement** -- Only customers who have completed an order can leave a review.
- **Admin Reply** -- Administrators can reply to reviews directly from the admin panel.
- **Cross-Database Reads** -- Reads user and product information from the main VeroRun database while storing review data in the `reviews` schema.
- **Order Paid Hook** -- Automatically prompts users to leave a review after order payment.

---

## Architecture

The plugin defines all routes in a dedicated `routes.py` module as a Blueprint:

```
reviews/
  __init__.py    -- Plugin entry point, event hooks, route registration
  routes.py      -- Blueprint with all review API endpoints
  models.py      -- Data layer (PostgreSQL `reviews` schema)
```

**Data Flow:**
1. A user's order is paid (triggers `order.paid` hook).
2. The user navigates to the product page and submits a review.
3. The plugin verifies the user is a verified purchaser.
4. The review is stored in the `reviews` schema.
5. Product rating averages are computed from the `reviews` schema.
6. Admin can view all reviews and reply via the admin panel.
7. The `review/validate` hook allows other plugins to check review eligibility.

---

## Directory Structure

```
plugins/reviews/
  __init__.py
  routes.py
  models.py
  i18n/
  README.en.md
```

---

## Installation & Activation

1. Ensure the `reviews/` directory is present under `plugins/`.
2. The plugin is auto-discovered by the VeroRun plugin loader.
3. Verify activation in the admin panel under **Plugins**.
4. The `reviews` schema is automatically created on install.

No additional dependencies are required beyond the core VeroRun platform.

---

## Configuration

This plugin does not have explicit configuration keys. All settings are managed through the VeroRun admin panel's general review settings.

---

## API Endpoints & Hooks

### Hooks Provided

| Hook               | Description                                              |
|--------------------|----------------------------------------------------------|
| `review/validate`  | Validate whether a user is eligible to review a product  |

### Hooks Listened

| Hook                | Description                                              |
|---------------------|----------------------------------------------------------|
| `order.paid`        | Trigger review prompt after order payment             |

### API Endpoints (Public)

| Method | Endpoint                       | Description                              |
|--------|--------------------------------|------------------------------------------|
| GET    | `/api/<product_id>`            | List reviews for a product               |
| POST   | `/api/<product_id>/create`     | Create a new review for a product        |
| DELETE | `/api/<review_id>`             | Delete a user's own review               |
| GET    | `/api/user/reviews`            | List reviews by the current user         |

### Admin Routes

| Method | Endpoint                              | Description                          |
|--------|---------------------------------------|--------------------------------------|
| GET    | `/admin/reviews`                      | List all reviews (admin view)        |
| POST   | `/admin/reviews/<rid>/reply`          | Reply to a review as admin           |

---

## Dependencies

This plugin has no external third-party dependencies. It relies on:

- VeroRun core (hook system, plugin loader, template engine)
- PostgreSQL (via VeroRun's database abstraction layer)
- Main VeroRun database (cross-reads for user and product data)

---

## Permissions

| Permission            | Description                              |
|-----------------------|------------------------------------------|
| `shop.product.read`   | Read product information for reviews     |
| `user.read`           | Read user information for review display |

---

## License

This plugin is part of the VeroRun platform and is distributed under the same license as the core platform.