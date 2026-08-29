# -*- coding: utf-8 -*-
"""Auto-generated split from admin.py"""
from .admin import admin_bp, _require_admin, _log, _cached_get
from i18n import _
from datetime import datetime, timedelta
from flask import Response, jsonify, request
from models import get_db, now_iso
import os
import json

@admin_bp.route('/users', methods=['GET'])
@_cached_get(ttl=3)
def user_list():
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 20, type=int)
    search = request.args.get("search", "").strip()
    tier_filter = request.args.get("tier", "").strip()
    industry = request.args.get("industry", "").strip()
    occupation = request.args.get("occupation", "").strip()
    region = request.args.get("region", "").strip()
    offset = (page - 1) * limit
    where = []
    params = []
    if search:
        where.append("(u.phone LIKE %s OR COALESCE(u.display_name, u.username) LIKE %s OR u.email LIKE %s)")
        s = '%' + search + '%'
        params.extend([s, s, s])
    if tier_filter:
        where.append('a.tier=%s')
        params.append(tier_filter)
    if industry:
        where.append("p.industry LIKE %s")
        params.append('%' + industry + '%')
    if occupation:
        where.append("p.occupation LIKE %s")
        params.append('%' + occupation + '%')
    if region:
        where.append("(p.province LIKE %s OR p.city LIKE %s OR p.district LIKE %s)")
        r = '%' + region + '%'
        params.extend([r, r, r])
    wsql = 'WHERE ' + ' AND '.join(where) if where else ''
    from_sql = ("FROM users u "
                "LEFT JOIN user_profiles p ON u.id=p.user_id "
                "LEFT JOIN user_addresses pa ON u.id=pa.user_id AND pa.is_default=1 AND pa.status=1")
    if industry or occupation or region:
        # If filtering, only join profiles (address join for region)
        pass
    sql = ("SELECT u.id, u.phone, COALESCE(u.display_name, u.username) as nickname, u.email, u.wechat_nickname, "
           "COALESCE((SELECT COUNT(*) FROM user_agents WHERE user_id=u.id),0) as agent_count, "
           "'' as agent_nickname, u.is_admin, u.active, u.created_at, u.last_login, "
           "'' as tier, '' as tier_expire_at, "
           "u.verified_by, u.verified_at, "
           "COALESCE(p.industry,'') as industry, COALESCE(p.occupation,'') as occupation "
           + from_sql + ' ' + wsql + ' GROUP BY u.id, p.industry, p.occupation ORDER BY u.created_at DESC LIMIT %s OFFSET %s')
    csql = 'SELECT COUNT(DISTINCT u.id) as c ' + from_sql + ' ' + wsql
    try:
        with get_db() as conn:
            total = conn.execute(csql, params).fetchone()
            rows = conn.execute(sql, params + [limit, offset]).fetchall()
    except Exception as e:
        return jsonify({"success": False, "error": _("Failed to query user list: ") + str(e)}), 500
    return jsonify({"success": True, "data": {
        'total': total['c'], 'page': page, 'limit': limit,
        'users': [dict(r) for r in rows],
    }})


@admin_bp.route('/users/<int:uid>', methods=['GET'])
def user_detail(uid):
    admin, err = _require_admin()
    if err:
        return err
    try:
        with get_db() as conn:
            user = conn.execute("SELECT id, username, phone, phone_verified, email, COALESCE(display_name, username, '') as nickname, "
                                "wechat_nickname, avatar_url, "
                                "verified_by, verified_at, display_name, "
                                "'' as agent_id, '' as agent_nickname, '' as agent_avatar_url, "
                                "is_admin, active, created_at, last_login "
                                "FROM users WHERE id=%s", (uid,)).fetchone()
            if not user:
                return jsonify({'success': False, 'error': chr(29992)+chr(25143)+chr(19981)+chr(23384)+chr(22312)}), 404
            auths = conn.execute('SELECT app_name, tier, tier_expire_at, calls_today, calls_total FROM app_authorizations WHERE user_id=%s', (uid,)).fetchall()
            orders = conn.execute('SELECT id, order_no, amount, item_type, item_desc, status, created_at FROM billing_orders WHERE user_id=%s ORDER BY created_at DESC LIMIT 10', (uid,)).fetchall()
        return jsonify({'success': True, 'data': {'user': dict(user), 'authorizations': [dict(a) for a in auths], 'orders': [dict(o) for o in orders]}})
    except Exception as e:
        return jsonify({'success': False, 'error': _('Query failed')}), 500

@admin_bp.route('/users/<int:uid>/status', methods=['PUT'])
def user_status(uid):
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    active = data.get('active', 1)
    try:
        with get_db() as conn:
            conn.execute('UPDATE users SET active=%s WHERE id=%s', (1 if active else 0, uid))
            conn.commit()
    except Exception as e:
        return jsonify({'success': False, 'error': _('Update failed')}), 500
    _log(admin['user_id'], 'ban_user' if not active else 'activate_user', 'user', str(uid))
    return jsonify({'success': True, 'message': chr(29366)+chr(24577)+chr(24050)+chr(26356)+chr(26032)})

