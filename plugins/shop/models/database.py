#!/usr/bin/env python3
"""
Shop Plugin — Database initialization
======================================
All 11 shop tables in the `shop` PostgreSQL schema.
Exact copy of auth-center/models/database.py init_shop_db().
"""
from contextlib import contextmanager
from plugins._base.db import get_pooled_connection
from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('shop')


@contextmanager
def get_shop_db():
    """shop 插件独立数据库连接（PG schema: shop），走共享连接池（§9.1/§11.2）。

    调用方使用 `with get_shop_db() as conn:`，退出自动 commit 并归还连接池。
    """
    with get_pooled_connection() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS shop")
        conn.execute("SET search_path TO shop")
        yield conn


def init_shop_db():
    """Create shop tables in shop schema."""
    with get_shop_db() as conn:
        cur = conn.cursor()
        cur.execute("CREATE SCHEMA IF NOT EXISTS shop")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.products (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                title           TEXT NOT NULL,
                subtitle        TEXT DEFAULT '',
                product_type    TEXT NOT NULL DEFAULT 'service',
                category        TEXT DEFAULT '',
                price           DOUBLE PRECISION NOT NULL DEFAULT 0,
                original_price  DOUBLE PRECISION DEFAULT 0,
                stock           BIGINT DEFAULT 0,
                sales_count     BIGINT DEFAULT 0,
                thumbnail       TEXT DEFAULT '',
                description     TEXT DEFAULT '',
                features        TEXT DEFAULT '[]',
                ai_config       TEXT DEFAULT '{}',
                sort_order      BIGINT DEFAULT 0,
                is_active       BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW(),
                images          TEXT DEFAULT '[]',
                category_id     BIGINT DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'active',
                slug            TEXT DEFAULT '',
                meta_title      TEXT DEFAULT '',
                meta_description TEXT DEFAULT '',
                wish_count      BIGINT DEFAULT 0
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_type ON shop.products(product_type)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_active ON shop.products(is_active)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.categories (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name        TEXT NOT NULL,
                slug        TEXT UNIQUE,
                parent_id   BIGINT DEFAULT 0,
                level       BIGINT DEFAULT 0,
                icon        TEXT DEFAULT '',
                sort_order  BIGINT DEFAULT 0,
                is_active   BIGINT DEFAULT 1,
                created_at  TIMESTAMP DEFAULT NOW(),
                updated_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cat_parent ON shop.categories(parent_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cat_level ON shop.categories(level)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.carts (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT NOT NULL,
                product_id      BIGINT NOT NULL,
                sku_id          BIGINT DEFAULT 0,
                quantity        BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_carts_user ON shop.carts(user_id)"
        )
        # 4.2：变体购物车约束迁移——同商品不同 SKU 需分别加购，
        # 旧 UNIQUE(user_id,product_id) 改为含 sku_id 的唯一索引
        # （旧库默认约束名 carts_user_id_product_id_key；IF EXISTS 保证幂等）
        cur.execute(
            "ALTER TABLE shop.carts DROP CONSTRAINT IF EXISTS carts_user_id_product_id_key"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_carts_user_product_sku "
            "ON shop.carts(user_id, product_id, sku_id)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.user_purchases (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT NOT NULL,
                product_id      BIGINT NOT NULL,
                order_id        TEXT DEFAULT '',
                purchase_type   TEXT NOT NULL DEFAULT 'once',
                expire_at       TIMESTAMP,
                status          TEXT DEFAULT 'active',
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_up_user ON shop.user_purchases(user_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_up_status ON shop.user_purchases(status)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.order_items (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                order_id        TEXT NOT NULL,
                user_id         BIGINT NOT NULL,
                product_id      BIGINT NOT NULL,
                product_title   TEXT NOT NULL DEFAULT '',
                quantity        BIGINT DEFAULT 1,
                unit_price      DOUBLE PRECISION NOT NULL DEFAULT 0,
                sku_id          BIGINT DEFAULT 0,
                sku_price       DOUBLE PRECISION DEFAULT 0,
                subtotal        DOUBLE PRECISION NOT NULL DEFAULT 0,
                coupon_id       BIGINT DEFAULT NULL,
                discount        DOUBLE PRECISION DEFAULT 0,
                total           DOUBLE PRECISION DEFAULT 0,
                status          TEXT DEFAULT 'pending',
                created_at      TIMESTAMP DEFAULT NOW(),
                paid_at         TIMESTAMP,
                idempotency_key TEXT DEFAULT '',
                receiver_name   TEXT DEFAULT '',
                receiver_phone  TEXT DEFAULT '',
                receiver_address TEXT DEFAULT '',
                payment_method  TEXT DEFAULT '',
                payment_trade_no TEXT DEFAULT '',
                tracking_company TEXT DEFAULT '',
                tracking_number  TEXT DEFAULT '',
                shipping_status  TEXT DEFAULT '',
                shipped_at       TIMESTAMP,
                completed_at     TIMESTAMP,
                refund_reason    TEXT DEFAULT '',
                refund_requested_at TIMESTAMP,
                refunded_at      TIMESTAMP,
                user_deleted     BIGINT DEFAULT 0,
                note             TEXT DEFAULT '',
                abandon_reminded_at TIMESTAMP DEFAULT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_oi_order ON shop.order_items(order_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_oi_user ON shop.order_items(user_id)"
        )
        # 4.4：幂等键改为应用层检查（api_checkout 事务前 SELECT 兜底）。
        # 唯一索引曾与「一单多商品共享同一 idempotency_key」冲突，故移除。
        cur.execute("DROP INDEX IF EXISTS shop.idx_oi_idempotency")
        # VR-SHOP-001（第二失败点）：补齐 order_items 缺失列。
        # 旧库可能在主库 init_db() 静默迁移（except: pass）之前已建表，导致
        # receiver_*/total 列缺失，checkout 写订单必 500。此处幂等补齐。
        for _col, _dtype in [
            ('receiver_name', "TEXT DEFAULT ''"),
            ('receiver_phone', "TEXT DEFAULT ''"),
            ('receiver_address', "TEXT DEFAULT ''"),
            ('total', 'DOUBLE PRECISION DEFAULT 0'),
            ('sku_id', 'BIGINT DEFAULT 0'),
            ('sku_price', 'DOUBLE PRECISION DEFAULT 0'),
            ('note', "TEXT DEFAULT ''"),
            ('abandon_reminded_at', 'TIMESTAMP DEFAULT NULL'),
        ]:
            cur.execute(
                f"ALTER TABLE shop.order_items ADD COLUMN IF NOT EXISTS {_col} {_dtype}"
            )

        # 商品表补列（幂等）：第二批 status/slug/meta_* + P2 wish_count（收藏热度）
        for _col, _dtype in [
            ('status', "TEXT NOT NULL DEFAULT 'active'"),
            ('slug', "TEXT DEFAULT ''"),
            ('meta_title', "TEXT DEFAULT ''"),
            ('meta_description', "TEXT DEFAULT ''"),
            ('wish_count', 'BIGINT DEFAULT 0'),
        ]:
            cur.execute(
                f"ALTER TABLE shop.products ADD COLUMN IF NOT EXISTS {_col} {_dtype}"
            )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.order_shipping (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                order_item_id   BIGINT NOT NULL,
                tracking_company TEXT DEFAULT '',
                tracking_number  TEXT DEFAULT '',
                shipping_status  TEXT DEFAULT '',
                shipped_at       TIMESTAMP,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_os_orderitem ON shop.order_shipping(order_item_id)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.product_specs (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                product_id  BIGINT NOT NULL,
                spec_name   TEXT NOT NULL,
                sort_order  BIGINT DEFAULT 0,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_ps_product ON shop.product_specs(product_id)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.product_spec_values (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                spec_id     BIGINT NOT NULL,
                spec_value  TEXT NOT NULL,
                sort_order  BIGINT DEFAULT 0,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_psv_spec ON shop.product_spec_values(spec_id)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.product_skus (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                product_id  BIGINT NOT NULL,
                sku_code    TEXT NOT NULL,
                spec_path   TEXT NOT NULL DEFAULT '{}',
                price       DOUBLE PRECISION NOT NULL DEFAULT 0,
                stock       BIGINT DEFAULT 0,
                image       TEXT DEFAULT '',
                is_active   BIGINT DEFAULT 1,
                created_at  TIMESTAMP DEFAULT NOW(),
                updated_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_psk_product ON shop.product_skus(product_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_psk_code ON shop.product_skus(sku_code)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.pricing_rules (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                rule_key    TEXT UNIQUE NOT NULL,
                label       TEXT NOT NULL,
                rule_type   TEXT NOT NULL DEFAULT 'radio',
                options_json TEXT NOT NULL DEFAULT '[]',
                sort_order  BIGINT DEFAULT 0,
                is_active   BIGINT DEFAULT 1,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.express_companies (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                code        TEXT NOT NULL UNIQUE,
                name        TEXT NOT NULL,
                kdniao_code TEXT DEFAULT '',
                is_active   BIGINT DEFAULT 1,
                sort_order  BIGINT DEFAULT 0,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)

        # P1-2：跨进程限流计数表（gunicorn 多 worker 下进程内 dict 失效，改用 DB 滑动窗口）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.rate_limits (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                rkey        TEXT NOT NULL,
                ts          DOUBLE PRECISION NOT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_limits_rkey_ts ON shop.rate_limits(rkey, ts)"
        )
        # 4.2 第二批：商品搜索加速——pg_trgm GIN 索引（加速 LIKE '%xx%' 中缀匹配）
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_title_trgm "
            "ON shop.products USING gin (title public.gin_trgm_ops)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_subtitle_trgm "
            "ON shop.products USING gin (subtitle public.gin_trgm_ops)"
        )
        conn.commit()
    logger.info('[ShopPlugin] shop schema initialized in PostgreSQL')
