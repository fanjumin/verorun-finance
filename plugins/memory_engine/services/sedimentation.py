#!/usr/bin/env python3
"""Sedimentation service — promote high-value memories into the shared knowledge base.

数据源（两个进化引擎，用户可按 user_profiles.meta.cognitive_engine 选择）:
  - memory_engine.memories               (schema: memory_engine)
  - cogevolution_substrate.curation_events (schema: cogevolution_substrate)

流程:
  run() 定时/手动扫描 → 质量门 → opt-in 隐私门 → PII 二次过滤
       → 知识库去重（标题精确 + 关键词 Jaccard>0.75）
       → 写入 sedimentation_queue(status=pending)
  管理员在后台审核通过后 → 写 knowledge_blocks(source='matrix', scope='user')
       → store_embedding() → 全站 RAG 可检索

插件单向适应系统模块：复用系统现有表/函数，不改任何系统文件。
"""

import hashlib
import json
import logging
import re

logger = logging.getLogger('memory_engine.sedimentation')

# PII 二次过滤委托给 plugins._base.pii（与 extractor 共用，S11b）

# 关键词 → 知识库分类（对齐 cleaner_agent.CATEGORY_LIMITS 的类别集合）
_CATEGORY_RULES = [
    ('company',  ('公司', '企业', '团队', 'company', 'about')),
    ('product',  ('产品', '功能', '平台', 'product', 'feature')),
    ('price',    ('价格', '收费', '套餐', '订阅', 'price', 'pricing')),
    ('tech',     ('技术', '模型', 'api', '部署', 'tech', 'server')),
    ('service',  ('服务', '售后', '支持', 'service', 'support')),
    ('faq',      ('如何', '怎么', '为什么', '什么是', 'how', 'what', 'why')),
    ('industry', ('行业', '市场', 'industry', 'market')),
]
_DEFAULT_CATEGORY = 'general'


