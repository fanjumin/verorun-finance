#!/usr/bin/env python3
"""向量字面量工具 —— 记忆/策展向量化与 PG vector 文本字面量（P1a 上提）。

从 memory_engine（retriever/extractor 内联）与 cogevolution_substrate
（retriever/curation_store `_embedding_literal`）上提。基座统一用
`repr(float(v))` 渲染分量（两侧输入均为浮点向量，数值/字面量一致）。
"""

__all__ = ['vector_literal', 'embedding_literal']


def vector_literal(vec) -> str:
    """把向量渲染为 PG vector 文本字面量 `[v1,v2,...]`。"""
    return '[' + ','.join(repr(float(v)) for v in vec) + ']'


def embedding_literal(text: str, config: dict = None):
    """按需向量化并渲染字面量；模型不可用/失败返回 None（调用方降级）。"""
    try:
        from plugins._base.embeddings import EmbeddingService

        service = EmbeddingService(config or {})
        if not service.is_ready():
            return None
        vector = service.embed(text)
        if not vector:
            return None
        return vector_literal(vector)
    except Exception:
        return None
