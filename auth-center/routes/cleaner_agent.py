#!/usr/bin/env python3
"""Cleaner Agent — data cleaning agent
   Admin submits raw content → AI cleaning → knowledge_blocks insertion → site-wide AI auto-discovery
   Can be called directly by Agent Matrix via process_clean_content()
"""
from i18n import _
import sys, os, json, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request
from models import get_db

cleaner_bp = Blueprint('cleaner', __name__, url_prefix='/admin/cleaner')

CLEANER_AGENT_NAME = 'Data Cleaner Agent'
CLEANER_AGENT_DOMAIN = 'cleaner'


def _require_admin():
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        return None, (jsonify({'success': False, 'error': _('Please login first')}), 401)
    from services.jwt_service import validate_token
    payload = validate_token(token)
    if not payload:
        return None, (jsonify({'success': False, 'error': _('Invalid Token')}), 401)
    if not payload.get('is_admin'):
        return None, (jsonify({'success': False, 'error': _('Requires admin permissions')}), 403)
    return payload, None


def _get_existing_for_dedup(scope: str = None):
    """Get existing KB titles + keywords for dedup and conflict detection, filtered by scope"""
    try:
        with get_db() as conn:
            sql = "SELECT id, title, content, keywords, category, source FROM knowledge_blocks WHERE deleted_at IS NULL"
            params = []
            if scope:
                sql += " AND scope=%s"
                params.append(scope)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def _jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two keyword lists"""
    set_a = set((a or '').split(','))
    set_b = set((b or '').split(','))
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _title_similarity(title1: str, title2: str) -> float:
    """Simple title similarity (character-level Jaccard)"""
    t1 = (title1 or '').strip().lower()
    t2 = (title2 or '').strip().lower()
    if not t1 or not t2:
        return 0.0
    set1 = set(t1)
    set2 = set(t2)
    return len(set1 & set2) / len(set1 | set2)


def _dedup_check(new_title: str, new_keywords: str, existing: list) -> tuple:
    """
    Two-level dedup check.
    Returns (is_duplicate: bool, existing_entry: dict or None, reason: str)
    """
    new_title_lower = (new_title or '').strip().lower()
    new_kw = new_keywords or ''

    # Level 1a: exact title match
    for entry in existing:
        if (entry['title'] or '').strip().lower() == new_title_lower:
            return True, entry, 'title_exact_match'

    # Level 1b: keyword Jaccard > 0.75
    if new_kw:
        for entry in existing:
            jac = _jaccard_similarity(new_kw, entry.get('keywords', ''))
            if jac > 0.75:
                return True, entry, f'keyword_jaccard_{jac:.2f}'

    return False, None, ''


CATEGORY_LIMITS = {
    'company': 30, 'product': 50, 'price': 20, 'tech': 50,
    'service': 30, 'faq': 100, 'industry': 30, 'general': 50,
}


def _evict_if_over_limit(category: str):
    """Evict auto entries when category exceeds limit (protect manual entries)"""
    try:
        limit = CATEGORY_LIMITS.get(category, 50)
        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) as c FROM knowledge_blocks WHERE category=%s AND deleted_at IS NULL",
                (category,)
            ).fetchone()['c']

            if count <= limit:
                return

            # Find lowest priority auto entry
            row = conn.execute(
                """SELECT id, title FROM knowledge_blocks
                   WHERE category=%s AND source='auto' AND deleted_at IS NULL
                   ORDER BY priority ASC, quality_score ASC LIMIT 1""",
                (category,)
            ).fetchone()

            if row:
                conn.execute(
                    "UPDATE knowledge_blocks SET deleted_at=NOW() WHERE id=%s",
                    (row['id'],)
                )
                conn.commit()
                import logging
                logging.getLogger(__name__).info(
                    f"Category eviction: {row['title']} (category={category}, {count}/{limit})"
                )
    except Exception:
        pass  # eviction failure should not block writes


def _update_quality(kb_id: str, factor: float, weight: float = 0.05):
    """EMA smoothed update of knowledge quality score"""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT quality_score FROM knowledge_blocks WHERE id=%s", (kb_id,)
            ).fetchone()
            if not row:
                return
            current = row['quality_score'] or 0.5
            new_score = current * (1 - weight) + factor * weight
            new_score = max(0.0, min(1.0, new_score))
            conn.execute(
                "UPDATE knowledge_blocks SET quality_score=%s WHERE id=%s",
                (new_score, kb_id)
            )
            conn.commit()
    except Exception:
        pass


# =============================================
# LLM 调用（通过 UnifiedLLM，复用 Agent Matrix 引擎）
# =============================================

def _get_cleaner_provider_model_id():
    """从 agent_matrix 表读取 Data Cleaner Agent 的 provider_model_id。
    走 ID 解析路径，避免 model_name 字符串精确匹配导致的失败。
    """
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT provider_model_id FROM agent_matrix "
                "WHERE domain='cleaner' AND is_active=1 LIMIT 1"
            ).fetchone()
        if row and row['provider_model_id']:
            return row['provider_model_id']
    except Exception:
        pass
    return None


def _call_llm(system_prompt: str, user_prompt: str):
    """
    调用 LLM 清洗数据，返回解析后的 JSON 结果 dict。

    成功返回: {'title':'...', 'content':'...', 'category':'...', 'keywords':'...', 'is_duplicate':False, 'duplicate_of':''}
    失败返回: {'error': '...'}
    """
    import logging
    _logger = logging.getLogger(__name__)

    # 构建 engine 配置（优先通过 provider_model_id 走 ID 解析路径）
    pm_id = _get_cleaner_provider_model_id()
    engine_config = {
        'provider_model_id': pm_id,   # 优先：AI Hub ID 解析
        'provider': 'deepseek',       # 回退：当 pm_id 为 None 时使用
        'model_name': '',
        'base_url': '',
        'system_prompt': '',
    }
    try:
        from models import get_db as _get_db
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM system_config WHERE key IN "
                "('cleaner_ai_provider', 'cleaner_ai_model', "
                "'cleaner_ai_base_url', 'cleaner_ai_api_key')"
            ).fetchall()
            for r in rows:
                key = r['key']
                val = r['value']
                if key == 'cleaner_ai_provider' and val:
                    engine_config['provider'] = val
                elif key == 'cleaner_ai_model' and val:
                    engine_config['model_name'] = val
                elif key == 'cleaner_ai_base_url' and val:
                    engine_config['base_url'] = val
    except Exception as e:
        _logger.warning(f'Failed to read cleaner_ai config from DB: {e}')

    # 初始化 UnifiedLLM
    try:
        from agent_matrix.engine import UnifiedLLM
        engine = UnifiedLLM(engine_config)
    except ImportError:
        _logger.error('agent_matrix.engine.UnifiedLLM not available')
        return {'error': 'AI engine not available'}
    except Exception as e:
        _logger.error(f'Failed to initialize UnifiedLLM: {e}')
        return {'error': f'AI engine init failed: {e}'}

    if not engine.is_ready():
        _logger.error('UnifiedLLM not ready (missing API key for provider=%s)', engine_config['provider'])
        return {'error': _('AI engine not ready, please check API Key configuration')}

    # 调用 LLM
    try:
        raw_response = engine.chat(
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            temperature=0.3,
            max_tokens=4096,
            module='cleaner_agent',
        )
    except ValueError as e:
        _logger.error(f'Model resolution failed: {e}')
        return {'error': f'Model configuration error: {e}'}
    except Exception as e:
        _logger.error(f'LLM call failed: {e}')
        return {'error': f'LLM call failed: {e}'}

    if not raw_response:
        return {'error': _('LLM returned empty response')}

    # 解析 JSON
    try:
        import re
        # 尝试提取 JSON 块（处理 LLM 可能包裹的 markdown 代码块）
        json_match = re.search(r'\{[\s\S]*\}', raw_response)
        if json_match:
            result = json.loads(json_match.group())
        else:
            result = json.loads(raw_response)

        # 验证必要字段
        if not result.get('title'):
            return {'error': _('LLM response missing title field')}
        return result

    except (json.JSONDecodeError, ValueError) as e:
        _logger.error(f'Failed to parse LLM JSON response: {e}\nResponse: {raw_response[:500]}')
        return {'error': _('Failed to parse LLM response as JSON')}


# =============================================
# Core function: directly callable by Agent Matrix
# =============================================

def process_clean_content(raw_content: str, admin_id: int = 0, scope: str = 'user', queue_id: int = None) -> dict:
    """Clean a raw content entry, write to knowledge_blocks

    Args:
        raw_content: 原始内容
        admin_id: 操作管理员 ID
        scope: 'system' | 'user' — 目标知识库作用域（默认 user）
               system 仅超级管理员可写入

    Returns: {'success': bool, 'kb_id': '...' or 'duplicate' or 'merged',
              'title': '...', 'category': '...', 'error': '...'}
    """
    if not raw_content or not raw_content.strip():
        return {'success': False, 'error': _('Content cannot be empty')}

    raw_content = raw_content.strip()[:50000]
    existing = _get_existing_for_dedup(scope=scope)
    existing_titles = {e['title'].strip().lower() for e in existing}

    system_prompt = """You are the data cleaning agent for VeroRun.
