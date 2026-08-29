# -*- coding: utf-8 -*-
"""Auto-generated split from admin.py"""
from .admin import admin_bp, _require_admin, _log, _cached_get
from i18n import _
from datetime import datetime, timedelta
from flask import Response, jsonify, request
from models import get_db
import os
import json

@admin_bp.route('/posts', methods=['GET'])
@_cached_get(ttl=3)
def post_list():
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 20, type=int)
    sf = request.args.get('status', chr(39)+chr(39)).strip()
    offset = (page - 1) * limit
    w = []
    p = []
    if sf:
        w.append('e.status=%s')
        p.append(sf)
    wsql = ('WHERE ' + ' AND '.join(w)) if w else ''
    sql = 'SELECT e.id, e.title, e.category, e.status, e.is_published, e.like_count, e.view_count, e.created_at, e.agent_id, COALESCE(u.display_name, u.username) as user_name FROM agent_experiences e LEFT JOIN users u ON e.user_id=u.id ' + wsql + ' ORDER BY e.created_at DESC LIMIT %s OFFSET %s'
    with get_db() as conn:
        total = conn.execute('SELECT COUNT(*) as c FROM agent_experiences e ' + wsql, p).fetchone()
        rows = conn.execute(sql, p + [limit, offset]).fetchall()
    return jsonify({'success': True, 'data': {'total': total['c'], 'page': page, 'limit': limit, 'posts': [dict(r) for r in rows]}})


@admin_bp.route('/posts/<int:pid>/review', methods=['PUT'])
def review_post(pid):
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    status = data.get('status', 'approved')
    pub = 1 if status == 'approved' else 0
    with get_db() as conn:
        conn.execute("UPDATE agent_experiences SET status=%s, is_published=%s, updated_at=NOW() WHERE id=%s", (status, pub, pid))
        conn.commit()
    _log(admin['user_id'], 'review_post', 'post', str(pid), 'Status: ' + status)
    return jsonify({'success': True, 'message': _('Audit Completed')})


@admin_bp.route('/contacts', methods=['GET'])
@_cached_get(ttl=3)
def contact_list():
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 20, type=int)
    offset = (page - 1) * limit
    with get_db() as conn:
        total = conn.execute('SELECT COUNT(*) as c FROM contact_messages').fetchone()
        rows = conn.execute("SELECT id, name, email, subject, message, status, created_at FROM contact_messages ORDER BY CASE status WHEN 'unread' THEN 0 ELSE 1 END, created_at DESC LIMIT %s OFFSET %s", (limit, offset)).fetchall()
    return jsonify({'success': True, 'data': {'total': total['c'], 'page': page, 'limit': limit, 'contacts': [dict(r) for r in rows]}})


# =============================================
# social_links CRUD — 后台社媒图标管理
# =============================================
@admin_bp.route('/admin/social-links', methods=['GET'])
def get_social_links():
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM social_links ORDER BY sort_order ASC, id ASC').fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})

@admin_bp.route('/admin/social-links', methods=['POST'])
def create_social_link():
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    url = (data.get('url') or '#').strip()
    icon_url = (data.get('icon_url') or '').strip()
    platform = (data.get('platform') or '').strip()
    is_active = 1 if data.get('is_active', 1) else 0
    if not name:
        return jsonify({'success': False, 'error': _('Name cannot be empty')}), 400
    with get_db() as conn:
        max_sort = conn.execute('SELECT COALESCE(MAX(sort_order), -1) + 1 AS max_sort FROM social_links').fetchone()['max_sort']
        lid = conn.execute(
            'INSERT INTO social_links (name, url, icon_url, platform, sort_order, is_active) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id',
            (name, url, icon_url, platform, max_sort, is_active)
        ).fetchone()['id']
        conn.commit()
        _log(admin['user_id'], 'create', 'social_link', str(lid), f'Add Social Media Icon: {name}')
    return jsonify({'success': True, 'data': {'id': lid}})