# PUT /admin/users/<int:uid>/verify — 管理员手动标记用户为已实名（合规v2：不存储身份证号）
@admin_bp.route('/users/<int:uid>/verify', methods=['PUT'])
def admin_verify_user(uid):
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    real_name = (data.get('real_name') or '').strip()
    if not real_name:
        return jsonify({'success': False, 'error': _('Name cannot be empty"')}), 400
    with get_db() as conn:
        user = conn.execute('SELECT id, is_real_name_verified FROM users WHERE id=%s', (uid,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': _('User does not exist')}), 404
        if user['is_real_name_verified']:
            return jsonify({'success': False, 'error': _('User has completed real-name authentication')}), 400
        # 合规v2：只写 display_name + 认证标记，不存储身份证号
        conn.execute(
            'UPDATE users SET display_name=%s, verified_by=%s, verified_at=%s, is_real_name_verified=1, real_name_verified_at=%s WHERE id=%s',
            (real_name, 'manual', now_iso(), now_iso(), uid)
        )
        conn.commit()
    _log(admin['user_id'], 'verify_user', 'user', str(uid))
    return jsonify({'success': True, 'message': _('Real-name Authentication Completed (Manually Marked, No ID Information Stored)')})


# GET /admin/users/<int:uid>/profile — admin查看用户扩展资料+收货地址
@admin_bp.route('/users/<int:uid>/profile', methods=['GET'])
def user_profile_admin(uid):
    admin, err = _require_admin()
    if err:
        return err
    import json as _json
    with get_db() as conn:
        prof = conn.execute('''
            SELECT up.*, ind.name AS industry_name, co.name AS career_name
            FROM user_profiles up
            LEFT JOIN industries ind ON up.industry_id = ind.id
            LEFT JOIN career_options co ON up.career_id = co.id
            WHERE up.user_id=%s
        ''', (uid,)).fetchone()
        addrs = conn.execute('''
            SELECT ua.*,
                p.name as province_name,
                c.name as city_name,
                d.name as district_name,
                s.name as street_name
            FROM user_addresses ua
            LEFT JOIN regions p ON ua.province_code = p.code
            LEFT JOIN regions c ON ua.city_code = c.code
            LEFT JOIN regions d ON ua.district_code = d.code
            LEFT JOIN regions s ON ua.street_code = s.code
            WHERE ua.user_id=%s AND ua.status=1
            ORDER BY ua.is_default DESC, ua.created_at DESC
        ''', (uid,)).fetchall()

    if prof:
        p = dict(prof)
        try:
            p['interests'] = _json.loads(p.get('interests', '[]'))
        except Exception:
            p['interests'] = []
        # 兼容admin.html前端字段名
        p.setdefault('industry_id', None)
        p.setdefault('career_id', None)
        p.setdefault('industry_name', '')
        p.setdefault('career_name', '')
    else:
        p = {
            'user_id': uid, 'gender': '', 'birth_date': None,
            'age_group': '', 'occupation': '', 'industry': '',
            'industry_id': None, 'career_id': None,
            'industry_name': '', 'career_name': '',
            'interests': [], 'bio': '', 'created_at': '', 'updated_at': ''
        }

    # 转换地址列表为前端需要的字段名（province/city/district -> province_name等）
    addr_list = []
    for a in addrs:
        ad = dict(a)
        ad['province'] = ad.pop('province_name', '') or ''
        ad['city'] = ad.pop('city_name', '') or ''
        ad['district'] = ad.pop('district_name', '') or ''
        addr_list.append(ad)

    return jsonify({'success': True, 'data': {
        'profile': p, 'addresses': addr_list
    }})


# GET /admin/users/export — 脱敏导出用户列表
@admin_bp.route('/agents', methods=['GET'])
@_cached_get(ttl=3)
def agent_list():
    """Legacy endpoint — delegates to new user_agents query (2026-05-10)"""
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 20, type=int)
    search = request.args.get('search', chr(39)+chr(39)).strip()
    offset = (page - 1) * limit
    w = ''
    params = []
    if search:
        w = "WHERE (ua.agent_name LIKE %s OR COALESCE(u.display_name, u.username) LIKE %s)"
        s = '%' + search + '%'
        params.extend([s, s])
    with get_db() as conn:
        total = conn.execute(
            'SELECT COUNT(*) as c FROM user_agents ua LEFT JOIN users u ON ua.user_id=u.id ' + w,
            params
        ).fetchone()
        rows = conn.execute(
            'SELECT ua.id, ua.agent_name, ua.agent_type, ua.status, ua.created_at, '
            "u.id as user_id, COALESCE(u.display_name, u.username) as user_name, u.phone "
            'FROM user_agents ua LEFT JOIN users u ON ua.user_id=u.id ' +
            w + ' ORDER BY ua.created_at DESC LIMIT %s OFFSET %s',
            params + [limit, offset]
        ).fetchall()
    return jsonify({'success': True, 'data': {'total': total['c'], 'page': page, 'limit': limit, 'agents': [dict(r) for r in rows]}})

def _require_super_admin():
    """验证当前用户是 super_admin"""
    admin, err = _require_admin()
    if err:
        return None, err
    with get_db() as conn:
        row = conn.execute('SELECT role FROM admin_profiles WHERE user_id=%s', (admin['user_id'],)).fetchone()
    if not row or row['role'] != 'super_admin':
        return None, (jsonify({'success': False, 'error': _('Only super administrators can perform this action')}), 403)
    return admin, None


@admin_bp.route('/admins', methods=['GET'])
def admin_list():
    """列出所有管理员（带完整 profile），仅 super_admin 可见"""
    admin, err = _require_super_admin()
    if err:
        return err
    try:
        with get_db() as conn:
            rows = conn.execute('''
                SELECT u.id, u.phone, COALESCE(u.display_name, u.username), u.email, u.avatar_url, u.active, 
                       u.last_login, u.created_at as registered_at,
                       p.role, p.permissions, p.real_name, p.internal_phone, 
                       p.internal_email, p.notes, p.last_login_ip
                FROM users u 
                JOIN admin_profiles p ON u.id = p.user_id
                WHERE u.is_admin = 1
                ORDER BY p.role, u.id
            ''').fetchall()
        admins = []
        for r in rows:
            d = dict(r)
            try:
                d['permissions'] = __import__('json').loads(d['permissions'] or '[]')
            except Exception:
                d['permissions'] = []
            admins.append(d)
        return jsonify({'success': True, 'data': admins})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Failed to load admins: {str(e)}'}), 500


@admin_bp.route('/admins/me', methods=['GET'])
def admin_me():
    """当前管理员的个人信息"""
    admin, err = _require_admin()
    if err:
        return err
    try:
        with get_db() as conn:
            row = conn.execute('''
                SELECT u.id, u.phone, COALESCE(u.display_name, u.username), u.email, u.avatar_url,
                       p.role, p.permissions, p.real_name, p.internal_phone,
                       p.internal_email, p.notes, p.last_login_ip, p.last_login_at,
                       p.created_at as admin_since
                FROM users u 
                JOIN admin_profiles p ON u.id = p.user_id
                WHERE u.id = %s
            ''', (admin['user_id'],)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Administrator configuration does not exist')}), 404
        d = dict(row)
        try:
            d['permissions'] = __import__('json').loads(d['permissions'] or '[]')
        except Exception:
            d['permissions'] = []
        return jsonify({'success': True, 'data': d})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Failed to load admin profile: {str(e)}'}), 500


@admin_bp.route('/admins/me', methods=['PUT'])
def admin_me_update():
    """当前管理员更新自己的个人信息（真实姓名、内部联系方式、备注）"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    fields = []
    params = []
    for key in ('real_name', 'internal_phone', 'internal_email', 'notes'):
        if key in data:
            fields.append(f'{key}=%s')
            params.append(data.get(key, chr(39)+chr(39)).strip())
    if not fields:
        return jsonify({'success': False, 'error': _('No fields to update')}), 400
    params.append(admin['user_id'])
    with get_db() as conn:
        conn.execute(f'UPDATE admin_profiles SET {", ".join(fields)}, updated_at=NOW() WHERE user_id=%s', params)
        conn.commit()
        _log(admin['user_id'], 'update_self', 'admin_profile', str(admin['user_id']))
    return jsonify({'success': True, 'message': _('Updated')})


@admin_bp.route('/admins/me/phone', methods=['PUT'])
def admin_me_phone():
    """当前管理员修改登录手机号 — 需新手机验证码"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    new_phone = data.get('phone', chr(39)+chr(39)).strip()
    code = data.get('code', chr(39)+chr(39)).strip()
    if not new_phone or not code:
        return jsonify({'success': False, 'error': _('Phone number and verification code cannot be empty')}), 400
    from models import get_db
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM sms_codes WHERE phone=%s AND code=%s AND purpose=%s AND used=0 AND expires_at>NOW() ORDER BY id DESC LIMIT 1',
            (new_phone, code, 'change_phone')
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Invalid or expired verification code')}), 400
        # 检查新手机号是否已占用
        existing = conn.execute('SELECT id FROM users WHERE phone=%s AND id!=%s', (new_phone, admin['user_id'])).fetchone()
        if existing:
            return jsonify({'success': False, 'error': _('This phone number is already bound to another user')}), 400
        conn.execute('UPDATE sms_codes SET used=1 WHERE id=%s', (row['id'],))
        conn.execute('UPDATE users SET phone=%s, phone_verified=1 WHERE id=%s', (new_phone, admin['user_id']))
        conn.commit()
        _log(admin['user_id'], 'change_phone', 'admin_profile', str(admin['user_id']), f'New Phone: {new_phone}')
    return jsonify({'success': True, 'message': _('Phone number has been updated')})


@admin_bp.route('/admins/<int:uid>', methods=['GET'])
def admin_detail(uid):
    """查看指定管理员详情（super_admin only）"""
    admin, err = _require_super_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute('''
            SELECT u.id, u.phone, COALESCE(u.display_name, u.username), u.email, u.avatar_url, u.active, u.last_login,
                   p.role, p.permissions, p.real_name, p.internal_phone,
                   p.internal_email, p.notes, p.last_login_ip, p.last_login_at,
                   p.created_at as admin_since, p.updated_at
            FROM users u 
            JOIN admin_profiles p ON u.id = p.user_id
            WHERE u.id=%s AND u.is_admin=1
        ''', (uid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Administrator does not exist')}), 404
        d = dict(row)
        try:
            d['permissions'] = __import__('json').loads(d['permissions'] or '[]')
        except Exception:
            d['permissions'] = []
        # 审计日志
        logs = conn.execute(
            'SELECT id, action, target_type, target_id, detail, ip_address, created_at FROM admin_logs WHERE admin_id=%s ORDER BY created_at DESC LIMIT 30',
            (uid,)
        ).fetchall()
        d['recent_logs'] = [dict(l) for l in logs]
    return jsonify({'success': True, 'data': d})


@admin_bp.route('/admins', methods=['POST'])
def admin_create():
    """将用户提升为管理员（super_admin only）"
    """
    admin, err = _require_super_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    phone = data.get('phone', chr(39)+chr(39)).strip()
    uid = data.get('user_id', 0)
    role = data.get('role', 'admin').strip()
    permissions = data.get('permissions', [])
    real_name = data.get('real_name', chr(39)+chr(39)).strip()[:32]
    notes = data.get('notes', chr(39)+chr(39)).strip()[:256]
    
    if not phone and not uid:
        return jsonify({'success': False, 'error': '请提供手机号或用户ID'}), 400
    
    with get_db() as conn:
        if uid:
            user = conn.execute('SELECT id, phone, display_name FROM users WHERE id=%s', (uid,)).fetchone()
        else:
            user = conn.execute('SELECT id, phone, display_name FROM users WHERE phone=%s', (phone,)).fetchone()
        
        if not user:
            return jsonify({'success': False, 'error': _('User does not exist')}), 404
        
        if user['id'] == admin['user_id']:
            return jsonify({'success': False, 'error': _('Cannot promote yourself, you are already an admin')}), 400
        
        existing = conn.execute('SELECT id FROM admin_profiles WHERE user_id=%s', (user['id'],)).fetchone()
        if existing:
            return jsonify({'success': False, 'error': f'{user["display_name"] or user["phone"]} is already an administrator'}), 400
        
        import json as _json
        permissions_str = _json.dumps(permissions if permissions else [])
        
        conn.execute('UPDATE users SET is_admin=1 WHERE id=%s', (user['id'],))
        conn.execute('''
            INSERT INTO admin_profiles (user_id, role, permissions, real_name, notes, created_by) 
            VALUES (%s,%s,%s,%s,%s,%s)
        ''', (user['id'], role, permissions_str, real_name, notes, admin['user_id']))
        conn.commit()
        _log(admin['user_id'], 'create_admin', 'admin', str(user['id']), f'{user["display_name"] or user["phone"]} ({role})')
    
    return jsonify({'success': True, 'message': f'Promoted {user["display_name"] or user["phone"]} to administrator'})


@admin_bp.route('/admins/<int:uid>', methods=['PUT'])
def admin_update(uid):
    """更新管理员信息（super_admin only）"""
    admin, err = _require_super_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    
    # 更新 admin_profiles 表
    pf_fields = []
    pf_params = []
    for key in ('role', 'real_name', 'internal_phone', 'internal_email', 'notes'):
        if key in data:
            pf_fields.append(f'{key}=%s')
            pf_params.append(data.get(key, chr(39)+chr(39)).strip())
    
    # 处理 permissions（JSON数组）
    if 'permissions' in data:
        import json as _json
        pf_fields.append('permissions=%s')
        pf_params.append(_json.dumps(data['permissions']))
    
    # 处理密码（单独字段，不走 profile）—— 仅短信验证码验证
    password = data.get('password', '').strip()
    code = data.get('code', '').strip()
    
    with get_db() as conn:
        # 验证目标确实是管理员
        target = conn.execute('SELECT id, phone, COALESCE(display_name, username) as nickname FROM users WHERE id=%s AND is_admin=1', (uid,)).fetchone()
        if not target:
            return jsonify({'success': False, 'error': _('Administrator does not exist')}), 404
        
        if uid == admin['user_id'] and 'role' in data and data['role'] != 'super_admin':
            return jsonify({'success': False, 'error': _('Cannot downgrade yourself to non-super admin')}), 400
        
        if pf_fields:
            pf_fields.append("updated_at=NOW()")
            pf_params.append(uid)
            conn.execute(f'UPDATE admin_profiles SET {", ".join(pf_fields)} WHERE user_id=%s', pf_params)
        
        # 修改密码：仅短信验证码验证
        if password:
            if not code:
                return jsonify({'success': False, 'error': '请输入短信验证码'}), 400
            # 验证 SMS 验证码
            row = conn.execute(
                'SELECT * FROM sms_codes WHERE phone=%s AND code=%s AND purpose=%s AND used=0 AND expires_at>NOW() ORDER BY id DESC LIMIT 1',
                (target['phone'], code, 'modify_password')
            ).fetchone()
            if not row:
                return jsonify({'success': False, 'error': _('Invalid or expired verification code')}), 400
            conn.execute('UPDATE sms_codes SET used=1 WHERE id=%s', (row['id'],))
            
            import hashlib, secrets
            salt = secrets.token_hex(16)
            pw_hash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()
            stored = f'pbkdf2:sha256:600000:{salt}:{pw_hash}'
            conn.execute('UPDATE users SET password_hash=%s WHERE id=%s', (stored, uid))
        
        conn.commit()
        _log(admin['user_id'], 'update_admin', 'admin', str(uid), f'role={data.get("role","")}')
    
    return jsonify({'success': True, 'message': _('Administrator information has been updated')})


@admin_bp.route('/admins/<int:uid>', methods=['DELETE'])
def admin_delete(uid):
    """将管理员降级为普通用户（super_admin only）"""
    admin, err = _require_super_admin()
    if err:
        return err
    if uid == admin['user_id']:
        return jsonify({'success': False, 'error': '不能移除自己，请先转移超管权限'}), 400
    with get_db() as conn:
        target = conn.execute('SELECT id, display_name, phone FROM users WHERE id=%s AND is_admin=1', (uid,)).fetchone()
        if not target:
            return jsonify({'success': False, 'error': _('Administrator does not exist')}), 404
        conn.execute('DELETE FROM admin_profiles WHERE user_id=%s', (uid,))
        conn.execute('UPDATE users SET is_admin=0 WHERE id=%s', (uid,))
        conn.commit()
        _log(admin['user_id'], 'remove_admin', 'admin', str(uid), f'{target["display_name"] or target["phone"]}')
    return jsonify({'success': True, 'message': f'Downgraded {target["display_name"] or target["phone"]} to a regular user'})


@admin_bp.route('/admins/me/avatar', methods=['POST'])
def admin_me_avatar():
    """上传管理员头像 — 800x800 max, 1MB max"""
    admin, err = _require_admin()
    if err:
        return err
    if 'avatar' not in request.files:
        return jsonify({'success': False, 'error': _('No file selected')}), 400
    file = request.files['avatar']
    if not file.filename:
        return jsonify({'success': False, 'error': _('File name is empty')}), 400
    
    import os
    # 验证文件大小
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > 1024 * 1024:
        return jsonify({'success': False, 'error': _('Picture size cannot exceed 1MB')}), 400
    
    # 验证图片尺寸
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(file.read()))
        w, h = img.size
        if w > 800 or h > 800:
            return jsonify({'success': False, 'error': f'Picture size cannot exceed 800×800 (current {w}×{h})'}), 400
        file.seek(0)
    except Exception:
        return jsonify({'success': False, 'error': '无法解析图片文件，请上传 JPG/PNG 格式'}), 400
    
    # 保存文件
    import uuid
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
        ext = '.jpg'
    filename = f'avatar_{admin["user_id"]}_{uuid.uuid4().hex[:8]}{ext}'
    save_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'admin', 'static', 'avatars')
    os.makedirs(save_dir, exist_ok=True)
    file.save(os.path.join(save_dir, filename))
    
    avatar_url = f'/static/avatars/{filename}'
    from models import get_db
    with get_db() as conn:
        conn.execute('UPDATE users SET avatar_url=%s WHERE id=%s', (avatar_url, admin['user_id']))
        conn.commit()
    _log(admin['user_id'], 'update_avatar', 'admin_profile', str(admin['user_id']))
    return jsonify({'success': True, 'data': {'avatar_url': avatar_url}})


# =============================================
# 用户头像管理 (普通用户 + Agent)
# =============================================

@admin_bp.route('/users/<int:uid>/avatar', methods=['POST'])
def user_avatar_upload(uid):
    """上传用户头像 — 512x512 max, 512KB max"""
    admin, err = _require_admin()
    if err:
        return err
    if 'avatar' not in request.files:
        return jsonify({'success': False, 'error': _('No file selected')}), 400
    file = request.files['avatar']
    if not file.filename:
        return jsonify({'success': False, 'error': _('File name is empty')}), 400

    # 验证用户存在
    with get_db() as conn:
        user = conn.execute('SELECT id, COALESCE(display_name, username) as nickname FROM users WHERE id=%s', (uid,)).fetchone()
    if not user:
        return jsonify({'success': False, 'error': _('User does not exist')}), 404

    import os
    # 文件大小验证 (512KB)
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > 512 * 1024:
        return jsonify({'success': False, 'error': _('Picture size cannot exceed 512KB')}), 400

    # 图片尺寸验证 (512x512)
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(file.read()))
        w, h = img.size
        if w > 512 or h > 512:
            return jsonify({'success': False, 'error': f'Picture size cannot exceed 512×512 (current {w}×{h})'}), 400
        file.seek(0)
    except Exception:
        return jsonify({'success': False, 'error': '无法解析图片文件，请上传 JPG/PNG/SVG 格式'}), 400

    # 保存文件
    import uuid
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg'):
        ext = '.jpg'
    filename = f'user_{uid}_avatar_{uuid.uuid4().hex[:8]}{ext}'
    save_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'admin', 'static', 'avatars')
    os.makedirs(save_dir, exist_ok=True)
    file.save(os.path.join(save_dir, filename))

    avatar_url = f'/static/avatars/{filename}'
    with get_db() as conn:
        conn.execute('UPDATE users SET avatar_url=%s WHERE id=%s', (avatar_url, uid))
        conn.commit()
    _log(admin['user_id'], 'set_user_avatar', 'user', str(uid))
    return jsonify({'success': True, 'data': {'avatar_url': avatar_url}})


