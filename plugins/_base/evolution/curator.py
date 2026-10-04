#!/usr/bin/env python3
"""Curator 配置解析 —— 复用承载插件能力的核心角色行（P1a 上提）。

memory_engine（extractor/reflexion 两处）与 cogevolution_substrate
（prompt_evolution/labeling 两处）的 `_load_curator_config` 结构完全一致：
读取插件内置 curator 提示词 → 解析归属角色 slug → `%s` 查 agent_matrix
核心角色行（is_system=1）→ 覆盖 `name`（token 归因）与 `system_prompt`。

差异仅在：plugin_id、agent_role、prompt 文件路径、cfg name —— 全部由调用方传入。
"""

import logging

logger = logging.getLogger('plugins._base.evolution.curator')

__all__ = ['load_curator_config']


def load_curator_config(plugin_id: str, agent_role: str, prompt_file: str, name: str) -> dict:
    """解析插件 curator 的 AgentRunner 配置；任一步失败返回 {}。

    旧内核（无 resolve_agent_roles）回退归属核心角色（agent_role），
    与下方 `or [agent_role]` 的运行时兜底同口径。
    """
    from agent_matrix.models import get_db
    try:
        from agent_matrix.models import resolve_agent_roles
    except ImportError:
        # F-DEP：旧内核无 resolve_agent_roles，回退归属核心角色，
        # 与下方 `or [agent_role]` 运行时兜底同口径。
        def resolve_agent_roles(_plugin_id, _metadata):
            return [agent_role]
    try:
        with open(prompt_file, 'r', encoding='utf-8') as f:
            prompt = f.read().strip()
    except OSError as e:
        logger.warning('[evolution] %s curator prompt unreadable: %s', plugin_id, e)
        return {}
    roles = resolve_agent_roles(plugin_id, {'agent_role': agent_role}) or [agent_role]
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM agent_matrix WHERE slug = %s AND is_system = 1",
            (roles[0],),
        ).fetchone()
    if not row:
        return {}
    cfg = dict(row)
    cfg['name'] = name              # 仅用于 token 日志归因
    cfg['system_prompt'] = prompt   # 覆盖为 curator 提示词
    return cfg
