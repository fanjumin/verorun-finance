#!/usr/bin/env python3
"""Twilio SMS Provider — for international phone numbers.

Environment variables:
    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_PHONE_NUMBER  (e.g. +15017122661)
"""
import os
from .base import BaseSMSProvider


class TwilioSMSProvider(BaseSMSProvider):
    """Twilio SMS — sends plain-text messages to E.164 numbers."""

    PROVIDER = 'twilio'

    def send(self, phone: str, message: str, **kwargs) -> dict:
        account_sid = os.environ.get('TWILIO_ACCOUNT_SID', '')
        auth_token = os.environ.get('TWILIO_AUTH_TOKEN', '')
        from_number = os.environ.get('TWILIO_PHONE_NUMBER', '')

        if not all([account_sid, auth_token, from_number]):
            return {'success': False, 'provider': 'twilio', 'error': 'Twilio not configured'}

        import urllib.request, urllib.parse

        data = urllib.parse.urlencode({
            'To': phone,
            'From': from_number,
            'Body': message,
        }).encode()

        # Twilio uses Basic Auth with Account SID as username
        import base64
        credentials = base64.b64encode(f'{account_sid}:{auth_token}'.encode()).decode()

        req = urllib.request.Request(
            f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json',
            data=data,
            headers={
                'Authorization': f'Basic {credentials}',
                'Content-Type': 'application/x-www-form-urlencoded',
            },
        )
        try:
            import json
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read().decode())
            return {'success': True, 'provider': 'twilio', 'sid': result.get('sid', '')}
        except Exception as e:
            return {'success': False, 'provider': 'twilio', 'error': str(e)}

    def is_configured(self) -> bool:
        return bool(os.environ.get('TWILIO_ACCOUNT_SID', ''))
