#!/usr/bin/env python3
"""Stripe Payment Gateway — Payment Intents API.

Environment variables:
    STRIPE_SECRET_KEY        (sk_live_...)
    STRIPE_PUBLISHABLE_KEY   (pk_live_...)
    STRIPE_WEBHOOK_SECRET    (whsec_...)
"""
import os, json
from typing import Optional, Dict, Any
from .base import BasePaymentGateway


class StripePaymentGateway(BasePaymentGateway):
    """Stripe Payment Intents + Webhooks."""

    PROVIDER = 'stripe'

    def __init__(self):
        self._secret_key = os.environ.get('STRIPE_SECRET_KEY', '')
        self._publishable_key = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
        self._webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

    def is_configured(self) -> bool:
        return bool(self._secret_key and self._publishable_key)

    def create_payment(self, order_no: str, description: str,
                       amount_cents: int, currency: str = 'USD',
                       return_url: str = '', **kwargs) -> Dict[str, Any]:
        if not self.is_configured():
            return {'success': False, 'error': 'Stripe not configured'}

        try:
            import stripe as stripe_lib
            stripe_lib.api_key = self._secret_key
            intent = stripe_lib.PaymentIntent.create(
                amount=amount_cents,
                currency=currency.lower(),
                description=description or '',
                metadata={'order_no': order_no},
                receipt_email=kwargs.get('email', ''),
            )
            return {
                'success': True,
                'provider': 'stripe',
                'payment_url': '',  # Stripe Elements handles client-side
                'client_secret': intent.client_secret,
                'transaction_id': intent.id,
                'publishable_key': self._publishable_key,
                'raw': {'id': intent.id, 'status': intent.status},
            }
        except Exception as e:
            return {'success': False, 'provider': 'stripe', 'error': str(e)}

    def verify_webhook(self, payload: bytes, headers: Dict[str, str],
                       **kwargs) -> Dict[str, Any]:
        if not self._webhook_secret:
            return {'verified': False, 'error': 'Stripe webhook not configured'}

        try:
            import stripe as stripe_lib
            stripe_lib.api_key = self._secret_key
            sig_header = headers.get('Stripe-Signature', '')
            event = stripe_lib.Webhook.construct_event(
                payload, sig_header, self._webhook_secret
            )
            event_type = event.get('type', '')
            data_obj = event.get('data', {}).get('object', {})
            status = 'unknown'
            if event_type in ('payment_intent.succeeded', 'checkout.session.completed'):
                status = 'paid'
            elif event_type == 'payment_intent.payment_failed':
                status = 'failed'
            elif event_type == 'charge.refunded':
                status = 'refunded'
            return {
                'verified': True,
                'order_no': data_obj.get('metadata', {}).get('order_no', ''),
                'transaction_id': data_obj.get('id', ''),
                'status': status,
                'raw': event,
            }
        except Exception as e:
            return {'verified': False, 'error': str(e)}

    def refund(self, transaction_id: str, amount_cents: Optional[int] = None,
               **kwargs) -> Dict[str, Any]:
        if not self.is_configured():
            return {'success': False, 'error': 'Stripe not configured'}
        try:
            import stripe as stripe_lib
            stripe_lib.api_key = self._secret_key
            params = {'payment_intent': transaction_id}
            if amount_cents:
                params['amount'] = amount_cents
            refund = stripe_lib.Refund.create(**params)
            return {
                'success': True,
                'provider': 'stripe',
                'refund_id': refund.id,
                'raw': {'id': refund.id, 'status': refund.status},
            }
        except Exception as e:
            return {'success': False, 'provider': 'stripe', 'error': str(e)}
