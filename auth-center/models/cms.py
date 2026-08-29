#!/usr/bin/env python3
"""CMS Models — cms_blocks, cms_posts, cms_settings, cms_categories tables for the database"""
import json
from models import get_db


def init_cms_tables():
    """Create CMS tables if not exist."""
    from models.database import get_table_columns
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cms_blocks (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                page            TEXT NOT NULL,
                section         TEXT NOT NULL,
                block_type      TEXT NOT NULL DEFAULT 'text',
                position        BIGINT NOT NULL DEFAULT 0,
                title           TEXT DEFAULT '',
                subtitle        TEXT DEFAULT '',
                content         TEXT DEFAULT '',
                image_url       TEXT DEFAULT '',
                link_url        TEXT DEFAULT '',
                link_text       TEXT DEFAULT '',
                icon            TEXT DEFAULT '',
                extra_json      TEXT DEFAULT '{}',
                is_published    BIGINT NOT NULL DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cms_blocks_page ON cms_blocks(page, position)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS cms_categories (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL,
                icon            TEXT DEFAULT '📄',
                slug            TEXT DEFAULT '',
                audience        TEXT NOT NULL DEFAULT 'public',
                sort_order      BIGINT NOT NULL DEFAULT 0,
                is_active       BIGINT NOT NULL DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS cms_posts (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                slug            TEXT UNIQUE NOT NULL,
                category        TEXT NOT NULL DEFAULT 'insights',
                title           TEXT NOT NULL DEFAULT '',
                excerpt         TEXT DEFAULT '',
                content         TEXT DEFAULT '',
                content_format  TEXT DEFAULT 'html',
                cover_image     TEXT DEFAULT '',
                author          TEXT DEFAULT '',
                tags            TEXT DEFAULT '[]',
                audience        TEXT NOT NULL DEFAULT 'public',
                is_published    BIGINT NOT NULL DEFAULT 0,
                publish_channels TEXT DEFAULT '[]',
                published_at    TIMESTAMP DEFAULT NULL,
                source          TEXT DEFAULT 'manual',
                source_id       BIGINT DEFAULT NULL,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cms_posts_cat ON cms_posts(category, published_at)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS cms_settings (
                key             TEXT PRIMARY KEY,
                value           TEXT NOT NULL DEFAULT '',
                description     TEXT DEFAULT ''
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                slug            TEXT UNIQUE NOT NULL,
                name            TEXT NOT NULL,
                tagline         TEXT DEFAULT '',
                description     TEXT DEFAULT '',
                category        TEXT NOT NULL DEFAULT 'skills',
                platforms       TEXT DEFAULT '["linux","macos","windows"]',
                version         TEXT NOT NULL DEFAULT '1.0.0',
                release_date    TEXT DEFAULT '',
                repo_url        TEXT DEFAULT '',
                download_url    TEXT DEFAULT '',
                docs_url        TEXT DEFAULT '',
                changelog_url   TEXT DEFAULT '',
                file_size       TEXT DEFAULT '',
                checksum_sha256 TEXT DEFAULT '',
                license         TEXT DEFAULT 'MIT',
                requirements    TEXT DEFAULT '',
                tags            TEXT DEFAULT '[]',
                icon            TEXT DEFAULT '📦',
                sort_order      BIGINT NOT NULL DEFAULT 0,
                is_published    BIGINT NOT NULL DEFAULT 1,
                download_count  BIGINT NOT NULL DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloads_cat ON downloads(category, sort_order)"
        )
        conn.commit()
        # Seed default categories if empty
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM cms_categories")
        existing = cur.fetchone()[0]
        if existing == 0:
            cats = [
                # ── 公开分类 ──
                ('快速入门', '🔰', 'getting-started', 'public', 1),
                ('Agent 开发', '🤖', 'agent-dev', 'internal', 2),
                ('金融分析', '📈', 'finance', 'public', 3),
                ('最佳实践', '⭐', 'best-practices', 'internal', 4),
                ('产品动态', '📢', 'insights', 'public', 5),
                ('帮助中心', '❓', 'help', 'public', 6),
                ('法律合规', '⚖️', 'legal', 'public', 90),
            ]
            for name, icon, slug, audience, sort in cats:
                conn.execute(
                    "INSERT INTO cms_categories (name, icon, slug, audience, sort_order) VALUES (%s,%s,%s,%s,%s)",
                    (name, icon, slug, audience, sort)
                )
        # Migration: add source/source_id columns for existing DBs (idempotent)
        cols = get_table_columns(conn, 'cms_posts')
        if 'source' not in cols:
            conn.execute("ALTER TABLE cms_posts ADD COLUMN source TEXT DEFAULT 'manual'")
        if 'source_id' not in cols:
            conn.execute("ALTER TABLE cms_posts ADD COLUMN source_id BIGINT DEFAULT NULL")
        if 'views' not in cols:
            conn.execute("ALTER TABLE cms_posts ADD COLUMN views BIGINT NOT NULL DEFAULT 0")
        conn.commit()


# ── Block helpers ──────────────────────────────────────────

def get_page_blocks(page: str):
    """Get all published blocks for a page, ordered by position."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM cms_blocks WHERE page=%s AND is_published=1 ORDER BY position",
            (page,)
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_block(data: dict):
    """Insert or update a content block.

    编辑采用字段级合并（PATCH 语义）：仅更新请求中显式提供的字段，
    避免 PUT 增量提交时未传字段被重置为默认值（VR-CMS-001）。
    """
    with get_db() as conn:
        if data.get('id'):
            # 动态 UPDATE：仅更新请求中显式提供的字段，避免全量覆盖
            updates = []
            params = []
            for f in ('page', 'section', 'block_type', 'position',
                      'title', 'subtitle', 'content', 'image_url',
                      'link_url', 'link_text', 'icon', 'extra_json',
                      'is_published'):
                if f not in data:
                    continue
                updates.append(f'{f}=%s')
                params.append(data[f])
            updates.append('updated_at=NOW()')
            params.append(data['id'])
            conn.execute(
                f'UPDATE cms_blocks SET {", ".join(updates)} WHERE id=%s',
                params
            )
        else:
            cur = conn.execute("""
                INSERT INTO cms_blocks (page, section, block_type, position, title, subtitle,
                    content, image_url, link_url, link_text, icon, extra_json, is_published)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (
                data.get('page', ''), data.get('section', ''), data.get('block_type', 'text'),
                data.get('position', 0), data.get('title', ''), data.get('subtitle', ''),
                data.get('content', ''), data.get('image_url', ''), data.get('link_url', ''),
                data.get('link_text', ''), data.get('icon', ''), data.get('extra_json', '{}'),
                data.get('is_published', 1)
            ))
            data['id'] = cur.fetchone()['id']
        conn.commit()
    return data


def delete_block(block_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM cms_blocks WHERE id=%s", (block_id,))
        conn.commit()


def reorder_blocks(page: str, block_ids: list):
    with get_db() as conn:
        for i, bid in enumerate(block_ids):
            conn.execute("UPDATE cms_blocks SET position=%s, updated_at=NOW() WHERE id=%s AND page=%s",
                         (i, bid, page))
        conn.commit()


# ── Post helpers ──────────────────────────────────────────

def get_posts(category: str = None, limit: int = 20, offset: int = 0, published_only: bool = True,
              audience: str = None):
    with get_db() as conn:
        sql = "SELECT * FROM cms_posts WHERE 1=1 "
        params = []
        if category:
            sql += "AND category=%s "
            params.append(category)
        if audience:
            sql += "AND audience=%s "
            params.append(audience)
        if published_only:
            sql += "AND is_published=1 "
        sql += "ORDER BY published_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        posts = [dict(r) for r in rows]
        for p in posts:
            if isinstance(p.get('publish_channels'), str):
                p['publish_channels'] = json.loads(p['publish_channels'])
        return posts


def get_post_by_slug(slug: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM cms_posts WHERE slug=%s AND is_published=1", (slug,)).fetchone()
        return dict(row) if row else None


def get_post_by_slug_preview(slug: str):
    """Get article by slug for preview (bypass is_published filter for admin preview)."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM cms_posts WHERE slug=%s", (slug,)).fetchone()
        return dict(row) if row else None


def get_all_posts(limit: int = 50, offset: int = 0, status_filter: str = None, audience: str = None,
                  source: str = None):
    with get_db() as conn:
        sql = "SELECT * FROM cms_posts"
        params = []
        conditions = []
        if status_filter == 'draft':
            conditions.append("is_published=0")
        elif status_filter == 'published':
            conditions.append("is_published=1")
        if source and source != 'all':
            conditions.append("source=%s")
            params.append(source)
        if audience:
            if ',' in audience:
                vals = [v.strip() for v in audience.split(',')]
                placeholders = ','.join(['%s'] * len(vals))
                conditions.append(f"audience IN ({placeholders})")
                params.extend(vals)
            else:
                conditions.append("audience=%s")
                params.append(audience)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        posts = [dict(r) for r in rows]
        for p in posts:
            if isinstance(p.get('publish_channels'), str):
                p['publish_channels'] = json.loads(p['publish_channels'])
        return posts


def sanitize_html(raw: str) -> str:
    """白名单式 HTML 内容净化（不依赖外部库）。"""
    import re

    MAX_LENGTH = 500_000  # 500KB
    if len(raw) > MAX_LENGTH:
        raise ValueError(f"Content too long: {len(raw)} chars, max {MAX_LENGTH}")

    ALLOWED_TAGS = {
        'p', 'div', 'span', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'ul', 'ol', 'li', 'a', 'img', 'br', 'hr',
        'strong', 'em', 'b', 'i', 'u', 's', 'sub', 'sup',
        'table', 'tr', 'td', 'th', 'thead', 'tbody', 'tfoot',
        'blockquote', 'pre', 'code', 'figure', 'figcaption',
    }
    DANGEROUS_TAG_PATTERN = re.compile(
        r'<\s*(script|iframe|object|embed|form|input|textarea|select|button|'
        r'meta|link|style|base|noscript|applet|audio|video)\b[^>]*>.*?'
        r'<\s*/\s*\1\s*>',
        re.DOTALL | re.IGNORECASE
    )
    DANGEROUS_SELF_CLOSING = re.compile(
        r'<\s*(input|meta|link|br|hr|img|embed|param|source|area|base|col)\b[^>]*/?>',
        re.IGNORECASE
    )

    # 阶段1：移除危险标签及其内容（不保留内部文本）
    cleaned = DANGEROUS_TAG_PATTERN.sub('', raw)

    # 阶段2：移除自闭合危险标签
    cleaned = DANGEROUS_SELF_CLOSING.sub('', cleaned)

    # 阶段3：清理所有标签的白名单和属性
    def _clean_tag(m):
        try:
            tag_text = m.group(0)
            if tag_text.startswith('</'):
                tag_name = tag_text[2:-1].strip().lower()
                return f'</{tag_name}>' if tag_name in ALLOWED_TAGS else ''
            m2 = re.match(r'<(\w+)(.*?)(\s*/?\s*)>', tag_text, re.DOTALL)
            if not m2:
                return ''
            tag_name = m2.group(1).lower()
            if tag_name not in ALLOWED_TAGS:
                return ''
            attrs_str = m2.group(2)
            closing = m2.group(3)

            SAFE_ATTRS = {'href', 'src', 'alt', 'title', 'class', 'id', 'style', 'target', 'rel', 'width', 'height', 'loading', 'decoding'}
            cleaned_attrs = []
            for attr_match in re.finditer(r'''([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')''', attrs_str):
                attr_name = attr_match.group(1).lower()
                attr_val = attr_match.group(2) or attr_match.group(3) or ''
                if attr_name.startswith('on'):
                    continue
                if attr_name not in SAFE_ATTRS:
                    continue
                lowered_val = attr_val.strip().lower()
                # 先做 HTML 实体解码再校验危险协议，防止
                # `&#106;avascript:` 之类编码绕过（解码后为 javascript:）
                import html as _html
                decoded_val = _html.unescape(lowered_val).replace('\x00', '')
                if any(decoded_val.startswith(p) for p in ['javascript:', 'vbscript:', 'data:', 'expression']):
                    continue
                cleaned_attrs.append(f'{attr_name}="{attr_val}"')

            attr_space = ' ' + ' '.join(cleaned_attrs) if cleaned_attrs else ''
            return f'<{tag_name}{attr_space}{closing}>'
        except Exception:
            return ''

    # 递归清理所有标签（带最大迭代次数保护）
    prev = None
    max_iterations = 20
    iterations = 0
    while prev != cleaned:
        prev = cleaned
        cleaned = re.sub(r'<[^>]*>', _clean_tag, cleaned)
        iterations += 1
        if iterations > max_iterations:
            break

    return cleaned


def upsert_post(data: dict):
    import re
    # HTML 内容净化（白名单式）
    content = data.get('content', '')
    data['content'] = sanitize_html(content)
    channels_json = json.dumps(data.get('publish_channels', []), ensure_ascii=False)
    tags_json = json.dumps(data.get('tags', []), ensure_ascii=False) if isinstance(data.get('tags'), list) else data.get('tags', '[]')
    with get_db() as conn:
        if data.get('id'):
            # 动态 UPDATE：仅更新请求中显式提供的字段，避免 PUT 增量提交
            # 时未传字段被重置（content 清空 / category 回退 / is_published 归 0）。
            updates = []
            params = []
            for f in ('slug', 'category', 'title', 'excerpt', 'content',
                      'content_format', 'cover_image', 'author', 'tags',
                      'audience', 'is_published', 'publish_channels',
                      'source', 'source_id', 'published_at'):
                if f not in data:
                    continue
                if f == 'published_at':
                    updates.append('published_at=COALESCE(%s, published_at)')
                    params.append(data['published_at'] if data.get('is_published') in (1, True) else None)
                elif f == 'content':
                    updates.append('content=%s')
                    params.append(data['content'])
                elif f == 'tags':
                    updates.append('tags=%s')
                    params.append(json.dumps(data['tags'], ensure_ascii=False) if isinstance(data['tags'], list) else data.get('tags', '[]'))
                elif f == 'publish_channels':
                    updates.append('publish_channels=%s')
                    params.append(json.dumps(data['publish_channels'], ensure_ascii=False))
                else:
                    updates.append(f'{f}=%s')
                    params.append(data[f])
            updates.append('updated_at=NOW()')
            params.append(data['id'])
            conn.execute(
                f'UPDATE cms_posts SET {", ".join(updates)} WHERE id=%s',
                params
            )
        else:
            cur = conn.execute("""
                INSERT INTO cms_posts (slug, category, title, excerpt, content, content_format, cover_image, author, tags, audience, is_published, publish_channels, source, source_id, published_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN NOW() ELSE NULL END) RETURNING id
            """, (
                data.get('slug', ''), data.get('category', 'insights'),
                data.get('title', ''), data.get('excerpt', ''), data.get('content', ''),
                data.get('content_format', 'html'), data.get('cover_image', ''), data.get('author', ''),
                tags_json, data.get('audience', 'public'),
                data.get('is_published', 0),
                channels_json,
                data.get('source', 'manual'), data.get('source_id'),
                True if data.get('is_published') in (1, True) else False
            ))
            data['id'] = cur.fetchone()['id']
        conn.commit()
    return data


def delete_post(post_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM cms_posts WHERE id=%s", (post_id,))
        conn.commit()


# ── Settings helpers ──────────────────────────────────────

def get_setting(key: str, default: str = ''):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM cms_settings WHERE key=%s", (key,)).fetchone()
        return row['value'] if row else default


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO cms_settings (key, value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        conn.commit()


def get_all_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM cms_settings").fetchall()
        return {r['key']: r['value'] for r in rows}


# ── Category helpers ──────────────────────────────────────

def get_categories(active_only=True, audience: str = None):
    with get_db() as conn:
        sql = "SELECT * FROM cms_categories"
        conditions = []
        params = []
        if active_only:
            conditions.append("is_active=1")
        if audience:
            conditions.append("audience=%s")
            params.append(audience)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY sort_order, id"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def upsert_category(data: dict):
    with get_db() as conn:
        if data.get('id'):
            conn.execute(
                "UPDATE cms_categories SET name=%s, icon=%s, slug=%s, audience=%s, sort_order=%s, is_active=%s WHERE id=%s",
                (data['name'], data.get('icon', '📄'), data.get('slug', ''),
                 data.get('audience', 'public'), int(data.get('sort_order', 0)),
                 data.get('is_active', 1), data['id'])
            )
        else:
            cur = conn.execute(
                "INSERT INTO cms_categories (name, icon, slug, audience, sort_order, is_active) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (data['name'], data.get('icon', '📄'), data.get('slug', ''),
                 data.get('audience', 'public'), int(data.get('sort_order', 0)),
                 data.get('is_active', 1))
            )
            data['id'] = cur.fetchone()['id']
        conn.commit()
    return data


def delete_category(cat_id: int):
    with get_db() as conn:
        # 检查是否有文章引用此分类
        ref_count = conn.execute(
            "SELECT COUNT(*) FROM cms_posts WHERE category=(SELECT name FROM cms_categories WHERE id=%s)",
            (cat_id,)
        ).fetchone()['count']
        if ref_count > 0:
            raise ValueError(f'该分类下有 {ref_count} 篇文章，请先移除或更改文章分类后再删除')
        conn.execute("DELETE FROM cms_categories WHERE id=%s", (cat_id,))
        conn.commit()


def reorder_categories(ids: list):
    with get_db() as conn:
        for i, cid in enumerate(ids):
            conn.execute("UPDATE cms_categories SET sort_order=%s WHERE id=%s", (i, cid))
        conn.commit()


# ── Downloads helpers ─────────────────────────────────────

def get_downloads(category: str = None, published_only: bool = True, limit: int = 50):
    """Get downloads list, optionally filtered by category."""
    with get_db() as conn:
        sql = "SELECT * FROM downloads WHERE 1=1 "
        params = []
        if category:
            sql += "AND category=%s "
            params.append(category)
        if published_only:
            sql += "AND is_published=1 "
        sql += "ORDER BY sort_order, id LIMIT %s"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        items = [dict(r) for r in rows]
        for it in items:
            for field in ('platforms', 'tags'):
                if isinstance(it.get(field), str):
                    try:
                        it[field] = json.loads(it[field])
                    except Exception:
                        it[field] = []
        return items


def get_download_by_slug(slug: str):
    """Get a single download item by slug."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM downloads WHERE slug=%s AND is_published=1", (slug,)
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item['tags'] = json.loads(item['tags']) if item.get('tags') else []
    except Exception:
        item['tags'] = []
    return item


def get_download(dl_id: int):
    """Get a single download item by ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM downloads WHERE id=%s", (dl_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item['tags'] = json.loads(item['tags']) if item.get('tags') else []
    except Exception:
        item['tags'] = []
    return item


def get_all_downloads(limit: int = 100):
    """Get all downloads (including unpublished) for admin."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM downloads ORDER BY sort_order, id LIMIT %s", (limit,)
        ).fetchall()
        items = [dict(r) for r in rows]
        for it in items:
            for field in ('platforms', 'tags'):
                if isinstance(it.get(field), str):
                    try:
                        it[field] = json.loads(it[field])
                    except Exception:
                        it[field] = []
        return items


def upsert_download(data: dict):
    """Insert or update a download item."""
    platforms_json = json.dumps(data.get('platforms', []), ensure_ascii=False) if isinstance(data.get('platforms'), list) else data.get('platforms', '[]')
    tags_json = json.dumps(data.get('tags', []), ensure_ascii=False) if isinstance(data.get('tags'), list) else data.get('tags', '[]')
    with get_db() as conn:
        if data.get('id'):
            conn.execute("""
                UPDATE downloads SET
                    slug=%s, name=%s, tagline=%s, description=%s, category=%s,
                    platforms=%s, version=%s, release_date=%s, repo_url=%s,
                    download_url=%s, docs_url=%s, changelog_url=%s,
                    file_size=%s, checksum_sha256=%s, license=%s, requirements=%s,
                    tags=%s, icon=%s, sort_order=%s, is_published=%s,
                    updated_at=NOW()
                WHERE id=%s
            """, (
                data.get('slug', ''), data.get('name', ''), data.get('tagline', ''),
                data.get('description', ''), data.get('category', 'skills'),
                platforms_json, data.get('version', '1.0.0'), data.get('release_date', ''),
                data.get('repo_url', ''), data.get('download_url', ''),
                data.get('docs_url', ''), data.get('changelog_url', ''),
                data.get('file_size', ''), data.get('checksum_sha256', ''),
                data.get('license', 'MIT'), data.get('requirements', ''),
                tags_json, data.get('icon', '📦'),
                int(data.get('sort_order', 0)), data.get('is_published', 1),
                data['id']
            ))
        else:
            cur = conn.execute("""
                INSERT INTO downloads (slug, name, tagline, description, category,
                    platforms, version, release_date, repo_url, download_url,
                    docs_url, changelog_url, file_size, checksum_sha256,
                    license, requirements, tags, icon, sort_order, is_published)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (
                data.get('slug', ''), data.get('name', ''), data.get('tagline', ''),
                data.get('description', ''), data.get('category', 'skills'),
                platforms_json, data.get('version', '1.0.0'), data.get('release_date', ''),
                data.get('repo_url', ''), data.get('download_url', ''),
                data.get('docs_url', ''), data.get('changelog_url', ''),
                data.get('file_size', ''), data.get('checksum_sha256', ''),
                data.get('license', 'MIT'), data.get('requirements', ''),
                tags_json, data.get('icon', '📦'),
                int(data.get('sort_order', 0)), data.get('is_published', 1)
            ))
            data['id'] = cur.fetchone()['id']
        conn.commit()
    return data


def delete_download(dl_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM downloads WHERE id=%s", (dl_id,))
        conn.commit()


def reorder_downloads(ids: list):
    with get_db() as conn:
        for i, did in enumerate(ids):
            conn.execute("UPDATE downloads SET sort_order=%s, updated_at=NOW() WHERE id=%s",
                         (i, did))
        conn.commit()


def increment_download_count(slug: str):
    with get_db() as conn:
        conn.execute("UPDATE downloads SET download_count=download_count+1 WHERE slug=%s", (slug,))
        conn.commit()