@admin_bp.route('/admin/social-links/<int:lid>', methods=['PUT'])
def update_social_link(lid):
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    url = (data.get('url') or '').strip()
    icon_url = (data.get('icon_url') or '').strip()
    platform = (data.get('platform') or '').strip()
    is_active = data.get('is_active')
    with get_db() as conn:
        row = conn.execute('SELECT * FROM social_links WHERE id=%s', (lid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Does not exist')}), 404
        name = name or row['name']
        if not url: url = '#'
        platform = platform or row.get('platform', '')
        if is_active is not None:
            conn.execute('UPDATE social_links SET name=%s, url=%s, icon_url=%s, platform=%s, is_active=%s, updated_at=NOW() WHERE id=%s',
                         (name, url, icon_url, platform, 1 if is_active else 0, lid))
        else:
            conn.execute('UPDATE social_links SET name=%s, url=%s, icon_url=%s, platform=%s, updated_at=NOW() WHERE id=%s',
                         (name, url, icon_url, platform, lid))
        conn.commit()
        _log(admin['user_id'], 'update', 'social_link', str(lid), f'Update social media icon: {name}')
    return jsonify({'success': True})

@admin_bp.route('/admin/social-links/<int:lid>', methods=['DELETE'])
def delete_social_link(lid):
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        row = conn.execute('SELECT name FROM social_links WHERE id=%s', (lid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Does not exist')}), 404
        conn.execute('DELETE FROM social_links WHERE id=%s', (lid,))
        conn.commit()
        _log(admin['user_id'], 'delete', 'social_link', str(lid), f'Delete Social Media Icon: {row["name"]}')
    return jsonify({'success': True})

@admin_bp.route('/admin/social-links/reorder', methods=['PUT'])
def reorder_social_links():
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    ids = data.get('ids', [])
    with get_db() as conn:
        for idx, lid in enumerate(ids):
            conn.execute('UPDATE social_links SET sort_order=%s WHERE id=%s', (idx, lid))
        conn.commit()
    return jsonify({'success': True})

# =============================================
# Brand Settings — global site branding
# =============================================
@admin_bp.route('/brand-settings', methods=['GET'])
def get_brand_settings():
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        row = conn.execute('SELECT * FROM brand_settings WHERE id=1').fetchone()
    if row:
        return jsonify({'success': True, 'data': dict(row)})
    return jsonify({'success': True, 'data': None})


@admin_bp.route('/brand-settings', methods=['PUT'])
def update_brand_settings():
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(silent=True) or {}
    allowed = ['company_name', 'site_name_cn', 'site_name_en', 'slogan', 'tagline',
               'description', 'copyright', 'seo_title', 'seo_desc', 'logo_full_url',
               'logo_icon_url', 'icp_number', 'security_number', 'contact_email']
    updates = {k: data[k] for k in allowed if k in data}
    if not updates:
        return jsonify({'success': False, 'error': _('No Valid Update Fields')}), 400
    sets = ', '.join(f'{k}=%s' for k in updates)
    vals = list(updates.values()) + [1]
    with get_db() as conn:
        conn.execute(f'UPDATE brand_settings SET {sets}, updated_at=NOW() WHERE id=%s', vals)
        conn.commit()
    _log(admin['user_id'], 'update_brand', detail=str(list(updates.keys())))
    return jsonify({'success': True})


def _save_brand_image(subdir, file_key):
    """Save uploaded image to admin/static/brand/<subdir>/ AND sync to all services' static dirs."""
    import time
    file = request.files.get(file_key)
    if not file or not file.filename:
        return None, _('No file selected')
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'png'
    if ext not in ('png', 'jpg', 'jpeg', 'svg', 'ico'):
        return None, _('Only supports PNG/JPG/SVG/ICO formats')
    # Read + size check
    data = file.read()
    max_size = 500 * 1024  # 500KB
    if len(data) > max_size:
        return None, f'文件过大 ({len(data)//1024}KB)，限制 {max_size//1024}KB'
    # Safe filename
    ts = int(time.time() * 1000)
    fname = f'{subdir}_{ts}.{ext}'
    # Save to admin/static/brand/
    base = os.path.join(os.path.dirname(__file__), '..', '..')
    admin_dir = os.path.join(base, 'admin', 'static', 'brand')
    os.makedirs(admin_dir, exist_ok=True)
    with open(os.path.join(admin_dir, fname), 'wb') as f:
        f.write(data)
    # Sync to all other services that might serve brand images
    for svc in ('platform',):
        svc_dir = os.path.join(base, svc, 'static', 'brand')
        os.makedirs(svc_dir, exist_ok=True)
        with open(os.path.join(svc_dir, fname), 'wb') as f:
            f.write(data)
    return f'/static/brand/{fname}', None


@admin_bp.route('/brand-settings/logo', methods=['POST'])
def upload_brand_logo():
    admin, err = _require_admin()
    if err: return err
    url, error = _save_brand_image('logo', 'logo')
    if error:
        return jsonify({'success': False, 'error': error}), 400
    with get_db() as conn:
        conn.execute("UPDATE brand_settings SET logo_url=%s, logo_full_url=%s, updated_at=NOW() WHERE id=1", (url, url))
        conn.commit()
    _log(admin['user_id'], 'upload_brand_logo', detail=url)
    return jsonify({'success': True, 'logo_url': url})


@admin_bp.route('/brand-settings/logo', methods=['DELETE'])
def delete_brand_logo():
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        conn.execute("UPDATE brand_settings SET logo_url='', logo_full_url='', updated_at=NOW() WHERE id=1")
        conn.commit()
    _log(admin['user_id'], 'delete_brand_logo')
    return jsonify({'success': True})


@admin_bp.route('/brand-settings/favicon', methods=['POST'])
def upload_brand_favicon():
    admin, err = _require_admin()
    if err: return err
    url, error = _save_brand_image('favicon', 'favicon')
    if error:
        return jsonify({'success': False, 'error': error}), 400
    with get_db() as conn:
        conn.execute("UPDATE brand_settings SET favicon_url=%s, updated_at=NOW() WHERE id=1", (url,))
        conn.commit()
    _log(admin['user_id'], 'upload_brand_favicon', detail=url)
    return jsonify({'success': True, 'favicon_url': url})


@admin_bp.route('/brand-settings/favicon', methods=['DELETE'])
def delete_brand_favicon():
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        conn.execute("UPDATE brand_settings SET favicon_url='', updated_at=NOW() WHERE id=1")
        conn.commit()
    _log(admin['user_id'], 'delete_brand_favicon')
    return jsonify({'success': True})



@admin_bp.route('/brand-settings/logo-icon', methods=['POST'])
def upload_brand_logo_icon():
    """上传纯图标 Logo（用于 Favicon、Admin 侧栏等小尺寸场景）"""
    admin, err = _require_admin()
    if err: return err
    url, error = _save_brand_image('logo', 'logo_icon')
    if error:
        return jsonify({'success': False, 'error': error}), 400
    with get_db() as conn:
        conn.execute("UPDATE brand_settings SET logo_icon_url=%s, updated_at=NOW() WHERE id=1", (url,))
        conn.commit()
    _log(admin['user_id'], 'upload_brand_logo_icon', detail=url)
    return jsonify({'success': True, 'logo_icon_url': url})


@admin_bp.route('/brand-settings/logo-icon', methods=['DELETE'])
def delete_brand_logo_icon():
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        conn.execute("UPDATE brand_settings SET logo_icon_url='', updated_at=NOW() WHERE id=1")
        conn.commit()
@admin_bp.route('/interests', methods=['GET'])
def admin_interests_list():
    admin, err = _require_admin()
    if err: return err
    category = request.args.get('category', '').strip()
    with get_db() as conn:
        if category:
            rows = conn.execute(
                'SELECT * FROM interests WHERE category=%s ORDER BY sort_order, id', (category,)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM interests ORDER BY category, sort_order, id'
            ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@admin_bp.route('/interests', methods=['POST'])
def admin_interests_create():
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    category = (data.get('category') or '').strip()
    if not name or not category:
        return jsonify({'success': False, 'error': _('Name and Category cannot be empty')}), 400
    with get_db() as conn:
        existing = conn.execute('SELECT id FROM interests WHERE name=%s', (name,)).fetchone()
        if existing:
            return jsonify({'success': False, 'error': f'Tag "{name}" Already Exists'}), 409
        new_id = conn.execute(
            'INSERT INTO interests (name, category, sort_order, is_hot, is_active) VALUES (%s,%s,%s,%s,%s) RETURNING id',
            (name, category, data.get('sort_order', 0), data.get('is_hot', 0), data.get('is_active', 1))
        ).fetchone()['id']
        conn.commit()
    _log(admin['user_id'], 'create_interest', detail=f'{name} ({category})')
    return jsonify({'success': True, 'data': {'id': new_id}})


@admin_bp.route('/interests/<int:iid>', methods=['PUT'])
def admin_interests_update(iid):
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(silent=True) or {}
    updates = {}
    for k in ['name', 'category', 'sort_order', 'is_hot', 'is_active']:
        if k in data:
            updates[k] = data[k]
    if not updates:
        return jsonify({'success': False, 'error': _('No fields to update')}), 400
    sets = ', '.join(f'{k}=%s' for k in updates)
    vals = list(updates.values()) + [iid]
    with get_db() as conn:
        conn.execute(f'UPDATE interests SET {sets} WHERE id=%s', vals)
        conn.commit()
    _log(admin['user_id'], 'update_interest', detail=f'id={iid}')
    return jsonify({'success': True})


@admin_bp.route('/interests/<int:iid>', methods=['DELETE'])
def admin_interests_delete(iid):
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        conn.execute('DELETE FROM interests WHERE id=%s', (iid,))
        conn.execute('DELETE FROM user_interests WHERE interest_id=%s', (iid,))
        conn.commit()
    _log(admin['user_id'], 'delete_interest', detail=f'id={iid}')
    return jsonify({'success': True})


# ── Public interests API (grouped by category) ──

@admin_bp.route('/interests/public', methods=['GET'])
def public_interests():
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM interests WHERE is_active=1 AND is_hot=1 ORDER BY category, sort_order, id'
        ).fetchall()
    grouped = {}
    for r in rows:
        d = dict(r)
        cat = d['category']
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(d)
    return jsonify({'success': True, 'data': grouped})

@admin_bp.route('/downloads', methods=['GET'])
@_cached_get(ttl=3)
def admin_downloads_list():
    admin, err = _require_admin()
    if err:
        return err
    from models.cms import get_all_downloads
    items = get_all_downloads()
    return jsonify({'success': True, 'data': items})


@admin_bp.route('/downloads', methods=['POST'])
def admin_downloads_create():
    admin, err = _require_admin()
    if err:
        return err
    # Support both JSON and multipart form
    if request.is_json:
        data = request.get_json(force=True) or {}
    else:
        data = {}
        for k in ('name','slug','tagline','category','version','download_url','repo_url',
                  'file_size','license','requirements','docs_url','changelog_url','icon'):
            data[k] = request.form.get(k, '').strip()
        try:
            data['tags'] = json.loads(request.form.get('tags', '[]'))
        except Exception:
            data['tags'] = []
        try:
            data['sort_order'] = int(request.form.get('sort_order', 0))
        except Exception:
            data['sort_order'] = 0
        data['is_published'] = int(request.form.get('is_published', 1))
    slug = data.get('slug', '').strip()
    name = data.get('name', '').strip()
    if not slug or not name:
        return jsonify({'success': False, 'error': _('Slug and name cannot be empty')}), 400
    # Handle file upload
    uploaded_file = request.files.get('file') if not request.is_json else None
    if uploaded_file and uploaded_file.filename:
        import os
        dl_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'downloads')
        os.makedirs(dl_dir, exist_ok=True)
        ext = os.path.splitext(uploaded_file.filename)[1]
        safe_name = slug + ext
        filepath = os.path.join(dl_dir, safe_name)
        uploaded_file.save(filepath)
        data['download_url'] = '/static/downloads/' + safe_name
        if not data.get('file_size'):
            fsize = os.path.getsize(filepath)
            if fsize < 1048576:
                data['file_size'] = f'{fsize/1024:.1f} KB'
            else:
                data['file_size'] = f'{fsize/1048576:.1f} MB'
    from models.cms import upsert_download
    item = upsert_download(data)
    _log(admin['user_id'], 'create_download', 'download', str(item.get('id')), f'{name} ({slug})')
    return jsonify({'success': True, 'data': item})


@admin_bp.route('/downloads/<int:dl_id>', methods=['POST','PUT'])
def admin_downloads_update(dl_id):
    admin, err = _require_admin()
    if err:
        return err
    if request.is_json:
        data = request.get_json(force=True) or {}
    else:
        data = {}
        for k in ('name','slug','tagline','category','version','download_url','repo_url',
                  'file_size','license','requirements','docs_url','changelog_url','icon'):
            data[k] = request.form.get(k, '').strip()
        try:
            data['tags'] = json.loads(request.form.get('tags', '[]'))
        except Exception:
            data['tags'] = []
        try:
            data['sort_order'] = int(request.form.get('sort_order', 0))
        except Exception:
            data['sort_order'] = 0
        data['is_published'] = int(request.form.get('is_published', 1))
    data['id'] = dl_id
    # Handle file upload
    uploaded_file = request.files.get('file') if not request.is_json else None
    if uploaded_file and uploaded_file.filename:
        import os
        dl_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'downloads')
        os.makedirs(dl_dir, exist_ok=True)
        slug = data.get('slug', '') or str(dl_id)
        ext = os.path.splitext(uploaded_file.filename)[1]
        safe_name = slug + ext
        filepath = os.path.join(dl_dir, safe_name)
        uploaded_file.save(filepath)
        data['download_url'] = '/static/downloads/' + safe_name
        if not data.get('file_size'):
            fsize = os.path.getsize(filepath)
            if fsize < 1048576:
                data['file_size'] = f'{fsize/1024:.1f} KB'
            else:
                data['file_size'] = f'{fsize/1048576:.1f} MB'
    from models.cms import upsert_download
    item = upsert_download(data)
    _log(admin['user_id'], 'update_download', 'download', str(dl_id))
    return jsonify({'success': True, 'data': item})


@admin_bp.route('/downloads/<int:dl_id>', methods=['DELETE'])
def admin_downloads_delete(dl_id):
    admin, err = _require_admin()
    if err:
        return err
    from models.cms import delete_download
    delete_download(dl_id)
    _log(admin['user_id'], 'delete_download', 'download', str(dl_id))
    return jsonify({'success': True})


@admin_bp.route('/downloads/<int:dl_id>', methods=['GET'])
def admin_downloads_get(dl_id):
    """获取单个下载项（替代全量加载）"""
    admin, err = _require_admin()
    if err:
        return err
    from models.cms import get_download
    item = get_download(dl_id)
    if not item:
        return jsonify({'success': False, 'error': _('Does not exist')}), 404
    return jsonify({'success': True, 'data': item})


@admin_bp.route('/downloads/reorder', methods=['POST'])
def admin_downloads_reorder():
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'success': False, 'error': _('Ids cannot be empty')}), 400
    from models.cms import reorder_downloads
    reorder_downloads(ids)
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════
#  本地媒体库 API — 上传 / 列表 / 下载 / 删除 / 推送
# ═══════════════════════════════════════════════════════════

