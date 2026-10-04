#!/usr/bin/env python3
"""文本工具 —— 关键词提取与记录去重哈希（P1a 上提）。

从 memory_engine 上提（该插件内 extractor/reflexion/sedimentation 三处
`_keywords` 与四处 owner 作用域哈希完全重复）。**仅收敛 memory_engine 的语义**：
- `keywords`：CJK 连续 2+ 字 + latin 2+ 词（小写），去重后取前 12；
- `record_hash`：`sha256(f"{owner_id}|{content}")`，无归一化。

cogevolution_substrate 的 keywords（混排 `[A-Za-z0-9_\\u4e00-\\u9fff]{2,}`）
与其四参 `content_hash`（含 lower+空白折叠归一化）语义不同，**不在本模块收敛**。
"""

import hashlib
import re

__all__ = ['keywords', 'record_hash']

_CJK_RE = re.compile(r'[\u4e00-\u9fff]{2,}')
_LATIN_RE = re.compile(r'[a-z]{2,}')


def keywords(text: str) -> list:
    """Naive keyword extraction: CJK bigrams + 2+ char latin tokens."""
    kws = set(_CJK_RE.findall(text))
    kws.update(w.lower() for w in _LATIN_RE.findall(text.lower()))
    return list(kws)[:12]


def record_hash(owner_id, content) -> str:
    """owner 作用域记录哈希：sha256("{owner_id}|{content}")（memory_engine 口径）。"""
    return hashlib.sha256(f"{owner_id}|{content}".encode('utf-8')).hexdigest()
