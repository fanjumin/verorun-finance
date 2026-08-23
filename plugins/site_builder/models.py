#!/usr/bin/env python3
"""Site Builder — Data Models & CRUD"""

import os, json, yaml
from .db import get_db

# ── Table Name Constants ──
TABLE_PROMPTS = 'site_builder_prompts'
TABLE_TASKS = 'site_builder_tasks'


def init_tables():
    """Create site_builder DB tables (idempotent)"""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS site_builder_prompts (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                identifier      TEXT UNIQUE NOT NULL,
                name            TEXT NOT NULL,
                description     TEXT DEFAULT '',
                icon            TEXT DEFAULT '📄',
                industry        TEXT DEFAULT '',
                tags_json       TEXT DEFAULT '[]',
                is_builtin      BIGINT DEFAULT 1,
                is_active       BIGINT DEFAULT 1,
                defaults_json   TEXT DEFAULT '{}',
                pages_json      TEXT DEFAULT '[]',
                documents_json  TEXT DEFAULT '[]',
                prompts_json    TEXT DEFAULT '{}',
                created_by      BIGINT DEFAULT 0,
                created_at      TEXT DEFAULT (NOW()),
                updated_at      TEXT DEFAULT (NOW())
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS site_builder_tasks (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                task_id         TEXT UNIQUE NOT NULL,
                user_id         BIGINT NOT NULL,
                site_config_id  BIGINT DEFAULT 1,
                prompt_id       BIGINT,
                user_input      TEXT DEFAULT '',
                status          TEXT DEFAULT 'pending',
                plan_json       TEXT DEFAULT '{}',
                result_json     TEXT DEFAULT '{}',
                current_step    TEXT DEFAULT '',
                error_message   TEXT DEFAULT '',
                created_at      TEXT DEFAULT (NOW()),
                updated_at      TEXT DEFAULT (NOW()),
                finished_at     TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbp_identifier ON site_builder_prompts(identifier)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbp_industry ON site_builder_prompts(industry)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbt_user ON site_builder_tasks(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbt_status ON site_builder_tasks(status)")
        conn.commit()
    print('[SiteBuilder] ✅ Tables initialized')


def seed_default_prompts():
    """Seed built-in industry prompt templates (idempotent, skip if exists)"""
    prompts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')
    if not os.path.isdir(prompts_dir):
        return 0

    count = 0
    for fname in sorted(os.listdir(prompts_dir)):
        if not fname.endswith('.yml'):
            continue
        fpath = os.path.join(prompts_dir, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            print(f'[SiteBuilder] Failed to load prompt template {fname}: {e}')
            continue

        identifier = data.get('identifier', fname.replace('.yml', ''))
        with get_db() as conn:
            exist = conn.execute(
                f"SELECT id FROM {TABLE_PROMPTS} WHERE identifier=%s",
                (identifier,)
            ).fetchone()
            if exist:
                continue

            conn.execute(
                f'''INSERT INTO {TABLE_PROMPTS}
                    (identifier, name, description, icon, industry, tags_json,
                     is_builtin, defaults_json, pages_json, documents_json, prompts_json)
                    VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s)
                    ON CONFLICT (identifier) DO NOTHING''',
                (
                    identifier,
                    data.get('name', identifier),
                    data.get('description', ''),
                    data.get('icon', '📄'),
                    data.get('industry', ''),
                    json.dumps(data.get('tags', []), ensure_ascii=False),
                    json.dumps(data.get('defaults', {}), ensure_ascii=False),
                    json.dumps(data.get('pages', []), ensure_ascii=False),
                    json.dumps(data.get('documents', []), ensure_ascii=False),
                    json.dumps(data.get('prompts', {}), ensure_ascii=False),
                )
            )
            conn.commit()
            count += 1
    if count:
        print(f'[SiteBuilder] ✅ Seeded {count} built-in prompt templates')
    return count


# ── CRUD Helpers ──

def get_prompt(identifier_or_id):
    """Get a single prompt template"""
    with get_db() as conn:
        if isinstance(identifier_or_id, int):
            row = conn.execute(
                f"SELECT * FROM {TABLE_PROMPTS} WHERE id=%s", (identifier_or_id,)
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT * FROM {TABLE_PROMPTS} WHERE identifier=%s", (identifier_or_id,)
            ).fetchone()
        if not row:
            return None
        return _parse_prompt_row(row)


def list_prompts(active_only=False, industry=None):
    """List all prompt templates"""
    with get_db() as conn:
        conditions = []
        params = []
        if active_only:
            conditions.append("is_active=1")
        if industry:
            conditions.append("industry=%s")
            params.append(industry)
        where = " AND ".join(conditions) if conditions else "1=1"
        rows = conn.execute(
            f"SELECT * FROM {TABLE_PROMPTS} WHERE {where} ORDER BY is_builtin DESC, id ASC",
            params
        ).fetchall()
    return [_parse_prompt_row(r) for r in rows]


def create_prompt(data: dict) -> int:
    """Create a custom prompt template, return new ID"""
    identifier = data.get('identifier', '').strip()
    if not identifier:
        identifier = 'custom_' + _short_id()
    with get_db() as conn:
        conn.execute(
            f'''INSERT INTO {TABLE_PROMPTS}
                (identifier, name, description, icon, industry, tags_json,
                 is_builtin, is_active, defaults_json, pages_json, documents_json, prompts_json, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,0,1,%s,%s,%s,%s,%s)''',
            (
                identifier,
                data.get('name', ''),
                data.get('description', ''),
                data.get('icon', '📄'),
                data.get('industry', ''),
                json.dumps(data.get('tags', []), ensure_ascii=False),
                json.dumps(data.get('defaults', {}), ensure_ascii=False),
                json.dumps(data.get('pages', []), ensure_ascii=False),
                json.dumps(data.get('documents', []), ensure_ascii=False),
                json.dumps(data.get('prompts', {}), ensure_ascii=False),
                data.get('created_by', 0),
            )
        )
        conn.commit()
        new_id = conn.execute("SELECT lastval()").fetchone()['lastval']
    return new_id


def update_prompt(prompt_id: int, data: dict):
    """Update prompt template"""
    fields = []
    params = []
    for key in ['name', 'description', 'icon', 'industry', 'is_active']:
        if key in data:
            fields.append(f"{key}=%s")
            params.append(data[key])
    for key in ['tags', 'defaults', 'pages', 'documents', 'prompts']:
        json_key = f"{key}_json" if key == 'tags' else f"{key}_json"
        if key in data:
            fields.append(f"{json_key}=%s")
            params.append(json.dumps(data[key], ensure_ascii=False))
    fields.append("updated_at=NOW()")
    params.append(prompt_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE {TABLE_PROMPTS} SET {', '.join(fields)} WHERE id=%s",
            params
        )
        conn.commit()


def delete_prompt(prompt_id: int):
    """Delete prompt template (only user-created)"""
    with get_db() as conn:
        conn.execute(
            f"DELETE FROM {TABLE_PROMPTS} WHERE id=%s AND is_builtin=0",
            (prompt_id,)
        )
        conn.commit()


# ── Task Management ──

def create_task(user_id: int, prompt_id: int, user_input: str, site_config_id: int = 1) -> str:
    """Create a build task, return task_id"""
    import datetime, secrets
    task_id = f"SB-{datetime.datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(4).upper()}"
    with get_db() as conn:
        conn.execute(
            f'''INSERT INTO {TABLE_TASKS}
                (task_id, user_id, site_config_id, prompt_id, user_input, status)
                VALUES (%s,%s,%s,%s,%s,'pending')''',
            (task_id, user_id, site_config_id, prompt_id, user_input)
        )
        conn.commit()
    return task_id


def update_task(task_id: str, **kwargs):
    """Update task status"""
    allowed = ['status', 'plan_json', 'result_json', 'current_step', 'error_message']
    fields = []
    params = []
    for key in allowed:
        if key in kwargs:
            fields.append(f"{key}=%s")
            val = kwargs[key]
            if isinstance(val, (dict, list)):
                val = json.dumps(val, ensure_ascii=False)
            params.append(val)
    if kwargs.get('status') in ('completed', 'failed'):
        fields.append("finished_at=NOW()")
    fields.append("updated_at=NOW()")
    params.append(task_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE {TABLE_TASKS} SET {', '.join(fields)} WHERE task_id=%s",
            params
        )
        conn.commit()


def get_task(task_id: str):
    """Get task details"""
    with get_db() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_TASKS} WHERE task_id=%s", (task_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for key in ('plan_json', 'result_json'):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d


def list_tasks(user_id=None, limit=20):
    """List tasks"""
    with get_db() as conn:
        if user_id:
            rows = conn.execute(
                f"SELECT * FROM {TABLE_TASKS} WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM {TABLE_TASKS} ORDER BY created_at DESC LIMIT %s",
                (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# ── Internal Helpers ──

def _parse_prompt_row(row):
    """Convert DB row to dict, parse JSON fields"""
    d = dict(row)
    for key in ('tags_json', 'defaults_json', 'pages_json', 'documents_json', 'prompts_json'):
        if d.get(key):
            try:
                d[key.replace('_json', '')] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                d[key.replace('_json', '')] = {}
        del d[key]
    return d


def _short_id():
    import secrets
    return secrets.token_hex(4)
