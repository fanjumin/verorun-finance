#!/usr/bin/env python3
"""User Routes — profile, API keys, usage stats"""
import sys, os, secrets, hashlib, hmac, base64, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from flask import Blueprint, request, jsonify, make_response
from i18n import _
from models import get_db, now_iso, TIERS
from services.jwt_service import validate_token
# Verification — delegates to VerificationPlugin if available
try:
    import flask as _flask
    _pm = _flask.current_app.extensions.get('plugin_manager')
    _ver = _pm.get_instance('verification') if (_pm and _pm.is_enabled('verification')) else None
    if _ver:
        initiate_verification = _ver.initiate_verification
        verify_callback = _ver.verify_callback
    else:
        raise RuntimeError('plugin not available')
except Exception:
    from services.verification_service import initiate_verification, verify_callback

user_bp = Blueprint('user', __name__, url_prefix='/user')


def _get_token_from_request():
    """从请求中提取 token — 优先 Authorization header，其次 cookie"""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return request.cookies.get('sso_token') or request.cookies.get('tm_token') or None

def _require_auth():
    """Extract and validate JWT from Authorization header OR cookie"""
    token = _get_token_from_request()
    payload = validate_token(token) if token else None
    if not payload:
        return None, (jsonify({'success': False, 'error': 'Not logged in or token expired'}), 401)
    return payload, None


def _verify_sms_code(phone, code, purpose, error_msg='Invalid or expired verification code'):
    """验证短信验证码（VR-AUTH-005：猜码失败递增计数，达到 5 次作废）

    Returns:
        (ok: bool, err: str)
    """
    now = now_iso()
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM sms_codes WHERE phone=%s AND purpose=%s AND used=0 AND expires_at>%s ORDER BY id DESC LIMIT 1',
            (phone, purpose, now)).fetchone()
        if not row:
            return False, error_msg
        row = dict(row)
        if row['code'] != code:
            new_attempts = row['attempts'] + 1
            if new_attempts >= 5:
                conn.execute('UPDATE sms_codes SET used=1 WHERE id=%s', (row['id'],))
            else:
                conn.execute('UPDATE sms_codes SET attempts=%s WHERE id=%s', (new_attempts, row['id']))
            conn.commit()
            return False, error_msg
        if row['attempts'] >= 5:
            return False, 'Too many attempts, please request a new code'
        conn.execute('UPDATE sms_codes SET used=1 WHERE id=%s', (row['id'],))
        conn.commit()
    return True, ''


def _get_user_app(payload):
    user_id = payload['user_id']
    app_name = payload.get('app_name', 'main')
    with get_db() as conn:
        user = conn.execute('SELECT * FROM users WHERE id=%s', (user_id,)).fetchone()
        authz = conn.execute(
            'SELECT * FROM app_authorizations WHERE user_id=%s AND app_name=%s',
            (user_id, app_name)).fetchone()
    return dict(user) if user else None, dict(authz) if authz else None


# =============================================
# GET /user/profile
# =============================================
def _get_dev_level(user_id) -> str:
    """查询用户开发者身份等级（P0-1：/user/profile 追加 dev_level）。

    未注册开发者返回 ''；已注册返回 verify_level（free/email/identity/org）。
    store_developers 表由 plugin_manager 建在 public schema，与 users 同库。
    """
    if not user_id:
        return ''
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT verify_level FROM store_developers "
                "WHERE user_id=%s AND status='active'",
                (user_id,)).fetchone()
        return (row['verify_level'] or '') if row else ''
    except Exception:
        return ''


@user_bp.route('/profile', methods=['GET'])
def profile():
    payload, err = _require_auth()
    if err:
        return err
    user, authz = _get_user_app(payload)
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404
    tier_info = TIERS.get(authz['tier'], {}) if authz else TIERS['free']
    return jsonify({'success': True, 'data': {
        'id': user['id'],
        'dev_level': _get_dev_level(user['id']),
        'phone': user['phone'],
        'display_name': user.get('display_name') or user.get('username') or '',
        'avatar': user['avatar_url'] or '',
        'wechat_nickname': user['wechat_nickname'],
        'douyin_nickname': user['douyin_nickname'],
        'created_at': user['created_at'],
        'last_login': user['last_login'],
        'app': payload.get('app_name', 'main'),
        'tier': authz['tier'] if authz else 'free',
        'tier_name': tier_info.get('name', 'Free'),
        'tier_desc': tier_info.get('desc', ''),
        'tier_expire_at': authz['tier_expire_at'] if authz else None,
        'calls_today': authz['calls_today'] if authz else 0,
        'calls_total': authz['calls_total'] if authz else 0,
        'daily_limit': tier_info.get('daily_limit', 20),
        'calls_remaining': max(0, tier_info.get('daily_limit', 20) - (authz['calls_today'] if authz else 0)),
        'is_admin': bool(user['is_admin']),
        'nickname': user.get('display_name') or user.get('username') or '',
        'email_verified': bool(user.get('email_verified')),
        'password_set': bool(user.get('password_hash')),
        'password_changed_at': user.get('password_changed_at') or '',
        'email': user.get('email') or '',
        'agent_id': user.get('agent_id') or '',
        'agent_nickname': user.get('agent_nickname') or '',
        'agent_avatar': user.get('agent_avatar_url') or '',
        'username': user.get('username') or '',
        'verified_by': user.get('verified_by') or '',
        'verified_at': user.get('verified_at') or '',
        'is_real_name_verified': bool(user.get('is_real_name_verified')),
        'real_name_verified_at': user.get('real_name_verified_at') or '',
    }})


# =============================================
# PUT /user/profile — update nickname (locked after real-name verification)
# =============================================
@user_bp.route('/profile', methods=['PUT'])
def update_profile():
    payload, err = _require_auth()
    if err:
        return err
    data = request.get_json() or {}
    nickname = data.get('nickname', '').strip()
    display_name = data.get('display_name', '').strip()
    # D-19: 长度校验（与前端 maxlength=30 对齐）
    if len(nickname) > 30:
        return jsonify({'success': False, 'error': _('Nickname cannot exceed 30 characters')}), 400
    if len(display_name) > 30:
        return jsonify({'success': False, 'error': _('Display name cannot exceed 30 characters')}), 400
    with get_db() as conn:
        user = conn.execute(
            'SELECT is_real_name_verified FROM users WHERE id=%s',
            (payload['user_id'],)
        ).fetchone()
        if user and user['is_real_name_verified']:
            return jsonify({'success': False, 'error': 'Verified account, display name cannot be modified'}), 403
        if nickname:
            conn.execute('UPDATE users SET display_name=%s WHERE id=%s', (nickname, payload['user_id']))
        elif display_name:
            conn.execute('UPDATE users SET display_name=%s WHERE id=%s', (display_name, payload['user_id']))
        conn.commit()
    # Trigger completion refresh
    try:
        from services.completion_service import refresh_and_check
        refresh_and_check(payload['user_id'])
    except Exception:
        pass
    return jsonify({'success': True, 'data': {'nickname': nickname, 'display_name': display_name}})


# =============================================
# PUT /user/username — change username (1x/month)
# =============================================
@user_bp.route('/username', methods=['PUT'])
def update_username():
    payload, err = _require_auth()
    if err:
        return err
    import re
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'Username cannot be empty'}), 400
    if len(username) < 3 or len(username) > 20:
        return jsonify({'success': False, 'error': 'Username must be 3-20 characters long'}), 400
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9_-]+$', username):
        return jsonify({'success': False, 'error': 'Username must start with a letter, only letters, digits, underscores and hyphens allowed'}), 400
    # Check against prohibited words
    from services.name_validator import check_username
    un = check_username(username)
    if not un['valid']:
        return jsonify({'success': False, 'error': un['error']}), 400
    user_id = payload['user_id']
    with get_db() as conn:
        # Check existing
        existing = conn.execute('SELECT id FROM users WHERE username=%s AND id!=%s', (username, user_id)).fetchone()
        if existing:
            return jsonify({'success': False, 'error': 'Username already in use'}), 400
        # Check 30-day limit
        row = conn.execute('SELECT username_changed_at FROM users WHERE id=%s', (user_id,)).fetchone()
        if row and row['username_changed_at']:
            from datetime import datetime, timedelta
            changed = datetime.fromisoformat(row['username_changed_at'])
            if datetime.now() - changed < timedelta(days=30):
                remaining = 30 - (datetime.now() - changed).days
                return jsonify({'success': False, 'error': f'Less than 30 days since last change, {remaining} days remaining'}), 400
        # Update
        from models import now_iso
        now = now_iso()
        conn.execute('UPDATE users SET username=%s, username_changed_at=%s WHERE id=%s',
                     (username, now, user_id))
        conn.commit()
    return jsonify({'success': True, 'data': {'username': username, 'next_change_after': '30 days later'}})


