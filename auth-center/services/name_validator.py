#!/usr/bin/env python3
"""
Username & Display Name Validator — 依据《互联网用户账号名称管理规定》
及主流平台（微信/微博/知乎/小红书/B站）审核实践。

禁止内容类型：
1. 违反宪法和法律
2. 危害国家安全、泄露国家秘密
3. 煽动分裂国家、破坏国家统一
4. 煽动民族仇恨、民族歧视
5. 传播淫秽色情、赌博、暴力、凶杀、恐怖
6. 侮辱诽谤他人，侵害合法权益
7. 冒充官方、名人、机构
8. 包含联系方式（手机号、邮箱、网址）
9. 包含违法交易信息
10. 包含系统保留名称
"""

import re
from i18n import _

# ═══════════════════════════════════════════
# 一级敏感词 — 政治类（法律法规禁止）
# 依据: 《互联网用户账号名称管理规定》第六条
# ═══════════════════════════════════════════
LEVEL1_POLITICAL = [
    # 国家安全类
    '法轮功', '法轮', 'falun', '轮功', '大纪元', '新唐人',
    '藏独', '疆独', '台独', '港独', '蒙独',
    '分裂国家', '颠覆国家', '泄露国家秘密',
    '恐怖主义', '极端主义', '圣战',
    '邪教', '全能神', '呼喊派', '门徒会',
    'td', 'dl', 'zd', 'gd',
]

# ═══════════════════════════════════════════
# 二级敏感词 — 色情/暴力/赌博/毒品类
# ═══════════════════════════════════════════
LEVEL2_VULGAR = [
    # 色情
    '色情', '淫秽', '裸聊', '色狼', '一夜情',
    '约炮', '约啪', '嫖娼', '卖淫', '招嫖',
    'av', '三级片', '成人片', '黄色',
    '裸体', '裸照', '走光', '偷拍',
    # 赌博
    '赌博', '赌场', '赌球', '六合彩', '彩票预测',
    '棋牌室', '真人视讯', '百家乐', '轮盘赌',
    # 毒品
    '毒品', '吸毒', '冰毒', '摇头丸', '大麻',
    '海洛因', '可卡因', '罂粟', '制毒',
    # 暴力
    '杀人', '抢劫', '贩毒', '绑架', '恐怖袭击',
    '枪', '弹药', '管制刀具',
]

# ═══════════════════════════════════════════
# 三级敏感词 — 侮辱/歧视/侵权类
# ═══════════════════════════════════════════
LEVEL3_INSULT = [
    # 中文侮辱
    '傻逼', 'sb', '煞笔', '草泥马', '尼玛',
    '去死', '死全家', '全家福', '操你妈',
    '废物', '垃圾', '人渣', '贱人',
    '婊子', '妓女', '狗屎', '脑残',
    '装逼', '欠操', '狗屁',
    # 英文脏词
    'fuck', 'shit', 'bitch', 'asshole', 'bastard',
    'damn', 'dick', 'piss', 'slut', 'whore',
    'cunt', 'cock', 'nigger', 'chink',
    'porn', 'xxx', 'sex', 'erotic', 'nude',
    'fucking', 'blowjob', 'milf',
    # 歧视
    '黑鬼', '鬼子', '棒子', '阿三',
    '支那', '东亚病夫',
]

# ═══════════════════════════════════════════
# 领导人姓名 — 防止冒充/不当使用
# 包含: 全名 + 职务组合
# ═══════════════════════════════════════════
LEADER_NAMES = [
    # 现任 — 中文
    '习近平', '习主席', '习总书记', '习大大',
    '李克强', '李总理',
    # 现任 — 拼音变体
    'xijinping', 'xijinpingzhuxi', 'xizhuxi',
    'xijin', 'xidada', 'xiping',
    'likeqiang', 'lizongli', 'likeqiangzongli',
    # 历任 — 中文
    '毛泽东', '毛主席',
    '周恩来', '周总理',
    '邓小平', '邓主席',
    '江泽民', '江主席', '江总书记',
    '胡锦涛', '胡主席', '胡总书记',
    '温家宝', '温总理',
    # 历任 — 拼音变体
    'maozedong', 'maozhuxi', 'mao chairman',
    'zhouenlai', 'zhouzongli',
    'dengxiaoping', 'dengzhuxi',
    'jiangzemin', 'jiangzhuxi',
    'hujintao', 'huzhuxi',
    'wenjiabao', 'wenzongli',
]

# ═══════════════════════════════════════════
# 系统保留名称 — 不可注册
# ═══════════════════════════════════════════
RESERVED_NAMES = [
    'admin', 'administrator', 'root', 'system', 'superadmin', '官方', _('Customer Service'),
    _('Admin'), '系统', _('Notifications'), '公告',
]

# ═══════════════════════════════════════════
# 冒充类关键词 — 不能包含这些词（允许组合判定）
# ═══════════════════════════════════════════
IMPERSONATION_KEYWORDS = [
    '官方', '认证', _('Customer Service'), '中国', '国家', '政府',
    '央视', '新华社', '人民日报', '中央',
    '公安局', '法院', '检察院', '军队',
    '税务', '工商', '银行',
]

