#!/usr/bin/env python3
"""
Base class for international social media push providers.

Each provider subclass implements a publish() method that handles
authentication, content formatting, and API interaction for a
specific social media platform.
"""
from abc import ABC, abstractmethod
from typing import Optional


class BaseSocialProvider(ABC):
    """Abstract base for social media content publishing providers."""

    PROVIDER: str = ''

    @abstractmethod
    def is_configured(self) -> bool:
        """Check whether all required credentials are present."""
        ...

    @abstractmethod
    def get_config_fields(self) -> list:
        """Return the list of config field definitions for admin UI.

        Each item: {'key': str, 'label': str, 'type': 'text'|'password'}
        """
        ...

    @abstractmethod
    def publish(self, title: str, body: str, summary: str = '',
                image_url: str = '', link_url: str = '',
                config: Optional[dict] = None, **kwargs) -> dict:
        """Publish content to the social media platform.

        Args:
            title: Article or post title
            body: Main body text
            summary: Optional short summary
            image_url: Optional cover image URL
            link_url: Optional external link
            config: Dict of credentials/configuration from system_config

        Returns:
            dict with keys: success, post_id, url, error
        """
        ...
