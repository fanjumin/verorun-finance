# Shop Plugin (商城插件)

> 中英双语文档 | Bilingual documentation (English + 中文)
> 版本 Version: 2.0.0 ｜ 插件标识 Identifier: `shop` ｜ 最低应用版本 Min app version: 0.10.0

---

## 0. Changelog / 版本历史

### v2.0.0 (2026-08-25)

**English**

- **Admin & user order pagination**: paged order lists (limit/offset, page_size capped at 100) with pager bars on both admin and user sides.
- **Cross-worker rate limiting**: DB-backed sliding window (`shop.rate_limits`) shared across gunicorn workers.
- **SEO fields + product lifecycle status**: `slug` / `meta_title` / `meta_description` and `draft` / `active` / `archived` status with admin filters.
- **CSV export/import**: product export/import (upsert by id) and order export.
- **Order notes**: per-order admin note editor.
- **Abandoned cart recovery**: scheduler scans pending orders > 30 min, sends in-app notification, advisory-lock guarded.
- **Guest cart**: session-based cart merged into the account cart on login.
- **Wishlist-driven recommendations**: `wish_count` tracking via `wishlist.updated` event, `GET /api/recommend`, "You may also like" section.
- **AI draft generation**: one-click AI product draft from a name; ai-title reliability (max_tokens 4096, 503 for AI unavailability).
- **SKU-aware cart & checkout**: cart unique index includes `sku_id`; SKU price/stock honored at checkout; stock restored on cancel/refund.
- **Full i18n**: 381 symmetric en/zh entries; plugin keys moved into the plugin i18n directory.

**中文**

- **管理端与用户端订单分页**：订单列表分页（limit/offset，page_size 上限 100），管理端与用户端均带翻页条。
- **跨进程限流**：基于数据库滑动窗口（`shop.rate_limits`），gunicorn 多 worker 间共享。
- **SEO 字段 + 商品生命周期状态**：`slug` / `meta_title` / `meta_description` 与 `draft` / `active` / `archived` 状态及后台筛选。
- **CSV 导入导出**：商品导入导出（按 id upsert）与订单导出。
- **订单备注**：每条订单的后台备注编辑器。
- **弃单挽回**：调度任务扫描超时 30 分钟未支付订单并发站内信提醒，advisory lock 防并发。
- **游客购物车**：基于会话的购物车，登录后合并进账号购物车。
- **收藏驱动推荐**：`wishlist.updated` 事件更新 `wish_count`，`GET /api/recommend` 推荐接口，详情页"猜你喜欢"区域。
- **AI 草稿生成**：输入商品名一键生成商品草稿；ai-title 可靠性提升（max_tokens 4096，AI 不可用时返回 503）。
- **SKU 感知购物车与结算**：购物车唯一索引含 `sku_id`；结算尊重 SKU 价格/库存；取消/退款返还库存。
- **国际化完善**：381 条中英对称词条；插件专属词条迁移至插件 i18n 目录。

### v1.2.0 (2026-08-06)

**English**

- **Order confirmation chain root-cause fix**: `confirm_shop_order` SQL placeholders in auth-center (`?` → `%s`, PostgreSQL-compatible); payment callbacks unified to `confirm_fn(order_id)` — order status transitions and `user_purchases` records are now reliably written on PostgreSQL.
- **Audit hardening P0–P3**: route parameter typo (`<int:oid>`), duplicate payment creation, duplicate stub-confirm, missing return, batch-AI SQL placeholder bug, `RETURNING id` (replacing `SELECT lastval()`), callback/event completeness.
- **Coupon fix**: `used_count` double-increment bug fixed (was +2 per use, causing premature `usage_limit`).
- **Admin-configurable AI settings**: `config` + `settings_schema` (JSON Schema) expose `shop_ai_provider` / `shop_ai_model`.
- **Dashboard stats**: `dashboard.stats` declaration + `get_dashboard_stats()`.
- **v1.4 store fields**: `icon_url` / `screenshots` / `readme_url` present.
- **Security & robustness**: magic-byte image verification, input validation (price/stock/quantity/category length), token-URL 302 consumption, `sys.path` guards, `print()` → `get_plugin_logger`.

