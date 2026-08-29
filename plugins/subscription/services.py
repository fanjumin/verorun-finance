#!/usr/bin/env python3
"""
Subscription Plugin — 核心业务层
==================================
SubscriptionService: 订阅/取消/续费/查询/权限检查
支付路由: 根据 DEPLOY_MARKET 自动选择支付渠道
"""

import os
import json
import secrets
import time
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta

from plugins._base.db import get_raw_connection, PgConnection

from .models import (
    SubItem, UserSubscription, SubOrder,
    SubStatus, OrderStatus,
)


def init_i18n(t_func):
    """由 __init__.py 在 on_enable 时调用注入翻译函数（注入到服务单例，L-05）"""
    get_subscription_service().set_t(t_func)


# ── 支付渠道路由（双环境） ──────────────────────────────────────────────

def get_market() -> str:
    """返回当前市场: 'cn' | 'intl'"""
    return os.environ.get('DEPLOY_MARKET', 'cn')


def get_default_payment_channel() -> str:
    """根据 DEPLOY_MARKET 返回默认支付渠道"""
    return 'stripe' if get_market() == 'intl' else 'alipay'


def get_available_channels() -> List[str]:
    """返回当前市场可用的支付渠道列表"""
    if get_market() == 'intl':
        return ['stripe', 'paypal']
    return ['alipay', 'wechat']


# Dunning 重试计划（对齐 A: auth-center/routes/subscription/renewal.py）
DUNNING_DAYS = [1, 3, 7]
GRACE_DAYS = 7

# 权限 tier 优先级（对齐 A: subscription_plans 的 basic/popular/premium）
TIER_RANK = {'free': 0, 'basic': 1, 'popular': 2, 'premium': 3}


# ── 订阅服务 ────────────────────────────────────────────────────────────

