#!/usr/bin/env python3
"""net_proxy — 管理端 API 路由（P5）。

方案依据：
    方案 §7.10（routes.py 模块设计）
    方案 §8   （11 个端点 + 响应契约）
    方案 §9.1 （信任边界：通道 endpoint 允许私网，业务目标走 §9.2 黑名单）

挂载方式（实证）：
    本 Blueprint **不设** url_prefix，由 plugin_manager 的
    `_get_route_prefix()`（manager.py L2661-2666）在无自定义前缀时
    补 `/plugin/<identifier>`，因此实际路径 = `/plugin/net_proxy/admin/...`
    —— 与方案 §8 完全一致。切勿在此硬写 url_prefix，否则会被原样采用。

响应契约（对齐本仓库既有插件，非 V3 内核的 {ok,data,error,meta}）：
    成功: {'success': True, 'data': ...}
    失败: {'success': False, 'error': <文案>} [+ HTTP 状态码]
    未登录: {'success': False, 'error': _t('Unauthorized')}, 401
"""

import os
import sys

from flask import Blueprint, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(BASE_DIR, '..', '..'))

try:
    from plugin_manager.logger import get_plugin_logger
    _logger = get_plugin_logger('net_proxy')
except ImportError:  # pragma: no cover - 独立运行兜底
    import logging
    _logger = logging.getLogger('net_proxy')

from . import channels as ch
from . import crypto
from . import egress as eg
from . import fuse
from . import models as m
from . import region_profile as rp
from . import rules as R

# ── i18n：插件自带 i18n/ 目录，由 i18n.seed_plugin_translations() 播种进
#    i18n_strings 表；`_(text)` 走 DB → YAML → 原文三级回退。若无 i18n
#    依赖（独立测试）则退化为恒等函数。
try:
    from i18n import _ as _t
except ImportError:  # pragma: no cover
    _t = lambda s: s  # noqa: E731

net_proxy_bp = Blueprint('net_proxy', __name__)

# 日志查询一次最多返回的行数（§8：limit <= 200）
_MAX_LOG_LIMIT = 200
_DEFAULT_LOG_LIMIT = 50


# ─────────────── 鉴权 ───────────────