# =============================================
# POST /user/avatar — upload own avatar
# =============================================
@user_bp.route('/avatar', methods=['POST'])
def upload_avatar():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']

    if 'avatar' not in request.files:
        return jsonify({'success': False, 'error': 'Please select an image'}), 400
    f = request.files['avatar']
    if not f.filename:
        return jsonify({'success': False, 'error': 'Please select an image'}), 400

    # Validate file type
    allowed = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in allowed:
        return jsonify({'success': False, 'error': 'Only PNG/JPG/GIF/WebP formats are supported'}), 400

    # Read file, validate size (2MB max)
    data = f.read()
    if len(data) > 2 * 1024 * 1024:
        return jsonify({'success': False, 'error': 'Image size must not exceed 2MB'}), 400

    # Save to disk
    filename = f'avatar_{user_id}_{secrets.token_hex(8)}.{ext}'
    upload_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'platform', 'static', 'avatars')
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, filename)
    with open(filepath, 'wb') as fout:
        fout.write(data)

    # Update DB
    avatar_url = f'/static/avatars/{filename}'
    from models import get_db
    with get_db() as conn:
        conn.execute('UPDATE users SET avatar_url=%s WHERE id=%s', (avatar_url, user_id))
        conn.commit()

    # Trigger completion refresh
    try:
        from services.completion_service import refresh_and_check
        refresh_and_check(user_id)
    except Exception:
        pass

    return jsonify({'success': True, 'data': {'avatar_url': avatar_url}})


# =============================================
# GET /user/keys — list API keys (prefix only)
# =============================================
@user_bp.route('/keys', methods=['GET'])
def list_keys():
    payload, err = _require_auth()
    if err:
        return err
    app_name = payload.get('app_name', 'main')
    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, key_prefix, name, calls_today, calls_total, created_at, expire_at, last_used, active '
            'FROM api_keys WHERE user_id=%s AND app_name=%s ORDER BY created_at DESC',
            (payload['user_id'], app_name)).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


# =============================================
# POST /user/keys/generate — create new key
# =============================================
@user_bp.route('/keys/generate', methods=['POST'])
def generate_key():
    payload, err = _require_auth()
    if err:
        return err
    data = request.get_json() or {}
    name = data.get('name', '')
    app_name = payload.get('app_name', 'main')
    user_id = payload['user_id']
    # Determine max tier
    with get_db() as conn:
        authz = conn.execute(
            'SELECT * FROM app_authorizations WHERE user_id=%s AND app_name=%s',
            (user_id, app_name)).fetchone()
    max_tier = authz['tier'] if authz else 'free'
    # Generate key
    raw_key = 'tm-' + secrets.token_hex(16)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12] + '...' + raw_key[-4:]
    now = now_iso()
    with get_db() as conn:
        conn.execute(
            'INSERT INTO api_keys (user_id, app_name, key_hash, key_prefix, name, created_at) '
            'VALUES (%s,%s,%s,%s,%s,%s)',
            (user_id, app_name, key_hash, key_prefix, name, now))
        conn.commit()
    return jsonify({
        'success': True,
        'data': {
            'key': raw_key,          # Full key — shown only ONCE
            'key_prefix': key_prefix,
            'name': name,
            'tier': max_tier,
            'warning': 'Save this key now! It will not be shown again.',
        }
    })


# =============================================
# DELETE /user/keys/<id> — revoke key
# =============================================
@user_bp.route('/keys/<int:key_id>', methods=['DELETE'])
def revoke_key(key_id):
    payload, err = _require_auth()
    if err:
        return err
    with get_db() as conn:
        conn.execute('UPDATE api_keys SET active=0 WHERE id=%s AND user_id=%s',
                     (key_id, payload['user_id']))
        conn.commit()
    return jsonify({'success': True})


# =============================================
# GET /user/tiers — list available tiers
# =============================================
@user_bp.route('/tiers', methods=['GET'])
def list_tiers():
    tiers = []
    for key, info in TIERS.items():
        tiers.append({
            'id': key,
            'name': info['name'],
            'desc': info['desc'],
            'daily_limit': info['daily_limit'],
            'price_month': info['price_month'],
            'price_year': info['price_year'],
            'features': info['features'],
        })
    return jsonify({'success': True, 'data': tiers})


# =============================================
# PUT /user/keys/<key_id> — update key name
# =============================================
@user_bp.route('/keys/<int:key_id>', methods=['PUT'])
def update_key(key_id):
    payload, err = _require_auth()
    if err:
        return err
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    with get_db() as conn:
        cur = conn.execute(
            'UPDATE api_keys SET name=%s WHERE id=%s AND user_id=%s',
            (name, key_id, payload['user_id']))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({'success': False, 'error': 'API key not found'}), 404
    return jsonify({'success': True})


# =============================================
# POST /user/password/set — set or change password
# Body: {phone, code, password}
# Requires SMS verification (purpose=modify_password)
# =============================================
@user_bp.route('/password/set', methods=['POST'])
def set_password():
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()
    code = data.get('code', '').strip()
    password = data.get('password', '').strip()
    if not phone or not code or not password:
        return jsonify({'success': False, 'error': 'Incomplete parameters'}), 400
    # Validate password strength
    from services.password_validator import validate_password, get_password_rules_text
    v = validate_password(password)
    if not v['valid']:
        return jsonify({'success': False, 'error': '；'.join(v['errors'])}), 400
    # Verify SMS code (VR-AUTH-005: 猜码失败递增计数，达到 5 次作废)
    ok, verr = _verify_sms_code(phone, code, 'modify_password')
    if not ok:
        return jsonify({'success': False, 'error': verr}), 400
    with get_db() as conn:
        # Hash password
        salt = secrets.token_hex(16)
        pw_hash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()
        stored = f'pbkdf2:sha256:600000:{salt}:{pw_hash}'
        conn.execute('UPDATE users SET password_hash=%s WHERE phone=%s', (stored, phone))
        # IAM v2: update password_changed_at
        user_row = conn.execute('SELECT id FROM users WHERE phone=%s', (phone,)).fetchone()
        user_id = user_row['id'] if user_row else None
        if user_id:
            conn.execute("UPDATE users SET password_changed_at=%s WHERE id=%s",
                         (now_iso(), user_id))
        conn.commit()
    # VR-AUTH-004：改密后吊销该用户全部 token（含当前会话，强制重新登录）
    if user_id:
        try:
            from services.jwt_service import revoke_all_user_tokens
            revoke_all_user_tokens(user_id)
        except Exception:
            pass
    return jsonify({'success': True, 'message': 'Password changed, please login again'})


