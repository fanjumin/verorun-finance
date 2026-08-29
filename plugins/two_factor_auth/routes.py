"""2FA plugin — Flask Blueprint (管理路由 + 登录挑战校验)。

路由前缀由 PluginManager 自动添加为 /plugin/two_factor_auth。
- /setup-page            : iframe 设置页（静态 HTML）
- /setup/init  (token)   : 生成 TOTP 密钥 + 二维码（is_enabled=false 暂存）
- /setup/verify (token)  : 校验动态码，正式启用并下发恢复码
- /setup/disable (token) : 关闭 2FA
- /status (token)        : 当前启用状态
- /challenge/verify      : 登录流程第二因子校验（公开），通过后签发最终 SSO 会话
"""
import logging

from flask import (
    Blueprint, request, jsonify, make_response, render_template, current_app,
)
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

try:
    from services.jwt_service import create_token, validate_token
    from services.session_service import set_sso_cookie
except Exception:
    from auth_center.services.jwt_service import create_token, validate_token
    from auth_center.services.session_service import set_sso_cookie

from .models import get_two_factor_db, get_pooled_connection
from .services import TOTPService, get_encryption_key

bp = Blueprint('two_factor_auth', __name__, template_folder='templates')


# ── 健康检查 ──
@bp.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True})


# ── 配置读取（标准 §10.3：plugin_registry.config 经 PluginManager；TOTP_* 环境变量为显式覆盖）──
def _cfg(key, default):
    env = current_app.config.get(f"TOTP_{key.upper()}")
    if env is not None:
        return env
    try:
        mgr = current_app.extensions.get('plugin_manager')
        if mgr:
            cfg = mgr.get_config('two_factor_auth') or {}
            # settings_schema 键名对齐：totp_valid_window ↔ valid_window
            k = 'totp_valid_window' if key == 'valid_window' else key
            if k in cfg:
                return cfg[k]
    except Exception:
        pass
    return default


def _get_service() -> TOTPService:
    key = get_encryption_key()
    return TOTPService(
        key,
        valid_window=int(_cfg('valid_window', 1)),
        issuer=_cfg('issuer_name', 'VeroRun'),
    )


def _auth_user_id():
    """仅接受 Authorization Bearer（F-11 修复：JWT 不进入 URL/日志/referrer）。
    F-10：校验 token_type=access，拒绝 refresh token（30 天）换取短期 setup_token。"""
    auth = request.headers.get('Authorization', '')
    tok = auth[7:] if auth.startswith('Bearer ') else None
    if not tok:
        return None
    payload = validate_token(tok)
    if not payload or payload.get('token_type') != 'access':
        return None
    return payload.get('user_id')


def _gen_setup_token(user_id):
    """生成短期 setup_token 并入库（3 分钟有效 + 绑定生成时 IP/UA）。

    F-01：缩短有效期并绑定设备上下文，降低令牌经 URL/日志泄露后的利用面；
    使用处（_setup_token_row）宽松比对设备，不一致视为泄露并作废。
    """
    import secrets
    from datetime import datetime, timedelta, timezone
    tok = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=3)
    ip = request.remote_addr or ''
    ua = request.headers.get('User-Agent', '')[:200]
    with get_two_factor_db() as conn:
        conn.execute(
            "INSERT INTO setup_tokens (token, user_id, expires_at, ip_address, user_agent) "
            "VALUES (%s,%s,%s,%s,%s)",
            (tok, user_id, expires, ip, ua))
        conn.commit()
    return tok


def _setup_token():
    """从 ?setup_token= 或 Authorization 取短期 setup_token 原文。"""
    auth = request.headers.get('Authorization', '')
    return (auth[7:] if auth.startswith('Bearer ') else request.args.get('setup_token')) or None


