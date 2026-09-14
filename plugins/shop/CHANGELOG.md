# Changelog

## v2.2.0 — 2026-08-30

### Changes

- Version bump from v2.1.0

## v2.1.0 — 2026-08-28

### Changes

- Version bump from v2.0.0
- feat(shop): add product list thumbnails and category icon picker
- fix(shop): prevent duplicate category creation
- fix(plugins): decouple shop/subscription payment

## v2.0.0 — 2026-08-25

### Added

- **Admin order pagination**: `GET /admin/shop/orders` now accepts `page`/`page_size` (LIMIT/OFFSET, page_size capped at 100) and returns `total`/`page`/`page_size`; order table renders a pager bar when pages > 1.
- **User order pagination**: `/shop/api/orders` and the "My Orders" page support paged results with page navigation.
- **Cross-worker rate limiting**: new `shop.rate_limits` table with a DB-backed sliding window, so limits are shared across gunicorn workers (replaces the process-local dict); DB unavailability degrades to pass-through.
- **Gateway refund abort**: the admin refund flow aborts immediately when the payment gateway call fails, preventing partial DB/refund state divergence.
- **pg_trgm search index**: `CREATE EXTENSION IF NOT EXISTS pg_trgm` plus GIN trigram indexes on `products.title`/`subtitle` for fast `LIKE '%keyword%'` mid-string search.
- **SEO fields**: `products.slug` / `meta_title` / `meta_description` columns with admin form fields and admin/product-detail rendering.
- **Product lifecycle status**: `products.status` (`draft` / `active` / `archived`) with admin list filter, form selector, and status badges.
- **Product CSV export/import**: `GET /admin/shop/products/export` and `POST /admin/shop/products/import` (upsert by id) with admin toolbar buttons.
- **Order CSV export**: `GET /admin/shop/orders/export` with admin toolbar button.
- **Order notes**: `order_items.note` column plus `PUT /admin/shop/orders/<oid>/note` with admin note editor.
- **Abandoned cart recovery**: `scheduler.py` scans pending orders older than 30 minutes, sends an in-app notification, and marks them reminded; guarded by `pg_try_advisory_lock` for multi-worker safety and registered with APScheduler (`shop_abandoned_cart_scan`).
- **Guest cart**: cart is kept in the flask session for anonymous users and merged into the account cart on login; checkout still requires login.
- **Wishlist-driven recommendations**: `products.wish_count` column, `wishlist.updated` event subscription (`on_wishlist_updated`), `GET /shop/api/recommend` ranked by `wish_count` + `sales_count`, and a "You may also like" section on the product detail page.
- **AI draft generation**: `POST /admin/shop/products/ai-generate` creates a product draft from a name (title/subtitle/tags/description/features) and a one-click "AI Generate" button in the product creation form.
- **SKU-aware cart constraint**: cart unique index migrated from `(user_id, product_id)` to `(user_id, product_id, sku_id)`, allowing the same product with different SKU variants in one cart.
- **SKU price/stock honored at checkout**: cart and direct-buy paths now use the selected SKU's price and stock.
- **Stock restoration on cancel/refund**: product and SKU stock are restored in a single transaction when an order is cancelled or refunded.
- **Category rendering fix**: duplicate flat category list removed, save action debounced.
- **Full i18n wordbook**: 381 symmetric en/zh entries; plugin-exclusive keys moved from global `i18n/*.yml` into `plugins/shop/i18n/` so they load only when the plugin is enabled.

### Changed

- `ai-optimize-title` raises `max_tokens` to 4096 to accommodate reasoning models, and maps configuration/gateway failures (model/provider/api-key/network/quota) to a 503 `AI Service Unavailable` instead of a generic 500.
- Order idempotency is now enforced at the application layer (checkout pre-check) instead of a DB unique index, since one order shares a single idempotency key across multiple items.

## v1.6.2 — 2026-08-22

### Changes

- Version bump from v1.6.1

## v1.6.1 — 2026-08-20

### Changes

- Version bump from v1.5.1

## v1.5.0 — 2026-08-19

### Changes

- Version bump from v1.4.0

