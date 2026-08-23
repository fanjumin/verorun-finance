#!/usr/bin/env python3
"""
visitor_profile/services/profile_retriever.py — 画像检索服务
=============================================================
将访客画像检索为可注入 System Prompt 的文本块。

检索策略（retrieve_context_block）:
  1. 访客摘要（visitors.profile_summary / tags / 位置等）
  2. 语义检索（pgvector）→ 候选画像记忆；embedding 不可用时降级为
     按时间倒序（created_at DESC）的时间排序检索
  3. 混合排序：语义相似度与时效性加权，返回 top_k 条格式化文本
"""
import json
import logging

from plugin_manager.logger import get_plugin_logger
from plugins._base.db import get_raw_connection

from ..models import MemoryModel

logger = get_plugin_logger('visitor_profile')


class ProfileRetriever:
    """
    检索访客画像并构建上下文块，供 _inject_visitor_persona 使用。
    """

    def __init__(self, plugin):
        self.plugin = plugin

    # ── 语义检索（pgvector，防御式） ──────────────────────────────

    def semantic_search(self, query_text, visitor_id=None, top_k=5):
        """语义相似度检索（embedding 失败时返回空列表）。"""
        try:
            from agent_matrix.engine import UnifiedLLM
            llm = UnifiedLLM()
            query_embedding = llm.get_embedding(query_text, module='visitor_profile')
            if not query_embedding:
                return []
        except Exception as e:
            logger.warning('semantic_search embedding failed: %s', e)
            return []
        try:
            return MemoryModel.semantic_search(
                query_embedding, visitor_id=visitor_id, top_k=top_k)
        except Exception as e:
            logger.warning('semantic_search query failed: %s', e)
            return []

    # ── 时间排序检索（embedding 不可用时的降级路径） ──────────────

    def temporal_search(self, visitor_id, top_k=5):
        """按时间倒序取最近画像记忆（无需 embedding）。"""
        try:
            return MemoryModel.get_active_by_visitor(visitor_id, limit=top_k)
        except Exception as e:
            logger.warning('temporal_search failed: %s', e)
            return []

    # ── 上下文块构建（主入口） ────────────────────────────────────

    def retrieve_context_block(self, visitor_id, top_k=5):
        """构建人类可读的画像上下文块，用于 prompt 注入。

        返回格式化字符串；访客不存在或无画像时返回 None。
        """
        conn = get_raw_connection()
        try:
            cur = conn.cursor()
            cur.execute("SET search_path TO visitor_profile, public")

            # 1. 访客摘要
            cur.execute('''
                SELECT profile_summary, tags, total_visits,
                       last_seen_at, country, city
                FROM visitor_profile.visitors
                WHERE visitor_id = %s
            ''', (visitor_id,))
            visitor_row = cur.fetchone()
            if not visitor_row:
                return None

            summary, tags, visits, last_seen, country, city = visitor_row
            if isinstance(summary, str):
                try:
                    summary = json.loads(summary)
                except Exception:
                    summary = {}
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []

            # 2. 画像记忆：先语义检索（范围限定当前访客），失败/为空降级时间排序
            memories = self.semantic_search(
                json.dumps(summary, ensure_ascii=False) if summary else 'recent behavior',
                visitor_id=visitor_id, top_k=top_k)
            if not memories:
                memories = self.temporal_search(visitor_id, top_k=top_k)
            memories = memories[:top_k]

            # 3. 构建文本块
            lines = []
            if isinstance(summary, dict):
                if summary.get('primary_intent'):
                    lines.append(
                        f"- Primary Intent: {summary.get('primary_intent')}")
                if summary.get('engagement_level'):
                    lines.append(
                        f"- Engagement Level: {summary.get('engagement_level')}")
                if summary.get('likely_buyer_stage'):
                    lines.append(
                        f"- Buyer Stage: {summary.get('likely_buyer_stage')}")

            if tags:
                tags_list = tags if isinstance(tags, list) else []
                if tags_list:
                    lines.append(f"- Interest Tags: {', '.join(map(str, tags_list[:10]))}")

            if visits is not None:
                lines.append(f"- Total Visits: {visits}")
            if country:
                location = str(country)
                if city:
                    location += f", {city}"
                lines.append(f"- Location: {location}")

            # 画像记忆摘要（含置信度）
            if memories:
                lines.append("\nRecent Behavioral Insights:")
                for m in memories:
                    content = m.get('content') or {}
                    if isinstance(content, str):
                        try:
                            content = json.loads(content)
                        except Exception:
                            content = {}
                    summary_text = content.get('summary', '') if isinstance(content, dict) else ''
                    if not summary_text and isinstance(content, dict):
                        summary_text = content.get('intent', '')
                    if summary_text:
                        confidence = m.get('confidence') or 0
                        conf_str = f" (confidence: {float(confidence):.0%})" if confidence else ""
                        lines.append(
                            f"  [{m.get('memory_type', 'behavior_profile')}] "
                            f"{summary_text}{conf_str}")

            return '\n'.join(lines) if lines else None

        finally:
            conn.close()
