#!/usr/bin/env python3
"""
模块策略引擎 — Phase 2
=====================
Phase 1：策略硬编码，所有用户默认可试用。
Phase 2：接入 subscriptions.module_states 做真实状态判断。
"""
import json
import logging
from datetime import datetime, timedelta

from i18n import _

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 模块策略配置（Phase 2 仍硬编码，后续可移入 subscription_plans）
# ═══════════════════════════════════════════════════════════════
MODULE_POLICIES = {
    'site_builder': {
        'pattern': 'one_shot',
        'trial_days': 14,
        'trial_daily_limit': None,
        'post_trial_action': 'lock',
        'refund_days': 14,
        'limit_even_byok': False,
        'price_month_fen': 19900,
        'price_year_fen': 199900,
        'name': 'Site Builder Pro',
        'desc': _('LLM 一键生成多页面品牌官网'),
    },
    'content_factory': {
        'pattern': 'interactive',
        'trial_days': 14,
        'trial_daily_limit': 3,
        'post_trial_action': 'lock',
        'refund_days': 0,
        'limit_even_byok': True,
        'price_month_fen': 9900,
        'price_year_fen': 99000,
        'name': 'Content Factory',
        'desc': _('AI 内容工厂，批量生成文章'),
    },
    'cms': {
        'pattern': 'interactive',
        'trial_days': 14,
        'trial_daily_limit': 5,
        'post_trial_action': 'lock',
        'refund_days': 0,
        'limit_even_byok': True,
        'price_month_fen': 9900,
        'price_year_fen': 99000,
        'name': 'AI CMS',
        'desc': _('智能内容管理，对话生成+编辑+发布'),
    },
    'commerce_plus': {
        'pattern': 'interactive',
        'trial_days': 14,
        'trial_daily_limit': None,
        'post_trial_action': 'lock',
        'refund_days': 0,
        'limit_even_byok': False,
        'price_month_fen': 19900,
        'price_year_fen': 199900,
        'name': 'Commerce Plus',
        'desc': _('1688 供应链采集 + 电商商城'),
    },
    'service_hub': {
        'pattern': 'interactive',
        'trial_days': 14,
        'trial_daily_limit': None,
        'post_trial_action': 'lock',
        'refund_days': 0,
        'limit_even_byok': False,
        'price_month_fen': 9900,
        'price_year_fen': 99000,
        'name': 'Service Hub',
        'desc': _('智能客服 + FAQ + 工单系统'),
    },
    'workflow': {
        'pattern': 'continuous',
        'trial_days': 14,
        'trial_daily_limit': None,
        'post_trial_action': 'pause',
        'refund_days': 0,
        'limit_even_byok': False,
        'price_month_fen': 14900,
        'price_year_fen': 149900,
        'name': 'Workflow Engine',
        'desc': _('自动化工作流 + 定时任务'),
    },
    'social_push': {
        'pattern': 'publish',
        'trial_days': 14,
        'trial_daily_limit': None,
        'post_trial_action': 'pay_per_use',
        'refund_days': 0,
        'limit_even_byok': False,
        'price_month_fen': 4900,
        'price_year_fen': 49000,
        'name': 'Social Media Suite',
        'desc': _('多平台一键内容分发'),
    },
    'mini_app': {
        'pattern': 'one_shot',
        'trial_days': 14,
        'trial_daily_limit': None,
        'post_trial_action': 'lock',
        'refund_days': 14,
        'limit_even_byok': False,
        'price_month_fen': 29900,
        'price_year_fen': 299900,
        'name': 'Mini-App Generator',
        'desc': _('抖音/微信小程序源码生成'),
    },
}

DOMAIN_TO_MODULE = {
    'site_builder': 'site_builder',
    'business': 'commerce_plus',
    'service': 'service_hub',
}

FREE_DOMAINS = {
    '', 'general', 'athena', 'finance', 'ops',
    'monitor', 'alerter', 'cron', 'deployment',
}

