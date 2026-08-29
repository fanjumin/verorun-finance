#!/usr/bin/env python3
"""Social Media Management Routes -- 社交媒体管理后台接口"""
from i18n import _
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flask import Blueprint, request, jsonify
from models import get_db
from routes.admin import _require_admin, _log

social_media_bp = Blueprint('social_media', __name__, url_prefix='/admin')


@social_media_bp.route('/social-media', methods=['GET'])
def get_social_media():
    """获取所有社交媒体链接 (管理员)"""
    admin, err = _require_admin()
    if err:
        return err
    try:
        with get_db() as conn:
            rows = conn.execute(
                'SELECT id, platform_name, icon_type, icon_value, url, display_order, is_enabled, hover_text, created_at, updated_at FROM social_media_links ORDER BY display_order ASC, id ASC'
            ).fetchall()
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@social_media_bp.route('/social-media', methods=['POST'])
def create_social_media():
    """创建新的社交媒体链接"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    platform_name = data.get('platform_name', '').strip()
    icon_type = data.get('icon_type', 'fontawesome').strip()
    icon_value = data.get('icon_value', '').strip()
    url = data.get('url', '').strip()
    hover_text = data.get('hover_text', '').strip()
    is_enabled = 1 if data.get('is_enabled', True) else 0
    
    if not platform_name or not icon_value or not url:
        return jsonify({'success': False, 'error': _('Platform name, icon, and link are required fields')}), 400
    
    # 获取最大 display_order
    try:
        with get_db() as conn:
            max_order_row = conn.execute('SELECT MAX(display_order) as m FROM social_media_links').fetchone()
            max_order = (max_order_row['m'] or 0) + 1 if max_order_row else 1
        
            new_id = conn.execute(
                'INSERT INTO social_media_links (platform_name, icon_type, icon_value, url, display_order, is_enabled, hover_text) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                (platform_name, icon_type, icon_value, url, max_order, is_enabled, hover_text)
            ).fetchone()['id']
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
    
    _log(admin['user_id'], 'create', 'social_media', str(new_id), platform_name)
    return jsonify({'success': True, 'data': {'id': new_id, 'message': _('Creation successful')}})


@social_media_bp.route('/social-media/<int:sm_id>', methods=['PUT'])
def update_social_media(sm_id):
    """更新社交媒体链接"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    platform_name = data.get('platform_name', '').strip()
    icon_type = data.get('icon_type', 'fontawesome').strip()
    icon_value = data.get('icon_value', '').strip()
    url = data.get('url', '').strip()
    hover_text = data.get('hover_text', '').strip()
    is_enabled = 1 if data.get('is_enabled', True) else 0
    
    if not platform_name or not icon_value or not url:
        return jsonify({'success': False, 'error': _('Platform name, icon, and link are required fields')}), 400
    
    try:
        with get_db() as conn:
            conn.execute(
                'UPDATE social_media_links SET platform_name=%s, icon_type=%s, icon_value=%s, url=%s, is_enabled=%s, hover_text=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s',
                (platform_name, icon_type, icon_value, url, is_enabled, hover_text, sm_id)
            )
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
    
    _log(admin['user_id'], 'update', 'social_media', str(sm_id), platform_name)
    return jsonify({'success': True, 'message': _('Update successful')})


@social_media_bp.route('/social-media/<int:sm_id>', methods=['DELETE'])
def delete_social_media(sm_id):
    """删除社交媒体链接"""
    admin, err = _require_admin()
    if err:
        return err
    
    try:
        with get_db() as conn:
            # 获取要删除的记录名称
            row = conn.execute('SELECT platform_name FROM social_media_links WHERE id=%s', (sm_id,)).fetchone()
            if not row:
                return jsonify({'success': False, 'error': _('Record does not exist')}), 404
        
            conn.execute('DELETE FROM social_media_links WHERE id=%s', (sm_id,))
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
    
    _log(admin['user_id'], 'delete', 'social_media', str(sm_id), row['platform_name'])
    return jsonify({'success': True, 'message': _('Deletion Successful')})


@social_media_bp.route('/social-media/reorder', methods=['PUT'])
def reorder_social_media():
    """批量更新社交媒体排序"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    order_list = data.get('order', [])  # [{'id': 1, 'order': 1}, ...]
    
    if not order_list:
        return jsonify({'success': False, 'error': _('Sort list cannot be empty')}), 400
    
    try:
        with get_db() as conn:
            for item in order_list:
                sm_id = item.get('id')
                order = item.get('order', 0)
                if sm_id:
                    conn.execute('UPDATE social_media_links SET display_order=%s WHERE id=%s', (order, sm_id))
            conn.commit()
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
    
    _log(admin['user_id'], 'reorder', 'social_media', '', f'Sorted {len(order_list)} items')
    return jsonify({'success': True, 'message': _('Sort successful')})


# ════════════════════════════════════════════════════════════════
# 公开 API - 供前台调用（不需要认证）
# ════════════════════════════════════════════════════════════════
@social_media_bp.route('/api/social-media', methods=['GET'])
def get_enabled_social_media():
    """获取已启用的社交媒体链接 (前台使用，无需认证)"""
    try:
        with get_db() as conn:
            rows = conn.execute(
                'SELECT id, platform_name, icon_type, icon_value, url, hover_text FROM social_media_links WHERE is_enabled=1 ORDER BY display_order ASC, id ASC'
            ).fetchall()
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})

# 向后兼容别名
@social_media_bp.route('/api/social-links', methods=['GET'])
def get_social_links_alias():
    """获取已启用的社交媒体链接 - 别名端点 (向后兼容旧前端页面)"""
    return get_enabled_social_media()
