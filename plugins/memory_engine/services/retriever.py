#!/usr/bin/env python3
"""Read pipeline: hybrid retrieval that assembles a safe memory block."""

import logging
import re

logger = logging.getLogger('memory_engine.retriever')


class MemoryRetriever:
    """Retrieve relevant memories for a user + agent, scoped and size-limited."""

    def __init__(self, config: dict):
        self._config = config or {}
        self._embedder = None  # lazy-init

    @property
    def _embed(self):
        if self._embedder is None:
            from .embedding import EmbeddingService
            self._embedder = EmbeddingService(self._config)
        return self._embedder

    def retrieve(self, user_id: str, agent_id: str, query: str, top_k: int = None) -> list:
        """Return list of memory rows, newest first, already scoped to the user."""
        top_k = top_k or int(self._config.get('top_k', 5))
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            use_global = self._config.get('allow_global_memory', False)
            recency_expr = ("1.0 / (1.0 + extract(epoch FROM now() - "
                            "COALESCE(last_hit_at, updated_at)) / 86400.0)")
            params = [user_id, use_global, top_k]
            vec = self._embed.embed(query) if self._embed.is_ready() else None
            if vec:
                vector_literal = '[' + ','.join(repr(v) for v in vec) + ']'
                rows = conn.execute(
                    "SELECT id, memory_type, content, keywords,"
                    " quality_score, importance, " + recency_expr + " AS recency,"
                    " 1 - (embedding <=> ?::vector) AS sim"
                    " FROM memories"
                    " WHERE status = 'active'"
                    " AND quality_score >= 0.3"
                    " AND ((owner_type = 'user' AND owner_id = ?)"
                    " OR (owner_type = 'global' AND ? = TRUE))"
                    " AND embedding IS NOT NULL"
                    " ORDER BY (0.6*sim + 0.3*quality_score + 0.1*recency) DESC"
                    " LIMIT ?",
                    [vector_literal] + params,
                ).fetchall()
                if not rows:
                    rows = self._keyword_rows(conn, query, params)
            else:
                rows = self._keyword_rows(conn, query, params)
            self._bump(conn, rows)
            conn.commit()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error('retrieve failed: %s', e)
            conn.rollback()
            return []
        finally:
            conn.close()

    def _keyword_rows(self, conn, query: str, params: list):
        """Keyword fallback: content ILIKE or keyword overlap."""
        tokens = [t for t in re.split(r'\W+', query) if len(t) >= 2][:6]
        if not tokens:
            return []
        like = ' OR '.join('content ILIKE ?' for _ in tokens)
        recency_expr = ("1.0 / (1.0 + extract(epoch FROM now() - "
                        "COALESCE(last_hit_at, updated_at)) / 86400.0)")
        return conn.execute(
            "SELECT id, memory_type, content, keywords,"
            " quality_score, importance, " + recency_expr + " AS recency, 0.0 AS sim"
            " FROM memories"
            " WHERE status = 'active'"
            " AND quality_score >= 0.3"
            " AND ((owner_type = 'user' AND owner_id = ?)"
            " OR (owner_type = 'global' AND ? = TRUE))"
            " AND (" + like + ")"
            " ORDER BY (0.6*quality_score + 0.4*recency) DESC"
            " LIMIT ?",
            params + ['%' + t + '%' for t in tokens] + [params[2]],
        ).fetchall()

    def _bump(self, conn, rows):
        """Update hit metrics for retrieved memories."""
        for r in rows:
            conn.execute(
                "UPDATE memories"
                " SET hit_count = hit_count + 1,"
                " last_hit_at = now(),"
                " quality_score = LEAST(1.0, quality_score + 0.01)"
                " WHERE id = ?",
                (r['id'],),
            )

    def build_injection_block(self, user_id: str, agent_id: str, query: str) -> str:
        """Assemble the text block injected into the system prompt.

        Guard rails: hard char cap, single-line rendering, no raw user data.
        """
        rows = self.retrieve(user_id, agent_id, query)
        if not rows:
            return ''
        cap = int(self._config.get('max_memory_block_len', 1200))
        lines = []
        for r in rows:
            label = r['memory_type'].upper()
            text = str(r['content']).replace('\n', ' ').strip()
            lines.append(f"- [{label}] {text[:200]}")
            if sum(len(l) for l in lines) > cap:
                lines = lines[:-1]
                break
        return '\n'.join(lines)
