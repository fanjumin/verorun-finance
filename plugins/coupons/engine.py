#!/usr/bin/env python3
"""
CouponEngine — 核心引擎
=======================
处理优惠券的匹配、验证、计算、叠加和领取。
"""

from i18n import _
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from plugins.coupons.scene import SceneName


class CouponEngine:
    """优惠券引擎，使用独立 PG schema coupons 存储券表，主库只读查询。"""

    def __init__(self, get_db, get_main_db, t_func=None):
        self._get_db = get_db
        self._get_main_db = get_main_db
        # BasePlugin.t does not accept **kwargs, but engine calls like
        # self._t(_('¥{val} Coupon'), val=val) rely on template formatting.
        # Wrap t_func: with kwargs -> i18n._t (translate+format); without
        # kwargs -> original t_func (plugin translation unchanged).
        def _t(text, **kwargs):
            if kwargs:
                from i18n import _t as _i18n_t
                return _i18n_t(text, **kwargs)
            return (t_func or (lambda s: s))(text)
        self._t = _t

    # ── 查询 ──

    def get_by_code(self, code: str) -> Optional[dict]:
        """按码查询优惠券"""
        with self._get_db() as conn:
            row = conn.execute(
                'SELECT * FROM coupons WHERE code=? AND is_active=1', (code.upper(),)
            ).fetchone()
            return dict(row) if row else None

    def get_by_id(self, cid: int) -> Optional[dict]:
        with self._get_db() as conn:
            row = conn.execute('SELECT * FROM coupons WHERE id=?', (cid,)).fetchone()
            return dict(row) if row else None

    def list_all(self) -> List[dict]:
        with self._get_db() as conn:
            return [dict(r) for r in conn.execute(
                'SELECT * FROM coupons ORDER BY id DESC').fetchall()]

    # ── CRUD ──

    def create(self, data: dict) -> int:
        cpn_type = data.get('coupon_type', 'fixed')
        with self._get_db() as conn:
            existing = conn.execute(
                'SELECT id FROM coupons WHERE code=?', (data['code'].upper(),)
            ).fetchone()
            if existing:
                raise ValueError(self._t(_('Coupon code already exists')))
            cur = conn.execute(
                '''INSERT INTO coupons (code, name, coupon_type, value, min_amount, min_quantity,
                   usage_limit, per_user_limit, expire_at, is_active, description, coupon_category,
                   applicable_products, scene, first_month_only, stackable, active_from, active_to,
                   created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,NOW()) RETURNING id''',
                (
                    data['code'].upper(),
                    data.get('name', ''),
                    cpn_type,
                    float(data['value']),
                    float(data.get('min_amount', 0)),
                    int(data.get('min_quantity', 0)),
                    int(data.get('usage_limit', 0)),
                    int(data.get('per_user_limit', 1)),
                    data.get('expire_at') or None,
                    data.get('description', ''),
                    data.get('coupon_category', 'general'),
                    data.get('applicable_products', ''),
                    data.get('scene', ''),
                    int(data.get('first_month_only', 0)),
                    int(data.get('stackable', 0)),
                    data.get('active_from') or None,
                    data.get('active_to') or None,
                )
            )
            cid = cur.fetchone()['id']
            conn.commit()
            return cid

    def update(self, cid: int, data: dict) -> bool:
        fields = ['name', 'coupon_type', 'value', 'min_amount', 'min_quantity',
                  'usage_limit', 'per_user_limit', 'expire_at', 'is_active',
                  'description', 'coupon_category', 'applicable_products', 'scene',
                  'first_month_only', 'stackable', 'active_from', 'active_to']
        sets = []
        vals = []
        for f in fields:
            if f in data:
                sets.append(f'{f}=?')
                vals.append(data[f])
        if not sets:
            return False
        vals.append(cid)
        with self._get_db() as conn:
            conn.execute(f'UPDATE coupons SET {",".join(sets)} WHERE id=?', vals)
            conn.commit()
        return True

    def delete(self, cid: int) -> bool:
        with self._get_db() as conn:
            conn.execute('DELETE FROM coupons WHERE id=?', (cid,))
            conn.execute('DELETE FROM coupon_redemptions WHERE coupon_id=?', (cid,))
            conn.commit()
        return True

    # ── 可用券查询 ──

    def get_available_coupons(self, user_id: int, cart_amount: float,
                              scene: str = None) -> List[dict]:
        """获取用户可用优惠券列表。"""
        now = datetime.now().isoformat()
        with self._get_db() as conn:
            rows = conn.execute(
                '''SELECT c.*,
                   CASE WHEN cr.id IS NOT NULL THEN 1 ELSE 0 END as is_redeemed
                   FROM coupons c
                   LEFT JOIN coupon_redemptions cr ON cr.coupon_id = c.id AND cr.user_id = ?
                   WHERE c.is_active = 1
                     AND (c.expire_at IS NULL OR c.expire_at > ?)
                     AND (c.usage_limit = 0 OR c.used_count < c.usage_limit)
                     AND (c.active_from IS NULL OR c.active_from = '' OR c.active_from <= ?)
                     AND (c.active_to IS NULL OR c.active_to = '' OR c.active_to >= ?)
                   ORDER BY c.id DESC''',
                (user_id, now, now, now)
            ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            # 场景过滤
            if scene and d.get('scene') and d['scene'] != scene:
                continue
            # 每人限用
            per_limit = d.get('per_user_limit', 1) or 1
            user_used = 0
            if d['is_redeemed']:
                with self._get_db() as conn:
                    row = conn.execute(
                        'SELECT COUNT(*) as c FROM coupon_redemptions WHERE coupon_id=? AND user_id=?',
                        (d['id'], user_id)
                    ).fetchone()
                    user_used = row['c'] if row else 0
            if user_used >= per_limit:
                continue
            # 新人专享检查（读主库 order_items）
            if d.get('coupon_category') == 'new_user':
                with self._get_main_db() as conn:
                    has = conn.execute(
                        'SELECT id FROM order_items WHERE user_id=%s LIMIT 1', (user_id,)
                    ).fetchone()
                if has:
                    continue
            if cart_amount < d['min_amount']:
                continue
            results.append(d)

        return results

    # ── 验证 ──

    def validate(self, code: str, amount: float,
                 user_id: int = None, quantity: int = 0,
                 product_id: int = None, scene: str = None,
                 plan: str = None) -> dict:
        """验证优惠券并返回折扣金额。

        Args:
            code: 优惠码
            amount: 订单金额（元）
            user_id: 用户 ID
            quantity: 商品数量
            product_id: 商品 ID
            scene: 场景标识
            plan: 订阅套餐标识

        Returns:
            {'valid': True, 'discount': float, 'coupon': dict}
            或 {'valid': False, 'error': str}
        """
        cpn = self.get_by_code(code)
        if not cpn:
            return {'valid': False, 'error': self._t(_('Coupon is invalid'))}

        if cpn['usage_limit'] and cpn['used_count'] >= cpn['usage_limit']:
            return {'valid': False, 'error': self._t(_('Coupon has been used up'))}

        now = datetime.now().isoformat()
        if cpn['expire_at'] and cpn['expire_at'] < now:
            return {'valid': False, 'error': self._t(_('Coupon has expired'))}
        if cpn.get('active_from') and cpn['active_from'] > now:
            return {'valid': False, 'error': self._t(_('Coupon has not taken effect'))}
        if cpn.get('active_to') and cpn['active_to'] < now:
            return {'valid': False, 'error': self._t(_('Coupon has expired'))}

        if amount < cpn['min_amount']:
            return {'valid': False, 'error': self._t(_('Minimum consumption not met')) + f' ¥{cpn["min_amount"]}'}

        if cpn['min_quantity'] and quantity < cpn['min_quantity']:
            return {'valid': False, 'error': self._t(_('At least {n} items need to be purchased'), n=cpn['min_quantity'])}

        # 场景检查
        if scene and cpn.get('scene') and cpn['scene'] != scene:
            return {'valid': False, 'error': self._t(_('This coupon is not applicable to the current scenario'))}

        # 适用套餐检查（订阅独有）
        if plan and cpn.get('applicable_plans'):
            allowed = str(cpn['applicable_plans']).split(',')
            if plan not in allowed:
                return {'valid': False, 'error': self._t(_('This coupon is not applicable to the current package'))}

        # 新人专享（读主库）
        if cpn.get('coupon_category') == 'new_user' and user_id:
            with self._get_main_db() as conn:
                has = conn.execute(
                    'SELECT id FROM order_items WHERE user_id=%s LIMIT 1', (user_id,)
                ).fetchone()
            if has:
                return {'valid': False, 'error': self._t(_('Available only to new users'))}

        # 适用商品
        if cpn.get('applicable_products') and product_id:
            allowed = str(cpn['applicable_products']).split(',')
            if str(product_id) not in allowed:
                return {'valid': False, 'error': self._t(_('This item is not applicable for this coupon'))}

        # 每人限用
        if user_id:
            per_limit = cpn.get('per_user_limit', 1) or 1
            with self._get_db() as conn:
                uc = conn.execute(
                    'SELECT COUNT(*) as c FROM coupon_redemptions WHERE coupon_id=? AND user_id=?',
                    (cpn['id'], user_id)
                ).fetchone()['c']
            if uc >= per_limit:
                return {'valid': False, 'error': self._t(_('You have already used this coupon'))}

        discount = self._calc_discount(cpn, amount)
        return {'valid': True, 'discount': round(discount, 2), 'coupon': cpn}

    def calculate_saving(self, coupon: dict, amount: float) -> float:
        """计算单张券的节省金额。"""
        return self._calc_discount(coupon, amount)

    def _calc_discount(self, cpn: dict, amount: float) -> float:
        """根据券类型计算折扣。"""
        ctype = cpn.get('coupon_type', 'fixed')
        value = float(cpn['value'])
        if ctype == 'fixed':
            return min(value, amount)
        elif ctype == 'percent':
            return round(amount * value / 100, 2)
        elif ctype == 'free_shipping':
            return value if value > 0 else 10  # 免运费默认 10 元
        elif ctype == 'first_month_percent':
            return round(amount * value / 100, 2)
        return min(value, amount)

    def apply_to_order(self, code: str, user_id: int,
                       order_no: str, amount: float) -> dict:
        """将优惠券应用到订单（更新 used_count + 插入 coupon_redemptions）。

        安全校验：
          - 订单必须真实存在于主库 orders 且属于该用户（VR-CPN-003）
          - UPDATE 带 `used_count < usage_limit` 原子条件防并发超卖（VR-CPN-005）
        """
        cpn = self.get_by_code(code)
        if not cpn:
            return {'success': False, 'error': _('Coupon is invalid')}

        # 订单存在性校验（fail-closed：校验失败即拒绝核销）
        try:
            with self._get_main_db() as conn:
                order = conn.execute(
                    'SELECT id FROM orders WHERE order_id=%s AND user_id=%s',
                    (order_no, user_id)
                ).fetchone()
            if not order:
                return {'success': False, 'error': self._t(_('Order does not exist'))}
        except Exception:
            return {'success': False, 'error': self._t(_('Failed to verify order'))}

        result = self.validate(code, amount, user_id=user_id)
        if not result['valid']:
            return {'success': False, 'error': result['error']}

        discount = result['discount']
        with self._get_db() as conn:
            cur = conn.execute(
                "UPDATE coupons SET used_count=used_count+1 WHERE id=%s "
                "AND (usage_limit = 0 OR used_count < usage_limit) RETURNING id",
                (cpn['id'],)
            )
            if not cur.fetchone():
                conn.rollback()
                return {'success': False, 'error': self._t(_('Coupon has been used up'))}
            conn.execute(
                '''INSERT INTO coupon_redemptions (coupon_id, user_id, order_no,
                   discount_fen, created_at) VALUES (%s,%s,%s,%s,NOW())''',
                (cpn['id'], user_id, order_no, int(discount * 100))
            )
            conn.commit()

        return {'success': True, 'discount': discount, 'coupon_id': cpn['id']}

    # ── 分发 ──

    def distribute(self, coupon_id: int, user_ids: list) -> int:
        """批量发放优惠券给用户。"""
        count = 0
        with self._get_db() as conn:
            for uid in user_ids:
                existing = conn.execute(
                    'SELECT id FROM coupon_redemptions WHERE coupon_id=? AND user_id=?',
                    (coupon_id, uid)
                ).fetchone()
                if not existing:
                    conn.execute(
                        '''INSERT INTO coupon_redemptions (coupon_id, user_id, order_no,
                           discount_fen, created_at) VALUES (%s,%s,%s,%s,NOW())''',
                        (coupon_id, uid, f'distribute_{coupon_id}_{uid}', 0)
                    )
                    count += 1
            conn.commit()
        return count

    # ── 统计 ──

    def stats(self) -> dict:
        with self._get_db() as conn:
            total = conn.execute('SELECT COUNT(*) as c FROM coupons').fetchone()['c']
            active = conn.execute('SELECT COUNT(*) as c FROM coupons WHERE is_active=1').fetchone()['c']
            used = conn.execute('SELECT SUM(used_count) as c FROM coupons').fetchone()['c'] or 0
            disc_fen = conn.execute(
                'SELECT COALESCE(SUM(discount_fen),0) as c FROM coupon_redemptions'
            ).fetchone()['c']
            by_cat = conn.execute(
                "SELECT coupon_category, COUNT(*) as c FROM coupons GROUP BY coupon_category"
            ).fetchall()
            by_type = conn.execute(
                "SELECT coupon_type, COUNT(*) as c FROM coupons GROUP BY coupon_type"
            ).fetchall()
            top = conn.execute(
                "SELECT code, name, used_count FROM coupons ORDER BY used_count DESC LIMIT 10"
            ).fetchall()

        # 读主库 order_items 折扣统计
        with self._get_main_db() as conn:
            shop_disc = conn.execute(
                'SELECT COALESCE(SUM(discount),0) as c FROM order_items WHERE coupon_id IS NOT NULL'
            ).fetchone()['c']

        return {
            'total_coupons': total,
            'active_coupons': active,
            'total_used': used or 0,
            'total_discount': round(float(disc_fen) / 100 + float(shop_disc), 2),
            'by_category': [dict(r) for r in by_cat],
            'by_type': [dict(r) for r in by_type],
            'top_used': [dict(r) for r in top],
        }

    def get_redemptions(self, coupon_id: int, page: int = 1, limit: int = 50) -> dict:
        offset = (page - 1) * limit
        with self._get_db() as conn:
            total = conn.execute(
                'SELECT COUNT(*) as c FROM coupon_redemptions WHERE coupon_id=?',
                (coupon_id,)
            ).fetchone()['c']
            rows = conn.execute(
                '''SELECT r.* FROM coupon_redemptions r
                   WHERE r.coupon_id=? ORDER BY r.created_at DESC LIMIT ? OFFSET ?''',
                (coupon_id, limit, offset)
            ).fetchall()

        # 跨库查用户信息（一次 IN 批量，避免 N+1 点查）
        user_ids = [r['user_id'] for r in rows if r['user_id']]
        user_map = {}
        uid_set = set(user_ids)
        if uid_set:
            # 主库为 PostgreSQL（psycopg2），占位符必须用 %s（不能用 ?）
            ph = ','.join(['%s'] * len(uid_set))
            with self._get_main_db() as conn:
                for u in conn.execute(
                    f'SELECT id, COALESCE(display_name, username) AS nickname, phone FROM users WHERE id IN ({ph})',
                    tuple(uid_set)
                ):
                    user_map[u['id']] = dict(u)

        redemptions = []
        for r in rows:
            d = dict(r)
            u = user_map.get(d['user_id'], {})
            d['nickname'] = u.get('nickname', '')
            d['phone'] = u.get('phone', '')
            redemptions.append(d)

        return {
            'total': total,
            'page': page,
            'redemptions': redemptions,
        }

    def get_user_coupons(self, user_id: int) -> List[dict]:
        """获取用户的可用优惠券列表。"""
        now = datetime.now().isoformat()
        try:
            with self._get_db() as conn:
                rows = conn.execute(
                    '''SELECT c.id, c.code,
                              COALESCE(c.name, '') as name,
                              COALESCE(c.coupon_type, 'fixed') as coupon_type,
                              c.value,
                              COALESCE(c.description, '') as description,
                              c.min_amount,
                              c.expire_at,
                              c.coupon_category,
                              c.scene,
                              CASE WHEN cr.id IS NOT NULL THEN 1 ELSE 0 END as is_redeemed,
                              cr.discount_fen,
                              cr.created_at as redeemed_at
                       FROM coupons c
                       LEFT JOIN coupon_redemptions cr ON cr.coupon_id = c.id AND cr.user_id = ?
                       WHERE c.is_active = 1
                         AND (c.expire_at IS NULL OR c.expire_at > ?)
                         AND (c.usage_limit = 0 OR c.used_count < c.usage_limit)
                       ORDER BY c.id DESC''',
                    (user_id, now)
                ).fetchall()
        except Exception:
            return []

        results = []
        for r in rows:
            d = dict(r)
            ctype = d['coupon_type']
            val = d['value']
            if ctype == 'fixed':
                desc = self._t(_('¥{val} Coupon'), val=val)
            elif ctype == 'percent':
                desc = self._t(_('{pct}% Discount'), pct=int(val))
            elif ctype == 'free_shipping':
                desc = self._t(_('Free Shipping'))
            else:
                desc = self._t(_('Discount {val}'), val=val)
            if d['min_amount'] and d['min_amount'] > 0:
                desc += self._t(_('(Available when ¥{amt} or more)'), amt=d['min_amount'])
            d['description'] = d.get('description') or desc
            results.append(d)
        return results
