#!/usr/bin/env python3
"""
Visitor Profile Engine — 访客画像引擎插件
============================================
AI 驱动的访客行为画像：通过前端埋点 SDK 采集行为事件 → 持久化到
visitor_profile schema → 由 profiler Agent（tier=cheap）异步语义提取
画像记忆（pgvector 存储）→ 通过 before_prompt_resolve Filter 将画像
注入 AI 对话的 System Prompt，实现千人千面。

生命周期:
  install   → 执行 migrations/v1.0.0_initial.sql（建 Schema + 表）
  enable    → 注册 EventBus 监听 + before_prompt_resolve Filter
              + profiler Agent 注册 + 运行时检测 analytics/chatbot
  disable   → 反注册事件监听与 Filter、注销 Agent
  uninstall → DROP SCHEMA visitor_profile CASCADE（零残留）
"""
import json
import logging
import os

from plugin_manager.base import BasePlugin
from plugin_manager.hooks import get_hook_registry
from plugin_manager.event_bus import get_event_bus
from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('visitor_profile')

# 自定义业务事件（EventName 无内置的 web 行为事件常量）
VISITOR_ACTIVITY_EVENT = 'visitor.activity'
# Filter 钩子点（与 memory_engine 共用）
BEFORE_PROMPT_RESOLVE = 'before_prompt_resolve'

_SCHEMA = 'visitor_profile'


def ensure_schema() -> bool:
    """幂等建表：执行 migrations/v1.0.0_initial.sql（CREATE TABLE IF NOT EXISTS）。

    on_install / on_enable 共用。此前建表仅在 on_install 触发，磁盘发现直接启用
    的插件未执行 install，导致 agent_registry 等表缺失 → upsert_agent 查询 aborted
    → PG 连接池耗尽。
    """
    from plugins._base.db import get_raw_connection
    try:
        migration_path = os.path.join(
            os.path.dirname(__file__), 'migrations', 'v1.0.0_initial.sql')
        with open(migration_path, 'r', encoding='utf-8') as f:
            sql = f.read()
        conn = get_raw_connection()
        try:
            cur = conn.cursor()
            cur.execute("SET search_path TO %s, public" % _SCHEMA)
            cur.execute(sql)
            conn.commit()
        finally:
            conn.close()
        # 记录 schema 版本（§10.6）
        try:
            from .models import set_schema_version
            set_schema_version(VisitorProfilePlugin.version)
        except Exception:
            pass
        logger.info('visitor_profile schema ensured')
        return True
    except Exception as e:
        logger.error('schema init failed: %s', e)
        return False


