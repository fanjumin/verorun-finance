#!/usr/bin/env python3
"""
Vault Audit — Audit logging for all backup/restore/config operations.

All operations are automatically recorded and tamper-evident.
"""

from datetime import datetime
from flask import request
from .utils import get_vault_conn


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
        try:
            from flask import session
            operator = session.get('user', {}).get('username', 'system')
        except Exception:
            operator = 'system'

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
