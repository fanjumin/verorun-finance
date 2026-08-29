#!/usr/bin/env python3
"""main_site — Internal Service API (v2.1.0).

mini_app_builder 插件数据库解耦后，不再直连主库读取共享数据
（cms_posts / cms_blocks / 品牌设置 / draft tokens）。本蓝图向插件提供
只读内部 API，用 X-Internal-Token 保护（未配置 INTERNAL_SERVICE_TOKEN 时
默认仅信任本机来源，部署时应设置环境变量）。

前缀：/api/internal/*，仅供服务间调用，不对公网开放。
"""

import os
import json

from flask import Blueprint, jsonify, request

internal_api_bp = Blueprint('internal_api', __name__, url_prefix='/api/internal')


def _authorized() -> bool:
    # 审计 D4：未配置 INTERNAL_SERVICE_TOKEN 时默认拒绝（fail-closed），
    # 禁止在无鉴权情况下开放内部 API
    token = os.environ.get('INTERNAL_SERVICE_TOKEN', '')
    if not token:
        return False
    return request.headers.get('X-Internal-Token', '') == token


@internal_api_bp.before_request
def _guard():
    if not _authorized():
        return jsonify({'error': 'Forbidden'}), 403
    return None


@internal_api_bp.route('/brand')
def internal_brand():
    """品牌设置（site_name / tagline / colors / logo）。"""
    try:
        from services.brand_service import get_brand_settings
        return jsonify(get_brand_settings() or {})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/cms/pages')
def internal_cms_pages():
    """已发布页面列表（slug/title/meta）。"""
    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT slug, title, meta_description, updated_at FROM cms_posts "
                "WHERE status='published' AND post_type='page' ORDER BY sort_order ASC"
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/cms/page/<slug>')
def internal_cms_page(slug):
    """单个已发布页面（含 blocks）。"""
    try:
        from models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM cms_posts WHERE slug=%s AND status='published' "
                "AND post_type='page' LIMIT 1",
                (slug,)
            ).fetchone()
            if not row:
                return jsonify({'error': 'Page not found'}), 404
            page = dict(row)
            blocks = conn.execute(
                "SELECT * FROM cms_blocks WHERE post_id=%s AND status='published' "
                "ORDER BY sort_order ASC",
                (page['id'],)
            ).fetchall()
            page['blocks'] = [dict(b) for b in blocks]
        return jsonify(page)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/site/draft-tokens')
def internal_draft_tokens():
    """site_builder draft tokens（生成站点时使用）。

    v2.1.0 起 design_tokens 位于插件独立库 site_builder，
    直接读插件库（与插件同进程部署，经 plugins 顶层包导入）。
    """
    try:
        from plugins.site_builder.site_settings.models import get_draft_tokens
        return jsonify(get_draft_tokens() or {})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Site Builder CMS 内部端点（v1.0.0 解耦新增） ──────────────
# 供 plugins/site_builder 经 internal_client 读写主库 cms_blocks /
# cms_posts（草稿数据）。全部为草稿维度，生产数据不受影响。

_ALLOWED_BLOCK_FIELDS = {
    'title', 'subtitle', 'content', 'link_text', 'link_url',
    'image_url', 'icon', 'extra_json',
}


def _json_value(val):
    """dict/list → JSON 字符串；其余原样返回。"""
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    return val


def _safe_extra_json(val):
    """extra_json 兼容 dict/list/字符串输入。"""
    if isinstance(val, str):
        return val
    return json.dumps(val or {}, ensure_ascii=False)


