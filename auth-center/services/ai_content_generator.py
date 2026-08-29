#!/usr/bin/env python3
"""AI Content Generator — text and image generation via unified AI gateway.

Text:  agent_matrix.UnifiedLLM.chat()（供应商/模型由 system_config 配置）。
Image: agent_matrix.UnifiedLLM.image()（配图网关，替代直连 DashScope）。
"""

import logging, json, os, requests, time, ipaddress, socket
from urllib.parse import urlparse
from models import get_db

logger = logging.getLogger(__name__)

# =============================================
# Config helpers
# =============================================

def _get_key(key_name):
    """Read a specific API key from system_config."""
    with get_db() as conn:
        row = conn.execute("SELECT value FROM system_config WHERE key=%s", (key_name,)).fetchone()
    if row and row['value']:
        return row['value']
    return ''


def _get_ai_text_config():
    """从 system_config 读取默认 AI 文本供应商和模型，不再硬编码。"""
    with get_db() as conn:
        row_p = conn.execute("SELECT value FROM system_config WHERE key='ai_text_provider'").fetchone()
        row_m = conn.execute("SELECT value FROM system_config WHERE key='ai_text_model'").fetchone()
    return (
        row_p['value'] if row_p and row_p['value'] else 'siliconflow',
        row_m['value'] if row_m and row_m['value'] else 'deepseek-ai/DeepSeek-V3',
    )


# =============================================
# 文案生成 — 通过 UnifiedLLM 调用
# =============================================

def _qwen_chat(messages, model=None, temperature=0.7):
    """Call AI via UnifiedLLM. Provider and model from system_config, not hardcoded."""
    from agent_matrix.engine import get_gateway
    provider, default_model = _get_ai_text_config()
    gw = get_gateway()
    return gw.chat(
        provider=provider,
        model=model or default_model,
        messages=messages,
        temperature=temperature,
        max_tokens=4096,
        module='content_generator',
    )


CONTENT_PROMPTS = {
    'article': '''请帮我写一篇微信公众号文章，主题是：{topic}

要求：
1. 标题：吸引眼球，20字以内
2. 正文：800-1500字，段落清晰
3. 摘要：100字以内的文章摘要
4. 风格：专业但易懂

输出格式：
---
标题：【标题】
摘要：【摘要】
正文：
【正文内容】
---''',

    'weibo': '''请帮我写一条微博，内容是：{topic}

要求：
1. 正文：140字以内，简洁有力
2. 可以带1-2个话题标签
3. 风格：轻松有趣

输出格式：
---
正文：
【微博正文】
---''',

    'toutiao': '''请帮我写一篇今日头条文章，主题是：{topic}

要求：
1. 标题：吸引点击，30字以内
2. 正文：500-1000字，信息密度高
3. 风格：资讯类，客观

输出格式：
---
标题：【标题】
正文：
【正文内容】
---''',

    'announcement': '''请帮我写一则通知，内容是：{topic}

输出格式：
---
标题：【标题】
正文：
【正文内容】
---''',

    'promotion': '''请帮我写一则推广文案，内容是：{topic}

输出格式：
---
标题：【标题】
摘要：【摘要】
正文：
【正文内容】
---''',
}


def generate_article(topic, content_type='article', temperature=0.7):
    """Generate content using DashScope Qwen. Returns {title, summary, body, body_html}."""
    prompt_template = CONTENT_PROMPTS.get(content_type, CONTENT_PROMPTS['article'])
    prompt = prompt_template.replace('{topic}', topic)

    messages = [
        {'role': 'system', 'content': '你是一个专业的中文内容创作者。'},
        {'role': 'user', 'content': prompt},
    ]

    result = _qwen_chat(messages, temperature=temperature)
    return _parse_output(result, content_type)


def _parse_output(text, content_type='article'):
    """Parse AI output into structured fields."""
    title = ''
    summary = ''
    body = ''
    body_lines = []
    current_section = None

    for line in text.strip().split('\n'):
        stripped = line.strip()
        if stripped.startswith('标题：') or stripped.startswith('标题:'):
            title = stripped.split('：', 1)[-1] if '：' in stripped else stripped.split(':', 1)[-1]
            title = title.strip('【】').strip()
        elif stripped.startswith('摘要：') or stripped.startswith('摘要:'):
            summary = stripped.split('：', 1)[-1] if '：' in stripped else stripped.split(':', 1)[-1]
            summary = summary.strip('【】').strip()
        elif stripped.startswith('正文：') or stripped.startswith('正文:'):
            current_section = 'body'
        elif stripped.startswith('---'):
            continue
        elif current_section == 'body':
            body_lines.append(stripped)

    body = '\n'.join(body_lines).strip()
    if not title and not body:
        for line in text.strip().split('\n'):
            if line.strip() and not line.strip().startswith('---'):
                title = line.strip()
                break
        body = text

    if content_type == 'weibo':
        return {
            'title': title or '微博',
            'summary': summary or body[:100],
            'body': body,
            'body_html': body,
        }

    body_html = ''
    for para in body.split('\n'):
        para = para.strip()
        if para:
            body_html += f'<p>{para}</p>\n'

    return {
        'title': title or '未命名',
        'summary': summary or title[:100],
        'body': body,
        'body_html': body_html or body,
    }


# =============================================
# 配图生成 — 统一 AI 网关（agent_matrix.UnifiedLLM.image）
# =============================================

def generate_image(prompt, size='1024x1024', reference_image_url=None):
    """Generate image via the unified AI gateway (UnifiedLLM.image()).

    供应商/模型/key 解析全部交由网关完成，本模块不再直连 DashScope。
    支持图生图（reference_image_url 传入参考图）。
    """
    from agent_matrix.engine import get_gateway
    return get_gateway().image(
        prompt=prompt,
        size=size,
        reference_image_url=reference_image_url,
        module='content_generator',
    )


def generate_cover_image(title, topic=''):
    """Generate cover image for article."""
    prompt = f'公众号封面：{topic or title}，简约设计'
    return generate_image(prompt, size='1280x720')


def _validate_image_url(image_url):
    """校验图片 URL，防止 SSRF 攻击。只允许 http/https 公网地址。"""
    parsed = urlparse(image_url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError(f'不支持的 URL 协议: {parsed.scheme}')
    hostname = parsed.hostname
    if not hostname:
        raise ValueError('无效的 URL 主机名')
    # 禁止内网地址
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(hostname))
    except Exception:
        raise ValueError(f'无法解析主机名: {hostname}')
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        raise ValueError(f'禁止访问内网地址: {hostname}')
    if ip.is_multicast or ip.is_reserved:
        raise ValueError(f'禁止访问保留地址: {hostname}')

def download_image_to_file(image_url, filepath):
    """Download image URL to local file."""
    _validate_image_url(image_url)
    resp = requests.get(image_url, timeout=60)
    resp.raise_for_status()
    os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
    with open(filepath, 'wb') as f:
        f.write(resp.content)
    return filepath


def analyze_image(image_url, question='请详细描述这张图片的内容、布局、颜色和文字信息'):
    """Analyze image via UnifiedLLM (multimodal). Provider from system_config, not hardcoded."""
    _validate_image_url(image_url)
    from agent_matrix.engine import get_gateway
    provider, _ = _get_ai_text_config()
    gw = get_gateway()
    return gw.chat(
        provider=provider,
        model='Qwen/Qwen2.5-VL-72B-Instruct',
        messages=[
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': question},
                    {'type': 'image_url', 'image_url': {'url': image_url}},
                ]
            }
        ],
        max_tokens=2048,
        module='content_generator',
    )
