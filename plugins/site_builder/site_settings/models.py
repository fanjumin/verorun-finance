#!/usr/bin/env python3
"""Site Settings Models — Unified Design Token Data Model

v2.1.0 运行于独立数据库 site_builder（site_builder schema）；
主库 cms_blocks 等共享数据经 main_site 内部 API（internal_client）访问。
"""

import json, os

from ..db import get_db

# ── Default Token Template ──
DEFAULT_TOKENS = {
    "brand": {
        "site_name": "",
        "slogan": "",
        "industry": "",
        "brand_story": "",
        "logo_url": "",
        "favicon_url": "",
        "company_name": "",
        "contact_email": "",
    },
    "colors": {
        "primary": "#6366f1",
        "secondary": "#8b5cf6",
        "accent": "#f59e0b",
        "background": "#ffffff",
        "surface": "#f7fafc",
        "text_primary": "#1a202c",
        "text_secondary": "#718096",
        "border": "#e2e8f0",
        "error": "#ef4444",
        "success": "#10b981",
    },
    "typography": {
        "heading_font": "Inter, sans-serif",
        "body_font": "Inter, -apple-system, sans-serif",
        "font_scale": 1.0,
        "h1_size": "2.5rem",
        "h2_size": "1.875rem",
        "h3_size": "1.5rem",
        "body_size": "1rem",
        "small_size": "0.875rem",
        "line_height": 1.75,
    },
    "navigation": {
        "items": [],
    },
    "footer": {
        "sections": [],
        "articles": [],
        "copyright": "",
        "icp_number": "",
        "security_number": "",
    },
    "spacing": {
        "xs": "4px", "sm": "8px", "md": "16px", "lg": "32px", "xl": "64px",
        "section_gap": "64px", "card_padding": "24px",
    },
    "border_radius": {
        "sm": "4px", "md": "8px", "lg": "12px", "full": "9999px",
    },
    "shadows": {
        "sm": "0 1px 2px rgba(0,0,0,0.05)",
        "md": "0 4px 6px rgba(0,0,0,0.1)",
        "lg": "0 10px 15px rgba(0,0,0,0.1)",
    },
    "seo": {
        "title": "",
        "description": "",
    },
    "meta": {
        "generated_by": "manual",
        "version": 1,
    },
}


def init_tables():
    """Create design_tokens table + site_versions table"""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS design_tokens (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                site_key    TEXT NOT NULL DEFAULT 'platform',
                token_json  TEXT DEFAULT '{}',
                generated_by TEXT DEFAULT 'manual',
                prompt_id   INTEGER DEFAULT NULL,
                version     INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT NOW(),
                updated_at  TEXT DEFAULT NOW(),
                UNIQUE(site_key)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dt_site_key ON design_tokens(site_key)")
        # Migration: add draft_json column if not exists
        try:
            conn.execute("ALTER TABLE design_tokens ADD COLUMN draft_json TEXT DEFAULT '{}'")
        except Exception:
            conn.rollback()  # column already exists
        conn.commit()
    # Also init site_versions table
    init_versions_table()


def get_tokens(site_key='platform'):
    """Get site tokens, return defaults if not found"""
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM design_tokens WHERE site_key=%s', (site_key,)
        ).fetchone()
    if row:
        data = dict(row)
        data['token_json'] = json.loads(data.get('token_json', '{}'))
        return data
    return {'site_key': site_key, 'token_json': dict(DEFAULT_TOKENS), 'generated_by': 'manual', 'version': 1}


def save_tokens(site_key, token_dict, generated_by='manual', prompt_id=None):
    """Save site tokens"""
    token_json = json.dumps(token_dict, ensure_ascii=False)
    with get_db() as conn:
        existing = conn.execute(
            'SELECT id, version FROM design_tokens WHERE site_key=%s', (site_key,)
        ).fetchone()
        if existing:
            new_version = existing['version'] + 1
            conn.execute(
                'UPDATE design_tokens SET token_json=%s, generated_by=%s, prompt_id=%s, version=%s, updated_at=NOW() WHERE site_key=%s',
                (token_json, generated_by, prompt_id, new_version, site_key)
            )
        else:
            conn.execute(
                'INSERT INTO design_tokens (site_key, token_json, generated_by, prompt_id) VALUES (%s,%s,%s,%s)',
                (site_key, token_json, generated_by, prompt_id)
            )
        conn.commit()
    return True


# ── Draft / Preview / Publish ─────────────────────────────────


