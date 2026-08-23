#!/usr/bin/env python3
"""Write pipeline: turn completed task traces into durable memories."""

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger('memory_engine.extractor')

# PII guard: never persist secrets or personal identifiers.
_PII_PATTERNS = [
    re.compile(r'(?i)(password|api[_-]?key|secret|token)\s*[:=]\s*\S+'),
    re.compile(r'\b1[3-9]\d{9}\b'),                    # CN mobile
    re.compile(r'\b\d{17}[\dXx]\b'),                   # CN ID card
]

_SKIP_MARKERS = ('hello', 'hi', 'thanks', 'thank you')


class MemoryExtractor:
    """Extract memory candidates from completed agent tasks."""

    def __init__(self, config: dict):
        self._config = config or {}
        self._embedder = None  # lazy-init from services.embedding
        self._pool = ThreadPoolExecutor(max_workers=2)

    @property
    def _embed(self):
        if self._embedder is None:
            from .embedding import EmbeddingService
            self._embedder = EmbeddingService(self._config)
        return self._embedder

    def submit(self, task: dict, result: dict, agent_id: str):
        """Fire-and-forget extraction; never blocks the request thread."""
        if not self._config.get('enable_auto_extract', True):
            return
        if not self._within_daily_budget():
            return
        self._pool.submit(self._extract, task, result, agent_id)

    def _within_daily_budget(self) -> bool:
        """Respect the configured daily extraction cap (config-level guard)."""
        cap = int(self._config.get('daily_extract_budget', 200))
        if cap <= 0:
            return False
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM reflexion_logs"
                " WHERE trigger = 'task_completed'"
                " AND created_at > CURRENT_DATE"
            ).fetchone()
            return (row['n'] or 0) < cap
        finally:
            conn.close()

    def _should_extract(self, task: dict, result: dict) -> bool:
        """Cheap heuristics first; only promising traces reach the LLM."""
        query = str(task.get('user_query') or task.get('query') or '')
        if len(query) < 4 or query.lower() in _SKIP_MARKERS:
            return False
        if result.get('failed') and not result.get('retries'):
            return False  # failure without retry is handled by reflexion
        return True

    def _extract(self, task: dict, result: dict, agent_id: str):
        """Run the curator agent in extract mode, then persist candidates."""
        try:
            if not self._should_extract(task, result):
                return
            agent_config = self._load_curator_config()
            if not agent_config:
                return
            from agent_matrix.agent_runner import AgentRunner
            runner = AgentRunner(agent_config)
            transcript = {
                'query': task.get('user_query') or task.get('query'),
                'result': str(result)[:2000],
            }
            resp = runner.execute({
                'title': 'Memory Extract',
                'description': json.dumps(transcript, ensure_ascii=False),
            })
            candidates = self._parse_candidates(resp)
            self._persist(candidates, agent_id, task, source='auto')
        except Exception as e:
            logger.error('extraction failed: %s', e)

    def _parse_candidates(self, resp) -> list:
        """Parse the curator JSON output defensively; malformed output yields [].

        Runner result structure: (result_text, retries, logs).
        """
        text = ''
        if isinstance(resp, tuple) and resp:
            text = str(resp[0])
        elif isinstance(resp, dict):
            text = str(resp.get('response') or resp.get('result') or resp.get('output') or '')
        try:
            data = json.loads(text)
            items = data.get('memories') or []
        except (ValueError, AttributeError):
            return []
        out = []
        for it in items:
            content = str(it.get('content', '')).strip()
            if not content or self._contains_pii(content):
                continue
            out.append({
                'type': it.get('type', 'fact'),
                'content': content[:500],
                'confidence': float(it.get('confidence', 0.5)),
            })
        return out

    @staticmethod
    def _contains_pii(text: str) -> bool:
        return any(p.search(text) for p in _PII_PATTERNS)

    def _load_curator_config(self) -> dict:
        """Load the memory_curator agent row (agent_matrix, registered via declare_roles)."""
        from agent_matrix.models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM agent_matrix"
                " WHERE slug = 'memory_curator' AND is_active = 1"
            ).fetchone()
        return dict(row) if row else {}

    def _persist(self, candidates: list, agent_id: str, task: dict, source: str):
        """Insert with content-hash idempotency and per-owner caps."""
        if not candidates:
            return
        owner_id = str(task.get('user_id') or '')
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            for c in candidates:
                digest = hashlib.sha256(
                    f"{owner_id}|{c['content']}".encode('utf-8')
                ).hexdigest()
                existing = conn.execute(
                    "SELECT id FROM memories WHERE content_hash = ?", (digest,)
                ).fetchone()
                if existing:
                    continue
                vec = self._embed.embed(c['content'])
                embedding_literal = None
                if vec:
                    embedding_literal = '[' + ','.join(repr(v) for v in vec) + ']'
                conn.execute(
                    "INSERT INTO memories"
                    " (owner_type, owner_id, agent_id, memory_type, content,"
                    "  keywords, embedding, confidence, source, content_hash, meta)"
                    " VALUES ('user', ?, ?, ?, ?, ?, ?::vector, ?, ?, ?, ?::jsonb)"
                    " ON CONFLICT (content_hash) DO NOTHING",
                    (owner_id, agent_id, c['type'], c['content'],
                     self._keywords(c['content']), embedding_literal, c['confidence'],
                     source, digest, json.dumps({'task_id': task.get('task_id')})),
                )
            self._enforce_owner_cap(conn, owner_id)
            conn.commit()
        except Exception as e:
            logger.error('persist failed: %s', e)
            conn.rollback()
        finally:
            conn.close()

    @staticmethod
    def _keywords(text: str) -> list:
        """Naive keyword extraction: CJK bigrams + 2+ char latin tokens."""
        kws = set(re.findall(r'[\u4e00-\u9fff]{2,}', text))
        kws.update(w.lower() for w in re.findall(r'[a-z]{2,}', text.lower()))
        return list(kws)[:12]

    def _enforce_owner_cap(self, conn, owner_id: str):
        """Archive oldest auto memories beyond max_memories_per_owner."""
        cap = int(self._config.get('max_memories_per_owner', 500))
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM memories"
            " WHERE owner_type = 'user' AND owner_id = ? AND status = 'active'",
            (owner_id,),
        ).fetchone()
        excess = (row['n'] or 0) - cap
        if excess <= 0:
            return
        conn.execute(
            "UPDATE memories SET status = 'archived'"
            " WHERE id IN ("
            " SELECT id FROM memories"
            " WHERE owner_type = 'user' AND owner_id = ? AND status = 'active'"
            " ORDER BY quality_score ASC, updated_at ASC LIMIT ?)",
            (owner_id, excess),
        )
