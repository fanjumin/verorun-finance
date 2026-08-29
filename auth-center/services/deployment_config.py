"""VeroRun 维洛智能 部署配置中心

所有域名、邮箱、品牌名称的集中配置点。
客户部署时只需设置环境变量，代码中不允许出现域名硬编码。

优先级: 环境变量 > 默认值
"""
import os


class DeployConfig:
    """部署配置 — 所有域名/邮箱/品牌统一从这里读取"""

    # ── 市场与语言配置 ──
    MARKET = os.environ.get('DEPLOY_MARKET', 'cn')
    LANG = os.environ.get('DEPLOY_LANG', 'en')
    CURRENCY = os.environ.get('DEPLOY_CURRENCY', 'CNY')  # CNY / USD / EUR

    # ── 域名配置（必须通过环境变量设置）──
    DOMAIN = os.environ.get('DEPLOY_DOMAIN', 'localhost')
    PROTOCOL = os.environ.get('DEPLOY_PROTOCOL', 'https')
    EMAIL_DOMAIN = os.environ.get('DEPLOY_EMAIL_DOMAIN', os.environ.get('DEPLOY_DOMAIN', ''))
    BRAND = os.environ.get('DEPLOY_BRAND', '')

    # ── No-domain path mode: subdomain → path prefix mapping ──
    _PATH_MAP = {
        'platform': '/auth',
        'agent': '/admin',
        'bot': '/bot',
        'tm': '/tm',
    }

    @classmethod
    def url(cls, subdomain=''):
        """Full URL: your-domain.com

        In no-domain mode (DOMAIN empty or 'localhost'), the subdomain
        is converted to a path prefix instead of an invalid subdomain URL,
        e.g. url('platform') → /auth
        """
        # No-domain mode: path prefix replaces subdomain
        if not cls.DOMAIN or cls.DOMAIN == 'localhost':
            base = f"{cls.PROTOCOL}://{cls.DOMAIN}" if cls.DOMAIN else ''
            if subdomain and subdomain in cls._PATH_MAP:
                return f"{base}{cls._PATH_MAP[subdomain]}"
            return base or '/'
        # Domain mode: original logic unchanged
        if subdomain:
            return f"{cls.PROTOCOL}://{subdomain}.{cls.DOMAIN}"
        return f"{cls.PROTOCOL}://{cls.DOMAIN}"

    @classmethod
    def email(cls, prefix='support'):
        """邮箱: deploy.email('support')"""
        return f"{prefix}@{cls.EMAIL_DOMAIN}"

    @classmethod
    def server_name(cls, subdomain=''):
        """nginx server_name: your-domain.com"""
        if subdomain:
            return f"{subdomain}.{cls.DOMAIN}"
        return cls.DOMAIN

    @classmethod
    def to_dict(cls):
        """注入模板用：返回所有常用值"""
        return {
            'market': cls.MARKET,
            'lang': cls.LANG,
            'currency': cls.CURRENCY,
            'domain': cls.DOMAIN,
            'protocol': cls.PROTOCOL,
            'email_domain': cls.EMAIL_DOMAIN,
            'brand': cls.BRAND,
            'url': cls.url(),
            'url_tm': cls.url('tm'),
            'url_platform': cls.url('platform'),
            'url_agent': cls.url('agent'),
            'url_bot': cls.url('bot'),
            'email_support': cls.email('support'),
            'email_postmaster': cls.email('postmaster'),
            'email_hi': cls.email('hi'),
            'server_name': cls.server_name(),
            'server_name_platform': cls.server_name('platform'),
            'server_name_agent': cls.server_name('agent'),
            'server_name_bot': cls.server_name('bot'),
        }


# 快捷引用
deploy = DeployConfig
