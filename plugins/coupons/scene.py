#!/usr/bin/env python3
"""
场景券（Scene Coupon）定义
=========================
券不再只绑定_("Product")，而是绑定_("Scenario")。
"""


from i18n import _
class SceneName:
    """系统内置场景常量。"""

    # ── 商城场景 ──
    SHOP_GENERAL = 'shop_general'           # 商城通用
    FIRST_ORDER = 'first_order'             # 首单
    REVIEW_REWARD = 'review_reward'         # 评价奖励

    # ── 采购场景 ──
    PURCHASE_1688 = 'purchase_1688'         # 1688 采购

    # ── AI 场景 ──
    AI_CONTENT = 'ai_content'               # AI 内容生成

    # ── 订阅场景 ──
    SUBSCRIPTION_RENEW = 'subscription_renew'   # 续费
    SUBSCRIPTION_UPGRADE = 'subscription_upgrade'  # 升级

    # ── 运营场景 ──
    NEW_USER = 'new_user'                   # 新人
    PROMOTION = 'promotion'                 # 推广
    REFERRAL = 'referral'                   # 推荐有礼


# 场景 → 可读描述（用于前端展示）
SCENE_LABELS = {
    SceneName.SHOP_GENERAL: _('Mall general'),
    SceneName.FIRST_ORDER: _('First order exclusive'),
    SceneName.REVIEW_REWARD: _('Review reward'),
    SceneName.PURCHASE_1688: _('1688 Procurement'),
    SceneName.AI_CONTENT: _('AI Content Generation'),
    SceneName.SUBSCRIPTION_RENEW: _('Renewal Exclusive'),
    SceneName.SUBSCRIPTION_UPGRADE: _('Upgrade discount'),
    SceneName.NEW_USER: _('New User Exclusive'),
    SceneName.PROMOTION: _('Promotion'),
    SceneName.REFERRAL: _('Gift for Referral'),
}

SCENE_LABELS_EN = {
    SceneName.SHOP_GENERAL: 'Shop General',
    SceneName.FIRST_ORDER: 'First Order',
    SceneName.REVIEW_REWARD: 'Review Reward',
    SceneName.PURCHASE_1688: '1688 Purchase',
    SceneName.AI_CONTENT: 'AI Content',
    SceneName.SUBSCRIPTION_RENEW: 'Renewal',
    SceneName.SUBSCRIPTION_UPGRADE: 'Upgrade',
    SceneName.NEW_USER: 'New User',
    SceneName.PROMOTION: 'Promotion',
    SceneName.REFERRAL: 'Referral',
}


def get_scene_label(scene: str, locale: str = 'zh-CN') -> str:
    """获取场景的可读名称。"""
    labels = SCENE_LABELS_EN if locale == 'en' else SCENE_LABELS
    return labels.get(scene, scene)