# =============================================
# POST /user/phone/change — change bound phone number
# Body: {old_phone, old_code, new_phone, new_code}
# =============================================
@user_bp.route('/phone/change', methods=['POST'])
def change_phone():
    payload, err = _require_auth()
    if err:
        return err
    data = request.get_json() or {}
    old_phone = data.get('old_phone', '').strip()
    old_code = data.get('old_code', '').strip()
    new_phone = data.get('new_phone', '').strip()
    new_code = data.get('new_code', '').strip()
    if not all([old_phone, old_code, new_phone, new_code]):
        return jsonify({'success': False, 'error': 'Incomplete parameters'}), 400
    # VR-AUTH-005：新旧验证码均猜码失败递增计数，达到 5 次作废
    ok, verr = _verify_sms_code(old_phone, old_code, 'change_phone', 'Invalid old phone verification code')
    if not ok:
        return jsonify({'success': False, 'error': verr}), 400
    ok, verr = _verify_sms_code(new_phone, new_code, 'change_phone', 'Invalid new phone verification code')
    if not ok:
        return jsonify({'success': False, 'error': verr}), 400
    with get_db() as conn:
        # Check if new phone already taken
        existing = conn.execute('SELECT id FROM users WHERE phone=%s AND id!=%s', (new_phone, payload['user_id'])).fetchone()
        if existing:
            return jsonify({'success': False, 'error': 'This phone is already bound'}), 400
        # Update phone
        conn.execute('UPDATE users SET phone=%s, phone_verified=1 WHERE id=%s', (new_phone, payload['user_id']))
        conn.commit()
    return jsonify({'success': True, 'message': 'Phone number updated'})


# =============================================
# POST /user/password/login — login with phone+password
# =============================================
@user_bp.route('/password/login', methods=['POST'])
def password_login():
    from services.session_service import issue_auth_session, set_sso_cookie
    data = request.get_json() or {}
    login_field = data.get('phone', '').strip() or data.get('username', '').strip()
    login_field = login_field.replace(' ', '')
    password = data.get('password', '').strip()
    if not login_field or not password:
        return jsonify({'success': False, 'error': 'Please enter account and password'}), 400
    ip = request.remote_addr or 'unknown'
    with get_db() as conn:
        # VR-AUTH-002：爆破防护增加账号维度（login_attempts.phone 已有索引）
        recent_ip = conn.execute(
            "SELECT COUNT(*) as c FROM login_attempts WHERE ip=%s AND success=0 AND created_at > NOW() - INTERVAL '15 minutes'",
            (ip,)).fetchone()
        recent_acct = conn.execute(
            "SELECT COUNT(*) as c FROM login_attempts WHERE phone=%s AND success=0 AND created_at > NOW() - INTERVAL '15 minutes'",
            (login_field,)).fetchone()
        need_captcha = recent_ip['c'] >= 10 or recent_acct['c'] >= 5
    captcha_id = data.get('captcha_id', '')
    if need_captcha and not captcha_id:
        return jsonify({'success': False, 'error': 'Please complete the CAPTCHA challenge'}), 400
    if captcha_id:
        try:
            import urllib.request, json as _json
            req = urllib.request.Request('http://127.0.0.1:8084/api/captcha/consume',
                data=_json.dumps({'token': captcha_id, 'drag_distance': 0, 'drag_trace': []}).encode(),
                headers={'Content-Type': 'application/json'})
            resp = urllib.request.urlopen(req, timeout=3)
            result = _json.loads(resp.read().decode())
            if not result.get('valid'):
                return jsonify({'success': False, 'error': 'CAPTCHA expired or incomplete, please retry'}), 400
        except Exception:
            # VR-AUTH-001：验证码服务异常 fail-closed（不再静默放行）
            return jsonify({'success': False, 'error': 'CAPTCHA service unavailable, please retry'}), 503
    # VR-AUTH-001：验证码通过后不再无条件 429（验证码校验本身即爆破防护）
    with get_db() as conn:
        # VR-AUTH-007：冻结账号（active=0）禁止登录
        user = conn.execute('SELECT * FROM users WHERE active=1 AND (username=%s OR email=%s OR phone=%s)', (login_field, login_field, login_field)).fetchone()
        if not user:
            # VR-AUTH-003：统一错误消息，消除账号存在性枚举
            return jsonify({'success': False, 'error': 'Invalid account or password'}), 400
        stored = user['password_hash']
        if not stored:
            return jsonify({'success': False, 'error': 'Invalid account or password'}), 400
        # Try pbkdf2:sha256:salt:hash format first, fallback to werkzeug
        import hashlib, hmac
        pw_ok = False
        parts = stored.split(':')
        if len(parts) == 5 and parts[0] == 'pbkdf2' and parts[1] == 'sha256':
            iterations = int(parts[2])
            salt = parts[3]
            pw_hash = parts[4]
            check = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations).hex()
            pw_ok = hmac.compare_digest(pw_hash, check)
        else:
            # Fallback: werkzeug-style hash (used by older admin accounts)
            try:
                from werkzeug.security import check_password_hash
                pw_ok = check_password_hash(stored, password)
            except Exception:
                pass
        if not pw_ok:
            conn.execute('INSERT INTO login_attempts (phone, ip, success) VALUES (%s,%s,0)',
                         (login_field, request.remote_addr or 'unknown'))
            conn.commit()
            # VR-AUTH-003：统一错误消息
            return jsonify({'success': False, 'error': 'Invalid account or password'}), 400
        now = now_iso()
        conn.execute('UPDATE users SET last_login=%s WHERE id=%s', (now, user['id']))
        conn.execute('INSERT INTO login_attempts (phone, ip, success) VALUES (%s,%s,1)',
                     (login_field, request.remote_addr or 'unknown'))
        conn.commit()
        # Inject role for admin users
        role = 'user'
        if user['is_admin']:
            try:
                prof = conn.execute('SELECT role FROM admin_profiles WHERE user_id=%s', (user['id'],)).fetchone()
                if prof:
                    role = prof['role']
            except Exception:
                pass
    # 统一登录签发通道
    result = issue_auth_session(
        user['id'], user['phone'], app_name='main',
        is_admin=user['is_admin'], role=role,
        user_info={'nickname': user['display_name'] or user['username'] or '',
                   'password_changed_at': user.get('password_changed_at') or ''},
        device_name='Password Login', device_type='web')
    token = result['token']
    resp = make_response(jsonify({'success': True, 'data': {
        'token': token,
        'user': result['user'],
    }}))
    set_sso_cookie(resp, token)
    return resp


# =============================================
# GET /user/usage-history — daily usage stats (last 30 days)
# =============================================
@user_bp.route('/usage-history', methods=['GET'])
def usage_history():
    payload, err = _require_auth()
    if err:
        return err
    app_name = payload.get('app_name', 'main')
    user_id = payload['user_id']
    # Simple approach: return the current snapshot
    with get_db() as conn:
        authz = conn.execute(
            'SELECT * FROM app_authorizations WHERE user_id=%s AND app_name=%s',
            (user_id, app_name)).fetchone()
        keys = conn.execute(
            'SELECT id, name, key_prefix, calls_today, calls_total, last_used, created_at, active '
            'FROM api_keys WHERE user_id=%s AND app_name=%s ORDER BY created_at DESC',
            (user_id, app_name)).fetchall()
        total_keys = len(keys)
        active_keys = sum(1 for k in keys if k['active'])
        total_calls = authz['calls_total'] if authz else 0
        today_calls = authz['calls_today'] if authz else 0
    return jsonify({'success': True, 'data': {
        'total_keys': total_keys,
        'active_keys': active_keys,
        'total_calls': total_calls,
        'today_calls': today_calls,
        'keys': [dict(k) for k in keys],
    }})


# =============================================
# GET /user/config — get system configs (admin)
# PUT /user/config — update system config (admin)
# =============================================