def get_draft_tokens(site_key='platform'):
    """Get draft tokens from design_tokens.draft_json"""
    with get_db() as conn:
        row = conn.execute(
            'SELECT draft_json FROM design_tokens WHERE site_key=%s', (site_key,)
        ).fetchone()
    if row and row['draft_json']:
        try:
            return json.loads(row['draft_json'])
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def save_draft_tokens(site_key, token_dict):
    """Save draft tokens to design_tokens.draft_json (does NOT touch token_json)"""
    draft_json = json.dumps(token_dict, ensure_ascii=False)
    with get_db() as conn:
        existing = conn.execute(
            'SELECT id FROM design_tokens WHERE site_key=%s', (site_key,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE design_tokens SET draft_json=%s, updated_at=NOW() WHERE site_key=%s",
                (draft_json, site_key)
            )
        else:
            conn.execute(
                'INSERT INTO design_tokens (site_key, token_json, draft_json, generated_by) VALUES (%s,%s,%s,%s)',
                (site_key, '{}', draft_json, 'ai_draft')
            )
        conn.commit()
    return True


def promote_draft_tokens(site_key='platform'):
    """Promote draft tokens to production (draft_json → token_json)"""
    draft = get_draft_tokens(site_key)
    if draft is None:
        return False
    return save_tokens(site_key, draft, generated_by='ai_published')


def backup_tokens(site_key='platform'):
    """Backup current production tokens into a field before overwriting"""
    data = get_tokens(site_key)
    # Save backup alongside current data (simply store as token_json backup)
    backup_label = f'_backup_{data["version"]}'
    backup_val = json.dumps(data['token_json'], ensure_ascii=False)
    with get_db() as conn:
        try:
            conn.execute(
                f"UPDATE design_tokens SET {backup_label}=%s WHERE site_key=%s",
                (backup_val, site_key)
            )
        except Exception:
            pass  # column may not exist — non-critical
        conn.commit()


# ── Editor Draft API Helpers ────────────────────────────────


# ── Site Version History ──────────────────────────────────────


def init_versions_table():
    """Create site_versions table (idempotent)"""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS site_versions (
                id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                site_key      TEXT NOT NULL DEFAULT 'platform',
                version_label TEXT NOT NULL,
                snapshot_json TEXT DEFAULT '{}',
                blocks_json   TEXT DEFAULT '[]',
                is_current    INTEGER DEFAULT 0,
                created_at    TEXT DEFAULT NOW()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sv_site_key ON site_versions(site_key)")
        conn.commit()


def save_site_version(site_key='platform', label=None):
    """Snapshot current draft tokens + draft blocks into site_versions.

    Called automatically during publish. Returns the new version id.
    """
    draft = get_draft_tokens(site_key)
    if draft is None:
        return None

    # Get draft blocks（主库 cms_blocks，经 main_site 内部 API）
    try:
        from ..internal_client import get_draft_blocks
        blocks = get_draft_blocks()
    except Exception:
        blocks = []

    # Determine next version label
    if not label:
        with get_db() as conn:
            last = conn.execute(
                "SELECT version_label FROM site_versions WHERE site_key=%s "
                "ORDER BY id DESC LIMIT 1", (site_key,)
            ).fetchone()
        if last and last['version_label']:
            try:
                num = int(last['version_label'].lstrip('v'))
                label = f'v{num + 1}'
            except (ValueError, IndexError):
                label = 'v1'
        else:
            label = 'v1'

    # Mark all existing versions as not current
    with get_db() as conn:
        conn.execute(
            "UPDATE site_versions SET is_current=0 WHERE site_key=%s",
            (site_key,)
        )
        row = conn.execute(
            "INSERT INTO site_versions (site_key, version_label, snapshot_json, blocks_json, is_current) "
            "VALUES (%s, %s, %s, %s, 1) RETURNING id",
            (site_key, label,
             json.dumps(draft, ensure_ascii=False),
             json.dumps(blocks, ensure_ascii=False))
        ).fetchone()
        new_id = row['id'] if row else None
        conn.commit()

    # Enforce max 30 versions (delete oldest)
    with get_db() as conn:
        conn.execute(
            "DELETE FROM site_versions WHERE site_key=%s AND id NOT IN "
            "(SELECT id FROM site_versions WHERE site_key=%s ORDER BY id DESC LIMIT 30)",
            (site_key, site_key)
        )
        conn.commit()

    return {'id': new_id, 'label': label}


def list_site_versions(site_key='platform'):
    """Return all versions for this site_key, ordered newest first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, version_label, is_current, created_at "
            "FROM site_versions WHERE site_key=%s ORDER BY id DESC",
            (site_key,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_site_version(version_id):
    """Return full version data including snapshot + blocks."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM site_versions WHERE id=%s", (version_id,)
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data['snapshot_json'] = json.loads(data['snapshot_json'])
    except (json.JSONDecodeError, TypeError):
        data['snapshot_json'] = {}
    try:
        data['blocks_json'] = json.loads(data['blocks_json'])
    except (json.JSONDecodeError, TypeError):
        data['blocks_json'] = []
    return data


def restore_site_version(version_id, site_key='platform'):
    """Restore a version: copy snapshot back to draft_json.

    Does NOT auto-publish; user can then edit and publish manually.
    """
    version = get_site_version(version_id)
    if not version:
        return False

    # Restore draft tokens from snapshot
    save_draft_tokens(site_key, version['snapshot_json'])

    # Restore draft blocks: delete current drafts, re-insert saved ones
    # （主库 cms_blocks，经 main_site 内部 API 全局替换）
    try:
        from ..internal_client import replace_draft_blocks, clear_cache
        replace_draft_blocks(page=None, blocks=version['blocks_json'])
        clear_cache()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'[SiteSettings] restore draft blocks failed: {e}')

    # Mark as current
    with get_db() as conn:
        conn.execute("UPDATE site_versions SET is_current=0 WHERE site_key=%s", (site_key,))
        conn.execute("UPDATE site_versions SET is_current=1 WHERE id=%s", (version_id,))
        conn.commit()

    return True


def update_draft_token_field(block_id, field, value):
    """Update a specific field within design_tokens.draft_json

    Special block_id values:
      - hero_title, hero_subtitle, hero_cta, site_name -> brand.*
      - footer_copyright -> footer.copyright
    """
    mapping = {
        'hero_title': ('brand', 'slogan'),
        'hero_subtitle': ('brand', 'brand_story'),
        'hero_cta': ('brand', 'site_name'),
        'site_name': ('brand', 'site_name'),
        'footer_copyright': ('footer', 'copyright'),
    }

    tokens = get_draft_tokens() or {}
    if block_id in mapping:
        section, key = mapping[block_id]
        if section not in tokens:
            tokens[section] = {}
        tokens[section][key] = value
        save_draft_tokens('platform', tokens)
        return True, tokens
    return False, tokens
