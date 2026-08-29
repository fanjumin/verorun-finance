#!/usr/bin/env python3
"""SMS Service — phone verification code sending.

Supports multiple providers via market-based strategy:
  - CN market: Aliyun SMS (purpose-specific templates), fallback to stub
  - INTL market: Twilio SMS (plain text), fallback to stub

Backwards-compatible: existing callers of send_sms() continue to work unchanged.
"""
import os, secrets, string
from datetime import datetime

# ── Legacy template mapping (kept for backward compat with direct callers) ──
TEMPLATE_MAP = {
    'register':        'SMS_506135003',
    'change_phone':    'SMS_506380001',
    'reset_password':  'SMS_506285002',
    'modify_password': 'SMS_506190002',
    'login':           'SMS_506330002',
}
DEFAULT_TEMPLATE = 'SMS_506135003'


def get_market():
    """Return current market: 'cn' or 'intl'."""
    return os.environ.get('DEPLOY_MARKET', 'cn')


def get_sms_provider():
    """Return the appropriate SMS provider instance based on market + config.

    Priority:
      1. If DEPLOY_MARKET=intl and Twilio configured → TwilioSMSProvider
      2. If Aliyun configured → AliyunSMSProvider
      3. Otherwise → None (caller falls back to stub)
    """
    market = get_market()

    if market == 'intl':
        try:
            from providers.sms.twilio import TwilioSMSProvider
            twilio = TwilioSMSProvider()
            if twilio.is_configured():
                return twilio
        except ImportError:
            pass

    # CN market or fallback: try Aliyun
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

    Uses market-based provider selection:
      - intl + Twilio → sends plain-text message
      - CN + Aliyun → sends purpose-specific template
      - fallback → stub (console log)
    """
    provider = get_sms_provider()
    market = get_market()

    if provider:
        if market == 'intl' and provider.PROVIDER == 'twilio':
            message = f'Your verification code is: {code}. Valid for 10 minutes.'
            result = provider.send(phone, message)
            result['template'] = 'plain_text'
            return result
        elif provider.PROVIDER == 'aliyun':
            return _send_aliyun_via_provider(provider, phone, code, purpose)

    # Fallback: stub mode — verification code is only printed to console/server logs;
    # never echoed back to the API caller (see auth-center/routes/auth.py sms_send)
    print(f"[SMS STUB] To: {phone} | Code: {code}")
    return {'success': True, 'provider': 'stub'}


def _send_aliyun_via_provider(provider, phone, code, purpose='login'):
    """Send SMS via Aliyun provider (uses purpose-specific templates)."""
    return provider.send(phone, '', purpose=purpose, code=code)


def check_rate_limit(phone, max_per_hour=5):
    """Check if phone has exceeded SMS rate limit."""
    from models import get_db, now_iso
    hour_bucket = datetime.now().strftime('%Y%m%d_%H')
    with get_db() as conn:
        row = conn.execute(
            'SELECT count FROM sms_rate_limits WHERE phone=%s AND hour_bucket=%s',
            (phone, hour_bucket)
        ).fetchone()
        if row and row['count'] >= max_per_hour:
            return False
        if row:
            conn.execute('UPDATE sms_rate_limits SET count=count+1 WHERE phone=%s AND hour_bucket=%s',
                         (phone, hour_bucket))
        else:
            conn.execute('INSERT INTO sms_rate_limits (phone, hour_bucket, count) VALUES (%s,%s,1)',
                         (phone, hour_bucket))
        conn.commit()
    return True


def validate_phone(phone, country_code=''):
    """Validate phone number format based on market.

    Args:
        phone: Raw phone number string
        country_code: International dialing code (e.g. '+1' for US)

    Returns:
        (is_valid: bool, normalized_phone: str, error: str)
    """
    import re
    market = get_market()

    if market == 'intl':
        # International: E.164 format (8-15 digits, optional leading +)
        cleaned = re.sub(r'[\s\-\(\)]+', '', phone)
        digits_only = cleaned.lstrip('+')
        if not digits_only.isdigit() or len(digits_only) < 7 or len(digits_only) > 15:
            return False, phone, 'Invalid phone number'
        # Normalize: ensure starts with +
        if not cleaned.startswith('+'):
            if country_code and country_code.startswith('+'):
                cleaned = country_code + digits_only
            elif country_code:
                cleaned = '+' + country_code.lstrip('+') + digits_only
            else:
                cleaned = '+' + digits_only
        return True, cleaned, ''
    else:
        # CN: 11 digits, starts with 1
        if not phone or len(phone) != 11 or not phone.isdigit() or not phone.startswith('1'):
            return False, phone, 'Please enter a valid phone number'
        return True, phone, ''