# Known config keys with metadata
CONFIG_SCHEMA = {
    # ├─ 邮箱配置（SMTP/IMAP 已迁移至 Email 插件，由插件独立管理） ─
    # ├─ 短信配置 ─
    'aliyun_sms_sign_name':    {'label': _('SMS Signature'),      'category': 'sms', 'sensitive': False, 'placeholder': _('Xuzhou Yikai Network Technology')},
    'aliyun_sms_access_key':   {'label': 'AccessKey ID',   'category': 'sms', 'sensitive': True,  'placeholder': _('Enter AccessKey ID')},
    'aliyun_sms_secret':       {'label': 'AccessKey Secret','category': 'sms', 'sensitive': True,  'placeholder': _('Enter AccessKey Secret')},
    # ├─ 联系邮箱 ─
    'contact_email':           {'label': _('Contact Email'),       'category': 'other', 'sensitive': False, 'placeholder': 'myname@163.com'},
    # ├─ 社媒推送配置 ─
    'wechat_token':            {'label': _('Official Account Token'),       'category': 'social', 'sensitive': True,  'placeholder': _('Enter Token (Optional)')},
    # ├─ 微博推送 ─
    'weibo_app_key':           {'label': _('Weibo App Key'),       'category': 'social', 'sensitive': False, 'placeholder': 'App Key'},
    'weibo_app_secret':        {'label': _('Weibo App Secret'),    'category': 'social', 'sensitive': True,  'placeholder': 'App Secret'},
    'weibo_access_token':      {'label': _('Weibo Access Token'),  'category': 'social', 'sensitive': True,  'placeholder': _('User Authorization Token')},
    # ├─ 今日头条推送 ─
    'toutiao_app_id':          {'label': _('Toutiao Account App ID"'),      'category': 'social', 'sensitive': False, 'placeholder': _('Toutiao Open Platform App ID"')},
    'toutiao_app_secret':      {'label': _('Toutiao Account App Secret"'),  'category': 'social', 'sensitive': True,  'placeholder': 'App Secret'},
    'toutiao_access_token':    {'label': _('Toutiao Account Access Token"'),'category': 'social', 'sensitive': True,  'placeholder': _('API Call Token')},
    # ├─ 小程序 AI 配置 ─
    'mp_ai_provider':          {'label': _('AI Provider'),          'category': 'miniapp_ai', 'sensitive': False, 'placeholder': 'deepseek / dashscope / openai / openrouter'},
    'mp_ai_model':             {'label': _('Model Name'),           'category': 'miniapp_ai', 'sensitive': False, 'placeholder': _('Leave empty to use AI Hub default')},
    'mp_ai_base_url':          {'label': 'API 地址',           'category': 'miniapp_ai', 'sensitive': False, 'placeholder': 'https://api.deepseek.com'},
    'mp_ai_api_key':           {'label': 'API Key',            'category': 'miniapp_ai', 'sensitive': True,  'placeholder': _('Enter API Key')},
    # ├─ 支付配置（已迁移至 PaymentPlugin 插件管理） ─
    # ├─ 实名认证（已迁移至 VerificationPlugin 插件管理） ─
    # ├─ 后台访问控制 ─
    'admin_allowed_domains':   {'label': _('Allowed Management Backend Domains'), 'category': 'admin_access', 'sensitive': False,
                                'placeholder': 'agent.your-domain.com, admin.your-domain.com'},
}

CONFIG_CATEGORIES = [
    {'id': 'social', 'title': _('Social Media Push')},
    {'id': 'miniapp_ai', 'title': _('Mini Program AI Configuration')},
    {'id': 'admin_access', 'title': _('Backend Access Control')},
]


def _mask_value(key, value):
    """Mask sensitive config values for display."""
    meta = CONFIG_SCHEMA.get(key, {})
    if not meta.get('sensitive') or not value:
        return value
    if len(value) <= 8:
        return value[:2] + '****'
    return value[:6] + '****' + value[-4:]


@user_bp.route('/config', methods=['GET'])
def get_configs():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    with get_db() as conn:
        user = conn.execute('SELECT is_admin FROM users WHERE id=%s', (user_id,)).fetchone()
    if not user or not user['is_admin']:
        return jsonify({'success': False, 'error': chr(20165)+chr(31649)+chr(29702)+chr(21592)+chr(21487)+chr(20316)+chr(20316)}), 403
    with get_db() as conn:
        rows = conn.execute('SELECT key, value, description FROM system_config ORDER BY key').fetchall()
    configs = [dict(r) for r in rows]
    # Annotate with schema & mask sensitive values
    result = []
    for c in configs:
        key = c['key']
        meta = CONFIG_SCHEMA.get(key, {})
        c['sensitive'] = meta.get('sensitive', False)
        c['category'] = meta.get('category', 'other')
        c['label'] = meta.get('label', key)
        c['placeholder'] = meta.get('placeholder', '')
        c['masked_value'] = _mask_value(key, c['value']) if meta.get('sensitive') else c['value']
        result.append(c)
    return jsonify({'success': True, 'data': result, 'categories': CONFIG_CATEGORIES, 'schema': CONFIG_SCHEMA})


@user_bp.route('/config', methods=['PUT'])
def update_config():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    with get_db() as conn:
        user = conn.execute('SELECT is_admin FROM users WHERE id=%s', (user_id,)).fetchone()
    if not user or not user['is_admin']:
        return jsonify({'success': False, 'error': chr(20165)+chr(31649)+chr(29702)+chr(21592)+chr(21487)+chr(20316)+chr(20316)}), 403
    data = request.get_json(force=True)
    key = data.get('key', '').strip()
    value = data.get('value', '').strip()
    if not key:
        return jsonify({'success': False, 'error': 'key ' + chr(19981)+chr(33021)+chr(20026)+chr(31354)}), 400
    sql = "INSERT INTO system_config (key, value, updated_at, updated_by) VALUES (%s, %s, NOW(), %s) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by"
    with get_db() as conn:
        conn.execute(sql, (key, value, user_id))
        conn.commit()
    return jsonify({'success': True, 'message': chr(37197)+chr(32622)+chr(24050)+chr(26356)+chr(26032)})


@user_bp.route('/config/upload', methods=['POST'])
def upload_config_csv():
    """Upload AccessKey.csv to seed SMS/email config from Aliyun CSV."""
    import csv, io
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    with get_db() as conn:
        user = conn.execute('SELECT is_admin FROM users WHERE id=%s', (user_id,)).fetchone()
    if not user or not user['is_admin']:
        return jsonify({'success': False, 'error': 'Admin only'}), 403
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'Please upload a file'}), 400
    f = request.files['file']
    if not f.filename or not f.filename.lower().endswith('.csv'):
        return jsonify({'success': False, 'error': 'Please upload a .csv file'}), 400
    try:
        content = f.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        row = next(reader)
        access_key = row.get('AccessKey ID', '').strip()
        access_secret = row.get('AccessKey Secret', '').strip()
        if not access_key or not access_secret:
            return jsonify({'success': False, 'error': 'CSV missing AccessKey ID / AccessKey Secret columns'}), 400
        with get_db() as conn:
            conn.execute(
                "INSERT INTO system_config (key, value, description, updated_at, updated_by) VALUES (%s, %s, %s, NOW(), %s) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                ('aliyun_sms_access_key', access_key, _('Aliyun SMS AccessKey ID'), user_id)
            )
            conn.execute(
                "INSERT INTO system_config (key, value, description, updated_at, updated_by) VALUES (%s, %s, %s, NOW(), %s) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                ('aliyun_sms_secret', access_secret, _('Aliyun SMS AccessKey Secret'), user_id)
            )
            conn.commit()
        return jsonify({'success': True, 'message': 'AccessKey configuration imported', 'access_key_prefix': access_key[:8]+'...'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'Import failed: {e}'}), 500


@user_bp.route('/config/seed', methods=['POST'])
def seed_config_defaults():
    """Initialize default email/SMS config keys in system_config if missing."""
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    with get_db() as conn:
        user = conn.execute('SELECT is_admin FROM users WHERE id=%s', (user_id,)).fetchone()
    if not user or not user['is_admin']:
        return jsonify({'success': False, 'error': 'Admin only'}), 403
    created = []
    with get_db() as conn:
        for key, meta in CONFIG_SCHEMA.items():
            existing = conn.execute('SELECT value FROM system_config WHERE key=%s', (key,)).fetchone()
            if not existing:
                default_desc = meta.get('label', key)
                conn.execute(
                    "INSERT INTO system_config (key, value, description, updated_at, updated_by) VALUES (%s, %s, %s, NOW(), %s)",
                    (key, '', default_desc, user_id)
                )
                created.append(key)
        conn.commit()
    return jsonify({'success': True, 'message': f'Initialized {len(created)} configuration items', 'keys': created})