Clean user-provided raw content into standardized knowledge base entries.

Rules:
1. Extract title (concise, accurate, max 50 characters)
2. Denoise body: remove ads, irrelevant links, duplicates, format layout
3. Category: choose the best fit from (company/product/price/tech/service/faq/industry)
4. Extract keywords: 5-10 keywords, comma separated
5. Dedup check: if content similarity with existing KB > 85%, mark as duplicate

Output pure JSON only, no other text:
{"title":"...","content":"...","category":"...","keywords":"...","is_duplicate":false,"duplicate_of":""}"""

    user_prompt = f"""Raw content:
---
{raw_content[:8000]}
---

Existing KB titles (for dedup reference):
{', '.join(list(existing_titles)[:50])}

Clean and output JSON per rules above."""

    # Write to queue: 若指定 queue_id（后台重跑/批量重跑），复用原队列行，避免重复入队
    qid = queue_id
    if qid is None:
        with get_db() as conn:
            qid = conn.execute(
                "INSERT INTO knowledge_queue (source, raw_content, admin_id, status) VALUES (%s,%s,%s,'cleaning') RETURNING id",
                ('matrix', raw_content, admin_id)
            ).fetchone()['id']
            conn.commit()

    # Call LLM
    result = _call_llm(system_prompt, user_prompt)
    if 'error' in result:
        with get_db() as conn:
            conn.execute("UPDATE knowledge_queue SET status='failed', error_msg=%s WHERE id=%s",
                         (result['error'][:500], qid))
            conn.commit()
        return {'success': False, 'error': result['error']}

    new_title = result['title'][:200]
    new_content = result['content']
    new_keywords = result.get('keywords', '')[:500]
    new_category = result.get('category', 'general')

    # === Phase 2 enhanced: two-level dedup ===
    is_dup, dup_entry, dup_reason = _dedup_check(new_title, new_keywords, existing)
    if is_dup and dup_entry:
        # Check for same-category conflict merge
        if dup_entry.get('category') == new_category and dup_entry.get('source') != 'manual':
            if _title_similarity(new_title, dup_entry['title']) > 0.80:
                # Conflict merge: write version history → update old entry
                return _merge_entry(dup_entry, new_title, new_content, new_keywords, qid)

        # Non-merge scenario: mark as duplicate
        with get_db() as conn:
            conn.execute(
                "UPDATE knowledge_queue SET status='done', cleaned_id=%s WHERE id=%s", (qid, qid))
            conn.commit()
        return {'success': True, 'kb_id': 'duplicate', 'title': new_title,
                'category': new_category, 'message': f'Duplicate detected ({dup_reason}), skipped'}

    # Double confirm when LLM returns is_duplicate
    if result.get('is_duplicate'):
        is_dup2, dup_entry2, _reason2 = _dedup_check(new_title, new_keywords, existing)
        if is_dup2 and dup_entry2 and dup_entry2.get('source') != 'manual':
            if _title_similarity(new_title, dup_entry2['title']) > 0.75:
                return _merge_entry(dup_entry2, new_title, new_content, new_keywords, qid)

        with get_db() as conn:
            conn.execute(
                "UPDATE knowledge_queue SET status='done', cleaned_id=%s WHERE id=%s", (qid, qid))
            conn.commit()
        return {'success': True, 'kb_id': 'duplicate', 'title': new_title,
                'category': new_category, 'message': 'LLM detected duplicate, skipped'}

    # === New entry: write with scope + owner_id ===
    kb_id = 'kb_cleaner_' + str(qid) + '_(' + ')'.join(re.findall(r'\w', new_title)[:10])
    with get_db() as conn:
        conn.execute(
            '''INSERT INTO knowledge_blocks
               (id, title, content, keywords, category, priority, source, quality_score, scope, owner_id, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
               ON CONFLICT (id) DO NOTHING''',
            (kb_id, new_title, new_content, new_keywords, new_category, 5, 'auto', 0.5, scope, admin_id)
        )
        conn.execute("UPDATE knowledge_queue SET status='done', cleaned_id=%s WHERE id=%s", (qid, qid))
        conn.commit()

    # 入库后生成 embedding（向量检索路；失败静默，不影响入库结果）
    try:
        from agent_matrix.rag_retriever import store_embedding
        store_embedding(kb_id, new_title, new_content)
    except Exception:
        pass

    # === Category eviction check ===
    _evict_if_over_limit(new_category)

    return {
        'success': True, 'kb_id': kb_id, 'title': new_title,
        'category': new_category, 'keywords': new_keywords,
        'message': _('Cleaning completed, written to knowledge base')
    }


def _merge_entry(old_entry: dict, new_title: str, new_content: str, new_keywords: str, qid) -> dict:
    """Conflict merge: write version history → update old entry"""
    import uuid
    history_id = 'kh_' + uuid.uuid4().hex[:12]

    with get_db() as conn:
        # Write version history
        conn.execute(
            """INSERT INTO knowledge_history (id, kb_id, previous_title, previous_content, changed_at)
               VALUES (%s,%s,%s,%s,NOW())""",
            (history_id, old_entry['id'], old_entry['title'], old_entry['content'])
        )
        # Update old entry
        conn.execute(
            """UPDATE knowledge_blocks
               SET content=%s, keywords=%s, updated_at=NOW(), quality_score=GREATEST(quality_score, 0.5)
               WHERE id=%s""",
            (new_content, new_keywords, old_entry['id'])
        )
        conn.execute(
            "UPDATE knowledge_queue SET status='done', cleaned_id=%s WHERE id=%s",
            (qid, qid)
        )
        conn.commit()

    # 合并更新后重新生成 embedding（向量路；失败静默）
    try:
        from agent_matrix.rag_retriever import store_embedding
        store_embedding(old_entry['id'], new_title, new_content)
    except Exception:
        pass

    import logging
    logging.getLogger(__name__).info(
        f"Conflict merge: '{old_entry['title']}' ← '{new_title}' (category={old_entry.get('category', '')})"
    )

    return {
        'success': True, 'kb_id': old_entry['id'], 'title': old_entry['title'],
        'category': old_entry.get('category', 'general'),
        'keywords': new_keywords,
        'message': _('Merged and updated existing entry (version history saved)')
    }


def auto_register_sub_agent():
    """Auto-register Cleaner Agent as a matrix sub-agent (idempotent)"""
    try:
        from agent_matrix import models as am_models
        existing = am_models.list_agents(domain=CLEANER_AGENT_DOMAIN, active_only=False)
        if existing:
            return  # Already registered
        am_models.create_agent({
            'name': CLEANER_AGENT_NAME,
            'role_type': 'sub',
            'domain': CLEANER_AGENT_DOMAIN,
            'managed_modules': json.dumps(['knowledge']),
            'capabilities': json.dumps(['text_clean', 'content_classify', 'dedup']),
            'description': 'Clean raw content into structured knowledge entries (dedup + classify + save to knowledge base)',
            'provider': 'deepseek',
            'model_name': '',
            'is_active': 1,
        })
        print(f'[CleanerAgent] ✅ Automatically registered as a matrix sub-agent')
    except Exception as e:
        print(f'[CleanerAgent] Auto-registration skipped: {e}')


# =============================================
# Phase 3: scheduled maintenance (APScheduler)
# =============================================

import logging
_kb_logger = logging.getLogger('knowledge_maintenance')
_kb_scheduler = None


def _single_instance_run(lock_key: int, job_id: str, fn):
    """Run a maintenance job on a single gunicorn worker.

    Uses a PostgreSQL session-level advisory lock held on a dedicated
    connection for the whole job. Other workers calling the same job get
    pg_try_advisory_lock=False and simply skip, preventing double execution
    under multi-worker gunicorn.
    """
    try:
        with get_db() as conn:
            ok = conn.execute(
                "SELECT pg_try_advisory_lock(%s) AS ok", (lock_key,)
            ).fetchone()['ok']
            if not ok:
                _kb_logger.info(f'[KB Scheduler] {job_id} skipped (another worker running)')
                return
            try:
                fn()
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                conn.commit()
    except Exception as e:
        _kb_logger.warning(f'[KB Scheduler] {job_id} single-instance run failed: {e}')


def _run_time_decay_single():
    _single_instance_run(7101, 'time-decay', _run_time_decay)


def _run_redundancy_single():
    _single_instance_run(7102, 'redundancy', _run_redundancy_check)


def _run_time_decay():
    """
    Time decay: runs weekly.
    - 180 days no hit + quality_score < 0.3 → soft delete
    - 365 days no hit → soft delete
    - 180 days no hit + quality_score >= 0.3 → reduce quality_score
    """
    try:
        with get_db() as conn:
            now = datetime.now()
            threshold_365 = (now - timedelta(days=365)).isoformat()
            threshold_180 = (now - timedelta(days=180)).isoformat()

            # 365 days → soft delete
            result = conn.execute(
                """UPDATE knowledge_blocks SET deleted_at=NOW()
                   WHERE deleted_at IS NULL AND created_at < %s AND hit_count = 0""",
                (threshold_365,)
            )
            deleted_365 = result.rowcount

            # 180 days + quality < 0.3 → soft delete
            result = conn.execute(
                """UPDATE knowledge_blocks SET deleted_at=NOW()
                   WHERE deleted_at IS NULL AND created_at < %s
                   AND hit_count = 0 AND quality_score < 0.3""",
                (threshold_180,)
            )
            deleted_180 = result.rowcount

            # 180 days + quality >= 0.3 → downgrade (no delete)
            result = conn.execute(
                """UPDATE knowledge_blocks
                   SET quality_score = GREATEST(quality_score - 0.2, 0.0)
                   WHERE deleted_at IS NULL AND created_at < %s
                   AND hit_count = 0 AND quality_score >= 0.3""",
                (threshold_180,)
            )
            downgraded = result.rowcount

            conn.commit()

        _kb_logger.info(
            f'[TimeDecay] 365d deleted={deleted_365}, '
            f'180d deleted={deleted_180}, downgraded={downgraded}'
        )
    except Exception as e:
        _kb_logger.error(f'[TimeDecay] Failed: {e}')


def _run_redundancy_check():
    """
    Redundancy check: runs monthly.
    Full Jaccard keyword dedup, log entry pairs with similarity > 90%.
    """
    try:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, title, keywords, category
                   FROM knowledge_blocks WHERE deleted_at IS NULL"""
            ).fetchall()

        if len(rows) < 2:
            return

        entries = [dict(r) for r in rows]
        duplicates = []

        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                e1, e2 = entries[i], entries[j]
                if e1['category'] != e2['category']:
                    continue
                jac = _jaccard_similarity(
                    e1.get('keywords', ''), e2.get('keywords', '')
                )
                if jac > 0.90:
                    duplicates.append({
                        'kb1': e1['id'], 'kb2': e2['id'],
                        'title1': e1['title'], 'title2': e2['title'],
                        'category': e1['category'], 'jaccard': round(jac, 3),
                    })

        if duplicates:
            _kb_logger.warning(
                f'[Redundancy] Found {len(duplicates)} duplicate pairs: {duplicates[:20]}'
            )
        else:
            _kb_logger.info('[Redundancy] No duplicates found')
    except Exception as e:
        _kb_logger.error(f'[Redundancy] Failed: {e}')


