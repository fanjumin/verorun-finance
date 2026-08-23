"""OAuth Config Plugin — OAuth Providers"""
import os
import importlib

MARKET = os.environ.get('DEPLOY_MARKET', 'cn')
_RUNTIME_CLASSES = {}

def register_provider(market: str, category: str, provider_cls):
    _RUNTIME_CLASSES.setdefault(market, {}).setdefault(category, []).append(provider_cls)

def _lazy_load(module_path: str, class_name: str = None):
    try:
        module = importlib.import_module(module_path)
        if class_name:
            return getattr(module, class_name, None)
        return module
    except (ImportError, AttributeError, ModuleNotFoundError):
        return None

_PROVIDER_REGISTRY = {
    'cn': {
        'oauth': [('plugins.oauth_config.providers.wechat', 'WeChatOAuthProvider'),
                  ('plugins.oauth_config.providers.alipay', 'AlipayOAuthProvider'),
                  ('plugins.oauth_config.providers.douyin', 'DouyinOAuthProvider')],
    },
    'intl': {
        'oauth': [('plugins.oauth_config.providers.google', 'GoogleOAuthProvider'),
                  ('plugins.oauth_config.providers.github', 'GitHubOAuthProvider'),
                  ('plugins.oauth_config.providers.facebook', 'FacebookOAuthProvider'),
                  ('plugins.oauth_config.providers.telegram', 'TelegramOAuthProvider')],
    },
}

def get_provider(category: str, position: int = 0):
    market_cfg = _PROVIDER_REGISTRY.get(MARKET, {})
    providers = market_cfg.get(category)
    if not providers or position >= len(providers):
        return None
    entry = providers[position]
    if isinstance(entry, tuple) and len(entry) == 2:
        return _lazy_load(entry[0], entry[1])
    return entry

def get_provider_list(category: str, market: str = '') -> list:
    mkt = market or MARKET
    market_cfg = _PROVIDER_REGISTRY.get(mkt, {})
    providers = market_cfg.get(category, [])
    result = []
    for entry in providers:
        if isinstance(entry, tuple) and len(entry) == 2:
            loaded = _lazy_load(entry[0], entry[1])
            result.append((loaded, entry[0]))
        else:
            result.append((entry, ''))
    runtime = _RUNTIME_CLASSES.get(mkt, {}).get(category, [])
    for cls in runtime:
        result.append((cls, cls.__module__ if hasattr(cls, '__module__') else ''))
    return result

def get_market() -> str:
    return MARKET
