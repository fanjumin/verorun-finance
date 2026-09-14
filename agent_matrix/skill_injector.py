#!/usr/bin/env python3
"""
技能提示词注入器 —— 挂 before_prompt_resolve 过滤器（F11）
===========================================================
唯一技能注入路径（决策 A：替换旧 _load_installed_skills）。
按 ctx.task_type 匹配 skill requirements.task_types，命中可用技能
则追加其 prompt_md 并以来源标注。失败静默降级，绝不影响主流程。
"""
import json

from plugin_manager.hooks import get_hook_registry

FILTER_IDENTIFIER = 'skill_injector'


def inject_skill_prompt(prompt, ctx=None, **kwargs):
    """ctx 含 task_type（由 prompt_resolver ctx 构造提供）；命中可用技能则追加。"""
    try:
        task_type = (ctx or {}).get('task_type') or ''
        if not task_type:
            return prompt
        from plugin_manager.skill_registry import get_skill_registry
        reg = get_skill_registry()
        if reg is None:
            return prompt
        from plugin_manager.models_store import get_registry_db
        with get_registry_db() as conn:
            rows = conn.execute(
                "SELECT identifier, prompt_md, requirements FROM store_skills "
                "WHERE status='approved' AND prompt_md != '' ORDER BY installs DESC"
            ).fetchall()
        for r in rows:
            if task_type not in _task_types(r):
                continue
            if reg.resolver.evaluate(dict(r)).available:
                return (prompt + "\n\n<!-- skill:" + r['identifier'] + " -->\n"
                        + r['prompt_md'])
        return prompt
    except Exception:
        return prompt      # 注入失败绝不影响主流程


def _task_types(row) -> list:
    try:
        return (json.loads(row['requirements'] or '{}').get('task_types') or [])
    except Exception:
        return []


def register_skill_injector():
    """注册 before_prompt_resolve 过滤器（幂等：add_filter 按 identifier 去重）。"""
    get_hook_registry().add_filter('before_prompt_resolve', inject_skill_prompt,
                                   priority=30, identifier=FILTER_IDENTIFIER)
