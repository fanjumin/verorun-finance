#!/usr/bin/env python3
"""SMS Provider — abstract base class.

Subclasses must implement send() and is_configured().
"""
from abc import ABC, abstractmethod


class BaseSMSProvider(ABC):
    """Abstract base for SMS providers."""

    PROVIDER: str = ''  # set by subclass

    @abstractmethod
    def send(self, phone: str, message: str, **kwargs) -> dict:
        """Send an SMS message.

        Args:
            phone: Recipient phone number (E.164 format for intl, plain for CN)
            message: Text content to send
            **kwargs: Provider-specific options (e.g. template_code, purpose)

        Returns:
            dict: {'success': bool, 'provider': str, ...}
        """
        ...

    @abstractmethod
    def is_configured(self) -> bool:
        """Check if provider credentials are configured."""
        ...

    def get_provider_name(self) -> str:
        return self.PROVIDER or self.__class__.__name__.lower()
