#!/usr/bin/env python3
"""Base class for social media push providers."""
from abc import ABC, abstractmethod
from typing import Optional


class BaseSocialPushProvider(ABC):
    """Abstract base for social media content publishing."""

    PROVIDER: str = ''

    @abstractmethod
    def is_configured(self) -> bool:
        ...

    @abstractmethod
    def publish(self, title: str, body: str, summary: str = '',
                image_url: str = '', link_url: str = '',
                **kwargs) -> dict:
        """Publish content to social media.

        Returns:
            dict with keys: success, post_id, url, error
        """
        ...