class VisitorProfilePlugin(BasePlugin):
    name = 'Visitor Profile Engine'
    identifier = 'visitor_profile'
    version = '1.1.0'
    description = 'AI-powered visitor behavior profiling engine with LLM Agent semantic extraction, pgvector storage, and dynamic persona injection'
    author = 'VeroRun'

    # ── 生命周期 ──────────────────────────────────────────────────

    def on_install(self, registry=None) -> bool:
        """安装时执行 migrations/v1.0.0_initial.sql（建 Schema + 表，幂等）。"""
        return ensure_schema()

    def on_enable(self, registry=None) -> bool:
        """启用时：注册事件监听 + Filter + Agent，并检测推荐插件。"""
        from .services.profile_extractor import ProfileExtractor
        from .services.profile_retriever import ProfileRetriever

        # 确保 schema 已建（磁盘发现直接启用的插件此前未走 install 建表；
        # 表缺失会导致 agent_registry 查询 aborted → PG 连接池耗尽）
        ensure_schema()

        # 运行时检测推荐插件（松耦合：检测到即增强，未检测到无影响）
        self._detect_companion_plugins(registry)

        # 初始化服务
        self.extractor = ProfileExtractor(self)
        self.retriever = ProfileRetriever(self)

        # 1. 监听访客行为事件 → 触发画像提取
        self._bus = get_event_bus()
        self._bus.on(VISITOR_ACTIVITY_EVENT, self._on_visitor_activity)

        # 2. 注册 Filter Hook → 注入画像到 System Prompt
        hooks = get_hook_registry()
        existing = hooks.list_filters(BEFORE_PROMPT_RESOLVE)
        already = any(
            h.get('identifier') == self.identifier
            for hooks_list in existing.values()
            for h in hooks_list
        )
        if not already:
            hooks.add_filter(
                BEFORE_PROMPT_RESOLVE,
                self._inject_visitor_persona,
                priority=10,
                identifier=self.identifier,
            )

        # 3. 注册 profiler Agent 到本地 agent_registry
        self._register_agents()

        logger.info('Visitor Profile Engine enabled')
        return True

    def on_disable(self, registry=None) -> bool:
        """禁用时：反注册事件监听与 Filter，注销 Agent。"""
        try:
            if getattr(self, '_bus', None):
                self._bus.off(VISITOR_ACTIVITY_EVENT, self._on_visitor_activity)
        except Exception as e:
            logger.warning('failed to unsubscribe event: %s', e)
        try:
            get_hook_registry().remove_filter(
                BEFORE_PROMPT_RESOLVE,
                callback=self._inject_visitor_persona,
                identifier=self.identifier,
            )
        except Exception:
            pass
        self._unregister_agents()
        logger.info('Visitor Profile Engine disabled')
        return True

    def on_uninstall(self, registry=None) -> bool:
        """卸载时：DROP SCHEMA visitor_profile CASCADE（零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            conn = get_raw_connection()
            try:
                cur = conn.cursor()
                cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % _SCHEMA)
                conn.commit()
            finally:
                conn.close()
            logger.info('visitor_profile schema dropped')
            return True
        except Exception as e:
            logger.error('schema drop failed: %s', e)
            return False

    def register_routes(self):
        """注册管理后台 Blueprint。"""
        from .routes import visitor_profile_bp
        return [visitor_profile_bp]

    # ── 推荐插件运行时检测 ────────────────────────────────────────

    def _detect_companion_plugins(self, registry):
        """检测 analytics / chatbot 是否启用，输出到日志（松耦合）。"""
        def _is_enabled(identifier: str) -> bool:
            mgr = registry or getattr(self, 'manager', None)
            if mgr is not None and hasattr(mgr, 'is_enabled'):
                try:
                    return bool(mgr.is_enabled(identifier))
                except Exception:
                    return False
            return False

        analytics_on = _is_enabled('analytics')
        chatbot_on = _is_enabled('chatbot')
        logger.info(
            'Companion plugins detected — analytics: %s, chatbot: %s',
            analytics_on, chatbot_on)

    # ── 事件处理 ──────────────────────────────────────────────────

    def _on_visitor_activity(self, **kwargs):
        """处理访客行为事件：持久化 → 触发画像提取。"""
        try:
            event_data = kwargs.get('event_data') or kwargs
            if not isinstance(event_data, dict):
                return
            self._persist_event(event_data)
            visitor_id = event_data.get('visitor_id')
            if visitor_id:
                self._maybe_trigger_extraction(visitor_id)
        except Exception as e:
            logger.error('Failed to process event: %s', e)

    def _persist_event(self, event_data):
        """写入 event_log 表 + upsert visitors 表。"""
        from .models import EventLogModel, VisitorModel
        EventLogModel.insert(event_data)
        VisitorModel.upsert_from_event(event_data)

    def _maybe_trigger_extraction(self, visitor_id):
        """事件累积达到阈值时创建提取任务并异步执行（默认每 5 条）。"""
        if not self.get_config_value('profile_extraction_enabled', True):
            return
        from .models import EventLogModel, ExtractionTaskModel

        threshold = int(self.get_config_value('extraction_batch_size', 5))
        max_events = int(self.get_config_value('extraction_max_events', 20))

        count = EventLogModel.count_unprocessed(visitor_id)
        if count < threshold:
            return

        event_ids = EventLogModel.get_unprocessed_event_ids(
            visitor_id, max_events=max_events)
        if not event_ids:
            return

        # 创建任务 → 标记事件已处理 → 异步提取
        ExtractionTaskModel.create(visitor_id, event_ids)
        EventLogModel.mark_processed(event_ids)
        self.extractor.process_task_async(visitor_id, event_ids)

    # ── Filter Hook: 画像注入 ─────────────────────────────────────

    def _inject_visitor_persona(self, value, **kwargs):
        """Filter 回调 (value, **kwargs) -> value。

        将当前访客画像追加到 prompt 文本末尾（memory_engine 同语义）。
        """
        prompt = value
        try:
            ctx = kwargs.get('ctx') or {}
            visitor_id = ctx.get('visitor_id')
            if not visitor_id:
                return prompt

            profile_block = self.retriever.retrieve_context_block(
                visitor_id,
                top_k=int(self.get_config_value('semantic_search_top_k', 5)))
            if not profile_block:
                return prompt

            append_text = (
                "\n[Current Visitor Dynamic Persona]\n"
                "The following is an AI-generated profile of the current visitor\n"
                "based on their recent behavior on our website. Use this context\n"
                "to personalize your response:\n\n"
                f"{profile_block}\n\n"
                "Adapt your tone, detail level, and recommendations based on\n"
                "this profile. Do NOT explicitly mention \"according to your profile\"\n"
                "or reveal that you have this information.\n"
            )
            logger.info('Profile injected for visitor %s', visitor_id)
            return f"{prompt}\n\n=== Visitor Persona ===\n{append_text}==="
        except Exception as e:
            logger.warning('Persona injection skipped: %s', e)
            return prompt

    # ── Agent 注册 ────────────────────────────────────────────────

    def _register_agents(self):
        """读取 plugin.json agents[] 并注册 profiler 到本地 agent_registry。"""
        try:
            from .models import upsert_agent
            metadata = getattr(self, 'plugin_info', None)
            metadata = getattr(metadata, 'metadata', None) or {}
            agents = metadata.get('agents', []) if isinstance(metadata, dict) else []
            if not agents:
                return

            prompt_path = os.path.join(
                os.path.dirname(__file__), agents[0].get('prompt_file', ''))
            system_prompt = ''
            if os.path.exists(prompt_path):
                with open(prompt_path, 'r', encoding='utf-8') as f:
                    system_prompt = f.read().strip()

            agent = agents[0]
            upsert_agent(
                name=agent['name'],
                identifier=agent.get('identifier', ''),
                role_type=agent.get('role_type', 'sub'),
                description=f"{agent['name']} — {agent.get('domain', 'visitor_profiling')}",
                domain=agent.get('domain', 'visitor_profiling'),
                provider='dashscope',
                model_name='qwen-turbo',
                system_prompt=system_prompt,
                capabilities=json.dumps(agent.get('capabilities', [])),
                is_active=1 if agent.get('enabled_by_default', True) else 0,
            )
        except Exception as e:
            self.log(f'Register agents failed: {e}', 'warning')

    def _unregister_agents(self):
        """注销本地 agent_registry（禁用/卸载时调用，零残留）。"""
        try:
            from .models import unregister_agents
            n = unregister_agents()
            self.log(f'Agents unregistered: {n}')
        except Exception as e:
            self.log(f'Unregister agents failed: {e}', 'warning')

    # ── Dashboard 统计 ────────────────────────────────────────────

    def get_dashboard_stats(self) -> dict:
        """返回插件卡片仪表盘统计（plugin.json dashboard.stats 声明对应）。"""
        from .models import (
            VisitorModel, MemoryModel, ExtractionTaskModel,
        )
        try:
            return {
                'total_visitors': VisitorModel.count(),
                'total_events_24h': VisitorModel.count_events_24h(),
                'profiles_extracted_24h': MemoryModel.count_created_24h(),
                'avg_extraction_time_ms': ExtractionTaskModel.avg_processing_time_24h(),
            }
        except Exception as e:
            logger.warning('Get dashboard stats failed: %s', e)
            return {
                'total_visitors': 0,
                'total_events_24h': 0,
                'profiles_extracted_24h': 0,
                'avg_extraction_time_ms': 0,
            }