# =============================================
# GET /user/notifications — user notifications
# =============================================
@user_bp.route('/notifications', methods=['GET'])
def get_notifications():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 20, type=int)
    page_size = max(1, min(100, page_size))
    offset = (page - 1) * page_size
    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, type, title, content, link_url, is_read, read_at, created_at FROM user_notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s',
            (user_id, page_size, offset)
        ).fetchall()
        total = conn.execute('SELECT COUNT(*) as c FROM user_notifications WHERE user_id=%s', (user_id,)).fetchone()
        unread = conn.execute('SELECT COUNT(*) as c FROM user_notifications WHERE user_id=%s AND is_read=0', (user_id,)).fetchone()
    data = [dict(r) for r in rows]
    return jsonify({'success': True, 'data': data, 'total': total['c'], 'unread': unread['c'], 'page': page, 'pageSize': page_size})


@user_bp.route('/notifications/unread-count', methods=['GET'])
def notifications_unread_count():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    from services.notification_service import get_unread_count
    count = get_unread_count(user_id)
    return jsonify({'success': True, 'unread': count})


@user_bp.route('/notifications/read', methods=['POST'])
def mark_notifications_read():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    data = request.get_json(force=True) or {}
    nid = data.get('id')
    from services.notification_service import mark_read
    mark_read(user_id, nid)
    return jsonify({'success': True})


@user_bp.route('/notifications/read-all', methods=['POST'])
def mark_notifications_read_all():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    from services.notification_service import mark_read
    mark_read(user_id)
    return jsonify({'success': True})


@user_bp.route('/notifications/<int:nid>', methods=['DELETE'])
def delete_notification(nid):
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    with get_db() as conn:
        conn.execute('DELETE FROM user_notifications WHERE user_id=%s AND id=%s', (user_id, nid))
        conn.commit()
    return jsonify({'success': True})


# =============================================
# GET/POST /user/tickets — user ticket system
# =============================================
@user_bp.route('/tickets', methods=['GET'])
def get_tickets():
    payload, err = _require_auth()
    if err: return err
    user_id = payload['user_id']
    filter_type = (request.args.get("type") or "").strip()
    with get_db() as conn:
        if filter_type and filter_type in ("presale","aftersale","complaint","suggestion"):
            rows = conn.execute(
                'SELECT id, type, category, title, content, contact, status, priority, admin_reply, replied_at, created_at, updated_at FROM user_tickets WHERE user_id=%s AND type=%s ORDER BY updated_at DESC',
                (user_id, filter_type)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT id, type, category, title, content, contact, status, priority, admin_reply, replied_at, created_at, updated_at FROM user_tickets WHERE user_id=%s ORDER BY updated_at DESC',
                (user_id,)
            ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})

@user_bp.route('/tickets', methods=['POST'])
def create_ticket():
    payload, err = _require_auth()
    if err: return err
    user_id = payload['user_id']
    data = request.get_json(force=True) or {}
    title = (data.get('title') or '').strip()
    content = (data.get('content') or '').strip()
    ttype = (data.get('type') or 'aftersale').strip()
    category = (data.get('category') or '').strip()
    contact = (data.get('contact') or '').strip()
    priority = 'high' if ttype == 'complaint' else (data.get('priority') or 'normal').strip()
    if not title: return jsonify({'success': False, 'error': 'Title cannot be empty'}), 400
    if not content: return jsonify({'success': False, 'error': 'Content cannot be empty'}), 400
    with get_db() as conn:
        row = conn.execute(
            'INSERT INTO user_tickets (user_id, type, category, title, content, contact, priority) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (user_id, ttype, category, title, content, contact, priority)
        ).fetchone()
        conn.commit()
    return jsonify({'success': True, 'id': row['id'], 'type': ttype, 'priority': priority})


# =============================================
# GET /user/activity — recent activity log
# =============================================
@user_bp.route('/activity', methods=['GET'])
def get_activity():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    limit = request.args.get('limit', 10, type=int)
    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, type, title, content, created_at FROM user_activity WHERE user_id=%s ORDER BY created_at DESC LIMIT %s',
            (user_id, limit)
        ).fetchall()
    # If no activity table yet, return empty
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


