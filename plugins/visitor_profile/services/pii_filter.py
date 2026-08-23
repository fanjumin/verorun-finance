#!/usr/bin/env python3
"""
visitor_profile/services/pii_filter.py — PII 敏感信息过滤器
=============================================================
在事件数据入库前与 Agent 输出后两个阶段调用，防止邮箱、手机号、
身份证、IP、银行卡号等个人敏感信息进入长期画像存储。

注：VeroRun 系统级 services.sensitive_words 仅提供 check_sensitive
（敏感词检测），无 filter_pii；因此本插件用正则实现独立过滤，
不依赖系统级 API（防御式 import，缺失时静默回退正则）。
"""
import re


class PIIFilter:
    """个人可识别信息（PII）过滤器。

    用法:
        PIIFilter.filter("联系: user@example.com")       # → "联系: [EMAIL]"
        PIIFilter.clean_dict({"email": "a@b.com", "n": 1})
    """

    # 正则模式 → 占位符（类属性 dict，修正设计文档中的语法错误）
    _PATTERNS = {
        'email': re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'),
        'phone_cn': re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'),
        'id_card_cn': re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'),
        'ip_v4': re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),
        'credit_card': re.compile(r'\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b'),
    }

    _REPLACEMENTS = {
        'email': '[EMAIL]',
        'phone_cn': '[PHONE]',
        'id_card_cn': '[ID_CARD]',
        'ip_v4': '[IP]',
        'credit_card': '[CREDIT_CARD]',
    }

    @classmethod
    def filter(cls, text):
        """过滤文本中的 PII，替换为占位符（主 API）。"""
        return cls.clean(text)

    @classmethod
    def clean(cls, text):
        """替换文本中的 PII 模式。"""
        if not text or not isinstance(text, str):
            return text

        # 防御式：系统级 PII 过滤器若存在则优先（当前代码库未提供）
        try:
            from services.sensitive_words import filter_pii  # type: ignore
            return filter_pii(text)
        except (ImportError, AttributeError):
            pass

        # 回退：正则过滤
        for name, pattern in cls._PATTERNS.items():
            text = pattern.sub(cls._REPLACEMENTS[name], text)
        return text

    @classmethod
    def clean_dict(cls, data):
        """递归清理 dict 值中的 PII。"""
        if isinstance(data, dict):
            return {
                k: cls.clean_dict(v) for k, v in data.items()
            }
        if isinstance(data, list):
            return [cls.clean_dict(item) for item in data]
        if isinstance(data, str):
            return cls.clean(data)
        return data