def _utc(dt):
    """统一转换为 UTC：aware 直接转换；naive 视为 UTC（本插件写入始终为 UTC 墙钟时间）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ip_prefix(ip):
    """宽松 IP 归一化：IPv4 取前三段，IPv6 取前两 hextet（容忍 NAT/运营商前缀变化）。"""
    if not ip:
        return ''
    if ':' in ip:
        parts = ip.split(':')
        return ':'.join(parts[:2])
    return '.'.join(ip.split('.')[:3])


def _setup_token_row(tok):
    """返回有效且设备绑定的 setup_token 行；无效/泄露则返回 None。

    F-01/F-05：校验过期（astimezone 正确转换，不依赖 SET LOCAL timezone）与
    生成时 IP/UA 宽松绑定——令牌被其它设备/网络使用时视为泄露，作废并审计。
    """
    from datetime import datetime, timezone
    if not tok:
        return None
    with get_two_factor_db() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at, ip_address, user_agent, "
            "       failed_attempts, locked_until, init_used, pending_secret, pending_iv "
            "FROM setup_tokens WHERE token=%s", (tok,)).fetchone()
    if not row:
        return None
    if row['expires_at'].astimezone(timezone.utc) < datetime.now(timezone.utc):
        return None
    cur_ip = request.remote_addr or ''
    cur_ua = request.headers.get('User-Agent', '')[:200]
    ip_ok = (not row['ip_address']) or (not cur_ip) or \
            _ip_prefix(row['ip_address']) == _ip_prefix(cur_ip)
    ua_ok = (not row['user_agent']) or cur_ua.startswith(row['user_agent'][:60])
    if not (ip_ok and ua_ok):
        # F-01：设备上下文不一致 → 视为令牌泄露，作废并审计
        with get_two_factor_db() as conn:
            conn.execute("DELETE FROM setup_tokens WHERE token=%s", (tok,))
            conn.commit()
        _audit(row['user_id'], 'setup_token_binding_mismatch')
        return None
    return row


def _setup_user_id():
    """解析 setup_token 或 sso_token cookie 对应的 user_id（校验过期 + 设备绑定）。

    setup_token 优先（门户短期令牌）；无 token 时回退验证 sso_token cookie——
    同源 admin iframe 自动携带该 cookie，JWT 不进 URL，与 F-11 设计一致。
    """
    tok = _setup_token()
    if tok:
        row = _setup_token_row(tok)
        return row['user_id'] if row else None
    cookie_tok = request.cookies.get('sso_token')
    if not cookie_tok:
        return None
    payload = validate_token(cookie_tok)
    if not payload or payload.get('token_type') != 'access':
        return None
    return payload.get('user_id')


def _issue_session(user_id, phone, app_name, is_admin, role):
    """签发最终 JWT + 写 user_sessions + 种 sso_token cookie（与核心登录一致）。"""
    token = create_token(user_id, phone=phone, app_name=app_name,
                         is_admin=is_admin, role=role)
    token_hash = __import__('hashlib').sha256(token.encode()).hexdigest()
    ua = request.headers.get('User-Agent', '')[:200]
    ip = request.remote_addr or ''
    device_name = '2FA Verified (admin)' if app_name == 'admin' else '2FA Verified'
    with get_pooled_connection() as conn:
        conn.execute(
            "INSERT INTO public.user_sessions "
            "(user_id, token_hash, device_name, device_type, ip_address, user_agent, is_current, last_active, created_at) "
            "VALUES (%s,%s,%s,'web',%s,%s,1,NOW(),NOW())",
            (user_id, token_hash, device_name, ip, ua),
        )
        conn.commit()
    resp = make_response(jsonify({'success': True, 'data': {
        'token': token,
        'user': {'id': user_id, 'phone': phone, 'is_admin': bool(is_admin)},
    }}))
    set_sso_cookie(resp, token, app_name)
    return resp


def _audit(user_id, action, details=None):
    try:
        with get_two_factor_db() as conn:
            conn.execute(
                "INSERT INTO audit_log (user_id, action, ip_address, user_agent, details) "
                "VALUES (%s,%s,%s,%s,%s)",
                (user_id, action, request.remote_addr or '',
                 request.headers.get('User-Agent', '')[:500], details),
            )
            conn.commit()
    except Exception as e:
        # F-07：审计写失败不再静默吞错
        logger.error("2FA audit write failed (%s): %s", action, e)


# ── 管理页（iframe）──
@bp.route('/setup-token', methods=['POST'])
def setup_token_issue():
    """门户入口换取短期 setup_token：登录 JWT 只在父页面内存中，不进入 URL（复审 follow-up #1）。"""
    uid = _auth_user_id()
    if not uid:
        return jsonify({'error': 'unauthorized'}), 401
    tok = _gen_setup_token(uid)
    # F-07：令牌签发审计
    _audit(uid, 'setup_token_issued')
    return jsonify({'token': tok})


