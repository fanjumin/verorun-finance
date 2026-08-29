#!/usr/bin/env python3
"""
Trigger Dispatch — 事件驱动工作流分发
======================================
当系统发生事件（如 cms.published）时，查 workflow_triggers 表中匹配的
触发器，并实例化对应工作流。

设计原则：
  - 全程 try/except 包裹，任何异常都不得影响调用方（如文章发布主流程）
  - 匹配逻辑：trigger_event 精确匹配 + match_condition 子集匹配
  - 统一由 run_workflow 创建并执行实例，避免双实例问题

用法：
    from orchestrator.trigger_dispatch import dispatch_event
    dispatch_event('cms.published', {'post_id': 1, 'category': 'news'})

@package orchestrator
"""

import logging

logger = logging.getLogger('orchestrator.trigger_dispatch')


def _match_condition(condition: dict, context: dict) -> bool:
    """match_condition 是 context 的子集则匹配。空条件=无条件匹配。"""
    if not condition:
        return True
    for k, v in condition.items():
        if context.get(k) != v:
            return False
    return True


def dispatch_event(event: str, context: dict = None) -> list:
    """根据事件触发匹配的工作流。

    P2-F17: 统一由 run_workflow 创建并执行实例，消除双实例问题。
    返回创建的 workflow_instance id 列表（失败返回空列表，绝不抛异常）。
    """
    context = context or {}
    created = []
    try:
        from . import models as m
        with m.get_db() as conn:
            rows = conn.execute(
                "SELECT id, name, workflow_id, match_condition "
                "FROM workflow_triggers "
                "WHERE trigger_event=%s AND is_active=1",
                (event,)
            ).fetchall()

        for row in rows:
            try:
                cond = m.from_json(row['match_condition'], {})
                if not _match_condition(cond or {}, context):
                    continue
                # P2-F17: 统一由 run_workflow 创建实例并执行（消除双实例）
                from orchestrator.workflow_engine import WorkflowEngine
                engine = WorkflowEngine()
                inst_id = engine.run_workflow(
                    row['workflow_id'],
                    trigger_type='event',
                    trigger_config={'event': event, 'context': context},
                    initial_context=context,
                )
                if inst_id:
                    created.append(inst_id)
                    logger.info("event '%s' triggered workflow %s -> instance %s",
                                event, row['workflow_id'], inst_id)
            except Exception as ex:
                logger.warning("dispatch trigger failed for event '%s': %s", event, ex)
                continue
    except Exception as ex:
        logger.warning("dispatch_event('%s') failed: %s", event, ex)
    return created
