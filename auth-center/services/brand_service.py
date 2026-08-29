#!/usr/bin/env python3
"""Brand settings service — shared across all 4 services for global brand config."""

import os, functools
import psycopg2
import psycopg2.extras

# 项目版本号（从 VERSION 文件读取）
_VERSION_CACHE = None
def _get_project_version():
    global _VERSION_CACHE
    if _VERSION_CACHE:
        return _VERSION_CACHE
    # 从当前文件向上查找 VERSION 文件
    base = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        candidate = os.path.join(base, 'VERSION')
        if os.path.exists(candidate):
            with open(candidate, 'r') as f:
                _VERSION_CACHE = f.read().strip()
            return _VERSION_CACHE
        base = os.path.dirname(base)
    _VERSION_CACHE = '0.9.5'
    return _VERSION_CACHE

_PG_CONFIG = None

def _get_pg_config():
    """Resolve PostgreSQL connection config."""
    global _PG_CONFIG
    if _PG_CONFIG:
        return _PG_CONFIG
    _PG_CONFIG = {
        'host': os.environ.get('PG_HOST', 'localhost'),
        'port': int(os.environ.get('PG_PORT', 5432)),
        'dbname': os.environ.get('PG_DB', 'appdb'),
        'user': os.environ.get('PG_USER', 'app'),
        'password': os.environ.get('PG_PASSWORD', ''),
    }
    return _PG_CONFIG


def _get_pg_conn():
    """Get a psycopg2 connection with RealDictCursor."""
    cfg = _get_pg_config()
    return psycopg2.connect(**cfg, cursor_factory=psycopg2.extras.RealDictCursor)


@functools.lru_cache(maxsize=1)
def get_brand_settings():
    """Return brand settings dict, or None if table doesn't exist yet."""
    try:
        conn = _get_pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM brand_settings WHERE id=1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            d = dict(row)
            d['version'] = _get_project_version()
            return d
    except psycopg2.OperationalError:
        # Table not created yet — app hasn't run init_db()
        pass
    return None


def get_tm_brand_settings():
    """Return TradeMind sub-brand settings dict."""
    try:
        conn = _get_pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM tm_brand_settings WHERE id=1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return dict(row)
    except psycopg2.OperationalError:
        pass
    return None



