#!/usr/bin/env python3
"""
统一 API 响应契约（后端对齐阶段 1）
====================================
信封格式（全站统一）：
  成功: {"success": true,  "data": ..., "code": 0}
  失败: {"success": false, "error": "...", "code": <业务错误码>}

code=0 表示成功；业务错误码 ≥1000，按域分段（4xxx 客户端错误 / 5xxx 服务端错误）。
HTTP 状态码仅表示传输层语义，业务细节由 code 承载。
"""
from flask import jsonify, request

ERROR_CODES = {
    'PARAM_INVALID': 40001,
    'NOT_AUTHENTICATED': 40101,
    'FORBIDDEN': 40301,
    'NOT_FOUND': 40401,
    'CONFLICT': 40901,
    'RATE_LIMITED': 42901,
    'INTERNAL': 50001,
    'PAYMENT_NOT_CONFIGURED': 50002,
}

_DEFAULT_CODE_BY_HTTP = {
    400: ERROR_CODES['PARAM_INVALID'],
    401: ERROR_CODES['NOT_AUTHENTICATED'],
    403: ERROR_CODES['FORBIDDEN'],
    404: ERROR_CODES['NOT_FOUND'],
    409: ERROR_CODES['CONFLICT'],
    429: ERROR_CODES['RATE_LIMITED'],
    500: ERROR_CODES['INTERNAL'],
}


def api_ok(data=None, code=0):
    """成功响应：{success: True, data: ..., code: 0}。"""
    return jsonify({'success': True, 'data': data, 'code': code})


def api_err(msg, http_code=400, code=None, **extra):
    """失败响应：{success: False, error: msg, code: <业务码>} + HTTP 状态码。

    code 未显式指定时按 http_code 映射默认业务错误码；
    extra 字段合并进响应体（如校验明细）。
    """
    if code is None:
        code = _DEFAULT_CODE_BY_HTTP.get(http_code, ERROR_CODES['PARAM_INVALID'])
    body = {'success': False, 'error': msg, 'code': code}
    body.update(extra)
    return jsonify(body), http_code


def _parse_positive_int(name: str, default: int, lo: int = 1, hi: int = 100000) -> int:
    """解析有界正整数查询参数（从 plugin_manager/routes.py 抽取，统一放公共层）。

    缺省/空 → default；非整数 → 抛 ValueError；超出 [lo, hi] → 截断到边界。
    """
    raw = request.args.get(name, '')
    if raw == '' or raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f'参数 {name} 必须为整数')
    return max(lo, min(hi, v))