**中文**

- **订单确认链路根因修复**：auth-center `confirm_shop_order` SQL 占位符（`?`→`%s`，兼容 PostgreSQL）；支付回调统一为 `confirm_fn(order_id)`——订单状态流转与 `user_purchases` 记录在 PostgreSQL 上可靠写入。
- **审计加固 P0–P3**：路由参数笔误（`<int:oid>`）、重复创建支付、重复桩确认、缺失 return、批量 AI 占位符 bug、`RETURNING id`（替代 `SELECT lastval()`）、回调/事件完整性。
- **优惠券修复**：`used_count` 双加 bug 修复（每次使用曾 +2，导致提前触达 `usage_limit`）。
- **后台可配置 AI 设置**：`config` + `settings_schema`（JSON Schema）暴露 `shop_ai_provider` / `shop_ai_model`。
- **Dashboard 统计**：`dashboard.stats` 声明 + `get_dashboard_stats()`。
- **v1.4 商店字段**：`icon_url` / `screenshots` / `readme_url` 已就位。
- **安全与健壮性**：图片魔数校验、输入校验（价格/库存/数量/分类长度）、token URL 302 消费、`sys.path` 守卫、`print()` → `get_plugin_logger`。

---

## 1. Overview / 概述

**English**

The **Shop Plugin** is the standalone e-commerce module of VeroRun. It provides full product and order management — products, categories, SKU specs, cart, checkout, payments, logistics, refunds and user purchases. The plugin is fully decoupled from the core system: it ships as an independent plugin package, registers its own blueprints under the `/mall` URL prefix (admin API under `/admin/shop`), stores all data in a dedicated PostgreSQL `shop` schema, and integrates with the Payment and Logistics plugins through the plugin manager.

**中文**

**商城插件（Shop Plugin）** 是 VeroRun 独立部署的电商模块。它提供完整的商品与订单管理能力——商品、分类、SKU 规格、购物车、结算、支付、物流、退款与用户已购。插件与核心系统完全解耦：以独立插件包形式分发，在 `/mall` URL 前缀下注册自己的蓝图（后台 API 在 `/admin/shop`），所有数据存放在专属 PostgreSQL `shop` schema 中，并通过插件管理器与 Payment、Logistics 插件联动。

---

## 2. Features / 功能特性

**English**

- **Products / Categories** — hierarchical categories (parent/child/level), product CRUD, multi-image gallery with reordering, title/subtitle/description, features, AI config, sort order, active toggle
- **Specs & SKUs** — spec groups (e.g. color, size), spec values, automatic SKU generation via Cartesian product, per-SKU price/stock/image
- **Cart & Checkout** — add/update/remove items, stock validation, coupon validation, single-transaction order creation, idempotency keys
- **Orders** — status lifecycle (pending → paid → shipped → completed), cancel, confirm receipt, request refund, admin confirm/refund/complete, admin shipment (express company + tracking number)
- **Payments** — Alipay default via PaymentPlugin with auth-center fallback; WeChat notify; Alipay notify; **stub-confirm** development mode; status polling
- **Logistics** — express company list, order tracking via LogisticsPlugin with fallback
- **User Purchases** — per-user entitlement records after confirmed payment
- **AI Product Optimization** — AI title generation, description optimization, selling points and tags generation, batch optimization
- **Pricing Rules & Express Companies** — pluggable pricing rule definitions, express company registry
- **i18n** — full zh-CN / en translation via YAML

**中文**

