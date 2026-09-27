#!/usr/bin/env python3
"""插件公共 PII 守卫 —— memory_engine / cogevolution_substrate 共用。

正则与原两插件内嵌版本逐字一致（仅去重，不扩覆盖；
扩大覆盖范围须单独评估误杀，另行方案）。
"""

import re

PII_PATTERNS = (
    re.compile(r'(?i)(password|api[_-]?key|secret|token)\s*[:=]\s*\S+'),
    re.compile(r'\b1[3-9]\d{9}\b'),          # CN mobile
    re.compile(r'\b\d{17}[\dXx]\b'),         # CN ID card
)


def contains_pii(text: str) -> bool:
    """True 当文本命中任一 PII 模式。"""
    return any(p.search(text or '') for p in PII_PATTERNS)
