#!/usr/bin/env python3
"""Header Navigation Management Routes — 顶部导航管理后台接口"""
from i18n import _
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flask import Blueprint, request, jsonify
from models.database import get_db
from routes.admin import _require_admin, _log

header_bp = Blueprint('header_admin', __name__, url_prefix='/admin')


@header_bp.route('/header-nav', methods=['GET'])
def get_header_nav():
    """获取指定子站的顶部导航列表"""
    admin, err = _require_admin()
    if err: return err
    site = request.args.get('site', 'platform')
    if site not in ('platform',):
        return jsonify({'success': False, 'error': _('Invalid site parameter')}), 400
    try:
        with get_db() as conn:
            rows = conn.execute(
                'SELECT id, site, title, url, sort_order, is_enabled FROM header_nav WHERE site=%s ORDER BY sort_order ASC, id ASC',
                (site,)
            ).fetchall()
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@header_bp.route('/header-nav', methods=['POST'])
def create_header_nav():
    """新增顶部导航链接"""
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    site = data.get('site', 'platform').strip()
    title = data.get('title', '').strip()
    url = data.get('url', '').strip()
    if site not in ('platform',):
        return jsonify({'success': False, 'error': _('Site must be platform')}), 400
    if not title or not url:
        return jsonify({'success': False, 'error': _('Title and URL are required')}), 400
    try:
        with get_db() as conn:
            m = conn.execute('SELECT MAX(sort_order) as m FROM header_nav WHERE site=%s', (site,)).fetchone()
            order = (m['m'] or 0) + 1 if m else 1
            new_id = conn.execute(
                'INSERT INTO header_nav (site, title, url, sort_order, is_enabled) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                (site, title, url, order, 1 if data.get('is_enabled', True) else 0)
            ).fetchone()['id']
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
    _log(admin['user_id'], 'create', 'header_nav', str(new_id), f'{site}/{title}')
    return jsonify({'success': True, 'data': {'id': new_id}})


@header_bp.route('/header-nav/<int:item_id>', methods=['PUT'])
def update_header_nav(item_id):
    """更新顶部导航链接"""
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    try:
        with get_db() as conn:
            existing = conn.execute('SELECT id FROM header_nav WHERE id=%s', (item_id,)).fetchone()
            if not existing:
                return jsonify({'success': False, 'error': _('Does not exist')}), 404
            conn.execute(
                'UPDATE header_nav SET title=%s, url=%s, is_enabled=%s, sort_order=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s',
                (data.get('title', '').strip(), data.get('url', '').strip(),
                 1 if data.get('is_enabled', True) else 0,
                 data.get('sort_order', 0), item_id)
            )
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
    _log(admin['user_id'], 'update', 'header_nav', str(item_id), '')
    return jsonify({'success': True, 'message': _('Updated')})


@header_bp.route('/header-nav/<int:item_id>', methods=['DELETE'])
def delete_header_nav(item_id):
    """删除顶部导航链接"""
    admin, err = _require_admin()
    if err: return err
    try:
        with get_db() as conn:
            r = conn.execute('SELECT title FROM header_nav WHERE id=%s', (item_id,)).fetchone()
            if not r:
                return jsonify({'success': False, 'error': _('Does not exist')}), 404
            conn.execute('DELETE FROM header_nav WHERE id=%s', (item_id,))
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
    _log(admin['user_id'], 'delete', 'header_nav', str(item_id), r['title'])
    return jsonify({'success': True, 'message': _('Deleted')})


@header_bp.route('/header-nav/reorder', methods=['POST'])
def reorder_header_nav():
    """拖拽排序"""
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    items = data.get('items', [])
    if not items:
        return jsonify({'success': False, 'error': _('Items cannot be empty')}), 400
    try:
        with get_db() as conn:
            for i, item in enumerate(items):
                conn.execute('UPDATE header_nav SET sort_order=%s WHERE id=%s', (i, item.get('id')))
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
    _log(admin['user_id'], 'reorder', 'header_nav', '', f'{len(items)} items')
    return jsonify({'success': True, 'message': _('Sort has been saved')})
