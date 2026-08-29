#!/usr/bin/env python3
"""
analytics/workflow_nodes.py — 分析系统 Workflow 节点处理器

可注册到 orchestrator 的 WorkflowEngine 中，配合 DAG 引擎自动运行。

注册方式（在 admin/app.py 中）:
  from analytics.workflow_nodes import register_analytics_handlers
  register_analytics_handlers(workflow_engine)

节点类型:
  - analytics_report:      生成分析报告（日报/周报）
  - analytics_insight:     AI 解读报告生成可读摘要
  - analytics_alert_check: 执行告警检查
  - analytics_export:      导出数据到 CSV
  - analytics_event:       记录自定义事件
"""

from i18n import _
import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
from .tracker import generate_report, generate_insight_text
from . import models as am

logger = logging.getLogger('analytics.workflow_nodes')


def handle_analytics_report(node_def: dict, input_data: dict) -> dict:
    """
    生成分析报告节点

    配置:
      - days: 报告天数 (默认 7)
      - report_type: "summary" | "full_" (Default ""full")
      - output: "json" | "text_" (Default ""json")
    """
    config = node_def.get('config', {})
    days = config.get('days', 7)
    report_type = config.get('report_type', 'full')
    output = config.get('output', 'json')

    try:
        report = generate_report(days)

        if output == 'text':
            text = generate_insight_text(report)
            return {
                'success': True,
                'output': text,
                'report_type': 'text',
            }

        # JSON 精简/完整
        if report_type == 'summary':
            result = {
                'success': True,
                'summary': report['summary'],
                'period': f'past_{days}_days',
                'generated_at': report['generated_at'],
            }
        else:
            result = {
                'success': True,
                'data': report,
            }
        return result

    except Exception as e:
        return {'success': False, 'error': str(e)}


def handle_analytics_insight(node_def: dict, input_data: dict) -> dict:
    """
    AI 解读报告节点

    将分析报告发给 智能体 生成可读解读。
    需要 input_data 中包含 report 数据，或配置中指定 days。

    配置:
      - days: 报告天数 (默认 7)
      - use_ai: 是否用 AI 解读 (默认 true)
    """
    config = node_def.get('config', {})
    days = config.get('days', 7)

    try:
        # 如果是被工作流串联调用，input_data 可能包含 report
        report = input_data.get('data') if isinstance(input_data, dict) else None
        if not report:
            report = generate_report(days)

        # 生成文字洞察
        text = generate_insight_text(report)

        # AI 增强解读（可选）
        use_ai = config.get('use_ai', True)
        ai_analysis = ''
        if use_ai:
            ai_analysis = _ai_interpret(report, text)

        result = {
            'success': True,
            'summary_text': text,
            'ai_analysis': ai_analysis,
            'report': report if config.get('include_raw', False) else None,
        }
        return result

    except Exception as e:
        return {'success': False, 'error': str(e)}


def handle_analytics_alert_check(node_def: dict, input_data: dict) -> dict:
    """
    执行告警检查节点

    配置:
      - notify: 是否通知 (默认 true)
    """
    config = node_def.get('config', {})
    notify = config.get('notify', True)

    conn = am.get_db()
    try:
        triggered = am.check_alerts(conn)
        result = {
            'success': True,
            'checked_at': datetime.now().isoformat(),
            'triggered_count': len(triggered),
            'triggered': triggered,
        }
        return result
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        conn.close()