- **商品/分类** — 多级分类（父子层级）、商品增删改查、多图库与排序、标题/副标题/描述、特性、AI 配置、排序与上下架
- **规格与 SKU** — 规格组（如颜色、尺寸）、规格值、笛卡尔积自动生成 SKU、每个 SKU 独立价格/库存/图片
- **购物车与结算** — 商品加购/更新/移除、库存校验、优惠券校验、单事务创建订单、幂等键防重复下单
- **订单** — 状态流转（待支付 → 已支付 → 已发货 → 已完成）、取消、确认收货、申请退款，后台确认/退款/完成/发货（快递公司+运单号）
- **支付** — 默认支付宝（PaymentPlugin，fallback 到 auth-center）；微信回调；支付宝回调；**stub-confirm 开发模式**；支付状态轮询
- **物流** — 快递公司列表、通过 LogisticsPlugin 查询物流轨迹（带 fallback）
- **用户已购** — 支付确认后写入用户权益记录
- **AI 商品优化** — AI 标题生成、描述优化、卖点与标签生成、批量优化
- **定价规则与快递公司** — 可插拔的定价规则定义、快递公司注册表
- **国际化** — YAML 驱动的 zh-CN / en 完整翻译

---

## 3. Architecture / 架构设计

### 3.1 Module Structure / 目录结构

```
plugins/shop/
├── __init__.py              # ShopPlugin class (BasePlugin), lifecycle hooks
├── plugin.json              # plugin metadata: identifier, version, hooks, menu
├── models/
│   ├── __init__.py
│   └── database.py          # init_shop_db(): creates the 11-table `shop` schema
├── routes/
│   ├── __init__.py
│   ├── admin.py             # shop_admin_bp (url_prefix=/admin/shop) — admin API
│   └── public.py            # shop_public_bp (url_prefix=/mall) — storefront pages + user API
├── services/
│   └── __init__.py
├── templates/
│   └── public/
│       ├── shop.html        # product listing page
│       └── cart.html        # cart page
└── i18n/
    ├── en.yml               # English translations
    └── zh-CN.yml            # Chinese translations
```

**English** — The admin UI of the shop is rendered inside the admin console (the plugin registers its menu items under the "Business Center" group: Categories, Products, Shop Orders, Purchases), while the API is served by `shop_admin_bp`. The storefront pages and user-facing APIs are served by `shop_public_bp` under the `/mall` URL prefix (admin API under `/admin/shop`).

**中文** — 商城后台界面渲染在管理控制台内（插件在「商务中心」分组下注册菜单项：分类、商品、商城订单、已购），API 由 `shop_admin_bp` 提供；店铺前台页面与用户端 API 由 `shop_public_bp` 提供（前台在 `/mall` 前缀，后台 API 在 `/admin/shop`）。

### 3.2 Plugin Lifecycle / 插件生命周期

**English** — `ShopPlugin` inherits from `BasePlugin` and implements:

| Hook | Behavior |
|------|----------|
| `on_install(registry)` | Calls `_init_db()` → `init_shop_db()` (creates `shop` schema + tables) |
| `on_enable(registry)` | Ensures DB schema is ready, logs enabled event |
| `register_routes()` | Returns `[shop_admin_bp, shop_public_bp]` |
| `on_disable(registry)` | Logs disabled event |

The runtime enable/disable state is managed by the Plugin Manager (persisted in DB); `plugin.json` intentionally does **not** contain an `enabled` flag.

**中文** — `ShopPlugin` 继承 `BasePlugin`，实现生命周期钩子：

| 钩子 | 行为 |
|------|------|
| `on_install(registry)` | 调用 `_init_db()` → `init_shop_db()`（创建 `shop` schema 与数据表） |
| `on_enable(registry)` | 确保数据库 schema 就绪，记录启用日志 |
| `register_routes()` | 返回 `[shop_admin_bp, shop_public_bp]` |
| `on_disable(registry)` | 记录停用日志 |

插件的启用/停用运行时状态由插件管理器维护（持久化在数据库中），`plugin.json` 有意不包含 `enabled` 字段。