def _claim_pending(limit: int = 5):
    """Atomically claim pending queue rows for cleaning (multi-worker safe).

    Uses FOR UPDATE SKIP LOCKED so concurrent workers never process the same row.
    Bumps created_at to NOW() so it doubles as a "last activity" timestamp for
    stale-recovery (rows stuck in 'cleaning' for >10min are reset to pending).
    Returns list of dicts [{id, raw_content, admin_id}, ...].
    """
    try:
        with get_db() as conn:
            rows = conn.execute(
                """UPDATE knowledge_queue SET status='cleaning', created_at=NOW()
                   WHERE id IN (
                       SELECT id FROM knowledge_queue
                       WHERE status='pending' ORDER BY id ASC LIMIT %s FOR UPDATE SKIP LOCKED
                   ) RETURNING id, raw_content, admin_id""",
                (limit,)
            ).fetchall()
            conn.commit()
            return [dict(r) for r in rows]
    except Exception as e:
        _kb_logger.warning(f'[CleanerWorker] Claim failed: {e}')
        return []


def _process_pending_queue():
    """Background worker: drain knowledge_queue (pending → cleaning → done/failed).

    Also recovers rows stuck in 'cleaning' for over 10 minutes (e.g. worker crash).
    Runs every few seconds via APScheduler interval job; safe under multi-worker
    gunicorn because claiming is atomic (FOR UPDATE SKIP LOCKED).
    """
    try:
        # Recover stale cleaning rows (created_at is bumped to claim time;
        # a row still 'cleaning' >10min after its last claim is surely stuck)
        with get_db() as conn:
            conn.execute(
                """UPDATE knowledge_queue SET status='pending'
                   WHERE status='cleaning' AND created_at::timestamptz < NOW() - interval '10 minutes'""")
            conn.commit()
    except Exception as e:
        _kb_logger.warning(f'[CleanerWorker] Stale recovery failed: {e}')

    for row in _claim_pending():
        try:
            process_clean_content(row['raw_content'], admin_id=row['admin_id'] or 0,
                                  scope='user', queue_id=row['id'])
        except Exception as e:
            try:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE knowledge_queue SET status='failed', error_msg=%s WHERE id=%s",
                        (str(e)[:500], row['id']))
                    conn.commit()
            except Exception:
                pass


