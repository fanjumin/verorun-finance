#!/usr/bin/env python3
"""site_builder — idempotent schema bootstrap (independent DB).

run_migrations 确保独立数据库 `site_builder` 中存在 site_builder schema；
数据表由 models.init_tables() / site_settings.models.init_tables() 创建。
"""

import logging

from .db import get_raw_connection

logger = logging.getLogger(__name__)


def run_migrations():
    """Ensure the dedicated schema exists (idempotent, main DB)."""
    conn = get_raw_connection()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS site_builder")
    finally:
        conn.close()
