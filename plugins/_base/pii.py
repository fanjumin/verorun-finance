#!/usr/bin/env python3
r"""插件公共 PII 守卫 —— memory_engine / cogevolution_substrate 共用。

CN 手机号/身份证以数字负向断言 (?<!\d)…(?!\d) 界定，而非 \b —— \b 是
Unicode 词边界，CJK 同属 \w，中文紧邻数字时不成立，会漏检
"联系手机13800138000尾号""身份证…X结束"这类最常见的写法（F-DEP 复测遗留项）。
此写法与 visitor_profile/services/pii_filter.py、chatbot/guard.py 口径一致，
属对齐既有标准而非扩大覆盖；对 20 位订单号、13 位时间戳、19 位卡号等回归无误杀。
扩大覆盖范围仍须单独评估误杀。
"""

import re

PII_PATTERNS = (
    re.compile(r'(?i)(password|api[_-]?key|secret|token)\s*[:=]\s*\S+'),
    re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'),          # CN mobile
    re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'),         # CN ID card
)


def contains_pii(text: str) -> bool:
    """True 当文本命中任一 PII 模式。"""
    return any(p.search(text or '') for p in PII_PATTERNS)