# ═══════════════════════════════════════════
# 手机号/邮箱/网址匹配模式
# ═══════════════════════════════════════════

def _compile_patterns():
    """编译正则表达式模式"""
    return {
        'phone': re.compile(r'1[3-9]\d{9}'),  # 11位手机号
        'email': re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'),
        'url': re.compile(r'(https?://|www\.)[a-zA-Z0-9./\-_]+'),
        'pure_numbers': re.compile(r'^\d{6,}$'),  # 纯数字6位+
    }

_patterns = _compile_patterns()


def check_username(username: str) -> dict:
    """
    检查用户名是否合规。
    
    Returns:
        {'valid': bool, 'error': str | None, 'level': int}
        level: 0=通过, 1=一级违规, 2=二级违规, 3=三级违规, 4=保留名, 5=格式违规
    """
    name = username.strip().lower()
    
    # ── 格式检查 ──
    if not name:
        return {'valid': False, 'error': '名称不能为空', 'level': 5}
    if len(name) < 2:
        return {'valid': False, 'error': '名称至少2个字符', 'level': 5}
    if len(name) > 20:
        return {'valid': False, 'error': '名称不超过20个字符', 'level': 5}
    
    # ── 纯数字或纯符号 ──
    if _patterns['pure_numbers'].match(name):
        return {'valid': False, 'error': '名称不能为纯数字', 'level': 5}
    if not re.search(r'[a-zA-Z\u4e00-\u9fff]', name):
        return {'valid': False, 'error': '名称需包含字母或汉字', 'level': 5}
    
    # ── 检查敏感词 ──
    for word in LEVEL1_POLITICAL:
        if word in name:
            return {'valid': False, 'error': '名称包含违规内容', 'level': 1}
    
    for word in LEVEL2_VULGAR:
        if word in name:
            return {'valid': False, 'error': '名称包含违规内容', 'level': 2}
    
    for word in LEVEL3_INSULT:
        if word in name:
            return {'valid': False, 'error': '名称包含不文明用语', 'level': 3}
    
    for word in LEADER_NAMES:
        if word in name:
            return {'valid': False, 'error': '名称包含受保护内容', 'level': 1}          
    
    # ── 保留名称（精确 + 前缀匹配）──
    for r in RESERVED_NAMES:
        lr = r.lower()
        if name == lr or name.startswith(lr + '_') or name.startswith(lr + '-'):
            return {'valid': False, 'error': '该名称已被系统保留', 'level': 4}
    # 不能包含 "admin" 作为主体部分
    if re.search(r'(^|[^a-z])admin($|[^a-z])', name):
        return {'valid': False, 'error': '该名称已被系统保留', 'level': 4}
    
    # ── 冒充判定：包含"官方/客服/认证"等词 ──
    for word in IMPERSONATION_KEYWORDS:
        if word in name:
            return {'valid': False, 'error': '名称不能包含"' + word + '"等误导性词汇', 'level': 5}
    
    # ── 联系方式泄漏 ──
    if _patterns['phone'].search(name):
        return {'valid': False, 'error': '名称不能包含手机号', 'level': 5}
    if _patterns['email'].search(name):
        return {'valid': False, 'error': '名称不能包含邮箱', 'level': 5}
    if _patterns['url'].search(name):
        return {'valid': False, 'error': '名称不能包含网址', 'level': 5}
    
    return {'valid': True, 'error': None, 'level': 0}


def check_display_name(name: str) -> dict:
    """
    检查显示名是否合规（比用户名宽松，但同样不能含敏感词）。
    """
    # 显示名规则基本同用户名，但允许更灵活
    return check_username(name)


def sanitize_name(name: str) -> str:
    """
    清理名称：去除首尾空白、连续空格、不可见字符。
    不修改过长的名称（由调用方自行截断）。
    """
    import re as _re
    # 去除不可见字符（除空格、汉字、字母、数字、常见符号外）
    cleaned = _re.sub(r'[^\w\s\u4e00-\u9fff\-_@.()（）]', '', name)
    # 合并连续空格
    cleaned = _re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


if __name__ == '__main__':
    # 测试
    tests = [
        ('admin', False, '保留名'),
        ('***REMOVED***', True, _('Normal')),
        ('法轮功学员', False, '一级敏感'),
        ('约炮神器', False, '二级敏感'),
        ('大傻逼', False, '三级敏感'),
        ('13800138000', False, '纯手机号'),
        ('官方客服', False, '冒充'),
        ('abc123', True, _('Normal')),
        ('xxoo', False, '需含字母或汉字'),
        ('hello@test.com', False, '含邮箱'),
        ('www.baidu.com', False, '含网址'),
        ('a', False, '太短'),
    ]
    for name, expect_valid, label in tests:
        r = check_username(name)
        status = '✅' if r['valid'] == expect_valid else '❌'
        print(f'{status} [{label}] "{name}" → valid={r["valid"]} err={r["error"]}')
