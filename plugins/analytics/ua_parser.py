#!/usr/bin/env python3
"""
analytics/ua_parser.py — User-Agent 解析

使用 ua-parser 库（Python 绑定）解析浏览器、操作系统、设备类型。
回退到内置的简单启发式解析。

依赖:
  pip install ua-parser user-agents
"""

import re

# 尝试导入 ua-parser
try:
    from user_agents import parse as ua_parse
    HAS_UA_PARSER = True
except ImportError:
    HAS_UA_PARSER = False

# ─── 设备类型检测（正则） ────────────────────────────────────────────────────

MOBILE_PATTERNS = [
    'mobile', 'iphone', 'ipod', 'android.*mobile', 'blackberry',
    'windows phone', 'opera mini', 'iemobile', 'nokia',
]

TABLET_PATTERNS = [
    'ipad', 'android.*tablet', 'tablet', 'playbook',
    'silk', 'kindle', 'sch-i', 'gt-p',
]

# BOT_PATTERNS — 全模块唯一真源（models.is_bot 也从这里导入）
# 匹配时统一对 UA 做小写化，因此这里全部使用小写形式。
BOT_PATTERNS = [
    'bot', 'crawler', 'spider', 'scrape', 'headless',
    'python-requests', 'python-urllib', 'curl', 'wget',
    'go-http-client', 'okhttp', 'apache-httpclient', 'java/',
    'ahrefs', 'semrush', 'baiduspider', 'googlebot', 'bingbot',
    'twitterbot', 'facebookexternalhit', 'slack', 'discordbot',
    'telegrambot', 'yisouspider', 'bytespider', 'sogou', '360spider',
]


def parse_ua(ua_string: str) -> dict:
    """
    解析 User-Agent 字符串

    返回:
    {
        'browser': 'Chrome',
        'browser_version': '120.0.0.0',
        'os_name': 'Windows 10',
        'device_type': 'desktop' | 'mobile' | 'tablet' | 'bot' | 'unknown',
        'is_bot': False,
    }
    """
    if not ua_string:
        return {
            'browser': '',
            'browser_version': '',
            'os_name': '',
            'device_type': 'unknown',
            'is_bot': False,
        }

    # 使用 ua-parser 库
    if HAS_UA_PARSER:
        try:
            ua = ua_parse(ua_string)
            is_bot = ua.is_bot
            if is_bot:
                return {
                    'browser': ua.browser.family or '',
                    'browser_version': ua.browser.version_string or '',
                    'os_name': ua.os.family or '',
                    'device_type': 'bot',
                    'is_bot': True,
                }

            if ua.is_mobile:
                device_type = 'mobile'
            elif ua.is_tablet:
                device_type = 'tablet'
            elif ua.is_pc:
                device_type = 'desktop'
            else:
                device_type = 'unknown'

            return {
                'browser': ua.browser.family or '',
                'browser_version': ua.browser.version_string or '',
                'os_name': ua.os.family or '',
                'device_type': device_type,
                'is_bot': False,
            }
        except Exception:
            pass

    # 回退：正则启发式
    return _simple_parse(ua_string)


def _simple_parse(ua: str) -> dict:
    """简单启发式 UA 解析（无需第三方库）"""
    ua_lower = ua.lower()

    # 爬虫检测
    for pat in BOT_PATTERNS:
        if pat in ua_lower:
            return {
                'browser': 'Bot',
                'browser_version': '',
                'os_name': '',
                'device_type': 'bot',
                'is_bot': True,
            }

    # 设备类型
    device_type = 'desktop'
    for pat in MOBILE_PATTERNS:
        if pat in ua_lower:
            device_type = 'mobile'
            break
    for pat in TABLET_PATTERNS:
        if pat in ua_lower:
            device_type = 'tablet'
            break

    # 浏览器
    browser = 'Unknown'
    browser_version = ''
    browser_patterns = [
        (r'edg(?:e)?/([\d.]+)', 'Edge'),
        (r'chrome/([\d.]+)', 'Chrome'),
        (r'safari/([\d.]+)', 'Safari'),
        (r'firefox/([\d.]+)', 'Firefox'),
        (r'opr/([\d.]+)', 'Opera'),
        (r'opera.*version/([\d.]+)', 'Opera'),
        (r'microapp/([\d.]+)', 'WeChat'),
        (r'ucbrowser/([\d.]+)', 'UCBrowser'),
        (r'qqbrowser/([\d.]+)', 'QQBrowser'),
    ]
    for pattern, name in browser_patterns:
        m = re.search(pattern, ua_lower)
        if m:
            browser = name
            browser_version = m.group(1)
            break

    # 操作系统
    os_name = 'Unknown'
    if 'windows nt 10' in ua_lower:
        os_name = 'Windows 10'
    elif 'windows nt 11' in ua_lower:
        os_name = 'Windows 11'
    elif 'windows nt 6.3' in ua_lower:
        os_name = 'Windows 8.1'
    elif 'windows nt 6.1' in ua_lower:
        os_name = 'Windows 7'
    elif 'mac os x' in ua_lower:
        m = re.search(r'mac os x ([\d_]+)', ua_lower)
        os_name = f'Mac OS X {m.group(1).replace("_", ".")}' if m else 'Mac OS X'
    elif 'android' in ua_lower:
        m = re.search(r'android ([\d.]+)', ua_lower)
        os_name = f'Android {m.group(1)}' if m else 'Android'
    elif 'iphone' in ua_lower or 'ipad' in ua_lower:
        m = re.search(r'os ([\d_]+)', ua_lower)
        os_name = f'iOS {m.group(1).replace("_", ".")}' if m else 'iOS'
    elif 'linux' in ua_lower:
        os_name = 'Linux'

    return {
        'browser': browser,
        'browser_version': browser_version,
        'os_name': os_name,
        'device_type': device_type,
        'is_bot': False,
    }
