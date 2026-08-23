#!/usr/bin/env python3
"""
Plugin Manager — 定价机制引擎
================================
可配置规则 + 纯计算函数。所有价格数字都是数据（由商店管理员日后通过
定价器产出并审核），本模块不写死任何具体价格。

规则来源（选择 A）：
  远端 verorun-store 仓库 `pricing_rules.json`（随目录同步），
  本地读取失败时回退到内嵌 DEFAULT_PRICING_RULES，保证机制始终可用。

计算层级：
  L1 单体定价：月 = M，季 = M × quarter_factor，年 = M × year_factor
  L2 配件定价：父插件已订阅 → 配件价 = 0.01 元 或 free（可配）
  L3 版本包定价：包价 = Σ(包内单品月价) × bundle_discount
  L4 促销：首期/限时/续费折扣系数
  L5 平滑升级：已订插件按剩余天数折算抵扣包价

单位约定：所有价格使用「分」（int），与 store_plugins.price_amount /
plugin_subscriptions.amount_fen 一致。
"""

import json
import os
import threading
from typing import Dict, List, Optional, Any
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── 内嵌默认规则（远端 pricing_rules.json 缺失/拉取失败时的兜底） ──────

DEFAULT_PRICING_RULES: Dict[str, Any] = {
    'version': 1,
    'quarter_factor': 2.7,           # 季价 = 月价 × 2.7（3 个月付 2.7 个月）
    'year_factor': 10.0,             # 年价 = 月价 × 10（12 个月付 10 个月，对齐 module_policy 约定）
    'round_rule': 'round',           # 分精度舍入: 'round' | 'ceil' | 'floor'
    'promo': {
        'first_period_rate': None,   # 首期折扣系数（如 0.8 = 8 折）
        'limited_time_rate': None,   # 限时折扣系数
        'renewal_rate': None,        # 续费折扣系数
        'stacking': False,           # 是否允许促销叠加（默认同一时刻仅一个促销生效）
    },
    'accessory_rule': {
        'enabled': True,
        'mode': 'free_or_penny',     # 'free_or_penny' | 'fixed_percent'
        'price_fen': 1,              # free_or_penny 模式：父订阅后配件价（1 分 = 0.01 元；0 = 免费）
        'fixed_percent': 0.1,        # fixed_percent 模式：配件价 = 父价 × 该比例
    },
    'bundles': {
        'pro': {
            'display_name': 'VeroRun Pro',
            # 包内插件清单（13 个内置订阅插件）；随版本定义调整
            'plugins': [
                'site_builder', 'analytics', 'visitor_profile', 'vault',
                'health_check', 'oauth_config', 'email', 'site_domains',
                'im_gateway', 'sms', 'captcha_embedded', 'chatbot',
                'memory_engine',
            ],
            # Pro 包包含 site_domains 20 域名权益（已确认），包价已覆盖该项
            'include_site_domains_20': True,
            'discount': 0.5,          # 包价 = Σ(包内单品月价) × 折扣
            'anchor': None,           # 可选锚定：最贵单品 × anchor（优先级高于 discount）
        },
    },
}

_PRICING_RULES_URL = os.environ.get(
    'PRICING_RULES_URL',
    'https://raw.githubusercontent.com/fanjumin/verorun-store/main/pricing_rules.json'
).strip()


# ── 规则加载（带内存缓存，失败回退默认） ──────────────────────────────