def handle_analytics_export(node_def: dict, input_data: dict) -> dict:
    """
    导出数据节点

    配置:
      - type: "trend" | "pages" | "sources" | "geo_" (Default ""trend")
      - days: 天数 (默认 30)
    """
    config = node_def.get('config', {})
    export_type = config.get('type', 'trend')
    days = config.get('days', 30)

    conn = am.get_db()
    try:
        if export_type == 'trend':
            data = am.get_trend(conn, days)
        elif export_type == 'pages':
            data = am.get_page_rank(conn, days, 100)
        elif export_type == 'sources':
            data = am.get_source_analysis(conn, days)
        elif export_type == 'geo':
            data = am.get_geo_distribution(conn, days)
        else:
            return {'success': False, 'error': f'Unknown type: {export_type}'}

        return {
            'success': True,
            'type': export_type,
            'count': len(data),
            'data': data,
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        conn.close()


def handle_analytics_event(node_def: dict, input_data: dict) -> dict:
    """
    记录自定义事件节点（供 Workflow 在工作流执行过程中记录事件）

    配置:
      - event_name: 事件名
      - category: 分类
      - label: 标签
      - value: 数值
    """
    config = node_def.get('config', {})
    event_name = config.get('event_name') or input_data.get('event_name', 'workflow_step')

    try:
        from .tracker import track_event
        event_id = track_event(
            event_name=event_name,
            category=config.get('category', 'workflow'),
            label=config.get('label', ''),
            value=config.get('value', 0),
            path=node_def.get('id', ''),
            service_name='orchestrator',
            metadata={'workflow_node': node_def.get('id', '')},
        )
        return {
            'success': True,
            'event_id': event_id,
            'event_name': event_name,
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


def handle_analytics_cleanup(node_def: dict, input_data: dict) -> dict:
    """清理过期日志节点"""
    config = node_def.get('config', {})
    retention_days = config.get('retention_days', 30)

    conn = am.get_db()
    try:
        deleted = am.cleanup_old_logs(conn, retention_days)
        return {
            'success': True,
            'deleted_logs': deleted,
            'retention_days': retention_days,
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        conn.close()


# ─── AI 解读 ──────────────────────────────────────────────────────────────────

def _resolve_llm_config() -> dict:
    """解析 AI 解读的 LLM 配置：环境变量 > 平台 provider_models 默认配置 > 内置默认值"""
    cfg = {
        'provider': os.environ.get('ANALYTICS_LLM_PROVIDER', ''),
        'model': os.environ.get('ANALYTICS_LLM_MODEL', ''),
        'temperature': float(os.environ.get('ANALYTICS_LLM_TEMPERATURE', '0.5')),
        'max_tokens': int(os.environ.get('ANALYTICS_LLM_MAX_TOKENS', '1024')),
    }
    if cfg['provider'] and cfg['model']:
        return cfg

    # 回退：平台 provider_models 中第一个启用的模型
    try:
        from models import get_db
        with get_db() as conn:
            row = conn.execute(
                """SELECT p.slug AS provider, pm.model_name AS model
                   FROM provider_models pm
                   JOIN providers p ON p.id = pm.provider_id
                   WHERE pm.is_active = 1 AND p.is_active = 1
                   ORDER BY pm.sort_order, pm.id
                   LIMIT 1"""
            ).fetchone()
        if row:
            cfg['provider'] = cfg['provider'] or row['provider']
            cfg['model'] = cfg['model'] or row['model']
    except Exception:
        pass

    # 最终回退到内置默认值
    cfg['provider'] = cfg['provider'] or 'dashscope'
    cfg['model'] = cfg['model'] or 'qwen-turbo'
    return cfg


def _ai_interpret(report: dict, text: str) -> str:
    """
    使用 UnifiedLLM 对统计数据进行专业解读
    复用平台已有的 provider_models 配置
    """
    prompt = _('analytics.ai_prompt', text=text)
    try:
        from agent_matrix.engine import get_gateway
        gw = get_gateway()
        cfg = _resolve_llm_config()
        resp = gw.chat(
            provider=cfg['provider'],
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content': _('analytics.ai_system_prompt')},
                {'role': 'user', 'content': prompt}
            ],
            temperature=cfg['temperature'],
            max_tokens=cfg['max_tokens'],
            module='analytics',
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return ''


# ─── 注册器 ────────────────────────────────────────────────────────────────────

def register_analytics_handlers(engine):
    """
    将所有分析节点处理器注册到 WorkflowEngine

    用法:
        from analytics.workflow_nodes import register_analytics_handlers
        register_analytics_handlers(worker_pool.workflow_engine)
    """
    engine.register_node_handler('analytics_report', handle_analytics_report)
    engine.register_node_handler('analytics_insight', handle_analytics_insight)
    engine.register_node_handler('analytics_alert_check', handle_analytics_alert_check)
    engine.register_node_handler('analytics_export', handle_analytics_export)
    engine.register_node_handler('analytics_event', handle_analytics_event)
    engine.register_node_handler('analytics_cleanup', handle_analytics_cleanup)

    logger.info('Registered 6 custom node processors')


# ─── 快捷方式（创建预设工作流） ────────────────────────────────────────────────

def create_daily_report_workflow(conn) -> int:
    """
    创建每日分析报告工作流定义
    返回 workflow_id
    """
    definition = json.dumps({
        "nodes": [
            {
                "id": "generate_report",
                "type": "analytics_report",
                "name": _("Generate Daily Report"),
                "config": {"days": 1, "report_type": "full", "output": "json"}
            },
            {
                "id": "ai_insight",
                "type": "analytics_insight",
                "name": _("AI Interpretation"),
                "config": {"days": 1, "use_ai": True}
            },
            {
                "id": "notify_admin",
                "type": "notify",
                "name": _("Push Administrator"),
                "config": {
                    "channels": ["notification"],
                    "title": _("📊 Daily Analysis Report")
                }
            }
        ],
        "edges": [
            {"from": "generate_report", "to": "ai_insight"},
            {"from": "ai_insight", "to": "notify_admin"}
        ]
    })

    from orchestrator import models as om
    wf_id = om.create_workflow(
        conn=conn,
        name=_("📊 Daily Analysis Report"),
        description="每天自动生成分析报告并 AI 解读",
        definition=definition,
        is_active=1,
    )
    return wf_id


def create_weekly_report_workflow(conn) -> int:
    """创建周报工作流"""
    definition = json.dumps({
        "nodes": [
            {
                "id": "generate_report",
                "type": "analytics_report",
                "name": _("Generate Weekly Report"),
                "config": {"days": 7, "report_type": "full", "output": "json"}
            },
            {
                "id": "ai_insight",
                "type": "analytics_insight",
                "name": _("Deep AI Interpretation"),
                "config": {"days": 7, "use_ai": True, "include_raw": True}
            },
            {
                "id": "notify_admin",
                "type": "notify",
                "name": _("Push Report"),
                "config": {
                    "channels": ["notification", "email"],
                    "title": _("📊 Weekly Operations Report")
                }
            }
        ],
        "edges": [
            {"from": "generate_report", "to": "ai_insight"},
            {"from": "ai_insight", "to": "notify_admin"}
        ]
    })

    from orchestrator import models as om
    wf_id = om.create_workflow(
        conn=conn,
        name=_("📊 Weekly Operations Report"),
        description="每周自动生成长周期分析报告和 AI 深度解读",
        definition=definition,
        is_active=1,
    )
    return wf_id