class SubscriptionService:
    """订阅管理核心服务"""

    def __init__(self, t_func=None):
        self._t = t_func or (lambda text, **kwargs: text)

    def set_t(self, t_func):
        """运行期注入翻译函数（L-05：避免模块级全局 _t 的多线程竞态）"""
        self._t = t_func or (lambda text, **kwargs: text)

    def _get_conn(self):
        # 配套修复：get_raw_connection() 返回原始 psycopg2 连接，
        # 必须包一层 PgConnection（提供 execute/上下文管理器与占位符兼容），
        # 否则下方所有 conn.execute() 调用都会 AttributeError。
        conn = PgConnection(get_raw_connection())
        conn.execute("CREATE SCHEMA IF NOT EXISTS subscription")
        conn.execute("SET search_path TO subscription")
        return conn

    # ── SKU 目录查询 ───────────────────────────────────────────────

    def list_items(self, locale: str = 'zh-CN', active_only: bool = True) -> List[Dict]:
        """获取所有可订阅项"""
        with self._get_conn() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM sub_items WHERE is_active=1 ORDER BY sort_order"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sub_items ORDER BY sort_order"
                ).fetchall()
            return [SubItem.from_row(dict(r)).to_dict(locale) for r in rows]

    def get_item(self, item_key: str) -> Optional[SubItem]:
        """获取单个 SKU"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM sub_items WHERE item_key=%s", (item_key,)
            ).fetchone()
            if row:
                return SubItem.from_row(dict(row))
        return None

    def upsert_item(self, item_data: Dict) -> bool:
        """管理后台：创建或更新 SKU"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO sub_items
                    (item_key, category, name_zh, name_en, description_zh, description_en,
                     price_month, price_year, is_active, auto_activate, sort_order, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT(item_key) DO UPDATE SET
                    category=excluded.category,
                    name_zh=excluded.name_zh,
                    name_en=excluded.name_en,
                    description_zh=excluded.description_zh,
                    description_en=excluded.description_en,
                    price_month=excluded.price_month,
                    price_year=excluded.price_year,
                    is_active=excluded.is_active,
                    auto_activate=excluded.auto_activate,
                    sort_order=excluded.sort_order,
                    updated_at=NOW()
            """, (
                item_data['item_key'],
                item_data.get('category', 'plugin'),
                item_data.get('name_zh', ''),
                item_data.get('name_en', ''),
                item_data.get('description_zh', ''),
                item_data.get('description_en', ''),
                item_data.get('price_month', 0),
                item_data.get('price_year', 0),
                int(item_data.get('is_active', 1)),
                item_data.get('auto_activate', ''),
                item_data.get('sort_order', 0),
            ))
            conn.commit()
        return True

    # ── 用户订阅查询 ───────────────────────────────────────────────

    def get_user_subscriptions(self, user_id: int) -> List[UserSubscription]:
        """获取用户所有订阅"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM user_subscriptions WHERE user_id=%s ORDER BY created_at DESC",
                (user_id,)
            ).fetchall()
            return [UserSubscription.from_row(dict(r)) for r in rows]

    def list_all_subscriptions(self, limit: int = 100, offset: int = 0) -> List[UserSubscription]:
        """管理员：全部订阅"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM user_subscriptions ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (limit, offset)
            ).fetchall()
            return [UserSubscription.from_row(dict(r)) for r in rows]

    def get_user_subscription(self, user_id: int, item_key: str) -> Optional[UserSubscription]:
        """获取用户某个订阅"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM user_subscriptions WHERE user_id=%s AND item_key=%s",
                (user_id, item_key)
            ).fetchone()
            if row:
                return UserSubscription.from_row(dict(row))
        return None

    def has_subscription(self, user_id: int, item_key: str) -> bool:
        """检查用户是否有某个有效订阅（含自动开通）"""
        # 先查直接订阅
        sub = self.get_user_subscription(user_id, item_key)
        if sub and sub.status == SubStatus.ACTIVE:
            return True

        # 再查 base 自动开通的项
        base_sub = self.get_user_subscription(user_id, 'base')
        if base_sub and base_sub.status == SubStatus.ACTIVE:
            base_item = self.get_item('base')
            if base_item and base_item.auto_activate:
                auto_items = [x.strip() for x in base_item.auto_activate.split(',') if x.strip()]
                if item_key in auto_items:
                    return True

        return False

    def get_active_features(self, user_id: int) -> List[str]:
        """获取用户当前所有活跃的 feature key 列表"""
        subs = self.get_user_subscriptions(user_id)
        active = [s.item_key for s in subs if s.status == SubStatus.ACTIVE]

        # base 自动开通的项
        if 'base' in active:
            base_item = self.get_item('base')
            if base_item and base_item.auto_activate:
                auto_items = [x.strip() for x in base_item.auto_activate.split(',') if x.strip()]
                for ai in auto_items:
                    if ai not in active:
                        active.append(ai)

        return active

    # ── 模块门控（U5：对齐 agent_matrix module_policy 消费端） ─────

    def check_module_access(self, user_id: int, module_key: str) -> Tuple[bool, str]:
        """检查用户是否可用某模块（module gating）

        匹配规则：
        1. 用户 active 订阅的 item_key == module_key
        2. 用户 active 订阅的 features_json 数组包含 module_key
        均不匹配 → 拒绝。试用逻辑由上层/Phase 2 决定。

        Returns:
            Tuple[bool, str]: (allowed, reason)
        """
        subs = self.get_user_subscriptions(user_id)
        active = [s for s in subs if s.status == SubStatus.ACTIVE]
        if not active:
            return False, 'No active subscription'

        for sub in active:
            if sub.item_key == module_key:
                return True, ''
            item = self.get_item(sub.item_key)
            if not item or not item.features_json:
                continue
            try:
                features = json.loads(item.features_json)
            except (ValueError, TypeError):
                features = [x.strip() for x in str(item.features_json).split(',') if x.strip()]
            if isinstance(features, list) and module_key in features:
                return True, ''

        return False, f'Module {module_key} requires subscription'

    # ── 站点建站套餐（U5：兼容 main_site site_* 消费端） ───────────

    def list_site_plans(self, active_only: bool = True) -> List[Dict]:
        """站点建站套餐列表（兼容 A: subscription_plans WHERE plan_key LIKE 'site_%'）"""
        with self._get_conn() as conn:
            sql = "SELECT * FROM sub_items WHERE item_key LIKE 'site_%'"
            if active_only:
                sql += " AND is_active=1"
            sql += " ORDER BY sort_order"
            rows = conn.execute(sql).fetchall()
            return [SubItem.from_row(dict(r)).to_dict('zh-CN') for r in rows]

    def get_site_plan(self, plan_key: str) -> Optional[SubItem]:
        """单个站点套餐"""
        return self.get_item(plan_key)

    # ── 收入统计（U6：兼容 A 的 Revenue Dashboard） ────────────────

    def get_stats(self) -> Dict[str, Any]:
        """收入统计：今日/本月收入、MRR、近30天趋势、订阅/订单概览"""
        with self._get_conn() as conn:
            today = dict(conn.execute(
                "SELECT COALESCE(SUM(amount_fen),0) AS s FROM sub_orders "
                "WHERE status='paid' AND date(paid_at::timestamp)=CURRENT_DATE"
            ).fetchone())
            month = dict(conn.execute(
                "SELECT COALESCE(SUM(amount_fen),0) AS s FROM sub_orders "
                "WHERE status='paid' "
                "  AND date_trunc('month', paid_at::timestamp)=date_trunc('month', NOW())"
            ).fetchone())
            trend_rows = conn.execute(
                "SELECT date(paid_at::timestamp) AS d, COALESCE(SUM(amount_fen),0) AS s "
                "FROM sub_orders WHERE status='paid' AND paid_at IS NOT NULL "
                "  AND paid_at::timestamp >= NOW() - INTERVAL '30 days' "
                "GROUP BY d ORDER BY d"
            ).fetchall()
            active_subs = dict(conn.execute(
                "SELECT COUNT(*) AS c FROM user_subscriptions WHERE status='active'"
            ).fetchone())
            mrr = dict(conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN u.interval_type='month' "
                "  THEN i.price_month ELSE i.price_year/12.0 END),0) AS s "
                "FROM user_subscriptions u JOIN sub_items i ON i.item_key=u.item_key "
                "WHERE u.status='active'"
            ).fetchone())
            pending = dict(conn.execute(
                "SELECT COUNT(*) AS c FROM sub_orders WHERE status='pending'"
            ).fetchone())

        trend = []
        for r in trend_rows:
            rd = dict(r)
            d = rd['d']
            trend.append({
                'date': d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d),
                'revenue': float(rd['s'] or 0) / 100.0,
            })
        return {
            'today_revenue': float(today['s'] or 0) / 100.0,
            'month_revenue': float(month['s'] or 0) / 100.0,
            'mrr': float(mrr['s'] or 0) / 100.0,
            'active_subscriptions': active_subs['c'],
            'pending_orders': pending['c'],
            'revenue_trend_30d': trend,
        }

    # ── 计费日志（U6：兼容 A 的 Billing Log） ──────────────────────

    def list_billing_events(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """计费事件日志（sub_payment_events）"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sub_payment_events ORDER BY id DESC LIMIT %s OFFSET %s",
                (limit, offset)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── 发票（U7：sub_invoices） ──────────────────────────────────

    def list_invoices(self, user_id: int, limit: int = 20) -> List[Dict]:
        """我的发票列表（sub_invoices）"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sub_invoices WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_invoice(self, user_id: int, invoice_no: str) -> Optional[Dict]:
        """单个发票（校验归属）"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM sub_invoices WHERE invoice_no=%s AND user_id=%s",
                (invoice_no, user_id)
            ).fetchone()
            return dict(row) if row else None

    # ── 部署码（U6：兼容 A 的 Deploy Codes） ────────────────────────

    def list_deploy_codes(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """部署码列表（不返回 code_hash）"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM deploy_codes ORDER BY id DESC LIMIT %s OFFSET %s",
                (limit, offset)
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d.pop('code_hash', None)
                out.append(d)
            return out

    def create_deploy_code(self, user_id: int, item_key: str = 'deploy_basic',
                           duration_days: int = 365) -> str:
        """生成部署码（存 SHA256 哈希）"""
        import hashlib
        code = f'VR-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}'
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        expires_at = (datetime.now() + timedelta(days=duration_days)).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO deploy_codes (code, code_hash, user_id, item_key, duration_days, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (code, code_hash, user_id, item_key, duration_days, expires_at))
            conn.commit()
        return code

    def update_deploy_code_status(self, code: str, status: str) -> Tuple[bool, str]:
        """更新部署码状态（active/used/expired/revoked）"""
        valid = {'active', 'used', 'expired', 'revoked'}
        if status not in valid:
            return False, f'Invalid status: {status}'
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE deploy_codes SET status=%s, updated_at=NOW() WHERE code=%s",
                (status, code))
            conn.commit()
        return True, 'ok'

    # ── 创建订阅 ──────────────────────────────────────────────────

    def subscribe(self, user_id: int, item_key: str, interval_type: str,
                  channel: str = None) -> Tuple[bool, str, Optional[dict]]:
        """创建订阅订单，返回 (success, message, order_data)

        订单创建后返回支付信息，支付成功后才正式创建 user_subscriptions 记录。
        """
        # 检查 SKU 是否存在且活跃
        item = self.get_item(item_key)
        if not item:
            return False, self._t('Subscription item not found'), None
        if not item.is_active:
            return False, self._t('Subscription item is not available'), None

        # 检查是否已订阅
        existing = self.get_user_subscription(user_id, item_key)
        if existing and existing.status == SubStatus.ACTIVE:
            return False, self._t('Already subscribed'), None

        # 计算价格
        if interval_type not in ('month', 'year'):
            return False, self._t('Invalid interval type'), None

        amount_fen = item.price_month if interval_type == 'month' else item.price_year
        if amount_fen <= 0:
            return False, self._t('Invalid price'), None

        # 选择支付渠道
        if channel is None:
            channel = get_default_payment_channel()

        # M2: 渠道必须属于当前市场（DEPLOY_MARKET），拒绝客户端任意指定
        if channel not in get_available_channels():
            return False, self._t('Invalid payment channel for current market'), None

        # 创建订单
        order_no = f'SUB{int(time.time())}{secrets.token_hex(4).upper()}'
        now = datetime.now()

        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO sub_orders
                    (order_no, user_id, item_key, interval_type, amount_fen, channel, status, extra)
                VALUES (%s,%s,%s,%s,%s,%s,'pending','{}')
            """, (order_no, user_id, item_key, interval_type, amount_fen, channel))
            conn.commit()

        # 调用支付网关创建支付
        from plugins.payment.gateways import create_payment
        pay_result = create_payment(
            order_no=order_no,
            amount_fen=amount_fen,
            subject=item.name_zh,
            description=item.description_zh,
            channel=channel,
            interval_type=interval_type,
        )

        # C-02：支付网关未配置/创建失败时，将订单标记为 failed，避免堆积无法支付的 pending 订单
        if not pay_result.get('success'):
            err_msg = pay_result.get('error', 'Payment gateway error')
            print(f'[Subscription] Payment creation failed: {err_msg}')
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE sub_orders SET status='failed', updated_at=NOW() WHERE order_no=%s",
                    (order_no,)
                )
                conn.commit()
            return False, self._t('Payment creation failed: {error}').format(error=err_msg), None

        # 更新订单支付信息
        with self._get_conn() as conn:
            if pay_result.get('qr_code') or pay_result.get('redirect_url'):
                conn.execute("""
                    UPDATE sub_orders SET
                        qr_code=%s, redirect_url=%s, trade_no=%s, updated_at=NOW()
                    WHERE order_no=%s
                """, (
                    pay_result.get('qr_code', ''),
                    pay_result.get('redirect_url', ''),
                    pay_result.get('trade_no', ''),
                    order_no,
                ))
                conn.commit()

        order_data = {
            'order_no': order_no,
            'amount_fen': amount_fen,
            'amount_yuan': f'{amount_fen / 100:.2f}',
            'channel': channel,
            'qr_code': pay_result.get('qr_code', ''),
            'redirect_url': pay_result.get('redirect_url', ''),
            'interval_type': interval_type,
            'item_name': item.name_zh if get_market() == 'cn' else item.name_en,
        }

        return True, 'ok', order_data

    # ── 重试支付（U7：对 pending 订单重新发起支付） ────────────────

    def retry_payment(self, order_no: str) -> Tuple[bool, str, Optional[dict]]:
        """对 pending 订单重新调用支付网关，生成新的二维码/跳转链接"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM sub_orders WHERE order_no=%s", (order_no,)
            ).fetchone()
        if not row:
            return False, self._t('Order not found'), None
        order = SubOrder.from_row(dict(row))
        if order.status != 'pending':
            return False, self._t('Only pending orders can be retried'), None

        item = self.get_item(order.item_key)
        if not item:
            return False, self._t('Subscription item not found'), None

        channel = order.channel or get_default_payment_channel()
        from plugins.payment.gateways import create_payment
        pay_result = create_payment(
            order_no=order_no,
            amount_fen=order.amount_fen,
            subject=item.name_zh,
            description=item.description_zh,
            channel=channel,
            interval_type=order.interval_type,
        )

        if not pay_result.get('success'):
            err_msg = pay_result.get('error', 'Payment gateway error')
            print(f'[Subscription] Retry payment creation failed: {err_msg}')
            return False, self._t('Payment creation failed: {error}').format(error=err_msg), None

        with self._get_conn() as conn:
            conn.execute("""
                UPDATE sub_orders SET
                    qr_code=%s, redirect_url=%s, trade_no=%s, status='pending', updated_at=NOW()
                WHERE order_no=%s
            """, (
                pay_result.get('qr_code', ''),
                pay_result.get('redirect_url', ''),
                pay_result.get('trade_no', ''),
                order_no,
            ))
            conn.commit()

        order_data = {
            'order_no': order_no,
            'amount_fen': order.amount_fen,
            'amount_yuan': f'{order.amount_fen / 100:.2f}',
            'channel': channel,
            'qr_code': pay_result.get('qr_code', ''),
            'redirect_url': pay_result.get('redirect_url', ''),
            'interval_type': order.interval_type,
            'item_name': item.name_zh if get_market() == 'cn' else item.name_en,
        }
        return True, 'ok', order_data

    # ── 支付成功回调 ──────────────────────────────────────────────

    def _generate_invoice(self, order) -> None:
        """支付成功后生成发票（sub_invoices 表 + data/invoices PDF）

        发票记录为核心（必写）；PDF 为附加（fpdf2/字体缺失时留空，
        download API 有 JSON 兜底）。失败仅告警，不影响支付主流程。
        """
        invoice_no = self._new_invoice_no()
        amount_yuan = float(order.amount_fen) / 100.0
        item = self.get_item(order.item_key)
        plan_name = item.name_zh if item else order.item_key
        period_text = f'{order.interval_type}'

        pdf_filename = ''
        try:
            import sys as _sys
            _auth_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                'auth-center')
            if _auth_dir not in _sys.path:
                _sys.path.insert(0, _auth_dir)
            from services.invoice_service import generate_invoice_pdf
            _, pdf_filename = generate_invoice_pdf(
                order_no=order.order_no,
                user_name=f'User#{order.user_id}',
                plan_name=plan_name,
                period_text=period_text,
                amount_fen=order.amount_fen,
                invoice_no=invoice_no,
            )
        except Exception as e:
            print(f'[Subscription] Invoice PDF skipped for {order.order_no}: {e}')

        try:
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT INTO sub_invoices
                        (invoice_no, order_no, user_id, amount_fen, amount_yuan,
                         plan_name, period_text, pdf_path, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'issued')
                """, (invoice_no, order.order_no, order.user_id, order.amount_fen,
                      amount_yuan, plan_name, period_text, pdf_filename))
                conn.commit()
        except Exception as e:
            print(f'[Subscription] Invoice record failed for {order.order_no}: {e}')

    @staticmethod
    def _new_invoice_no() -> str:
        """生成发票号：INV + 日期 + 6位安全随机（L4: 改用 secrets 防可预测/撞号）"""
        import secrets
        import string
        date_part = datetime.now().strftime('%Y%m%d')
        rand_part = ''.join(secrets.choice(string.digits) for _ in range(6))
        return f'INV{date_part}{rand_part}'

    def on_payment_success(self, order_no: str, trade_no: str = '',
                           parsed: dict = None) -> Tuple[bool, str]:
        """支付成功后：标记订单 + 创建/续费用户订阅"""
        with self._get_conn() as conn:
            # H-02：SELECT ... FOR UPDATE 行级锁防止重复回调并发处理。
            # 并发请求会阻塞于此，待本事务提交后再查询时 status 已非 pending，直接返回已处理。
            order_row = conn.execute(
                "SELECT * FROM sub_orders WHERE order_no=%s AND status='pending' FOR UPDATE",
                (order_no,)
            ).fetchone()

            if not order_row:
                return False, 'Order not found or already processed'

            order = SubOrder.from_row(dict(order_row))

            # M1 金额校验：回调金额必须与订单金额一致（1 分容差）
            callback_fen = None
            if parsed:
                if parsed.get('total_amount'):          # 支付宝回调金额单位为元
                    try:
                        callback_fen = int(round(float(parsed['total_amount']) * 100))
                    except (TypeError, ValueError):
                        callback_fen = None
                elif parsed.get('total_fee'):           # 微信回调金额单位为分
                    try:
                        callback_fen = int(parsed['total_fee'])
                    except (TypeError, ValueError):
                        callback_fen = None
            if callback_fen is not None and abs(callback_fen - order.amount_fen) > 1:
                print(f'[Subscription] SECURITY: amount mismatch '
                      f'order={order.amount_fen} notify={callback_fen}')
                return False, 'Amount mismatch'

            # 更新订单状态
            conn.execute("""
                UPDATE sub_orders SET
                    status='paid', trade_no=%s, paid_at=NOW(), updated_at=NOW()
                WHERE order_no=%s
            """, (trade_no, order_no))

            # 创建或续费订阅
            now = datetime.now()
            interval = order.interval_type
            period_end = self._calc_period_end(now, interval)

            existing = conn.execute(
                "SELECT * FROM user_subscriptions WHERE user_id=%s AND item_key=%s",
                (order.user_id, order.item_key)
            ).fetchone()

            if existing:
                ex = UserSubscription.from_row(dict(existing))
                if ex.status in (SubStatus.EXPIRED, SubStatus.CANCELED):
                    # 重新激活
                    conn.execute("""
                        UPDATE user_subscriptions SET
                            status='active', interval_type=%s, amount_fen=%s,
                            period_start=%s, period_end=%s, auto_renew=1,
                            order_no=%s, updated_at=NOW()
                        WHERE user_id=%s AND item_key=%s
                    """, (interval, order.amount_fen, now.isoformat(), period_end.isoformat(),
                          order_no, order.user_id, order.item_key))
                else:
                    # 续费：延长 period_end
                    conn.execute("""
                        UPDATE user_subscriptions SET
                            status='active', interval_type=%s, amount_fen=%s,
                            period_start=%s, period_end=%s, auto_renew=1,
                            order_no=%s, updated_at=NOW()
                        WHERE user_id=%s AND item_key=%s
                    """, (interval, order.amount_fen, now.isoformat(), period_end.isoformat(),
                          order_no, order.user_id, order.item_key))
            else:
                conn.execute("""
                    INSERT INTO user_subscriptions
                        (user_id, item_key, interval_type, amount_fen, period_start, period_end,
                         auto_renew, order_no)
                    VALUES (%s,%s,%s,%s,%s,%s,1,%s)
                """, (order.user_id, order.item_key, interval, order.amount_fen,
                      now.isoformat(), period_end.isoformat(), order_no))

            # 处理 auto_activate：订阅 base 时自动开通关联项
            # M-05：改为同一连接内查询，避免在事务中另开连接（嵌套连接可能死锁/读不到未提交数据）
            if order.item_key == 'base':
                base_row = conn.execute(
                    "SELECT * FROM sub_items WHERE item_key=%s", ('base',)
                ).fetchone()
                auto_activate = ''
                if base_row:
                    auto_activate = (base_row.get('auto_activate') if isinstance(base_row, dict) else base_row['auto_activate']) or ''
                if auto_activate:
                    auto_items = [x.strip() for x in auto_activate.split(',') if x.strip()]
                    for ai in auto_items:
                        ai_existing = conn.execute(
                            "SELECT * FROM user_subscriptions WHERE user_id=%s AND item_key=%s",
                            (order.user_id, ai)
                        ).fetchone()
                        if not ai_existing:
                            conn.execute("""
                                INSERT INTO user_subscriptions
                                    (user_id, item_key, interval_type, amount_fen, period_start,
                                     period_end, auto_renew, order_no, status)
                                VALUES (%s,%s,'month',0,%s,%s,0,%s,'active')
                            """, (order.user_id, ai, now.isoformat(), period_end.isoformat(), order_no))

            conn.commit()

        # U4：订阅生效后同步主库权限（main tier）
        self._sync_authorizations(order.user_id)

        # U7：支付成功后生成发票（主库 invoices 表；失败不影响主流程）
        self._generate_invoice(order)

        return True, 'ok'

    # ── 取消订阅 ──────────────────────────────────────────────────

    def cancel(self, user_id: int, item_key: str, immediate: bool = False) -> Tuple[bool, str]:
        """取消订阅

        Args:
            immediate: True=立即过期, False=到期不续
        """
        sub = self.get_user_subscription(user_id, item_key)
        if not sub:
            return False, self._t('Subscription not found')

        # 不允许取消 base 自动开通的子项
        if item_key != 'base':
            base_item = self.get_item('base')
            if base_item and base_item.auto_activate:
                auto_items = [x.strip() for x in base_item.auto_activate.split(',') if x.strip()]
                if item_key in auto_items:
                    return False, self._t('This item is included in your Base subscription and cannot be canceled separately')

        with self._get_conn() as conn:
            if immediate:
                conn.execute("""
                    UPDATE user_subscriptions SET
                        status='canceled', auto_renew=0, updated_at=NOW()
                    WHERE user_id=%s AND item_key=%s
                """, (user_id, item_key))
            else:
                conn.execute("""
                    UPDATE user_subscriptions SET
                        auto_renew=0, updated_at=NOW()
                    WHERE user_id=%s AND item_key=%s
                """, (user_id, item_key))
            conn.commit()

        # U4：立即取消 → 若已无其他 active 订阅，权限降级为 free
        if immediate:
            self._sync_authorizations(user_id)

        return True, 'ok'

    # ── 续费 ──────────────────────────────────────────────────────

    def renew(self, user_id: int, item_key: str, channel: str = None) -> Tuple[bool, str, Optional[dict]]:
        """手动续费：创建续费订单"""
        sub = self.get_user_subscription(user_id, item_key)
        if not sub:
            return False, self._t('Subscription not found'), None

        item = self.get_item(item_key)
        if not item:
            return False, self._t('Subscription item not found'), None

        interval = sub.interval_type
        amount_fen = item.price_month if interval == 'month' else item.price_year

        if channel is None:
            channel = get_default_payment_channel()

        order_no = f'SUB{int(time.time())}{secrets.token_hex(4).upper()}'

        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO sub_orders
                    (order_no, user_id, item_key, interval_type, amount_fen, channel, status, extra)
                VALUES (%s,%s,%s,%s,%s,%s,'pending','{}')
            """, (order_no, user_id, item_key, interval, amount_fen, channel))
            conn.commit()

        from plugins.payment.gateways import create_payment
        pay_result = create_payment(
            order_no=order_no,
            amount_fen=amount_fen,
            subject=f"{item.name_zh} - {self._t('Renewal')}",
            description=item.description_zh,
            channel=channel,
            interval_type=interval,
        )

        # H3: 支付网关未配置/创建失败时，标记订单 failed 并返回错误（与 subscribe() 一致）
        if not pay_result.get('success'):
            err_msg = pay_result.get('error', 'Payment gateway error')
            print(f'[Subscription] Renew payment creation failed: {err_msg}')
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE sub_orders SET status='failed', updated_at=NOW() "
                    "WHERE order_no=%s",
                    (order_no,)
                )
                conn.commit()
            return False, self._t(
                'Payment creation failed: {error}'
            ).format(error=err_msg), None

        order_data = {
            'order_no': order_no,
            'amount_fen': amount_fen,
            'amount_yuan': f'{amount_fen / 100:.2f}',
            'channel': channel,
            'qr_code': pay_result.get('qr_code', ''),
            'redirect_url': pay_result.get('redirect_url', ''),
            'interval_type': interval,
        }
        return True, 'ok', order_data

    # ── 到期检查 ──────────────────────────────────────────────────

    def check_expired(self) -> List[UserSubscription]:
        """检查并处理到期订阅"""
        now = datetime.now().isoformat()
        expired = []

        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM user_subscriptions WHERE status='active' AND period_end < %s",
                (now,)
            ).fetchall()

            # M-01：先收集需批量更新的 id，循环结束后一次性提交（避免 N 次独立提交）
            non_renew_ids = []
            affected_users = set()                                   # H2: 收集受影响用户
            for row in rows:
                sub = UserSubscription.from_row(dict(row))
                if sub.auto_renew:
                    # 标记待自动续费（由 scheduler 处理）
                    expired.append(sub)
                else:
                    sub.status = SubStatus.EXPIRED
                    non_renew_ids.append(sub.id)
                    affected_users.add(sub.user_id)                  # H2
                    expired.append(sub)

            if non_renew_ids:
                conn.execute(
                    "UPDATE user_subscriptions SET status='expired', updated_at=NOW() "
                    "WHERE id = ANY(%s)",
                    (non_renew_ids,)
                )
            conn.commit()

        # H2 修复：到期后同步主库权限（app_authorizations tier 降级为 free，不再滞留 premium）
        for uid in affected_users:
            try:
                self._sync_authorizations(uid)
            except Exception as e:
                print(f'[Subscription] _sync_authorizations failed for user={uid}: {e}')

        return expired

    # ── 自动续费引擎（U3：代扣 + dunning + 宽限期） ──────────────────

    def renew_subscription(self, user_id: int, item_key: str) -> Tuple[bool, str]:
        """自动续费（代扣）：对已签约订阅执行周期扣款

        成功 → 延长周期 + 写 payment_event(success)/audit
        失败 → retry_count+1 + 标记 past_due + 写 payment_event(fail)/audit
        """
        sub = self.get_user_subscription(user_id, item_key)
        if not sub:
            return False, self._t('Subscription not found')
        if sub.status != SubStatus.ACTIVE:
            return False, self._t('Subscription is not active')
        if not sub.auto_renew:
            return False, self._t('Auto-renew is disabled')

        agreement_id = (sub.agreement_id or '').strip()
        payment_method = (sub.payment_method or '').strip()
        if not agreement_id or not payment_method:
            # 未签约 → 无法代扣：标记 past_due（宽限期后锁定），由 dunning 处理
            with self._get_conn() as conn:
                conn.execute("""
                    UPDATE user_subscriptions SET
                        status='past_due',
                        retry_count=retry_count + 1,
                        last_charge_at=NOW(),
                        grace_end=COALESCE(grace_end, (period_end::timestamp + (%s * INTERVAL '1 day'))::text),
                        updated_at=NOW()
                    WHERE id=%s
                """, (GRACE_DAYS, sub.id))
                conn.commit()
            self._record_payment_event(user_id, sub.id, '', 'charge_fail', payment_method or 'unknown',
                                       0, 'No deduction agreement configured')
            self._record_audit(user_id, 'renewal_failed',
                               f'No deduction agreement configured for {item_key}', sub_id=sub.id)
            return False, 'No deduction agreement configured'

        item = self.get_item(item_key)
        if not item:
            return False, self._t('Subscription item not found')

        interval = sub.interval_type or 'month'
        amount_fen = item.price_month if interval == 'month' else item.price_year
        if amount_fen <= 0:
            return False, self._t('Invalid price')

        order_no = f'SUB{int(time.time())}{secrets.token_hex(4).upper()}'

        # 创建续费订单
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO sub_orders
                    (order_no, user_id, item_key, interval_type, amount_fen, channel, status, extra)
                VALUES (%s,%s,%s,%s,%s,%s,'pending','{}')
            """, (order_no, user_id, item_key, interval, amount_fen, payment_method))
            conn.commit()

        # 执行代扣（fail-closed：未配置网关 → 拒绝，严禁 mock 放行）
        from plugins.payment.gateways import process_charge
        success, fail_reason = process_charge(
            channel=payment_method,
            agreement_id=agreement_id,
            order_no=order_no,
            amount_fen=amount_fen,
            subject=item.name_zh,
        )

        if success:
            with self._get_conn() as conn:
                now = datetime.now()
                cur_end = None
                if sub.period_end:
                    try:
                        cur_end = datetime.fromisoformat(sub.period_end)
                    except (TypeError, ValueError):
                        cur_end = None
                new_start = max(now, cur_end) if cur_end else now
                new_end = self._calc_period_end(new_start, interval)
                conn.execute("""
                    UPDATE user_subscriptions SET
                        status='active', period_start=%s, period_end=%s,
                        retry_count=0, last_charge_at=NOW(), updated_at=NOW()
                    WHERE id=%s
                """, (new_start.isoformat(), new_end.isoformat(), sub.id))
                conn.execute("""
                    UPDATE sub_orders SET status='paid', paid_at=NOW(), updated_at=NOW()
                    WHERE order_no=%s
                """, (order_no,))
                conn.commit()
            self._record_payment_event(user_id, sub.id, order_no, 'charge_success', payment_method, amount_fen)
            self._record_audit(user_id, 'renewal_success',
                               f'Auto-renewed {item_key} via {payment_method}', sub_id=sub.id)
            return True, 'ok'

        # 扣款失败 → past_due + dunning 计数
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE sub_orders SET status='failed', fail_reason=%s, updated_at=NOW()
                WHERE order_no=%s
            """, (fail_reason or 'Charge failed', order_no))
            conn.execute("""
                UPDATE user_subscriptions SET
                    status='past_due',
                    retry_count=retry_count + 1,
                    last_charge_at=NOW(),
                    grace_end=COALESCE(grace_end, (period_end::timestamp + (%s * INTERVAL '1 day'))::text),
                    updated_at=NOW()
                WHERE id=%s
            """, (GRACE_DAYS, sub.id))
            conn.commit()
        self._record_payment_event(user_id, sub.id, order_no, 'charge_fail', payment_method, amount_fen,
                                   fail_reason or 'Charge failed')
        self._record_audit(user_id, 'renewal_failed',
                           f'Charge failed for {item_key}: {fail_reason}', sub_id=sub.id)
        return False, fail_reason or 'Charge failed'

    def run_dunning_scan(self) -> None:
        """每日 dunning 扫描：宽限期内重试扣款；超宽限期 → 过期锁定"""
        today = datetime.now().date()
        now_iso = datetime.now().isoformat()

        # 1) 宽限期内 past_due 且已签约 → 重试（同一天不重复扣款）
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM user_subscriptions
                WHERE status='past_due' AND auto_renew=1 AND agreement_id <> ''
                  AND (grace_end IS NULL OR grace_end::timestamp >= %s)
            """, (now_iso,)).fetchall()
        for row in rows:
            sub = UserSubscription.from_row(dict(row))
            if sub.last_charge_at:
                try:
                    if datetime.fromisoformat(sub.last_charge_at).date() == today:
                        continue
                except (TypeError, ValueError):
                    pass
            self.renew_subscription(sub.user_id, sub.item_key)

        # 2) 超宽限期 → 过期锁定
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM user_subscriptions
                WHERE status='past_due'
                  AND grace_end IS NOT NULL
                  AND grace_end::timestamp < %s
            """, (now_iso,)).fetchall()
            ids = [dict(r)['id'] for r in rows]
            if ids:
                conn.execute(
                    "UPDATE user_subscriptions SET status='expired', auto_renew=0, updated_at=NOW() "
                    "WHERE id = ANY(%s)",
                    (ids,)
                )
                conn.commit()
                for r in rows:
                    rd = dict(r)
                    self._record_audit(rd['user_id'], 'subscription_expired',
                                       'Grace period ended, subscription locked', sub_id=rd['id'])
                    # U4：宽限期结束锁定 → 同步权限（降级为 free）
                    self._sync_authorizations(rd['user_id'])

    def _record_payment_event(self, user_id: int, sub_id: int, order_no: str, event_type: str,
                              channel: str, amount_fen: int, fail_reason: str = '') -> None:
        """写计费事件 sub_payment_events（兼容 A 的 payment_events）"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO sub_payment_events
                    (user_id, sub_id, order_no, event_type, channel, amount_fen, result, fail_reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (user_id, sub_id, order_no, event_type, channel, amount_fen,
                  'fail' if fail_reason else 'success', fail_reason))
            conn.commit()

    def _record_audit(self, user_id: int, action: str, detail: str,
                      sub_id: int = None, admin_id: int = None) -> None:
        """写审计 sub_audit_log（兼容 A 的 subscription_audit_log）"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO sub_audit_log (user_id, sub_id, action, detail, admin_id)
                VALUES (%s,%s,%s,%s,%s)
            """, (user_id, sub_id, action, detail, admin_id))
            conn.commit()

    def _sync_authorizations(self, user_id: int) -> None:
        """同步订阅权限到主库 app_authorizations(main)

        取用户所有 active 订阅中的最高 tier 与最晚 period_end；
        无 active 订阅 → tier='free'。
        主库表在 public schema，需用独立连接（_get_conn 会 SET search_path）。
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT u.item_key, u.period_end, COALESCE(i.tier, 'basic') AS tier "
                "FROM user_subscriptions u "
                "LEFT JOIN sub_items i ON i.item_key = u.item_key "
                "WHERE u.user_id=%s AND u.status='active'",
                (user_id,)
            ).fetchall()

        tier = 'free'
        expire_at = ''
        for r in rows:
            rd = dict(r)
            if TIER_RANK.get(rd['tier'], 0) > TIER_RANK.get(tier, 0):
                tier = rd['tier']
            if rd.get('period_end') and rd['period_end'] > expire_at:
                expire_at = rd['period_end']

        # 主库权限表（public schema）— 独立连接，失败不阻断订阅主流程
        try:
            conn = PgConnection(get_raw_connection())
            expire_ts = expire_at.replace('T', ' ') if expire_at else None
            existing = conn.execute(
                "SELECT id FROM app_authorizations WHERE user_id=%s AND app_name='main'",
                (user_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE app_authorizations SET tier=%s, tier_expire_at=%s, updated_at=NOW() "
                    "WHERE user_id=%s AND app_name='main'",
                    (tier, expire_ts, user_id))
            else:
                conn.execute(
                    "INSERT INTO app_authorizations (user_id, app_name, tier, tier_expire_at) "
                    "VALUES (%s,'main',%s,%s)",
                    (user_id, tier, expire_ts))
            conn.commit()
        except Exception as e:
            print(f'[Subscription] _sync_authorizations failed for user={user_id}: {e}')

    # ── 订单查询 ──────────────────────────────────────────────────

    def get_order(self, order_no: str) -> Optional[SubOrder]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM sub_orders WHERE order_no=%s", (order_no,)
            ).fetchone()
            if row:
                return SubOrder.from_row(dict(row))
        return None

    def list_orders(self, user_id: int, limit: int = 50) -> List[SubOrder]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sub_orders WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit)
            ).fetchall()
            return [SubOrder.from_row(dict(r)) for r in rows]

    def list_all_orders(self, limit: int = 100, offset: int = 0) -> List[SubOrder]:
        """管理员：全部订单"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sub_orders ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (limit, offset)
            ).fetchall()
            return [SubOrder.from_row(dict(r)) for r in rows]

    # ── 退款 ──────────────────────────────────────────────────────

    def refund_order(self, order_no: str) -> Tuple[bool, str]:
        """管理员：退款订单
        1. 查订单确认状态为 paid
        2. 调用支付网关退款
        3. 更新订单状态为 refunded
        4. 取消对应用户订阅
        """
        order = self.get_order(order_no)
        if not order:
            return False, self._t('Order not found')
        if order.status != OrderStatus.PAID:
            return False, self._t('Order cannot be refunded (current status: {status})').format(status=order.status.value)

        # 调用支付网关退款
        from plugins.payment.gateways import process_refund
        refund_result = process_refund(
            order_no=order.order_no,
            amount_fen=order.amount_fen,
            channel=order.channel,
            trade_no=order.trade_no,
        )

        if not refund_result.get('success'):
            error_msg = refund_result.get('error', 'Unknown error')
            print(f'[Subscription Refund] Gateway refund failed for {order_no}: {error_msg}')
            return False, self._t('Refund failed: {error}').format(error=error_msg)

        # 更新订单
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE sub_orders SET
                    status='refunded', updated_at=NOW()
                WHERE order_no=%s
            """, (order_no,))

            # 取消用户订阅
            conn.execute("""
                UPDATE user_subscriptions SET
                    status='canceled', auto_renew=0, updated_at=NOW()
                WHERE user_id=%s AND item_key=%s
            """, (order.user_id, order.item_key))

            conn.commit()

        # U4：退款取消订阅后同步权限（可能降级为 free）
        self._sync_authorizations(order.user_id)

        return True, 'ok'

    # ── 内部工具 ──────────────────────────────────────────────────

    def _calc_period_end(self, start: datetime, interval: str) -> datetime:
        if interval == 'month':
            month = start.month + 1
            year = start.year
            if month > 12:
                month -= 12
                year += 1
            try:
                return start.replace(year=year, month=month)
            except ValueError:
                import calendar
                last_day = calendar.monthrange(year, month)[1]
                return start.replace(year=year, month=month, day=last_day)
        elif interval == 'year':
            try:
                return start.replace(year=start.year + 1)
            except ValueError:
                return start.replace(year=start.year + 1, month=2, day=28)
        return start + timedelta(days=30)


