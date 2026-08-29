#!/usr/bin/env python3
"""Base class for logistics/tracking providers."""
from abc import ABC, abstractmethod


class BaseLogisticsProvider(ABC):
    """Abstract base for shipping/tracking providers."""

    PROVIDER: str = ''

    @abstractmethod
    def is_configured(self) -> bool:
        ...

    @abstractmethod
    def track(self, tracking_number: str, carrier: str = '', **kwargs) -> dict:
        """Track a shipment by tracking number.

        Returns:
            dict with keys: success, status, location, estimated_delivery, events, error
        """
        ...

    @abstractmethod
    def get_rates(self, origin: dict, destination: dict,
                  parcel: dict, **kwargs) -> dict:
        """Get shipping rates.

        Args:
            origin: {'country', 'state', 'city', 'zip'}
            destination: {'country', 'state', 'city', 'zip'}
            parcel: {'weight', 'length', 'width', 'height', 'unit'}

        Returns:
            dict with keys: success, rates (list), error
        """
        ...