@bp.route('/setup-page')
def setup_page():
    # 门户先经 /setup-token 换取短期令牌再跳转本页（?setup_token=），避免长期登录 JWT 明文入 URL
    uid = _setup_user_id()
    if not uid:
        return jsonify({'error': 'unauthorized',
                        'reason': 'setup token invalid or expired'}), 401
    # F-07：页面访问审计
    _audit(uid, 'setup_page_access')
    lang = request.args.get('lang', 'zh-CN')
    if lang not in ('zh-CN', 'en'):
        lang = 'zh-CN'
    # F-06 修复：done 参数经服务端同源校验后再下发模板，杜绝客户端开放重定向
    done = _safe_redirect(request.args.get('done', '') or '')
    return render_template('setup.html', lang=lang, done=done)


# ── 登录第二因子校验页（公开，插件自带 UI）──
def _safe_redirect(target):
    """开放重定向防护（复审 P0）：仅允许站内相对路径或本站 host。"""
    import urllib.parse
    if not target:
        return '/'
    if target.startswith('/') and not target.startswith('//'):
        return target
    try:
        p = urllib.parse.urlparse(target)
        host = urllib.parse.urlparse(request.host_url).netloc
        if p.scheme in ('http', 'https') and p.netloc in (host, request.host):
            return target
    except Exception:
        pass
    return '/'


@bp.route('/challenge-page')
def challenge_page():
    """公开：核心登录返回 needs_2fa 后跳转至此完成第二因子校验。"""
    challenge_token = request.args.get('challenge_token', '') or ''
    redirect = _safe_redirect(request.args.get('redirect', '') or '')
    lang = request.args.get('lang', 'zh-CN')
    if lang not in ('zh-CN', 'en'):
        lang = 'zh-CN'
    return render_template('challenge.html', challenge_token=challenge_token,
                           redirect=redirect, lang=lang)


# ── 状态 ──
@bp.route('/status')
def status():
    uid = _auth_user_id()
    if not uid:
        return jsonify({'error': 'unauthorized'}), 401
    with get_two_factor_db() as conn:
        row = conn.execute(
            "SELECT is_enabled, enabled_at FROM user_totp WHERE user_id=%s",
            (uid,)).fetchone()
    return jsonify({
        'enabled': bool(row['is_enabled']) if row else False,
        'enabled_at': row['enabled_at'].isoformat() if row and row['enabled_at'] else None,
    })


# ── 初始化绑定（待确认密钥挂到本次 setup_token，不触碰 user_totp.is_enabled）──
@bp.route('/setup/init', methods=['POST'])
def setup_init():
    tok = _setup_token()
    row = _setup_token_row(tok)
    if not row:
        return jsonify({'error': 'unauthorized'}), 401
    uid = row['user_id']
    if row['init_used']:
        # F-01：同一 setup_token 拒绝二次 init，防泄露令牌被反复用于生成密钥
        return jsonify({'error': 'already initialized'}), 400
    svc = _get_service()
    secret = svc.generate_secret()
    enc, iv = svc.encrypt_secret(secret, uid)

    # 重绑定中途放弃不得降级既有 2FA：待确认密钥只写 setup_tokens（0002 迁移加列）
    # F-01：init 一次性——标记 init_used，后续相同 token 再 init 直接拒绝
    with get_two_factor_db() as conn:
        conn.execute(
            "UPDATE setup_tokens SET pending_secret=%s, pending_iv=%s, init_used=true "
            "WHERE token=%s AND user_id=%s",
            (enc, iv, tok, uid))
        conn.commit()

    account = _resolve_account(uid)
    uri = svc.get_provisioning_uri(secret, account)
    qr = svc.generate_qr_code(uri)
    _audit(uid, 'setup_init')
    return jsonify({'qr_code': qr, 'secret': secret, 'account': account})


