#!/usr/bin/env python3
"""project_workspace/services/embedding.py — Embedding service.

薄封装插件公共 EmbeddingService（plugins/_base/embeddings.py），
仅保留 project_workspace 的 config 键差异（module 标识）。
向量模型不可用时自动降级为关键词检索。
"""

from plugins._base.embeddings import EmbeddingService as _BaseEmbeddingService


class EmbeddingService(_BaseEmbeddingService):
    """project_workspace 专用 embedding 封装。"""

    def __init__(self, config: dict):
        super().__init__(config)
        self._config.setdefault('module', 'project_workspace')
