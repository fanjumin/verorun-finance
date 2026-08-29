#!/usr/bin/env python3
"""CMS Admin Routes — CRUD for blocks, posts, categories, settings"""
from i18n import _
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flask import Blueprint, request, jsonify
from models.cms import (
    get_page_blocks, upsert_block, delete_block, reorder_blocks,
    get_all_posts, upsert_post, delete_post,
    get_all_settings, set_setting,
    get_categories, upsert_category, delete_category, reorder_categories,
    get_posts
)
from routes.admin import _require_admin, _log

cms_admin_bp = Blueprint('cms_admin', __name__, url_prefix='/admin/cms')


def _check():
    admin, err = _require_admin()
    if err:
        return None, err
    return admin, None


def _ok(data=None):
    return jsonify({"success": True, "data": data})


def _err(msg, code=400):
    return jsonify({"success": False, "error": msg}), code


# ── Blocks ────────────────────────────────────────────────

@cms_admin_bp.route('/blocks/<page>', methods=['GET'])
def list_blocks(page):
    a, e = _check()
    if e: return e
    return _ok(get_page_blocks(page))


@cms_admin_bp.route('/blocks/<page>/all', methods=['GET'])
def list_all_blocks(page):
    a, e = _check()
    if e: return e
    from models import get_db
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM cms_blocks WHERE page=%s ORDER BY position", (page,)
        ).fetchall()
    return _ok([dict(r) for r in rows])


@cms_admin_bp.route('/blocks', methods=['POST'])
def create_block():
    a, e = _check()
    if e: return e
    data = request.get_json(force=True)
    return _ok(upsert_block(data))


@cms_admin_bp.route('/blocks/<int:block_id>', methods=['PUT'])
def update_block(block_id):
    a, e = _check()
    if e: return e
    data = request.get_json(force=True)
    data['id'] = block_id
    return _ok(upsert_block(data))


@cms_admin_bp.route('/blocks/<int:block_id>', methods=['DELETE'])
def remove_block(block_id):
    a, e = _check()
    if e: return e
    delete_block(block_id)
    return _ok({"deleted": block_id})


@cms_admin_bp.route('/blocks/<page>/reorder', methods=['POST'])
def reorder_page_blocks(page):
    a, e = _check()
    if e: return e
    data = request.get_json(force=True)
    reorder_blocks(page, data.get('ids', []))
    return _ok({"reordered": page})


# ── Posts ─────────────────────────────────────────────────

@cms_admin_bp.route('/posts', methods=['GET'])
def list_posts():
    a, e = _check()
    if e: return e
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    status = request.args.get('status', None)
    source = request.args.get('source', None)
    return _ok(get_all_posts(limit=limit, offset=offset, status_filter=status, source=source))


@cms_admin_bp.route('/posts', methods=['POST'])
def create_post():
    a, e = _check()
    if e: return e
    try:
        data = request.get_json(force=True)
        import uuid
        data['slug'] = 'article-' + str(uuid.uuid4())[:8]
        if not data.get('title'): return _err("title is required")
        return _ok(upsert_post(data))
    except Exception as ex:
        import traceback
        traceback.print_exc()
        return _err(f"Failed to create post: {str(ex)}", 500)


@cms_admin_bp.route('/posts/<int:post_id>', methods=['PUT'])
def update_post(post_id):
    a, e = _check()
    if e: return e
    data = request.get_json(force=True)
    data['id'] = post_id
    return _ok(upsert_post(data))


@cms_admin_bp.route('/posts/<int:post_id>', methods=['DELETE'])
def remove_post(post_id):
    a, e = _check()
    if e: return e
    delete_post(post_id)
    return _ok({"deleted": post_id})


