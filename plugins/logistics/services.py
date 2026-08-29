#!/usr/bin/env python3
"""
Logistics Plugin Services — 物流查询核心逻辑
==============================================
封装快递鸟 Kdniao API 客户端，从插件配置读取。
"""
from i18n import _
import hashlib
import json
import base64
import os
from typing import Dict, Any, List, Tuple
from urllib import request, parse

from .models import get_logistics_db
from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('logistics')

# ── 默认配置（环境变量兜底）──
KDNIAO_API_URL = os.environ.get('KDNIAO_API_URL',
    'https://api.kdniao.com/api/dist')


def _get_kdniao_config() -> Tuple[str, str]:
    """读取快递鸟配置：优先环境变量，其次插件配置（完全独立）"""
    eid = os.environ.get('KDNIAO_EBUSINESS_ID', '').strip()
    api_key = os.environ.get('KDNIAO_API_KEY', '').strip()
    if eid and api_key:
        return eid, api_key

    try:
        import flask
        pm = flask.current_app.extensions.get('plugin_manager')
        plugin = pm.get_instance('logistics') if (pm and pm.is_enabled('logistics')) else None
        if plugin:
            peid, pkey = plugin.get_kdniao_config()
            eid = eid or peid
            api_key = api_key or pkey
    except Exception:
        pass
    return eid, api_key


def _generate_data_sign(request_data: str, api_key: str) -> str:
    raw = request_data + api_key
    md5 = hashlib.md5(raw.encode('utf-8')).hexdigest()
    return base64.b64encode(md5.encode('utf-8')).decode('utf-8')


def query_track(shipper_code: str, logistic_code: str,
                order_code: str = '', customer_name: str = '',
                eid: str = '', api_key: str = '') -> Tuple[bool, Dict[str, Any], str]:
    """查询物流轨迹（快递鸟在途监控即时查询, RequestType=8001）"""
    if not eid or not api_key:
        eid, api_key = _get_kdniao_config()
    if not eid or not api_key:
        _log_query(shipper_code, logistic_code, order_code, False, _('Kuaidi100 Not Configured'))
        return False, {}, _('快递鸟未配置: 请在系统设置→基本设置→物流配送中填写商户ID和API Key')

    req_body = {
        'OrderCode': order_code,
        'ShipperCode': shipper_code,
        'LogisticCode': logistic_code,
    }
    if customer_name:
        req_body['CustomerName'] = customer_name

    request_data = json.dumps(req_body, ensure_ascii=False, separators=(',', ':'))
    data_sign = _generate_data_sign(request_data, api_key)

    post_data = parse.urlencode({
        'RequestType': '8001',
        'EBusinessID': eid,
        'RequestData': request_data,
        'DataSign': data_sign,
        'DataType': '2',
    }).encode('utf-8')

    try:
        req = request.Request(KDNIAO_API_URL, data=post_data, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded;charset=utf-8')
        with request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode('utf-8')
            result = json.loads(body)
            if result.get('Success'):
                traces = result.get('Traces', [])
                formatted = [{'time': t.get('AcceptTime', ''), 'station': t.get('AcceptStation', ''), 'remark': t.get('Remark', '')} for t in traces]
                formatted.sort(key=lambda x: x['time'], reverse=True)
                data = {
                    'shipper_code': shipper_code,
                    'logistic_code': logistic_code,
                    'traces': formatted,
                    'state': int(result.get('State', 0)),
                    'state_text': _state_text(str(result.get('State', '0'))),
                    'state_ex': result.get('StateEx', ''),
                    'location': result.get('Location', ''),
                }
                _log_query(shipper_code, logistic_code, order_code, True)
                return True, data, ''
            else:
                reason = result.get('Reason', _('Query Failed'))
                _log_query(shipper_code, logistic_code, order_code, False, reason)
                return False, {}, f'Kuaidi100 Query Failed: {reason}'
    except json.JSONDecodeError as e:
        _log_query(shipper_code, logistic_code, order_code, False, str(e))
        return False, {}, f'Failed to parse response: {e}'
    except Exception as e:
        _log_query(shipper_code, logistic_code, order_code, False, str(e))
        return False, {}, _('网络请求失败: {}').format(e)


def _state_text(state: str) -> str:
    return {
        '0': _('No Trajectory'), '1': _('Collected'), '2': _('On the way'), '3': _('Sign for Receipt'),
        '4': _('Issue Item'), '5': _('Forward'), '6': _('Cancel sign-up'), '7': _('Pending customs clearance'),
        '8': _('In Customs Clearance'), '9': _('Rejected'), '10': _('Pending delivery'), '11': _('Delivered'), '14': _('Returning'),
    }.get(state, f'Unknown ({state})')


def get_shipping_status_text(shipping_status: str) -> str:
    return {
        'pending': _('Pending shipment'),
        'shipped': _('Shipped'),
        'delivered': _('Signed'),
    }.get(shipping_status, shipping_status)


def _log_query(shipper_code, logistic_code, order_code, success, error_msg=''):
    try:
        with get_logistics_db() as conn:
            conn.execute(
                'INSERT INTO logistics_queries (shipper_code, logistic_code, order_code, success, error_msg) VALUES (%s,%s,%s,%s,%s)',
                (shipper_code, logistic_code, order_code, 1 if success else 0, error_msg)
            )
            conn.commit()
    except Exception as e:
        logger.error(f'Log write failed: {e}')
