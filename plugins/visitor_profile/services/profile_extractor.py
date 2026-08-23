#!/usr/bin/env python3
"""
visitor_profile/services/profile_extractor.py — 画像提取服务
=============================================================
通过 Agent Matrix 的 profiler Agent（tier=cheap）对访客行为事件做
语义提取，产出结构化画像记忆（memories）并更新访客摘要。

异步流水线（process_task_async → _process_task）:
  事件加载 → PII 过滤 → profiler Agent 调用 → JSON 解析 →
  embedding 生成（防御式，失败降级）→ 记忆入库 → 访客摘要更新
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from plugin_manager.logger import get_plugin_logger
from plugins._base.db import get_raw_connection

from ..models import (
    EventLogModel, MemoryModel, ExtractionTaskModel,
    VisitorModel, get_agent,
)
from .pii_filter import PIIFilter

logger = get_plugin_logger('visitor_profile')

# 默认模型配置（无 provider_models / system_config 配置时的兜底）
_DEFAULT_PROVIDER = 'dashscope'
_DEFAULT_MODEL = 'qwen-turbo'


class ProfileExtractor:
    """
    编排画像提取流水线：事件批处理 → PII 清洗 → Agent 调用 →
    结果存储 → embedding 生成。
    """

    def __init__(self, plugin):
        self.plugin = plugin
        self._executor = None  # Lazy init ThreadPoolExecutor

    # ── 异步入口 ──────────────────────────────────────────────────

    def process_task_async(self, visitor_id, event_ids):
        """提交提取任务到后台线程池。"""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix='profiler')
        self._executor.submit(self._process_task, visitor_id, event_ids)

    # ── 核心流水线 ────────────────────────────────────────────────

    def _process_task(self, visitor_id, event_ids):
        """核心提取流水线（后台线程执行）。"""
        start_time = time.time()
        task_id = None
        try:
            # 定位任务并标记 processing
            task = ExtractionTaskModel.get_latest(visitor_id, event_ids)
            if not task:
                return
            task_id = task['id']
            ExtractionTaskModel.mark_processing(task_id)

            # 1. 加载事件
            events = EventLogModel.get_events_by_ids(event_ids)
            if not events:
                return

            # 2. PII 过滤（入库前阶段）
            if self.plugin.get_config_value('pii_filter_enabled', True):
                for e in events:
                    if e.get('element_text'):
                        e['element_text'] = PIIFilter.clean(e['element_text'])
                    if e.get('event_data'):
                        e['event_data'] = PIIFilter.clean_dict(e['event_data'])

            # 3. 调用 profiler Agent
            profile_result = self._invoke_profiler_agent(visitor_id, events)
            if not profile_result:
                ExtractionTaskModel.mark_failed(task_id, 'Agent returned empty result')
                return

            # 4. 存储画像记忆（snippets → memories）
            memory_ids = []
            for snippet in profile_result.get('profile_snippets', []):
                memory_id = self._store_memory(visitor_id, snippet, task_id)
                if memory_id:
                    memory_ids.append(memory_id)

            # 5. 更新访客摘要
            summary_update = profile_result.get('visitor_summary_update')
            if summary_update:
                self._update_visitor_summary(visitor_id, summary_update)

            # 6. 标记任务完成
            elapsed_ms = int((time.time() - start_time) * 1000)
            ExtractionTaskModel.mark_completed(task_id, memory_ids, elapsed_ms)
            logger.info(
                'Profile extracted: visitor=%s, events=%s, memories=%s, time=%sms',
                visitor_id, len(events), len(memory_ids), elapsed_ms)

        except Exception as e:
            logger.error('Extraction failed: %s', e)
            if task_id:
                try:
                    ExtractionTaskModel.mark_failed(task_id, str(e))
                except Exception:
                    pass

    # ── Agent 调用 ────────────────────────────────────────────────

    def _resolve_profiler_config(self):
        """解析 profiler Agent 的模型配置（防御式三级回退）。

        1. 本插件 agent_registry 中 profiler 的 provider/model_name
        2. system_config 中 model_tier_cheap（若后台配置存在）
        3. 默认 dashscope/qwen-turbo
        """
        # 一级：本地 agent_registry
        try:
            agent = get_agent('profiler')
            if agent and agent.get('provider') and agent.get('model_name'):
                return {
                    'provider': agent['provider'],
                    'model': agent['model_name'],
                }
        except Exception as e:
            logger.debug('profiler config from agent_registry failed: %s', e)

        # 二级：system_config 的 tier→模型映射（model_tier_cheap）
        try:
            from models import get_db
            with get_db() as conn:
                row = conn.execute(
                    "SELECT value FROM system_config WHERE key=%s",
                    ('model_tier_cheap',)
                ).fetchone()
            if row and row.get('value'):
                val = str(row['value']).strip()
                if val:
                    # 支持 "provider:model" 或纯 "model" 两种格式
                    if ':' in val:
                        provider, model = val.split(':', 1)
                        return {'provider': provider.strip(), 'model': model.strip()}
                    return {'provider': _DEFAULT_PROVIDER, 'model': val}
        except Exception as e:
            logger.debug('model_tier_cheap lookup failed: %s', e)

        # 三级：默认
        return {'provider': _DEFAULT_PROVIDER, 'model': _DEFAULT_MODEL}

    def _invoke_profiler_agent(self, visitor_id, events):
        """通过 Agent Matrix 的 UnifiedLLM 调用 profiler Agent。"""
        from agent_matrix.engine import UnifiedLLM

        cfg = self._resolve_profiler_config()
        events_text = json.dumps(events, ensure_ascii=False, indent=2)
        user_message = (
            f"Analyze the following visitor behavior events and extract\n"
            f"structured profile insights.\n\n"
            f"Visitor ID: {visitor_id}\n"
            f"Events: {events_text}\n"
        )

        try:
            llm = UnifiedLLM()
            # chat() 返回字符串（非 dict），raw_response=False
            result = llm.chat(
                messages=[
                    {"role": "system",
                     "content": self._get_profiler_system_prompt()},
                    {"role": "user", "content": user_message},
                ],
                provider=cfg['provider'],
                model=cfg['model'],
                temperature=0.3,
                max_tokens=2000,
                module='visitor_profile',
            )
            if not result:
                return None
            content = result
            # 剥离 markdown 代码围栏
            if '```json' in content:
                content = content.split('```json')[1].split('```')[0]
            elif '```' in content:
                content = content.split('```')[1].split('```')[0]
            return json.loads(content.strip())
        except Exception as e:
            logger.error('Agent invocation failed: %s', e)
            return None

    def _get_profiler_system_prompt(self):
        """读取 profiler Agent 的 System Prompt。"""
        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'agents', 'profiler_prompt.md')
        try:
            with open(prompt_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            logger.warning('Failed to read profiler prompt: %s', e)
            return 'You are the Visitor Profile Analyzer (profiler).'

    # ── 结果存储 ──────────────────────────────────────────────────

    def _get_embedding(self, text):
        """生成 embedding（防御式：内核 API 缺失时返回 None 降级）。"""
        try:
            from agent_matrix.engine import UnifiedLLM
            llm = UnifiedLLM()
            embedding = llm.get_embedding(text, module='visitor_profile')
            if embedding is not None and hasattr(embedding, 'tolist'):
                embedding = embedding.tolist()
            return embedding
        except Exception as e:
            logger.warning('Embedding generation failed: %s', e)
            return None

    def _store_memory(self, visitor_id, snippet, task_id):
        """将画像片段存为带向量 embedding 的记忆。"""
        content = snippet.get('content', {})
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except Exception:
                content = {'summary': content}
        memory_type = snippet.get('memory_type', 'behavior_profile')
        confidence = snippet.get('confidence', 0.5)

        summary_text = ''
        if isinstance(content, dict):
            summary_text = content.get('summary', '')
            if not summary_text:
                summary_text = content.get('intent', '')
        if not summary_text:
            summary_text = json.dumps(content, ensure_ascii=False)

        # embedding 生成（失败返回 None → 纯文本降级存储）
        embedding = self._get_embedding(summary_text)

        retention_days = self.plugin.get_config_value('retention_days', 365)
        try:
            return MemoryModel.insert(
                visitor_id=visitor_id,
                memory_type=memory_type,
                content=content,
                embedding=embedding,
                confidence=float(confidence),
                source_event_id=task_id,
                retention_days=retention_days,
            )
        except Exception as e:
            logger.error('Memory insert failed: %s', e)
            return None

    def _update_visitor_summary(self, visitor_id, summary):
        """更新访客的 profile_summary 与 tags（合并去重）。"""
        try:
            visitor = VisitorModel.get_by_id(visitor_id)
            existing_tags = (visitor or {}).get('tags') or []
            if isinstance(existing_tags, str):
                try:
                    existing_tags = json.loads(existing_tags)
                except Exception:
                    existing_tags = []
            new_tags = summary.get('interest_tags', [])
            merged = list(dict.fromkeys(list(existing_tags) + list(new_tags)))
            VisitorModel.update_summary(visitor_id, summary, merged)
        except Exception as e:
            logger.error('Visitor summary update failed: %s', e)
