#!/usr/bin/env python3
"""Admin endpoints for memory_engine.

Blueprint prefix: /admin/memory
Includes: memories CRUD, reflexion logs, prompt metrics, and
Evolution Ring APIs (C.3): phases, rounds, graph.
"""

import hashlib
import json
import uuid
from functools import wraps
from flask import Blueprint, jsonify, request

from .models import get_memory_engine_db, EVOLUTION_PHASES

bp = Blueprint('memory_engine_admin', __name__, url_prefix='/admin/memory')


# ── Auth guard ──────────────────────────────────────────────────

def _require_admin():
    """Require an authenticated admin (JWT Bearer header or sso/tm cookie)."""
    from services.jwt_service import validate_token
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    try:
        payload = validate_token(token) if token else None
    except Exception:
        payload = None
    if not payload or not payload.get('is_admin'):
        return None, jsonify({'ok': False, 'error': 'Requires management permissions'}), 401
    return payload, None, None


def admin_required(fn):
    """Reject anonymous / non-admin callers."""
    @wraps(fn)
    def wrapper(*a, **kw):
        _payload, err, status = _require_admin()
        if err:
            return err, status
        return fn(*a, **kw)
    return wrapper


def _is_uuid(value):
    """Path ids are UUID primary keys (memories.id / sedimentation_queue.id);
    handing a non-UUID string to a ``WHERE id = ?`` query makes PostgreSQL raise
    ``invalid input syntax for type uuid`` → 500. Callers answer 404 instead."""
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


# ── Memories ─────────────────────────────────────────────────────

@bp.route('/memories')
@admin_required
def list_memories():
    """List/search memories with owner + type filters (admin page data)."""
    q = request.args.get('q', '')
    owner_type = request.args.get('owner_type', '')
    mtype = request.args.get('type', '')
    conn = get_memory_engine_db()
    try:
        sql = ("SELECT id, owner_type, owner_id, agent_id, memory_type, content,"
               " confidence, hit_count, quality_score, source, status, created_at"
               " FROM memories WHERE 1=1")
        params = []
        if q:
            sql += " AND content ILIKE ?"
            params.append('%' + q + '%')
        if owner_type:
            sql += " AND owner_type = ?"
            params.append(owner_type)
        if mtype:
            sql += " AND memory_type = ?"
            params.append(mtype)
        sql += " ORDER BY updated_at DESC LIMIT 200"
        rows = conn.execute(sql, params).fetchall()
        return jsonify({'ok': True, 'rows': [dict(r) for r in rows]})
    finally:
        conn.close()


@bp.route('/memories/<mem_id>', methods=['DELETE'])
@admin_required
def delete_memory(mem_id):
    """Soft-delete a memory (admin action)."""
    if not _is_uuid(mem_id):
        return jsonify({'ok': False, 'error': 'Memory not found'}), 404
    conn = get_memory_engine_db()
    try:
        conn.execute(
            "UPDATE memories SET status = 'archived' WHERE id = ?", (mem_id,)
        )
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


# ── Reflexions ───────────────────────────────────────────────────