MEDIA_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               '..', '..', 'admin', 'static', 'media')

def _media_lib_ensure_dir():
    os.makedirs(MEDIA_LIB_DIR, exist_ok=True)
    os.makedirs(os.path.join(MEDIA_LIB_DIR, 'thumbs'), exist_ok=True)

@admin_bp.route('/media-library/upload', methods=['POST'])
def media_library_upload():
    admin, err = _require_admin()
    if err:
        return err
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': _('No file selected')}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'success': False, 'error': _('File name is empty')}), 400
    _media_lib_ensure_dir()
    import uuid as _uuid
    safe_name = _uuid.uuid4().hex + os.path.splitext(f.filename)[1].lower()
    save_path = os.path.join(MEDIA_LIB_DIR, safe_name)
    MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100MB
    file_data = f.read()
    if len(file_data) > MAX_UPLOAD_SIZE:
        return jsonify({'success': False, 'error': _('File size exceeds 100MB limit')}), 413
    with open(save_path, 'wb') as fout:
        fout.write(file_data)
    file_size = len(file_data)
    mime = f.content_type or 'application/octet-stream'
    # 兜底：浏览器可能不传正确 content_type，按扩展名补
    if mime == 'application/octet-stream' or not mime:
        ext = os.path.splitext(f.filename)[1].lower()
        ext_map = {'.mp4':'video/mp4','.mov':'video/quicktime','.avi':'video/x-msvideo',
                   '.webm':'video/webm','.mkv':'video/x-matroska','.flv':'video/x-flv','.m4v':'video/mp4',
                   '.mp3':'audio/mpeg','.wav':'audio/wav','.ogg':'audio/ogg','.flac':'audio/flac',
                   '.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.gif':'image/gif','.webp':'image/webp'}
        mime = ext_map.get(ext, mime)
    # 缩略图：视频缩略图由本地 FFmpeg 预生成后一并上传，服务器仅存储分发
    # 图片本身就是缩略图，不设 thumb_path，前端用 file_path 显示
    thumb_name = ''

    with get_db() as conn:
        try:
            new_id = conn.execute(
                "INSERT INTO media_files (filename, original_name, mime_type, file_size, file_path, thumb_path) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (safe_name, f.filename, mime, file_size, 'media/' + safe_name,
                 'media/thumbs/' + thumb_name if thumb_name else '')
            ).fetchone()['id']
        except Exception:
            cur = conn.execute(
                "INSERT INTO media_files (filename, original_name, mime_type, file_size, file_path, thumb_path) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (safe_name, f.filename, mime, file_size, 'media/' + safe_name,
                 'media/thumbs/' + thumb_name if thumb_name else '')
            )
            new_id = cur.lastrowid
        conn.commit()
    return jsonify({
        'success': True,
        'data': {
            'id': new_id, 'filename': safe_name, 'original_name': f.filename,
            'mime_type': mime, 'file_size': file_size,
            'thumb_path': 'media/thumbs/' + thumb_name if thumb_name else ''
        }
    })

