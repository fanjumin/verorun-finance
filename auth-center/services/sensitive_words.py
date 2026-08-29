#!/usr/bin/env python3
"""Sensitive word filtering for article comments."""
import re, json, os

SENSITIVE_WORDS = {}

def load_words(word_dict=None):
    if word_dict:
        SENSITIVE_WORDS.update(word_dict)
    return SENSITIVE_WORDS

def check_sensitive(text):
    """Check text against sensitive word list. Returns (has_match, category, matched_word)."""
    text_lower = text.lower()
    for category, words in SENSITIVE_WORDS.items():
        for word in words:
            if word.lower() in text_lower:
                return True, category, word
    return False, '', ''

# Initialize with default list
load_words({"politics": ["法轮功", "六四", "天安门", "台独", "藏独", "疆独", "分裂", "邪教"], "violence": ["杀人", "恐怖", "爆炸", "袭击", "砍人", "枪击", "贩毒"], "porn": ["色情", "裸聊", "一夜情", "约炮", "援交", "卖淫"], "spam": ["加微信", "QQ群", "兼职", "刷单", "日赚", "月入", "彩票", "赌博", "赌场", "跑分", "洗钱"], "abuse": ["傻逼", "操你妈", "草泥马", "他妈", "去死", "废物", "脑残"], "finance_scam": ["保本", "稳赚", "翻倍", "100%收益", "无风险", "内部消息", "推荐股票", "涨停"]})
