#!/usr/bin/env python3
"""Payment Gateway — abstract base class.

Defines the interface for creating payments, verifying webhooks, and processing refunds.
"""
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any


class BasePaymentGateway(ABC):
    """Abstract base for payment gateways (Alipay, WeChat, Stripe, PayPal)."""

    PROVIDER: str = ''  # e.g. 'stripe', 'paypal', 'alipay', 'wechat'

    @abstractmethod
    def is_configured(self) -> bool:
        """Check if this gateway has credentials configured."""
        ...

    @abstractmethod
    def create_payment(self, order_no: str, description: str,
                       amount_cents: int, currency: str = 'USD',
                       return_url: str = '', **kwargs) -> Dict[str, Any]:
        """Create a payment and return payment parameters.

        Args:
            order_no: Internal order number
            description: Payment description
            amount_cents: Amount in smallest currency unit (cents)
            currency: ISO 4217 currency code (e.g. 'USD', 'CNY')
            return_url: URL to redirect after payment (if applicable)
            **kwargs: Gateway-specific options

        Returns:
            dict with keys:
                - success: bool
                - payment_url: str (URL for redirect/embedded payment)
                - transaction_id: str (gateway transaction ID)
                - raw: dict (gateway raw response, for logging)
        """
        ...

    @abstractmethod
    def verify_webhook(self, payload: bytes, headers: Dict[str, str],
                       **kwargs) -> Dict[str, Any]:
        """Verify and parse a webhook callback.

        Args:
            payload: Raw request body bytes
            headers: Request headers dict
            **kwargs: Gateway-specific options

        Returns:
            dict with keys:
                - verified: bool
                - order_no: str (internal order number)
                - transaction_id: str (gateway transaction ID)
                - status: str ('paid', 'failed', 'refunded')
                - raw: dict (raw response for logging)
        """
        ...

    @abstractmethod
    def refund(self, transaction_id: str, amount_cents: Optional[int] = None,
               **kwargs) -> Dict[str, Any]:
        """Process a refund.

        Args:
            transaction_id: Gateway transaction ID
            amount_cents: Amount to refund (None = full)
            **kwargs: Gateway-specific options

        Returns:
            dict with keys:
                - success: bool
                - refund_id: str
                - raw: dict
        """
        ...

    def get_provider_name(self) -> str:
        return self.PROVIDER or self.__class__.__name__.lower()