# ── 确认启用（从 setup_tokens 取待确认密钥，校验通过后原子落 user_totp）──
@bp.route('/setup/verify', methods=['POST'])
def setup_verify():
    tok = _setup_token()
    row = _setup_token_row(tok)
    if not row:
        return jsonify({'error': 'unauthorized'}), 401
    uid = row['user_id']
    data = request.get_json() or {}
    code = data.get('code', '')
    svc = _get_service()

    # F-02：setup/verify 失败锁定（与 challenge_verify / setup_disable 对齐）
    if row['locked_until'] and \
       _utc(row['locked_until']) > datetime.now(timezone.utc):
        return jsonify({'error': 'setup locked'}), 429
    if not row['pending_secret']:
        return jsonify({'error': 'no pending setup'}), 400

    secret = svc.decrypt_secret(row['pending_secret'], row['pending_iv'], uid)
    ok = svc.verify_code(secret, code)

    # F-01：已启用 2FA 用户重绑定需旧码确认——防泄露令牌直接覆盖既有 2FA 完成接管
    with get_two_factor_db() as conn:
        existing = conn.execute(
            "SELECT encrypted_secret, secret_iv FROM user_totp "
            "WHERE user_id=%s AND is_enabled=true", (uid,)).fetchone()
    if ok and existing:
        confirm_code = (data.get('confirm_old_code') or '').strip()
        if not confirm_code:
            return jsonify({'error': 'old code required'}), 400
        ok = svc.verify_code(
            svc.decrypt_secret(existing['encrypted_secret'], existing['secret_iv'], uid),
            confirm_code)

    if not ok:
        # F-02：失败计数 + 短锁定（原子自增，与 challenge_verify 同款单语句 CASE，无 TOCTOU）
        max_f = int(_cfg('max_failed_attempts', 5))
        lock_s = int(_cfg('lockout_seconds', 900))
        with get_two_factor_db() as conn:
            cur = conn.execute(
                """UPDATE setup_tokens SET
                      failed_attempts = CASE
                          WHEN locked_until IS NOT NULL AND locked_until <= NOW() THEN 1
                          ELSE failed_attempts + 1
                      END,
                      locked_until = CASE
                          WHEN (CASE
                              WHEN locked_until IS NOT NULL AND locked_until <= NOW() THEN 1
                              ELSE failed_attempts + 1 END) >= %s
                          THEN NOW() + (%s * interval '1 second')
                          ELSE locked_until
                      END
                   WHERE token=%s RETURNING failed_attempts""",
                (max_f, lock_s, tok))
            cur.fetchone()
            conn.commit()
        _audit(uid, 'setup_verify_failed')
        return jsonify({'error': 'Invalid code'}), 401

    count = int(_cfg('recovery_code_count', 10))
    raw_codes = svc.generate_recovery_codes(count)
    hashed = [svc.hash_recovery_code(c) for c in raw_codes]

    with get_two_factor_db() as conn:
        conn.execute(
            """INSERT INTO user_totp
                 (user_id, encrypted_secret, secret_iv, is_enabled, enabled_at, recovery_code_hashes)
               VALUES (%s,%s,%s,true,NOW(),%s)
               ON CONFLICT (user_id) DO UPDATE
                 SET encrypted_secret = EXCLUDED.encrypted_secret,
                     secret_iv = EXCLUDED.secret_iv,
                     is_enabled = true,
                     enabled_at = NOW(),
                     recovery_code_hashes = %s,
                     failed_attempts = user_totp.failed_attempts,
                     locked_until = user_totp.locked_until""",
            (uid, row['pending_secret'], row['pending_iv'], hashed, hashed))
        # C2 修复：setup_token 一次性——启用成功后作废，防泄露令牌被复用/重放覆盖 pending_secret
        conn.execute("DELETE FROM setup_tokens WHERE token=%s", (tok,))
        conn.commit()
    _audit(uid, 'setup_enable')
    return jsonify({'success': True, 'recovery_codes': raw_codes})


