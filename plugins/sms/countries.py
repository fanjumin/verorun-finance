#!/usr/bin/env python3
"""
SMS Countries — 国际手机号国家数据
==================================
33 个常用国家：国旗 emoji + 区号 + 本地号码验证规则。

验证规则说明：
  - phone_min/phone_max：本地号码位数（不含区号）
  - phone_prefix：号码首数字要求（如 CN 必须 1 开头）
  - 当 phone_min == phone_max 时使用固定长度校验
"""
# 国家数据：按使用频率排序
COUNTRIES = [
    # ── 大中华区 ──
    {"code": "CN", "dial": "+86",  "flag": "\U0001F1E8\U0001F1F3",
     "name_en": "China", "name_zh": "中国",
     "phone_min": 11, "phone_max": 11, "phone_prefix": "1"},
    {"code": "HK", "dial": "+852", "flag": "\U0001F1ED\U0001F1F0",
     "name_en": "Hong Kong", "name_zh": "中国香港",
     "phone_min": 8, "phone_max": 8},
    {"code": "MO", "dial": "+853", "flag": "\U0001F1F2\U0001F1F4",
     "name_en": "Macau", "name_zh": "中国澳门",
     "phone_min": 8, "phone_max": 8},
    {"code": "TW", "dial": "+886", "flag": "\U0001F1F9\U0001F1FC",
     "name_en": "Taiwan", "name_zh": "中国台湾",
     "phone_min": 9, "phone_max": 9},

    # ── 北美 ──
    {"code": "US", "dial": "+1",   "flag": "\U0001F1FA\U0001F1F8",
     "name_en": "United States", "name_zh": "美国",
     "phone_min": 10, "phone_max": 10},
    {"code": "CA", "dial": "+1",   "flag": "\U0001F1E8\U0001F1E6",
     "name_en": "Canada", "name_zh": "加拿大",
     "phone_min": 10, "phone_max": 10},

    # ── 欧洲 ──
    {"code": "GB", "dial": "+44",  "flag": "\U0001F1EC\U0001F1E7",
     "name_en": "United Kingdom", "name_zh": "英国",
     "phone_min": 10, "phone_max": 11},
    {"code": "DE", "dial": "+49",  "flag": "\U0001F1E9\U0001F1EA",
     "name_en": "Germany", "name_zh": "德国",
     "phone_min": 10, "phone_max": 11},
    {"code": "FR", "dial": "+33",  "flag": "\U0001F1EB\U0001F1F7",
     "name_en": "France", "name_zh": "法国",
     "phone_min": 9, "phone_max": 10},
    {"code": "IT", "dial": "+39",  "flag": "\U0001F1EE\U0001F1F9",
     "name_en": "Italy", "name_zh": "意大利",
     "phone_min": 10, "phone_max": 10},
    {"code": "ES", "dial": "+34",  "flag": "\U0001F1EA\U0001F1F8",
     "name_en": "Spain", "name_zh": "西班牙",
     "phone_min": 9, "phone_max": 9},
    {"code": "NL", "dial": "+31",  "flag": "\U0001F1F3\U0001F1F1",
     "name_en": "Netherlands", "name_zh": "荷兰",
     "phone_min": 9, "phone_max": 10},
    {"code": "CH", "dial": "+41",  "flag": "\U0001F1E8\U0001F1ED",
     "name_en": "Switzerland", "name_zh": "瑞士",
     "phone_min": 9, "phone_max": 10},
    {"code": "SE", "dial": "+46",  "flag": "\U0001F1F8\U0001F1EA",
     "name_en": "Sweden", "name_zh": "瑞典",
     "phone_min": 9, "phone_max": 10},
    {"code": "NO", "dial": "+47",  "flag": "\U0001F1F3\U0001F1F4",
     "name_en": "Norway", "name_zh": "挪威",
     "phone_min": 8, "phone_max": 8},
    {"code": "DK", "dial": "+45",  "flag": "\U0001F1E9\U0001F1F0",
     "name_en": "Denmark", "name_zh": "丹麦",
     "phone_min": 8, "phone_max": 8},
    {"code": "FI", "dial": "+358", "flag": "\U0001F1EB\U0001F1EE",
     "name_en": "Finland", "name_zh": "芬兰",
     "phone_min": 9, "phone_max": 10},
    {"code": "PL", "dial": "+48",  "flag": "\U0001F1F5\U0001F1F1",
     "name_en": "Poland", "name_zh": "波兰",
     "phone_min": 9, "phone_max": 9},
    {"code": "RU", "dial": "+7",   "flag": "\U0001F1F7\U0001F1FA",
     "name_en": "Russia", "name_zh": "俄罗斯",
     "phone_min": 10, "phone_max": 10},

    # ── 亚太 ──
    {"code": "JP", "dial": "+81",  "flag": "\U0001F1EF\U0001F1F5",
     "name_en": "Japan", "name_zh": "日本",
     "phone_min": 10, "phone_max": 11},
    {"code": "KR", "dial": "+82",  "flag": "\U0001F1F0\U0001F1F7",
     "name_en": "South Korea", "name_zh": "韩国",
     "phone_min": 10, "phone_max": 11},
    {"code": "SG", "dial": "+65",  "flag": "\U0001F1F8\U0001F1EC",
     "name_en": "Singapore", "name_zh": "新加坡",
     "phone_min": 8, "phone_max": 8},
    {"code": "MY", "dial": "+60",  "flag": "\U0001F1F2\U0001F1FE",
     "name_en": "Malaysia", "name_zh": "马来西亚",
     "phone_min": 9, "phone_max": 10},
    {"code": "TH", "dial": "+66",  "flag": "\U0001F1F9\U0001F1ED",
     "name_en": "Thailand", "name_zh": "泰国",
     "phone_min": 9, "phone_max": 10},
    {"code": "VN", "dial": "+84",  "flag": "\U0001F1FB\U0001F1F3",
     "name_en": "Vietnam", "name_zh": "越南",
     "phone_min": 9, "phone_max": 10},
    {"code": "PH", "dial": "+63",  "flag": "\U0001F1F5\U0001F1ED",
     "name_en": "Philippines", "name_zh": "菲律宾",
     "phone_min": 10, "phone_max": 10},
    {"code": "ID", "dial": "+62",  "flag": "\U0001F1EE\U0001F1E9",
     "name_en": "Indonesia", "name_zh": "印度尼西亚",
     "phone_min": 10, "phone_max": 12},
    {"code": "IN", "dial": "+91",  "flag": "\U0001F1EE\U0001F1F3",
     "name_en": "India", "name_zh": "印度",
     "phone_min": 10, "phone_max": 10},
    {"code": "AU", "dial": "+61",  "flag": "\U0001F1E6\U0001F1FA",
     "name_en": "Australia", "name_zh": "澳大利亚",
     "phone_min": 9, "phone_max": 10},
    {"code": "NZ", "dial": "+64",  "flag": "\U0001F1F3\U0001F1FF",
     "name_en": "New Zealand", "name_zh": "新西兰",
     "phone_min": 9, "phone_max": 10},

    # ── 中东 ──
    {"code": "AE", "dial": "+971", "flag": "\U0001F1E6\U0001F1EA",
     "name_en": "UAE", "name_zh": "阿联酋",
     "phone_min": 9, "phone_max": 9},
    {"code": "SA", "dial": "+966", "flag": "\U0001F1F8\U0001F1E6",
     "name_en": "Saudi Arabia", "name_zh": "沙特阿拉伯",
     "phone_min": 9, "phone_max": 9},

    # ── 南美 ──
    {"code": "BR", "dial": "+55",  "flag": "\U0001F1E7\U0001F1F7",
     "name_en": "Brazil", "name_zh": "巴西",
     "phone_min": 10, "phone_max": 11},
]