@admin_bp.route('/users/<int:uid>/avatar/default', methods=['PUT'])
def user_avatar_default(uid):
    """为用户设置默认头像 (from library)"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    default_name = data.get('default', chr(39)+chr(39))
    if not default_name:
        return jsonify({'success': False, 'error': '请指定默认头像文件名'}), 400
    avatar_url = f'/static/avatars/default/users/{default_name}'
    with get_db() as conn:
        conn.execute('UPDATE users SET avatar_url=%s WHERE id=%s', (avatar_url, uid))
        conn.commit()
    _log(admin['user_id'], 'set_user_default_avatar', 'user', str(uid), default_name)
    return jsonify({'success': True, 'data': {'avatar_url': avatar_url}})


@admin_bp.route('/users/<int:uid>/agent-avatar', methods=['POST'])
def user_agent_avatar_upload(uid):
    """上传Agent头像 — 512x512 max, 512KB max"""
    admin, err = _require_admin()
    if err:
        return err
    if 'avatar' not in request.files:
        return jsonify({'success': False, 'error': _('No file selected')}), 400
    file = request.files['avatar']
    if not file.filename:
        return jsonify({'success': False, 'error': _('File name is empty')}), 400

    with get_db() as conn:
        user = conn.execute('SELECT id, COALESCE(display_name, username) as nickname FROM users WHERE id=%s', (uid,)).fetchone()
    if not user:
        return jsonify({'success': False, 'error': _('User does not exist')}), 404

    import os
    # 文件大小验证 (512KB)
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > 512 * 1024:
        return jsonify({'success': False, 'error': _('Picture size cannot exceed 512KB')}), 400

    # 图片尺寸验证 (512x512)
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(file.read()))
        w, h = img.size
        if w > 512 or h > 512:
            return jsonify({'success': False, 'error': f'Picture size cannot exceed 512×512 (current {w}×{h})'}), 400
        file.seek(0)
    except Exception:
        return jsonify({'success': False, 'error': '无法解析图片文件，请上传 JPG/PNG/SVG 格式'}), 400

    # 保存文件
    import uuid
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg'):
        ext = '.jpg'
    filename = f'agent_{uid}_avatar_{uuid.uuid4().hex[:8]}{ext}'
    save_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'admin', 'static', 'avatars')
    os.makedirs(save_dir, exist_ok=True)
    file.save(os.path.join(save_dir, filename))

    agent_avatar_url = f'/static/avatars/{filename}'
    with get_db() as conn:
        conn.execute('UPDATE users SET agent_avatar_url=%s WHERE id=%s', (agent_avatar_url, uid))
        conn.commit()
    _log(admin['user_id'], 'set_agent_avatar', 'user', str(uid))
    return jsonify({'success': True, 'data': {'agent_avatar_url': agent_avatar_url}})


@admin_bp.route('/users/<int:uid>/agent-avatar/default', methods=['PUT'])
def user_agent_avatar_default(uid):
    """为Agent设置默认头像 (from library)"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    default_name = data.get('default', chr(39)+chr(39))
    if not default_name:
        return jsonify({'success': False, 'error': '请指定默认头像文件名'}), 400
    agent_avatar_url = f'/static/avatars/default/agents/{default_name}'
    with get_db() as conn:
        conn.execute('UPDATE users SET agent_avatar_url=%s WHERE id=%s', (agent_avatar_url, uid))
        conn.commit()
    _log(admin['user_id'], 'set_agent_default_avatar', 'user', str(uid), default_name)
    return jsonify({'success': True, 'data': {'agent_avatar_url': agent_avatar_url}})


