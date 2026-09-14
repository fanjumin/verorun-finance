#!/usr/bin/env python3
"""Reddit 社媒渠道适配器（Phase 3）。

OAuth 2.0（installable app）授权后 channel_accounts 存 access_token + refresh_token
（永久有效）。发布用 PRAW 以 refresh_token 授权（user_agent 按 Reddit 规范）。
app 凭据（reddit_client_id / reddit_client_secret）来自 channel_configs。
"""
from .. import register_channel_adapter
from . import _SocialChannelBase, app_credentials


@register_channel_adapter
class RedditChannel(_SocialChannelBase):
    channel = 'reddit'
    auth_mode = 'oauth'
    supports_test = False

    def send(self, payload: dict, **kw) -> dict:
        try:
            cfg = self._account()
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
        try:
            import praw
            creds = app_credentials(self.channel)
            refresh = cfg.get('refresh_token', '')
            if refresh:
                reddit = praw.Reddit(
                    client_id=creds.get('reddit_client_id', ''),
                    client_secret=creds.get('reddit_client_secret', ''),
                    refresh_token=refresh,
                    user_agent='verorun-unified-gateway/2.0',
                )
            else:
                # 手动配置兜底（旧 script app 模式）
                reddit = praw.Reddit(
                    client_id=creds.get('reddit_client_id', ''),
                    client_secret=creds.get('reddit_client_secret', ''),
                    username=cfg.get('reddit_username', ''),
                    password=cfg.get('reddit_password', ''),
                    user_agent='verorun-unified-gateway/2.0',
                )

            title = (payload.get('title')
                     or payload.get('summary')
                     or (payload.get('body') or '')[:80])
            link_url = payload.get('link_url', '')
            subreddit_name = kw.get('subreddit') or payload.get('subreddit') or ''
            if not subreddit_name:
                return {'success': False, 'error': 'No subreddit specified'}
            subreddit = reddit.subreddit(subreddit_name)
            if link_url:
                submission = subreddit.submit(title=title, url=link_url)
            else:
                selftext = (payload.get('summary') or payload.get('body', ''))[:40000]
                submission = subreddit.submit(title=title, selftext=selftext)
            return {
                'success': True,
                'post_id': submission.id,
                'url': f'https://reddit.com{submission.permalink}',
                'error': '',
            }
        except ImportError:
            return {'success': False, 'error': 'praw not installed'}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