def _require_admin():
    """复用主系统管理端 JWT 鉴权。

    与 health_check/routes.py::_require_admin 同款：Authorization Bearer
    优先，回退 sso_token Cookie / X-Token 头；要求 payload.is_admin。
    返回 payload（dict）表示通过，None 表示未登录/非管理员。
    """
    try:
        from services.jwt_service import validate_token
    except ImportError:
        # admin 服务上下文外（如独立单测）视为未鉴权，绝不 fail-open。
        return None
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.cookies.get('sso_token') or request.headers.get('X-Token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return None
    return payload


def _unauthorized():
    return jsonify({'success': False, 'error': _t('Unauthorized')}), 401


def _plugin_instance():
    """取当前插件实例（可能为 None —— 插件被禁用时）。"""
    try:
        from flask import current_app
        pm = current_app.extensions.get('plugin_manager')
        if pm and pm.is_enabled('net_proxy'):
            return pm.get_instance('net_proxy')
    except Exception:
        pass
    return None


# ─────────────── 参数小工具 ───────────────

def _int_arg(name, default, minimum=None, maximum=None):
    """从 JSON body 或 query string 取整数，非法即抛 ValueError。"""
    raw = None
    if request.is_json:
        body = request.get_json(silent=True) or {}
        raw = body.get(name)
    if raw is None:
        raw = request.args.get(name)
    if raw is None or raw == '':
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ValueError('%s must be an integer' % name)
    if minimum is not None and val < minimum:
        raise ValueError('%s must be >= %s' % (name, minimum))
    if maximum is not None and val > maximum:
        raise ValueError('%s must be <= %s' % (name, maximum))
    return val


def _bool_arg(name, default=False):
    raw = None
    if request.is_json:
        body = request.get_json(silent=True) or {}
        raw = body.get(name)
    if raw is None:
        raw = request.args.get(name)
    if raw is None or raw == '':
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def _json_body():
    """取 JSON body，非 JSON 返回空 dict（调用方各自校验必填）。"""
    if not request.is_json:
        return {}
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _validate_error(exc):
    """校验类异常的统一响应（400）。"""
    return jsonify({'success': False, 'error': str(exc)}), 400


def _conflict_error(exc):
    """唯一约束等冲突的统一响应（409）。"""
    return jsonify({'success': False, 'error': str(exc)}), 409


def _credential_error(exc):
    """凭据加解密不可用 —— 服务端配置问题，返回 503 而非 400。

    语义依据：`crypto.CredentialError` 的触发条件是 ENCRYPTION_KEY 缺失/过短、
    cryptography 未安装、或解密失败（密钥轮换）。**这些都不是客户端入参错误**，
    返回 400 会让运维误判为「用户填错了」，把排查方向带偏。

    `Retry-After: 0` 明确告知调用方这是可恢复的配置问题（配好密钥即可重试），
    而非永久性失败。fail-closed 行为不变：密钥不可用时**绝不明文落库**。
    """
    return jsonify({
        'success': False,
        'error': str(exc),
        'hint': 'ENCRYPTION_KEY unavailable — server-side configuration issue',
    }), 503, {'Retry-After': '0'}


# ═══════════════ GET /admin/status ═══════════════

@net_proxy_bp.route('/admin/status')
def api_status():
    """出口治理总览：开关、通道健康、今日请求统计、熔断数。"""
    if not _require_admin():
        return _unauthorized()

    try:
        stats = m.request_stats()
    except Exception as e:
        _logger.error('net_proxy status: request_stats 失败: %s', e)
        stats = {}

    try:
        channels = m.list_channels(enabled_only=False)
        healthy = m.pick_healthy_channels()
        healthy_ids = {row['id'] for row in healthy}
        channel_total = len(channels)
        channel_enabled = sum(1 for c in channels if c.get('enabled'))
        healthy_total = len(healthy_ids)
    except Exception as e:
        _logger.error('net_proxy status: 通道统计失败: %s', e)
        channel_total = channel_enabled = healthy_total = 0

    try:
        fused_total = m.fused_channel_count()
    except Exception as e:
        _logger.error('net_proxy status: 熔断统计失败: %s', e)
        fused_total = 0

    data = {
        'proxy_enabled': R.is_proxy_enabled(),
        'current_region': rp.current_profile(),
        'default_policy': R._config_default_policy(),
        'crypto_available': crypto.crypto_available(),
        'channels_total': channel_total,
        'channels_enabled': channel_enabled,
        'channels_healthy': healthy_total,
        'channels_fused': fused_total,
        'direct_ratio_24h': stats.get('direct_ratio', 0.0),
        'success_rate_24h': stats.get('success_rate', 100.0),
        'avg_latency_ms_24h': stats.get('avg_latency_ms'),
        'blocked_networks': len(eg.BLOCKED_NETWORKS),
    }
    return jsonify({'success': True, 'data': data})


# ═══════════════ channels ═══════════════

@net_proxy_bp.route('/admin/channels', methods=['GET'])
def api_list_channels():
    """通道列表（密文不下发，密码经 mask_channel 脱敏）。"""
    if not _require_admin():
        return _unauthorized()
    enabled_only = _bool_arg('enabled_only', False)
    try:
        rows = m.list_channels(enabled_only=enabled_only)
    except Exception as e:
        _logger.error('net_proxy channels list 失败: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({
        'success': True,
        'data': {'channels': [ch.to_public(row) for row in rows], 'total': len(rows)},
    })


@net_proxy_bp.route('/admin/channels', methods=['POST'])
def api_create_channel():
    """新建出口通道。密码在落库前加密（fail-closed）。"""
    if not _require_admin():
        return _unauthorized()
    body = _json_body()
    try:
        fields = ch.prepare_create_fields(body)
    except ch.ChannelValidationError as e:
        return _validate_error(e)
    except crypto.CredentialError as e:
        # 服务端配置问题（ENCRYPTION_KEY 不可用），非入参错误 → 503
        _logger.error('net_proxy: 凭据加密不可用，拒绝落库: %s', e)
        return _credential_error(e)

    try:
        channel_id = m.create_channel(
            name=fields.pop('name'),
            protocol=fields.pop('protocol'),
            host=fields.pop('host'),
            port=fields.pop('port'),
            **fields
        )
    except Exception as e:
        _logger.error('net_proxy channel create 失败: %s', e)
        return _conflict_error(e)

    _logger.info('net_proxy: admin 新建通道 id=%s', channel_id)
    return jsonify({'success': True, 'data': {'id': channel_id}})