@admin_bp.route('/media-library/list', methods=['GET'])
def media_library_list():
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 50, type=int)
    if limit > 500: limit = 500
    offset = (page - 1) * limit
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM media_files").fetchone()['c']
        rows = conn.execute(
            "SELECT id, filename, original_name, mime_type, file_size, file_path, thumb_path, push_status, created_at FROM media_files ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (limit, offset)
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows], 'total': total, 'page': page, 'limit': limit})

@admin_bp.route('/media-library/<int:fid>', methods=['DELETE'])
def media_library_delete(fid):
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute("SELECT * FROM media_files WHERE id=%s", (fid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('File does not exist')}), 404
        fp = os.path.join(MEDIA_LIB_DIR, row['filename'])
        if os.path.exists(fp):
            os.remove(fp)
        if row['thumb_path']:
            tp = os.path.join(MEDIA_LIB_DIR, '..', row['thumb_path'])
            if os.path.exists(tp):
                os.remove(tp)
        conn.execute("DELETE FROM media_files WHERE id=%s", (fid,))
        conn.commit()
    return jsonify({'success': True})

@admin_bp.route('/media-library/<int:fid>/download', methods=['GET'])
def media_library_download(fid):
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute("SELECT * FROM media_files WHERE id=%s", (fid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('File does not exist')}), 404
    fp = os.path.join(MEDIA_LIB_DIR, row['filename'])
    if not os.path.exists(fp):
        return jsonify({'success': False, 'error': _('File deleted')}), 404
    return _send_file_or_stream(fp, row['original_name'], row['mime_type'])

