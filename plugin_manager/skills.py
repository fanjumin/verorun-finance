#!/usr/bin/env python3
"""
Plugin Manager — 轻量技能层（Skill）
=====================================
对标 OpenClaw / Hermes / WorkBuddy 的社区 UGC 模式：
技能 = 一个 Markdown 文件（YAML front-matter + 正文），低门槛发布，
与"完整插件"（蓝图+DB+权限）形成两级供给。

技能格式（对齐 SKILL.md 开放标准）：
    ---
    identifier: pdf_reader
    name: PDF Reader
    description: Extract text from PDF files
    tags: [pdf, reader]
    version: 1.0.0
    ---
    <正文：给 Agent 的指令 + 可用工具描述>

本模块为纯逻辑（无 Flask 依赖），供 routes.py 薄封装 + 单测直调。
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

# 技能安装根目录（运行时生成，data/ 未被 veroguard 跟踪）
SKILLS_DIR = os.path.join(os.environ.get('VERORUN_DATA_DIR', 'data'), 'skills')

# 单技能内容上限（50KB，防止滥用存储/提示词注入长文本）
_MAX_CONTENT_BYTES = 50 * 1024

_IDENTIFIER_RE = re.compile(r'^[a-z0-9_-]{2,32}$')

# Prompt 注入黑名单关键词（命中即 audit_status=reject，需人工复核）
_PROMPT_INJECTION_KEYWORDS = [
    'ignore previous instructions',
    'ignore all previous',
    'disregard your instructions',
    'disregard all previous',
    'you are now',
    'pretend you are',
    'reveal system prompt',
    'show your system prompt',
    'jailbreak',
    'act as openai',
    'act as chatgpt',
]


def parse_front_matter(content_md: str) -> Tuple[Optional[Dict[str, str]], str]:
    """解析开头的 --- front-matter --- 块。

    Returns:
        (meta_dict, body)：meta 为 front-matter 键值（字符串）；
        无 front-matter 或格式缺失返回 (None, 原文)。
    """
    text = content_md.lstrip('\ufeff \t\r\n')
    if not text.startswith('---'):
        return None, content_md
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            end = i
            break
    if end is None:
        return None, content_md
    meta_raw = '\n'.join(lines[1:end])
    body = '\n'.join(lines[end + 1:]).strip()
    meta: Dict[str, str] = {}
    for line in meta_raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' in line:
            k, _, v = line.partition(':')
            meta[k.strip().strip('"\'')] = v.strip().strip('"\'')
    return meta, body


def _parse_tags(tags_val: str) -> List[str]:
    """解析 tags 字段：`[pdf, reader]` 或 `pdf, reader` → list[str]。"""
    tags_val = (tags_val or '').strip()
    tags_val = tags_val.strip('[]')
    return [t.strip() for t in tags_val.split(',') if t.strip()][:8]


def validate_skill(content_md: str) -> Tuple[List[str], Dict[str, str]]:
    """校验技能内容。

    Returns:
        (errors, meta)：errors 非空表示校验失败（调用方应返回 400）；
        meta 含 identifier/name/description/tagline/tags/version。
    """
    errors: List[str] = []
    meta, body = parse_front_matter(content_md)
    if meta is None:
        return ['content_md must start with --- front-matter ---'], {}

    identifier = (meta.get('identifier') or '').strip()
    name = (meta.get('name') or '').strip()
    description = (meta.get('description') or '').strip()

    if not identifier or not _IDENTIFIER_RE.match(identifier):
        errors.append('identifier: 2-32 chars of [a-z0-9_-]')
    if not name or len(name) > 60:
        errors.append('name is required (<=60 chars)')
    if not description or len(description) > 300:
        errors.append('description is required (<=300 chars)')
    if not body:
        errors.append('skill body is empty')
    if len(content_md.encode('utf-8')) > _MAX_CONTENT_BYTES:
        errors.append(f'content_md exceeds {_MAX_CONTENT_BYTES // 1024}KB limit')

    return errors, {
        'identifier': identifier,
        'name': name,
        'description': description,
        'tagline': (meta.get('tagline') or '').strip()[:140],
        'tags': _parse_tags(meta.get('tags', '')),
        'version': (meta.get('version') or '1.0.0').strip(),
    }


def audit_skill(content_md: str) -> Tuple[str, List[str]]:
    """自动审核：prompt 注入关键词 + 代码块危险扫描。

    Returns:
        (audit_status, reasons)：reasons 为空 → 'pass'；否则 'reject'。
        人工复核兜底由 /skills/admin/<id>/review 承担。
    """
    reasons: List[str] = []
    low = content_md.lower()

    # 1) Prompt 注入关键词
    for kw in _PROMPT_INJECTION_KEYWORDS:
        if kw in low:
            reasons.append(f'prompt injection keyword: {kw}')

    # 2) 代码块危险模式扫描（复用插件审核引擎的正则库）
    blocks = re.findall(r'```(?:\w+)?\n(.*?)```', content_md, re.S)
    if blocks:
        from .audit import _DANGEROUS_PATTERNS
        for block in blocks:
            for pat, label in _DANGEROUS_PATTERNS:
                if re.search(pat, block, re.I):
                    reasons.append(f'dangerous code in code block: {label}')

    return ('reject' if reasons else 'pass'), reasons


def install_skill(identifier: str, content_md: str) -> str:
    """安装技能到本地 data/skills/<identifier>/SKILL.md，返回文件绝对路径。"""
    d = os.path.join(SKILLS_DIR, identifier)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, 'SKILL.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content_md)
    return path


def uninstall_skill(identifier: str) -> bool:
    """卸载本地技能（仅删 SKILL.md，保留目录防误删他人文件）。"""
    path = os.path.join(SKILLS_DIR, identifier, 'SKILL.md')
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def export_skill_md(row: Dict[str, Any]) -> str:
    """从 store_skills 行生成标准 SKILL.md（供 P1-4 导出复用）。"""
    tags = row.get('tags') or '[]'
    if isinstance(tags, str):
        tags = tags.strip('[]')
    return (
        f"---\nidentifier: {row.get('identifier', '')}\n"
        f"name: {row.get('name', '')}\n"
        f"description: {row.get('description', '')}\n"
        f"tags: [{tags}]\n"
        f"version: {row.get('version', '1.0.0')}\n"
        f"---\n\n{row.get('content_md', '')}"
    )


# ── Skill 1.0（skill.json 声明式清单）──────────────────────────────────
# 0.9 技能 = SKILL.md 整篇；1.0 技能带 skill.json 权威清单（依赖/工具/任务类型）。
# 本模块保持纯逻辑（无 Flask、无 DB），供 routes 薄封装与单测直调。

# 角色归一化：agent_matrix/roles/*.yaml 带数字前缀（如 07-service.yaml），
# 清单里 role slug 统一按去前缀后的名称匹配（'07-service' → 'service'）。
_ROLE_PREFIX_RE = re.compile(r'^\d+[_-]?')


def normalize_role_slug(role: str) -> str:
    """剥离角色编号前缀：'07-service' / '7_service' → 'service'。"""
    return _ROLE_PREFIX_RE.sub('', (role or '').strip())


def parse_skill_package(skill_json: str) -> Tuple[List[str], Dict[str, Any]]:
    """解析 skill.json 声明 → 归一化 manifest（requirements 用）。

    Returns:
        (errors, manifest)：errors 非空表示解析/校验失败；
        manifest 含 slug/name/version/description/requirements/task_types。
        0.9 技能（无 skill.json）由调用方传入 '{}'，返回空 manifest。
    """
    errors: List[str] = []
    manifest: Dict[str, Any] = {}
    if not skill_json or not skill_json.strip():
        return errors, manifest
    try:
        data = json.loads(skill_json)
    except Exception:
        return ['skill.json is not valid JSON'], {}

    if not isinstance(data, dict):
        return ['skill.json must be a JSON object'], {}

    slug = (data.get('slug') or '').strip()
    name = (data.get('name') or '').strip()
    version = (data.get('version') or '').strip() or '1.0.0'
    description = (data.get('description') or '').strip()

    if not _IDENTIFIER_RE.match(slug):
        errors.append('slug: 2-32 chars of [a-z0-9_-]')
    if not name or len(name) > 60:
        errors.append('name is required (<=60 chars)')
    if not description or len(description) > 300:
        errors.append('description is required (<=300 chars)')
    if version and not re.match(r'^\d+\.\d+\.\d+', version):
        errors.append('version must be SemVer like 1.2.0')

    reqs_raw = data.get('requirements') or {}
    if not isinstance(reqs_raw, dict):
        reqs_raw = {}

    # plugins: dict {slug: spec} 或 list [{slug, min_version, max_version, spec}]
    plugins: List[dict] = []
    p = reqs_raw.get('plugins')
    if isinstance(p, dict):
        plugins = [{'slug': str(k), 'spec': str(v) or ''} for k, v in p.items()]
    elif isinstance(p, list):
        for d in p:
            if not isinstance(d, dict) or not d.get('slug'):
                continue
            spec = d.get('spec') or ''
            if not spec:
                bits = []
                if d.get('min_version'):
                    bits.append(f">={d['min_version']}")
                if d.get('max_version'):
                    bits.append(f"<={d['max_version']}")
                spec = ', '.join(bits)
            plugins.append({'slug': str(d['slug']).strip(), 'spec': spec})

    roles = [normalize_role_slug(r) for r in (reqs_raw.get('roles') or [])
             if isinstance(r, str) and r.strip()]

    manifest = {
        'slug': slug,
        'name': name,
        'version': version,
        'description': description,
        'requirements': {
            'plugins': plugins,
            'skills': [str(s) for s in (reqs_raw.get('skills') or [])],
            'roles': roles,
            'capabilities': [str(c) for c in (reqs_raw.get('capabilities') or [])],
            'editions': [str(e) for e in (reqs_raw.get('editions') or [])],
        },
        'permissions': [str(x) for x in (data.get('permissions') or [])],
        'task_types': [str(t) for t in (data.get('task_types') or [])],
    }
    return errors, manifest
