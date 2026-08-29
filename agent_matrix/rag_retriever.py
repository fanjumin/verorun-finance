#!/usr/bin/env python3
"""agent_matrix/rag_retriever.py — 全局知识库混合检索服务（RAG）。

对齐 Dify/RAGFlow 标准三段式检索：
  1. 向量路：EmbeddingService → pgvector 余弦相似度（embedding 可用时）
  2. 关键词路：pg_trgm 相似度 + 中文双字组合/字符重叠评分（保留原 _rag_search 算法）
  3. RRF（k=60）融合 → 可选 rerank 精排（开关默认关闭，二期接入）

全链路优雅降级：
  - embedding 模型未配置 / pgvector 扩展缺失 / 向量路异常 → 纯关键词路
  - 关键词路异常 → 返回 []（与原 _rag_search 行为一致）

返回结构兼容 api_v1._rag_search 的调用方：
  [{id, title, content, keywords, category, score}]
"""

import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger('agent_matrix.rag_retriever')

_RRF_K = 60


class RagRetriever:
    """全局 knowledge_blocks 混合检索器。"""

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._embedder = None

    # -- 内核/embedding 访问 -------------------------------------------------

    @property
    def _embed(self):
        if self._embedder is None:
            from plugins._base.embeddings import EmbeddingService
            self._embedder = EmbeddingService(self._config)
        return self._embedder

    @staticmethod
    def _get_db():
        """复用 agent_matrix 的 PG 连接（auth-center/models/database.get_db）。"""
        from agent_matrix.models import get_db
        return get_db()

    # -- 主检索入口 ----------------------------------------------------------

    def retrieve(self, query: str, top_k: int = 5,
                 category: str = None, scope: str = None) -> list:
        """混合检索 knowledge_blocks，返回按融合分降序的片段列表。

        Args:
            query: 查询文本
            top_k: 返回条数（上限 20，与原 _rag_search 一致）
            category: 可选，仅检索指定分类
            scope: 可选 'system' | 'user'，None 检索全部
        """
        query = (query or '').strip()
        if not query:
            return []

        top_k = min(top_k or 5, 20)
        vec_rows, kw_rows = [], []

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_kw = pool.submit(self._keyword_search, query, top_k, category, scope)
                f_vec = None
                try:
                    if self._embed.is_ready():
                        f_vec = pool.submit(self._vector_search, query, top_k,
                                            category, scope)
                except Exception as e:
                    logger.warning('vector path disabled: %s', e)
                kw_rows = f_kw.result()
                if f_vec is not None:
                    vec_rows = f_vec.result()
        except Exception as e:
            logger.error('hybrid search pool failed: %s', e)
            return []

        if not vec_rows and not kw_rows:
            return []

        results = self._rrf_fusion(vec_rows, kw_rows, top_k)

        # 可选 rerank 精排（二期接入，默认关闭）
        rerank_model = (self._config.get('rerank_model') or '').strip()
        if rerank_model and results:
            # TODO(二期): 调用 UnifiedLLM 或专用 reranker API 对 results 重排。
            # 目前仅打日志，不改变结果顺序，保持主链路稳定。
            logger.info('rerank_model configured (%s) but reranker not implemented yet; skip',
                        rerank_model)

        return results

    # -- 向量路 ---------------------------------------------------------------

    def _vector_search(self, query: str, top_k: int,
                       category: str, scope: str) -> list:
        """向量路：pgvector 余弦距离。embedding 不可用/失败时返回 []。"""
        try:
            vec = self._embed.embed(query)
            if not vec:
                return []
            vec_literal = '[' + ','.join(repr(float(v)) for v in vec) + ']'
            sql = ("SELECT id, title, content, keywords, category,"
                   " 1 - (embedding <=> %s::vector) AS similarity"
                   " FROM knowledge_blocks"
                   " WHERE deleted_at IS NULL AND embedding IS NOT NULL")
            params = [vec_literal]
            if scope:
                sql += " AND scope=%s"
                params.append(scope)
            if category:
                sql += " AND category=%s"
                params.append(category)
            sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
            params += [vec_literal, top_k * 2]
            with self._get_db() as conn:
                rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            return rows
        except Exception as e:
            logger.warning('vector search failed (fallback to keyword only): %s', e)
            return []

    # -- 关键词路 -------------------------------------------------------------

    def _keyword_search(self, query: str, top_k: int,
                        category: str, scope: str) -> list:
        """关键词路：pg_trgm 相似度 + 中文双字组合/字符重叠评分。

        pg_trgm 扩展缺失时自动回退为不含 trgm 的查询，保证与
        原 _rag_search 相同的行为（关键词评分永远可用）。
        """
        try:
            sql = ("SELECT id, title, content, keywords, category,"
                   " GREATEST(similarity(content, %s), similarity(title, %s)) AS trgm_sim"
                   " FROM knowledge_blocks"
                   " WHERE deleted_at IS NULL")
            params = [query, query]
            if scope:
                sql += " AND scope=%s"
                params.append(scope)
            if category:
                sql += " AND category=%s"
                params.append(category)
            sql += " ORDER BY priority DESC, quality_score DESC"
            with self._get_db() as conn:
                blocks = [dict(r) for r in conn.execute(sql, params).fetchall()]
        except Exception as e:
            logger.warning('keyword search w/ pg_trgm failed (fallback to plain): %s', e)
            try:
                sql = ("SELECT id, title, content, keywords, category, 0.0 AS trgm_sim"
                       " FROM knowledge_blocks"
                       " WHERE deleted_at IS NULL")
                params = []
                if scope:
                    sql += " AND scope=%s"
                    params.append(scope)
                if category:
                    sql += " AND category=%s"
                    params.append(category)
                sql += " ORDER BY priority DESC, quality_score DESC"
                with self._get_db() as conn:
                    blocks = [dict(r) for r in conn.execute(sql, params).fetchall()]
            except Exception as e2:
                logger.warning('keyword search failed: %s', e2)
                return []

        query_c = query.replace(' ', '')
        chars = list(query_c)
        bigrams = [query_c[i:i + 2] for i in range(len(query_c) - 1)]
        search_terms = set(chars + bigrams)

        results = []
        for block in blocks:
            score = 0.0
            keywords = (block['keywords'] or '').split(',')
            content = block['content'] or ''
            title = block['title'] or ''

            # 原算法：关键词命中 + 字符/二元组重叠 + 精确匹配
            kw_matches = sum(1 for kw in keywords if kw and kw in query_c)
            if kw_matches > 0:
                score += min(kw_matches / max(len(keywords), 1), 1.0) * 0.6

            content_chars = set(content)
            title_chars = set(title)
            char_overlap = len(search_terms & content_chars) / max(len(search_terms), 1)
            title_overlap = len(search_terms & title_chars) / max(len(search_terms), 1)
            score += char_overlap * 0.25 + title_overlap * 0.15

            if query_c in content:
                score += 0.3
            if query_c in title:
                score += 0.2

            # pg_trgm 相似度加分（0.3 权重，作为第 4 路信号）
            trgm_sim = block.get('trgm_sim') or 0.0
            score += float(trgm_sim) * 0.3

            if score > 0:
                results.append({
                    'id': block['id'],
                    'title': title,
                    'content': content,
                    'keywords': block['keywords'] or '',
                    'category': block['category'],
                    'score': round(score, 4),
                    'hits': 1 if trgm_sim > 0 else 0,
                })

        results.sort(key=lambda x: -x['score'])
        return results[:top_k * 2]

    # -- RRF 融合 -------------------------------------------------------------

    @classmethod
    def _rrf_fusion(cls, vector_rows: list, keyword_rows: list,
                    top_k: int, k: int = _RRF_K) -> list:
        """Reciprocal Rank Fusion，按融合分降序取 top_k。"""
        scores = {}
        for rank, r in enumerate(vector_rows, start=1):
            e = scores.setdefault(r['id'], {
                'row': r, 'score': 0.0,
                'similarity': r.get('similarity', 0.0),
            })
            e['score'] += 1.0 / (k + rank)
        for rank, r in enumerate(keyword_rows, start=1):
            e = scores.setdefault(r['id'], {
                'row': r, 'score': 0.0,
                'similarity': r.get('similarity', 0.0),
            })
            e['score'] += 1.0 / (k + rank)

        fused = sorted(scores.values(), key=lambda e: e['score'],
                       reverse=True)[:top_k]
        results = []
        for e in fused:
            row = dict(e['row'])
            row['score'] = round(e['score'], 6)
            row['similarity'] = e['similarity']
            results.append(row)
        return results


