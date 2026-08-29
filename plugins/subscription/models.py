#!/usr/bin/env python3
"""
Subscription Plugin — 数据模型
=================================
独立 PG Schema: subscription（通过 plugins/_base/db.py 连接主库）
表:
  - sub_items           SKU 目录（可订阅项定义）
  - user_subscriptions  用户订阅记录
  - sub_orders          订阅订单
"""

from i18n import _
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from plugins._base.db import PgConnection
from plugins._base.db import get_raw_connection


# ── 状态枚举 ────────────────────────────────────────────────────────────

class SubStatus(str, Enum):
    ACTIVE = 'active'
    CANCELED = 'canceled'
    EXPIRED = 'expired'
    SUSPENDED = 'suspended'


class OrderStatus(str, Enum):
    PENDING = 'pending'
    PAID = 'paid'
    FAILED = 'failed'
    REFUNDED = 'refunded'
    EXPIRED = 'expired'


class IntervalType(str, Enum):
    MONTH = 'month'
    YEAR = 'year'


# ── DDL ─────────────────────────────────────────────────────────────────

SUBSCRIPTION_DDL = """
-- SKU 目录（可订阅项定义）
CREATE TABLE IF NOT EXISTS sub_items (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    item_key        TEXT UNIQUE NOT NULL,
    category        TEXT NOT NULL DEFAULT 'plugin',
    name_zh         TEXT NOT NULL,
    name_en         TEXT NOT NULL,
    description_zh  TEXT DEFAULT '',
    description_en  TEXT DEFAULT '',
    price_month     BIGINT NOT NULL DEFAULT 0,
    price_year      BIGINT NOT NULL DEFAULT 0,
    tier            TEXT NOT NULL DEFAULT 'basic',    -- 套餐档位 basic/popular/premium（兼容 subscription_plans.tier）
    features_json   TEXT DEFAULT '[]',                 -- 特性列表 JSON（兼容 subscription_plans.features_json）
    trial_days      BIGINT NOT NULL DEFAULT 0,         -- 试用天数（兼容 module_pricing.trial_days）
    currency        TEXT NOT NULL DEFAULT 'CNY',
    billing_mode    TEXT NOT NULL DEFAULT 'continuous',-- one_shot/interactive/continuous/publish
    trial_daily_limit BIGINT,                          -- 试用期每日额度
    post_trial_action TEXT NOT NULL DEFAULT 'lock',    -- lock/pause/pay_per_use
    refund_days     BIGINT NOT NULL DEFAULT 0,
    limit_even_byok BIGINT NOT NULL DEFAULT 0,
    is_active       BIGINT NOT NULL DEFAULT 1,
    auto_activate   TEXT DEFAULT '',     -- 自动开通的 item_key 列表（逗号分隔）
    sort_order      BIGINT DEFAULT 0,
    created_at      TEXT DEFAULT (NOW()),
    updated_at      TEXT DEFAULT (NOW())
);

-- 用户订阅记录
CREATE TABLE IF NOT EXISTS user_subscriptions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    item_key        TEXT NOT NULL,
    interval_type   TEXT NOT NULL DEFAULT 'month',
    amount_fen      BIGINT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','canceled','expired','suspended')),
    tier            TEXT NOT NULL DEFAULT '',          -- 档位快照（订阅时从 sub_items 冗余）
    module_states   TEXT DEFAULT '{}',                 -- 模块状态 JSON（module gating）
    retry_count     BIGINT NOT NULL DEFAULT 0,         -- dunning 重试次数
    last_charge_at  TEXT,                              -- 最近一次扣款时间
    grace_end       TEXT,                              -- 宽限期截止（period_end + grace_days）
    payment_method  TEXT DEFAULT '',                   -- 支付方式（自动续费代扣用）
    agreement_id    TEXT DEFAULT '',                   -- 代扣签约号（alipay_agreement/wechat_contract）
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    auto_renew      BIGINT NOT NULL DEFAULT 1,
    canceled_at     TEXT,
    cancel_reason   TEXT DEFAULT '',
    order_no        TEXT DEFAULT '',
    created_at      TEXT DEFAULT (NOW()),
    updated_at      TEXT DEFAULT (NOW()),
    UNIQUE(user_id, item_key)
);

-- 订阅订单
CREATE TABLE IF NOT EXISTS sub_orders (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_no        TEXT UNIQUE NOT NULL,
    user_id         BIGINT NOT NULL,
    item_key        TEXT NOT NULL,
    interval_type   TEXT NOT NULL DEFAULT 'month',
    amount_fen      BIGINT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'CNY',
    channel         TEXT NOT NULL DEFAULT 'alipay',
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','paid','failed','refunded','expired')),
    trade_no        TEXT DEFAULT '',
    qr_code         TEXT DEFAULT '',
    redirect_url    TEXT DEFAULT '',
    fail_reason     TEXT DEFAULT '',
    notify_id       TEXT DEFAULT '',
    notify_raw      TEXT DEFAULT '',
    paid_at         TEXT,
    created_at      TEXT DEFAULT (NOW()),
    updated_at      TEXT DEFAULT (NOW()),
    extra           TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sub_items_active ON sub_items(is_active);
CREATE INDEX IF NOT EXISTS idx_sub_items_category ON sub_items(category);
CREATE INDEX IF NOT EXISTS idx_user_subs_user ON user_subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_subs_status ON user_subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_user_subs_item ON user_subscriptions(item_key);
CREATE INDEX IF NOT EXISTS idx_sub_orders_user ON sub_orders(user_id);
CREATE INDEX IF NOT EXISTS idx_sub_orders_status ON sub_orders(status);

-- 计费事件（Billing Log，兼容 payment_events）
CREATE TABLE IF NOT EXISTS sub_payment_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    sub_id          BIGINT,
    order_no        TEXT DEFAULT '',
    event_type      TEXT NOT NULL,           -- charge_success/charge_fail/refund/notify
    channel         TEXT NOT NULL,
    channel_event_id TEXT DEFAULT '',
    amount_fen      BIGINT,
    result          TEXT DEFAULT '',          -- success/fail
    fail_reason     TEXT DEFAULT '',
    raw_response    TEXT DEFAULT '',
    created_at      TEXT DEFAULT (NOW())
);
CREATE INDEX IF NOT EXISTS idx_sub_pay_events_user ON sub_payment_events(user_id);
CREATE INDEX IF NOT EXISTS idx_sub_pay_events_order ON sub_payment_events(order_no);

-- 订阅审计日志（兼容 subscription_audit_log）
CREATE TABLE IF NOT EXISTS sub_audit_log (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    sub_id          BIGINT,
    action          TEXT NOT NULL,
    detail          TEXT DEFAULT '',
    ip_address      TEXT DEFAULT '',
    admin_id        BIGINT,
    created_at      TEXT DEFAULT (NOW())
);
CREATE INDEX IF NOT EXISTS idx_sub_audit_user ON sub_audit_log(user_id);

-- 部署码（私有化授权，兼容 deployment_codes）
CREATE TABLE IF NOT EXISTS deploy_codes (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code            TEXT UNIQUE NOT NULL,
    code_hash       TEXT NOT NULL,
    user_id         BIGINT NOT NULL,
    item_key        TEXT NOT NULL DEFAULT 'deploy_basic',
    duration_days   BIGINT NOT NULL DEFAULT 365,
    expires_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','used','expired','revoked')),
    last_heartbeat  TEXT,
    last_hostname   TEXT DEFAULT '',
    last_version    TEXT DEFAULT '',
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_deploy_codes_code ON deploy_codes(code);
CREATE INDEX IF NOT EXISTS idx_deploy_codes_user ON deploy_codes(user_id);
CREATE INDEX IF NOT EXISTS idx_deploy_codes_status ON deploy_codes(status);

-- 发票（兼容主库 invoices，V20260812 完整迁移后独立）
CREATE TABLE IF NOT EXISTS sub_invoices (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_no      TEXT UNIQUE NOT NULL,
    order_no        TEXT NOT NULL,
    user_id         BIGINT NOT NULL,
    amount_fen      BIGINT NOT NULL DEFAULT 0,
    amount_yuan     DOUBLE PRECISION NOT NULL DEFAULT 0,
    plan_name       TEXT DEFAULT '',
    period_text     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'issued',
                    -- issued / cancelled
    pdf_path        TEXT DEFAULT '',
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sub_invoices_user ON sub_invoices(user_id);
CREATE INDEX IF NOT EXISTS idx_sub_invoices_order ON sub_invoices(order_no);
"""


