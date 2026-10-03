#!/usr/bin/env python3
"""
Vault Audit — Audit logging for all backup/restore/config operations.

All operations are automatically recorded and tamper-evident.
"""

from datetime import datetime
from flask import request, session, has_request_context
from .utils import get_vault_conn


# JWT payload 中可作为操作者名的 claim，按平台既有口径依次尝试
# （与 plugin_manager/routes.py 的用户名解析保持一致；vault 自己的
#  access token 通常只带 user_id/phone，username 不一定存在）。
_OPERATOR_CLAIMS = ('username', 'display_name', 'phone', 'user_id')


def _name_from_vault_user(payload) -> str:
    """从鉴权装饰器注入的 request.vault_user 解析操作者名；取不到返回 ''。"""
    if not isinstance(payload, dict):
        return ''
    for claim in _OPERATOR_CLAIMS:
        val = payload.get(claim)
        if val not in (None, ''):
            return str(val)
    return ''


def current_operator() -> str:
    """Resolve the acting operator within a request (SD-10).

    Order: JWT identity injected as request.vault_user -> Flask session
    user -> 'system'. Safe outside a request context (service-layer calls
    and the scheduled job) where it always returns 'system'.
    """
    if not has_request_context():
        return 'system'
    try:
        name = _name_from_vault_user(getattr(request, 'vault_user', None))
        if name:
            return name
    except Exception:
        pass
    try:
        return session.get('user', {}).get('username') or 'system'
    except Exception:
        return 'system'


def log_audit(action: str, resource_type: str, resource_id: str,
              details: dict = None, operator: str = None) -> int:
    """
    Record an audit log entry.

    Args:
        action: operation type (backup.create, backup.delete, restore.execute, config.update, etc.)
        resource_type: resource type (backup, schedule, storage, config)
        resource_id: resource identifier
        details: operation details as dict
        operator: operator identifier, inferred from request context if not provided

    Returns:
        New record ID
    """
    import json as _json

    if operator is None:
        operator = current_operator()

    ip_address = None
    try:
        ip_address = request.remote_addr
    except Exception:
        pass

    conn = get_vault_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO vault_audit_log (action, resource_type, resource_id,
                                      operator, ip_address, details)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        action, resource_type, resource_id,
        operator, ip_address,
        _json.dumps(details) if details else None,
    ))
    row_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return row_id


def get_audit_logs(action: str = None, resource_type: str = None,
                   operator: str = None, limit: int = 100,
                   offset: int = 0) -> list:
    """
    Query audit logs with optional filtering.

    Args:
        action: filter by action type
        resource_type: filter by resource type
        operator: filter by operator
        limit: max records to return
        offset: pagination offset

    Returns:
        List of audit log dicts
    """
    conn = get_vault_conn()
    cur = conn.cursor()

    conditions = []
    params = []
    if action:
        conditions.append("action = %s")
        params.append(action)
    if resource_type:
        conditions.append("resource_type = %s")
        params.append(resource_type)
    if operator:
        conditions.append("operator = %s")
        params.append(operator)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    query = f"""
        SELECT id, action, resource_type, resource_id, operator,
               ip_address, details, created_at
        FROM vault_audit_log
        {where}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    cols = ['id', 'action', 'resource_type', 'resource_id', 'operator',
            'ip_address', 'details', 'created_at']
    return [dict(zip(cols, row)) for row in rows]
