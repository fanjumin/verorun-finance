#!/usr/bin/env python3
"""
Currency Converter Services — 汇率抓取 / 内存缓存 / 换算
==========================================================

数据流:
  外部 API ──→ 内存缓存 (TTL) ──→ PostgreSQL 持久化 (兜底)
                ↑
            前端查询 ←── 用户偏好
"""
import time
import json
import logging
import os
import sys
from typing import Optional, Dict, Tuple
from datetime import datetime, timezone

import httpx

from .models import get_db, init_db

logger = logging.getLogger(__name__)

# ── 内存缓存 ────────────────────────────────────────────

_RATE_CACHE: Dict[str, float] = {}       # {currency_code: rate_to_base}
_CACHE_TIME: float = 0                    # time.time() of last fetch
_CACHE_TTL: int = 3600                    # default 1h, overridden at runtime

_BASE_CURRENCY: str = 'CNY'

# ── 默认币种信息 ────────────────────────────────────────

CURRENCY_INFO = {
    'CNY': {'symbol': '¥',  'name': 'Chinese Yuan',       'decimals': 2},
    'USD': {'symbol': '$',  'name': 'US Dollar',           'decimals': 2},
    'EUR': {'symbol': '€',  'name': 'Euro',                'decimals': 2},
    'GBP': {'symbol': '£',  'name': 'British Pound',       'decimals': 2},
    'JPY': {'symbol': '¥',  'name': 'Japanese Yen',        'decimals': 0},
    'HKD': {'symbol': 'HK$','name': 'Hong Kong Dollar',    'decimals': 2},
    'KRW': {'symbol': '₩',  'name': 'South Korean Won',    'decimals': 0},
    'AUD': {'symbol': 'A$', 'name': 'Australian Dollar',    'decimals': 2},
    'CAD': {'symbol': 'C$', 'name': 'Canadian Dollar',     'decimals': 2},
    'SGD': {'symbol': 'S$', 'name': 'Singapore Dollar',    'decimals': 2},
    'THB': {'symbol': '฿',  'name': 'Thai Baht',           'decimals': 2},
    'MYR': {'symbol': 'RM', 'name': 'Malaysian Ringgit',   'decimals': 2},
    'PHP': {'symbol': '₱',  'name': 'Philippine Peso',     'decimals': 2},
    'IDR': {'symbol': 'Rp', 'name': 'Indonesian Rupiah',   'decimals': 0},
    'VND': {'symbol': '₫',  'name': 'Vietnamese Dong',     'decimals': 0},
    'TWD': {'symbol': 'NT$','name': 'Taiwan Dollar',       'decimals': 2},
    'MOP': {'symbol': 'MOP$','name': 'Macanese Pataca',    'decimals': 2},
    'NZD': {'symbol': 'NZ$','name': 'New Zealand Dollar',  'decimals': 2},
    'CHF': {'symbol': 'Fr', 'name': 'Swiss Franc',         'decimals': 2},
    'SEK': {'symbol': 'kr', 'name': 'Swedish Krona',       'decimals': 2},
}


def configure(base_currency: str = 'CNY', cache_ttl: int = 3600):
    """运行时更新配置（由插件启用时调用）"""
    global _BASE_CURRENCY, _CACHE_TTL
    _BASE_CURRENCY = base_currency.upper()
    _CACHE_TTL = cache_ttl


def _check_cache() -> bool:
    """内存缓存是否有效"""
    return bool(_RATE_CACHE) and (time.time() - _CACHE_TIME) < _CACHE_TTL


# ── 外部 API 抓取 ────────────────────────────────────────

async def fetch_rates_from_frankfurter() -> Optional[Dict[str, float]]:
    """主源: Frankfurter API (欧洲央行数据, 免费, ~30+ 币种)"""
    url = f'https://api.frankfurter.dev/v1/latest?from={_BASE_CURRENCY}'  # Q5: 官方主域名 .dev，v1 路径实测为 /v1/latest
    try:
        # 修复 FX-D1: follow_redirects=True（frankfurter 对 from= 参数返回 301）
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        rates = data.get('rates', {})
        # Frankfurter 返回的汇率不含基础币种自己
        rates[_BASE_CURRENCY] = 1.0
        return {k.upper(): v for k, v in rates.items()}
    except Exception as e:
        logger.warning(f'[CurrencyConverter] Frankfurter API failed: {e}')
        return None


