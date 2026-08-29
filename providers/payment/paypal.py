#!/usr/bin/env python3
"""PayPal Payment Gateway — Orders API v2 (REST).

Environment variables:
    PAYPAL_CLIENT_ID
    PAYPAL_CLIENT_SECRET
    PAYPAL_WEBHOOK_ID       (webhook verification ID)
"""
import os, json, base64, urllib.request, urllib.parse
from typing import Optional, Dict, Any
from .base import BasePaymentGateway


class PayPalPaymentGateway(BasePaymentGateway):
    """PayPal Orders API v2 — REST (no SDK required)."""

    PROVIDER = 'paypal'

    API_BASE = 'https://api-m.paypal.com'

    def __init__(self):
        self._client_id = os.environ.get('PAYPAL_CLIENT_ID', '')
        self._client_secret = os.environ.get('PAYPAL_CLIENT_SECRET', '')
        self._webhook_id = os.environ.get('PAYPAL_WEBHOOK_ID', '')
        if os.environ.get('PAYPAL_SANDBOX', '').lower() in ('true', '1'):
            self.API_BASE = 'https://api-m.sandbox.paypal.com'

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _get_access_token(self) -> str:
        """Get OAuth 2.0 access token from PayPal."""
        credentials = base64.b64encode(
            f'{self._client_id}:{self._client_secret}'.encode()
        ).decode()
        req = urllib.request.Request(
            f'{self.API_BASE}/v1/oauth2/token',
            data=b'grant_type=client_credentials',
            headers={
                'Authorization': f'Basic {credentials}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json',
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        return data.get('access_token', '')

    def create_payment(self, order_no: str, description: str,
                       amount_cents: int, currency: str = 'USD',
                       return_url: str = '', **kwargs) -> Dict[str, Any]:
        if not self.is_configured():
            return {'success': False, 'error': 'PayPal not configured'}
        try:
            token = self._get_access_token()
            amount_str = f'{amount_cents / 100:.2f}'
            body = {
                'intent': 'CAPTURE',
                'purchase_units': [{
                    'reference_id': order_no,
                    'description': description or '',
                    'amount': {
                        'currency_code': currency.upper(),
                        'value': amount_str,
                    },
                }],
                'payment_source': {
                    'paypal': {
                        'experience_context': {
                            'payment_method_preference': 'IMMEDIATE_PAYMENT_REQUIRED',
                            'landing_page': 'LOGIN',
                            'user_action': 'PAY_NOW',
                            'return_url': return_url or '',
                            'cancel_url': return_url or '',
                        }
                    }
                }
            }
            req = urllib.request.Request(
                f'{self.API_BASE}/v2/checkout/orders',
                data=json.dumps(body).encode(),
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'PayPal-Request-Id': order_no,
                },
            )
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read().decode())
            approval_url = ''
            for link in result.get('links', []):
                if link.get('rel') == 'payer-action':
                    approval_url = link['href']
                    break
            return {
                'success': True,
                'provider': 'paypal',
                'payment_url': approval_url,
                'transaction_id': result.get('id', ''),
                'raw': result,
            }
        except Exception as e:
            return {'success': False, 'provider': 'paypal', 'error': str(e)}

    def verify_webhook(self, payload: bytes, headers: Dict[str, str],
                       **kwargs) -> Dict[str, Any]:
        if not self._webhook_id:
            return {'verified': False, 'error': 'PayPal webhook not configured'}
        try:
            token = self._get_access_token()
            verification_body = {
                'auth_algo': headers.get('paypal-auth-algo', ''),
                'cert_url': headers.get('paypal-cert-url', ''),
                'transmission_id': headers.get('paypal-transmission-id', ''),
                'transmission_sig': headers.get('paypal-transmission-sig', ''),
                'transmission_time': headers.get('paypal-transmission-time', ''),
                'webhook_id': self._webhook_id,
                'webhook_event': json.loads(payload.decode()),
            }
            req = urllib.request.Request(
                f'{self.API_BASE}/v1/notifications/verify-webhook-signature',
                data=json.dumps(verification_body).encode(),
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
            )
            resp = urllib.request.urlopen(req, timeout=15)
            veri_result = json.loads(resp.read().decode())
            if veri_result.get('verification_status') != 'SUCCESS':
                return {'verified': False, 'error': 'PayPal webhook verification failed'}
            event = json.loads(payload.decode())
            event_type = event.get('event_type', '')
            resource = event.get('resource', {})
            status = 'unknown'
            if event_type in ('PAYMENT.CAPTURE.COMPLETED', 'CHECKOUT.ORDER.APPROVED'):
                status = 'paid'
            elif event_type == 'PAYMENT.CAPTURE.DENIED':
                status = 'failed'
            elif event_type == 'PAYMENT.CAPTURE.REFUNDED':
                status = 'refunded'
            order_no = ''
            purchase_units = resource.get('purchase_units', [])
            if purchase_units:
                order_no = purchase_units[0].get('reference_id', '')
            if not order_no:
                order_no = resource.get('custom_id', resource.get('invoice_id', ''))
            return {
                'verified': True,
                'order_no': order_no,
                'transaction_id': resource.get('id', event.get('id', '')),
                'status': status,
                'raw': event,
            }
        except Exception as e:
            return {'verified': False, 'error': str(e)}

    def refund(self, transaction_id: str, amount_cents: Optional[int] = None,
               **kwargs) -> Dict[str, Any]:
        if not self.is_configured():
            return {'success': False, 'error': 'PayPal not configured'}
        try:
            token = self._get_access_token()
            body = {}
            if amount_cents:
                body['amount'] = {
                    'currency_code': 'USD',
                    'value': f'{amount_cents / 100:.2f}',
                }
            req = urllib.request.Request(
                f'{self.API_BASE}/v2/payments/captures/{transaction_id}/refund',
                data=json.dumps(body).encode(),
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                method='POST',
            )
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read().decode())
            return {
                'success': True,
                'provider': 'paypal',
                'refund_id': result.get('id', ''),
                'raw': result,
            }
        except Exception as e:
            return {'success': False, 'provider': 'paypal', 'error': str(e)}