### 3.3 Database Schema / 数据库结构

**English** — All data lives in the dedicated PostgreSQL schema `shop` (created idempotently by `init_shop_db()`). 11 tables:

| Table | Purpose |
|-------|---------|
| `shop.products` | Products: title, price, original_price, stock, sales_count, images, features, ai_config, category, is_active |
| `shop.categories` | Hierarchical categories: name, slug (unique), parent_id, level |
| `shop.carts` | Cart items per user: user_id, product_id, sku_id, quantity |
| `shop.user_purchases` | User entitlements after payment: purchase_type, status, expire_at |
| `shop.order_items` | Orders: status, payment, coupon, discount, idempotency_key, shipping/refund tracking |
| `shop.order_shipping` | Per-order-item shipment records: company, tracking number, status |
| `shop.product_specs` | Spec groups per product (color/size/…) |
| `shop.product_spec_values` | Spec values per group |
| `shop.product_skus` | SKUs: sku_code, spec_path (JSON), price, stock, image |
| `shop.pricing_rules` | Pluggable pricing rule definitions (rule_key, options_json) |
| `shop.express_companies` | Express company registry: code, name, kdniao_code |

All IDs are `BIGINT GENERATED ALWAYS AS IDENTITY`; INSERTs use `RETURNING id` (no `lastval()` dependency). `order_items.idempotency_key` has a partial unique index to prevent duplicate orders.

**中文** — 所有数据存放在专属 PostgreSQL `shop` schema 中（由 `init_shop_db()` 幂等创建），共 11 张表：

| 表 | 用途 |
|----|------|
| `shop.products` | 商品：标题、价格、原价、库存、销量、图片、特性、AI 配置、分类、上下架 |
| `shop.categories` | 多级分类：名称、slug（唯一）、父级、层级 |
| `shop.carts` | 用户购物车：user_id、product_id、sku_id、数量 |
| `shop.user_purchases` | 支付后的用户权益：purchase_type、status、expire_at |
| `shop.order_items` | 订单：状态、支付、优惠券、折扣、幂等键、发货/退款追踪 |
| `shop.order_shipping` | 每订单项的发货记录：快递公司、运单号、状态 |
| `shop.product_specs` | 商品规格组（颜色/尺寸等） |
| `shop.product_spec_values` | 每组规格值 |
| `shop.product_skus` | SKU：sku_code、spec_path（JSON）、价格、库存、图片 |
| `shop.pricing_rules` | 可插拔的定价规则定义（rule_key、options_json） |
| `shop.express_companies` | 快递公司注册表：code、name、kdniao_code |

所有主键为 `BIGINT GENERATED ALWAYS AS IDENTITY`；INSERT 使用 `RETURNING id`（不依赖 `lastval()`）。`order_items.idempotency_key` 建有部分唯一索引，防止重复下单。

### 3.4 Payment & Logistics Integration / 支付与物流集成

**English**

- **PaymentPlugin first**: checkout/pay endpoints try the enabled `payment` plugin (`_get_plugin_instance('payment')`). If enabled, `create_shop_payment` / `confirm_shop_order` / `verify_notify` are delegated to it.
- **Fallback**: when the plugin is not enabled, the plugin calls `services.payment_service` from `auth-center` directly.
- **Callbacks**: `POST /api/pay/notify` (Alipay) and `POST /api/pay/wechat-notify` verify signatures, confirm the order, and write `user_purchases`.
- **Stub mode**: `POST /api/pay/<oid>/stub-confirm` confirms a pending order directly (development/testing), never double-confirms.
- **Logistics**: admin order list and tracking query use the `logistics` plugin when enabled, otherwise fall back to the express company registry.

**中文**