async def fetch_rates_from_open_er() -> Optional[Dict[str, float]]:
    """备用源: Open Exchange Rate API (免费, ~170 币种)"""
    url = f'https://open.er-api.com/v6/latest/{_BASE_CURRENCY}'
    try:
        # 修复 FX-D1: follow_redirects=True（frankfurter 对 from= 参数返回 301）
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        rates = data.get('rates', {})
        rates[_BASE_CURRENCY] = 1.0
        return {k.upper(): v for k, v in rates.items()}
    except Exception as e:
        logger.warning(f'[CurrencyConverter] OpenER API failed: {e}')
        return None


async def sync_rates() -> int:
    """
    同步汇率到本地缓存 + 数据库。
    返回写入的币种数。0 表示失败（使用本地缓存）。

    尝试顺序: Frankfurter → OpenER → 本地缓存
    """
    rates = await fetch_rates_from_frankfurter()
    if rates is None:
        rates = await fetch_rates_from_open_er()

    if rates is None:
        logger.warning('[CurrencyConverter] All APIs failed, using local cache')
        return 0

    # 更新内存缓存
    global _RATE_CACHE, _CACHE_TIME
    _RATE_CACHE = rates
    _CACHE_TIME = time.time()

    # 持久化到 PostgreSQL
    with get_db() as conn:
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        for code, rate in rates.items():
            conn.execute('''
                INSERT INTO exchange_rates (currency_code, rate_to_base, base_currency, source, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(currency_code) DO UPDATE SET
                    rate_to_base = excluded.rate_to_base,
                    base_currency = excluded.base_currency,
                    fetched_at = excluded.fetched_at
            ''', (code, rate, _BASE_CURRENCY, 'api', now))
        conn.commit()

    logger.info(f'[CurrencyConverter] Synced {len(rates)} exchange rates')
    return len(rates)


# ── 从数据库恢复缓存 ────────────────────────────────────

def _load_rates_from_db() -> bool:
    """从 PostgreSQL 加载汇率到内存缓存（启动时调用）"""
    try:
        with get_db() as conn:
            rows = conn.execute('SELECT currency_code, rate_to_base FROM exchange_rates').fetchall()
            if not rows:
                return False
            global _RATE_CACHE, _CACHE_TIME
            _RATE_CACHE = {r['currency_code']: r['rate_to_base'] for r in rows}
            _CACHE_TIME = time.time()
            logger.info(f'[CurrencyConverter] Loaded {len(rows)} rates from database')
            return True
    except Exception as e:
        logger.warning(f'[CurrencyConverter] Failed to load rates from DB: {e}')
        return False


def ensure_rates_loaded():
    """确保汇率已加载（API 首次或 DB 恢复）"""
    if _check_cache():
        return
    if _load_rates_from_db():
        return
    # 没有缓存也没有数据库 → 首次使用，返回基础汇率
    global _RATE_CACHE, _CACHE_TIME
    _RATE_CACHE = {_BASE_CURRENCY: 1.0}
    _CACHE_TIME = time.time()


# ── 换算核心 ─────────────────────────────────────────────