@admin_bp.route('/users/<int:uid>/avatar/clear', methods=['POST'])
def user_avatar_clear(uid):
    """清除用户头像"""
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        conn.execute('UPDATE users SET avatar_url=\'\' WHERE id=%s', (uid,))
        conn.commit()
    _log(admin['user_id'], 'clear_user_avatar', 'user', str(uid))
    return jsonify({'success': True})


@admin_bp.route('/users/<int:uid>/agent-avatar/clear', methods=['POST'])
def user_agent_avatar_clear(uid):
    """清除Agent头像"""
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        conn.execute('UPDATE users SET agent_avatar_url=\'\' WHERE id=%s', (uid,))
        conn.commit()
    _log(admin['user_id'], 'clear_agent_avatar', 'user', str(uid))
    return jsonify({'success': True})


@admin_bp.route('/avatars/defaults', methods=['GET'])
def default_avatars_list():
    """列出所有可用的默认头像"""
    admin, err = _require_admin()
    if err:
        return err
    import os as _os
    base = _os.path.join(_os.path.dirname(__file__), '..', '..', 'admin', 'static', 'avatars', 'default')
    result = {'users': [], 'agents': []}
    users_dir = _os.path.join(base, 'users')
    agents_dir = _os.path.join(base, 'agents')
    if _os.path.isdir(users_dir):
        for f in sorted(_os.listdir(users_dir)):
            if f.lower().endswith(('.svg', '.png', '.jpg', '.jpeg')):
                result['users'].append({
                    'filename': f,
                    'url': f'/static/avatars/default/users/{f}',
                })
    if _os.path.isdir(agents_dir):
        for f in sorted(_os.listdir(agents_dir)):
            if f.lower().endswith(('.svg', '.png', '.jpg', '.jpeg')):
                result['agents'].append({
                    'filename': f,
                    'url': f'/static/avatars/default/agents/{f}',
                })
    return jsonify({'success': True, 'data': result})