# =============================================
# GET /user/agent/stats — Agent overview stats
# =============================================
@user_bp.route('/agent/stats', methods=['GET'])
def agent_stats():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    with get_db() as conn:
        user = conn.execute('SELECT agent_id, agent_nickname, created_at FROM users WHERE id=%s', (user_id,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        key_count = conn.execute('SELECT COUNT(*) as c FROM api_keys WHERE user_id=%s', (user_id,)).fetchone()
        exp_count = conn.execute('SELECT COUNT(*) as c FROM agent_experiences WHERE user_id=%s', (user_id,)).fetchone()
        fav_count = conn.execute('SELECT COUNT(*) as c FROM favorites WHERE user_id=%s', (user_id,)).fetchone()
    return jsonify({'success': True, 'data': {
        'agent_id': user['agent_id'] or '',
        'agent_nickname': user['agent_nickname'] or '',
        'key_count': key_count['c'],
        'exp_count': exp_count['c'],
        'fav_count': fav_count['c']
    }})


# =============================================
# GET /user/posts — 用户社区内容（agent_experiences）
# =============================================
@user_bp.route('/posts', methods=['GET'])
def user_posts():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    page = request.args.get('page', 1, type=int)
    limit = min(request.args.get('limit', 20, type=int), 100)
    offset = (page - 1) * limit
    with get_db() as conn:
        total = conn.execute('SELECT COUNT(*) as c FROM agent_experiences WHERE user_id=%s', (user_id,)).fetchone()
        rows = conn.execute(
            'SELECT id, title, category, status, like_count, view_count, created_at '
            'FROM agent_experiences WHERE user_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s',
            (user_id, limit, offset)
        ).fetchall()
    return jsonify({'success': True, 'data': {
        'total': total['c'],
        'page': page,
        'limit': limit,
        'posts': [dict(r) for r in rows]
    }})


# =============================================
# GET /user/keys/<int:key_id>/stats — single key usage
# =============================================
@user_bp.route('/keys/<int:key_id>/stats', methods=['GET'])
def key_stats(key_id):
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    with get_db() as conn:
        key = conn.execute('SELECT id, name, key_prefix, calls_today, calls_total, created_at FROM api_keys WHERE id=%s AND user_id=%s', (key_id, user_id)).fetchone()
    if not key:
        return jsonify({'success': False, 'error': 'API key not found'}), 404
    return jsonify({'success': True, 'data': dict(key)})


# =============================================
# GET /user/profile/detail — 读用户扩展资料
# =============================================
@user_bp.route('/profile/detail', methods=['GET'])
def profile_detail():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    with get_db() as conn:
        prof = conn.execute('''
            SELECT up.*, ind.name AS industry_name, co.name AS career_name
            FROM user_profiles up
            LEFT JOIN industries ind ON up.industry_id = ind.id
            LEFT JOIN career_options co ON up.career_id = co.id
            WHERE up.user_id=%s
        ''', (user_id,)).fetchone()
        addr_count = conn.execute(
            'SELECT COUNT(*) as c FROM user_addresses WHERE user_id=%s AND status=1', (user_id,)
        ).fetchone()['c']
    if prof:
        p = dict(prof)
        try:
            p['interests'] = json.loads(p.get('interests', '[]'))
        except Exception:
            p['interests'] = []
        # Ensure new fields have defaults
        p.setdefault('industry_id', None)
        p.setdefault('career_id', None)
        p.setdefault('industry_name', '')
        p.setdefault('career_name', '')
    else:
        p = {
            'user_id': user_id, 'gender': '', 'birth_date': None,
            'age_group': '', 'occupation': '', 'industry': '',
            'industry_id': None, 'career_id': None,
            'industry_name': '', 'career_name': '',
            'interests': [], 'bio': '', 'created_at': '', 'updated_at': ''
        }
    return jsonify({'success': True, 'data': {
        'profile': p, 'address_count': addr_count
    }})


# =============================================
# PUT /user/profile/detail — 写用户扩展资料（部分更新）
# =============================================
@user_bp.route('/profile/detail', methods=['PUT'])
def update_profile_detail():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    data = request.get_json(force=True) or {}
    # 字段白名单（保留 occupation/industry 兼容旧前端）
    allowed = ['gender', 'birth_date', 'age_group', 'occupation', 'industry',
               'industry_id', 'career_id', 'interests', 'bio']
    updates = {}
    for k in allowed:
        if k in data:
            updates[k] = data[k]
    if 'bio' in updates and len(updates.get('bio', '') or '') > 500:
        return jsonify({'success': False, 'error': 'Bio cannot exceed 500 characters'}), 400
    if 'birth_date' in updates and updates['birth_date']:
        import re
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', str(updates['birth_date'])):
            return jsonify({'success': False, 'error': 'Date of birth format should be YYYY-MM-DD'}), 400
    if 'interests' in updates:
        updates['interests'] = json.dumps(updates['interests'] if isinstance(updates['interests'], list) else [], ensure_ascii=False)
    if not updates:
        return jsonify({'success': False, 'error': 'No fields to update'}), 400
    updates['updated_at'] = now_iso()
    with get_db() as conn:
        existing = conn.execute(
            'SELECT id FROM user_profiles WHERE user_id=%s', (user_id,)
        ).fetchone()
        if existing:
            sets = ', '.join(f'{k}=%s' for k in updates.keys())
            vals = list(updates.values()) + [user_id]
            conn.execute(
                f'UPDATE user_profiles SET {sets} WHERE user_id=%s', vals
            )
        else:
            updates['user_id'] = user_id
            cols = ', '.join(updates.keys())
            placeholders = ', '.join('%s' for _ in updates)
            conn.execute(
                f'INSERT INTO user_profiles ({cols}) VALUES ({placeholders})',
                list(updates.values())
            )
        conn.commit()
        prof = conn.execute('''
            SELECT up.*, ind.name AS industry_name, co.name AS career_name
            FROM user_profiles up
            LEFT JOIN industries ind ON up.industry_id = ind.id
            LEFT JOIN career_options co ON up.career_id = co.id
            WHERE up.user_id=%s
        ''', (user_id,)).fetchone()
    p = dict(prof)
    try:
        p['interests'] = json.loads(p.get('interests', '[]'))
    except Exception:
        p['interests'] = []
    # Trigger completion refresh + reward check
    try:
        from services.completion_service import refresh_and_check
        refresh_and_check(user_id)
    except Exception:
        pass
    return jsonify({'success': True, 'data': {'profile': p}})


# =============================================
# GET /user/profile/completion — 资料完成度
# =============================================
@user_bp.route('/profile/completion', methods=['GET'])
def get_profile_completion():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    from services.completion_service import calc_completion, save_completion
    result = calc_completion(user_id)
    # Also persist it
    save_completion(user_id)
    return jsonify({'success': True, 'data': result})


# =============================================
# GET /user/interests — 用户已选兴趣标签
# PUT /user/interests — 更新用户兴趣标签
# =============================================
@user_bp.route('/interests', methods=['GET'])
def get_user_interests():
    payload, err = _require_auth()
    if err: return err
    user_id = payload['user_id']
    with get_db() as conn:
        rows = conn.execute("""
            SELECT i.id, i.name, i.category, i.is_hot
            FROM user_interests ui
            JOIN interests i ON ui.interest_id = i.id
            WHERE ui.user_id=%s AND i.is_active=1
            ORDER BY i.category, i.sort_order
        """, (user_id,)).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@user_bp.route('/interests', methods=['PUT'])
def update_user_interests():
    payload, err = _require_auth()
    if err: return err
    user_id = payload['user_id']
    data = request.get_json(silent=True) or {}
    ids = data.get('interest_ids', [])
    custom = data.get('custom_tags', [])
    if not isinstance(ids, list):
        return jsonify({'success': False, 'error': 'interest_ids must be an array'}), 400
    ids = [int(x) for x in ids if str(x).isdigit()]
    custom = [str(s).strip()[:20] for s in custom if isinstance(s, str) and s.strip()]
    total = len(ids) + len(custom)
    if total < 3 or total > 5:
        return jsonify({'success': False, 'error': 'Please select 3-5 interest tags'}), 400
    with get_db() as conn:
        conn.execute('DELETE FROM user_interests WHERE user_id=%s', (user_id,))
        # Existing interests
        for iid in ids:
            conn.execute(
                'INSERT INTO user_interests (user_id, interest_id) VALUES (%s,%s) ON CONFLICT (user_id, interest_id) DO NOTHING',
                (user_id, iid)
            )
        # Custom tags: find or create interest record, then link
        for name in custom:
            row = conn.execute('SELECT id FROM interests WHERE name=%s', (name,)).fetchone()
            if row:
                iid = row['id']
            else:
                cursor = conn.execute(
                    'INSERT INTO interests (name, category, sort_order, is_hot, is_active) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                    (name, _('Custom'), 999, 0, 1)
                )
                iid = cursor.fetchone()['id']
            conn.execute(
                'INSERT INTO user_interests (user_id, interest_id) VALUES (%s,%s) ON CONFLICT (user_id, interest_id) DO NOTHING',
                (user_id, iid)
            )
        conn.commit()
    # Update completion
    try:
        from services.completion_service import refresh_and_check
        refresh_and_check(user_id)
    except Exception: pass
    return jsonify({'success': True, 'data': {'count': total}})


# =============================================
# GET /user/industries — 行业列表（数据字典）
# =============================================
@user_bp.route('/industries', methods=['GET'])
def list_industries():
    payload, err = _require_auth()
    if err:
        return err
    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, name, sort_order FROM industries ORDER BY sort_order, id'
        ).fetchall()
    return jsonify({'success': True, 'data': {
        'industries': [dict(r) for r in rows]
    }})


# =============================================
# GET /user/career-options — 职业/自由职业选项
# %sparent_id=xxx  返回子选项；不传返回常规职位
# =============================================
@user_bp.route('/career-options', methods=['GET'])
def list_career_options():
    payload, err = _require_auth()
    if err:
        return err
    parent_id = request.args.get('parent_id', type=int)
    with get_db() as conn:
        if parent_id is not None:
            rows = conn.execute(
                'SELECT id, category, name, parent_id FROM career_options WHERE parent_id=%s ORDER BY sort_order, id',
                (parent_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, category, name, parent_id FROM career_options WHERE category='job' ORDER BY sort_order, id"
            ).fetchall()
    return jsonify({'success': True, 'data': {
        'career_options': [dict(r) for r in rows]
    }})


# =============================================
# GET /user/regions — 行政区划级联查询
# %sparent_code=xxx  返回下级区域；不传返回 top-level
# =============================================
@user_bp.route('/regions', methods=['GET'])
def regions_cascade():
    parent_code = request.args.get('parent_code', '').strip()
    with get_db() as conn:
        if not parent_code:
            # Return level 0 (中国) + level 1 (省)
            rows = conn.execute(
                'SELECT code, name, level, parent_code, full_name FROM regions WHERE level IN (0, 1) ORDER BY code'
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT code, name, level, parent_code, full_name FROM regions WHERE parent_code=%s ORDER BY code',
                (parent_code,)
            ).fetchall()
    return jsonify({'success': True, 'data': {
        'regions': [dict(r) for r in rows]
    }})


# =============================================
# GET /user/addresses — 收货地址列表
# =============================================
@user_bp.route('/addresses', methods=['GET'])
def address_list():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    market = os.environ.get('DEPLOY_MARKET', 'cn')
    with get_db() as conn:
        if market == 'intl':
            rows = conn.execute(
                '''SELECT * FROM user_addresses_intl
                WHERE user_id=%s AND status=1
                ORDER BY is_default DESC, created_at DESC''',
                (user_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                '''SELECT ua.*,
                    p.name as province_name,
                    c.name as city_name,
                    d.name as district_name,
                    s.name as street_name
                FROM user_addresses ua
                LEFT JOIN regions p ON ua.province_code = p.code::text
                LEFT JOIN regions c ON ua.city_code = c.code::text
                LEFT JOIN regions d ON ua.district_code = d.code::text
                LEFT JOIN regions s ON ua.street_code = s.code::text
                WHERE ua.user_id=%s AND ua.status=1
                ORDER BY ua.is_default DESC, ua.created_at DESC''',
                (user_id,)
            ).fetchall()
    return jsonify({'success': True, 'data': {
        'addresses': [dict(r) for r in rows]
    }})


# =============================================
# POST /user/addresses — 创建收货地址
# =============================================
@user_bp.route('/addresses', methods=['POST'])
def address_create():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    data = request.get_json(force=True) or {}
    market = os.environ.get('DEPLOY_MARKET', 'cn')

    if market == 'intl':
        # International: free-text address, no region code validation
        street_addr = (data.get('street_address', '') or '').strip()
        recipient = (data.get('recipient_name', '') or '').strip()
        phone = (data.get('phone', '') or '').strip()
        if not street_addr:
            return jsonify({'success': False, 'error': 'Missing required field: street_address'}), 400
        is_default = 1 if data.get('is_default') else 0
        with get_db() as conn:
            if is_default:
                conn.execute('UPDATE user_addresses SET is_default=0 WHERE user_id=%s AND status=1', (user_id,))
            cols = ['user_id', 'recipient_name', 'phone', 'street_address', 'postal_code', 'is_default']
            vals = [user_id, recipient, phone, street_addr,
                    (data.get('postal_code', '') or '').strip(), is_default]
            conn.execute(
                f'INSERT INTO user_addresses ({", ".join(cols)}) VALUES ({", ".join("%s" for _ in vals)})',
                vals
            )
            conn.commit()
            addr_id = conn.execute('SELECT lastval()').fetchone()['lastval']
            addr = conn.execute(
                'SELECT * FROM user_addresses WHERE id=%s', (addr_id,)
            ).fetchone()
        return jsonify({'success': True, 'data': {'address': dict(addr)}})

    required = ['province_code', 'city_code', 'district_code', 'street_address']
    for f in required:
        if not (data.get(f, '') or '').strip():
            return jsonify({'success': False, 'error': f'Missing required field: {f}'}), 400
    is_default = 1 if data.get('is_default') else 0
    with get_db() as conn:
        # Validate region codes exist
        for code_field in ['province_code', 'city_code', 'district_code']:
            code = data[code_field].strip()
            exist = conn.execute('SELECT 1 FROM regions WHERE code=%s', (code,)).fetchone()
            if not exist:
                return jsonify({'success': False, 'error': f'Invalid {code_field}: {code}'}), 400
        street_code = (data.get('street_code', '') or '').strip()
        if street_code:
            exist = conn.execute('SELECT 1 FROM regions WHERE code=%s', (street_code,)).fetchone()
            if not exist:
                return jsonify({'success': False, 'error': f'Invalid street_code: {street_code}'}), 400
        if is_default:
            conn.execute(
                'UPDATE user_addresses SET is_default=0 WHERE user_id=%s AND status=1',
                (user_id,)
            )
        cols = ['user_id', 'recipient_name', 'phone', 'province_code', 'city_code', 'district_code',
                'street_code', 'street_address', 'postal_code', 'is_default']
        vals = [user_id,
                (data.get('recipient_name', '') or '').strip(),
                (data.get('phone', '') or '').strip(),
                data['province_code'].strip(),
                data['city_code'].strip(),
                data['district_code'].strip(),
                street_code,
                data['street_address'].strip(),
                (data.get('postal_code', '') or '').strip(),
                is_default]
        conn.execute(
            f'INSERT INTO user_addresses ({", ".join(cols)}) VALUES ({", ".join("%s" for _ in vals)})',
            vals
        )
        conn.commit()
        addr_id = conn.execute('SELECT lastval()').fetchone()['lastval']
        addr = conn.execute(
            '''SELECT ua.*,
                p.name as province_name, c.name as city_name,
                d.name as district_name, s.name as street_name
            FROM user_addresses ua
            LEFT JOIN regions p ON ua.province_code = p.code::text
            LEFT JOIN regions c ON ua.city_code = c.code::text
            LEFT JOIN regions d ON ua.district_code = d.code::text
            LEFT JOIN regions s ON ua.street_code = s.code::text
            WHERE ua.id=%s''', (addr_id,)
        ).fetchone()
    return jsonify({'success': True, 'data': {'address': dict(addr)}})


# =============================================
# PUT /user/addresses/<int:addr_id> — 更新收货地址
# =============================================
@user_bp.route('/addresses/<int:addr_id>', methods=['PUT'])
def address_update(addr_id):
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    data = request.get_json(force=True) or {}
    market = os.environ.get('DEPLOY_MARKET', 'cn')
    with get_db() as conn:
        exist = conn.execute(
            'SELECT * FROM user_addresses WHERE id=%s AND user_id=%s AND status=1',
            (addr_id, user_id)
        ).fetchone()
        if not exist:
            return jsonify({'success': False, 'error': 'Address not found or no permission'}), 403
        updates = {}
        if market == 'intl':
            # International: free-text address, no region code validation
            for f in ['recipient_name', 'phone', 'street_address', 'postal_code']:
                if f in data:
                    updates[f] = (data[f] or '').strip()
            if not updates:
                return jsonify({'success': False, 'error': 'No fields to update'}), 400
        else:
            # CN market: validate region codes if provided
            for f in ['province_code', 'city_code', 'district_code', 'street_code']:
                if f in data:
                    code = (data[f] or '').strip()
                    if code:
                        rc = conn.execute('SELECT 1 FROM regions WHERE code=%s', (code,)).fetchone()
                        if not rc:
                            return jsonify({'success': False, 'error': f'Invalid {f}: {code}'}), 400
                    updates[f] = code
        for f in ['recipient_name', 'phone', 'street_address', 'postal_code']:
            if f in data:
                updates[f] = (data[f] or '').strip()
        if 'is_default' in data:
            is_def = 1 if data['is_default'] else 0
            if is_def:
                conn.execute(
                    'UPDATE user_addresses SET is_default=0 WHERE user_id=%s AND status=1',
                    (user_id,)
                )
            updates['is_default'] = is_def
        if not updates:
            return jsonify({'success': False, 'error': 'No fields to update'}), 400
        updates['updated_at'] = now_iso()
        sets = ', '.join(f'{k}=%s' for k in updates.keys())
        vals = list(updates.values()) + [addr_id]
        conn.execute(f'UPDATE user_addresses SET {sets} WHERE id=%s', vals)
        conn.commit()
        addr = conn.execute(
            '''SELECT ua.*,
                p.name as province_name, c.name as city_name,
                d.name as district_name, s.name as street_name
            FROM user_addresses ua
            LEFT JOIN regions p ON ua.province_code = p.code::text
            LEFT JOIN regions c ON ua.city_code = c.code::text
            LEFT JOIN regions d ON ua.district_code = d.code::text
            LEFT JOIN regions s ON ua.street_code = s.code::text
            WHERE ua.id=%s''', (addr_id,)
        ).fetchone()
    return jsonify({'success': True, 'data': {'address': dict(addr)}})


# =============================================
# DELETE /user/addresses/<int:addr_id> — 软删除收货地址
# =============================================
@user_bp.route('/addresses/<int:addr_id>', methods=['DELETE'])
def address_delete(addr_id):
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    market = os.environ.get('DEPLOY_MARKET', 'cn')
    table = 'user_addresses_intl' if market == 'intl' else 'user_addresses'
    with get_db() as conn:
        exist = conn.execute(
            f'SELECT * FROM {table} WHERE id=%s AND user_id=%s AND status=1',
            (addr_id, user_id)
        ).fetchone()
        if not exist:
            return jsonify({'success': False, 'error': 'Address not found or no permission'}), 403
        was_default = exist['is_default']
        conn.execute(
            f'UPDATE {table} SET status=0, is_default=0, updated_at=%s WHERE id=%s',
            (now_iso(), addr_id)
        )
        if was_default:
            latest = conn.execute(
                f'SELECT id FROM {table} WHERE user_id=%s AND status=1 ORDER BY created_at DESC LIMIT 1',
                (user_id,)
            ).fetchone()
            if latest:
                conn.execute(
                    f'UPDATE {table} SET is_default=1 WHERE id=%s',
                    (latest['id'],)
                )
        conn.commit()
    return jsonify({'success': True})


# =============================================
# PUT /user/addresses/<int:addr_id>/default — 设为默认地址
# =============================================
@user_bp.route('/addresses/<int:addr_id>/default', methods=['PUT'])
def address_set_default(addr_id):
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    market = os.environ.get('DEPLOY_MARKET', 'cn')
    table = 'user_addresses_intl' if market == 'intl' else 'user_addresses'
    with get_db() as conn:
        exist = conn.execute(
            f'SELECT * FROM {table} WHERE id=%s AND user_id=%s AND status=1',
            (addr_id, user_id)
        ).fetchone()
        if not exist:
            return jsonify({'success': False, 'error': 'Address not found or no permission'}), 403
        conn.execute(
            f'UPDATE {table} SET is_default=0 WHERE user_id=%s AND status=1',
            (user_id,)
        )
        conn.execute(
            f'UPDATE {table} SET is_default=1, updated_at=%s WHERE id=%s',
            (now_iso(), addr_id)
        )
        conn.commit()
    return jsonify({'success': True})

# ================================================================
# 第三方账号绑定管理
# ================================================================

@user_bp.route('/oauth/unbind', methods=['POST'])
def oauth_unbind():
    """解绑第三方账号（微信/抖音）"""
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    data = request.get_json(force=True) or {}
    provider = (data.get('provider') or '').strip().lower()

    if provider not in ('wechat', 'douyin'):
        return jsonify({'success': False, 'error': 'Unsupported platform, only wechat/douyin'}), 400

    with get_db() as conn:
        if provider == 'wechat':
            conn.execute(
                'UPDATE users SET wechat_openid=NULL, wechat_unionid=NULL, wechat_nickname=NULL, updated_at=%s WHERE id=%s',
                (now_iso(), user_id)
            )
        else:
            conn.execute(
                'UPDATE users SET douyin_open_id=NULL, douyin_nickname=NULL, douyin_avatar=NULL, updated_at=%s WHERE id=%s',
                (now_iso(), user_id)
            )
        conn.commit()

    return jsonify({'success': True, 'message': f'{provider} account unlinked'})


# ================================================================
# 通知偏好设置
# ================================================================

DEFAULT_NOTIF_PREFS = {
    'system_site': True,
    'system_mail': True,
    'order_site': True,
    'order_mail': True,
    'activity_site': True,
    'activity_mail': False,
}


@user_bp.route('/notification-preferences', methods=['GET', 'PUT'])
def notification_preferences():
    """获取/更新通知偏好设置"""
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']

    if request.method == 'GET':
        with get_db() as conn:
            row = conn.execute(
                'SELECT prefs FROM notification_preferences WHERE user_id=%s', (user_id,)
            ).fetchone()
        if row:
            prefs = json.loads(row['prefs'])
        else:
            prefs = dict(DEFAULT_NOTIF_PREFS)
        return jsonify({'success': True, 'data': prefs})

    # PUT
    data = request.get_json(force=True) or {}
    prefs = {}
    for key in DEFAULT_NOTIF_PREFS:
        if key in data:
            prefs[key] = bool(data[key])
        else:
            prefs[key] = DEFAULT_NOTIF_PREFS[key]

    with get_db() as conn:
        conn.execute(
            '''INSERT INTO notification_preferences (user_id, prefs, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT(user_id) DO UPDATE SET prefs=excluded.prefs, updated_at=excluded.updated_at''',
            (user_id, json.dumps(prefs, ensure_ascii=False), now_iso())
        )
        conn.commit()

    return jsonify({'success': True, 'data': prefs, 'message': 'Notification preferences updated'})


# ================================================================
# 活动日志
# ================================================================

@user_bp.route('/activity', methods=['GET'])
def user_activity():
    """获取用户活动日志"""
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 20, type=int)
    offset = (page - 1) * page_size

    with get_db() as conn:
        total = conn.execute(
            'SELECT COUNT(*) as cnt FROM user_activity WHERE user_id=%s', (user_id,)
        ).fetchone()['cnt']
        rows = conn.execute(
            'SELECT id, type, title, content, created_at FROM user_activity WHERE user_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s',
            (user_id, page_size, offset)
        ).fetchall()

    return jsonify({
        'success': True,
        'data': [dict(r) for r in rows],
        'total': total,
        'page': page,
        'page_size': page_size,
    })


# ================================================================
# 实名认证接口 v2（合规：不存储身份证号）
# ================================================================

# GET /user/verification — 查询当前用户认证状态
@user_bp.route('/verification', methods=['GET'])
def get_verification():
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']

    # INTL market: skip real-name verification (return as already verified)
    if os.environ.get('DEPLOY_MARKET', 'cn') == 'intl':
        return jsonify({
            'success': True,
            'data': {
                'verified': True,
                'verified_by': 'intl_bypass',
                'verified_at': '',
                'display_name': '',
                'is_real_name_verified': False,
                'real_name_verified_at': '',
                'need_verification': False,
            }
        })

    with get_db() as conn:
        user = conn.execute(
            'SELECT verified_by, verified_at, display_name, is_real_name_verified, real_name_verified_at FROM users WHERE id=%s',
            (user_id,)
        ).fetchone()
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404
    return jsonify({
        'success': True,
        'data': {
            'verified': bool(user['verified_by']),
            'verified_by': user['verified_by'] or '',
            'verified_at': user['verified_at'] or '',
            'display_name': user['display_name'] or '',
            'is_real_name_verified': bool(user['is_real_name_verified']),
            'real_name_verified_at': user['real_name_verified_at'] or '',
        }
    })


# POST /user/verification/apply — 发起实名认证（JWT 鉴权）
@user_bp.route('/verification/apply', methods=['POST'])
def apply_verification():
    try:
        payload, err = _require_auth()
        if err:
            return err
        user_id = payload['user_id']

        # INTL market: skip real-name verification
        if os.environ.get('DEPLOY_MARKET', 'cn') == 'intl':
            return jsonify({'success': True, 'data': {
                'verified': True,
                'need_verification': False,
                'display_name': payload.get('display_name', ''),
            }})

        data = request.get_json() or {}
        return_url = data.get('return_url', '')
        cert_name = data.get('cert_name', '').strip()
        cert_no = data.get('cert_no', '').strip()
        if not cert_name or not cert_no:
            return jsonify({'success': False, 'error': 'Please provide real name and ID number'}), 400
        if not return_url:
            from urllib.parse import urljoin
            return_url = urljoin(request.host_url, '/%ssection=account')

        result = initiate_verification(user_id, return_url, cert_name=cert_name, cert_no=cert_no)
        if result['success']:
            auth_url = result.get('data', {}).get('auth_url', '')
            # 返回JSON包含认证URL，前端直接跳转
            return jsonify(result)
        return jsonify(result), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Verification service error: {str(e)}'}), 500


# POST /user/verification/callback — 第三方认证回调（无需 JWT，通过 request_id 识别用户）
@user_bp.route('/verification/callback', methods=['GET', 'POST'])
def verification_callback():
    # 合并 GET 和 POST 参数（支付宝用 POST，微信用 GET）
    params = {}
    if request.method == 'POST':
        params.update(request.form.to_dict())
    params.update(request.args.to_dict())

    request_id = params.get('request_id') or params.get('outer_order_no') or ''

    # 从流水号中解析 user_id（格式: rv_<user_id>_<timestamp>_<uuid>）
    user_id = None
    if request_id.startswith('rv_'):
        try:
            user_id = int(request_id.split('_')[1])
        except (ValueError, IndexError):
            pass

    if not user_id:
        # 尝试从 verification_requests 表查询
        with get_db() as conn:
            row = conn.execute(
                "SELECT user_id FROM verification_requests WHERE request_id=%s",
                (request_id,)
            ).fetchone()
        if row:
            user_id = row['user_id']

    if not user_id:
        return jsonify({'success': False, 'error': 'Unrecognized verification request'}), 400

    result = verify_callback(user_id, params)

    if result['success']:
        return jsonify(result)
    return jsonify(result), 400