def init_kb_scheduler():
    """
    Initialize knowledge base maintenance scheduler.
    Called once when admin/app.py starts.
    """
    global _kb_scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        if _kb_scheduler is not None:
            return  # Already initialized

        _kb_scheduler = BackgroundScheduler(
            daemon=True,
            job_defaults={'misfire_grace_time': 3600},
        )

        # Time decay: every Sunday 3:00 AM (single-instance across workers)
        _kb_scheduler.add_job(
            _run_time_decay_single,
            CronTrigger(day_of_week='sun', hour=3, minute=0),
            id='kb_time_decay',
            name='Knowledge Time Decay',
            replace_existing=True,
        )

        # Redundancy check: 1st of every month 4:00 AM (single-instance across workers)
        _kb_scheduler.add_job(
            _run_redundancy_single,
            CronTrigger(day=1, hour=4, minute=0),
            id='kb_redundancy',
            name='Knowledge Redundancy Check',
            replace_existing=True,
        )

        # Cleaner queue worker: drain pending cleaning tasks every 5 seconds
        _kb_scheduler.add_job(
            _process_pending_queue,
            IntervalTrigger(seconds=5),
            id='kb_cleaner_worker',
            name='Cleaner Queue Worker',
            replace_existing=True,
        )

        _kb_scheduler.start()
        _kb_logger.setLevel(logging.INFO)
        if not _kb_logger.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s %(message)s'))
            _kb_logger.addHandler(h)
        _kb_logger.info('[KnowledgeMaintenance] Scheduler started (weekly decay + monthly redundancy + cleaner worker; maintenance jobs single-instance)')

    except ImportError:
        print('[KnowledgeMaintenance] APScheduler not available, skip')
    except Exception as e:
        print(f'[KnowledgeMaintenance] Scheduler init failed: {e}')


