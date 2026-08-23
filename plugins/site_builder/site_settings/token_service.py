#!/usr/bin/env python3
"""Token Service — 设计令牌业务逻辑"""

import json, copy

from i18n import _

from .models import DEFAULT_TOKENS, get_tokens, save_tokens


def validate_tokens(token_dict):
    """验证令牌结构完整性，返回 (valid, errors)"""
    errors = []
    required_sections = ['brand', 'colors', 'typography', 'navigation', 'footer', 'spacing', 'border_radius', 'shadows', 'seo']

    if not isinstance(token_dict, dict):
        return False, ['token_dict must be a dict']

    for section in required_sections:
        if section not in token_dict:
            errors.append(f'Missing required section: {section}')

    # 品牌验证
    brand = token_dict.get('brand', {})
    if not brand.get('site_name'):
        errors.append('brand.site_name is required')

    # 颜色验证
    colors = token_dict.get('colors', {})
    if not colors.get('primary'):
        errors.append('colors.primary is required')

    # 导航验证
    nav = token_dict.get('navigation', {})
    items = nav.get('items', [])
    for item in items:
        if not item.get('title'):
            errors.append(f'navigation item missing title')
        if not item.get('url'):
            errors.append(f'navigation item "{item.get("title", "?")}" missing url')

    return len(errors) == 0, errors


def merge_tokens(base_tokens, override_tokens):
    """深度合并令牌（override 覆盖 base）"""
    result = copy.deepcopy(base_tokens)
    for key, value in override_tokens.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key].update(value)
        else:
            result[key] = value
    return result


def get_token_schema():
    """返回令牌的 JSON Schema 描述（供 LLM 使用）"""
    return {
        "brand": {
            "site_name": _("品牌/网站名称"),
            "slogan": _("品牌口号（10字以内）"),
            "industry": _("行业分类"),
            "brand_story": _("品牌故事（200字）"),
            "logo_url": _("Logo 图片 URL"),
            "favicon_url": _("Favicon URL"),
            "company_name": _("公司全称"),
            "contact_email": _("联系邮箱"),
        },
        "colors": {
            "primary": _("主色调（hex）"),
            "secondary": _("辅色调（hex）"),
            "accent": _("强调色（hex）"),
            "background": _("背景色（hex）"),
            "surface": _("卡片/面板背景色（hex）"),
            "text_primary": _("主文字色（hex）"),
            "text_secondary": _("次要文字色（hex）"),
            "border": _("边框色（hex）"),
        },
        "typography": {
            "heading_font": _("标题字体（CSS font-family）"),
            "body_font": _("正文字体（CSS font-family）"),
            "font_scale": _("字体缩放比例（0.8~1.5）"),
            "h1_size": _("H1 字号"),
            "h2_size": _("H2 字号"),
            "body_size": _("正文 字号"),
            "line_height": _("行高"),
        },
        "navigation": {
            "items": [
                {
                    "title": _("导航名称"),
                    "url": _("链接地址"),
                    "icon": _("图标（可选）"),
                    "target": _("_self 或 _blank"),
                    "children": [{"title": _("子菜单名"), "url": _("子链接")}],
                }
            ],
        },
        "footer": {
            "sections": [{"name": _("分组名称"), "links": [{"title": _("链接名"), "url": _("链接")}]}],
            "articles": [{"title": _("文档标题"), "url": _("文档链接")}],
            "copyright": _("版权信息"),
            "icp_number": _("ICP 备案号"),
        },
        "spacing": {"xs": _("4px"), "sm": _("8px"), "md": _("16px"), "lg": _("32px"), "xl": _("64px")},
        "border_radius": {"sm": _("4px"), "md": _("8px"), "lg": _("12px"), "full": _("9999px")},
        "shadows": {"sm": _("小阴影"), "md": _("中阴影"), "lg": _("大阴影")},
        "seo": {"title": _("SEO 标题"), "description": _("SEO 描述")},
    }