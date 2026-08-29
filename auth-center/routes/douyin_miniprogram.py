#!/usr/bin/env python3
"""Douyin Mini-Program Routes — Public endpoints for Douyin mini-program integration"""
from i18n import _
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from flask import Blueprint, request, jsonify
from models import get_db, now_iso
from services.jwt_service import validate_token, create_token
from services.name_validator import sanitize_name
try:
    from plugins.oauth_config.services.douyin_service import code2session, miniprogram_is_stub
except ImportError:
    # verorun-pro 精简版无 plugins 目录
    code2session = None
    miniprogram_is_stub = True

douyin_mp_bp = Blueprint('douyin_mp', __name__, url_prefix='/douyin_mp')


def _get_site_domain():
    """从请求中提取域名"""
    host = request.headers.get('Host', '')
    domain = host.split(':')[0]
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain


def api_ok(data=None):
    return jsonify({'success': True, 'data': data})


def api_err(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code


def get_current_user_id(token):
    """Validate token and return user_id if valid"""
    payload = validate_token(token)
    if not payload:
        return None
    return payload.get('user_id')


# =============================================
# 小程序登录接口
# =============================================

@douyin_mp_bp.route('/login/code', methods=['POST'])
def login_with_code():
    """抖音小程序登录：前端调用 tt.login() 获取 code，后端用 code 换取 openid 登录
    
    请求体：
    {
        "code": "xxxxx",           // 必填，tt.login() 返回的 code
        "nickname": _("User Nickname"),     // 可选，用于更新用户信息
        "avatar": "https://..."    // 可选，用于更新用户头像
    }
    
    返回：
    {
        "success": true,
        "data": {
            "token": "jwt_token",
            "user": {
                "id": 123,
                "username": _("TikTok User xxx"),
                "douyin_nickname": _("User Nickname"),
                "douyin_avatar": _("Avatar URL")",
                "is_new_user": true/false
            }
        }
    }
    """
    data = request.get_json() or {}
    code = data.get('code')
    nickname = data.get('nickname', '')
    avatar = data.get('avatar', '')

    if not code:
        return api_err(_('Code is required'), 400)

    domain = _get_site_domain()
    
    # 调用 code2session 获取 openid
    result = code2session(code, site_domain=domain)
    if result.get('error'):
        return api_err(f'TikTok Login Failed: {result["error"]}', 500)

    openid = result['openid']

    if not openid:
        return api_err(_('Failed to Obtain User Identifier'), 500)

    # 查找或创建用户
    try:
        with get_db() as conn:
            # 检查是否已存在该抖音用户
            user = conn.execute(
                'SELECT id, phone, username, display_name, douyin_open_id, douyin_nickname, '
                'douyin_avatar, is_admin, agent_id, agent_nickname, agent_avatar_url '
                'FROM users WHERE douyin_open_id = %s',
                (openid,)
            ).fetchone()

            is_new_user = False
            if user:
                # 已存在用户：更新昵称和头像（如果提供）
                if nickname or avatar:
                    conn.execute('''
                        UPDATE users 
                        SET douyin_nickname = COALESCE(%s, douyin_nickname),
                            douyin_avatar = COALESCE(%s, douyin_avatar),
                            last_login = %s
                        WHERE id = %s
                    ''', (nickname or None, avatar or None, now_iso(), user['id']))
                    conn.commit()
            else:
                # 新用户：自动创建
                is_new_user = True
                display_name = nickname or f'TikTok User_{openid[-6:]}'
                safe_name = sanitize_name(display_name) if nickname else f'dy_{openid[-8:]}'
            
                conn.execute('''
                    INSERT INTO users (
                        username, display_name, douyin_open_id, douyin_nickname, 
                        douyin_avatar, created_at, last_login
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''', (
                    safe_name,
                    display_name,
                    openid,
                    nickname or '',
                    avatar or '',
                    now_iso(),
                    now_iso()
                ))
                conn.commit()
            
                # 获取刚创建的用户
                user = conn.execute(
                    'SELECT id, phone, username, display_name, douyin_open_id, douyin_nickname, '
                    'douyin_avatar, is_admin, agent_id, agent_nickname, agent_avatar_url '
                    'FROM users WHERE douyin_open_id = %s',
                    (openid,)
                ).fetchone()
    except Exception:
        return api_err('Query failed', 500)

    # 统一登录签发通道
    from services.session_service import issue_auth_session
    result = issue_auth_session(
        user['id'], user['phone'] or '', app_name='douyin_miniprogram',
        is_admin=bool(user['is_admin']),
        user_info={
            'username': user['username'],
            'display_name': user['display_name'] or user['username'],
            'douyin_nickname': user['douyin_nickname'] or '',
            'douyin_avatar': user['douyin_avatar'] or '',
            'agent_id': user['agent_id'] or '',
            'agent_nickname': user['agent_nickname'] or '',
            'agent_avatar_url': user['agent_avatar_url'] or '',
        },
        device_name='Douyin Mini Program', device_type='mobile')
    token = result['token']

    return api_ok({
        'token': token,
        'user': result['user'],
        'is_new_user': is_new_user
    })


@douyin_mp_bp.route('/user/info', methods=['GET'])
def user_info():
    """Get current user's information (for mini-program)
    Requires Authorization: Bearer <token> header
    """
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return api_err(_('No valid token provided'), 401)
    token = auth.replace('Bearer ', '')
    user_id = get_current_user_id(token)
    if not user_id:
        return api_err(_('Invalid or Expired Token'), 401)
    
    try:
        with get_db() as conn:
            user = conn.execute('''
                SELECT id, phone, username, display_name, 
                       douyin_open_id, douyin_nickname, douyin_avatar,
                       is_admin, agent_id, agent_nickname, agent_avatar_url
                FROM users WHERE id = %s
            ''', (user_id,)).fetchone()
            if not user:
                return api_err(_('User does not exist'), 404)
        
            # Convert to dict and sanitize
            user_dict = {
                'id': user['id'],
                'phone': user['phone'],
                'username': user['username'],
                'display_name': user['display_name'] or user['username'],
                'is_admin': bool(user['is_admin']),
                'douyin_bound': bool(user['douyin_open_id']),
                'douyin_nickname': user['douyin_nickname'] or '',
                'douyin_avatar': user['douyin_avatar'] or '',
                'agent_id': user['agent_id'] or '',
                'agent_nickname': user['agent_nickname'] or '',
                'agent_avatar_url': user['agent_avatar_url'] or ''
            }
    except Exception:
        return api_err('Query failed', 500)
    return api_ok(user_dict)


@douyin_mp_bp.route('/user/unbind_douyin', methods=['POST'])
def unbind_douyin():
    """Unbind Douyin account from current user
    Requires Authorization: Bearer <token> header
    """
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return api_err(_('No valid token provided'), 401)
    token = auth.replace('Bearer ', '')
    user_id = get_current_user_id(token)
    if not user_id:
        return api_err(_('Invalid or Expired Token'), 401)
    
    try:
        with get_db() as conn:
            # Check if user has Douyin bound
            current = conn.execute(
                'SELECT douyin_open_id FROM users WHERE id = %s', 
                (user_id,)
            ).fetchone()
            if not current or not current['douyin_open_id']:
                return api_err(_('Current user has not bound a Douyin account'), 400)
        
            # Unbind: set Douyin fields to NULL/empty
            conn.execute('''
                UPDATE users 
                SET douyin_open_id = NULL, 
                    douyin_nickname = NULL, 
                    douyin_avatar = NULL
                WHERE id = %s
            ''', (user_id,))
            conn.commit()
    except Exception:
        return api_err('Query failed', 500)
        
    return api_ok({'message': _('TikTok Account Successfully Unlinked')})


@douyin_mp_bp.route('/user/bind_status', methods=['GET'])
def bind_status():
    """Check if current user has Douyin bound
    Requires Authorization: Bearer <token> header
    """
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return api_err(_('No valid token provided'), 401)
    token = auth.replace('Bearer ', '')
    user_id = get_current_user_id(token)
    if not user_id:
        return api_err(_('Invalid or Expired Token'), 401)
    
    try:
        with get_db() as conn:
            douyin_open_id = conn.execute(
                'SELECT douyin_open_id FROM users WHERE id = %s', 
                (user_id,)
            ).fetchone()
    except Exception:
        return api_err('Query failed', 500)
    is_bound = bool(douyin_open_id and douyin_open_id['douyin_open_id'])
    return api_ok({'bound': is_bound})


# Note: Publishing content from mini-program would require additional security considerations
# and is not included in this initial implementation. If needed, similar endpoints can be added
# with proper validation and rate limiting.