def init_tables():
    """初始化所有表"""
    conn = PgConnection(get_raw_connection())
    conn.execute("CREATE SCHEMA IF NOT EXISTS subscription")
    conn.execute("SET search_path TO subscription")
    conn.execute(SUBSCRIPTION_DDL)
    _migrate_existing_tables(conn)
    conn.commit()
    conn.close()


def _migrate_existing_tables(conn):
    """存量库幂等迁移：为已存在的表补充新增列（V20260812 合并套餐制/模块定价后的字段）"""
    alters = [
        # sub_items 新字段
        "ALTER TABLE sub_items ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'basic'",
        "ALTER TABLE sub_items ADD COLUMN IF NOT EXISTS features_json TEXT DEFAULT '[]'",
        "ALTER TABLE sub_items ADD COLUMN IF NOT EXISTS trial_days BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE sub_items ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'CNY'",
        "ALTER TABLE sub_items ADD COLUMN IF NOT EXISTS billing_mode TEXT NOT NULL DEFAULT 'continuous'",
        "ALTER TABLE sub_items ADD COLUMN IF NOT EXISTS trial_daily_limit BIGINT",
        "ALTER TABLE sub_items ADD COLUMN IF NOT EXISTS post_trial_action TEXT NOT NULL DEFAULT 'lock'",
        "ALTER TABLE sub_items ADD COLUMN IF NOT EXISTS refund_days BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE sub_items ADD COLUMN IF NOT EXISTS limit_even_byok BIGINT NOT NULL DEFAULT 0",
        # user_subscriptions 新字段
        "ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS tier TEXT DEFAULT ''",
        "ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS module_states TEXT DEFAULT '{}'",
        "ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS retry_count BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS last_charge_at TEXT",
        "ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS grace_end TEXT",
        "ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS payment_method TEXT DEFAULT ''",
        "ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS agreement_id TEXT DEFAULT ''",
        "ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS canceled_at TEXT",
        "ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS cancel_reason TEXT DEFAULT ''",
        # sub_orders 新字段
        "ALTER TABLE sub_orders ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'CNY'",
        "ALTER TABLE sub_orders ADD COLUMN IF NOT EXISTS fail_reason TEXT DEFAULT ''",
        "ALTER TABLE sub_orders ADD COLUMN IF NOT EXISTS notify_id TEXT DEFAULT ''",
        "ALTER TABLE sub_orders ADD COLUMN IF NOT EXISTS notify_raw TEXT DEFAULT ''",
    ]
    for sql in alters:
        try:
            conn.execute(sql)
        except Exception as e:
            print(f'[Subscription/Migrate] ALTER skipped ({sql[:60]}...): {e}')
            conn.rollback()