# ── 关闭（须提交当前动态码，防会话持有者静默降级 2FA）──
@bp.route('/setup/disable', methods=['POST'])
def setup_disable():
    uid = _setup_user_id()
    tok = _setup_token()
    if not uid:
        return jsonify({'error': 'unauthorized'}), 401
    code = (request.get_json() or {}).get('code', '').strip()
    if not code:
        return jsonify({'error': '2FA code required'}), 400
    svc = _get_service()
    with get_two_factor_db() as conn:
        row = conn.execute(
            "SELECT encrypted_secret, secret_iv, locked_until FROM user_totp "
            "WHERE user_id=%s AND is_enabled=true", (uid,)).fetchone()
    if not row:
        return jsonify({'error': '2FA not enabled'}), 400
    # 锁定检查（与 challenge_verify 一致）
    if row['locked_until'] and \
       _utc(row['locked_until']) > datetime.now(timezone.utc):
        return jsonify({'error': 'Account locked'}), 429
    secret = svc.decrypt_secret(row['encrypted_secret'], row['secret_iv'], uid)
    if not svc.verify_code(secret, code):
        # F-03 修复：失败计数 + 锁定（原子自增），防会话持有者暴力枚举关闭码
        max_f = int(_cfg('max_failed_attempts', 5))
        lock_s = int(_cfg('lockout_seconds', 900))
        with get_two_factor_db() as conn:
            cur = conn.execute(
                """UPDATE user_totp SET
                      failed_attempts = CASE
                          WHEN locked_until IS NOT NULL AND locked_until <= NOW() THEN 1
                          ELSE failed_attempts + 1
                      END,
                      locked_until = CASE
                          WHEN (CASE
                              WHEN locked_until IS NOT NULL AND locked_until <= NOW() THEN 1
                              ELSE failed_attempts + 1 END) >= %s
                          THEN NOW() + (%s * interval '1 second')
                          ELSE locked_until
                      END
                   WHERE user_id=%s RETURNING failed_attempts""",
                (max_f, lock_s, uid))
            cur.fetchone()
            conn.commit()
        _audit(uid, 'setup_disable_failed')
        return jsonify({'error': 'Invalid code'}), 401
    with get_two_factor_db() as conn:
        conn.execute(
            "UPDATE user_totp SET is_enabled=false, recovery_code_hashes='{}', "
            "failed_attempts=0, locked_until=NULL WHERE user_id=%s", (uid,))
        # C2 修复：setup_token 一次性——关闭成功后作废，防泄露令牌被复用
        if tok:
            conn.execute("DELETE FROM setup_tokens WHERE token=%s", (tok,))
        conn.commit()
    _audit(uid, 'setup_disable')
    return jsonify({'success': True})


