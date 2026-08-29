#!/usr/bin/env python3
"""Footer Management Routes — 页脚管理后台接口"""
from i18n import _
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flask import Blueprint, request, jsonify
from models import get_db
from routes.admin import _require_admin, _log

footer_bp = Blueprint('footer_admin', __name__, url_prefix='/admin')

# ════════════════════════════════════════════════════════
# 1. 站内连接 (footer_links)
# ════════════════════════════════════════════════════════
@footer_bp.route('/footer-links', methods=['GET'])
def get_footer_links():
    admin, err = _require_admin()
    if err: return err
    try:
        with get_db() as conn:
            rows = conn.execute(
                'SELECT id, section, title, url, sort_order, is_enabled, created_at, updated_at FROM footer_links ORDER BY section ASC, sort_order ASC, id ASC'
            ).fetchall()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})

@footer_bp.route('/footer-links', methods=['POST'])
def create_footer_link():
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    section = data.get('section', '').strip()
    title = data.get('title', '').strip()
    url = data.get('url', '').strip()
    is_enabled = 1 if data.get('is_enabled', True) else 0
    if not section or not title or not url:
        return jsonify({'success': False, 'error': _('Section, title, and URL are required')}), 400
    try:
        with get_db() as conn:
            max_order = conn.execute('SELECT MAX(sort_order) as m FROM footer_links WHERE section=%s', (section,)).fetchone()
            order = (max_order['m'] or 0) + 1 if max_order else 1
            new_id = conn.execute(
                'INSERT INTO footer_links (section, title, url, sort_order, is_enabled) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                (section, title, url, order, is_enabled)
            ).fetchone()['id']
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'create', 'footer_links', str(new_id), f'{section}/{title}')
    return jsonify({'success': True, 'data': {'id': new_id}})

@footer_bp.route('/footer-links/<int:item_id>', methods=['PUT'])
def update_footer_link(item_id):
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    try:
        with get_db() as conn:
            conn.execute(
                'UPDATE footer_links SET section=%s, title=%s, url=%s, is_enabled=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s',
                (data.get('section','').strip(), data.get('title','').strip(), data.get('url','').strip(),
                 1 if data.get('is_enabled',True) else 0, item_id)
            )
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'update', 'footer_links', str(item_id), '')
    return jsonify({'success': True, 'message': _('Updated')})

@footer_bp.route('/footer-links/<int:item_id>', methods=['DELETE'])
def delete_footer_link(item_id):
    admin, err = _require_admin()
    if err: return err
    try:
        with get_db() as conn:
            r = conn.execute('SELECT title FROM footer_links WHERE id=%s', (item_id,)).fetchone()
            if not r: return jsonify({'success': False, 'error': _('Does not exist')}), 404
            conn.execute('DELETE FROM footer_links WHERE id=%s', (item_id,))
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'delete', 'footer_links', str(item_id), r['title'])
    return jsonify({'success': True, 'message': _('Deleted')})

# ════════════════════════════════════════════════════════
# 2. 站内导航 (footer_nav)
# ════════════════════════════════════════════════════════
@footer_bp.route('/footer-nav', methods=['GET'])
def get_footer_nav():
    admin, err = _require_admin()
    if err: return err
    try:
        with get_db() as conn:
            rows = conn.execute('SELECT id, title, url, sort_order, is_enabled FROM footer_nav ORDER BY sort_order ASC, id ASC').fetchall()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})

@footer_bp.route('/footer-nav', methods=['POST'])
def create_footer_nav():
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    title = data.get('title', '').strip()
    url = data.get('url', '').strip()
    if not title or not url:
        return jsonify({'success': False, 'error': _('Title and URL are required')}), 400
    try:
        with get_db() as conn:
            m = conn.execute('SELECT MAX(sort_order) as m FROM footer_nav').fetchone()
            order = (m['m'] or 0) + 1 if m else 1
            new_id = conn.execute('INSERT INTO footer_nav (title, url, sort_order, is_enabled) VALUES (%s,%s,%s,%s) RETURNING id',
                (title, url, order, 1 if data.get('is_enabled', True) else 0)).fetchone()['id']
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'create', 'footer_nav', str(new_id), title)
    return jsonify({'success': True, 'data': {'id': new_id}})

@footer_bp.route('/footer-nav/<int:item_id>', methods=['PUT'])
def update_footer_nav(item_id):
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    try:
        with get_db() as conn:
            if 'sort_order' in data:
                conn.execute('UPDATE footer_nav SET title=%s, url=%s, is_enabled=%s, sort_order=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s',
                    (data.get('title','').strip(), data.get('url','').strip(), 1 if data.get('is_enabled',True) else 0, data['sort_order'], item_id))
            else:
                conn.execute('UPDATE footer_nav SET title=%s, url=%s, is_enabled=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s',
                    (data.get('title','').strip(), data.get('url','').strip(), 1 if data.get('is_enabled',True) else 0, item_id))
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'update', 'footer_nav', str(item_id), '')
    return jsonify({'success': True, 'message': _('Updated')})

@footer_bp.route('/footer-nav/<int:item_id>', methods=['DELETE'])
def delete_footer_nav(item_id):
    admin, err = _require_admin()
    if err: return err
    try:
        with get_db() as conn:
            r = conn.execute('SELECT title FROM footer_nav WHERE id=%s', (item_id,)).fetchone()
            if not r: return jsonify({'success': False, 'error': _('Does not exist')}), 404
            conn.execute('DELETE FROM footer_nav WHERE id=%s', (item_id,))
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'delete', 'footer_nav', str(item_id), r['title'])
    return jsonify({'success': True, 'message': _('Deleted')})