# ── 默认 SKU 种子数据 ──────────────────────────────────────────────────

DEFAULT_ITEMS = [
    # 系统底座
    {
        'item_key': 'base',
        'category': 'base',
        'tier': 'premium',
        'name_zh': _('System Base'),
        'name_en': 'System Base',
        'description_zh': 'CMS 内容管理 + Agent 矩阵 + 口令控制台 + 模型配置 + 邮件服务 + AI 图片生成',
        'description_en': 'CMS + Agent Matrix + Command Console + Model Config + Email + AI Image Gen',
        'price_month': 9900,
        'price_year': 106800,
        'sort_order': 1,
        'auto_activate': 'email',
    },
    # 小程序网关
    {
        'item_key': 'miniapp_wechat',
        'category': 'miniapp',
        'tier': 'popular',
        'name_zh': _('WeChat Mini Program'),
        'name_en': 'WeChat Mini App',
        'description_zh': '生成并发布微信小程序',
        'description_en': 'Generate and publish WeChat Mini App',
        'price_month': 9900,
        'price_year': 106800,
        'sort_order': 10,
    },
    {
        'item_key': 'miniapp_douyin',
        'category': 'miniapp',
        'tier': 'popular',
        'name_zh': _('TikTok Mini Program'),
        'name_en': 'Toutiao Mini App',
        'description_zh': '生成并发布抖音小程序',
        'description_en': 'Generate and publish Toutiao Mini App',
        'price_month': 9900,
        'price_year': 106800,
        'sort_order': 11,
    },
    {
        'item_key': 'miniapp_telegram',
        'category': 'miniapp',
        'tier': 'popular',
        'name_zh': _('Telegram Mini Program'),
        'name_en': 'Telegram Mini App',
        'description_zh': '生成并发布 Telegram Mini App',
        'description_en': 'Generate and publish Telegram Mini App',
        'price_month': 9900,
        'price_year': 106800,
        'sort_order': 12,
    },
    {
        'item_key': 'miniapp_line',
        'category': 'miniapp',
        'tier': 'popular',
        'name_zh': _('LINE Mini Program'),
        'name_en': 'LINE Mini App',
        'description_zh': '生成并发布 LINE 小程序',
        'description_en': 'Generate and publish LINE Mini App',
        'price_month': 9900,
        'price_year': 106800,
        'sort_order': 13,
    },
    # 能力项
    {
        'item_key': 'api_management',
        'category': 'feature',
        'tier': 'basic',
        'name_zh': _('API Management'),
        'name_en': 'API Management',
        'description_zh': 'API 密钥生成、额度管理、调用统计、访问日志',
        'description_en': 'API key generation, quota management, call stats, access logs',
        'price_month': 1500,
        'price_year': 16200,
        'sort_order': 20,
    },
    {
        'item_key': 'enterprise_verify',
        'category': 'feature',
        'tier': 'basic',
        'name_zh': _('Enterprise Certification'),
        'name_en': 'Enterprise Verification',
        'description_zh': '企业资质认证服务',
        'description_en': 'Enterprise qualification verification',
        'price_month': 1000,
        'price_year': 10800,
        'sort_order': 21,
    },
    {
        'item_key': 'oauth_config',
        'category': 'feature',
        'tier': 'basic',
        'name_zh': _('OAuth Login Configuration'),
        'name_en': 'OAuth Login Config',
        'description_zh': '配置第三方 OAuth 登录（Google/GitHub/Facebook）',
        'description_en': 'Configure third-party OAuth login (Google/GitHub/Facebook)',
        'price_month': 1000,
        'price_year': 10800,
        'sort_order': 22,
    },
    {
        'item_key': 'site_domains',
        'category': 'feature',
        'tier': 'basic',
        'name_zh': _('Custom Domain'),
        'name_en': 'Custom Domain',
        'description_zh': '绑定独立域名到你的站点',
        'description_en': 'Bind a custom domain to your site',
        'price_month': 1000,
        'price_year': 10800,
        'sort_order': 23,
    },
    {
        'item_key': 'analytics',
        'category': 'feature',
        'tier': 'basic',
        'name_zh': _('Data analysis'),
        'name_en': 'Analytics',
        'description_zh': '站点访问统计与趋势分析',
        'description_en': 'Site traffic stats and trend analysis',
        'price_month': 1500,
        'price_year': 16200,
        'sort_order': 24,
    },
    {
        'item_key': 'health_check',
        'category': 'feature',
        'tier': 'basic',
        'name_zh': _('Health Check'),
        'name_en': 'Health Check',
        'description_zh': '系统健康监控与自动告警',
        'description_en': 'System health monitoring and auto-alerting',
        'price_month': 1000,
        'price_year': 16200,
        'sort_order': 25,
    },
    {
        'item_key': 'sms_service',
        'category': 'feature',
        'tier': 'basic',
        'name_zh': _('SMS Service'),
        'name_en': 'SMS Service',
        'description_zh': '短信验证码与通知发送',
        'description_en': 'SMS verification and notification',
        'price_month': 1500,
        'price_year': 16200,
        'sort_order': 26,
    },
    {
        'item_key': 'social_push',
        'category': 'feature',
        'tier': 'basic',
        'name_zh': _('Social Media Push'),
        'name_en': 'Social Push',
        'description_zh': '自动推送内容到社交媒体平台',
        'description_en': 'Auto-publish content to social media',
        'price_month': 1500,
        'price_year': 16200,
        'sort_order': 27,
    },
    {
        'item_key': 'content_factory',
        'category': 'feature',
        'tier': 'basic',
        'name_zh': _('Content Factory'),
        'name_en': 'Content Factory',
        'description_zh': 'AI 批量内容生成',
        'description_en': 'AI batch content generation',
        'price_month': 1500,
        'price_year': 16200,
        'sort_order': 28,
    },
    {
        'item_key': 'automation',
        'category': 'feature',
        'tier': 'basic',
        'name_zh': _('Auto Task'),
        'name_en': 'Automation',
        'description_zh': '定时自动执行工作流',
        'description_en': 'Scheduled workflow automation',
        'price_month': 1000,
        'price_year': 10800,
        'sort_order': 29,
    },
    {
        'item_key': 'logistics',
        'category': 'feature',
        'tier': 'basic',
        'name_zh': _('Logistics Inquiry'),
        'name_en': 'Logistics',
        'description_zh': '国际物流追踪查询',
        'description_en': 'International logistics tracking',
        'price_month': 1000,
        'price_year': 10800,
        'sort_order': 30,
    },
]


