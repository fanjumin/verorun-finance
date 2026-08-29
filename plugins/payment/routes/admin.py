#!/usr/bin/env python3
"""
Payment Plugin — /admin/payment/configs 配置管理路由
====================================================
支付凭证存储在插件独立 PostgreSQL schema `payment`（payment_configs 表），
完全独立于主库 system_config。
"""
from i18n import _
import os
import sys

from flask import Blueprint, request, jsonify

payment_admin_bp = Blueprint('payment_admin', __name__, url_prefix='/admin/payment')

# 支持的提供商及字段定义
PAYMENT_PROVIDERS = {
    'alipay': {
        'label': 'Alipay',
        'label_zh': _('Alipay'),
        'market': 'cn',
        'fields': [
            {'key': 'app_id', 'label': 'Alipay App ID', 'label_zh': _('Alipay App ID'), 'type': 'text'},
            {'key': 'private_key', 'label': 'Alipay Private Key', 'label_zh': _('Alipay Payment Private Key'), 'type': 'password'},
            {'key': 'public_key', 'label': 'Alipay Public Key', 'label_zh': _('Alipay Payment Public Key'), 'type': 'password'},
            {'key': 'notify_base', 'label': 'Payment Callback Domain', 'label_zh': _('Payment Callback Domain'), 'type': 'text'},
            {'key': 'sandbox', 'label': 'Sandbox Mode', 'label_zh': _('Sandbox Mode'), 'type': 'checkbox', 'hint': 'Use Alipay sandbox environment for testing', 'hint_zh': _('Test using Alipay sandbox environment')},
        ]
    },
    'wechat': {
        'label': 'WeChat Pay',
        'label_zh': _('WeChat Pay'),
        'market': 'cn',
        'fields': [
            {'key': 'app_id', 'label': 'WeChat Pay AppID', 'label_zh': _('WeChat Pay AppID'), 'type': 'text'},
            {'key': 'mchid', 'label': 'WeChat Merchant ID', 'label_zh': _('WeChat Pay Merchant ID'), 'type': 'text'},
            {'key': 'api_v3_key', 'label': 'WeChat Pay API V3 Key', 'label_zh': _('WeChat Pay API V3 Key'), 'type': 'password'},
            {'key': 'cert_serial', 'label': 'WeChat Cert Serial Number', 'label_zh': _('WeChat Pay Certificate Serial Number'), 'type': 'text'},
            {'key': 'plan_id', 'label': 'WeChat Pay Plan ID', 'label_zh': _('WeChat Pay Deduction Plan ID'), 'type': 'text'},
        ]
    },
    'stripe': {
        'label': 'Stripe',
        'label_zh': 'Stripe',
        'market': 'intl',
        'fields': [
            {'key': 'secret_key', 'label': 'Secret Key', 'label_zh': 'Secret Key', 'type': 'password'},
            {'key': 'publishable_key', 'label': 'Publishable Key', 'label_zh': 'Publishable Key', 'type': 'text'},
            {'key': 'webhook_secret', 'label': 'Webhook Secret', 'label_zh': 'Webhook Secret', 'type': 'password'},
        ]
    },
    'paypal': {
        'label': 'PayPal',
        'label_zh': 'PayPal',
        'market': 'intl',
        'fields': [
            {'key': 'client_id', 'label': 'Client ID', 'label_zh': 'Client ID', 'type': 'text'},
            {'key': 'client_secret', 'label': 'Client Secret', 'label_zh': 'Client Secret', 'type': 'password'},
            {'key': 'webhook_id', 'label': 'Webhook ID', 'label_zh': 'Webhook ID', 'type': 'text'},
            {'key': 'mode', 'label': 'Mode', 'label_zh': _('Mode'), 'type': 'select', 'options': [{'value': 'sandbox', 'label': 'Sandbox (Test)', 'label_zh': _('Sandbox (Test)')}, {'value': 'live', 'label': 'Live (Production)', 'label_zh': _('Production Environment')}], 'default': 'sandbox'},
        ]
    },
    'adyen': {
        'label': 'Adyen',
        'label_zh': 'Adyen',
        'market': 'intl',
        'fields': [
            {'key': 'api_key', 'label': 'API Key', 'label_zh': 'API Key', 'type': 'password'},
            {'key': 'hmac_key', 'label': 'Webhook HMAC Key', 'label_zh': 'Webhook HMAC Key', 'type': 'password'},
            {'key': 'merchant_account', 'label': 'Merchant Account', 'label_zh': 'Merchant Account', 'type': 'text'},
            {'key': 'client_key', 'label': 'Client Key', 'label_zh': 'Client Key', 'type': 'text'},
            {'key': 'currency', 'label': 'Currency', 'label_zh': 'Currency', 'type': 'text', 'default': 'USD'},
            {'key': 'environment', 'label': 'Environment', 'label_zh': 'Environment', 'type': 'select', 'options': [{'value': 'sandbox', 'label': 'Sandbox (Test)', 'label_zh': 'Sandbox (Test)'}, {'value': 'live', 'label': 'Live (Production)', 'label_zh': 'Live (Production)'}], 'default': 'sandbox'},
        ]
    },
    'checkoutcom': {
        'label': 'Checkout.com',
        'label_zh': 'Checkout.com',
        'market': 'intl',
        'fields': [
            {'key': 'secret_key', 'label': 'Secret Key', 'label_zh': 'Secret Key', 'type': 'password'},
            {'key': 'public_key', 'label': 'Public Key', 'label_zh': 'Public Key', 'type': 'text'},
            {'key': 'webhook_key', 'label': 'Webhook Signing Key', 'label_zh': 'Webhook Signing Key', 'type': 'password'},
            {'key': 'processing_channel_id', 'label': 'Processing Channel ID', 'label_zh': 'Processing Channel ID', 'type': 'text'},
            {'key': 'currency', 'label': 'Currency', 'label_zh': 'Currency', 'type': 'text', 'default': 'USD'},
            {'key': 'environment', 'label': 'Environment', 'label_zh': 'Environment', 'type': 'select', 'options': [{'value': 'sandbox', 'label': 'Sandbox (Test)', 'label_zh': 'Sandbox (Test)'}, {'value': 'live', 'label': 'Live (Production)', 'label_zh': 'Live (Production)'}], 'default': 'sandbox'},
        ]
    },
    'airwallex': {
        'label': 'Airwallex',
        'label_zh': 'Airwallex',
        'market': 'cn',
        'fields': [
            {'key': 'client_id', 'label': 'Client ID', 'label_zh': 'Client ID', 'type': 'text'},
            {'key': 'api_key', 'label': 'API Key', 'label_zh': 'API Key', 'type': 'password'},
            {'key': 'webhook_secret', 'label': 'Webhook Secret', 'label_zh': 'Webhook Secret', 'type': 'password'},
            {'key': 'currency', 'label': 'Currency', 'label_zh': 'Currency', 'type': 'text', 'default': 'USD'},
            {'key': 'environment', 'label': 'Environment', 'label_zh': 'Environment', 'type': 'select', 'options': [{'value': 'sandbox', 'label': 'Sandbox (Test)', 'label_zh': 'Sandbox (Test)'}, {'value': 'live', 'label': 'Live (Production)', 'label_zh': 'Live (Production)'}], 'default': 'sandbox'},
        ]
    },
    'lianlian': {
        'label': 'LianLian Global',
        'label_zh': 'LianLian Global',
        'market': 'cn',
        'fields': [
            {'key': 'merchant_id', 'label': 'Merchant ID', 'label_zh': 'Merchant ID', 'type': 'text'},
            {'key': 'sub_merchant_id', 'label': 'Sub Merchant ID', 'label_zh': 'Sub Merchant ID', 'type': 'text'},
            {'key': 'private_key', 'label': 'Merchant Private Key', 'label_zh': 'Merchant Private Key', 'type': 'password'},
            {'key': 'public_key', 'label': 'LianLian Public Key', 'label_zh': 'LianLian Public Key', 'type': 'password'},
            {'key': 'country', 'label': 'Merchant Country', 'label_zh': 'Merchant Country', 'type': 'text', 'default': 'US'},
            {'key': 'currency', 'label': 'Currency', 'label_zh': 'Currency', 'type': 'text', 'default': 'USD'},
            {'key': 'environment', 'label': 'Environment', 'label_zh': 'Environment', 'type': 'select', 'options': [{'value': 'sandbox', 'label': 'Sandbox (Test)', 'label_zh': 'Sandbox (Test)'}, {'value': 'live', 'label': 'Live (Production)', 'label_zh': 'Live (Production)'}], 'default': 'sandbox'},
        ]
    }
}