# ════════════════════════════════════════════════════════
# 3. 页脚文章 (footer_articles)
# ════════════════════════════════════════════════════════
@footer_bp.route('/footer-articles', methods=['GET'])
def get_footer_articles():
    admin, err = _require_admin()
    if err: return err
    try:
        with get_db() as conn:
            rows = conn.execute('SELECT id, title, url, sort_order, is_enabled FROM footer_articles ORDER BY sort_order ASC, id ASC').fetchall()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})

@footer_bp.route('/footer-articles', methods=['POST'])
def create_footer_article():
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    title = data.get('title', '').strip()
    url = data.get('url', '').strip()
    if not title or not url:
        return jsonify({'success': False, 'error': _('Title and URL are required')}), 400
    try:
        with get_db() as conn:
            m = conn.execute('SELECT MAX(sort_order) as m FROM footer_articles').fetchone()
            order = (m['m'] or 0) + 1 if m else 1
            new_id = conn.execute('INSERT INTO footer_articles (title, url, sort_order, is_enabled) VALUES (%s,%s,%s,%s) RETURNING id',
                (title, url, order, 1 if data.get('is_enabled', True) else 0)).fetchone()['id']
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'create', 'footer_articles', str(new_id), title)
    return jsonify({'success': True, 'data': {'id': new_id}})

@footer_bp.route('/footer-articles/<int:item_id>', methods=['PUT'])
def update_footer_article(item_id):
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    try:
        with get_db() as conn:
            conn.execute('UPDATE footer_articles SET title=%s, url=%s, is_enabled=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s',
                (data.get('title','').strip(), data.get('url','').strip(), 1 if data.get('is_enabled',True) else 0, item_id))
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'update', 'footer_articles', str(item_id), '')
    return jsonify({'success': True, 'message': _('Updated')})

@footer_bp.route('/footer-articles/<int:item_id>', methods=['DELETE'])
def delete_footer_article(item_id):
    admin, err = _require_admin()
    if err: return err
    try:
        with get_db() as conn:
            r = conn.execute('SELECT title FROM footer_articles WHERE id=%s', (item_id,)).fetchone()
            if not r: return jsonify({'success': False, 'error': _('Does not exist')}), 404
            conn.execute('DELETE FROM footer_articles WHERE id=%s', (item_id,))
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'delete', 'footer_articles', str(item_id), r['title'])
    return jsonify({'success': True, 'message': _('Deleted')})

# ════════════════════════════════════════════════════════
# 4. 生态伙伴 (partner_links)
# ════════════════════════════════════════════════════════
@footer_bp.route('/partners', methods=['GET'])
def get_partners():
    admin, err = _require_admin()
    if err: return err
    try:
        with get_db() as conn:
            rows = conn.execute('SELECT id, name, url, icon_url, sort_order, is_enabled FROM partner_links ORDER BY sort_order ASC, id ASC').fetchall()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})

@footer_bp.route('/partners', methods=['POST'])
def create_partner():
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    name = data.get('name', '').strip()
    url = data.get('url', '').strip()
    if not name or not url:
        return jsonify({'success': False, 'error': _('Name and URL are required')}), 400
    try:
        with get_db() as conn:
            m = conn.execute('SELECT MAX(sort_order) as m FROM partner_links').fetchone()
            order = (m['m'] or 0) + 1 if m else 1
            new_id = conn.execute('INSERT INTO partner_links (name, url, icon_url, sort_order, is_enabled) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                (name, url, data.get('icon_url','').strip(), order, 1 if data.get('is_enabled', True) else 0)).fetchone()['id']
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'create', 'partner_links', str(new_id), name)
    return jsonify({'success': True, 'data': {'id': new_id}})

@footer_bp.route('/partners/<int:item_id>', methods=['PUT'])
def update_partner(item_id):
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    try:
        with get_db() as conn:
            conn.execute('UPDATE partner_links SET name=%s, url=%s, icon_url=%s, is_enabled=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s',
                (data.get('name','').strip(), data.get('url','').strip(), data.get('icon_url','').strip(),
                 1 if data.get('is_enabled', True) else 0, item_id))
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'update', 'partner_links', str(item_id), '')
    return jsonify({'success': True, 'message': _('Updated')})

@footer_bp.route('/partners/<int:item_id>', methods=['DELETE'])
def delete_partner(item_id):
    admin, err = _require_admin()
    if err: return err
    try:
        with get_db() as conn:
            r = conn.execute('SELECT name FROM partner_links WHERE id=%s', (item_id,)).fetchone()
            if not r: return jsonify({'success': False, 'error': _('Does not exist')}), 404
            conn.execute('DELETE FROM partner_links WHERE id=%s', (item_id,))
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    _log(admin['user_id'], 'delete', 'partner_links', str(item_id), r['name'])
    return jsonify({'success': True, 'message': _('Deleted')})

# ════════════════════════════════════════════════════════
# 公开 API - 前台使用（无需认证）
# ════════════════════════════════════════════════════════
@footer_bp.route('/api/footer-links', methods=['GET'])
def public_footer_links():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT section, title, url FROM footer_links WHERE is_enabled=1 ORDER BY section ASC, sort_order ASC").fetchall()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})

@footer_bp.route('/api/footer-nav', methods=['GET'])
def public_footer_nav():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT title, url FROM footer_nav WHERE is_enabled=1 ORDER BY sort_order ASC").fetchall()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})

@footer_bp.route('/api/footer-articles', methods=['GET'])
def public_footer_articles():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT title, url FROM footer_articles WHERE is_enabled=1 ORDER BY sort_order ASC").fetchall()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})

@footer_bp.route('/api/partners', methods=['GET'])
def public_partners():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT name, url, icon_url FROM partner_links WHERE is_enabled=1 ORDER BY sort_order ASC").fetchall()
    except Exception:
        return jsonify({'success': False, 'error': 'Query failed'}), 500
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})