# ── 便捷函数：与 api_v1._rag_search 签名兼容 -----------------------------
_retriever = None


def rag_search(query: str, top_k: int = 5,
               category: str = None, scope: str = None) -> list:
    """全局混合检索便捷入口（模块级单例，兼容原 _rag_search 调用方）。"""
    global _retriever
    if _retriever is None:
        _retriever = RagRetriever()
    return _retriever.retrieve(query, top_k=top_k, category=category, scope=scope)


# ── 入库埋点：生成并写入 embedding（幂等，失败静默降级） ------------------

def store_embedding(kb_id: str, title: str, content: str) -> bool:
    """为一条知识块生成 embedding 并写回 knowledge_blocks.embedding。

    - embedding 模型不可用 / 调用失败 / 写入失败 → 返回 False（不抛异常），
      向量路自动跳过该条，关键词路不受影响。
    - 幂等：可对同一 kb_id 重复调用，重复写入相同向量。
    """
    try:
        from plugins._base.embeddings import EmbeddingService
        embedder = EmbeddingService()
        if not embedder.is_ready():
            return False
        text = f"{title or ''}\n{content or ''}".strip()
        if not text:
            return False
        vec = embedder.embed(text)
        if not vec:
            return False
        vec_literal = '[' + ','.join(repr(float(v)) for v in vec) + ']'
        from agent_matrix.models import get_db
        with get_db() as conn:
            conn.execute(
                "UPDATE knowledge_blocks SET embedding=%s::vector WHERE id=%s",
                (vec_literal, kb_id),
            )
            conn.commit()
        return True
    except Exception as e:
        logger.warning('store_embedding failed for %s: %s', kb_id, e)
        return False
