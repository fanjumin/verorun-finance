#!/usr/bin/env python3
"""Admin endpoints for memory_engine.

Blueprint prefix: /admin/memory
Includes: memories CRUD, reflexion logs, prompt metrics, and
Evolution Ring APIs (C.3): phases, rounds, graph.
"""

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
        return jsonify({'ok': True, 'rows': [dict(r) for r in rows]})
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
