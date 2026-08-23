#!/usr/bin/env python3
"""Token Generator — LLM 驱动的设计令牌生成器"""

import json

from ..models import DEFAULT_TOKENS, save_tokens
from ..token_service import validate_tokens, merge_tokens, get_token_schema


def generate_tokens_from_llm(llm_response_text, industry_template=None):
    """从 LLM 响应文本解析令牌 JSON

    Args:
        llm_response_text: LLM 返回的 JSON 字符串
        industry_template: 行业模板 dict（可选，包含预设配置）

    Returns:
        (success, tokens_dict_or_error)
    """
    # 解析 JSON
    try:
        tokens = json.loads(llm_response_text)
    except json.JSONDecodeError as e:
        # 尝试提取 JSON 块
        import re
        match = re.search(r'\{[\s\S]*\}', llm_response_text)
        if match:
            try:
                tokens = json.loads(match.group())
            except json.JSONDecodeError:
                return False, f'Failed to parse LLM response as JSON: {e}'
        else:
            return False, f'Failed to parse LLM response as JSON: {e}'

    # 合并行业模板预设
    if industry_template and isinstance(industry_template, dict):
        defaults = industry_template.get('defaults', {})
        if defaults:
            # 映射行业模板预设到令牌
            color_map = {
                'primary_color': ('colors', 'primary'),
                'accent_color': ('colors', 'accent'),
                'font_preference': ('typography', 'heading_font'),
                'style': ('brand', 'industry'),
            }
            for key, (section, field) in color_map.items():
                if key in defaults:
                    tokens.setdefault(section, {})[field] = defaults[key]

    # 填充默认值
    base = DEFAULT_TOKENS.copy()
    tokens = merge_tokens(base, tokens)

    # 验证
    valid, errors = validate_tokens(tokens)
    if not valid:
        return False, f'Validation failed: {"; ".join(errors)}'

    return True, tokens


def build_llm_prompt(industry_template, user_input, site_key='platform'):
    """构建 LLM 提示词

    Args:
        industry_template: 行业模板 dict
        user_input: 用户原始输入
        site_key: 站点标识

    Returns:
        LLM 提示词字符串
    """
    schema = get_token_schema()

    prompt = f"""你是一个网站设计系统专家。请根据用户需求生成完整的网站设计令牌（Design Tokens）JSON。

## 行业模板信息
{json.dumps(industry_template, ensure_ascii=False, indent=2) if industry_template else '无'}

## 用户需求
{user_input}

## 输出要求
请严格按照以下 JSON Schema 输出完整的令牌 JSON。所有字段必填。

### 输出 Schema（每个字段的说明）：
{json.dumps(schema, ensure_ascii=False, indent=2)}

## 规则
1. 品牌名称（brand.site_name）从用户需求中提取
2. 颜色方案（colors）应符合行业特征和用户风格偏好
3. 导航（navigation.items）应包含行业标准页面，最多 6 个
4. 页脚（footer）应包含标准分组和法律文档链接
5. 排版（typography）根据行业选择合适字体
6. 只返回 JSON，不要任何其他文字

## 站点标识
site_key: {site_key}

请输出完整 JSON："""
    return prompt


def build_modify_prompt(current_tokens, user_message, site_key='platform'):
    """构建增量修改提示词

    Args:
        current_tokens: 当前完整令牌 dict
        user_message: 用户修改请求

    Returns:
        LLM 提示词字符串
    """
    schema = get_token_schema()

    prompt = f"""你是一个网站设计系统维护专家。用户想修改当前网站的部分配置，请只输出需要变更的部分。

## 当前完整令牌
{json.dumps(current_tokens, ensure_ascii=False, indent=2)}

## 用户修改请求
{user_message}

## 输出要求
请输出一个 JSON 对象，只包含需要修改的字段路径和值。

格式：
{{
  "changes": {{
    "brand.site_name": "新名称",
    "colors.primary": "#ff0000",
    "navigation.items[0].title": "新标题"
  }},
  "explanation": "简要说明修改了什么"
}}

## 规则
1. 只输出需要变更的字段，不要输出整个令牌
2. 字段路径使用点号分隔，如 "brand.site_name"
3. 数组元素使用 [index] 访问，如 "navigation.items[0].title"
4. 只返回 JSON，不要任何其他文字

## 站点标识
site_key: {site_key}

请输出变更 JSON："""
    return prompt


def apply_partial_changes(current_tokens, changes_dict):
    """应用部分变更到令牌

    Args:
        current_tokens: 当前完整令牌
        changes_dict: {{"brand.site_name": "新值", "colors.primary": "#ff0000"}}

    Returns:
        更新后的令牌
    """
    import copy
    result = copy.deepcopy(current_tokens)

    for path, value in changes_dict.items():
        parts = path.split('.')
        target = result

        # 遍历路径到倒数第二层
        for part in parts[:-1]:
            # 处理数组索引: "items[0]" -> ("items", 0)
            if '[' in part:
                arr_name, idx = part.replace(']', '').split('[')
                target = target.setdefault(arr_name, [])
                target = target[int(idx)]
            else:
                target = target.setdefault(part, {})

        # 设置最后一层的值
        last = parts[-1]
        if '[' in last:
            arr_name, idx = last.replace(']', '').split('[')
            target[arr_name][int(idx)] = value
        else:
            target[last] = value

    return result