def seed_default_items():
    """种子 SKU 目录（INSERT ... ON CONFLICT，不覆盖已有数据）"""
    conn = PgConnection(get_raw_connection())
    conn.execute("CREATE SCHEMA IF NOT EXISTS subscription")
    conn.execute("SET search_path TO subscription")
    for item in DEFAULT_ITEMS:
        conn.execute("""
            INSERT INTO sub_items
                (item_key, category, name_zh, name_en, description_zh, description_en,
                 price_month, price_year, sort_order, auto_activate, tier)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (item_key) DO NOTHING
        """, (
            item['item_key'], item['category'],
            item['name_zh'], item['name_en'],
            item['description_zh'], item['description_en'],
            item['price_month'], item['price_year'],
            item['sort_order'], item.get('auto_activate', ''),
            item.get('tier', 'basic'),   # M5: 显式写入 tier 分档
        ))
    conn.commit()
    conn.close()


# ── 数据类 ──────────────────────────────────────────────────────────────

@dataclass
class SubItem:
    """可订阅项"""
    item_key: str
    category: str
    name_zh: str
    name_en: str
    price_month: int
    price_year: int
    description_zh: str = ''
    description_en: str = ''
    tier: str = 'premium'
    features_json: str = '[]'
    trial_days: int = 0
    currency: str = 'CNY'
    billing_mode: str = 'continuous'
    trial_daily_limit: Optional[int] = None
    post_trial_action: str = 'lock'
    refund_days: int = 0
    limit_even_byok: int = 0
    is_active: bool = True
    auto_activate: str = ''
    sort_order: int = 0
    id: Optional[int] = None

    def to_dict(self, locale: str = 'zh-CN') -> dict:
        import json as _json
        features = []
        try:
            features = _json.loads(self.features_json or '[]')
        except Exception:
            features = []
        return {
            'id': self.id,
            'item_key': self.item_key,
            'category': self.category,
            'name': self.name_zh if locale == 'zh-CN' else self.name_en,
            'description': self.description_zh if locale == 'zh-CN' else self.description_en,
            'price_month': self.price_month,
            'price_year': self.price_year,
            'price_month_yuan': f'{self.price_month / 100:.2f}',
            'price_year_yuan': f'{self.price_year / 100:.2f}',
            'tier': self.tier,
            'features': features,
            'trial_days': self.trial_days,
            'currency': self.currency,
            'billing_mode': self.billing_mode,
            'trial_daily_limit': self.trial_daily_limit,
            'post_trial_action': self.post_trial_action,
            'refund_days': self.refund_days,
            'limit_even_byok': self.limit_even_byok,
            'is_active': self.is_active,
            'sort_order': self.sort_order,
        }

    @classmethod
    def from_row(cls, row: dict) -> 'SubItem':
        return cls(
            id=row['id'],
            item_key=row['item_key'],
            category=row['category'],
            name_zh=row['name_zh'],
            name_en=row['name_en'],
            description_zh=row.get('description_zh', ''),
            description_en=row.get('description_en', ''),
            price_month=row['price_month'],
            price_year=row['price_year'],
            tier=row.get('tier', 'premium'),
            features_json=row.get('features_json', '[]'),
            trial_days=row.get('trial_days', 0),
            currency=row.get('currency', 'CNY'),
            billing_mode=row.get('billing_mode', 'continuous'),
            trial_daily_limit=row.get('trial_daily_limit'),
            post_trial_action=row.get('post_trial_action', 'lock'),
            refund_days=row.get('refund_days', 0),
            limit_even_byok=row.get('limit_even_byok', 0),
            is_active=bool(row.get('is_active', 1)),
            auto_activate=row.get('auto_activate', ''),
            sort_order=row.get('sort_order', 0),
        )