def convert(amount: float, from_currency: str, to_currency: str) -> Tuple[float, float]:
    """
    币种换算。

    Args:
        amount: 金额
        from_currency: 源币种 (ISO 4217)
        to_currency: 目标币种 (ISO 4217)

    Returns:
        (converted_amount, rate_used)
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    ensure_rates_loaded()

    if from_currency == to_currency:
        return amount, 1.0

    # 换算路径: from → base → to
    # 1. from 到 base
    from_rate = _RATE_CACHE.get(from_currency)
    if from_rate is None:
        raise ValueError(f'Currency not supported: {from_currency}')
    base_amount = amount / from_rate  # 换算回基准币种

    # 2. base 到 to
    to_rate = _RATE_CACHE.get(to_currency)
    if to_rate is None:
        raise ValueError(f'Currency not supported: {to_currency}')
    result = base_amount * to_rate

    return result, to_rate


def format_amount(amount: float, currency: str) -> str:
    """
    格式化金额显示。

    Args:
        amount: 金额
        currency: 币种代码

    Returns:
        格式化字符串，如 "$12.99"、"¥1,500"
    """
    info = CURRENCY_INFO.get(currency.upper(), {'symbol': '', 'decimals': 2})
    fmt = f'{{:,.{info["decimals"]}f}}'
    return f'{info["symbol"]}{fmt.format(amount)}'


# ── 用户偏好 ─────────────────────────────────────────────

def get_user_preferred_currency(user_id: int) -> str:
    """获取用户偏好的币种"""
    try:
        with get_db() as conn:
            row = conn.execute(
                'SELECT preferred_currency FROM user_currency_prefs WHERE user_id=?',
                (user_id,)
            ).fetchone()
            if row:
                return row['preferred_currency']
    except Exception:
        pass
    return _BASE_CURRENCY


def set_user_preferred_currency(user_id: int, currency: str) -> bool:
    """设置用户偏好的币种"""
    try:
        with get_db() as conn:
            conn.execute('''
                INSERT INTO user_currency_prefs (user_id, preferred_currency, updated_at)
                VALUES (?, ?, NOW())
                ON CONFLICT(user_id) DO UPDATE SET
                    preferred_currency=excluded.preferred_currency,
                    updated_at=excluded.updated_at
            ''', (user_id, currency.upper()))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f'[CurrencyConverter] Failed to set preference: {e}')
        return False


# ── 对外批量数据 ─────────────────────────────────────────

def get_all_rates() -> Dict[str, float]:
    """获取所有汇率映射 {code: rate_to_base}"""
    ensure_rates_loaded()
    return dict(_RATE_CACHE)


def get_enabled_currencies(config_currencies: list = None) -> list:
    """获取启用的币种列表（含展示信息）"""
    codes = config_currencies or list(CURRENCY_INFO.keys())
    return [
        {'code': code, **CURRENCY_INFO.get(code, {'symbol': '', 'name': code, 'decimals': 2})}
        for code in codes if code in CURRENCY_INFO
    ]


# ── GeoIP 币种自动检测 ──────────────────────────────────

# ISO 3166-1 国家代码 → 推荐币种映射
COUNTRY_TO_CURRENCY = {
    # 大中华区
    'CN': 'CNY', 'HK': 'HKD', 'TW': 'TWD', 'MO': 'MOP',
    # 北美
    'US': 'USD', 'CA': 'CAD',
    # 欧洲
    'DE': 'EUR', 'FR': 'EUR', 'IT': 'EUR', 'ES': 'EUR', 'NL': 'EUR',
    'BE': 'EUR', 'AT': 'EUR', 'IE': 'EUR', 'PT': 'EUR', 'FI': 'EUR',
    'GR': 'EUR', 'LU': 'EUR', 'SK': 'EUR', 'SI': 'EUR', 'EE': 'EUR',
    'LV': 'EUR', 'LT': 'EUR', 'CY': 'EUR', 'MT': 'EUR', 'HR': 'EUR',
    'GB': 'GBP', 'CH': 'CHF', 'SE': 'SEK',
    # 亚太
    'JP': 'JPY', 'KR': 'KRW', 'AU': 'AUD', 'NZ': 'NZD',
    'SG': 'SGD', 'TH': 'THB', 'MY': 'MYR', 'PH': 'PHP',
    'ID': 'IDR', 'VN': 'VND',
    # 其他
    'AE': 'USD', 'SA': 'SAR', 'IN': 'INR', 'BR': 'BRL',
}

# GeoIP 引擎是否已初始化
_GEOIP_READY = False


def _init_geoip() -> bool:
    """初始化 GeoIP 引擎（懒加载）"""
    global _GEOIP_READY
    if _GEOIP_READY:
        return True
    try:
        # 尝试从 analytics 插件加载 GeoIP
        analytics_dir = os.path.join(os.path.dirname(__file__), '..', 'analytics')
        if analytics_dir not in sys.path:
            sys.path.insert(0, analytics_dir)
        from geoip import init_geoip, geoip_lookup
        init_geoip()
        _GEOIP_READY = True
        return True
    except Exception as e:
        logger.warning(f'[CurrencyConverter] GeoIP init failed: {e}')
        return False


def detect_currency_by_ip(ip: str) -> dict:
    """
    根据访客 IP 自动检测推荐币种。

    Args:
        ip: 访客 IP 地址

    Returns:
        {'currency': 'CNY', 'country': 'CN', 'source': 'geoip'}
        或 {'currency': 'CNY', 'country': '', 'source': 'default'}
    """
    default = {'currency': _BASE_CURRENCY, 'country': '', 'source': 'default'}

    if not ip or ip in ('127.0.0.1', '0.0.0.0', '::1') or ip.startswith(('192.168.', '10.', '172.')):
        return default

    if not _init_geoip():
        return default

    try:
        from geoip import geoip_lookup
        loc = geoip_lookup(ip)
        country = loc.get('country', '')
        if not country:
            return default
        currency = COUNTRY_TO_CURRENCY.get(country, _BASE_CURRENCY)
        return {'currency': currency, 'country': country, 'source': 'geoip'}
    except Exception as e:
        logger.debug(f'[CurrencyConverter] GeoIP lookup failed: {e}')
        return default
