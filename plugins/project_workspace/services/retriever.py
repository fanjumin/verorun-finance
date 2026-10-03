#!/usr/bin/env python3
"""project_workspace/services/retriever.py — Knowledge retrieval (RAG) pipeline.

Hybrid retrieval:
  - 向量路：pgvector cosine（embedding 可用时）
  - 关键词路：ILIKE 按命中 token 数降序
两条路并行检索，经 Reciprocal Rank Fusion（k=60）融合取 top_k。
向量模型不可用时自动降级为纯关键词检索，服务不中断。

Cross-project 搜索时按当前用户的成员关系过滤，保证项目隔离。
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger('project_workspace.retriever')

_RRF_K = 60


class KnowledgeRetriever:
    """Retrieve relevant document chunks for a given query, project-scoped."""

    def __init__(self, config: dict):
        self._config = config or {}
        self._embedder = None

    @property
    def _embed(self):
        if self._embedder is None:
            from .embedding import EmbeddingService
            self._embedder = EmbeddingService(self._config)
        return self._embedder

    @property
    def _top_k(self) -> int:
        return int(self._config.get('semantic_search_top_k', 10))

    @staticmethod
    def _tokenize(query: str) -> list:
        """简单分词：按空格/英文标点/中文标点切分，保留长度≥2 的 token，最多 6 个。"""
        tokens = re.split(r'[\s,，。．.;；：:、!！?？()（）\[\]{}"\'`~\-_]+', query)
        return [t for t in tokens if len(t) >= 2][:6]

    # -- 主检索入口 ------------------------------------------------------

    def retrieve(self, query: str, project_id: str,
                 top_k: int = None, cross_project: bool = False,
                 user_id: str = '') -> list:
        """Hybrid search: vector + keyword in parallel, RRF fusion.

        Args:
            query: 查询文本
            project_id: 当前项目（cross_project 时作为成员过滤边界）
            top_k: 返回条数
            cross_project: 跨项目搜索（仅检索当前用户有成员关系的项目）
            user_id: 当前用户 id（用于跨项目成员过滤）
        """
        top_k = top_k or self._top_k
        if not query or not query.strip():
            return []

        vec_rows, kw_rows = [], []
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_kw = pool.submit(self._keyword_search, query, project_id,
                               top_k, cross_project, user_id)
            f_vec = None
            try:
                if self._embed.is_ready():
                    f_vec = pool.submit(self._vector_search, query, project_id,
                                        top_k, cross_project, user_id)
            except Exception:
                f_vec = None
            kw_rows = f_kw.result()
            if f_vec is not None:
                vec_rows = f_vec.result()

        if not vec_rows and not kw_rows:
            return []

        results = self._rrf_fusion(vec_rows, kw_rows, top_k)

        # 可配置 rerank 模型：RRF 之后追加语义重排
        rerank_model = (self._config.get('rerank_model') or '').strip()
        if rerank_model:
            # TODO(选型): 调用 UnifiedLLM 或专用 reranker API 对 results 重排。
            # 目前仅打日志，不改变结果顺序。
            logger.info('rerank_model configured (%s) but reranker not implemented yet; skip', rerank_model)

        return results

    # -- 双路检索 --------------------------------------------------------

    def _vector_search(self, query: str, project_id: str,
                       top_k: int, cross_project: bool, user_id: str) -> list:
        """向量路：pgvector cosine。embedding 不可用/失败时返回 []。"""
        from ..models import get_db
        conn = get_db()
        try:
            vec = self._embed.embed(query)
            if not vec:
                return []
            vec_literal = '[' + ','.join(repr(v) for v in vec) + ']'
            sql = (
                "SELECT c.id, c.document_id, c.chunk_index, c.content,"
                " c.section_title, c.page_number,"
                " d.filename, d.original_name, d.summary,"
                " 1 - (c.embedding <=> ?::vector) AS similarity,"
                " 0 AS hits"
                " FROM document_chunks c"
                " JOIN documents d ON d.id = c.document_id"
                " WHERE c.embedding IS NOT NULL AND d.status = 'ready'"
            )
            params = [vec_literal]
            scope_clause, scope_params = self._project_scope(cross_project, project_id, user_id)
            sql += scope_clause
            params.extend(scope_params)
            sql += " ORDER BY c.embedding <=> ?::vector LIMIT ?"
            params.extend([vec_literal, top_k * 2])
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error('vector search failed: %s', e)
            return []
        finally:
            conn.close()

    def _keyword_search(self, query: str, project_id: str,
                        top_k: int, cross_project: bool, user_id: str) -> list:
        """关键词路：ILIKE 按命中 token 数降序。"""
        tokens = self._tokenize(query)
        if not tokens:
            return []
        from ..models import get_db
        conn = get_db()
        try:
            like_clauses = ' OR '.join('c.content ILIKE ?' for _ in tokens)
            case_hits = ' + '.join('(CASE WHEN c.content ILIKE ? THEN 1 ELSE 0 END)'
                                   for _ in tokens)
            # 参数顺序：所有 ILIKE 的 like 参数在前，case_hits 的 like 参数在后
            like_params = ['%' + t + '%' for t in tokens]
            params = like_params + like_params
            sql = (
                "SELECT c.id, c.document_id, c.chunk_index, c.content,"
                " c.section_title, c.page_number,"
                " d.filename, d.original_name, d.summary,"
                " 0.0 AS similarity,"
                " (" + case_hits + ") AS hits"
                " FROM document_chunks c"
                " JOIN documents d ON d.id = c.document_id"
                " WHERE d.status = 'ready' AND (" + like_clauses + ")"
            )
            scope_clause, scope_params = self._project_scope(cross_project, project_id, user_id)
            sql += scope_clause
            params.extend(scope_params)
            sql += " ORDER BY hits DESC, c.chunk_index LIMIT ?"
            params.append(top_k * 2)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error('keyword search failed: %s', e)
            return []
        finally:
            conn.close()

    @staticmethod
    def _project_scope(cross_project: bool, project_id: str, user_id: str):
        """返回 (scope_clause, params)。cross_project 时按成员关系过滤。"""
        if cross_project:
            return (" AND c.project_id IN (SELECT DISTINCT project_id"
                    " FROM project_members WHERE user_id = ?)", [user_id or ''])
        return (' AND c.project_id = ?', [project_id])

    # -- RRF 融合 --------------------------------------------------------

    @classmethod
    def _rrf_fusion(cls, vector_rows: list, keyword_rows: list,
                    top_k: int, k: int = _RRF_K) -> list:
        """Reciprocal Rank Fusion，按融合分降序取 top_k。"""
        scores = {}
        for rank, r in enumerate(vector_rows, start=1):
            e = scores.setdefault(r['id'], {
                'row': r, 'score': 0.0, 'similarity': r.get('similarity', 0.0),
            })
            e['score'] += 1.0 / (k + rank)
        for rank, r in enumerate(keyword_rows, start=1):
            e = scores.setdefault(r['id'], {
                'row': r, 'score': 0.0, 'similarity': 0.0,
            })
            e['score'] += 1.0 / (k + rank)
            if not e['row'].get('similarity'):
                e['row'] = r
            e['row']['hits'] = r.get('hits', 0)

        fused = sorted(scores.values(), key=lambda e: e['score'], reverse=True)[:top_k]
        results = []
        for e in fused:
            row = dict(e['row'])
            row['rrf_score'] = round(e['score'], 6)
            results.append(row)
        return results

    # -- 其他查询接口 ----------------------------------------------------

    def retrieve_documents(self, project_id: str, status: str = 'ready',
                           limit: int = 50, offset: int = 0, keyword: str = '') -> list:
        """List documents in a project with optional keyword filter."""
        from ..models import get_db
        conn = get_db()
        try:
            sql = ("SELECT id, project_id, filename, original_name, file_ext,"
                   " file_size, mime_type, status, summary, word_count,"
                   " tags, uploaded_by, uploaded_at, processed_at"
                   " FROM documents WHERE project_id = ?")
            params = [project_id]
            if status:
                sql += " AND status = ?"
                params.append(status)
            if keyword:
                sql += " AND (original_name ILIKE ? OR summary ILIKE ?)"
                like = '%%%s%%' % keyword
                params.extend([like, like])
            sql += " ORDER BY uploaded_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_chunks_by_document(self, document_id: str) -> list:
        """Get all chunks for a specific document."""
        from ..models import get_db
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT id, chunk_index, content, section_title, page_number, token_count"
                " FROM document_chunks WHERE document_id = ?"
                " ORDER BY chunk_index",
                (document_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