# =============================================
# Flask API endpoints (thin wrappers)
# =============================================

@cleaner_bp.route('/submit', methods=['POST'])
def submit_content():
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    raw = (data.get('content', '') or '').strip()
    scope = data.get('scope', 'user')  # 默认用户KB
    
    if not raw:
        return jsonify({'success': False, 'error': _('Content cannot be empty')}), 400

    # 如果目标为系统KB，需要超级管理员权限；系统KB保持同步清洗（仅超管、罕见）
    if scope == 'system':
        from services.kb_permission import check_kb_permission
        allowed, err2 = check_kb_permission('system', None, 'write', payload)
        if not allowed:
            return err2
        result = process_clean_content(raw, admin_id=payload['user_id'], scope=scope)
        if not result['success']:
            return jsonify({'success': False, 'error': result['error']}), 500
        return jsonify({'success': True, 'data': result, 'message': result.get('message', _('Cleaning completed'))})

    # 用户知识库：入队异步清洗，立即返回（后台 worker 每 5 秒排空队列）
    with get_db() as conn:
        qid = conn.execute(
            "INSERT INTO knowledge_queue (source, raw_content, admin_id, status) VALUES (%s,%s,%s,%s) RETURNING id",
            ('admin', raw, payload['user_id'], 'pending')
        ).fetchone()['id']
        conn.commit()
    return jsonify({'success': True, 'data': {'id': qid, 'status': 'pending'}, 'message': _('Added to Cleaning Queue')})


