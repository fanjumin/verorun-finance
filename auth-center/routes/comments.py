#!/usr/bin/env python3
"""Article Comments Routes — Public API + Admin management."""
import sys, os, json, logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from flask import Blueprint, request, jsonify
from models import get_db
from services.comment_review import review_comment

logger = logging.getLogger(__name__)
comments_bp = Blueprint('comments', __name__)


# ===== Public: Submit comment =====
@comments_bp.route('/api/v1/comments', methods=['POST'])
def submit_comment():
    data = request.get_json(force=True) or {}
    post_id = data.get('post_id')
    content = data.get('content', '').strip()
    nickname = data.get('nickname', '').strip() or 'Anonymous'
    parent_id = data.get('parent_id')

    if not post_id:
        return jsonify({'success': False, 'error': 'post_id required'}), 400
    if not content:
        return jsonify({'success': False, 'error': 'Comment cannot be empty'}), 400
    if len(content) > 500:
        return jsonify({'success': False, 'error': 'Comment exceeds 500 characters'}), 400

    # Verify post exists
    try:
        with get_db() as conn:
            post = conn.execute('SELECT id FROM cms_posts WHERE id=%s AND is_published=1', (post_id,)).fetchone()
            if not post:
                return jsonify({'success': False, 'error': 'Post not found'}), 404
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500

    # AI review
    status, reason, score = review_comment(nickname, content)
    ai_review = json.dumps({'reason': reason, 'score': score}, ensure_ascii=False)

    try:
        with get_db() as conn:
            cur = conn.execute('INSERT INTO article_comments'
                ' (post_id, parent_id, nickname, content, status, ai_review, ai_score, ip_address)'
                ' VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                (post_id, parent_id, nickname, content, status, ai_review, score,
                 request.remote_addr or ''))
            comment_id = cur.fetchone()['id']
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500

    msg_map = {
        'approved': 'Comment posted successfully',
        'pending': 'Comment submitted for review',
        'rejected': 'Comment rejected by content filter'
    }
    return jsonify({
        'success': status == 'approved',
        'data': {'id': comment_id, 'status': status, 'message': msg_map.get(status, 'Unknown')}
    })


# ===== Public: Get comments for article =====
@comments_bp.route('/api/v1/comments/<int:post_id>', methods=['GET'])
def get_comments(post_id):
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 20, type=int)
    offset = (page - 1) * limit

    try:
        with get_db() as conn:
            total = conn.execute(
                "SELECT COUNT(*) as c FROM article_comments WHERE post_id=%s AND status='approved'",
                (post_id,)
            ).fetchone()['c']
            rows = conn.execute(
                "SELECT id, parent_id, nickname, content, created_at FROM article_comments "
                "WHERE post_id=%s AND status='approved' ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (post_id, limit, offset)
            ).fetchall()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500

    return jsonify({
        'success': True,
        'data': {
            'total': total,
            'page': page,
            'limit': limit,
            'items': [dict(r) for r in rows]
        }
    })


# ===== Admin: List all comments =====
@comments_bp.route('/admin/comments', methods=['GET'])
def admin_list_comments():
    from routes.admin import _require_admin
    admin, err = _require_admin()
    if err: return err

    status = request.args.get('status', '')
    post_id = request.args.get('post_id', type=int)
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 30, type=int)
    offset = (page - 1) * limit

    where = ['1=1']
    params = []
    if status:
        where.append('c.status=%s')
        params.append(status)
    if post_id:
        where.append('c.post_id=%s')
        params.append(post_id)

    try:
        with get_db() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) as c FROM article_comments c WHERE {' AND '.join(where)}", params
            ).fetchone()['c']
            rows = conn.execute(
                f"""SELECT c.*, p.title as post_title FROM article_comments c
                    LEFT JOIN cms_posts p ON c.post_id=p.id
                    WHERE {' AND '.join(where)}
                    ORDER BY c.created_at DESC LIMIT %s OFFSET %s""",
                params + [limit, offset]
            ).fetchall()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500

    return jsonify({
        'success': True,
        'data': {
            'total': total,
            'page': page,
            'limit': limit,
            'items': [dict(r) for r in rows]
        }
    })


# ===== Admin: Review comment =====
@comments_bp.route('/admin/comments/<int:cid>/review', methods=['PUT'])
def admin_review_comment(cid):
    from routes.admin import _require_admin, _log
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True) or {}
    action = data.get('action', '')  # approve / reject / delete

    if action == 'delete':
        try:
            with get_db() as conn:
                conn.execute('DELETE FROM article_comments WHERE id=%s', (cid,))
                conn.commit()
        except Exception:
            return jsonify({'success': False, 'error': 'Query failed'}), 500
        _log(admin['user_id'], 'comment_delete', 'comment', str(cid))
        return jsonify({'success': True})

    if action not in ('approve', 'reject'):
        return jsonify({'success': False, 'error': 'Invalid action'}), 400

    new_status = 'approved' if action == 'approve' else 'rejected'
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE article_comments SET status=%s, reviewed_at=NOW(), reviewed_by=%s WHERE id=%s",
                (new_status, admin['user_id'], cid)
            )
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500

    _log(admin['user_id'], f'comment_{action}', 'comment', str(cid))
    return jsonify({'success': True, 'status': new_status})


# ===== Admin: Comment stats =====
@comments_bp.route('/admin/comments/stats', methods=['GET'])
def admin_comment_stats():
    from routes.admin import _require_admin
    admin, err = _require_admin()
    if err: return err

    try:
        with get_db() as conn:
            total = conn.execute('SELECT COUNT(*) as c FROM article_comments').fetchone()['c']
            pending = conn.execute("SELECT COUNT(*) as c FROM article_comments WHERE status='pending'").fetchone()['c']
            approved = conn.execute("SELECT COUNT(*) as c FROM article_comments WHERE status='approved'").fetchone()['c']
            rejected = conn.execute("SELECT COUNT(*) as c FROM article_comments WHERE status='rejected'").fetchone()['c']
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500

    return jsonify({
        'success': True,
        'data': {
            'total': total,
            'pending': pending,
            'approved': approved,
            'rejected': rejected,
        }
    })