@bp.route('/reflexions')
@admin_required
def list_reflexions():
    """Recent reflexion logs."""
    conn = get_memory_engine_db()
    try:
        rows = conn.execute(
            "SELECT agent_id, task_id, trigger, success, issue, lesson,"
            " action, rating, created_at"
            " FROM reflexion_logs ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        return jsonify({'ok': True, 'rows': [dict(r) for r in rows]})
    finally:
        conn.close()


# ── Prompt Metrics ────────────────────────────────────────────────

@bp.route('/prompts')
@admin_required
def list_prompt_metrics():
    """Prompt version metrics + evolution suggestions."""
    conn = get_memory_engine_db()
    try:
        rows = conn.execute(
            "SELECT agent_id, prompt_hash, prompt_version, sample_count,"
            " success_rate, avg_rating, updated_at"
            " FROM prompt_metrics ORDER BY agent_id, updated_at DESC"
        ).fetchall()
        from .services.prompt_evolution import PromptEvolutionService
        suggestions = PromptEvolutionService().list_suggestions()
        return jsonify({
            'ok': True,
            'rows': [dict(r) for r in rows],
            'suggestions': suggestions,
        })
    finally:
        conn.close()


# ── Evolution Ring (Appendix C.3) ─────────────────────────────────

@bp.route('/phases')
@admin_required
def memory_phases():
    """Return the EVOLUTION_PHASES configuration.
    Frontend uses this to dynamically render ring segments.
    """
    return jsonify({'ok': True, 'phases': EVOLUTION_PHASES})


@bp.route('/rounds')
@admin_required
def memory_rounds():
    """Round timeline (player data source).
    ?agent_id=  optional filter.
    """
    agent_id = request.args.get('agent_id', '')
    conn = get_memory_engine_db()
    try:
        if agent_id:
            rows = conn.execute(
                "SELECT id, agent_id, round_seq, status, window_start, window_end,"
                " mem_count, ref_count, prompt_from, prompt_to"
                " FROM evolution_rounds"
                " WHERE agent_id = ?"
                " ORDER BY round_seq DESC LIMIT 60",
                (agent_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, agent_id, round_seq, status, window_start, window_end,"
                " mem_count, ref_count, prompt_from, prompt_to"
                " FROM evolution_rounds"
                " ORDER BY window_start DESC LIMIT 60"
            ).fetchall()
        return jsonify({'ok': True, 'rounds': [dict(r) for r in rows]})
    finally:
        conn.close()


@bp.route('/graph')
@admin_required
def memory_graph():
    """Evolution Ring payload: nodes + links for one round.

    ?round_id=  specific round (default: latest closed round)
    ?owner_type=user  &  ?owner_id=  scope filter
    """
    owner_type = request.args.get('owner_type', 'user')
    owner_id = request.args.get('owner_id', '')
    round_id = request.args.get('round_id', '')
    conn = get_memory_engine_db()
    try:
        # Resolve the target round's time window (fallback: last closed round).
        if round_id:
            win = conn.execute(
                "SELECT agent_id, window_start, window_end"
                " FROM evolution_rounds WHERE id = ?",
                (round_id,),
            ).fetchone()
        else:
            win = conn.execute(
                "SELECT agent_id, window_start, window_end FROM evolution_rounds"
                " WHERE status = 'closed' ORDER BY window_start DESC LIMIT 1"
            ).fetchone()

        agent_id = win['agent_id'] if win else ''
        since = win['window_start'] if win else None
        until = win['window_end'] if win else None

        # Build memory nodes.
        sql = ("SELECT id, agent_id, memory_type, content, importance, quality_score"
               " FROM memories"
               " WHERE owner_type = ? AND owner_id = ? AND status = 'active'")
        args = [owner_type, owner_id]
        if since:
            sql += " AND created_at >= ?"; args.append(since)
        if until:
            sql += " AND created_at < ?"; args.append(until)
        sql += " ORDER BY importance DESC LIMIT 200"
        mem_rows = conn.execute(sql, args).fetchall()

        nodes = []
        links = []
        mem_ids = []

        for r in mem_rows:
            phase = 'experience' if r['memory_type'] == 'lesson' else 'mem_extract'
            nodes.append({
                'id': str(r['id']),
                'kind': 'memory',
                'phase': phase,
                'agent_id': r['agent_id'],
                'content': str(r['content'])[:120],
                'importance': float(r['importance'] or 0.5),
                'quality_score': float(r['quality_score'] or 0.5),
            })
            mem_ids.append(str(r['id']))

        # Same-agent memory → memory links (limit: only among top 200 memories).
        _agent_mems = {}
        for n in nodes:
            _agent_mems.setdefault(n['agent_id'], []).append(n['id'])
        for ag, ids in _agent_mems.items():
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    links.append({
                        'source': ids[i], 'target': ids[j],
                        'relation': 'same_agent',
                    })

        # Build reflexion nodes.
        rsql = ("SELECT id, agent_id, issue, lesson, rating"
                " FROM reflexion_logs WHERE 1=1")
        rargs = []
        if since:
            rsql += " AND created_at >= ?"; rargs.append(since)
        if until:
            rsql += " AND created_at < ?"; rargs.append(until)
        rsql += " ORDER BY created_at DESC LIMIT 100"
        ref_rows = conn.execute(rsql, rargs).fetchall()

        for r in ref_rows:
            nid = 'ref_' + str(r['id'])
            nodes.append({
                'id': nid,
                'kind': 'reflexion',
                'phase': 'reflexion',
                'agent_id': r['agent_id'],
                'content': str(r['lesson'] or r['issue'] or '')[:120],
                'importance': 0.5,
            })
            # Link reflexion → top-5 memories for that agent (avoid edge explosion).
            top_mems = [
                m['id'] for m in nodes
                if m['kind'] == 'memory' and m['agent_id'] == r['agent_id']
            ][:5]
            for mid in top_mems:
                links.append({
                    'source': nid, 'target': mid,
                    'relation': 'reflexed',
                })

        # Build prompt version nodes (latest per agent).
        p_rows = conn.execute(
            "SELECT DISTINCT ON (agent_id) agent_id, prompt_hash, prompt_version,"
            " success_rate, avg_rating"
            " FROM prompt_metrics ORDER BY agent_id, updated_at DESC"
        ).fetchall()
        for r in p_rows:
            nid = 'prm_' + str(r['prompt_hash'])
            nodes.append({
                'id': nid,
                'kind': 'prompt',
                'phase': 'prompt_evolve',
                'agent_id': r['agent_id'],
                'content': 'v' + str(r['prompt_version']),
                'importance': 0.5,
            })
            top_mems = [
                m['id'] for m in nodes
                if m['kind'] == 'memory' and m['agent_id'] == r['agent_id']
            ][:5]
            for mid in top_mems:
                links.append({
                    'source': nid, 'target': mid,
                    'relation': 'evolves',
                })

        return jsonify({
            'ok': True,
            'nodes': nodes,
            'links': links,
            'round': dict(win) if win else None,
        })
    finally:
        conn.close()


# ── Sedimentation（记忆 → 知识库沉淀审核）────────────────────

@bp.route('/sedimentation')
@admin_required
def list_sedimentation():
    """Review queue items (?status=pending|approved|rejected)."""
    status = request.args.get('status', 'pending')
    conn = get_memory_engine_db()
    try:
        rows = conn.execute(
            "SELECT id, source_schema, memory_id, title, content, keywords, category,"
            " owner_id, memory_type, confidence, quality_score, status, note, kb_id,"
            " created_at, reviewed_at"
            " FROM sedimentation_queue WHERE status = ?"
            " ORDER BY created_at DESC LIMIT 200",
            (status,),
        ).fetchall()
        return jsonify({'ok': True, 'rows': [dict(r) for r in rows]})
    finally:
        conn.close()


@bp.route('/sedimentation/run', methods=['POST'])
@admin_required
def run_sedimentation():
    """Manually trigger a sedimentation scan (idempotent)."""
    from .services.sedimentation import get_service
    try:
        count = get_service().run_daily()
        return jsonify({'ok': True, 'queued': count})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@bp.route('/sedimentation/<queue_id>/approve', methods=['POST'])
def approve_sedimentation(queue_id):
    """Approve → write knowledge_blocks (gated by system kb permission)."""
    payload, err, status = _require_admin()
    if err:
        return err, status
    if not _is_uuid(queue_id):
        return jsonify({'ok': False, 'error': 'Sedimentation entry not found'}), 404
    from services.kb_permission import check_kb_permission
    conn = get_memory_engine_db()
    try:
        row = conn.execute(
            "SELECT owner_id, status FROM sedimentation_queue WHERE id = ?", (queue_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({'ok': False, 'error': 'Sedimentation entry not found'}), 404
    if row['status'] != 'pending':
        return jsonify({'ok': False, 'error': 'Entry already reviewed'}), 400
    allowed, perr = check_kb_permission('user', row['owner_id'], 'write', payload)
    if not allowed:
        return perr
    from .services.sedimentation import get_service
    res = get_service().approve(queue_id)
    return jsonify(res), (200 if res.get('ok') else 400)


@bp.route('/sedimentation/<queue_id>/reject', methods=['POST'])
def reject_sedimentation(queue_id):
    """Reject a pending entry (optional note in JSON body)."""
    payload, err, status = _require_admin()
    if err:
        return err, status
    note = ''
    try:
        _d = request.get_json(silent=True) or {}
        note = str(_d.get('note', ''))[:500]
    except Exception:
        pass
    from .services.sedimentation import get_service
    res = get_service().reject(queue_id, note)
    return jsonify(res), (200 if res.get('ok') else 400)


# ── 用户自助（GDPR 访问权/编辑权/删除权；JWT user_id，非管理员）────

def _require_user():
    """Require any authenticated user (JWT user_id claim)."""
    from services.jwt_service import validate_token
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    try:
        payload = validate_token(token) if token else None
    except Exception:
        payload = None
    if not payload or not payload.get('user_id'):
        return None, jsonify({'ok': False, 'error': 'Authentication required'}), 401
    return payload, None, None


user_bp = Blueprint('memory_engine_user', __name__, url_prefix='/api/memory')


@user_bp.route('/my')
def my_memories():
    """当前用户自己的活跃记忆（不含 embedding 向量）。"""
    payload, err, status = _require_user()
    if err:
        return err, status
    conn = get_memory_engine_db()
    try:
        rows = conn.execute(
            "SELECT id, memory_type, content, agent_id, confidence, hit_count,"
            " quality_score, source, created_at, updated_at"
            " FROM memories"
            " WHERE owner_type = 'user' AND owner_id = ? AND status = 'active'"
            " ORDER BY updated_at DESC LIMIT 500",
            (str(payload['user_id']),),
        ).fetchall()
        return jsonify({'ok': True, 'rows': [dict(r) for r in rows]})
    finally:
        conn.close()


@user_bp.route('/my/<mem_id>', methods=['DELETE'])
def my_memory_delete(mem_id):
    """用户删除自己的记忆（软删，status → archived）。"""
    payload, err, status = _require_user()
    if err:
        return err, status
    if not _is_uuid(mem_id):
        return jsonify({'ok': False, 'error': 'Memory not found'}), 404
    conn = get_memory_engine_db()
    try:
        cur = conn.execute(
            "UPDATE memories SET status = 'archived'"
            " WHERE id = ? AND owner_type = 'user' AND owner_id = ?",
            (mem_id, str(payload['user_id'])),
        )
        conn.commit()
        return jsonify({'ok': bool(cur.rowcount)}), (200 if cur.rowcount else 404)
    finally:
        conn.close()


@user_bp.route('/my/<mem_id>', methods=['PUT'])
def my_memory_edit(mem_id):
    """用户编辑自己的记忆内容（重算 content_hash 保持去重语义）。"""
    payload, err, status = _require_user()
    if err:
        return err, status
    data = request.get_json(silent=True) or {}
    content = str(data.get('content', '')).strip()[:500]
    if len(content) < 4:
        return jsonify({'ok': False, 'error': 'content too short'}), 400
    from .services.extractor import MemoryExtractor
    if MemoryExtractor._contains_pii(content):
        return jsonify({'ok': False, 'error': 'content contains sensitive data'}), 400
    uid = str(payload['user_id'])
    digest = hashlib.sha256(f"{uid}|{content}".encode('utf-8')).hexdigest()
    conn = get_memory_engine_db()
    try:
        try:
            cur = conn.execute(
                "UPDATE memories SET content = ?, keywords = ?, content_hash = ?,"
                " updated_at = now()"
                " WHERE id = ? AND owner_type = 'user' AND owner_id = ?"
                " AND status = 'active'",
                (content, MemoryExtractor._keywords(content), digest, mem_id, uid),
            )
            conn.commit()
            return jsonify({'ok': bool(cur.rowcount)}), (200 if cur.rowcount else 404)
        except Exception:
            conn.rollback()
            return jsonify({'ok': False, 'error': 'duplicate content'}), 409
    finally:
        conn.close()


def _ensure_user_profiles_meta(conn):
    """user_profiles.meta 缺列时补建（auth-center 建表历史缺该列）。

    用户级 memory_opt_in 覆盖存于 public.user_profiles.meta；老库无该列时
    直接 SELECT 会抛 ``column "meta" does not exist``（500）。此处惰性自愈，
    与 veroscholar kb_sync 同一做法；列已存在时仅多做一次目录查询。
    """
    try:
        exists = conn.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'user_profiles'"
            " AND column_name = 'meta' LIMIT 1").fetchone()
        if not exists:
            conn.execute(
                "ALTER TABLE public.user_profiles"
                " ADD COLUMN meta JSONB NOT NULL DEFAULT '{}'::jsonb")
            conn.commit()
    except Exception:
        conn.rollback()


@user_bp.route('/my/optin', methods=['GET'])
def my_optin_get():
    """查询本人 memory_opt_in（未设置时为 null）。"""
    payload, err, status = _require_user()
    if err:
        return err, status
    from agent_matrix.models import get_db
    with get_db() as conn:
        _ensure_user_profiles_meta(conn)
        row = conn.execute(
            "SELECT meta FROM public.user_profiles WHERE user_id = %s",
            (str(payload['user_id']),),
        ).fetchone()
    if not row:
        return jsonify({'ok': True, 'opted_in': None})
    meta = row['meta'] or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    return jsonify({'ok': True, 'opted_in': bool(meta.get('memory_opt_in'))
                    if 'memory_opt_in' in meta else None})


@user_bp.route('/my/optin', methods=['PUT'])
def my_optin_put():
    """设置本人 memory_opt_in（写入 user_profiles.meta，与注入/提取隐私门同源）。"""
    payload, err, status = _require_user()
    if err:
        return err, status
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('opted_in'), bool):
        return jsonify({'ok': False, 'error': 'opted_in (boolean) required'}), 400
    uid = str(payload['user_id'])
    from agent_matrix.models import get_db
    with get_db() as conn:
        _ensure_user_profiles_meta(conn)
        row = conn.execute(
            "SELECT meta FROM public.user_profiles WHERE user_id = %s", (uid,)
        ).fetchone()
        if not row:
            return jsonify({'ok': False, 'error': 'profile not found'}), 404
        meta = row['meta'] or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        meta['memory_opt_in'] = bool(data['opted_in'])
        conn.execute(
            "UPDATE public.user_profiles SET meta = %s::jsonb WHERE user_id = %s",
            (json.dumps(meta), uid),
        )
        conn.commit()
    return jsonify({'ok': True})


# ── A/B 注入收益对照（P2）────────────────────────────────────────

@bp.route('/abtest/report')
@admin_required
def abtest_report():
    """A/B 对照报告：?days=14&min_sample=30。"""
    from .services.abtest import AbTestService
    days = min(max(request.args.get('days', 14, type=int), 1), 90)
    min_sample = min(max(request.args.get('min_sample', 30, type=int), 1), 10000)
    try:
        report = AbTestService({}).report(days=days, min_sample=min_sample)
        return jsonify({'ok': True, 'report': report})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── 检索回归评测（P2）────────────────────────────────────────────

@bp.route('/eval/cases')
@admin_required
def eval_cases_list():
    """列出评测用例（?active=true 仅活跃）。"""
    active_only = request.args.get('active', 'true') == 'true'
    conn = get_memory_engine_db()
    try:
        sql = ("SELECT id, name, owner_id, agent_id, query,"
               " expected_memory_id, expected_keywords, active, created_at"
               " FROM eval_cases")
        params = []
        if active_only:
            sql += " WHERE active = TRUE"
        sql += " ORDER BY created_at LIMIT 500"
        rows = conn.execute(sql, params).fetchall()
        return jsonify({'ok': True, 'rows': [dict(r) for r in rows]})
    finally:
        conn.close()


@bp.route('/eval/cases', methods=['POST'])
@admin_required
def eval_cases_add():
    """新增评测用例。body: {name, owner_id, agent_id?, query,
    expected_memory_id?, expected_keywords?: [..]}"""
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()[:128]
    owner_id = str(data.get('owner_id', '')).strip()
    query = str(data.get('query', '')).strip()
    if not (name and owner_id and query):
        return jsonify({'ok': False, 'error': 'name, owner_id, query required'}), 400
    kws = [str(k)[:64] for k in (data.get('expected_keywords') or [])][:12]
    conn = get_memory_engine_db()
    try:
        conn.execute(
            "INSERT INTO eval_cases"
            " (name, owner_id, agent_id, query, expected_memory_id, expected_keywords)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, owner_id, str(data.get('agent_id', '')), query[:1000],
             str(data.get('expected_memory_id') or '') or None, kws),
        )
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/eval/run', methods=['POST'])
@admin_required
def eval_run():
    """手动跑一轮评测（幂等留档于 eval_runs）。"""
    from .services.eval_harness import EvalHarness
    try:
        result = EvalHarness({}).run()
        return jsonify({'ok': True, 'result': result})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@bp.route('/eval/runs')
@admin_required
def eval_runs_list():
    """历史评测运行（变更前后对比数据源）。"""
    conn = get_memory_engine_db()
    try:
        rows = conn.execute(
            "SELECT id, case_count, hit_at_k, mrr, metrics, created_at"
            " FROM eval_runs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return jsonify({'ok': True, 'rows': [dict(r) for r in rows]})
    finally:
        conn.close()