@admin_bp.route('/media-library/<int:fid>/push', methods=['POST'])
def media_library_push(fid):
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    target = data.get('target', 'feishu')
    with get_db() as conn:
        row = conn.execute("SELECT * FROM media_files WHERE id=%s", (fid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('File does not exist')}), 404

    file_url = deploy.url("agent") + "/static/" + row["file_path"]
    filename = row['original_name']
    mime = row['mime_type']

    result = {'success': True, 'target': target}
    if target in ('feishu', 'wecom'):
        try:
            im = None
            import flask as _flask
            pm = _flask.current_app.extensions.get('plugin_manager') if hasattr(_flask.current_app, 'extensions') else None
            if pm and pm.is_enabled('im_gateway'):
                im = pm.get_instance('im_gateway')
            if im is None:
                result = {'success': False, 'error': _('IM Gateway plugin is not enabled, cannot push')}
            else:
                im.push_media(target, file_url, filename, mime)
        except Exception as e:
            result = {'success': False, 'error': f'{target} Push Failed: ' + str(e)}

    if result['success']:
        with get_db() as conn:
            conn.execute(
                "UPDATE media_files SET push_status='done', push_target=%s, "
                "pushed_at=NOW(), updated_at=NOW() WHERE id=%s",
                (target, fid)
            )
            conn.commit()
    return jsonify(result)


# NOTE: 媒体推送函数（_push_media_to_feishu / _push_media_to_wecom / _upload_feishu_*
#        / _fetch_as_base64）已迁移至 plugins/im_gateway/adapters/，
#        由 media_library_push 通过插件实例 im.push_media() 调用。


def _send_file_or_stream(fp, filename, mime):
    from flask import Response, request as _req
    range_header = _req.headers.get('Range', None)
    size = os.path.getsize(fp)
    if range_header:
        import re
        byte_range = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if byte_range:
            start = int(byte_range.group(1))
            end = int(byte_range.group(2)) if byte_range.group(2) else size - 1
            length = end - start + 1
            with open(fp, 'rb') as f:
                f.seek(start)
                data = f.read(length)
            return Response(data, 206, {
                'Content-Type': mime,
                'Content-Range': 'bytes {}-{}/{}'.format(start, end, size),
                'Content-Length': str(length),
                'Accept-Ranges': 'bytes',
                'Content-Disposition': 'inline; filename="{}"'.format(filename)
            })
    from flask import send_file as _sf
    return _sf(fp, mimetype=mime, as_attachment=False,
               download_name=filename, conditional=True)
