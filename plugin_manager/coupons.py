#!/usr/bin/env python3
"""
Plugin Manager — 优惠券引擎
===========================
优惠券/折扣码管理，支持百分比折扣和固定金额折扣。

数据存储: plugin_registry.db → coupon_codes 表
"""

import json
import secrets
import threading
from typing import Optional, Dict, Any, List
from datetime import datetime

from .models import get_registry_db


# ── DDL ──────────────────────────────────────────────────────────────

COUPON_DDL = """
CREATE TABLE IF NOT EXISTS coupon_codes (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    discount_type   TEXT NOT NULL CHECK(discount_type IN ('percentage', 'fixed')),
    discount_value  INTEGER NOT NULL,
    max_uses        BIGINT DEFAULT 0,
    used_count      BIGINT DEFAULT 0,
    min_amount_fen  BIGINT DEFAULT 0,
    applicable_plugins TEXT DEFAULT '[]',
    expires_at      TEXT,
    is_active       BIGINT DEFAULT 1,
    created_at      TEXT DEFAULT NOW(),
    updated_at      TEXT DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_coupon_code ON coupon_codes(code);
"""


def init_coupon_table():
    with get_registry_db() as conn:
        conn.executescript(COUPON_DDL)
        conn.commit()


# ── CouponManager ────────────────────────────────────────────────────

class CouponManager:
    """优惠券管理器"""

    def __init__(self):
        init_coupon_table()

    def create(self, code: str, discount_type: str, discount_value: int,
               max_uses: int = 0, min_amount_fen: int = 0,
               applicable_plugins: list = None,
               expires_at: str = None) -> dict:
        """创建优惠券"""
        existing = self._find(code)
        if existing:
            return {'success': False, 'error': f'Coupon "{code}" already exists'}

        try:
            with get_registry_db() as conn:
                conn.execute("""
                    INSERT INTO coupon_codes
                        (code, discount_type, discount_value, max_uses,
                         min_amount_fen, applicable_plugins, expires_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                """, (
                    code, discount_type, discount_value, max_uses,
                    min_amount_fen,
                    json.dumps(applicable_plugins or []),
                    expires_at,
                ))
                conn.commit()
            return {'success': True, 'code': code}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def validate(self, code: str, plugin_id: str = '',
                 amount_fen: int = 0) -> dict:
        """校验优惠券有效性

        Returns:
            {'valid': bool, 'discount_fen': int, 'final_fen': int,
             'discount_type': str, 'discount_value': int, 'error': str}
        """
        coupon = self._find(code)
        if not coupon:
            return {'valid': False, 'discount_fen': 0, 'final_fen': amount_fen,
                    'error': 'Coupon not found'}

        if not coupon['is_active']:
            return {'valid': False, 'discount_fen': 0, 'final_fen': amount_fen,
                    'error': 'Coupon is deactivated'}

        # 过期检查
        if coupon['expires_at']:
            try:
                expires = datetime.strptime(coupon['expires_at'], '%Y-%m-%d %H:%M:%S')
                if datetime.now() > expires:
                    return {'valid': False, 'discount_fen': 0, 'final_fen': amount_fen,
                            'error': 'Coupon expired'}
            except ValueError:
                pass

        # 使用次数检查
        if coupon['max_uses'] > 0 and coupon['used_count'] >= coupon['max_uses']:
            return {'valid': False, 'discount_fen': 0, 'final_fen': amount_fen,
                    'error': 'Coupon usage limit reached'}

        # 适用插件检查
        applicable = json.loads(coupon['applicable_plugins'] or '[]')
        if applicable and plugin_id and plugin_id not in applicable:
            return {'valid': False, 'discount_fen': 0, 'final_fen': amount_fen,
                    'error': 'Coupon not applicable for this plugin'}

        # 最低金额检查
        if amount_fen < coupon['min_amount_fen']:
            return {'valid': False, 'discount_fen': 0, 'final_fen': amount_fen,
                    'error': f'Minimum order amount: ¥{coupon["min_amount_fen"]/100:.2f}'}

        # 计算折扣
        discount_fen = 0
        if coupon['discount_type'] == 'percentage':
            discount_fen = int(amount_fen * coupon['discount_value'] / 100)
        elif coupon['discount_type'] == 'fixed':
            discount_fen = min(coupon['discount_value'], amount_fen)

        final_fen = max(amount_fen - discount_fen, 0)

        return {
            'valid': True,
            'discount_fen': discount_fen,
            'final_fen': final_fen,
            'discount_type': coupon['discount_type'],
            'discount_value': coupon['discount_value'],
        }

    def apply(self, code: str, order_no: str) -> bool:
        """核销优惠券（used_count++）"""
        try:
            with get_registry_db() as conn:
                conn.execute(
                    "UPDATE coupon_codes SET used_count = used_count + 1, "
                    "updated_at = NOW() WHERE code=%s",
                    (code,)
                )
                conn.commit()
            return True
        except Exception:
            return False

    def list_coupons(self) -> List[dict]:
        """列出所有优惠券"""
        with get_registry_db() as conn:
            rows = conn.execute(
                "SELECT * FROM coupon_codes ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def _find(self, code: str) -> Optional[dict]:
        with get_registry_db() as conn:
            row = conn.execute(
                "SELECT * FROM coupon_codes WHERE code=%s", (code,)
            ).fetchone()
            return dict(row) if row else None


# ── 模块级单例 ──────────────────────────────────────────────────────

_COUPON_MGR = None
_COUPON_LOCK = threading.Lock()


def get_coupon_manager() -> CouponManager:
    global _COUPON_MGR
    if _COUPON_MGR is None:
        with _COUPON_LOCK:
            if _COUPON_MGR is None:
                _COUPON_MGR = CouponManager()
    return _COUPON_MGR