@internal_api_bp.route('/cms/draft-blocks')
def internal_cms_draft_blocks():
    """草稿区块列表（is_published=0），可选按 page 过滤。"""
    page = request.args.get('page', '')
    try:
        from models import get_db
        with get_db() as conn:
            if page:
                rows = conn.execute(
                    "SELECT * FROM cms_blocks WHERE is_published=0 AND page=%s "
                    "ORDER BY page, position",
                    (page,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cms_blocks WHERE is_published=0 "
                    "ORDER BY page, position"
                ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/cms/draft-documents')
def internal_cms_draft_documents():
    """草稿法律文档列表（is_published=0 AND category='legal'）。"""
    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT slug, title, content FROM cms_posts "
                "WHERE is_published=0 AND category='legal'"
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/cms/page-blocks')
def internal_cms_page_blocks():
    """指定 page 的全部区块（含已发布），供 LLM 修改上下文。"""
    page = request.args.get('page', '')
    if not page:
        return jsonify({'error': 'page required'}), 400
    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM cms_blocks WHERE page=%s ORDER BY position",
                (page,)
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/cms/draft-blocks/replace', methods=['POST'])
def internal_cms_draft_blocks_replace():
    """幂等替换区块：清空目标 page（或全局）目标状态后重写。

    请求体：{page?, blocks: [], is_published?: 0|1}
    """
    data = request.get_json(force=True, silent=True) or {}
    page = data.get('page') or None
    blocks = data.get('blocks', [])
    if not isinstance(blocks, list):
        return jsonify({'error': 'blocks must be a list'}), 400
    is_published = 1 if data.get('is_published') else 0
    try:
        from models import get_db
        with get_db() as conn:
            if page:
                conn.execute(
                    "DELETE FROM cms_blocks WHERE page=%s AND is_published=%s",
                    (page, is_published)
                )
            else:
                conn.execute(
                    "DELETE FROM cms_blocks WHERE is_published=%s",
                    (is_published,)
                )
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                conn.execute(
                    "INSERT INTO cms_blocks "
                    "(page, section, block_type, position, title, subtitle, content, "
                    " image_url, link_url, link_text, icon, extra_json, is_published) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        b.get('page', page or 'home'),
                        b.get('section', 'main'),
                        b.get('block_type', 'text'),
                        b.get('position', 0),
                        b.get('title', ''),
                        b.get('subtitle', ''),
                        b.get('content', ''),
                        b.get('image_url', ''),
                        b.get('link_url', ''),
                        b.get('link_text', ''),
                        b.get('icon', ''),
                        _safe_extra_json(b.get('extra_json', {})),
                        is_published,
                    )
                )
            conn.commit()
        return jsonify({'ok': True, 'inserted': len(blocks)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/cms/blocks/update', methods=['POST'])
def internal_cms_block_update():
    """更新单个草稿区块字段（field 白名单校验）。"""
    data = request.get_json(force=True, silent=True) or {}
    block_id = data.get('block_id')
    field = data.get('field', '')
    value = data.get('value', '')
    if not block_id:
        return jsonify({'error': 'block_id required'}), 400
    if field not in _ALLOWED_BLOCK_FIELDS:
        return jsonify({'error': f'Field "{field}" is not editable'}), 400
    try:
        from models import get_db
        with get_db() as conn:
            if field == 'extra_json' and isinstance(value, dict):
                # 深度合并现有 extra_json（避免覆盖整字段）
                row = conn.execute(
                    "SELECT extra_json FROM cms_blocks "
                    "WHERE id=%s AND is_published=0",
                    (block_id,)
                ).fetchone()
                if not row:
                    return jsonify({'error': 'Block not found'}), 404
                try:
                    current = json.loads(row['extra_json'] or '{}')
                except (json.JSONDecodeError, TypeError):
                    current = {}
                if not isinstance(current, dict):
                    current = {}
                current.update(value)
                value = json.dumps(current, ensure_ascii=False)
            else:
                value = _json_value(value)
            conn.execute(
                f"UPDATE cms_blocks SET {field}=%s, updated_at=NOW() "
                "WHERE id=%s AND is_published=0",
                (value, block_id)
            )
            conn.commit()
        return jsonify({'ok': True, 'block_id': block_id, 'field': field})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/cms/blocks/order', methods=['POST'])
def internal_cms_block_order():
    """批量更新草稿区块排序。"""
    data = request.get_json(force=True, silent=True) or {}
    order = data.get('order', [])
    if not isinstance(order, list) or not order:
        return jsonify({'error': 'order must be a non-empty array'}), 400
    try:
        from models import get_db
        with get_db() as conn:
            for item in order:
                bid = item.get('block_id')
                pos = item.get('position')
                if bid is not None and pos is not None:
                    conn.execute(
                        "UPDATE cms_blocks SET position=%s, updated_at=NOW() "
                        "WHERE id=%s AND is_published=0",
                        (pos, bid)
                    )
            conn.commit()
        return jsonify({'ok': True, 'updated': len(order)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/cms/blocks/delete', methods=['POST'])
def internal_cms_block_delete():
    """软删除草稿区块（extra_json.deleted=true）。"""
    data = request.get_json(force=True, silent=True) or {}
    block_id = data.get('block_id')
    if not block_id:
        return jsonify({'error': 'block_id required'}), 400
    try:
        from models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT extra_json FROM cms_blocks "
                "WHERE id=%s AND is_published=0",
                (block_id,)
            ).fetchone()
            if not row:
                return jsonify({'error': 'Block not found'}), 404
            try:
                current = json.loads(row['extra_json'] or '{}')
            except (json.JSONDecodeError, TypeError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            current['deleted'] = True
            conn.execute(
                "UPDATE cms_blocks SET extra_json=%s, updated_at=NOW() "
                "WHERE id=%s AND is_published=0",
                (json.dumps(current, ensure_ascii=False), block_id)
            )
            conn.commit()
        return jsonify({'ok': True, 'block_id': block_id, 'deleted': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/cms/blocks/add', methods=['POST'])
def internal_cms_block_add():
    """在指定位置插入新区块（后移既有区块）。"""
    data = request.get_json(force=True, silent=True) or {}
    page = data.get('page', 'home')
    try:
        position = int(data.get('position', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'position must be an integer'}), 400
    block_type = data.get('block_type', 'feature-card')
    title = data.get('title', 'New Section')
    content = data.get('content', '')
    icon = data.get('icon', '')
    is_published = 1 if data.get('is_published') else 0
    try:
        from models import get_db
        with get_db() as conn:
            conn.execute(
                "UPDATE cms_blocks SET position=position+1 "
                "WHERE page=%s AND position>=%s AND is_published=%s",
                (page, position, is_published)
            )
            row = conn.execute(
                "INSERT INTO cms_blocks "
                "(page, position, block_type, title, content, icon, is_published, "
                " created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),NOW()) RETURNING id",
                (page, position, block_type, title, content, icon, is_published)
            ).fetchone()
            new_id = row['id'] if row else None
            conn.commit()
        return jsonify({'ok': True, 'block_id': new_id, 'page': page, 'position': position})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/cms/documents', methods=['POST'])
def internal_cms_document():
    """UPSERT 法律文档到 cms_posts（默认草稿 is_published=0）。"""
    data = request.get_json(force=True, silent=True) or {}
    slug = data.get('slug', '')
    title = data.get('title', '')
    content = data.get('content', '')
    if not slug:
        return jsonify({'error': 'slug required'}), 400
    is_published = 1 if data.get('is_published') else 0
    try:
        from models import get_db
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM cms_posts WHERE slug=%s AND category='legal'",
                (slug,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE cms_posts SET title=%s, content=%s, is_published=%s, "
                    "updated_at=NOW() WHERE id=%s",
                    (title, content, is_published, existing['id'])
                )
            else:
                conn.execute(
                    "INSERT INTO cms_posts "
                    "(slug, category, title, content, content_format, is_published, "
                    " created_at, updated_at) "
                    "VALUES (%s,'legal',%s,%s,'html',%s,NOW(),NOW())",
                    (slug, title, content, is_published)
                )
            conn.commit()
        return jsonify({'ok': True, 'slug': slug})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/cms/publish', methods=['POST'])
def internal_cms_publish():
    """发布草稿：cms_blocks / cms_posts 的 is_published 0→1。"""
    try:
        from models import get_db
        with get_db() as conn:
            conn.execute("UPDATE cms_blocks SET is_published=1 WHERE is_published=0")
            conn.execute("UPDATE cms_posts SET is_published=1 WHERE is_published=0")
            conn.commit()
        return jsonify({'ok': True, 'published': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_api_bp.route('/users/register-platform', methods=['POST'])
def internal_register_platform():
    """平台登录用户注册（get-or-create，联邦身份）。

    请求体：
        {
            "platform": "douyin|wechat|telegram|line",
            "platform_user_id": "...",
            "username": "wx_xxxx",
            "display_name": "...",
            "avatar": "..."
        }
    返回： {"id", "username", "display_name", "avatar"}

    供 mini_app_builder 插件（或未来跨服务调用方）经 X-Internal-Token 认证后调用。
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        platform = data.get('platform', '')
        platform_user_id = data.get('platform_user_id', '')
        username = data.get('username', '')
        display_name = data.get('display_name', '')
        avatar = data.get('avatar', '')
        if not platform or not platform_user_id or not username:
            return jsonify({'error': 'platform/platform_user_id/username required'}), 400

        from services.user_registry import register_or_get_platform_user
        user = register_or_get_platform_user(
            platform, platform_user_id, username, display_name, avatar)
        return jsonify(user)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