def _require_admin():
    from routes.admin import _require_admin as _ra
    return _ra()


def _get_db():
    from plugins.payment.models import get_payment_db
    return get_payment_db()


def _get_provider_configs(provider):
    from plugins.payment.models import get_provider_configs
    return get_provider_configs(provider)


def _set_provider_configs(provider, configs):
    from plugins.payment.models import set_provider_configs
    set_provider_configs(provider, configs)


def _log(admin_id, action, target_type='', target_id='', detail=''):
    from routes.admin import _log as _l
    _l(admin_id, action, target_type, target_id, detail)


# ── API: 获取提供商定义 ──

@payment_admin_bp.route('/providers', methods=['GET'])
def get_providers():
    """返回所有支持的提供商定义（含字段结构）"""
    a, e = _require_admin()
    if e:
        return e
    market = request.args.get('market', 'all')
    result = {}
    for k, v in PAYMENT_PROVIDERS.items():
        if market == 'all' or v['market'] == market:
            result[k] = v
    return jsonify({'success': True, 'data': result})


# ── API: 获取全部配置概览 ──

@payment_admin_bp.route('/configs', methods=['GET'])
def get_all_configs():
    """获取所有支付提供商当前配置"""
    try:
        a, e = _require_admin()
        if e:
            return e
        from plugins.payment.models import get_all_providers_summary
        summary = get_all_providers_summary()
        return jsonify({'success': True, 'data': summary})
    except Exception as ex:
        import traceback
        tb = traceback.format_exc()
        print(f'[PaymentPlugin] ERROR /admin/payment/configs: {ex}\n{tb}')
        return jsonify({'success': False, 'error': repr(ex)})


