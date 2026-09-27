#!/usr/bin/env python3
"""检索回归评测：对 eval_cases 逐条直驱 MemoryRetriever，算 hit@k / MRR。

零 LLM 成本（只测检索层）；适用于任何 prompt/权重/索引变更前后对比。
"""

import logging

logger = logging.getLogger('memory_engine.eval_harness')

_TOP_K = 5


class EvalHarness:
    def __init__(self, config: dict):
        self._config = config or {}

    def run(self) -> dict:
        """跑全部 active 用例，写 eval_runs，返回本次指标。"""
        from ..models import get_memory_engine_db
        from .retriever import MemoryRetriever
        retriever = MemoryRetriever(self._config)
        conn = get_memory_engine_db()
        try:
            cases = conn.execute(
                "SELECT id, name, owner_id, agent_id, query,"
                " expected_memory_id, expected_keywords"
                " FROM eval_cases WHERE active = TRUE ORDER BY created_at"
            ).fetchall()
            hits = 0
            rr_sum = 0.0
            failures = []
            for c in cases:
                rank = self._rank(retriever, c)
                if rank is not None:
                    hits += 1
                    rr_sum += 1.0 / rank
                else:
                    failures.append({'case': c['name'], 'query': c['query'][:80]})
            n = len(cases)
            hit_at_k = hits / n if n else 0.0
            mrr = rr_sum / n if n else 0.0
            conn.execute(
                "INSERT INTO eval_runs (case_count, hit_at_k, mrr, metrics)"
                " VALUES (?, ?, ?, ?::jsonb)",
                (n, hit_at_k, mrr, __import__('json').dumps({
                    'failures': failures[:20], 'top_k': _TOP_K})),
            )
            conn.commit()
            return {'case_count': n, 'hit_at_k': round(hit_at_k, 4),
                    'mrr': round(mrr, 4), 'failures': len(failures)}
        finally:
            conn.close()

    @staticmethod
    def _rank(retriever, case) -> "int | None":
        """期望记忆在 top-k 的排名；不在则按关键词兜底判命中；未命中返回 None。"""
        rows = retriever.retrieve(case['owner_id'], case['agent_id'],
                                  case['query'], top_k=_TOP_K)
        expected_id = str(case['expected_memory_id'] or '')
        kws = case['expected_keywords'] or []
        for i, r in enumerate(rows, start=1):
            if expected_id and str(r['id']) == expected_id:
                return i
            if not expected_id and kws:
                content = str(r['content'])
                if any(k in content for k in kws):
                    return i
        return None