# 可用的权限列表
ALL_PERMISSIONS = [
    {'key': 'users', 'label': _('User Management'), 'desc': _('View/Manage Regular Users')},
    {'key': 'content', 'label': _('Content Management'), 'desc': _('CMS/Community Content/Comment Review')},
    {'key': 'finance', 'label': _('Financial Management'), 'desc': _('Plan/Subscriptions/Orders/Revenue"')},
    {'key': 'system', 'label': _('System Settings'), 'desc': _('Community Section/System Configuration/Operation Log')},
    {'key': 'matrix', 'label': _('Agent Matrix'), 'desc': _('Manage Agent Matrix/Automatic Scheduling')},
    {'key': 'admins', 'label': _('Administrator Management'), 'desc': _('Manage Other Administrators (Only super_admin)')},
]

@admin_bp.route('/admins/permissions-list', methods=['GET'])
def admin_permissions_list():
    """返回所有可用的权限定义（给前端勾选用）"""
# RBAC: Permission-based middleware
# =============================================

def _require_permission(perm):
    """Verify the admin has a specific permission.
       Usage: wrap around route logic after _require_admin().
    """
    def decorator(f):
        import functools
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            admin, err = _require_admin()
            if err:
                return err
            with get_db() as conn:
                prof = conn.execute(
                    'SELECT permissions, role FROM admin_profiles WHERE user_id=%s',
                    (admin['user_id'],)
                ).fetchone()
            if not prof:
                return jsonify({'success': False, 'error': _('Administrator configuration does not exist')}), 403
            if prof['role'] == 'super_admin':
                # super_admin has all permissions
                return f(*args, **kwargs)
            try:
                perms = __import__('json').loads(prof['permissions'] or '[]')
            except Exception:
                perms = []
            if perm not in perms:
                return jsonify({'success': False, 'error': f'No "{perm}" permission'}), 403