# ── API: 获取单个提供商配置 ──

@payment_admin_bp.route('/configs/<provider>', methods=['GET'])
def get_provider_config(provider):
    """获取指定提供商的配置"""
    a, e = _require_admin()
    if e:
        return e
    if provider not in PAYMENT_PROVIDERS:
        return jsonify({'success': False, 'error': f'Unknown provider: {provider}'}), 400
    configs = _get_provider_configs(provider)
    return jsonify({'success': True, 'data': {provider: configs}})


# ── API: 保存提供商配置 ──

@payment_admin_bp.route('/configs/<provider>', methods=['PUT'])
def save_provider_config(provider):
    """保存指定提供商的配置"""
    a, e = _require_admin()
    if e:
        return e
    if provider not in PAYMENT_PROVIDERS:
        return jsonify({'success': False, 'error': f'Unknown provider: {provider}'}), 400

    body = request.get_json(silent=True) or {}
    configs = body.get('configs', {})

    _set_provider_configs(provider, configs)
    _log(a, 'update', 'payment_config', provider, f'Updated {provider} payment config ({len(configs)} fields)')

    return jsonify({'success': True, 'message': f'{provider} config saved'})


# ── API: 删除提供商配置 ──

@payment_admin_bp.route('/configs/<provider>', methods=['DELETE'])
def delete_provider_config(provider):
    """清空指定提供商的配置"""
    a, e = _require_admin()
    if e:
        return e
    if provider not in PAYMENT_PROVIDERS:
        return jsonify({'success': False, 'error': f'Unknown provider: {provider}'}), 400

    with _get_db() as conn:
        conn.execute('DELETE FROM payment_configs WHERE provider=%s', (provider,))
        conn.commit()
    _log(a, 'delete', 'payment_config', provider, f'Cleared {provider} payment config')
    return jsonify({'success': True, 'message': f'{provider} config cleared'})


# ── API: 检查是否已配置（供支付网关调用） ──

@payment_admin_bp.route('/check/<provider>', methods=['GET'])
def check_provider_configured(provider):
    """检查提供商是否已配置完整"""
    configs = _get_provider_configs(provider)
    if provider not in PAYMENT_PROVIDERS:
        return jsonify({'success': False, 'error': f'Unknown provider: {provider}'}), 400
    required = [f['key'] for f in PAYMENT_PROVIDERS[provider]['fields'] if f['type'] == 'password']
    missing = [k for k in required if not configs.get(k)]
    return jsonify({
        'success': True,
        'data': {
            'provider': provider,
            'configured': len(missing) == 0,
            'configured_fields': list(configs.keys()),
            'missing_required': missing
        }
    })
