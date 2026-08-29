#!/usr/bin/env python3
"""
Pricing Service — Phase 4

Standalone pricing calculation engine for:
- Plugin product pricing
- Proration (upgrade/downgrade mid-cycle)
- Coupon application
- Renewal pricing

All methods are pure calculation — no DB writes, no side effects.

Usage:
    from services.pricing_service import PricingService
    svc = PricingService()
    price = svc.calculate_proration(old_price_fen=8800, new_price_fen=18800,
                                     days_remaining=15, total_days=30)
"""

# ⚠️ DEPRECATED (legacy) — 本服务无生产调用者。
# 主站订阅链路当前由 auth-center/routes/subscription/__init__.py 承载。
# 迁移/重构前请勿基于本文件实现新逻辑。上线任务 T12 要求：仅标注，不迁移。


import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

from models import get_db, now_iso


# ── Constants ──────────────────────────────────────────────────────────────

PERIOD_DAYS = {'month': 30, 'quarter': 90, 'semi_annual': 182, 'year': 365}
VALID_PERIODS = tuple(PERIOD_DAYS.keys())
COUPON_TYPES = ('fixed', 'percent', 'first_month_percent')


# ── PricingService ─────────────────────────────────────────────────────────


class PricingService:
    """Pure pricing calculations — no side effects."""

    # ── Plugin Product Pricing ─────────────────────────────────────────

    def get_plugin_products(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """List all plugin products with pricing."""
        with get_db() as conn:
            if active_only:
                rows = conn.execute(
                    'SELECT * FROM plugin_products WHERE is_active=1 ORDER BY sort_order'
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM plugin_products ORDER BY sort_order'
                ).fetchall()

        result = []
        for r in rows:
            p = dict(r)
            p['price_month_yuan'] = f'¥{p.get("price_month_fen", 0)/100:.2f}'
            p['price_year_yuan'] = f'¥{p.get("price_year_fen", 0)/100:.2f}'
            result.append(p)
        return result

    def get_plugin_price(self, plugin_key: str, period: str = 'month') -> int:
        """Get price in fen for a plugin product."""
        with get_db() as conn:
            row = conn.execute(
                'SELECT price_month_fen, price_year_fen FROM plugin_products '
                'WHERE plugin_key=%s AND is_active=1',
                (plugin_key,),
            ).fetchone()
        if not row:
            return 0
        return row['price_year_fen'] if period == 'year' else row['price_month_fen']

    # ── Proration ──────────────────────────────────────────────────────

    def calculate_proration(
        self,
        old_price_fen: int,
        new_price_fen: int,
        days_remaining: int,
        total_days: int = 30,
    ) -> Dict[str, Any]:
        """Calculate prorated credit and amount due for mid-cycle plan change.

        Args:
            old_price_fen: Price of current plan (fen)
            new_price_fen: Price of target plan (fen)
            days_remaining: Days remaining in current period
            total_days: Total days in current period (default 30)

        Returns:
            {prorated_credit_fen, amount_due_fen, is_upgrade, summary}
        """
        if total_days <= 0:
            total_days = 1

        prorated_credit = int(old_price_fen * days_remaining / total_days)
        amount_due = max(0, new_price_fen - prorated_credit)
        is_upgrade = new_price_fen > old_price_fen

        return {
            'prorated_credit_fen': prorated_credit,
            'prorated_credit_yuan': f'¥{prorated_credit/100:.2f}',
            'amount_due_fen': amount_due,
            'amount_due_yuan': f'¥{amount_due/100:.2f}',
            'is_upgrade': is_upgrade,
            'is_downgrade': not is_upgrade and new_price_fen < old_price_fen,
            'days_remaining': days_remaining,
            'total_days': total_days,
            'summary': (
                f'Remaining value: ¥{prorated_credit/100:.2f} '
                f'({days_remaining}/{total_days} days). '
                f'Amount due: ¥{amount_due/100:.2f}.'
            ),
        }

    def calculate_upgrade(
        self,
        old_price_fen: int,
        new_price_fen: int,
        days_remaining: int,
        total_days: int = 30,
    ) -> Dict[str, Any]:
        """Calculate upgrade cost. Same as proration but validates is_upgrade."""
        result = self.calculate_proration(old_price_fen, new_price_fen, days_remaining, total_days)
        if not result['is_upgrade']:
            result['error'] = 'Not an upgrade: new price must be higher than old price'
        return result

    def calculate_downgrade(
        self,
        old_price_fen: int,
        new_price_fen: int,
        days_remaining: int,
        total_days: int = 30,
    ) -> Dict[str, Any]:
        """Calculate downgrade credit. Downgrade takes effect next period."""
        result = self.calculate_proration(old_price_fen, new_price_fen, days_remaining, total_days)
        if not result['is_downgrade']:
            result['error'] = 'Not a downgrade: new price must be lower than old price'
        # Downgrades: credit applied at next billing, no immediate charge
        result['amount_due_fen'] = 0
        result['amount_due_yuan'] = '¥0.00'
        result['credit_applied_next_period'] = result['prorated_credit_fen']
        return result

    def calculate_renewal(
        self,
        price_fen: int,
        period: str = 'month',
        discount_percent: int = 0,
    ) -> Dict[str, Any]:
        """Calculate renewal price with optional loyalty discount.

        Args:
            price_fen: Base price in fen
            period: Billing period
            discount_percent: Loyalty discount percentage (0-100)

        Returns:
            {base_price_fen, discount_fen, final_price_fen, period_days, summary}
        """
        period_days = PERIOD_DAYS.get(period, 30)
        discount_fen = int(price_fen * discount_percent / 100)
        final_price = price_fen - discount_fen

        return {
            'base_price_fen': price_fen,
            'base_price_yuan': f'¥{price_fen/100:.2f}',
            'discount_percent': discount_percent,
            'discount_fen': discount_fen,
            'discount_yuan': f'¥{discount_fen/100:.2f}',
            'final_price_fen': final_price,
            'final_price_yuan': f'¥{final_price/100:.2f}',
            'period_days': period_days,
            'period': period,
            'summary': (
                f'{period} renewal: ¥{price_fen/100:.2f}'
                + (f' - {discount_percent}% = ¥{final_price/100:.2f}' if discount_percent > 0 else '')
            ),
        }

    # ── Coupon ─────────────────────────────────────────────────────────

    def apply_coupon(
        self,
        coupon_code: str,
        amount_fen: int,
        user_id: int,
        plan_key: str = '',
    ) -> Dict[str, Any]:
        """Apply a coupon to an order amount.

        Returns:
            {valid, discount_fen, final_amount_fen, coupon_info, error}
        """
        with get_db() as conn:
            coupon = conn.execute(
                '''SELECT * FROM coupons
                   WHERE code=%s AND is_active=1
                   AND (expires_at IS NULL OR expires_at > NOW())''',
                (coupon_code,),
            ).fetchone()

        if not coupon:
            return {'valid': False, 'discount_fen': 0, 'final_amount_fen': amount_fen,
                    'error': 'Invalid or expired coupon'}

        coupon = dict(coupon)

        # Check time window
        now = now_iso()
        active_from = coupon.get('active_from', '')
        active_to = coupon.get('active_to', '')
        if active_from and now < active_from:
            return {'valid': False, 'discount_fen': 0, 'final_amount_fen': amount_fen,
                    'error': 'Coupon not yet active'}
        if active_to and now > active_to:
            return {'valid': False, 'discount_fen': 0, 'final_amount_fen': amount_fen,
                    'error': 'Coupon expired'}

        # Check usage limits
        if coupon['max_uses'] > 0 and coupon['used_count'] >= coupon['max_uses']:
            return {'valid': False, 'discount_fen': 0, 'final_amount_fen': amount_fen,
                    'error': 'Coupon usage limit reached'}

        # Check per-user limit
        with get_db() as conn:
            user_uses = conn.execute(
                'SELECT COUNT(*) as c FROM coupon_redemptions WHERE coupon_id=%s AND user_id=%s',
                (coupon['id'], user_id),
            ).fetchone()
        if user_uses['c'] >= coupon['max_per_user']:
            return {'valid': False, 'discount_fen': 0, 'final_amount_fen': amount_fen,
                    'error': 'Per-user coupon limit reached'}

        # Check applicable plans
        if coupon.get('applicable_plans') and plan_key:
            allowed = coupon['applicable_plans'].split(',')
            if plan_key not in allowed:
                return {'valid': False, 'discount_fen': 0, 'final_amount_fen': amount_fen,
                        'error': 'Coupon not applicable to this plan'}

        # Check minimum amount
        if amount_fen < coupon.get('min_amount_fen', 0):
            return {'valid': False, 'discount_fen': 0, 'final_amount_fen': amount_fen,
                    'error': f'Minimum order amount: ¥{coupon.get("min_amount_fen", 0)/100:.2f}'}

        # Calculate discount
        coupon_type = coupon.get('coupon_type', coupon.get('type', 'fixed'))
        if coupon_type == 'fixed':
            discount_fen = min(coupon['value'], amount_fen)
        elif coupon_type == 'percent':
            discount_fen = int(amount_fen * coupon['value'] / 100)
        elif coupon_type == 'first_month_percent':
            discount_fen = int(amount_fen * coupon['value'] / 100)
        else:
            return {'valid': False, 'discount_fen': 0, 'final_amount_fen': amount_fen,
                    'error': f'Unknown coupon type: {coupon_type}'}

        final_amount = max(0, amount_fen - discount_fen)

        return {
            'valid': True,
            'discount_fen': discount_fen,
            'discount_yuan': f'¥{discount_fen/100:.2f}',
            'final_amount_fen': final_amount,
            'final_amount_yuan': f'¥{final_amount/100:.2f}',
            'coupon_info': {
                'id': coupon['id'],
                'code': coupon_code,
                'type': coupon_type,
                'value': coupon['value'],
                'name': coupon.get('name', coupon_code),
            },
            'error': None,
        }

    # ── Bundle Discount ────────────────────────────────────────────────

    def calculate_bundle_discount(
        self,
        plugin_keys: List[str],
        period: str = 'month',
    ) -> Dict[str, Any]:
        """Calculate multi-plugin discount.

        Discount tiers:
          2 plugins → 5% off
          3 plugins → 10% off
          4+ plugins → 15% off
        """
        if not plugin_keys:
            return {'total_fen': 0, 'discount_fen': 0, 'discount_percent': 0,
                    'items': [], 'summary': 'No plugins selected'}

        items = []
        total = 0
        for key in plugin_keys:
            price = self.get_plugin_price(key, period)
            items.append({'plugin_key': key, 'price_fen': price, 'price_yuan': f'¥{price/100:.2f}'})
            total += price

        count = len(plugin_keys)
        if count >= 4:
            discount_pct = 15
        elif count >= 3:
            discount_pct = 10
        elif count >= 2:
            discount_pct = 5
        else:
            discount_pct = 0

        discount_fen = int(total * discount_pct / 100)
        final = total - discount_fen

        return {
            'total_fen': total,
            'total_yuan': f'¥{total/100:.2f}',
            'discount_percent': discount_pct,
            'discount_fen': discount_fen,
            'discount_yuan': f'¥{discount_fen/100:.2f}',
            'final_fen': final,
            'final_yuan': f'¥{final/100:.2f}',
            'plugin_count': count,
            'items': items,
            'summary': (
                f'{count} plugins: ¥{total/100:.2f}'
                + (f' → {discount_pct}% off = ¥{final/100:.2f}' if discount_pct > 0 else '')
            ),
        }

    # ── Period Helpers ─────────────────────────────────────────────────

    def get_period_days(self, period: str) -> int:
        """Get days in a billing period."""
        return PERIOD_DAYS.get(period, 30)

    def get_period_end(self, start_iso: str, period: str) -> str:
        """Calculate period end date from start."""
        days = self.get_period_days(period)
        try:
            start = datetime.fromisoformat(start_iso)
            return (start + timedelta(days=days)).isoformat()
        except (ValueError, TypeError):
            return (datetime.now() + timedelta(days=days)).isoformat()

    def format_price(self, fen: int, currency: str = None) -> str:
        """Format fen to yuan display string with currency symbol."""
        if not currency:
            currency = os.environ.get('DEPLOY_CURRENCY', 'CNY')
        symbols = {'CNY': '¥', 'USD': '$', 'EUR': '€', 'GBP': '£', 'JPY': '¥', 'SGD': 'S$', 'HKD': 'HK$'}
        sym = symbols.get(currency, '')
        if sym:
            return f'{sym}{fen/100:.2f}'
        return f'{fen/100:.2f} {currency}'