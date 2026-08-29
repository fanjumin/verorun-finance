#!/usr/bin/env python3
"""Twitter/X Social Push Provider.

Environment variables:
    TWITTER_API_KEY
    TWITTER_API_SECRET
    TWITTER_ACCESS_TOKEN
    TWITTER_ACCESS_SECRET
    TWITTER_BEARER_TOKEN
"""
import os, json
from typing import Optional
from .base import BaseSocialPushProvider


class TwitterPushProvider(BaseSocialPushProvider):
    """Twitter/X API v2 — tweet publishing."""

    PROVIDER = 'twitter'

    def __init__(self):
        self._api_key = os.environ.get('TWITTER_API_KEY', '')
        self._api_secret = os.environ.get('TWITTER_API_SECRET', '')
        self._access_token = os.environ.get('TWITTER_ACCESS_TOKEN', '')
        self._access_secret = os.environ.get('TWITTER_ACCESS_SECRET', '')
        self._bearer_token = os.environ.get('TWITTER_BEARER_TOKEN', '')

    def is_configured(self) -> bool:
        return bool(self._bearer_token or (self._api_key and self._api_secret))

    def publish(self, title: str, body: str, summary: str = '',
                image_url: str = '', link_url: str = '',
                **kwargs) -> dict:
        if not self.is_configured():
            return {'success': False, 'error': 'Twitter not configured'}

        try:
            import tweepy
            auth = tweepy.OAuth1UserHandler(
                self._api_key, self._api_secret,
                self._access_token, self._access_secret
            )
            api = tweepy.API(auth)
            client = tweepy.Client(
                bearer_token=self._bearer_token,
                consumer_key=self._api_key,
                consumer_secret=self._api_secret,
                access_token=self._access_token,
                access_token_secret=self._access_secret,
            )

            text = (title or '')[:280]
            if link_url:
                remaining = 280 - len(link_url) - 2
                text = (title or '')[:remaining] + ' ' + link_url

            resp = client.create_tweet(text=text)
            tweet_id = resp.data['id'] if resp.data else ''

            return {
                'success': True,
                'post_id': tweet_id,
                'url': f'https://twitter.com/i/web/status/{tweet_id}' if tweet_id else '',
                'error': '',
            }
        except ImportError:
            return {'success': False, 'error': 'tweepy not installed'}
        except Exception as e:
            return {'success': False, 'error': str(e)}
