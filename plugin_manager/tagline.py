#!/usr/bin/env python3
"""
Plugin Manager — tagline 宣传语自动提取
=========================================
优先级：手写 tagline > AI 从 README 提取 > 类别默认语 > 插件名。
LLM 失败自动降级，绝不阻断上架（方案 §3 / §14 对齐）。
"""

from .logger import get_plugin_logger

logger = get_plugin_logger('store-tagline')

# 7 类规范类别的默认宣传语（中英双份，与前端 _CAT_GRADS 分类一致）
_DEFAULT_TAGLINES = {
    'zh-CN': {
        'system': '系统运行，尽在掌握',
        'shop': 'AI 智能商城，一站式经营',
        'content': '内容生产，AI 驱动',
        'ai_agent': '智能 Agent，替你思考',
        'social': '多平台内容，一键分发',
        'tools': '生产力工具箱',
        'supply_chain': '货源直连，供应链提效',
    },
    'en': {
        'system': 'System running, fully under control',
        'shop': 'AI-powered store, one-stop operations',
        'content': 'Content production, AI-driven',
        'ai_agent': 'Smart agents that think for you',
        'social': 'Multi-platform content, one-click publishing',
        'tools': 'Productivity toolbox',
        'supply_chain': 'Direct supply, streamlined sourcing',
    },
}


def _lang_key(lang: str) -> str:
    """zh-CN / zh → 'zh-CN'，其余 → 'en'。"""
    return 'zh-CN' if (lang or '').lower().startswith('zh') else 'en'


def build_tagline(plugin: dict, readme_text: str, lang: str = 'zh-CN') -> str:
    """生成/兜底插件宣传语。

    Args:
        plugin: 商店插件 dict（含 identifier/category/tagline/name）
        readme_text: README 文本（可为空串）
        lang: 目标语言，如 'zh-CN' / 'en'

    Returns:
        宣传语字符串（绝不返回空串——降级到类别默认语/插件名）。
    """
    t = (plugin.get('tagline') or '').strip()
    if t:
        return t[:32]

    category = plugin.get('category') or 'system'
    name = plugin.get('name') or plugin.get('identifier') or ''
    lk = _lang_key(lang)

    try:
        if not readme_text.strip():
            raise ValueError('empty readme')
        from services.ai_content_generator import _qwen_chat
        lang_hint = '中文' if lk == 'zh-CN' else 'English'
        prompt = (
            f'根据下面的插件 README，用{lang_hint}写一句不超过 14 字的宣传语（slogan），'
            f'只输出口号本身，不要引号、标点、解释：\n\n{readme_text[:2000]}'
        )
        out = (_qwen_chat([{'role': 'user', 'content': prompt}]) or '').strip()
        out = out.strip('"\'“”‘’')
        if out:
            logger.info('tagline generated for %s: %s', plugin.get('identifier'), out)
            return out[:32]
    except Exception as e:
        logger.warning('tagline AI fallback for %s: %s', plugin.get('identifier'), e)

    return _DEFAULT_TAGLINES.get(lk, {}).get(category) or name