# User Agent Management (admin)
# =============================================

@admin_bp.route('/user-agents', methods=['GET'])
def admin_user_agents_list():
    """列出所有用户 Agent（含所属用户信息）"""
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 20, type=int)
    search = request.args.get('search', chr(39)+chr(39)).strip()
    status_filter = request.args.get('status', chr(39)+chr(39)).strip()
    offset = (page - 1) * limit
    
    where = []
    params = []
    if search:
        where.append('(ua.agent_name LIKE %s OR COALESCE(u.display_name, u.username) LIKE %s OR u.phone LIKE %s)')
        s = '%' + search + '%'
        params.extend([s, s, s])
    if status_filter:
        where.append('ua.status=%s')
        params.append(status_filter)
    wsql = 'WHERE ' + ' AND '.join(where) if where else ''
    
    with get_db() as conn:
        total = conn.execute(
            'SELECT COUNT(*) as c FROM user_agents ua LEFT JOIN users u ON ua.user_id=u.id ' + wsql,
            params
        ).fetchone()
        rows = conn.execute(
            "SELECT ua.id, ua.agent_name, ua.agent_type, ua.status, ua.last_active_at, "
            "       ua.created_at, ua.user_id, COALESCE(u.display_name, u.username) as user_name, u.phone as user_phone "
            "FROM user_agents ua LEFT JOIN users u ON ua.user_id=u.id " +
            wsql + " ORDER BY ua.created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        ).fetchall()
    
    return jsonify({'success': True, 'data': {
        'total': total['c'],
        'page': page,
        'limit': limit,
        'agents': [dict(r) for r in rows],
    }})


@admin_bp.route('/users/<int:uid>/user-agents', methods=['GET'])
def admin_user_agent_list(uid):
    """查看指定用户的所有 Agent"""
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        user = conn.execute('SELECT id, COALESCE(display_name, username) as nickname, phone FROM users WHERE id=%s', (uid,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': _('User does not exist')}), 404
        rows = conn.execute(
            "SELECT ua.id, ua.agent_name, ua.agent_type, ua.avatar_url, ua.status, "
            "       ua.default_scopes, ua.last_active_ip, ua.last_active_at, ua.created_at, ua.updated_at "
            "FROM user_agents ua WHERE ua.user_id=%s ORDER BY ua.created_at DESC",
            (uid,)
        ).fetchall()
        
        agents = []
        for r in rows:
            d = dict(r)
            try:
                d['default_scopes'] = __import__('json').loads(d['default_scopes'] or '[]')
            except Exception:
                d['default_scopes'] = []
            # Count active keys
            kc = conn.execute(
                "SELECT COUNT(*) as c FROM agent_api_keys WHERE agent_id=%s AND status='active'",
                (r['id'],)
            ).fetchone()
            d['active_keys'] = kc['c'] if kc else 0
            agents.append(d)
    
    return jsonify({'success': True, 'data': {
        'user': dict(user),
        'agents': agents,
    }})


@admin_bp.route('/user-agents/<int:aid>/status', methods=['PUT'])
def admin_user_agent_status(aid):
    """管理Agent状态（suspend/activate）"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    status = data.get('status', chr(39)+chr(39)).strip()
    if status not in ('active', 'inactive', 'suspended'):
        return jsonify({'success': False, 'error': _('Invalid status value')}), 400
    
    with get_db() as conn:
        row = conn.execute(
            'SELECT ua.id, ua.agent_name, u.id as uid FROM user_agents ua JOIN users u ON ua.user_id=u.id WHERE ua.id=%s',
            (aid,)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Agent does not exist')}), 404
        conn.execute('UPDATE user_agents SET status=%s, updated_at=NOW() WHERE id=%s', (status, aid))
        conn.commit()
        _log(admin['user_id'], 'set_agent_status', 'user_agent', str(aid),
             f'Agent "{row["agent_name"]}" → {status}')
    
    return jsonify({'success': True, 'message': f'Agent status has been updated to {status}'})


@admin_bp.route('/users/<int:uid>/user-agents', methods=['POST'])
def admin_user_agent_create(uid):
    """管理员为用户创建 Agent"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    agent_name = data.get('agent_name', chr(39)+chr(39)).strip()
    if not agent_name:
        return jsonify({'success': False, 'error': _('Agent name cannot be empty')}), 400
    
    with get_db() as conn:
        user = conn.execute('SELECT id, display_name FROM users WHERE id=%s', (uid,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': _('User does not exist')}), 404
        existing = conn.execute(
            'SELECT id FROM user_agents WHERE user_id=%s AND agent_name=%s',
            (uid, agent_name)
        ).fetchone()
        if existing:
            return jsonify({'success': False, 'error': _('A user with the same name Agent already exists')}), 400
        aid = conn.execute(
            'INSERT INTO user_agents (user_id, agent_name) VALUES (%s,%s) RETURNING id',
            (uid, agent_name)
        ).fetchone()['id']
        conn.commit()
        _log(admin['user_id'], 'create_user_agent', 'user_agent', str(aid),
             f'Create Agent "{agent_name}" for {user["display_name"] or uid}')
    
    return jsonify({'success': True, 'data': {'id': aid, 'agent_name': agent_name}})
# =============================================
# GET /admin/users/export — 脱敏导出用户列表
# =============================================
@admin_bp.route('/users/export', methods=['GET'])
def user_export():
    admin, err = _require_admin()
    if err:
        return err
    industry = request.args.get('industry', '').strip()
    occupation = request.args.get('occupation', '').strip()
    region = request.args.get('region', '').strip()

    where = []
    params = []
    if industry:
        where.append('p.industry LIKE %s')
        params.append('%' + industry + '%')
    if occupation:
        where.append('p.occupation LIKE %s')
        params.append('%' + occupation + '%')
    if region:
        where.append('(pa.province LIKE %s OR pa.city LIKE %s OR pa.district LIKE %s)')
        r = '%' + region + '%'
        params.extend([r, r, r])
    wsql = 'WHERE ' + ' AND '.join(where) if where else ''

    sql = (
        "SELECT u.id, u.phone, COALESCE(u.display_name, u.username) as nickname, "
        "COALESCE(p.industry,'') as industry, COALESCE(p.occupation,'') as occupation, "
        "COALESCE(pa.province_code,'') as province, COALESCE(pa.city_code,'') as city, "
        "COALESCE(pa.district_code,'') as district, "
        "'' as tier, u.created_at "
        "FROM users u "
        "LEFT JOIN user_profiles p ON u.id=p.user_id "
        "LEFT JOIN user_addresses pa ON u.id=pa.user_id AND pa.is_default=1 AND pa.status=1 "
        + wsql + ' ORDER BY u.id'
    )

    def _mask_phone(phone):
        s = str(phone or '')
        if len(s) >= 7:
            return s[:3] + '****' + s[-4:]
        return s

    def _mask_address(prov, city, dist):
        parts = [p for p in [prov, city, dist] if p]
        if parts:
            return ''.join(parts) + '***'
        return ''

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    lines = []
    lines.append(_('ID, Phone (masked), Nickname, Industry, Occupation, Region (masked), Plan, Registration Time'))
    for r in rows:
        phone_m = _mask_phone(r['phone'])
        addr_m = _mask_address(r['province'], r['city'], r['district'])
        nickname = (r['nickname'] or '').replace(',', ' ')
        industry_v = (r['industry'] or '').replace(',', ' ')
        occupation_v = (r['occupation'] or '').replace(',', ' ')
        tier = r['tier'] or 'free'
        created = r['created_at'] or ''
        lines.append(f"{r['id']},{phone_m},{nickname},{industry_v},{occupation_v},{addr_m},{tier},{created}")

    csv_content = '\n'.join(lines)
    from flask import Response
    return Response(
        csv_content,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=users_export.csv'}
    )
# ── 客户管理 (Customer Management) ──

@admin_bp.route('/customers', methods=['GET'])
def customer_list():
    """客户列表 — 统一查看个人/企业认证状态"""
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 20, type=int)
    search = request.args.get("search", "").strip()
    cust_type = request.args.get("type", "").strip()       # enterprise / individual / ''
    verify_status = request.args.get("verify", "").strip() # verified / unverified / ''
    offset = (page - 1) * limit

    where = []
    params = []

    if search:
        where.append("(u.phone LIKE %s OR COALESCE(u.display_name, u.username) LIKE %s OR u.enterprise_name LIKE %s)")
        s = '%' + search + '%'
        params.extend([s, s, s])

    if cust_type == 'enterprise':
        where.append("u.enterprise_verified = 1")
    elif cust_type == 'individual':
        where.append("u.enterprise_verified = 0")

    if verify_status == 'verified':
        where.append("(u.enterprise_verified = 1 OR u.is_real_name_verified = 1)")
    elif verify_status == 'unverified':
        where.append("u.enterprise_verified = 0 AND u.is_real_name_verified = 0")

    wsql = 'WHERE ' + ' AND '.join(where) if where else ''

    from_sql = ("FROM users u")
    sql = ("SELECT u.id, u.phone, COALESCE(u.display_name, u.username) as nickname, u.email, "
           "u.created_at, u.last_login, u.active, "
           "u.is_real_name_verified, u.real_name_verified_at, u.verified_by, "
           "u.enterprise_name, u.enterprise_tax_id, u.enterprise_verified, u.enterprise_verified_at, "
           "'' as plan_key, NULL as sub_expires "
           + from_sql + ' ' + wsql + ' ORDER BY u.created_at DESC LIMIT %s OFFSET %s')
    csql = 'SELECT COUNT(DISTINCT u.id) as c ' + from_sql + ' ' + wsql

    with get_db() as conn:
        total = conn.execute(csql, params).fetchone()['c']
        rows = conn.execute(sql, params + [limit, offset]).fetchall()

    customers = []
    for r in rows:
        c = dict(r)
        if c.get('enterprise_verified'):
            c['cert_status'] = 'enterprise'
            c['cert_badge'] = _('Enterprise Verified')
        elif c.get('is_real_name_verified'):
            c['cert_status'] = 'individual'
            c['cert_badge'] = _('Individual Verified')
        else:
            c['cert_status'] = 'none'
            c['cert_badge'] = _('Unverified')
        customers.append(c)

    return jsonify({"success": True, "data": {
        "total": total, "page": page, "limit": limit,
        "customers": customers,
    }})
