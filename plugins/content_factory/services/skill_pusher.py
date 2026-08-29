#!/usr/bin/env python3
"""Content Factory Plugin — Skill Pusher: 将加工内容导出为 Hermes/OpenClaw SKILL.md"""
from i18n import _
import json, re
from datetime import datetime
from typing import Optional
from plugins.content_factory.models import get_cf_db
from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('content_factory')


def generate_skill_md(processed: dict, raw_source_url: str = '') -> str:
    title = processed.get('title') or _('No Title')
    summary = processed.get('summary') or ''
    keywords = processed.get('keywords') or ''
    body = processed.get('body') or ''
    risk_level = processed.get('risk_level', 'normal')
    content_type = processed.get('content_type', 'article')
    kw_list = [k.strip() for k in keywords.split(',') if k.strip()]
    tags_str = ', '.join(kw_list[:5]) if kw_list else _('Finance, Analysis')
    safe_name = _safe_skill_name(title)

    skill_content = f"""---
name: {safe_name}
description: {_escape_yaml(summary[:200])}
tags: [{tags_str}]
source: VeroRun 维洛智能
risk_level: {risk_level}
type: {content_type}
created_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
pushed_via: content-factory
---

# {title}

{summary}

---

{body}

---

> 来源: [{raw_source_url}]({raw_source_url}) | 由 VeroRun 内容工厂生成
"""
    return skill_content


def generate_skill_name(title: str) -> str:
    return _safe_skill_name(title)


def _safe_skill_name(title: str) -> str:
    name = title[:40]
    name = re.sub(r'[^\w\u4e00-\u9fff]', '-', name)
    name = re.sub(r'-+', '-', name).strip('-').lower()
    if len(name) < 5:
        name = f'content-{datetime.now().strftime("%Y%m%d-%H%M")}'
    return name[:64]


def _escape_yaml(text: str) -> str:
    if not text:
        return ''
    return text.replace('"', '\\"').replace('\n', ' ').strip()


def push_to_skill(processed_id: int, admin_id: int = 1,
                  target_agent: str = 'hermes', category: str = 'content') -> dict:
    with get_cf_db() as conn:
        pc = conn.execute(
            """SELECT p.*, r.source_url
               FROM processed_contents p LEFT JOIN raw_contents r ON p.raw_id=r.id
               WHERE p.id=?""", (processed_id,)
        ).fetchone()
        if not pc:
            return {'success': False, 'error': _('Processed Content Does Not Exist')}

        skill_content = generate_skill_md(dict(pc), pc.get('source_url', '') or '')
        skill_name = generate_skill_name(pc['title'] or f'content-{processed_id}')

        existing = conn.execute(
            'SELECT id, push_count FROM skill_pushes WHERE processed_id=? AND target_agent=?',
            (processed_id, target_agent)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE skill_pushes SET skill_content=?, title=?, description=?,
                   skill_version=?, status='pushed', push_count=push_count+1,
                   last_pushed_at=NOW() WHERE id=?""",
                (skill_content, pc['title'], pc['summary'] or '',
                 datetime.now().strftime('%Y%m%d'), existing['id'])
            )
            push_id = existing['id']
        else:
            cur = conn.execute(
                """INSERT INTO skill_pushes (processed_id, title, description,
                   skill_name, skill_category, skill_content, target_agent,
                   push_count, last_pushed_at, created_by)
                   VALUES (?,?,?,?,?,?,?,1,NOW(),?) RETURNING id""",
                (processed_id, pc['title'], pc['summary'] or '',
                 skill_name, category, skill_content, target_agent, admin_id)
            )
            push_id = cur.fetchone()['id']
        conn.commit()

        return {'success': True, 'push_id': push_id, 'skill_name': skill_name,
                'target_agent': target_agent, 'skill_content': skill_content}


def list_pushed_skills(limit: int = 20, target_agent: str = '') -> list:
    where = ['1=1']
    params = []
    if target_agent:
        where.append('s.target_agent=?')
        params.append(target_agent)
    with get_cf_db() as conn:
        rows = conn.execute(
            f"""SELECT s.*, p.title as processed_title
                FROM skill_pushes s LEFT JOIN processed_contents p ON s.processed_id=p.id
                WHERE {" AND ".join(where)}
                ORDER BY s.id DESC LIMIT ?""",
            params + [limit]
        ).fetchall()
        return [dict(r) for r in rows]


def get_skill_by_id(push_id: int) -> Optional[dict]:
    with get_cf_db() as conn:
        row = conn.execute(
            """SELECT s.*, p.title as processed_title
               FROM skill_pushes s LEFT JOIN processed_contents p ON s.processed_id=p.id
               WHERE s.id=?""", (push_id,)
        ).fetchone()
        return dict(row) if row else None


def get_skill_for_download(push_id: int) -> Optional[dict]:
    skill = get_skill_by_id(push_id)
    if not skill:
        return None
    return {
        'id': skill['id'], 'skill_name': skill['skill_name'],
        'skill_content': skill['skill_content'], 'target_agent': skill['target_agent'],
        'category': skill['skill_category'], 'version': skill['skill_version'],
        'pushed_at': skill['last_pushed_at'],
    }