@net_proxy_bp.route('/admin/channels/<int:channel_id>', methods=['PUT'])
def api_update_channel(channel_id):
    """更新通道。未提交 auth_password 时密文保持不变（局部更新）。"""
    if not _require_admin():
        return _unauthorized()
    if not m.get_channel(channel_id):
        return jsonify({'success': False, 'error': _t('Channel not found')}), 404

    body = _json_body()
    try:
        fields = ch.prepare_update_fields(body)
    except ch.ChannelValidationError as e:
        return _validate_error(e)
    except crypto.CredentialError as e:
        # 服务端配置问题（ENCRYPTION_KEY 不可用），非入参错误 → 503
        _logger.error('net_proxy: 凭据加密不可用，拒绝更新: %s', e)
        return _credential_error(e)

    if not fields:
        return jsonify({'success': False, 'error': _t('No fields to update')}), 400

    try:
        m.update_channel(channel_id, **fields)
    except Exception as e:
        _logger.error('net_proxy channel update id=%s 失败: %s', channel_id, e)
        return _conflict_error(e)

    _logger.info('net_proxy: admin 更新通道 id=%s 字段=%s', channel_id, sorted(fields))
    return jsonify({'success': True, 'message': _t('Updated')})


@net_proxy_bp.route('/admin/channels/<int:channel_id>', methods=['DELETE'])
def api_delete_channel(channel_id):
    """删除通道。

    注意：`proxy_probe_log.channel_id` 无外键（见方案 §6.0），删除通道会
    留下探测日志孤儿行 —— 已在方案评审中报告，修复方式待拍板，本阶段
    不擅自改 DDL / 不擅自加清理逻辑。
    """
    if not _require_admin():
        return _unauthorized()
    if not m.get_channel(channel_id):
        return jsonify({'success': False, 'error': _t('Channel not found')}), 404
    try:
        m.delete_channel(channel_id)
    except Exception as e:
        _logger.error('net_proxy channel delete id=%s 失败: %s', channel_id, e)
        return jsonify({'success': False, 'error': str(e)}), 500
    _logger.info('net_proxy: admin 删除通道 id=%s', channel_id)
    return jsonify({'success': True, 'message': _t('Deleted')})


@net_proxy_bp.route('/admin/channels/<int:channel_id>/probe', methods=['POST'])
def api_probe_channel(channel_id):
    """单个通道连通性探测。

    probe_target 取 body.probe_target → 配置 probe_target → ''。
    为空时探测被跳过（不落日志、不污染健康分），与 §7.6 一致。
    """
    if not _require_admin():
        return _unauthorized()
    row = m.get_channel(channel_id)
    if not row:
        return jsonify({'success': False, 'error': _t('Channel not found')}), 404

    body = _json_body()
    target = (body.get('probe_target') or '').strip()
    if not target:
        plugin = _plugin_instance()
        if plugin is not None:
            target = (plugin.get_config_value('probe_target', '') or '').strip()

    probe_row = m.list_channels_for_probe()
    probe_row = next((r for r in probe_row if r['id'] == channel_id), None)
    if probe_row is None:
        return jsonify({'success': False, 'error': _t('Channel not found')}), 404

    try:
        result = fuse.probe_channel(
            dict(probe_row), probe_target=target, plugin=_plugin_instance()
        )
    except Exception as e:
        _logger.error('net_proxy probe id=%s 失败: %s', channel_id, e)
        return jsonify({'success': False, 'error': str(e)}), 500

    if result.get('skipped'):
        return jsonify({
            'success': True,
            'data': dict(result, hint=_t('Probe target not configured, probe skipped')),
        })
    return jsonify({'success': True, 'data': dict(result)})


# ═══════════════ rules ═══════════════

