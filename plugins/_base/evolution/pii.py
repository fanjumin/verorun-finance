#!/usr/bin/env python3
"""PII 判定唯一真源 + 旧内核自包含回退。

优先委托 plugins._base.pii（宿主共享模块）；旧内核缺该模块（F-DEP，如服务器
1.2.x）时，用下方与其逐字一致的正则兜底，避免自动提取链因 ImportError 整体宕掉。

CN 手机/身份证边界用数字负向断言 (?<!\\d)…(?!\\d)，而非 \\b —— \\b 是 Unicode
词边界，CJK 同属 \\w，中文紧邻数字时不成立，会漏检"联系手机138…尾号"这类写法。
调整覆盖范围时须与 plugins/_base/pii.py 同步（误杀口径单独评估）。
"""

import re

_PATTERNS = (
    re.compile(r'(?i)(password|api[_-]?key|secret|token)\s*[:=]\s*\S+'),
    re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'),          # CN mobile
    re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'),         # CN ID card
)


def _fallback(text: str) -> bool:
    return any(p.search(text or '') for p in _PATTERNS)


def contains_pii(text: str) -> bool:
    """True 当文本命中任一 PII 模式；优先使用宿主共享实现。"""
    try:
        from plugins._base.pii import contains_pii as shared
    except ImportError:
        return _fallback(text)
    return shared(text)