@dataclass
class UserSubscription:
    """用户订阅"""
    user_id: int
    item_key: str
    interval_type: str
    amount_fen: int
    period_start: str
    period_end: str
    status: SubStatus = SubStatus.ACTIVE
    tier: str = ''
    module_states: str = '{}'
    retry_count: int = 0
    last_charge_at: Optional[str] = None
    grace_end: Optional[str] = None
    payment_method: str = ''
    agreement_id: str = ''
    auto_renew: bool = True
    canceled_at: Optional[str] = None
    cancel_reason: str = ''
    order_no: str = ''
    id: Optional[int] = None

    def to_dict(self) -> dict:
        import json as _json
        module_states = {}
        try:
            module_states = _json.loads(self.module_states or '{}')
        except Exception:
            module_states = {}
        return {
            'id': self.id,
            'user_id': self.user_id,
            'item_key': self.item_key,
            'interval_type': self.interval_type,
            'amount_fen': self.amount_fen,
            'amount_yuan': f'{self.amount_fen / 100:.2f}',
            'status': self.status.value,
            'tier': self.tier,
            'module_states': module_states,
            'retry_count': self.retry_count,
            'grace_end': self.grace_end,
            'payment_method': self.payment_method,
            'period_start': self.period_start,
            'period_end': self.period_end,
            'auto_renew': self.auto_renew,
            'canceled_at': self.canceled_at,
            'cancel_reason': self.cancel_reason,
            'order_no': self.order_no,
        }

    @classmethod
    def from_row(cls, row: dict) -> 'UserSubscription':
        return cls(
            id=row['id'],
            user_id=row['user_id'],
            item_key=row['item_key'],
            interval_type=row['interval_type'],
            amount_fen=row['amount_fen'],
            period_start=row['period_start'],
            period_end=row['period_end'],
            status=SubStatus(row['status']),
            tier=row.get('tier', ''),
            module_states=row.get('module_states', '{}'),
            retry_count=row.get('retry_count', 0),
            last_charge_at=row.get('last_charge_at'),
            grace_end=row.get('grace_end'),
            payment_method=row.get('payment_method', ''),
            agreement_id=row.get('agreement_id', ''),
            auto_renew=bool(row.get('auto_renew', 1)),
            canceled_at=row.get('canceled_at'),
            cancel_reason=row.get('cancel_reason', ''),
            order_no=row.get('order_no', ''),
        )