# ═══════════════════════════════════════════════════════════════
# 能力意图索引（通用能力检测）
# 用户指令命中关键词 → 判定需要对应付费插件的能力。
# 仅收录商店付费订阅插件（price_type=sub）；新增付费插件时在此追加一行即可，
# 无需改任何逻辑（数据驱动）。
# ═══════════════════════════════════════════════════════════════
CAPABILITY_INDEX = {
    'mini_app_builder': ['小程序', '微信小程序', '抖音小程序', 'mini program', 'taro', 'mini_app_builder'],
    'payment': ['收款', '支付渠道配置', '商户配置', 'payment_gateway'],
    'subscription': ['订阅系统', '会员订阅', 'subscription_system'],
    'shop': ['商城', '在线商店', '购物车', '商品管理', '订单管理'],
    'coupons': ['优惠券', 'coupons'],
    'content_factory': ['内容工厂', '批量生成文章', 'rss 采集', 'content_factory'],
    'social_push': ['社媒推送', '社交分发', 'linkedin', 'social_push'],
    'ads': ['广告位', '广告投放', '广告管理', 'ads_plugin'],
    'reviews': ['评价系统', '商品评价', 'reviews_plugin'],
    'wishlist': ['心愿单', '收藏功能', 'wishlist_plugin'],
    'currency_converter': ['货币换算', '汇率转换', 'currency_converter'],
    'order_notify': ['订单通知', 'order_notify'],
    'logistics': ['物流查询', '快递查询', '物流跟踪', 'logistics_plugin'],
    'enterprise_verify': ['企业认证', '企业资质验证', 'enterprise_verify'],
    'verification': ['实名认证', '身份核验', 'verification_plugin'],
    'ali_api': ['阿里云', '1688', '供应链采集', 'ali_api'],
}


def detect_capability_plugins(instruction: str) -> list:
    """扫描用户指令，返回命中的付费插件标识符列表（数据驱动）。"""
    if not instruction:
        return []
    text = instruction.lower()
    matched = []
    for plugin_id, keywords in CAPABILITY_INDEX.items():
        for kw in keywords:
            if kw.lower() in text:
                matched.append(plugin_id)
                break
    return matched

# 可访问状态：这些状态下模块可用
ACCESSIBLE_STATUSES = {'trial', 'paying', 'active'}


def get_module_key(agent_config: dict, task_def: dict = None) -> str:
    """从 Agent 配置推断付费模块 key，返回 None 表示免费模块"""
    domain = (agent_config.get('domain') or '').lower()
    if domain in FREE_DOMAINS:
        return None
    if domain in DOMAIN_TO_MODULE:
        return DOMAIN_TO_MODULE[domain]
    if domain == 'content':
        target_module = (task_def.get('target_module') or '').lower() if task_def else ''
        if 'social_push' in target_module or 'social' in target_module:
            return 'social_push'
        if 'content_factory' in target_module:
            return 'content_factory'
        return 'cms'
    if domain in ('workflow', 'automation'):
        return 'workflow'
    return None