@net_proxy_bp.route('/admin/rules', methods=['GET'])
def api_list_rules():
    """规则列表（按 priority 升序）。"""
    if not _require_admin():
        return _unauthorized()
    enabled_only = _bool_arg('enabled_only', False)
    try:
        rows = m.list_rules(enabled_only=enabled_only)
    except Exception as e:
        _logger.error('net_proxy rules list 失败: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500

    items = []
    for row in rows:
        item = dict(row)
        for key in ('created_at', 'updated_at'):
            if hasattr(item.get(key), 'isoformat'):
                item[key] = item[key].isoformat()
        item['profile_tags'] = ch.parse_tags(item.get('profile_tags'))
        items.append(item)
    return jsonify({'success': True, 'data': {'rules': items, 'total': len(items)}})


@net_proxy_bp.route('/admin/rules', methods=['POST'])
def api_create_rule():
    """新建路由规则。

    CHANNEL 动作引用的通道必须真实存在 —— 保存即校验，避免运行时才发现
    规则永远降级（见 §7.4 与 P3 实测结论）。
    """
    if not _require_admin():
        return _unauthorized()
    body = _json_body()
    try:
        valid_ids = {row['id'] for row in m.list_channels(enabled_only=False)}
    except Exception as e:
        _logger.error('net_proxy rule create 取通道集失败: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500

    try:
        fields = R.prepare_create_rule_fields(body, valid_channel_ids=valid_ids)
    except R.RuleValidationError as e:
        return _validate_error(e)

    try:
        rule_id = m.create_rule(
            priority=fields.pop('priority'),
            target_pattern=fields.pop('target_pattern'),
            action=fields.pop('action'),
            **fields
        )
    except Exception as e:
        _logger.error('net_proxy rule create 失败: %s', e)
        return _conflict_error(e)

    _logger.info('net_proxy: admin 新建规则 id=%s', rule_id)
    return jsonify({'success': True, 'data': {'id': rule_id}})


@net_proxy_bp.route('/admin/rules/<int:rule_id>', methods=['PUT'])
def api_update_rule(rule_id):
    """更新规则。"""
    if not _require_admin():
        return _unauthorized()
    if not m.get_rule(rule_id):
        return jsonify({'success': False, 'error': _t('Rule not found')}), 404

    body = _json_body()
    try:
        valid_ids = {row['id'] for row in m.list_channels(enabled_only=False)}
    except Exception as e:
        _logger.error('net_proxy rule update 取通道集失败: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500

    try:
        fields = R.prepare_update_rule_fields(body, valid_channel_ids=valid_ids)
    except R.RuleValidationError as e:
        return _validate_error(e)

    if not fields:
        return jsonify({'success': False, 'error': _t('No fields to update')}), 400

    try:
        m.update_rule(rule_id, **fields)
    except Exception as e:
        _logger.error('net_proxy rule update id=%s 失败: %s', rule_id, e)
        return _conflict_error(e)

    _logger.info('net_proxy: admin 更新规则 id=%s 字段=%s', rule_id, sorted(fields))
    return jsonify({'success': True, 'message': _t('Updated')})


@net_proxy_bp.route('/admin/rules/<int:rule_id>', methods=['DELETE'])
def api_delete_rule(rule_id):
    """删除规则。"""
    if not _require_admin():
        return _unauthorized()
    if not m.get_rule(rule_id):
        return jsonify({'success': False, 'error': _t('Rule not found')}), 404
    try:
        m.delete_rule(rule_id)
    except Exception as e:
        _logger.error('net_proxy rule delete id=%s 失败: %s', rule_id, e)
        return jsonify({'success': False, 'error': str(e)}), 500
    _logger.info('net_proxy: admin 删除规则 id=%s', rule_id)
    return jsonify({'success': True, 'message': _t('Deleted')})


# ═══════════════ logs ═══════════════

@net_proxy_bp.route('/admin/logs')
def api_logs():
    """出站请求日志 / 探测日志（kind=request|probe，limit<=200）。"""
    if not _require_admin():
        return _unauthorized()

    kind = (request.args.get('kind') or 'request').strip().lower()
    if kind not in ('request', 'probe'):
        return jsonify({
            'success': False,
            'error': _t('kind must be request or probe'),
        }), 400

    try:
        limit = _int_arg('limit', _DEFAULT_LOG_LIMIT, minimum=1, maximum=_MAX_LOG_LIMIT)
        offset = _int_arg('offset', 0, minimum=0)
    except ValueError as e:
        return _validate_error(e)

    channel_id = request.args.get('channel_id')
    caller = (request.args.get('caller') or '').strip()

    try:
        if kind == 'request':
            rows = m.list_request_log(
                limit=limit, offset=offset,
                caller=caller or None,
                channel_id=int(channel_id) if channel_id else None,
            )
        else:
            rows = m.list_probe_log(
                limit=limit, offset=offset,
                channel_id=int(channel_id) if channel_id else None,
            )
    except ValueError:
        return jsonify({'success': False, 'error': _t('channel_id must be an integer')}), 400
    except Exception as e:
        _logger.error('net_proxy logs kind=%s 失败: %s', kind, e)
        return jsonify({'success': False, 'error': str(e)}), 500

    items = []
    for row in rows:
        item = dict(row)
        created = item.get('created_at')
        if hasattr(created, 'isoformat'):
            item['created_at'] = created.isoformat()
        items.append(item)

    return jsonify({
        'success': True,
        'data': {
            'kind': kind,
            'limit': limit,
            'offset': offset,
            'logs': items,
        }
    })