- **优先走支付插件**：下单/支付接口先尝试已启用的 `payment` 插件（`_get_plugin_instance('payment')`）。启用时委托其 `create_shop_payment` / `confirm_shop_order` / `verify_notify`。
- **回退**：插件未启用时，直接调用 `auth-center` 的 `services.payment_service`。
- **回调**：`POST /api/pay/notify`（支付宝）与 `POST /api/pay/wechat-notify` 验签、确认订单并写入 `user_purchases`。
- **桩模式**：`POST /api/pay/<oid>/stub-confirm` 直接确认待支付订单（开发/测试用），绝不重复确认。
- **物流**：后台订单列表与物流查询在 `logistics` 插件启用时调用插件，否则回退到快递公司注册表。

---

## 4. Quick Start / 快速开始

**English**

The plugin is managed by the Plugin Manager:

```bash
# Install & enable through the admin panel (Plugin Manager) or API:
# POST /admin/plugins/install   { "identifier": "shop" }
# POST /admin/plugins/enable    { "identifier": "shop" }
```

- Installation / enabling triggers `init_shop_db()` — the `shop` schema and 11 tables are created automatically.
- Routes are registered on the admin console (8084) and platform console; the storefront is served under `/mall/...`.
- Menu entries appear under **Business Center** → Categories / Products / Shop Orders / Purchases.

**中文**

插件由插件管理器统一管理：

```bash
# 在管理后台（插件管理器）或通过 API 安装并启用：
# POST /admin/plugins/install   { "identifier": "shop" }
# POST /admin/plugins/enable    { "identifier": "shop" }
```

- 安装/启用会触发 `init_shop_db()`，自动创建 `shop` schema 与 11 张表。
- 路由注册在管理后台（8084）与平台控制台；店铺前台服务在 `/mall/...` 下。
- 菜单项出现在「商务中心」→ 分类 / 商品 / 商城订单 / 已购。

---

## 5. Admin API / 管理端 API

**English** — All admin endpoints require a JWT in the `Authorization: Bearer <token>` header with `is_admin=true`. Invalid/missing tokens return `401`; non-admin tokens return `403`.

**中文** — 所有管理端接口要求 `Authorization: Bearer <token>` 携带管理员 JWT（`is_admin=true`）。无效/缺失返回 `401`，非管理员返回 `403`。

### 5.1 Products / 商品

| Method | Path | Description / 说明 |
|--------|------|--------------------|
| GET | `/shop/products` | List products / 商品列表 |
| POST | `/shop/products` | Create product / 创建商品 |
| GET | `/shop/products/<pid>` | Product detail / 商品详情 |
| PUT | `/shop/products/<pid>` | Update product / 更新商品 |
| DELETE | `/shop/products/<pid>` | Delete product + cleanup specs/skus/images / 删除商品并清理关联数据 |
| GET | `/shop/products/<pid>/preview` | Admin preview / 后台预览 |
| POST | `/shop/products/upload-image` | Upload image (magic-byte validated) / 上传图片（魔数校验） |
| GET/POST | `/shop/products/<pid>/images` | List / add product images / 图片列表/新增 |
| DELETE | `/shop/products/<pid>/images/<idx>` | Remove one image / 删除单张图片 |
| POST | `/shop/products/<pid>/images/reorder` | Reorder gallery / 图片排序 |

### 5.2 Specs & SKUs / 规格与 SKU

| Method | Path | Description / 说明 |
|--------|------|--------------------|
| GET/POST | `/shop/products/<pid>/specs` | List / create spec group / 规格组列表/创建 |
| PUT/DELETE | `/shop/products/<pid>/specs/<sid>` | Update / delete spec group / 更新/删除规格组 |
| POST | `/shop/products/<pid>/specs/<sid>/values` | Add spec value / 新增规格值 |
| PUT/DELETE | `/shop/products/<pid>/specs/values/<vid>` | Update / delete spec value / 更新/删除规格值 |
| GET | `/shop/products/<pid>/skus` | List SKUs / SKU 列表 |
| POST | `/shop/products/<pid>/skus/generate` | Generate SKUs by Cartesian product (base_price validated) / 笛卡尔积自动生成 SKU |
| PUT/DELETE | `/shop/products/<pid>/skus/<skuid>` | Update / delete SKU / 更新/删除 SKU |