class ModulePolicyEngine:
    """模块策略引擎 — Phase 2"""

    def __init__(self, db_getter=None, main_db_getter=None):
        self._get_db = db_getter          # 用于 module_usage_log
        self._get_main_db = main_db_getter  # 用于 subscriptions + module_pricing
        self._db_policies = None           # 缓存 DB 策略

    def get_policy(self, module_key: str) -> dict:
        """获取模块策略配置（DB 优先，硬编码兜底）"""
        db_policies = self._load_db_policies()
        if db_policies and module_key in db_policies:
            return db_policies[module_key]
        return MODULE_POLICIES.get(module_key)

    def reload_policies(self):
        """清除缓存，强制下次从 DB 重新加载"""
        self._db_policies = None

    def list_all_policies(self) -> dict:
        """返回所有模块策略（用于管理后台）"""
        db_policies = self._load_db_policies()
        result = {}
        for key, policy in MODULE_POLICIES.items():
            result[key] = dict(policy)
        for key, db_policy in (db_policies or {}).items():
            result[key] = dict(db_policy)
        return result

    def _load_db_policies(self) -> dict:
        """从 module_pricing 表加载策略（带缓存）"""
        if self._db_policies is not None:
            return self._db_policies

        if not self._get_main_db:
            return {}

        try:
            with self._get_main_db() as conn:
                rows = conn.execute(
                    "SELECT item_key AS module_key, name_zh AS name, description_zh AS description, "
                    "billing_mode AS pattern, trial_days, trial_daily_limit, "
                    "post_trial_action, refund_days, limit_even_byok, "
                    "price_month AS price_month_fen, price_year AS price_year_fen "
                    "FROM subscription.sub_items WHERE category='module' AND is_active = 1 ORDER BY sort_order"
                ).fetchall()

                policies = {}
                for row in rows:
                    r = dict(row)
                    key = r.pop('module_key')
                    # 转换 BIGINT 空值为 None
                    daily_limit = r.get('trial_daily_limit')
                    policies[key] = {
                        'pattern': r.get('pattern', 'interactive'),
                        'trial_days': int(r.get('trial_days', 14)),
                        'trial_daily_limit': int(daily_limit) if daily_limit is not None else None,
                        'post_trial_action': r.get('post_trial_action', 'lock'),
                        'refund_days': int(r.get('refund_days', 0)),
                        'limit_even_byok': bool(r.get('limit_even_byok', 0)),
                        'price_month_fen': int(r.get('price_month_fen', 0)),
                        'price_year_fen': int(r.get('price_year_fen', 0)),
                        'name': r.get('name', ''),
                        'desc': r.get('description', ''),
                    }

                self._db_policies = policies
                return policies
        except Exception as e:
            logger.warning(f"[ModulePolicy] _load_db_policies failed: {e}")
            return {}

    # ═══════════════════════════════════════════════════════════
    # 能力闸门辅助（通用能力检测）
    # ═══════════════════════════════════════════════════════════

    def is_plugin_installed(self, plugin_id: str) -> bool:
        """判断插件是否已安装（plugin_registry 状态为 installed/enabled/active）。

        查询失败时保守放行（返回 True，避免误伤正常请求）。
        """
        if not self._get_main_db:
            return True
        try:
            with self._get_main_db() as conn:
                row = conn.execute(
                    "SELECT status FROM plugin_registry WHERE identifier = %s",
                    (plugin_id,)
                ).fetchone()
                if not row:
                    return False
                return row['status'] in ('installed', 'enabled', 'active')
        except Exception as e:
            logger.warning(f"[ModulePolicy] is_plugin_installed({plugin_id}) failed: {e}")
            return True

    def subscribe_prompt(self, plugin_id: str) -> str:
        """生成付费插件订阅引导文案。"""
        return f'This capability requires subscribing to the {plugin_id} plugin, please subscribe in the plugin store.'

    # ═══════════════════════════════════════════════════════════
    # 访问控制（Phase 2：接入真实 DB）
    # ═══════════════════════════════════════════════════════════

    def check_access(self, user_id: int, module_key: str) -> tuple:
        """
        检查模块是否可用。
        Phase 2：从 subscriptions.module_states 读取真实状态。
        首次使用自动开启试用。

        Returns: (allowed: bool, reason: str)
        """
        policy = self.get_policy(module_key)
        if not policy:
            return (True, '')  # 非付费模块

        if not user_id:
            return (True, '')  # 无用户上下文，放行

        state = self._get_module_state(user_id, module_key)

        if state is None:
            # 首次使用 → 自动开启试用
            self.start_trial(user_id, module_key)
            return (True, '')

        status = state.get('status', '')

        if status in ACCESSIBLE_STATUSES:
            # 检查试用是否到期（状态是 trial 但已过期）
            if status == 'trial':
                trial_end_str = state.get('trial_end', '')
                if trial_end_str:
                    try:
                        trial_end = datetime.fromisoformat(trial_end_str)
                        if datetime.now() > trial_end:
                            # 试用已到期，执行到期处理
                            action = policy.get('post_trial_action', 'lock')
                            self._apply_expire_action(user_id, module_key, action)
                            return self._blocked_result(module_key, action)
                    except (ValueError, TypeError):
                        pass
            return (True, '')

        # 不可用状态
        reason_map = {
            'locked': f'Module {module_key} is locked, please subscribe',
            'paused': f'Module {module_key} is paused, please subscribe to resume',
            'expired': f'Module {module_key} has expired',
            'pay_per_use': f'Module {module_key} requires pay-per-use',
        }
        return (False, reason_map.get(status, f'Module {module_key} is unavailable'))

    # ═══════════════════════════════════════════════════════════
    # 状态管理
    # ═══════════════════════════════════════════════════════════

    def start_trial(self, user_id: int, module_key: str):
        """首次使用 → 自动开启试用"""
        policy = self.get_policy(module_key)
        if not policy:
            return

        trial_days = policy.get('trial_days', 14)
        now = datetime.now()
        trial_end = now + timedelta(days=trial_days)
        # one_shot 模式：refund 窗口 = 试用天数（无额外付费，直接生效）
        refund_days = policy.get('refund_days', 0)

        state = {
            'status': 'trial',
            'trial_start': now.isoformat(),
            'trial_end': trial_end.isoformat(),
            'paid_at': None,
            'effective_at': None,
            'refundable_until': None,
        }

        if refund_days > 0:
            state['refundable_until'] = (now + timedelta(days=refund_days)).isoformat()

        self._save_module_state(user_id, module_key, state)
        logger.info(f"[ModulePolicy] Trial started: user={user_id} module={module_key} end={trial_end.isoformat()}")

    def record_payment(self, user_id: int, module_key: str):
        """用户付费 → paying 状态，设置退款窗口"""
        policy = self.get_policy(module_key)
        if not policy:
            return

        refund_days = policy.get('refund_days', 0)
        now = datetime.now()
        state = self._get_module_state(user_id, module_key) or {}

        state.update({
            'status': 'paying',
            'paid_at': now.isoformat(),
        })

        if refund_days > 0:
            state['refundable_until'] = (now + timedelta(days=refund_days)).isoformat()
            state['effective_at'] = (now + timedelta(days=refund_days)).isoformat()
        else:
            # 无退款窗口 → 直接生效
            state['status'] = 'active'
            state['effective_at'] = now.isoformat()
            state['refundable_until'] = None

        self._save_module_state(user_id, module_key, state)
        logger.info(f"[ModulePolicy] Payment recorded: user={user_id} module={module_key} status={state['status']}")

    def refund_module(self, user_id: int, module_key: str):
        """退款 → locked"""
        state = self._get_module_state(user_id, module_key) or {}
        state['status'] = 'locked'
        state['refunded_at'] = datetime.now().isoformat()
        self._save_module_state(user_id, module_key, state)
        logger.info(f"[ModulePolicy] Refunded: user={user_id} module={module_key}")

    def activate_module(self, user_id: int, module_key: str):
        """退款窗口到期 → paying → active"""
        state = self._get_module_state(user_id, module_key) or {}
        if state.get('status') == 'paying':
            state['status'] = 'active'
            state['effective_at'] = datetime.now().isoformat()
            state['refundable_until'] = None
            self._save_module_state(user_id, module_key, state)
            logger.info(f"[ModulePolicy] Activated: user={user_id} module={module_key}")

    # ═══════════════════════════════════════════════════════════
    # 配额检查
    # ═══════════════════════════════════════════════════════════

    def _is_admin_user(self, user_id: int) -> bool:
        """检查用户是否为管理员"""
        if not user_id or not self._get_main_db:
            return False
        try:
            with self._get_main_db() as conn:
                row = conn.execute(
                    "SELECT is_admin FROM users WHERE id = %s",
                    (user_id,)
                ).fetchone()
                return bool(row and row['is_admin'])
        except Exception:
            return False

    def check_quota(self, user_id: int, module_key: str) -> tuple:
        """
        检查当日配额。
        仅对 interactive 模式 + trial_daily_limit 不为 None 的模块生效。
        管理员免配额限制。

        Returns: (allowed: bool, used: int, limit: int)
        """
        # 管理员豁免
        if self._is_admin_user(user_id):
            return (True, 0, 0)

        policy = self.get_policy(module_key)
        if not policy:
            return (True, 0, 0)

        if policy['pattern'] != 'interactive':
            return (True, 0, 0)

        daily_limit = policy.get('trial_daily_limit')
        if daily_limit is None:
            return (True, 0, 0)

        used = self._get_today_usage(user_id, module_key)
        return (used < daily_limit, used, daily_limit)

    # ═══════════════════════════════════════════════════════════
    # 用量记录
    # ═══════════════════════════════════════════════════════════

    def record_usage(self, user_id: int, module_key: str,
                     agent_id: int, task_id: str):
        """记录一次成功的 Agent 调用（管理员不计费）"""
        if not user_id or not self._get_db:
            return
        if self._is_admin_user(user_id):
            return
        try:
            with self._get_db() as conn:
                conn.execute(
                    """INSERT INTO module_usage_log
                       (user_id, module_key, agent_id, task_id)
                       VALUES (%s, %s, %s, %s)""",
                    (user_id, module_key, agent_id, task_id)
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[ModulePolicy] record_usage failed: {e}")

    # ═══════════════════════════════════════════════════════════
    # 每日扫描（由定时任务调用）
    # ═══════════════════════════════════════════════════════════

    def daily_scan(self):
        """
        每日凌晨扫描：
        1. 试用到期 → 执行 post_trial_action
        2. 退款窗口到期 → paying → active
        """
        if not self._get_main_db:
            return

        now = datetime.now()
        try:
            with self._get_main_db() as conn:
                rows = conn.execute(
                    """SELECT user_id, module_states FROM subscription.user_subscriptions
                       WHERE module_states IS NOT NULL AND module_states != '{}'"""
                ).fetchall()

                for row in rows:
                    user_id = row['user_id']
                    states = json.loads(row['module_states'] or '{}')
                    changed = False

                    for module_key, state in states.items():
                        if not isinstance(state, dict):
                            continue

                        status = state.get('status', '')
                        policy = MODULE_POLICIES.get(module_key)
                        if not policy:
                            continue

                        # 1) 试用到期
                        if status == 'trial':
                            trial_end_str = state.get('trial_end', '')
                            if trial_end_str:
                                try:
                                    trial_end = datetime.fromisoformat(trial_end_str)
                                    if now > trial_end:
                                        action = policy.get('post_trial_action', 'lock')
                                        states[module_key] = self._build_expired_state(state, action, now)
                                        changed = True
                                        logger.info(f"[DailyScan] Trial expired: user={user_id} module={module_key} action={action}")
                                except (ValueError, TypeError):
                                    pass

                        # 2) 退款窗口到期
                        if status == 'paying':
                            refund_until_str = state.get('refundable_until', '')
                            if refund_until_str:
                                try:
                                    refund_until = datetime.fromisoformat(refund_until_str)
                                    if now > refund_until:
                                        states[module_key]['status'] = 'active'
                                        states[module_key]['refundable_until'] = None
                                        changed = True
                                        logger.info(f"[DailyScan] Activated: user={user_id} module={module_key}")
                                except (ValueError, TypeError):
                                    pass

                    if changed:
                        conn.execute(
                            "UPDATE subscription.user_subscriptions SET module_states = %s, updated_at = NOW() WHERE user_id = %s",
                            (json.dumps(states), user_id)
                        )
                if rows:
                    conn.commit()
        except Exception as e:
            logger.error(f"[DailyScan] Scan failed: {e}")

    # ═══════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════

    def _get_module_state(self, user_id: int, module_key: str) -> dict:
        """从 subscriptions.module_states 读取单个模块状态"""
        if not self._get_main_db:
            return None
        try:
            with self._get_main_db() as conn:
                row = conn.execute(
                    "SELECT module_states FROM subscription.user_subscriptions WHERE user_id = %s",
                    (user_id,)
                ).fetchone()
                if not row or not row['module_states']:
                    return None
                states = json.loads(row['module_states'] or '{}')
                return states.get(module_key)
        except Exception as e:
            logger.warning(f"[ModulePolicy] _get_module_state failed: {e}")
            return None

    def _save_module_state(self, user_id: int, module_key: str, state: dict):
        """写入单个模块状态到 subscriptions.module_states"""
        if not self._get_main_db:
            return
        try:
            with self._get_main_db() as conn:
                row = conn.execute(
                    "SELECT module_states FROM subscription.user_subscriptions WHERE user_id = %s",
                    (user_id,)
                ).fetchone()

                if row:
                    states = json.loads(row['module_states'] or '{}')
                else:
                    # 用户还没有 subscriptions 记录 → 创建一条占位记录
                    now_iso = datetime.now().isoformat()
                    conn.execute(
                        """INSERT INTO subscription.user_subscriptions
                           (user_id, item_key, interval_type, amount_fen, status, period_start, period_end, module_states)
                           VALUES (%s, 'free', 'month', 0, 'active', %s, %s, '{}')
                           ON CONFLICT (user_id, item_key) DO NOTHING""",
                        (user_id, now_iso, (datetime.now() + timedelta(days=36500)).isoformat())
                    )
                    conn.commit()
                    states = {}

                states[module_key] = state
                conn.execute(
                    "UPDATE subscription.user_subscriptions SET module_states = %s, updated_at = NOW() WHERE user_id = %s",
                    (json.dumps(states), user_id)
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[ModulePolicy] _save_module_state failed: {e}")

    def _apply_expire_action(self, user_id: int, module_key: str, action: str):
        """执行到期动作"""
        state = self._get_module_state(user_id, module_key) or {}
        now = datetime.now()
        state = self._build_expired_state(state, action, now)
        self._save_module_state(user_id, module_key, state)

    def _build_expired_state(self, old_state: dict, action: str, now: datetime) -> dict:
        """构建到期后的新状态"""
        status_map = {
            'lock': 'locked',
            'pause': 'paused',
            'pay_per_use': 'pay_per_use',
        }
        return {
            **old_state,
            'status': status_map.get(action, 'locked'),
            'expired_at': now.isoformat(),
        }

    def _blocked_result(self, module_key: str, action: str) -> tuple:
        """构建 blocked 返回"""
        reason_map = {
            'lock': f'Module {module_key} trial expired, please subscribe',
            'pause': f'Module {module_key} trial expired, please subscribe to resume',
            'pay_per_use': f'Module {module_key} trial expired, pay-per-use required',
        }
        return (False, reason_map.get(action, f'Module {module_key} is unavailable'))

    def _get_today_usage(self, user_id: int, module_key: str) -> int:
        """查询今日用量"""
        if not self._get_db:
            return 0
        try:
            with self._get_db() as conn:
                row = conn.execute(
                    """SELECT COUNT(*) as c FROM module_usage_log
                       WHERE user_id = %s AND module_key = %s
                       AND used_at::date = CURRENT_DATE""",
                    (user_id, module_key)
                ).fetchone()
                return row['c'] if row else 0
        except Exception as e:
            logger.warning(f"[ModulePolicy] _get_today_usage failed: {e}")
            return 0


# ═══════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════
_engine = None


def get_policy_engine():
    """获取模块策略引擎单例"""
    global _engine
    if _engine is None:
        import os as _os, sys as _sys
        # agent_matrix DB（用于 module_usage_log）
        _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', '..', 'agent_matrix'))
        from agent_matrix.models import get_db as _agent_get_db
        # auth-center 主 DB（用于 subscriptions）
        _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', 'models'))
        from database import get_db as _auth_get_db
        _engine = ModulePolicyEngine(
            db_getter=_agent_get_db,
            main_db_getter=_auth_get_db,
        )
    return _engine
