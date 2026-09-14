#!/usr/bin/env python3
"""
migrate_veroguard.py — VeroGuard 数据库迁移（官方服务器端）
=============================================================
创建 VeroGuard 所需的 5 张表。仅在官方服务器上的 PostgreSQL 中运行。

表:
  veroguard.probe_instances     — 探针实例注册表
  veroguard.integrity_violations — 完整性违规记录
  veroguard.remote_commands     — 远程命令队列
  veroguard.probe_heartbeats    — 心跳历史
  veroguard.alert_events        — 告警事件

用法（在官方服务器上）:
    python3 veroguard/tools/migrate_veroguard.py
"""
import os
import sys
import psycopg2
import psycopg2.extras

PG_CONFIG = {
    'host': os.environ.get('PG_HOST', 'localhost'),
    'port': int(os.environ.get('PG_PORT', 5432)),
    'dbname': os.environ.get('PG_DB', 'appdb'),
    'user': os.environ.get('PG_USER', 'app'),
    'password': os.environ.get('PG_PASSWORD', ''),
}


def _get_conn():
    conn = psycopg2.connect(**PG_CONFIG,
                            cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn


def init_veroguard_schema():
    """创建 veroguard schema 和 5 张表（幂等）"""
    conn = _get_conn()
    cur = conn.cursor()

    # 1. 创建 schema
    cur.execute('CREATE SCHEMA IF NOT EXISTS veroguard')
    print('[OK] Schema veroguard ready')

    # 2. probe_instances — 探针实例
    cur.execute('''
        CREATE TABLE IF NOT EXISTS veroguard.probe_instances (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            deployment_code TEXT NOT NULL,
            machine_id      TEXT NOT NULL,
            fingerprint     JSONB NOT NULL DEFAULT '{}',
            status          TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active','locked','shutdown','offline','destroyed')),
            first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_heartbeat  TIMESTAMPTZ,
            heartbeat_count BIGINT NOT NULL DEFAULT 0,
            version         TEXT,
            integrity_status TEXT DEFAULT 'clean'
                            CHECK(integrity_status IN ('clean','warning','violated')),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(deployment_code, machine_id)
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_pi_status ON veroguard.probe_instances(status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_pi_heartbeat ON veroguard.probe_instances(last_heartbeat)')
    print('[OK] veroguard.probe_instances ready')

    # 3. integrity_violations — 违规记录
    cur.execute('''
        CREATE TABLE IF NOT EXISTS veroguard.integrity_violations (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            instance_id     BIGINT NOT NULL REFERENCES veroguard.probe_instances(id),
            machine_id      TEXT NOT NULL,
            file_path       TEXT NOT NULL,
            violation_type  TEXT NOT NULL CHECK(violation_type IN ('modified','deleted')),
            expected_hash   TEXT,
            actual_hash     TEXT,
            severity        TEXT NOT NULL DEFAULT 'warning',
            detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved        BOOLEAN NOT NULL DEFAULT FALSE
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_iv_instance ON veroguard.integrity_violations(instance_id)')
    print('[OK] veroguard.integrity_violations ready')

    # 4. remote_commands — 命令队列
    cur.execute('''
        CREATE TABLE IF NOT EXISTS veroguard.remote_commands (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            instance_id     BIGINT,
            machine_id      TEXT NOT NULL,
            command_id      TEXT UNIQUE NOT NULL,
            action          TEXT NOT NULL,
            params          JSONB DEFAULT '{}',
            status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','sent','executed','failed','acknowledged')),
            issued_by       BIGINT,
            issued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            executed_at     TIMESTAMPTZ,
            result          TEXT
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rc_status ON veroguard.remote_commands(status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rc_machine ON veroguard.remote_commands(machine_id)')
    print('[OK] veroguard.remote_commands ready')

    # 5. probe_heartbeats — 心跳历史
    cur.execute('''
        CREATE TABLE IF NOT EXISTS veroguard.probe_heartbeats (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            instance_id         BIGINT NOT NULL REFERENCES veroguard.probe_instances(id),
            machine_id          TEXT NOT NULL,
            fingerprint_snapshot JSONB DEFAULT '{}',
            integrity_status    TEXT,
            debugger_detected   BOOLEAN DEFAULT FALSE,
            received_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ph_machine ON veroguard.probe_heartbeats(machine_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ph_time ON veroguard.probe_heartbeats(received_at)')
    print('[OK] veroguard.probe_heartbeats ready')

    # 6. alert_events — 告警
    cur.execute('''
        CREATE TABLE IF NOT EXISTS veroguard.alert_events (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            instance_id     BIGINT REFERENCES veroguard.probe_instances(id),
            alert_type      TEXT NOT NULL,
            severity        TEXT NOT NULL DEFAULT 'warning'
                            CHECK(severity IN ('info','warning','critical')),
            title           TEXT NOT NULL,
            detail          JSONB DEFAULT '{}',
            acknowledged    BOOLEAN NOT NULL DEFAULT FALSE,
            acknowledged_by BIGINT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ae_type ON veroguard.alert_events(alert_type)')
    print('[OK] veroguard.alert_events ready')

    cur.close()
    conn.close()
    print('[DONE] VeroGuard schema migration complete (6 tables)')


if __name__ == '__main__':
    try:
        init_veroguard_schema()
    except Exception as e:
        print(f'[FAIL] Migration error: {e}')
        sys.exit(1)
