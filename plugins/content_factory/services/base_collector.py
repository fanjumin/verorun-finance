#!/usr/bin/env python3
"""Content Factory Plugin — Base Collector + 去重工具"""
import hashlib, json, random, time, re
from typing import List, Tuple
from difflib import SequenceMatcher


def content_hash(text: str) -> str:
    return hashlib.sha256((text or '')[:5000].encode('utf-8')).hexdigest()


def title_similar(t1: str, t2: str, threshold: float = 0.80) -> bool:
    if not t1 or not t2:
        return False
    return SequenceMatcher(None, t1.strip().lower(), t2.strip().lower()).ratio() >= threshold


class CollectResult:
    def __init__(self, **kw):
        self.title = (kw.get('title') or '')[:200]
        self.content_text = (kw.get('content_text') or '')[:50000]
        self.source_url = kw.get('source_url') or ''
        self.author = kw.get('author') or ''
        self.summary = (kw.get('summary') or '')[:500]
        self.content_html = kw.get('content_html') or self.content_text
        self.publish_time = kw.get('publish_time') or ''
        self.tags = kw.get('tags') or ''
        self.content_json = json.dumps(kw.get('content_json') or {}, ensure_ascii=False)
        self.content_hash = content_hash(self.content_text)

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items()}


class BaseCollector:
    """采集器基类"""
    name = 'base'
    source_type = 'rss'

    def __init__(self, source_id: int, config: dict = None):
        self.source_id = source_id
        self.config = config or {}
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
        ]

    def _random_ua(self) -> str:
        return random.choice(self.user_agents)

    def _random_delay(self, lo=1.0, hi=3.0):
        time.sleep(random.uniform(lo, hi))

    def _headers(self, referer: str = 'https://www.baidu.com/') -> dict:
        return {
            'User-Agent': self._random_ua(),
            'Accept': 'text/html,application/json,*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': referer,
        }

    @staticmethod
    def _strip_html(html: str) -> str:
        if not html:
            return ''
        return re.sub(r'<[^>]+>', '', html).replace('&nbsp;', ' ').replace('&amp;', '&').strip()

    def _is_duplicate(self, conn, title: str, c_hash: str) -> Tuple[bool, str]:
        row = conn.execute('SELECT id FROM raw_contents WHERE content_hash=?', (c_hash,)).fetchone()
        if row:
            return True, 'exact_hash'
        recent = conn.execute('SELECT title FROM raw_contents ORDER BY id DESC LIMIT 100').fetchall()
        for r in recent:
            if title_similar(title, r['title']):
                return True, 'similar_title'
        return False, ''

    def save_results(self, results: List[CollectResult], task_id: int = 0) -> Tuple[int, int]:
        from plugins.content_factory.models import get_cf_db
        with get_cf_db() as conn:
            inserted = 0
            skipped = 0
            for r in results:
                dup, why = self._is_duplicate(conn, r.title, r.content_hash)
                if dup:
                    skipped += 1
                    continue
                conn.execute(
                    """INSERT INTO raw_contents (source_id, task_id, title, author,
                       source_url, content_text, content_html, summary, content_hash,
                       publish_time, tags, content_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (self.source_id, task_id or None, r.title, r.author,
                     r.source_url, r.content_text, r.content_html, r.summary,
                     r.content_hash, r.publish_time, r.tags, r.content_json)
                )
                inserted += 1
            conn.commit()
            return inserted, skipped

    def collect(self, **kwargs) -> List[CollectResult]:
        raise NotImplementedError