### 5.3 Categories / 分类

| Method | Path | Description / 说明 |
|--------|------|--------------------|
| GET | `/shop/categories` | Category tree / 分类树 |
| POST | `/shop/categories` | Create category (name/slug ≤ 100 chars) / 创建分类 |
| PUT | `/shop/categories/<cid>` | Update category / 更新分类 |
| DELETE | `/shop/categories/<cid>` | Delete category / 删除分类 |

### 5.4 Orders / 订单

| Method | Path | Description / 说明 |
|--------|------|--------------------|
| GET | `/shop/orders` | Order list (filters, pagination) / 订单列表 |
| GET | `/shop/orders/<oid>/detail` | Order detail / 订单详情 |
| POST | `/shop/orders/<oid>/confirm` | Confirm order (payment verified) / 确认订单（支付已验证） |
| POST | `/shop/orders/<oid>/refund` | Refund (gateway + DB in one flow) / 退款（网关+数据库） |
| POST | `/shop/orders/<oid>/complete` | Mark completed / 标记完成 |
| POST | `/shop/orders/<oid>/ship` | Ship order (express company + tracking no.) / 发货 |
| GET | `/shop/orders/<oid>/track` | Query logistics tracking / 查询物流轨迹 |
| GET | `/shop/express-companies` | Express company list / 快递公司列表 |
| GET | `/shop/purchases` | User purchases list / 用户已购列表 |

### 5.5 AI Optimization / AI 优化

| Method | Path | Description / 说明 |
|--------|------|--------------------|
| POST | `/shop/products/<pid>/ai-optimize` | Full AI optimization / AI 全面优化 |
| POST | `/shop/products/<pid>/ai-title` | AI title generation / AI 标题生成 |
| POST | `/shop/products/<pid>/ai-description` | AI description optimization / AI 描述优化 |
| POST | `/shop/products/<pid>/ai-features` | AI selling points & tags / AI 卖点与标签 |
| POST | `/shop/products/ai-batch` | Batch optimize / 批量优化 |

---

## 6. Storefront Pages & User API / 前台页面与用户 API

**English** — Page routes serve the storefront; API routes are JSON. User APIs accept `Authorization: Bearer <token>` **or** fall back to the `sso_token` / `tm_token` cookie (JWT SSO).

**中文** — 页面路由渲染前台页面；API 路由返回 JSON。用户 API 支持 `Authorization: Bearer <token>`，或回退到 `sso_token` / `tm_token` cookie（JWT SSO）。

### 6.1 Pages / 页面

| Method | Path | Description / 说明 |
|--------|------|--------------------|
| GET | `/mall` / `/mall/` | Product listing / 商品列表页 |
| GET | `/mall/<pid>` | Product detail page / 商品详情页 |
| GET | `/mall/preview/<pid>` | Product preview / 商品预览 |
| GET | `/mall/cart` | Cart page / 购物车页 |
| GET | `/mall/pay/<oid>` | Payment page / 支付页 |
| GET | `/mall/orders` | My orders page / 我的订单页 |
| GET | `/mall/orders/<oid>/track-user` | User-side tracking page / 用户端物流页 |
| GET | `/mall/cloud` | Cloud instances page / 云实例页 |

### 6.2 APIs / 接口

