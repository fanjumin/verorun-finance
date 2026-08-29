#!/usr/bin/env python3
"""
Social Push Provider Registry — factory for international platform adapters.

Each platform is registered with its provider class and market availability.
Market filtering ensures CN users only see domestic platforms while
international users see all available platforms.
"""
from typing import Optional
from .base import BaseSocialProvider
from .twitter import TwitterPushProvider
from .linkedin import LinkedInPushProvider
from .reddit import RedditPushProvider
from .telegram_channel import TelegramChannelPushProvider

# Provider class registry: provider_id -> class
_PROVIDERS = {
    'twitter': TwitterPushProvider,
    'linkedin': LinkedInPushProvider,
    'reddit': RedditPushProvider,
    'telegram': TelegramChannelPushProvider,
}

# Which markets each provider is available in
# cn = China domestic, intl = international (all)
_PROVIDER_MARKET = {
    'twitter':  ('intl',),
    'linkedin': ('intl',),
    'reddit':   ('intl',),
    'telegram': ('intl',),
}


def get_provider(provider_name: str, config: Optional[dict] = None) -> BaseSocialProvider:
    """Return a provider instance by name, or None if unknown."""
    cls = _PROVIDERS.get(provider_name)
    return cls(config) if cls else None


def list_providers(market: str = '') -> list:
    """List all provider IDs available for the given market.

    Args:
        market: 'cn', 'intl', or '' (empty = return all)

    Returns:
        List of provider ID strings
    """
    if not market:
        return list(_PROVIDERS.keys())

    result = []
    for pid in _PROVIDERS:
        allowed = _PROVIDER_MARKET.get(pid, ())
        if market in allowed:
            result.append(pid)
    return result


def get_provider_info(market: str = '', configs: dict = None) -> dict:
    """Return provider metadata dict keyed by provider_id.

    Each item: {provider_id: {'name': str, 'icon': str, 'configured': bool}}

    Args:
        market: 'cn', 'intl', or '' (empty = return all)
        configs: system_config dict subset; passed to providers so their
                 is_configured() can reflect real credential status.
    """
    info = {}
    for pid in list_providers(market):
        provider = get_provider(pid, configs)
        if provider is None:
            continue
        info[pid] = {
            'id': pid,
            'name': getattr(provider, 'DISPLAY_NAME', pid),
            'icon': getattr(provider, 'ICON', ''),
            'configured': provider.is_configured(),
        }
    return info