class SedimentationService:
    """每日/手动扫描高价值记忆，生成待审沉淀条目。"""

    def __init__(self, config: dict):
        self._config = config or {}

    # ── 入口 ──────────────────────────────────────────────
    def run_daily(self):
        """APScheduler 每日任务。"""
        budget = int(self._config.get('sedimentation_daily_budget', 50))
        return self.run(batch_limit=budget)

    def run(self, batch_limit: int = 50):
        """扫描两个进化引擎的高价值记忆，写入待审队列（幂等）。"""
        if not self._config.get('enable_sedimentation', True):
            return 0
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            count = self._collect_memories(conn, batch_limit)
            count += self._collect_ces(conn, batch_limit - count)
            logger.info('sedimentation run done: %d candidate(s) queued', count)
            return count
        except Exception as e:
            logger.error('sedimentation run failed: %s', e)
            return 0
        finally:
            conn.close()

    # ── 数据源 A：memory_engine.memories ───────────────────
    def _collect_memories(self, conn, limit: int) -> int:
        if limit <= 0:
            return 0
        min_conf = float(self._config.get('sedimentation_fact_min_confidence', 0.9))
        min_q = float(self._config.get('sedimentation_min_quality_score', 0.7))
        min_rating = int(self._config.get('sedimentation_lesson_min_rating', 4))
        rows = conn.execute(
            """
            SELECT m.id, m.owner_id, m.memory_type, m.content, m.confidence,
                   m.quality_score, m.keywords
            FROM memories m
            WHERE m.status = 'active'
              AND m.memory_type IN ('fact', 'preference', 'lesson')
              AND (
                    (m.memory_type = 'lesson' AND EXISTS (
                        SELECT 1 FROM reflexion_logs r
                        WHERE r.lesson = m.content AND r.rating >= ?))
                    OR (m.memory_type = 'lesson' AND NOT EXISTS (
                        SELECT 1 FROM reflexion_logs r
                        WHERE r.lesson = m.content) AND m.quality_score >= ?)
                    OR (m.memory_type IN ('fact', 'preference')
                        AND m.confidence >= ? AND m.quality_score >= ?)
                   )
              AND NOT EXISTS (
                    SELECT 1 FROM sedimentation_queue q
                    WHERE q.source_schema = 'memory_engine'
                      AND q.memory_id = m.id::text)
            LIMIT ?
            """,
            (min_rating, min_q, min_conf, min_q, limit),
        ).fetchall()
        added = 0
        for r in rows:
            if self._enqueue(conn, 'memory_engine', str(r['id']), r['owner_id'],
                             r['memory_type'], r['content'], r['keywords'],
                             r['confidence'], r['quality_score']):
                added += 1
        return added

    # ── 数据源 B：cogevolution_substrate.curation_events ────
    def _collect_ces(self, conn, limit: int) -> int:
        if limit <= 0:
            return 0
        # schema 不存在（CES 未安装/未初始化）时直接跳过
        try:
            chk = conn.execute(
                "SELECT 1 FROM information_schema.tables"
                " WHERE table_schema = 'cogevolution_substrate'"
                " AND table_name = 'curation_events'"
            ).fetchone()
            if not chk:
                return 0
        except Exception:
            return 0
        min_conf = float(self._config.get('sedimentation_fact_min_confidence', 0.9))
        min_q = float(self._config.get('sedimentation_min_quality_score', 0.7))
        rows = conn.execute(
            """
            SELECT c.id, c.owner_id, c.record_type, c.content, c.confidence,
                   c.quality_score, c.keywords
            FROM cogevolution_substrate.curation_events c
            WHERE c.status = 'active'
              AND c.record_type IN ('fact', 'lesson')
              AND c.confidence >= ? AND c.quality_score >= ?
              AND NOT EXISTS (
                    SELECT 1 FROM sedimentation_queue q
                    WHERE q.source_schema = 'cogevolution_substrate'
                      AND q.memory_id = c.id::text)
            LIMIT ?
            """,
            (min_conf, min_q, limit),
        ).fetchall()
        added = 0
        for r in rows:
            if self._enqueue(conn, 'cogevolution_substrate', str(r['id']), r['owner_id'],
                             r['record_type'], r['content'], r['keywords'],
                             r['confidence'], r['quality_score']):
                added += 1
        return added

    # ── 入队（幂等） ───────────────────────────────────────
    def _enqueue(self, conn, source_schema, memory_id, owner_id,
                 memory_type, content, keywords, confidence, quality_score) -> bool:
        content = str(content or '').strip()
        from plugins._base.pii import contains_pii
        if not content or contains_pii(content):
            if content:
                logger.info('sedimentation skipped (PII): %s/%s', source_schema, memory_id)
            return False
        if not self._user_opted_in(owner_id):
            return False
        title = content[:40]
        category = self._categorize(content)
        kw_text = ''
        if keywords:
            if isinstance(keywords, list):
                kw_text = ','.join(str(k) for k in keywords)
            else:
                kw_text = str(keywords)
        if not kw_text:
            kw_text = ','.join(self._naive_keywords(content))
        # 知识库去重（标题精确 + 关键词 Jaccard>0.75，语义对齐 cleaner_agent）
        existing = self._existing_user_blocks()
        if existing and self._is_dup(title, kw_text, existing):
            logger.info('sedimentation skipped (dup in KB): %s', title)
            return False
        digest = hashlib.sha256(
            f"{owner_id}|{content}".encode('utf-8')).hexdigest()
        try:
            conn.execute(
                "INSERT INTO sedimentation_queue"
                " (source_schema, memory_id, content_hash, title, content, keywords,"
                "  category, owner_id, memory_type, confidence, quality_score)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (source_schema, memory_id, content_hash) DO NOTHING",
                (source_schema, memory_id, digest, title, content, kw_text,
                 category, owner_id, memory_type, float(confidence or 0), float(quality_score or 0)),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error('sedimentation enqueue failed: %s', e)
            conn.rollback()
            return False

    # ── 审核通过：写 knowledge_blocks（插件单向适应系统）──
    def approve(self, queue_id: str) -> dict:
        """管理员审核通过：写知识库 + 向量化 + 回写队列状态。"""
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            row = conn.execute(
                "SELECT * FROM sedimentation_queue WHERE id = ?", (queue_id,)
            ).fetchone()
            if not row:
                return {'ok': False, 'error': 'Sedimentation entry not found'}
            if row['status'] != 'pending':
                return {'ok': False, 'error': 'Entry already reviewed'}
            kb_id = self._write_kb(conn, row)
            conn.execute(
                "UPDATE sedimentation_queue SET status='approved', kb_id=?, reviewed_at=now()"
                " WHERE id = ?",
                (kb_id, queue_id),
            )
            conn.commit()
            # 向量化（失败静默，不影响审核结果）
            try:
                from agent_matrix.rag_retriever import store_embedding
                store_embedding(kb_id, row['title'], row['content'])
            except Exception as e:
                logger.warning('store_embedding failed for %s: %s', kb_id, e)
            return {'ok': True, 'kb_id': kb_id}
        except Exception as e:
            logger.error('sedimentation approve failed: %s', e)
            conn.rollback()
            return {'ok': False, 'error': str(e)}
        finally:
            conn.close()

    def reject(self, queue_id: str, note: str = '') -> dict:
        """管理员拒绝：标记 rejected + 备注。"""
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            conn.execute(
                "UPDATE sedimentation_queue SET status='rejected', note=?, reviewed_at=now()"
                " WHERE id = ? AND status = 'pending'",
                (note, queue_id),
            )
            conn.commit()
            return {'ok': True}
        except Exception as e:
            logger.error('sedimentation reject failed: %s', e)
            conn.rollback()
            return {'ok': False, 'error': str(e)}
        finally:
            conn.close()

    # ── 写库 ───────────────────────────────────────────────
    def _write_kb(self, conn, row) -> str:
        """写 knowledge_blocks（source='matrix'，scope='user'，owner_id 保留）。"""
        from agent_matrix.models import get_db as _get_main_db
        kb_id = 'kb_matrix_' + str(row['id'])[:8] + '_' + ''.join(
            re.findall(r'\w', row['title'] or ''))[:10]
        with _get_main_db() as mdb:
            mdb.execute(
                "INSERT INTO public.knowledge_blocks"
                " (id, title, content, keywords, category, priority, source, quality_score,"
                "  scope, owner_id, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())"
                " ON CONFLICT (id) DO NOTHING",
                (kb_id, row['title'], row['content'], row['keywords'] or '',
                 row['category'] or 'general', 5, 'matrix',
                 float(row['quality_score'] or 0.5), 'user', row['owner_id']),
            )
            mdb.commit()
        return kb_id

    # ── 工具 ───────────────────────────────────────────────
    def _user_opted_in(self, owner_id) -> bool:
        """隐私门：默认取配置，用户可在 user_profiles.meta 覆盖（与 prompt_injector 一致）。"""
        if not owner_id:
            return False
        default = self._config.get('memory_opt_in_default', True)
        try:
            from agent_matrix.models import get_db
            with get_db() as conn:
                row = conn.execute(
                    "SELECT meta FROM public.user_profiles WHERE user_id = %s",
                    (owner_id,),
                ).fetchone()
            if not row:
                return default
            meta = row['meta'] or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            return bool(meta.get('memory_opt_in', default))
        except Exception:
            return default

    def _existing_user_blocks(self):
        """知识库去重参考集：当前 user scope 有效条目。"""
        try:
            from agent_matrix.models import get_db
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT id, title, content, keywords, category, source"
                    " FROM public.knowledge_blocks"
                    " WHERE deleted_at IS NULL AND scope = 'user'"
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    @staticmethod
    def _is_dup(title, keywords, existing) -> bool:
        t = (title or '').strip().lower()
        for e in existing:
            if (e['title'] or '').strip().lower() == t:
                return True
        if keywords:
            for e in existing:
                if SedimentationService._jaccard(keywords, e.get('keywords') or '') > 0.75:
                    return True
        return False

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        sa = set((a or '').split(','))
        sb = set((b or '').split(','))
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    @staticmethod
    def _categorize(content: str) -> str:
        low = (content or '').lower()
        for cat, kws in _CATEGORY_RULES:
            if any(k in low for k in kws):
                return cat
        return _DEFAULT_CATEGORY

    @staticmethod
    def _naive_keywords(text: str) -> list:
        kws = set(re.findall(r'[\u4e00-\u9fff]{2,}', text))
        kws.update(w.lower() for w in re.findall(r'[a-z]{2,}', text.lower()))
        return list(kws)[:12]


# ── 模块级服务单例（插件 on_enable 时绑定，routes 手动触发用）──

_active_service = None


def set_service(svc):
    global _active_service
    _active_service = svc


def get_service():
    global _active_service
    if _active_service is None:
        _active_service = SedimentationService({})
    return _active_service