@dataclass
class SubOrder:
    """订阅订单"""
    order_no: str
    user_id: int
    item_key: str
    interval_type: str
    amount_fen: int
    channel: str
    status: OrderStatus = OrderStatus.PENDING
    currency: str = 'CNY'
    trade_no: str = ''
    qr_code: str = ''
    redirect_url: str = ''
    fail_reason: str = ''
    notify_id: str = ''
    notify_raw: str = ''
    paid_at: Optional[str] = None
    created_at: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'order_no': self.order_no,
            'user_id': self.user_id,
            'item_key': self.item_key,
            'interval_type': self.interval_type,
            'amount_fen': self.amount_fen,
            'amount_yuan': f'{self.amount_fen / 100:.2f}',
            'currency': self.currency,
            'channel': self.channel,
            'status': self.status.value,
            'trade_no': self.trade_no,
            'qr_code': self.qr_code,
            'redirect_url': self.redirect_url,
            'fail_reason': self.fail_reason,
            'paid_at': self.paid_at,
            'created_at': self.created_at,
        }

    @classmethod
    def from_row(cls, row: dict) -> 'SubOrder':
        import json
        return cls(
            id=row['id'],
            order_no=row['order_no'],
            user_id=row['user_id'],
            item_key=row['item_key'],
            interval_type=row['interval_type'],
            amount_fen=row['amount_fen'],
            channel=row['channel'],
            status=OrderStatus(row['status']),
            currency=row.get('currency', 'CNY'),
            trade_no=row.get('trade_no', ''),
            qr_code=row.get('qr_code', ''),
            redirect_url=row.get('redirect_url', ''),
            fail_reason=row.get('fail_reason', ''),
            notify_id=row.get('notify_id', ''),
            notify_raw=row.get('notify_raw', ''),
            paid_at=row.get('paid_at'),
            created_at=row.get('created_at'),
            extra=json.loads(row.get('extra', '{}')),
        )
