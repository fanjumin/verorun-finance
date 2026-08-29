#!/usr/bin/env python3
"""Content Factory Plugin — RSS/Atom 通用采集器"""
import ipaddress
import re
import socket
from datetime import datetime
from typing import List
from urllib.parse import urlparse
from i18n import _
from plugins.content_factory.services.base_collector import BaseCollector, CollectResult

try:
    import feedparser
except ImportError:
    feedparser = None

# §11.3: 禁止访问内网/保留 IP 段（防 SSRF）
_BLOCKED_RANGES = (
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('169.254.0.0/16'),
    ipaddress.ip_network('0.0.0.0/8'),
)


def _is_safe_url(url: str) -> bool:
    """校验 RSS 源 URL 不指向内网/保留 IP，防止 SSRF。"""
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        infos = socket.getaddrinfo(host, None)
        if not infos:
            return False
        for info in infos:
            addr = ipaddress.ip_address(info[4][0])
            if any(addr in net for net in _BLOCKED_RANGES):
                return False
    except Exception:
        return False
    return True


class RSSCollector(BaseCollector):
    name = 'rss'
    source_type = 'rss'

    def collect(self, **kwargs) -> List[CollectResult]:
        if feedparser is None:
            raise ImportError(_("请先 pip install feedparser"))

        url = kwargs.get('url') or self.config.get('url', '')
        if not url:
            return []
        if not _is_safe_url(url):
            raise ValueError(_("RSS source URL blocked by security policy (intranet/reserved IP)"))

        feed = feedparser.parse(url, agent=self._random_ua())
        if not feed.entries:
            return []

        results = []
        limit = kwargs.get('count') or self.config.get('max_per_run', 10)

        for entry in feed.entries[:limit]:
            content_text = ''
            content_html = ''
            if hasattr(entry, 'content') and entry.content:
                raw = entry.content[0].get('value', '')
                content_html = raw
                content_text = self._strip_html(raw)
            elif hasattr(entry, 'summary'):
                raw = entry.summary or ''
                content_html = raw
                content_text = self._strip_html(raw)
            elif hasattr(entry, 'description'):
                raw = entry.description or ''
                content_html = raw
                content_text = self._strip_html(raw)

            cover_url = ''
            if content_html:
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content_html)
                if m: cover_url = m.group(1)

            pub_time = ''
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                pub_time = datetime(*entry.published_parsed[:6]).strftime('%Y-%m-%d %H:%M:%S')
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                pub_time = datetime(*entry.updated_parsed[:6]).strftime('%Y-%m-%d %H:%M:%S')

            tags = []
            if hasattr(entry, 'tags'):
                tags = [t.get('term', '') for t in entry.tags if t.get('term')]
            tags_str = ','.join(tags)

            results.append(CollectResult(
                title=getattr(entry, 'title', ''),
                content_text=content_text, content_html=content_html,
                source_url=entry.get('link', ''),
                author=getattr(entry, 'author', ''),
                publish_time=pub_time, summary=content_text[:300],
                tags=tags_str,
                content_json={'cover_url': cover_url},
            ))

        return results