| Method | Path | Description / 说明 |
|--------|------|--------------------|
| GET | `/mall/api/user/info` | Current user info / 当前用户信息 |
| GET | `/mall/api/products` | Product list (filter/search/pagination) / 商品列表 |
| GET | `/mall/api/products/<pid>` | Product detail / 商品详情 |
| GET | `/mall/api/products/<pid>/skus` | SKU options / SKU 选项 |
| GET | `/mall/api/cart` | Cart contents + totals / 购物车内容与合计 |
| POST | `/mall/api/cart/add` | Add to cart (rate-limited 60/min) / 加购 |
| POST | `/mall/api/cart/update` | Update quantity / 更新数量 |
| POST | `/mall/api/cart/remove` | Remove item / 移除商品 |
| GET | `/mall/api/addresses` | User addresses / 用户地址 |
| POST | `/mall/api/checkout` | Checkout — single transaction order + coupon + cart clear / 结算下单 |
| GET | `/mall/api/orders` | My orders / 我的订单 |
| POST | `/mall/api/orders/<oid>/delete` | Delete order / 删除订单 |
| POST | `/mall/api/orders/<oid>/cancel` | Cancel order / 取消订单 |
| POST | `/mall/api/orders/<oid>/confirm-receipt` | Confirm receipt / 确认收货 |
| POST | `/mall/api/orders/<oid>/request-refund` | Request refund / 申请退款 |
| POST | `/mall/api/pay/<oid>` | Create payment (Alipay default) / 发起支付 |
| POST | `/mall/api/pay/<oid>/stub-confirm` | Stub confirm (dev) / 桩确认（开发） |
| POST | `/mall/api/pay/notify` | Alipay async notify / 支付宝异步回调 |
| POST | `/mall/api/pay/wechat-notify` | WeChat async notify / 微信异步回调 |
| GET | `/mall/api/pay/status/<oid>` | Payment status / 支付状态 |
| POST | `/mall/api/coupon/validate` | Validate coupon / 校验优惠券 |

---

## 7. Authentication & Security / 认证与安全

**English**

- **Admin**: `_require_admin()` reads the JWT only from the `Authorization` header (never from cookies), then validates it via `services.jwt_service.validate_token` and requires `is_admin=true` — admin CSRF risk is therefore minimal.
- **User**: `_require_user()` reads the JWT from the header first, then falls back to `sso_token` / `tm_token` cookies for JWT SSO across subdomains.
- **Token URL consumption**: when a storefront page is opened with `?token=`, the token is written into the `sso_token` cookie and the page immediately 302-redirects to the clean URL (token removed), preventing token leakage into the address bar, browser history, and access logs.
- **Image upload**: extension whitelist + **magic-byte** header verification (PNG / JPG / GIF / WebP) — rejects disguised files.
- **Input validation**: price / original_price / stock are type-safe parsed and must be non-negative; base_price for SKU generation is validated; single-item quantity is capped at `_MAX_ORDER_QTY = 999`; stock is checked against quantity at cart-add; category name/slug limited to 100 characters.
- **Idempotency**: `order_items.idempotency_key` partial unique index prevents duplicate orders.
- **Rate limiting**: per-user in-memory window (e.g. cart-add 60 req/min). See known limitation below.

**中文**

- **管理端**：`_require_admin()` 只从 `Authorization` 请求头读取 JWT（绝不读 cookie），经 `services.jwt_service.validate_token` 校验并要求 `is_admin=true`——管理端 CSRF 风险因此很低。
- **用户端**：`_require_user()` 优先读取请求头 JWT，再回退到 `sso_token` / `tm_token` cookie，实现跨子域 JWT SSO。
- **图片上传**：扩展名白名单 + **文件头魔数**校验（PNG / JPG / GIF / WebP），拒绝伪装文件。
- **输入校验**：价格/原价/库存做类型安全解析且必须非负；SKU 生成 base_price 校验；单件数量上限 `_MAX_ORDER_QTY = 999`；加购时库存对照数量校验；分类名称/slug 限长 100 字符。
- **幂等**：`order_items.idempotency_key` 部分唯一索引防止重复下单。
- **限流**：按用户内存滑动窗口（如加购 60 次/分钟）。局限见下。

---

## 8. Known Limitations / 已知限制

**English**

