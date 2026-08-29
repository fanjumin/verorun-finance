#!/usr/bin/env python3
"""
AI 智能推荐引擎
===============
在结算时自动分析用户和购物车，推荐最优优惠券组合。
"""

import json
import logging

from plugins.coupons.engine import CouponEngine

logger = logging.getLogger(__name__)


class AICouponRecommender:
    """AI 优惠券推荐器。"""

    def __init__(self, engine: CouponEngine):
        self._engine = engine

    def recommend(self, user_id: int, cart_amount: float,
                  scene: str = None, locale: str = 'zh-CN') -> dict:
        """为用户推荐最优优惠券。

        Args:
            user_id: 用户 ID
            cart_amount: 购物车金额
            scene: 场景标识（如 'shop_general', '1688_purchase'）
            locale: 语言

        Returns:
            {
                'recommended': [coupon_dict, ...],  # 推荐的券列表
                'best': coupon_dict,                 # 最优单张
                'estimated_saving': float,            # 预计节省
                'applied_automatically': bool         # 是否自动应用
            }
        """
        try:
            available = self._engine.get_available_coupons(
                user_id, cart_amount, scene=scene
            )
        except Exception as e:
            logger.error(f'[CouponAI] get_available failed: {e}')
            available = []

        if not available:
            return {
                'recommended': [],
                'best': None,
                'estimated_saving': 0,
                'applied_automatically': False,
            }

        # 计算每张券的节省金额，排序
        scored = []
        for cpn in available:
            saving = self._engine.calculate_saving(cpn, cart_amount)
            if saving > 0:
                scored.append((saving, cpn))

        scored.sort(key=lambda x: x[0], reverse=True)

        recommended = [c[1] for c in scored[:3]]
        best = scored[0][1] if scored else None
        best_saving = scored[0][0] if scored else 0

        # 最优券是否自动应用（节省金额 > 10 元且不是首单优先）
        auto_apply = best_saving >= 10 and (best is None or best.get('scene', '') != 'first_order')

        return {
            'recommended': recommended,
            'best': best,
            'estimated_saving': round(best_saving, 2),
            'applied_automatically': auto_apply,
        }