@cleaner_bp.route('/list', methods=['GET'])
def list_queue():
    payload, err = _require_admin()
    if err:
        return err
    status_filter = request.args.get('status', '')
    with get_db() as conn:
        sql = 'SELECT * FROM knowledge_queue'
        params = []
        if status_filter:
            sql += ' WHERE status=%s'
            params.append(status_filter)
        sql += ' ORDER BY id DESC LIMIT 100'
        rows = conn.execute(sql, params).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@cleaner_bp.route('/run/<int:qid>', methods=['POST'])
def run_clean(qid):
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute('SELECT * FROM knowledge_queue WHERE id=%s', (qid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Queue item does not exist')}), 404
        if row['status'] == 'cleaning':
            return jsonify({'success': False, 'error': _('Cleaning in progress, please wait')}), 400
        # 重新入队，由后台 worker 异步清洗
        conn.execute("UPDATE knowledge_queue SET status='pending', error_msg='' WHERE id=%s", (qid,))
        conn.commit()
    return jsonify({'success': True, 'data': {'id': qid, 'status': 'pending'}, 'message': _('Added to Cleaning Queue')})


@cleaner_bp.route('/run-all', methods=['POST'])
def run_all():
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM knowledge_queue WHERE status='pending'").fetchone()['c']

    if not n:
        return jsonify({'success': True, 'data': [], 'message': _('No items to clean')})

    # 后台 worker 自动排空 pending 队列，此处仅确认已入队
    return jsonify({'success': True, 'data': {'pending': n}, 'message': _('Added to Cleaning Queue')})


@cleaner_bp.route('/config', methods=['GET'])
def get_config():
    payload, err = _require_admin()
    if err:
        return err
    from models.database import get_active_model
    _, model_name, base_url = get_active_model('deepseek')
    return jsonify({
        'success': True,
        'data': {'provider': 'deepseek', 'model': model_name or '', 'base_url': base_url or 'https://api.deepseek.com/v1'}
    })
