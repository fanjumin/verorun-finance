#!/usr/bin/env python3
"""Social Push Plugin — 社媒账号只读访问（权威源：im_gateway.channel_accounts）

P1：账号权威源收敛到 im_gateway channel_accounts。
- 本模块只读消费 im_gateway 账号表，不做任何写入。
- social_push 自有 accounts 表保留作兜底，新增逻辑不再写它。
- wechat（client_credential）无 channel_accounts，仍以 system_config 配置为准。
"""
import logging

logger = logging.getLogger(__name__)

# social_push 平台 id → im_gateway 渠道 id 映射
PLATFORM_TO_CHANNEL = {
    'wechat': 'wechat_oa',
    'weibo': 'weibo',
    'toutiao': 'toutiao',
    'twitter': 'twitter',
    'linkedin': 'linkedin',
    'reddit': 'reddit',
    'telegram': 'telegram_channel',
}

# 敏感字段（脱敏展示用）
_SENSITIVE_KEYS = (
    'access_token', 'refresh_token', 'app_secret', 'client_secret',
    'api_secret', 'access_secret', 'bot_token', 'bearer_token',
    'password', 'token',
)


def list_connected_accounts(platform: str = '') -> list:
    """列出已连接的社媒账号（脱敏）。platform 为 social_push 平台 id，空=全部。

    返回列表项：{id, channel, account_key, handle, token_expires_at, is_enabled, config_summary}
    """
    try:
        from plugins.im_gateway.models_accounts import list_accounts as gw_list
    except Exception:
        logger.exception('[SocialPush] im_gateway unavailable')
        return []

    ch = PLATFORM_TO_CHANNEL.get(platform) if platform else None
    try:
        rows = gw_list(channel=ch)
    except Exception:
        logger.exception('[SocialPush] list_accounts failed')
        return []

    result = []
    for r in rows:
        cfg = r.get('config') or {}
        result.append({
            'id': r.get('id'),
            'channel': r.get('channel'),
            'account_key': r.get('account_key', ''),
            'handle': r.get('handle', ''),
            'token_expires_at': r.get('token_expires_at', ''),
            'is_enabled': bool(r.get('is_enabled')),
            'config_summary': {
                k: v for k, v in cfg.items()
                if k not in _SENSITIVE_KEYS and not str(k).lower().endswith(('_secret', '_token'))
            },
        })
    return result


def has_active_account(platform: str) -> bool:
    """平台是否已有可用账号（check-config 用）。

    - OAuth 渠道：im_gateway channel_accounts 存在启用账号
    - wechat（client_credential）：system_config 已配置 AppID/AppSecret
    """
    if platform == 'wechat':
        return _wechat_configured()
    ch = PLATFORM_TO_CHANNEL.get(platform)
    if not ch:
        return False
    try:
        from plugins.im_gateway.models_accounts import list_accounts as gw_list
        rows = gw_list(channel=ch)
    except Exception:
        logger.exception('[SocialPush] has_active_account failed: %s', platform)
        return False
    return any(bool(r.get('is_enabled')) for r in rows)


def _wechat_configured() -> bool:
    """微信公众号：client_credential 模式，凭据在 system_config"""
    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM system_config "
                "WHERE key IN ('wechat_app_id', 'wechat_app_secret')"
            ).fetchall()
        cfg = {r['key']: r['value'] for r in rows}
        return bool(cfg.get('wechat_app_id') and cfg.get('wechat_app_secret'))
    except Exception:
        logger.exception('[SocialPush] wechat config check failed')
        return False
