#!/usr/bin/env python3
"""Reflexion service: learn from task outcomes through the curator agent."""

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger('memory_engine.reflexion')


def _keywords(text: str) -> list:
    """Shared keyword helper (mirrors extractor; kept local to avoid circular import)."""
    kws = set(re.findall(r'[\u4e00-\u9fff]{2,}', text))
    kws.update(w.lower() for w in re.findall(r'[a-z]{2,}', text.lower()))
    return list(kws)[:12]


class ReflexionService:
    """Decide when to reflect, run the curator in reflexion mode, persist lessons."""

    def __init__(self, config: dict):
        self._config = config or {}
        self._pool = ThreadPoolExecutor(max_workers=1)
        # 反思任务自身也会经过 AgentRunner → emit AGENT_TASK_COMPLETED，
        # 该标志防止"反思失败→再次反思"的无限递归循环。
        self._reflecting = False

    def on_task_completed(self, **kwargs):
        """Event handler for AGENT_TASK_COMPLETED (action hook)."""
        if self._reflecting:
            return
        if not self._config.get('enable_reflexion', True):
            return
        task = kwargs.get('task') or {}
        result = kwargs.get('result') or {}
        agent_id = kwargs.get('agent_id') or ''
        if not agent_id or not task:
            return
        failed = bool(result.get('failed'))
        retries = int(result.get('retries') or 0)
        confidence = float(result.get('confidence') or 0.0)
        if self._config.get('reflexion_failure_only', True) and not failed:
            return
        if not failed and confidence >= float(self._config.get('reflexion_min_confidence', 0.4)):
            return
        self._pool.submit(self._reflect, task, result, agent_id, failed, retries)

    def _reflect(self, task, result, agent_id, failed, retries):
        """Reflexion 入口：置位防循环标志后执行，异常统一记录。"""
        if self._reflecting:
            return
        self._reflecting = True
        try:
            self._reflect_impl(task, result, agent_id, failed, retries)
        except Exception as e:
            logger.error('reflexion run failed: %s', e)
        finally:
            self._reflecting = False

    def _reflect_impl(self, task, result, agent_id, failed, retries):
        """Run curator reflexion mode and persist structured output."""
        try:
            agent_config = self._load_curator_config()
            if not agent_config:
                return
            from agent_matrix.agent_runner import AgentRunner
            runner = AgentRunner(agent_config)
            payload = json.dumps({
                'query': task.get('user_query') or task.get('query'),
                'result': str(result)[:2000],
                'failed': failed,
                'retries': retries,
            }, ensure_ascii=False)
            resp = runner.execute({
                'title': 'Memory Reflexion',
                'description': payload,
            })
            parsed = self._parse_reflexion(resp)
            if not parsed:
                return
            from ..models import get_memory_engine_db
            conn = get_memory_engine_db()
            try:
                conn.execute(
                    "INSERT INTO reflexion_logs"
                    " (agent_id, task_id, trigger, success, user_query,"
                    "  issue, lesson, action, rating, tokens_used)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (agent_id, task.get('task_id') or '', 'task_completed',
                     not failed, str(task.get('user_query') or '')[:500],
                     parsed['issue'], parsed['lesson'], parsed['action'],
                     parsed['rating'], 0),
                )
                if parsed['lesson']:
                    self._store_lesson(conn, agent_id, task, parsed['lesson'])
                conn.commit()
            except Exception as e:
                logger.error('reflexion persist failed: %s', e)
                conn.rollback()
            finally:
                conn.close()
        except Exception as e:
            logger.error('reflexion run failed: %s', e)

    def _parse_reflexion(self, resp) -> dict:
        """Extract {issue, lesson, action, rating} from the curator JSON output."""
        text = ''
        if isinstance(resp, tuple) and resp:
            text = str(resp[0])
        elif isinstance(resp, dict):
            text = str(resp.get('response') or resp.get('result') or resp.get('output') or '')
        try:
            data = json.loads(text)
        except (ValueError, AttributeError):
            return {}
        return {
            'issue': str(data.get('issue', ''))[:500],
            'lesson': str(data.get('lesson', ''))[:500],
            'action': str(data.get('action', ''))[:500],
            'rating': max(1, min(5, int(data.get('rating') or 3))),
        }

    def _store_lesson(self, conn, agent_id, task, lesson: str):
        """Persist the lesson as a durable memory for the owner."""
        owner_id = str(task.get('user_id') or '')
        if not owner_id:
            return
        digest = hashlib.sha256(f"{owner_id}|{lesson}".encode('utf-8')).hexdigest()
        conn.execute(
            "INSERT INTO memories"
            " (owner_type, owner_id, agent_id, memory_type, content,"
            "  keywords, confidence, source, content_hash)"
            " VALUES ('user', ?, ?, 'lesson', ?, ?, 0.8, 'reflexion', ?)"
            " ON CONFLICT (content_hash) DO NOTHING",
            (owner_id, agent_id, lesson, _keywords(lesson), digest),
        )

    def _load_curator_config(self) -> dict:
        from agent_matrix.models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM agent_matrix"
                " WHERE slug = 'memory_curator' AND is_active = 1"
            ).fetchone()
        return dict(row) if row else {}