1. **Rate limiter is cross-process**: rate limiting uses the `shop.rate_limits` table in PostgreSQL with a sliding window, so limits are shared across gunicorn workers. If the DB is temporarily unavailable the limiter degrades to pass-through (no blocking) to avoid breaking business flows.
2. **Import-time `sys.path` inserts**: `models/database.py` and `__init__.py` insert the `auth-center` path once at import time (guarded by `os.path.isdir` existence check and deduplication; does not grow per request). If `auth-center` is already importable these are redundant but harmless.

**中文**

1. **限流器仅单进程生效**：`_RATE_LIMIT` 是进程内字典。在多进程 WSGI 部署下（如 gunicorn workers ≥ 2），各进程独立计数，限流会失效。如需可靠限流，应迁移到 Redis 滑动窗口或 PostgreSQL 计数（审计项 P1-2）。
2. **导入期 `sys.path` 插入**：`models/database.py` 与 `__init__.py` 在导入时一次性插入 `auth-center` 路径（已加 `os.path.isdir` 存在性判断与去重守卫，不随请求增长）。若 `auth-center` 已可导入，则为冗余但无害。

---

## 9. Development Notes / 开发说明

### 9.1 Code Style / 代码规范

**English**

- Follow the existing module layout — do not create standalone files/databases; reuse `models.get_db()` and the `shop` schema.
- All user-facing strings use the i18n `_()` function with **English keys**; add both `en.yml` and `zh-CN.yml` entries.
- Use `get_plugin_logger('shop')` for logging instead of `print()`.
- INSERT statements must use `RETURNING id` (never `SELECT lastval()`).
- Same-file multi-edit should be applied sequentially to avoid write races.

**中文**

- 遵循现有模块布局——不新建独立文件/数据库；复用 `models.get_db()` 与 `shop` schema。
- 所有面向用户的文案使用 i18n `_()` 函数且键必须为**英文**；同时补充 `en.yml` 与 `zh-CN.yml`。
- 日志使用 `get_plugin_logger('shop')`，不要使用 `print()`。
- INSERT 必须使用 `RETURNING id`（禁止 `SELECT lastval()`）。
- 同一文件的多次编辑应串行执行，避免写竞争。

### 9.2 Verify / 本地验证

**English**

```bash
# Syntax check
python -m py_compile plugins/shop/__init__.py plugins/shop/models/database.py \
    plugins/shop/routes/admin.py plugins/shop/routes/public.py

# YAML validity
python -c "import yaml; yaml.safe_load(open('plugins/shop/i18n/en.yml', encoding='utf-8')); yaml.safe_load(open('plugins/shop/i18n/zh-CN.yml', encoding='utf-8'))"
```

Always test with real logins (admin + normal user), not just HTTP status codes.

**中文**

```bash
# 语法检查
python -m py_compile plugins/shop/__init__.py plugins/shop/models/database.py \
    plugins/shop/routes/admin.py plugins/shop/routes/public.py

# YAML 合法性
python -c "import yaml; yaml.safe_load(open('plugins/shop/i18n/en.yml', encoding='utf-8')); yaml.safe_load(open('plugins/shop/i18n/zh-CN.yml', encoding='utf-8'))"
```

务必使用真实登录（管理员 + 普通用户）测试，而非仅看 HTTP 状态码。

---

## 10. Related Documents / 相关文档

**English**

- Project overview: [README.md](../../README.md) (root)
- Payment plugin: `plugins/payment/README_CN.md`, `plugins/payment/README.en.md`
- Plugin Manager: `plugin_manager/` (lifecycle, store, license, hooks)

**中文**

- 项目总览：[README.md](../../README.md)（根目录）
- 支付插件：`plugins/payment/README_CN.md`、`plugins/payment/README.en.md`
- 插件管理器：`plugin_manager/`（生命周期、商店、授权、钩子）

---

## 11. License / 许可

**English** — Part of the VeroRun project. See the project [LICENSE](../../LICENSE).

**中文** — VeroRun 项目的一部分。参见项目 [LICENSE](../../LICENSE)。
