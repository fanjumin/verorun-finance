#!/usr/bin/env python3
"""IM Gateway — 小程序开发者账户管理端点（Phase 7：集中 mini_app_builder 登录）。

把 mini_app_builder 的 dev_accounts（小程序平台开发者凭据）集中到 IM Gateway 管理：
不新建表、不改 mini_app_builder，直接复用其 models 数据层函数（共享连接池）。
mini_app_builder 未启用时优雅报错，不影响 IM Gateway 其余功能。

各平台小程序登录方式（供前端展示）：
    douyin / toutiao   tt.login() code → code2session → openid → JWT
    wechat             wx.login() code → get_openid_by_code → unionid → JWT
    telegram           WebApp.initData → HMAC 验签（bot_token）→ JWT
    line               LIFF accessToken → LINE profile API 校验 → JWT

Prefix: /admin/channels/miniapp-accounts
"""
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

miniapp_bp = Blueprint('im_gateway_miniapp_accounts', __name__,
                       url_prefix='/admin/channels/miniapp-accounts')

# 与 mini_app_builder submodules/accounts 白名单一致
# （whatsapp 生成器存在但 dev_accounts 无凭据条目，暂不纳入 CRUD，待其白名单扩展后跟进）
PLATFORMS = ['douyin', 'toutiao', 'wechat', 'telegram', 'line']

# 各平台小程序登录方式 meta（前端据此渲染说明，key 对应 i18n）
LOGIN_METHODS = {
    'douyin':   {'method_type': 'code',        'endpoint': '/api/v1/mini-program/auth/login'},
    'toutiao':  {'method_type': 'code',        'endpoint': '/api/v1/mini-program/auth/login'},
    'wechat':   {'method_type': 'code',        'endpoint': '/api/v1/mini-program/auth/login'},
    'telegram': {'method_type': 'initdata',    'endpoint': '/api/v1/mini-program/auth/login'},
    'line':     {'method_type': 'accesstoken', 'endpoint': '/api/v1/mini-program/auth/login'},
}


def _require_admin():
    """复用主系统管理员鉴权（与 routes_overview / routes_developer 一致）"""
    from routes.admin import _require_admin as _ra
    return _ra()


def _ok(data=None):
    return jsonify({'success': True, 'data': data})


def _err(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code


def _mini_models():
    """懒加载 mini_app_builder 数据层；插件未启用时抛 ImportError。"""
    from plugins.mini_app_builder.submodules.accounts import models
    return models


def _decorate(accounts):
    """给账户列表补充登录方式 meta（method_type / login_endpoint）。"""
    out = []
    for a in accounts:
        meta = LOGIN_METHODS.get(a.get('platform', ''), {})
        a['method_type'] = meta.get('method_type', '')
        a['login_endpoint'] = meta.get('endpoint', '')
        out.append(a)
    return out


@miniapp_bp.route('/', methods=['GET'])
def list_accounts():
    """列出小程序开发者账户（可选 platform 过滤），敏感字段已掩码。"""
    admin, err = _require_admin()
    if err:
        return err
    platform = request.args.get('platform', '') or None
    try:
        accounts = _mini_models().get_all(platform=platform)
        return _ok(_decorate(accounts))
    except ImportError:
        return _err('mini_app_builder plugin is not enabled', 503)
    except Exception as e:
        logger.exception('[IMGateway] miniapp-accounts list failed')
        return _err(str(e)[:2000], 500)


@miniapp_bp.route('/', methods=['POST'])
def create_account():
    """新增小程序开发者账户。"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    platform = data.get('platform', '')
    account_name = data.get('account_name', '')
    if platform not in PLATFORMS:
        return _err(f'Unsupported platform: {platform}', 400)
    if not account_name:
        return _err('account_name is required', 400)
    try:
        account_id = _mini_models().create(
            platform=platform,
            account_name=account_name,
            app_id=data.get('app_id', ''),
            app_secret=data.get('app_secret', ''),
            bot_token=data.get('bot_token', ''),
            channel_id=data.get('channel_id', ''),
            channel_secret=data.get('channel_secret', ''),
            access_token=data.get('access_token', ''),
            extra_config=data.get('extra_config', {}),
            is_active=data.get('is_active', 1),
        )
        return _ok({'id': account_id})
    except ImportError:
        return _err('mini_app_builder plugin is not enabled', 503)
    except Exception as e:
        logger.exception('[IMGateway] miniapp-accounts create failed')
        return _err(str(e)[:2000], 500)


@miniapp_bp.route('/<int:account_id>', methods=['PUT'])
def update_account(account_id):
    """编辑小程序开发者账户（敏感字段留空 = 保持原值）。"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    if not data:
        return _err('No data provided', 400)
    if 'platform' in data and data['platform'] not in PLATFORMS:
        return _err(f'Unsupported platform: {data["platform"]}', 400)
    try:
        ok = _mini_models().update(account_id, **data)
        if not ok:
            return _err('No valid fields to update', 400)
        return _ok({'updated': True})
    except ImportError:
        return _err('mini_app_builder plugin is not enabled', 503)
    except Exception as e:
        logger.exception('[IMGateway] miniapp-accounts update failed')
        return _err(str(e)[:2000], 500)


@miniapp_bp.route('/<int:account_id>', methods=['DELETE'])
def delete_account(account_id):
    """删除小程序开发者账户。"""
    admin, err = _require_admin()
    if err:
        return err
    try:
        _mini_models().delete(account_id)
        return _ok({'deleted': True})
    except ImportError:
        return _err('mini_app_builder plugin is not enabled', 503)
    except Exception as e:
        logger.exception('[IMGateway] miniapp-accounts delete failed')
        return _err(str(e)[:2000], 500)


@miniapp_bp.route('/<int:account_id>/test', methods=['POST'])
def test_account(account_id):
    """测试小程序开发者账户连接（telegram / line 支持真实 API 探测）。"""
    admin, err = _require_admin()
    if err:
        return err
    try:
        result = _mini_models().test_connection(account_id)
        return jsonify(result)
    except ImportError:
        return _err('mini_app_builder plugin is not enabled', 503)
    except Exception as e:
        logger.exception('[IMGateway] miniapp-accounts test failed')
        return _err(str(e)[:2000], 500)