@cms_admin_bp.route('/posts/<int:post_id>/publish', methods=['POST'])
def publish_post(post_id):
    """Unified publish: publish to selected local + social channels."""
    a, e = _check()
    if e: return e
    data = request.get_json(force=True) or {}
    from models import get_db
    with get_db() as conn:
        row = conn.execute('SELECT * FROM cms_posts WHERE id=%s', (post_id,)).fetchone()
    if not row:
        return _err('Post not found')
    post = dict(row)
    channels = data.get('channels', [])
    results = {'local': [], 'social': []}

    # Separate local vs social
    local_cats = []
    social_platforms = []
    # social_push 已解耦为插件，经 PluginManager 获取实例（禁用则无社媒平台）
    import flask as _flask
    _pm = _flask.current_app.extensions.get('plugin_manager') if hasattr(_flask.current_app, 'extensions') else None
    _sp = _pm.get_instance('social_push') if (_pm and _pm.is_enabled('social_push')) else None
    _platform_info = _sp.PLATFORM_INFO if _sp else {}
    for ch in channels:
        if ch.startswith('local:'):
            local_cats.append(ch.split(':', 1)[1])
        elif ch in _platform_info:
            social_platforms.append(ch)

    # Local publish: update post record
    is_published_local = len(local_cats) > 0
    auto_pub = data.get('auto_publish', False)
    if is_published_local:
        upsert_post({
            'id': post_id,
            'slug': post['slug'],
            'category': local_cats[0],  # primary category
            'title': post['title'],
            'excerpt': post.get('excerpt', ''),
            'content': post['content'],
            'cover_image': post.get('cover_image', ''),
            'author': post.get('author', ''),
            'is_published': 1,
            'publish_channels': channels,
            'published_at': data.get('published_at', post.get('published_at')),
        })
        results['local'] = [{'channel': c, 'status': 'published'} for c in local_cats]
    else:
        # Still update publish_channels even if only social
        upsert_post({
            'id': post_id,
            'slug': post['slug'],
            'category': post['category'],
            'title': post['title'],
            'excerpt': post.get('excerpt', ''),
            'content': post['content'],
            'cover_image': post.get('cover_image', ''),
            'author': post.get('author', ''),
            'is_published': post['is_published'],
            'publish_channels': channels,
        })

    # Social publish
    for plat in social_platforms:
        result = _sp.publish_to_platform(
            platform=plat, title=post['title'] or '',
            body=post['content'] or '', body_html=post['content'] or '',
            summary=post.get('excerpt', '') or '',
            author=post.get('author', 'admin'),
            cover_image_url=post.get('cover_image', '') or '',
            auto_publish=auto_publish, admin_id=a['user_id'],
        )
        results['social'].append(result)

    # ── Hook: content published ──
    try:
        from plugin_manager.injectors import fire_hook
        fire_hook('cms/published', post_id=post_id,
                   channels=channels, admin_id=a['user_id'])
    except Exception:
        pass

    # ── 触发匹配的工作流（事件驱动，失败静默不影响发布） ──
    try:
        from orchestrator.trigger_dispatch import dispatch_event
        dispatch_event('cms.published', {
            'post_id': post_id,
            'category': post.get('category'),
            'source': post.get('source', 'manual'),
            'channels': channels,
        })
    except Exception:
        pass

    return _ok({'channels': channels, 'results': results})


# ── Categories ────────────────────────────────────────────

@cms_admin_bp.route('/categories', methods=['GET'])
def list_categories():
    a, e = _check()
    if e: return e
    return _ok(get_categories())


@cms_admin_bp.route('/categories', methods=['POST'])
def create_category():
    a, e = _check()
    if e: return e
    data = request.get_json(force=True)
    if not data.get('name'):
        return _err(_("Column Name cannot be empty"))
    return _ok(upsert_category(data))


@cms_admin_bp.route('/categories/<int:cat_id>', methods=['PUT'])
def update_category(cat_id):
    a, e = _check()
    if e: return e
    data = request.get_json(force=True)
    data['id'] = cat_id
    return _ok(upsert_category(data))


@cms_admin_bp.route('/categories/<int:cat_id>', methods=['DELETE'])
def remove_category(cat_id):
    a, e = _check()
    if e: return e
    from models import get_db
    with get_db() as conn:
        refs = conn.execute("SELECT COUNT(*) as c FROM cms_posts WHERE category IN "
                           "(SELECT name FROM cms_categories WHERE id=%s)", (cat_id,)).fetchone()
        if refs and refs['c'] > 0:
            return _err(_('This category has {count} articles, please migrate or delete them first').format(count=refs["c"]))
    delete_category(cat_id)
    return _ok({"deleted": cat_id})


@cms_admin_bp.route('/categories/reorder', methods=['POST'])
def reorder_cats():
    a, e = _check()
    if e: return e
    data = request.get_json(force=True)
    reorder_categories(data.get('ids', []))
    return _ok({"reordered": True})


# ── Settings ──────────────────────────────────────────────

@cms_admin_bp.route('/settings', methods=['GET'])
def list_settings():
    a, e = _check()
    if e: return e
    return _ok(get_all_settings())


@cms_admin_bp.route('/preview/<slug>', methods=['GET'])
def preview_post(slug):
    """预览文章（管理员可预览草稿）"""
    a, e = _check()
    if e: return e
    from models.cms import get_post_by_slug
    from models import get_db
    with get_db() as conn:
        post = conn.execute("SELECT * FROM cms_posts WHERE slug=%s", (slug,)).fetchone()
    post = dict(post) if post else None
    if not post:
        return _err(_('Article does not exist')), 404
    return f'''<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title}: {post_title}</title>
<style>body{{font-family:sans-serif;max-width:800px;margin:0 auto;padding:40px 20px;background:#fff;color:#222;line-height:1.8}}
.preview-banner{{background:#f0f8ff;border:1px solid #cce;padding:8px 16px;border-radius:6px;font-size:13px;color:#558;margin-bottom:24px;text-align:center}}
h1{{font-size:28px;margin-bottom:8px}}.meta{{color:#888;font-size:13px;margin-bottom:24px}}
img{{max-width:100%;border-radius:6px}}</style></head><body>
<div class="preview-banner">{banner}</div>
<h1>{post.get("title","")}</h1>
<div class="meta">{post.get("author","")} · {post.get("created_at","")[:10]}</div>
{post.get("content",_("<p>No content</p>"))}
</body></html>'''.format(
        title=_('Preview'),
        post_title=post.get("title",""),
        banner=_('Preview mode — admin only')
    )


@cms_admin_bp.route('/settings', methods=['PUT'])
def update_setting():
    a, e = _check()
    if e: return e
    data = request.get_json(force=True)
    set_setting(data['key'], data['value'])
    return _ok({"key": data['key']})
