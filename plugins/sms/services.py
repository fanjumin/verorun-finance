#!/usr/bin/env python3
"""SMS Plugin Services — 验证码发送核心逻辑

Provider 配置从 system_config 表读取（通过主库只读连接）。
与旧 auth-center/services/sms_service.py 兼容，逐步迁移至此。
"""
import os
import secrets
import string
from datetime import datetime

from .models import get_sms_db

# ── 模板映射（与旧系统兼容）──
TEMPLATE_MAP = {
    'register':        'SMS_506135003',
    'change_phone':    'SMS_506380001',
    'reset_password':  'SMS_506285002',
    'modify_password': 'SMS_506190002',
    'login':           'SMS_506330002',
}
DEFAULT_TEMPLATE = 'SMS_506135003'


def get_market():
    """Return current market: 'cn' or 'intl' (legacy, kept for backward compat)."""
    return os.environ.get('DEPLOY_MARKET', 'cn')


def _select_provider_by_phone(phone):
    """从手机号号段判断使用哪个提供商

    - +86（中国）→ Aliyun（即使部署在国际市场也用阿里云）
    - 其他区号    → Twilio
    - 无区号      → 按 DEPLOY_MARKET 环境变量
    """
    if phone and phone.startswith('+86'):
        return 'aliyun'
    if phone and phone.startswith('+'):
        return 'twilio'
    # 无区号时回退到 DEPLOY_MARKET
    return 'twilio' if get_market() == 'intl' else 'aliyun'


def get_sms_provider(phone=None):
    """Return the appropriate SMS provider instance.

    When phone is provided, uses smart routing based on phone number's country code.
    Falls back to DEPLOY_MARKET env var for backward compatibility.
    """
    import sys
    _auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
    if _auth_dir not in sys.path:
        sys.path.insert(0, _auth_dir)

    provider_type = _select_provider_by_phone(phone)

    if provider_type == 'twilio':
        try:
            from providers.sms.twilio import TwilioSMSProvider
            twilio = TwilioSMSProvider()
            if twilio.is_configured():
                return twilio
        except ImportError:
            pass

    if provider_type == 'aliyun':
        try:
            from providers.sms.aliyun import AliyunSMSProvider
            aliyun = AliyunSMSProvider()
            if aliyun.is_configured():
                return aliyun
        except ImportError:
            pass

    return None


def generate_code(length=6):
    return ''.join(secrets.choice(string.digits) for _ in range(length))


def send_sms(phone, code, purpose='login'):
    """Send verification code for a specific purpose.

    Uses phone-number-based provider selection:
      - +86        -> Aliyun (purpose-specific template)
      - other +XX  -> Twilio (plain-text message)
      - fallback   -> stub (console log + save to sms_logs)
    """
    provider = get_sms_provider(phone)
    provider_type = _select_provider_by_phone(phone)
    template = None
    result = None

    try:
        if provider:
            if provider_type == 'twilio':
                lang = os.environ.get('DEPLOY_LANG', 'en')
                if lang and lang.lower().startswith('zh'):
                    message = f'您的验证码是：{code}，10 分钟内有效。'
                else:
                    message = f'Your verification code is: {code}. Valid for 10 minutes.'
                result = provider.send(phone, message)
                result['template'] = 'plain_text'
                template = 'plain_text'
            elif provider.PROVIDER == 'aliyun':
                result = _send_aliyun_via_provider(provider, phone, code, purpose)
                template = TEMPLATE_MAP.get(purpose, DEFAULT_TEMPLATE)
        else:
            # Fallback: stub mode
            print(f"[SMS STUB] To: {phone} | Code: {code}")
            result = {'success': True, 'provider': 'stub', 'code': code}
    except Exception as e:
        print(f"[SmsPlugin] send_sms provider error: {e}")
        result = {'success': False, 'provider': provider_type, 'error': str(e)}

    # 记录发送日志（无论成功失败均记录）
    _log_send(phone, code, purpose, result.get('provider', 'stub'),
              'sent' if result.get('success') else 'failed',
              result.get('error', ''))

    return result


def _send_aliyun_via_provider(provider, phone, code, purpose='login'):
    """Send SMS via Aliyun provider (uses purpose-specific templates)."""
    return provider.send(phone, '', purpose=purpose, code=code)


def _log_send(phone, code, purpose, provider, status, error=''):
    """记录短信发送日志到 PG schema sms"""
    try:
        with get_sms_db() as conn:
            conn.execute(
                'INSERT INTO sms_logs (phone, code, purpose, provider, status, error) VALUES (?,?,?,?,?,?)',
                (phone, code, purpose, provider, status, error)
            )
            conn.commit()
    except Exception as e:
        print(f'[SmsPlugin] Log write failed: {e}')


def check_rate_limit(phone, max_per_hour=5):
    """Check if phone has exceeded SMS rate limit.

    频率限制表存放在插件自有 schema `sms` 中，遵守数据隔离规范
    （不再读写主库 public schema）。
    """
    with get_sms_db() as conn:
        hour_bucket = datetime.now().strftime('%Y%m%d_%H')
        row = conn.execute(
            'SELECT count FROM sms_rate_limits WHERE phone=? AND hour_bucket=?',
            (phone, hour_bucket)
        ).fetchone()
        if row and row['count'] >= max_per_hour:
            return False
        if row:
            conn.execute('UPDATE sms_rate_limits SET count=count+1 WHERE phone=? AND hour_bucket=?',
                         (phone, hour_bucket))
        else:
            conn.execute('INSERT INTO sms_rate_limits (phone, hour_bucket, count) VALUES (?,?,1)',
                         (phone, hour_bucket))
        conn.commit()
        return True


def validate_phone(phone, country_code=''):
    """Validate phone number format based on country code.

    - With country_code: use per-country validation rules
    - Phone starts with +: auto-detect country from number
    - No country info: generic E.164 validation
    """
    import re

    # 先用区号规则验证
    from .countries import find_country, detect_country_from_phone, validate_phone_by_country

    # 尝试从手机号自动解析国家
    country = detect_country_from_phone(phone)

    # 如果手机号没有 + 前缀但有 country_code 参数
    if not country and country_code:
        country = find_country(dial=country_code)

    if country:
        error = validate_phone_by_country(phone, country)
        if error:
            return False, phone, error
        return True, phone, ''

    # 无匹配国家时使用通用 E.164 验证
    cleaned = re.sub(r'[\s\-\(\)]+', '', phone)
    digits_only = cleaned.lstrip('+')
    if not digits_only.isdigit() or len(digits_only) < 7 or len(digits_only) > 15:
        return False, phone, 'Invalid phone number'

    if not cleaned.startswith('+'):
        if country_code:
            cleaned = '+' + country_code.lstrip('+') + digits_only
        else:
            cleaned = '+' + digits_only
    return True, cleaned, ''