def find_country(dial='', code=''):
    """按区号或国家代码查找国家信息"""
    if dial:
        for c in COUNTRIES:
            if c['dial'] == dial:
                return c
    if code:
        for c in COUNTRIES:
            if c['code'] == code:
                return c
    return None


def detect_country_from_phone(phone: str):
    """从完整手机号中解析国家代码

    如 "+8613800138000" → {'code':'CN', 'dial':'+86', ...}
    如 "13800138000" → None（无区号无法判断）
    """
    if not phone or not phone.startswith('+'):
        return None
    # 按 dial 长度降序匹配（+852 优先于 +85）
    for c in sorted(COUNTRIES, key=lambda x: -len(x['dial'])):
        if phone.startswith(c['dial']):
            return c
    return None


def validate_phone_by_country(phone: str, country: dict) -> str:
    """按国家规则验证手机号，返回错误信息（空字符串表示通过）"""
    if not phone:
        return 'Phone number cannot be empty'

    has_dial = phone.startswith('+')
    digits = phone.lstrip('+')
    if not digits.isdigit():
        return 'Invalid phone number format'

    # 剥离国家区号，只校验本地号码部分。
    # 例如 "+8613800138000" → "13800138000"，避免把国家码 86 计入位数/前缀判断。
    # 仅当号码带 + 前缀时才剥离，防止把本地号码误判为区号。
    dial_digits = country.get('dial', '').lstrip('+')
    if has_dial and dial_digits and digits.startswith(dial_digits):
        digits = digits[len(dial_digits):]

    pmin = country.get('phone_min', 7)
    pmax = country.get('phone_max', 15)
    prefix = country.get('phone_prefix', '')

    if len(digits) < pmin or len(digits) > pmax:
        expected_len = f"{pmin}" if pmin == pmax else f"{pmin}-{pmax}"
        return f'Phone number should be {expected_len} digits'

    if prefix and not digits.startswith(prefix):
        return f'Phone number must start with {prefix}'

    return ''