_rules_cache: Optional[Dict[str, Any]] = None
_rules_lock = threading.Lock()


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并规则：override 覆盖 base，未提供的键保留默认。"""
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_pricing_rules(force: bool = False) -> Dict[str, Any]:
    """加载定价规则：优先远端 pricing_rules.json，失败回退内嵌默认。

    远端文件结构（verorun-store 仓库）：
        { "version": 2, "quarter_factor": 2.7, "bundles": {...}, ... }
    仅做浅层校验（必须是 dict），非法/拉取失败一律回退默认，机制不中断。
    """
    global _rules_cache
    if _rules_cache is not None and not force:
        return _rules_cache
    with _rules_lock:
        if _rules_cache is not None and not force:
            return _rules_cache
        remote = {}
        if _PRICING_RULES_URL:
            try:
                req = Request(_PRICING_RULES_URL, headers={'User-Agent': 'VeroRun-PluginManager/1.0'})
                with urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())
                if isinstance(data, dict):
                    remote = data
            except Exception as e:
                print(f'[Pricing] fetch pricing_rules failed, fallback to defaults: {e}')
        rules = _deep_merge(DEFAULT_PRICING_RULES, remote)
        _rules_cache = rules
        return rules


def get_pricing_rules() -> Dict[str, Any]:
    return load_pricing_rules()


def _round_fen(value: float, rule: str = 'round') -> int:
    """按 round_rule 对分值取整（保证输出为 int 分）。"""
    if rule == 'ceil':
        return int(value + 0.999999)
    if rule == 'floor':
        return int(value)
    return int(round(value))


# ── L1 单体定价 ──────────────────────────────────────────────────────

def compute_tiered_price(base_month_fen: int, rules: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """输入基础月价 → 输出 月/季/年 三档价（分）。

    Args:
        base_month_fen: 插件基础月价（分），由商店管理员单独核定
        rules: 定价规则（None 时加载全局规则）

    Returns:
        {'month': int, 'quarter': int, 'year': int}
    """
    r = rules or get_pricing_rules()
    qf = float(r.get('quarter_factor', 2.7))
    yf = float(r.get('year_factor', 10.0))
    rule = r.get('round_rule', 'round')
    return {
        'month': _round_fen(float(base_month_fen), rule),
        'quarter': _round_fen(float(base_month_fen) * qf, rule),
        'year': _round_fen(float(base_month_fen) * yf, rule),
    }


# ── L2 配件定价 ──────────────────────────────────────────────────────

def resolve_accessory_price(plugin_id: str, parent_id: str,
                            parent_month_fen: int, parent_subscribed: bool,
                            rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """解析配件插件价格。

    Args:
        plugin_id: 配件插件标识（仅作日志/返回）
        parent_id: 父插件标识
        parent_month_fen: 父插件月价（分）
        parent_subscribed: 父插件当前是否已订阅
        rules: 定价规则

    Returns:
        {
            'mode': 'free' | 'penny' | 'fixed_percent' | 'normal',
            'month': int,           # 生效月价（分）
            'note': str,
        }
    """
    r = rules or get_pricing_rules()
    ar = r.get('accessory_rule') or {}
    if not ar.get('enabled', True):
        return {'mode': 'normal', 'month': parent_month_fen, 'note': 'accessory_rule disabled'}
    if not parent_subscribed:
        # 父插件未订阅：配件按自身价（正常价），由调用方传入自身价场景另行处理
        return {'mode': 'normal', 'month': parent_month_fen, 'note': f'parent {parent_id} not subscribed'}
    mode = ar.get('mode', 'free_or_penny')
    if mode == 'free_or_penny':
        price = int(ar.get('price_fen', 1))
        return {
            'mode': 'free' if price == 0 else 'penny',
            'month': price,
            'note': f'accessory of {parent_id}',
        }
    # fixed_percent 模式：配件价 = 父价 × 比例
    pct = float(ar.get('fixed_percent', 0.1))
    return {
        'mode': 'fixed_percent',
        'month': _round_fen(float(parent_month_fen) * pct, r.get('round_rule', 'round')),
        'note': f'fixed {pct} of parent {parent_id}',
    }


# ── L3 版本包定价 ────────────────────────────────────────────────────

def compute_bundle_price(bundle_id: str, plugin_month_prices: Dict[str, int],
                         rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """计算版本包三档价与对比信息。

    Args:
        bundle_id: 版本包标识（如 'pro'）
        plugin_month_prices: {插件 identifier: 月价(分)}，仅需包内插件
        rules: 定价规则

    Returns:
        {
            'bundle_id': str, 'display_name': str,
            'plugins': List[str], 'include_site_domains_20': bool,
            'sum_month_fen': int,           # Σ 包内单品月价
            'discount': float,
            'month': int, 'quarter': int, 'year': int,
            'saving_pct': float,            # 省多少 %
        }
    """
    r = rules or get_pricing_rules()
    bundles = r.get('bundles') or {}
    cfg = bundles.get(bundle_id)
    if not cfg:
        raise ValueError(f'unknown bundle id: {bundle_id}')

    plugins = [p for p in cfg.get('plugins', []) if p in plugin_month_prices]
    if not plugins:
        raise ValueError(f'bundle {bundle_id}: no price data for any member plugin')

    total = sum(int(plugin_month_prices[p]) for p in plugins)

    # 锚定优先：anchor = 最贵单品 × anchor；否则 discount = Σ × 折扣
    anchor = cfg.get('anchor')
    if anchor:
        month = _round_fen(max(int(plugin_month_prices[p]) for p in plugins) * float(anchor),
                           r.get('round_rule', 'round'))
    else:
        month = _round_fen(float(total) * float(cfg.get('discount', 0.5)),
                           r.get('round_rule', 'round'))

    tiered = compute_tiered_price(month, r)
    saving = (1.0 - (month / total)) * 100 if total > 0 else 0.0

    return {
        'bundle_id': bundle_id,
        'display_name': cfg.get('display_name', bundle_id),
        'plugins': plugins,
        'include_site_domains_20': bool(cfg.get('include_site_domains_20', False)),
        'sum_month_fen': total,
        'discount': float(cfg.get('discount', 0.5)),
        'month': tiered['month'],
        'quarter': tiered['quarter'],
        'year': tiered['year'],
        'saving_pct': round(saving, 1),
    }


# ── L4 促销 ──────────────────────────────────────────────────────────

def apply_promo(price_fen: int, promo_key: str,
                rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """应用促销折扣。

    Args:
        price_fen: 原价（分）
        promo_key: 促销类型: 'first_period' | 'limited_time' | 'renewal'
        rules: 定价规则

    Returns:
        {'price_fen': int, 'rate': Optional[float], 'note': str}
        无对应促销时原价返回，rate=None。
    """
    r = rules or get_pricing_rules()
    promo = r.get('promo') or {}
    rate = promo.get(promo_key + '_rate') if promo_key.endswith('_rate') else None
    if rate is None:
        rate = promo.get(promo_key, None)
    if rate is None:
        return {'price_fen': int(price_fen), 'rate': None, 'note': f'no promo {promo_key}'}
    discounted = _round_fen(float(price_fen) * float(rate), r.get('round_rule', 'round'))
    return {'price_fen': discounted, 'rate': float(rate), 'note': f'promo {promo_key} x{rate}'}


def apply_promo_tiered(prices: Dict[str, int], promo_key: str,
                       rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """对 月/季/年 三档统一应用促销。"""
    out = {}
    for k, v in prices.items():
        out[k] = apply_promo(v, promo_key, rules)
    return {'tiered': prices, 'applied': out}


# ── L5 平滑升级（剩余价值折算抵扣） ───────────────────────────────────

def compute_upgrade_credit(active_subs: List[Dict[str, Any]],
                           rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """计算已订插件升级版本包时的剩余价值折算抵扣。

    Args:
        active_subs: 活跃订阅列表，每项需含：
            plugin_id, amount_fen(分), current_period_start(ISO), current_period_end(ISO)
        rules: 定价规则

    Returns:
        {
            'credit_fen': int,          # 总折算抵扣（分）
            'items': [{'plugin_id', 'paid_fen', 'remaining_days', 'period_days', 'credit_fen'}],
        }
    """
    from datetime import datetime, timezone

    total = 0
    items = []
    now = datetime.now(timezone.utc)
    for sub in active_subs or []:
        try:
            start = datetime.fromisoformat(str(sub.get('current_period_start')).replace('Z', '+00:00'))
            end = datetime.fromisoformat(str(sub.get('current_period_end')).replace('Z', '+00:00'))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        period_days = max((end - start).total_seconds() / 86400.0, 1.0)
        remaining_days = max((end - now).total_seconds() / 86400.0, 0.0)
        paid = int(sub.get('amount_fen', 0))
        credit = _round_fen(paid * (remaining_days / period_days),
                            (rules or get_pricing_rules()).get('round_rule', 'round'))
        if credit > 0:
            total += credit
            items.append({
                'plugin_id': sub.get('plugin_id'),
                'paid_fen': paid,
                'remaining_days': round(remaining_days, 1),
                'period_days': round(period_days, 1),
                'credit_fen': credit,
            })
    return {'credit_fen': total, 'items': items}


def quote_bundle_upgrade(bundle_id: str, plugin_month_prices: Dict[str, int],
                         active_subs: List[Dict[str, Any]],
                         promo_key: Optional[str] = None,
                         rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """平滑升级报价：包价 − 剩余价值折算 = 应付差额。

    Args:
        bundle_id: 版本包标识
        plugin_month_prices: 包内插件月价表
        active_subs: 当前活跃订阅（用于折算）
        promo_key: 可选促销类型
        rules: 定价规则

    Returns:
        {
            'bundle': 包价明细,
            'credit_fen': int,
            'upgrade_cost_fen': int,    # 应付差额（≤0 表示免费升级，剩余价值顺延）
            'needs_payment': bool,
            'items': [...],
        }
    """
    r = rules or get_pricing_rules()
    bundle = compute_bundle_price(bundle_id, plugin_month_prices, r)
    credit = compute_upgrade_credit(active_subs, r)
    month_price = bundle['month']
    if promo_key:
        month_price = apply_promo(month_price, promo_key, r)['price_fen']
    cost = month_price - credit['credit_fen']
    return {
        'bundle': bundle,
        'credit_fen': credit['credit_fen'],
        'upgrade_cost_fen': max(cost, 0),
        'carryover_fen': max(-cost, 0),   # 差额为负时的顺延价值（分）
        'needs_payment': cost > 0,
        'items': credit['items'],
    }
