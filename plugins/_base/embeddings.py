#!/usr/bin/env python3
"""插件公共 EmbeddingService —— memory_engine / project_workspace / visitor_profile 复用。

薄封装 agent_matrix.engine.UnifiedLLM 的向量化能力，
统一 is_ready()/dim/embed()/embed_batch() 接口。
所有方法失败时返回 None / [None, ...]，由调用方降级为关键词检索。
"""

import logging

logger = logging.getLogger('plugins._base.embeddings')


class EmbeddingService:
    """Resolve an embedding-capable model through the kernel and embed texts."""

    def __init__(self, config: dict = None):
        self._config = dict(config or {})
        self._dim = None
        self._llm = None

    # -- 内核访问 -------------------------------------------------------

    @property
    def _engine(self):
        if self._llm is None:
            from agent_matrix.engine import UnifiedLLM
            self._llm = UnifiedLLM()
        return self._llm

    @property
    def _module(self) -> str:
        return self._config.get('module', '')

    # -- 公共接口 -------------------------------------------------------

    def is_ready(self) -> bool:
        """True 当 provider_models 中存在可用的 embedding 模型（含可解析密钥）。"""
        try:
            model_id, _b, _k, _d = self._engine._resolve_embedding_model()
            return bool(model_id)
        except Exception as e:
            logger.warning('embedding readiness check failed: %s', e)
            return False

    @property
    def dim(self) -> int:
        """embedding 向量维度（首次解析后缓存）。"""
        if self._dim is None:
            try:
                _m, _b, _k, dim = self._engine._resolve_embedding_model()
                self._dim = int(dim) if dim else int(self._config.get('embedding_dim', 1536))
            except Exception:
                self._dim = int(self._config.get('embedding_dim', 1536))
        return self._dim

    def embed(self, text: str):
        """返回文本 embedding 向量（list[float]）；失败返回 None。"""
        try:
            return self._engine.get_embedding(text, module=self._module)
        except Exception as e:
            logger.error('embedding call failed: %s', e)
            return None

    def embed_batch(self, texts: list):
        """批量向量化；返回与输入等长的列表，单条失败为 None。"""
        try:
            return self._engine.embed_batch(texts, module=self._module)
        except Exception as e:
            logger.error('embedding batch call failed: %s', e)
            return [None] * len(texts)