# ── 登录第二因子校验（公开）──
@bp.route('/challenge/verify', methods=['POST'])
def challenge_verify():
    data = request.get_json() or {}
    challenge_id = (data.get('challenge_token') or '').strip()
    code = (data.get('code') or '').strip()
    if not challenge_id or not code:
        return jsonify({'error': 'missing params'}), 400

    with get_two_factor_db() as conn:
        row = conn.execute(
            "SELECT user_id, method, expires_at, consumed, app_name, ip_address, user_agent "
            "FROM two_factor_challenges WHERE challenge_id=%s", (challenge_id,)).fetchone()

    if not row:
        return jsonify({'error': 'invalid challenge'}), 400
    if row['consumed']:
        return jsonify({'error': 'challenge already used'}), 400
    if row['expires_at'].astimezone(timezone.utc) < datetime.now(timezone.utc):
        return jsonify({'error': 'challenge expired'}), 400
    # F-06：challenge 绑定生成时设备（IP/UA 宽松比对），不一致视为泄露、拒绝完成登录
    cur_ip = request.remote_addr or ''
    cur_ua = request.headers.get('User-Agent', '')[:200]
    if (row['ip_address'] and cur_ip and
            _ip_prefix(row['ip_address']) != _ip_prefix(cur_ip)):
        return jsonify({'error': 'device changed, please login again'}), 400
    if row['user_agent'] and not cur_ua.startswith(row['user_agent'][:60]):
        return jsonify({'error': 'device changed, please login again'}), 400

    user_id = row['user_id']
    challenge_app_name = row['app_name'] or 'main'
    svc = _get_service()
    ok = False
    used_recovery = False

    with get_two_factor_db() as conn:
        totp_row = conn.execute(
            "SELECT encrypted_secret, secret_iv, failed_attempts, locked_until "
            "FROM user_totp WHERE user_id=%s AND is_enabled=true", (user_id,)
        ).fetchone()

        if not totp_row:
            # F-09：收敛探测——不区分「未启用 2FA」，统一 invalid challenge
            return jsonify({'error': 'invalid challenge'}), 400

        # 锁定检查
        if totp_row['locked_until'] and \
           _utc(totp_row['locked_until']) > datetime.now(timezone.utc):
            return jsonify({'error': 'Account locked'}), 429

        secret = svc.decrypt_secret(totp_row['encrypted_secret'], totp_row['secret_iv'], user_id)
        hashes = totp_row['recovery_code_hashes'] or []

        if svc.verify_code(secret, code):
            ok = True
        else:
            # F-08：失败路径随机抽样 1–2 个恢复码 hash，降低 bcrypt 遍历的 CPU DoS 面
            import random
            sample = random.sample(hashes, min(2, len(hashes))) if hashes else []
            for h in sample:
                if svc.verify_recovery_code(code, h):
                    ok = True
                    used_recovery = True
                    break

        if ok:
            # 清除已用恢复码，重置失败计数
            new_hashes = [h for h in hashes if not (used_recovery and svc.verify_recovery_code(code, h))] \
                if used_recovery else hashes
            conn.execute(
                "UPDATE user_totp SET failed_attempts=0, locked_until=NULL, "
                "recovery_code_hashes=%s, last_verified_at=NOW() WHERE user_id=%s",
                (new_hashes, user_id))
            # 原子消费：仅当尚未被消费时才置 consumed=true，并用 RETURNING 校验，
            # 杜绝并发重放（同一 challenge 领多个会话）。
            cur = conn.execute(
                "UPDATE two_factor_challenges SET consumed=true "
                "WHERE challenge_id=%s AND consumed=false RETURNING id",
                (challenge_id,))
            if not cur.fetchone():
                conn.commit()
                return jsonify({'error': 'challenge already used'}), 400
            conn.commit()
        else:
            max_f = int(_cfg('max_failed_attempts', 5))
            lock_s = int(_cfg('lockout_seconds', 900))
            # F-02 修复：原子自增并返回新值，消除并发绕过锁定的 TOCTOU 竞态。
            # 锁定期已过则归零重计（CASE 单语句内完成，避免"读-改-写"非原子窗口）。
            cur = conn.execute(
                """UPDATE user_totp SET
                      failed_attempts = CASE
                          WHEN locked_until IS NOT NULL AND locked_until <= NOW() THEN 1
                          ELSE failed_attempts + 1
                      END,
                      locked_until = CASE
                          WHEN (CASE
                              WHEN locked_until IS NOT NULL AND locked_until <= NOW() THEN 1
                              ELSE failed_attempts + 1 END) >= %s
                          THEN NOW() + (%s * interval '1 second')
                          ELSE locked_until
                      END
                   WHERE user_id=%s
                   RETURNING failed_attempts""",
                (max_f, lock_s, user_id))
            cur.fetchone()
            conn.commit()
            _audit(user_id, 'login_failed')
            return jsonify({'error': 'Invalid code'}), 401

    _audit(user_id, 'login_success', {'method': 'recovery' if used_recovery else 'totp'})

    # 取用户资料，签发最终会话（app_name 沿用登录入口，避免错配成 platform）
    phone, is_admin, role = _load_user_profile(user_id)
    return _issue_session(user_id, phone, challenge_app_name, is_admin, role or 'user')


# ── 工具 ──
def _resolve_account(user_id):
    with get_pooled_connection() as conn:
        row = conn.execute(
            "SELECT email, username, phone FROM public.users WHERE id=%s",
            (user_id,)).fetchone()
    if not row:
        return str(user_id)
    return row['email'] or row['username'] or row['phone'] or str(user_id)


def _load_user_profile(user_id):
    with get_pooled_connection() as conn:
        row = conn.execute(
            "SELECT phone, is_admin FROM public.users WHERE id=%s", (user_id,)
        ).fetchone()
    phone = row['phone'] if row else None
    is_admin = bool(row['is_admin']) if row else False
    role = 'user'
    if is_admin:
        try:
            with get_pooled_connection() as c2:
                prof = c2.execute(
                    "SELECT role FROM public.admin_profiles WHERE user_id=%s",
                    (user_id,)).fetchone()
            if prof:
                role = prof['role']
        except Exception:
            pass
    return phone, is_admin, role
