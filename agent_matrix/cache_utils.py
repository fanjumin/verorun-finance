#!/usr/bin/env python3
"""
Cache Store — file-based KV cache for LLM responses, session summaries, etc.

Location: .cache/ under APP_HOME
All data is rebuildable — safe to delete the entire .cache/ directory.
"""

import os
import json
import time
import hashlib
import threading
from pathlib import Path

# ── Path resolution ─────────────────────────────────────────────────────

def _get_cache_root() -> Path:
    """Resolve .cache/ directory relative to project root."""
    root = os.environ.get('APP_HOME', '')
    if not root:
        # Fallback: walk up from agent_matrix/ to project root
        root = Path(__file__).resolve().parent.parent
    return Path(root) / '.cache'


CACHE_ROOT = _get_cache_root()
CACHE_MAX_FILES = 5000        # Hard limit: max cache files
CACHE_MAX_SIZE_MB = 1024     # Hard limit: 1 GB


class CacheStore:
    """Base class for file-based caches.

    Subclass and override _subdir(), _serialize(), _deserialize().
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subdir_path = CACHE_ROOT / self._subdir()
        self._subdir_path.mkdir(parents=True, exist_ok=True)

    def _subdir(self) -> str:
        raise NotImplementedError

    def _serialize(self, data: dict) -> str:
        return json.dumps(data, ensure_ascii=False)

    def _deserialize(self, raw: str) -> dict:
        return json.loads(raw)

    def get(self, key: str) -> dict | None:
        """Read cache entry. Returns None if missing or expired."""
        # P1-F09: 防路径穿越 — 缓存键禁止包含路径分隔符
        if '/' in key or '\\' in key or '..' in key:
            return None
        fpath = self._subdir_path / f'{key}.json'
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            ttl = data.get('ttl', 0)
            created = data.get('created_at', 0)
            if ttl > 0 and time.time() - created > ttl:
                os.remove(fpath)
                return None
            return data
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return None

    def set(self, key: str, data: dict):
        """Write cache entry."""
        # P1-F09: 防路径穿越 — 缓存键禁止包含路径分隔符
        if '/' in key or '\\' in key or '..' in key:
            return
        data.setdefault('created_at', time.time())
        fpath = self._subdir_path / f'{key}.json'
        with self._lock:
            self._enforce_limits()
            # Atomic write: write to temp, then rename
            tmp = fpath.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, fpath)

    def delete(self, key: str):
        """Delete a cache entry."""
        fpath = self._subdir_path / f'{key}.json'
        try:
            os.remove(fpath)
        except FileNotFoundError:
            pass

    def cleanup(self):
        """Remove all expired entries."""
        now = time.time()
        removed = 0
        for fpath in self._subdir_path.glob('*.json'):
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                ttl = data.get('ttl', 0)
                created = data.get('created_at', 0)
                if ttl > 0 and now - created > ttl:
                    os.remove(fpath)
                    removed += 1
            except (json.JSONDecodeError, OSError):
                os.remove(fpath)
                removed += 1
        return removed

    def flush(self):
        """Delete all cache entries in this subdir."""
        for fpath in self._subdir_path.glob('*.json'):
            try:
                os.remove(fpath)
            except OSError:
                pass

    def _enforce_limits(self):
        """Enforce max file count and total size."""
        files = sorted(
            self._subdir_path.glob('*.json'),
            key=lambda p: p.stat().st_mtime
        )
        while len(files) > CACHE_MAX_FILES:
            try:
                os.remove(files.pop(0))
            except OSError:
                pass
        total_size = sum(f.stat().st_size for f in files if f.exists())
        max_bytes = CACHE_MAX_SIZE_MB * 1024 * 1024
        while total_size > max_bytes and files:
            try:
                total_size -= files[0].stat().st_size
                os.remove(files.pop(0))
            except OSError:
                pass


# ── LLM Response Cache ───────────────────────────────────────────────────

class LLMResponseCache(CacheStore):
    """Cache for deterministic (temperature=0) LLM responses."""

    DEFAULT_TTL = 3600  # 1 hour

    def _subdir(self) -> str:
        return 'llm'

    @staticmethod
    def make_key(model: str, system_prompt: str, user_message: str,
                 history: list | None = None) -> str:
        """Generate MD5 cache key from input parameters."""
        parts = [model, system_prompt, user_message]
        if history:
            parts.append(json.dumps(history, sort_keys=True, ensure_ascii=False))
        raw = '|'.join(parts)
        return hashlib.md5(raw.encode('utf-8')).hexdigest()

    def get_response(self, model: str, system_prompt: str,
                     user_message: str, history: list | None = None) -> str | None:
        """Get cached LLM response. Returns None if not found or expired."""
        key = self.make_key(model, system_prompt, user_message, history)
        entry = self.get(key)
        if entry:
            return entry.get('response')
        return None

    def set_response(self, model: str, system_prompt: str,
                     user_message: str, response: str,
                     tokens_used: int = 0, history: list | None = None,
                     ttl: int | None = None):
        """Cache an LLM response."""
        key = self.make_key(model, system_prompt, user_message, history)
        self.set(key, {
            'model': model,
            'response': response,
            'tokens_used': tokens_used,
            'ttl': ttl if ttl is not None else self.DEFAULT_TTL,
        })


# ── Session Summary Cache ────────────────────────────────────────────────

class SessionSummaryStore(CacheStore):
    """Cache for compressed conversation history summaries."""

    STALE_THRESHOLD = 4  # Regenerate after 4 new messages

    def _subdir(self) -> str:
        return 'sessions'

    def get_summary(self, session_id: str, current_message_count: int) -> str | None:
        """Get cached summary if it's still fresh enough."""
        entry = self.get(session_id)
        if not entry:
            return None
        if current_message_count - entry.get('message_count', 0) >= self.STALE_THRESHOLD:
            return None
        return entry.get('summary')

    def set_summary(self, session_id: str, summary: str,
                    message_count: int, message_range: tuple,
                    model: str = ''):
        """Cache a session summary."""
        self.set(session_id, {
            'summary': summary,
            'message_count': message_count,
            'message_range': list(message_range),
            'model': model,
            'ttl': 0,  # No TTL — valid as long as session exists
        })


# ── Global instances (lazy init) ──────────────────────────────────────────

_llm_cache: LLMResponseCache | None = None
_summary_store: SessionSummaryStore | None = None


def get_llm_cache() -> LLMResponseCache:
    global _llm_cache
    if _llm_cache is None:
        _llm_cache = LLMResponseCache()
    return _llm_cache


def get_summary_store() -> SessionSummaryStore:
    global _summary_store
    if _summary_store is None:
        _summary_store = SessionSummaryStore()
    return _summary_store