# ── 模块级单例 ──────────────────────────────────────────────────────────

_SERVICE = None


def get_subscription_service() -> SubscriptionService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = SubscriptionService()
    return _SERVICE


# ── 快捷函数（供外部模块调用） ──────────────────────────────────────────

def has_subscription(user_id: int, item_key: str) -> bool:
    """快捷检查：用户是否有某项订阅"""
    return get_subscription_service().has_subscription(user_id, item_key)


def get_active_features(user_id: int) -> List[str]:
    """快捷获取：用户活跃功能列表"""
    return get_subscription_service().get_active_features(user_id)


# ── 装饰器（供路由层使用） ──────────────────────────────────────────────

def require_subscription(item_key: str):
    """装饰器：要求用户订阅了指定项才能访问

    用法:
        @require_subscription('miniapp_wechat')
        def generate_miniapp():
            ...
    """
    import functools
    from flask import request, jsonify

    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            user_id = getattr(request, 'user_id', None)
            if not user_id:
                return jsonify({'error': 'Authentication required', 'code': 'AUTH_REQUIRED'}), 401
            if not has_subscription(user_id, item_key):
                return jsonify({'error': f'Subscription required: {item_key}', 'code': 'NO_SUBSCRIPTION'}), 402
            return f(*args, **kwargs)
        return wrapper
    return decorator
