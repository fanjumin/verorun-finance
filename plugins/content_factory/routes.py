#!/usr/bin/env python3
"""Content Factory Plugin — 23 API 路由"""
from i18n import _
import sys, os, json

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request, jsonify
from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('content_factory')
cf_bp = Blueprint('content_factory', __name__, url_prefix='/admin/content-factory')


# ── Helpers ──
def _require_admin():
    from routes.admin import _require_admin as _ra
    return _ra()


def _log(admin_id, action, target_type='', target_id='', detail=''):
    from routes.admin import _log as _l
    _l(admin_id, action, target_type, target_id, detail)


def _get_db():
    from plugins.content_factory.models import get_cf_db
    return get_cf_db()


# ── 根路由：仪表盘统计 ──
@cf_bp.route('/', methods=['GET'])
def dashboard():
    admin, err = _require_admin()
    if err: return err
    with _get_db() as conn:
        source_count = conn.execute('SELECT COUNT(*) FROM content_sources WHERE is_active=1').fetchone()['count']
        pending = conn.execute("SELECT COUNT(*) FROM raw_contents WHERE status='pending'").fetchone()['count']
        processed = conn.execute("SELECT COUNT(*) FROM processed_contents").fetchone()['count']
        published = conn.execute("SELECT COUNT(*) FROM processed_contents WHERE is_published=1").fetchone()['count']
        failed = conn.execute("SELECT COUNT(*) FROM raw_contents WHERE status='failed'").fetchone()['count']
    return jsonify({'success': True, 'data': {
        'source_count': source_count, 'pending': pending,
        'processed': processed, 'published': published, 'failed': failed,
    }})


# =============================================
# 1. 来源管理 CRUD
# =============================================

@cf_bp.route('/sources', methods=['GET'])
def list_sources():
    admin, err = _require_admin()
    if err: return err
    with _get_db() as conn:
        rows = conn.execute('SELECT * FROM content_sources ORDER BY sort_order, id').fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@cf_bp.route('/sources', methods=['POST'])
def add_source():
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    required = ['name', 'source_type', 'url']
    for k in required:
        if not d.get(k):
            return jsonify({'success': False, 'error': f'{k} Required'}), 400
    with _get_db() as conn:
        cur = conn.execute(
            """INSERT INTO content_sources (name, source_type, platform, url, config_json,
               crawl_interval, keywords, max_per_run, created_by)
               VALUES (?,?,?,?,?,?,?,?,?) RETURNING id""",
            (d['name'], d['source_type'], d.get('platform', ''),
             d['url'], json.dumps(d.get('config', {}), ensure_ascii=False),
             int(d.get('crawl_interval', 0)),
             d.get('keywords', ''),
             int(d.get('max_per_run', 10)),
             admin['user_id'])
        )
        conn.commit()
        sid = cur.fetchone()['id']
    _log(admin['user_id'], 'cf_source_add', 'content_source_', str(sid), f"Source: {d['name']}")
    return jsonify({'success': True, 'id': sid})


@cf_bp.route('/sources/<int:sid>', methods=['PUT'])
def update_source(sid):
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    fields = ['name', 'source_type', 'platform', 'url', 'crawl_interval',
              'keywords', 'max_per_run', 'is_active', 'sort_order']
    sets = []
    vals = []
    for k in fields:
        if k in d:
            sets.append(f'{k}=?')
            vals.append(d[k])
    if not sets:
        return jsonify({'success': False, 'error': _('No Update Fields')})
    sets.append("config_json=?")
    vals.append(json.dumps(d.get('config', {}), ensure_ascii=False))
    vals.append(sid)
    with _get_db() as conn:
        conn.execute(f"UPDATE content_sources SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
    _log(admin['user_id'], 'cf_source_update', 'content_source', str(sid))
    return jsonify({'success': True})


@cf_bp.route('/sources/<int:sid>', methods=['DELETE'])
def delete_source(sid):
    admin, err = _require_admin()
    if err: return err
    with _get_db() as conn:
        conn.execute('DELETE FROM content_sources WHERE id=?', (sid,))
        conn.commit()
    _log(admin['user_id'], 'cf_source_delete', 'content_source', str(sid))
    return jsonify({'success': True})


# =============================================
# 2. 采集执行
# =============================================

@cf_bp.route('/crawl', methods=['POST'])
def trigger_crawl():
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    source_id = d.get('source_id')
    if not source_id:
        return jsonify({'success': False, 'error': _('Source_id is required')})
    from plugins.content_factory.services import run_collection
    result = run_collection(source_id, admin_id=admin['user_id'])
    _log(admin['user_id'], 'cf_crawl', 'content_source', str(source_id),
         f"Added {result.get('inserted',0)}, Skipped {result.get('skipped',0)}")
    return jsonify(result)


# =============================================
# 3. 原始内容列表
# =============================================

@cf_bp.route('/contents', methods=['GET'])
def list_contents():
    admin, err = _require_admin()
    if err: return err
    source_id = request.args.get('source_id')
    status = request.args.get('status', '')
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 20))
    offset = (page - 1) * limit

    where = ['1=1']
    params = []
    if source_id:
        where.append('r.source_id=?')
        params.append(int(source_id))
    if status:
        where.append('r.status=?')
        params.append(status)

    with _get_db() as conn:
        total = conn.execute(
            f'SELECT COUNT(*) FROM raw_contents r WHERE {" AND ".join(where)}', params
        ).fetchone()['count']
        rows = conn.execute(
            f"""SELECT r.*, s.name as source_name
                FROM raw_contents r LEFT JOIN content_sources s ON r.source_id=s.id
                WHERE {" AND ".join(where)}
                ORDER BY r.id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset]
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows],
                    'total': total, 'page': page, 'limit': limit})


@cf_bp.route('/contents/<int:rid>', methods=['DELETE'])
def delete_content(rid):
    admin, err = _require_admin()
    if err: return err
    with _get_db() as conn:
        conn.execute('DELETE FROM raw_contents WHERE id=?', (rid,))
        conn.execute('DELETE FROM processed_contents WHERE raw_id=?', (rid,))
        conn.commit()
    _log(admin['user_id'], 'cf_delete', 'raw_content', str(rid))
    return jsonify({'success': True})


# =============================================
# 4. AI 加工
# =============================================

@cf_bp.route('/process', methods=['POST'])
def process():
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    raw_ids = d.get('raw_ids', [])
    if not raw_ids:
        return jsonify({'success': False, 'error': _('Raw_ids are required')})
    from plugins.content_factory.services.ai_processor import batch_process
    result = batch_process(raw_ids, admin_id=admin['user_id'])
    _log(admin['user_id'], 'cf_process', '', '',
         f"Process {len(raw_ids)} Items: OK={result.get('ok',0)} FAIL={result.get('fail',0)}")
    return jsonify(result)


# =============================================
# 5. 加工内容列表
# =============================================

@cf_bp.route('/processed', methods=['GET'])
def list_processed():
    admin, err = _require_admin()
    if err: return err
    status = request.args.get('status', '')
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 20))
    offset = (page - 1) * limit

    where = ['1=1']
    params = []
    if status:
        where.append('p.status=?')
        params.append(status)

    with _get_db() as conn:
        total = conn.execute(
            f'SELECT COUNT(*) FROM processed_contents p WHERE {" AND ".join(where)}', params
        ).fetchone()['count']
        rows = conn.execute(
            f"""SELECT p.*, r.title as raw_title, r.source_url
                FROM processed_contents p
                LEFT JOIN raw_contents r ON p.raw_id=r.id
                WHERE {" AND ".join(where)}
                ORDER BY p.id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset]
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows],
                    'total': total, 'page': page, 'limit': limit})


@cf_bp.route('/processed/batch-delete', methods=['POST'])
def batch_delete_processed():
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    ids = d.get('ids', [])
    if not ids:
        return jsonify({'success': False, 'error': _('Ids are required')})
    with _get_db() as conn:
        for pid in ids:
            conn.execute('DELETE FROM skill_pushes WHERE processed_id=?', (pid,))
            conn.execute('DELETE FROM processed_contents WHERE id=?', (pid,))
        conn.commit()
    _log(admin['user_id'], 'cf_batch_delete', 'processed', f'{len(ids)} items')
    return jsonify({'success': True, 'deleted': len(ids)})


# =============================================
# 6. AI 排版 + 配图
# =============================================

@cf_bp.route('/ai-format', methods=['POST'])
def ai_format():
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    content = d.get('content', '')
    title = d.get('title', '')
    if not content.strip():
        return jsonify({'success': False, 'error': _('Content cannot be empty')})
    try:
        try:
            from services.ai_content_generator import _qwen_chat
        except ImportError:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
            from services.ai_content_generator import _qwen_chat
    except ImportError:
        return jsonify({'success': False, 'error': _('AI Layout Module Not Ready (ai_content_generator Not Available)')}), 503
    prompt = f"""你是一个专业的内容排版编辑。请仔细阅读全文，然后执行以下步骤：

## 任务
1. **修复排版错误**：纠正缩进、标点符号(全角/半角混用)、段落分段、多余换行
2. **重新组织结构**：用 <h2> 或 <h3> 划分章节，每章之间用 <p> 段落，列表用 <ul><li>
3. **数据突出**：重要数字、百分比、日期用 <strong> 加粗
4. **生成摘要**：用 <blockquote> 包裹一句话摘要放在正文开头
5. **配图建议**：在正文末尾添加 <p class="cover-suggest">配图建议：xxx</p>

输出纯 HTML，不要用 markdown。段落分明，每段之间空行。保持原文意思完整不变。不要丢失任何原文内容。

原文标题：{title}
原文正文：
{content[:8000]}"""
    try:
        result = _qwen_chat([{'role': 'user', 'content': prompt}], temperature=0.3)
        _log(admin['user_id'], 'cf_ai_format', '', '', f'AI Formatting: {title[:30]}')
        return jsonify({'success': True, 'formatted': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@cf_bp.route('/ai-cover', methods=['POST'])
def ai_cover():
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    title = d.get('title', '')
    topic = d.get('topic', '')
    prompt_text = d.get('prompt', '')
    if not prompt_text:
        prompt_text = f'Tech Finance Cover Image: {topic or title}, Dark Sci-Fi Style, Blue-Purple Gradient'
    try:
        try:
            from services.ai_content_generator import generate_image
        except ImportError:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
            from services.ai_content_generator import generate_image
    except ImportError:
        return jsonify({'success': False, 'error': _('AI image generation module is not ready (ai_content_generator is unavailable)')}), 503
    try:
        url = generate_image(prompt_text, size='1280x720')
        _log(admin['user_id'], 'cf_ai_cover', '', '', f'Illustration: {title[:30]}')
        return jsonify({'success': True, 'image_url': url})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# =============================================
# 7. 编辑加工内容
# =============================================

@cf_bp.route('/processed/<int:pid>', methods=['GET'])
def get_processed(pid):
    admin, err = _require_admin()
    if err: return err
    with _get_db() as conn:
        row = conn.execute(
            """SELECT p.*, r.title as raw_title, r.source_url, r.content_text as raw_text
               FROM processed_contents p LEFT JOIN raw_contents r ON p.raw_id=r.id
               WHERE p.id=?""",
            (pid,)
        ).fetchone()
    if not row:
        return jsonify({'success': False, 'error': _('Does not exist')})
    return jsonify({'success': True, 'data': dict(row)})


@cf_bp.route('/processed/<int:pid>', methods=['PUT'])
def update_processed(pid):
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    fields = ['title', 'summary', 'body', 'keywords', 'risk_level', 'status']
    sets = []
    vals = []
    for k in fields:
        if k in d:
            sets.append(f'{k}=?')
            vals.append(d[k])
    if sets:
        vals.append(pid)
        with _get_db() as conn:
            conn.execute(f"UPDATE processed_contents SET {', '.join(sets)} WHERE id=?", vals)
            conn.commit()
    return jsonify({'success': True})


# =============================================
# 8. 审核流程
# =============================================

@cf_bp.route('/review', methods=['POST'])
def review_content():
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    pid = d.get('processed_id')
    action = d.get('action', '')
    if not pid or action not in ('submit_review', 'approve', 'reject', 'back_to_draft'):
        return jsonify({'success': False, 'error': _('Processed_id and action are required')})

    status_map = {'submit_review': 'review', 'approve': 'approved', 'reject': 'rejected', 'back_to_draft': 'draft'}
    target = status_map[action]

    with _get_db() as conn:
        pc = conn.execute('SELECT * FROM processed_contents WHERE id=?', (pid,)).fetchone()
        if not pc:
            return jsonify({'success': False, 'error': _('Does not exist')})
        cur = pc['status']
        valid_transitions = {
            'draft': ['submit_review', 'publish'],
            'review': ['approve', 'reject'],
            'rejected': ['submit_review', 'back_to_draft'],
            'approved': ['publish', 'back_to_draft'],
            'published': [],
        }
        if action not in valid_transitions.get(cur, []):
            return jsonify({'success': False, 'error': f'Status {cur} does not allow {action}'}), 400

        conn.execute(
            "UPDATE processed_contents SET status=?, reviewed_by=?, reviewed_at=NOW() WHERE id=?",
            (target, admin['user_id'], pid)
        )
        conn.commit()

    action_labels = {'submit_review': _('Submit for Review'), 'approve': _('Approved'), 'reject': _('Reject'), 'back_to_draft': _('Return to draft')}
    _log(admin['user_id'], f'cf_review_{action}', 'processed_content', str(pid),
         f'{action_labels[action]}: {pc["title"][:50]}')
    return jsonify({'success': True, 'status': target})


# =============================================
# 9. 发布 (内部 → CMS 文章)
# =============================================

@cf_bp.route('/publish', methods=['POST'])
def publish():
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    pid = d.get('processed_id')
    platform = d.get('platform', 'internal')
    if not pid:
        return jsonify({'success': False, 'error': _('Processed_id is required')})

    with _get_db() as conn:
        pc = conn.execute('SELECT * FROM processed_contents WHERE id=?', (pid,)).fetchone()
    if not pc:
        return jsonify({'success': False, 'error': _('Processed Content Does Not Exist')})
    if pc['status'] not in ('approved', 'draft'):
        return jsonify({'success': False, 'error': f'Current status {pc["status"]} does not allow publishing (needs approved or draft)'})

    if platform == 'internal':
        try:
            from models.cms import upsert_post
        except ImportError:
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
                from models.cms import upsert_post
            except ImportError:
                return jsonify({'success': False, 'error': _('CMS publishing module is not ready (models.cms is not available)')}), 503
        import time
        slug = f'cf-{pid}-{int(time.time())}'
        post = upsert_post({
            'slug': slug,
            'category': 'content_factory',
            'title': pc['title'] or f"内容工厂#{pid}",
            'excerpt': pc['summary'] or '',
            'content': pc['body'] or '',
            'cover_image': pc['image_url'] or '',
            'author': f'admin_{admin["display_name"]}',
            'is_published': 1,
            'source': 'factory',
            'source_id': pid,
        })
        post_id = post.get('id')
        conn.execute(
            "UPDATE processed_contents SET is_published=1, status='published' WHERE id=?", (pid,)
        )
        conn.commit()
        _log(admin['user_id'], 'cf_publish', 'processed_content', str(pid), f"Publish to this site post_id={post_id}")
        return jsonify({'success': True, 'post_id': post_id, 'platform': 'internal'})

    elif platform in ('social', 'both'):
        # social_push 已解耦为插件，经 PluginManager 获取实例调用（禁用则降级）
        import flask as _flask
        _pm = _flask.current_app.extensions.get('plugin_manager') if hasattr(_flask.current_app, 'extensions') else None
        _sp = _pm.get_instance('social_push') if (_pm and _pm.is_enabled('social_push')) else None
        if _sp is None:
            return jsonify({'success': False, 'error': _('Social Media Posting Module Not Ready (social_push plugin not enabled)')}), 503
        social_platforms = d.get('social_platforms', ['wechat'])
        auto_publish = d.get('auto_publish', False)
        social_results = []
        for sp in social_platforms:
            result = _sp.publish_to_platform(
                platform=sp, title=pc['title'] or '', body=pc['body'] or '',
                body_html=pc.get('body_html', '') or pc['body'] or '',
                summary=pc['summary'] or '', author=f'admin_{admin["display_name"]}',
                cover_image_url=pc['image_url'] or '', auto_publish=auto_publish,
                admin_id=admin['user_id'],
            )
            social_results.append(result)

        post_id = None
        if platform == 'both':
            try:
                from models.cms import upsert_post
            except ImportError:
                try:
                    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
                    from models.cms import upsert_post
                except ImportError:
                    return jsonify({'success': False, 'error': _('CMS publishing module is not ready (models.cms is not available)')}), 503
            import time
            slug = f'cf-{pid}-{int(time.time())}'
            post = upsert_post({
                'slug': slug, 'category': 'content_factory', 'title': pc['title'] or f"内容工厂#{pid}",
                'excerpt': pc['summary'] or '', 'content': pc['body'] or '',
                'cover_image': pc['image_url'] or '', 'author': f'admin_{admin["display_name"]}',
                'is_published': 1, 'source': 'factory', 'source_id': pid,
            })
            post_id = post.get('id')

        with _get_db() as conn:
            conn.execute(
                "UPDATE processed_contents SET is_published=1, status='published' WHERE id=?", (pid,)
            )
            conn.commit()

        log_msg = f"Social Media Post: {', '.join(social_platforms)}"
        if post_id: log_msg += f", CMS post_id={post_id}"
        _log(admin['user_id'], 'cf_publish_social', 'processed_content', str(pid), log_msg)

        resp = {'success': True, 'platform': platform, 'social_results': social_results}
        if post_id: resp['post_id'] = post_id
        return jsonify(resp)
    else:
        return jsonify({'success': False, 'error': f'Unknown publishing platform: {platform}'}), 400


# =============================================
# 10. 任务列表
# =============================================

@cf_bp.route('/tasks', methods=['GET'])
def list_tasks():
    admin, err = _require_admin()
    if err: return err
    source_id = request.args.get('source_id')
    limit = int(request.args.get('limit', 20))
    where = ['1=1']
    params = []
    if source_id:
        where.append('t.source_id=?')
        params.append(int(source_id))
    with _get_db() as conn:
        rows = conn.execute(
            f"""SELECT t.*, s.name as source_name
                FROM content_tasks t LEFT JOIN content_sources s ON t.source_id=s.id
                WHERE {" AND ".join(where)}
                ORDER BY t.id DESC LIMIT ?""",
            params + [limit]
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


# =============================================
# 11. 仪表盘统计
# =============================================

@cf_bp.route('/stats', methods=['GET'])
def stats():
    admin, err = _require_admin()
    if err: return err
    with _get_db() as conn:
        source_count = conn.execute('SELECT COUNT(*) FROM content_sources WHERE is_active=1').fetchone()['count']
        pending = conn.execute("SELECT COUNT(*) FROM raw_contents WHERE status='pending'").fetchone()['count']
        processed = conn.execute("SELECT COUNT(*) FROM processed_contents").fetchone()['count']
        published = conn.execute("SELECT COUNT(*) FROM processed_contents WHERE is_published=1").fetchone()['count']
        failed = conn.execute("SELECT COUNT(*) FROM raw_contents WHERE status='failed'").fetchone()['count']
        recent_sources = conn.execute(
            'SELECT name, last_crawled_at FROM content_sources ORDER BY last_crawled_at DESC LIMIT 5'
        ).fetchall()
    return jsonify({'success': True, 'data': {
        'source_count': source_count, 'pending': pending, 'processed': processed,
        'published': published, 'failed': failed,
        'recent_sources': [dict(r) for r in recent_sources],
    }})


# =============================================
# 12. Skill 推送
# =============================================

@cf_bp.route('/push-skill', methods=['POST'])
def push_to_skill():
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    pid = d.get('processed_id')
    target = d.get('target_agent', 'hermes')
    if not pid:
        return jsonify({'success': False, 'error': _('Processed_id is required')})
    from plugins.content_factory.services.skill_pusher import push_to_skill as do_push
    result = do_push(pid, admin_id=admin['user_id'], target_agent=target)
    if result['success']:
        _log(admin['user_id'], 'cf_skill_push', 'processed_content', str(pid),
             f"Push to {target}: {result['skill_name']}")
    return jsonify(result)


@cf_bp.route('/pushed-skills', methods=['GET'])
def list_pushed():
    admin, err = _require_admin()
    if err: return err
    from plugins.content_factory.services.skill_pusher import list_pushed_skills
    skills = list_pushed_skills()
    return jsonify({'success': True, 'data': skills})


@cf_bp.route('/pushed-skills/<int:push_id>', methods=['DELETE'])
def delete_pushed(push_id):
    admin, err = _require_admin()
    if err: return err
    with _get_db() as conn:
        conn.execute("DELETE FROM skill_pushes WHERE id=?", (push_id,))
        conn.commit()
    _log(admin['user_id'], 'cf_skill_delete', 'skill_push', str(push_id))
    return jsonify({'success': True})


# =============================================
# 13. 用户端拉取 Skill API (无认证)
# =============================================

@cf_bp.route('/api/v1/skills', methods=['GET'])
def api_list_skills():
    agent = request.args.get('agent', 'hermes')
    from plugins.content_factory.services.skill_pusher import list_pushed_skills
    skills = list_pushed_skills(limit=50, target_agent=agent)
    return jsonify({
        'success': True, 'agent': agent, 'count': len(skills),
        'skills': [{
            'id': s['id'], 'skill_name': s['skill_name'], 'title': s['title'],
            'description': s['description'], 'category': s['skill_category'],
            'version': s['skill_version'], 'pushed_at': s['last_pushed_at'],
        } for s in skills],
    })


@cf_bp.route('/api/v1/skills/<int:push_id>/download', methods=['GET'])
def api_download_skill(push_id):
    from plugins.content_factory.services.skill_pusher import get_skill_for_download
    skill = get_skill_for_download(push_id)
    if not skill:
        return jsonify({'success': False, 'error': _('Does not exist')}), 404
    return jsonify({'success': True, 'skill': skill})


# =============================================
# 14. 静态页面生成
# =============================================

@cf_bp.route('/generate-static', methods=['POST'])
def generate_static():
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    action = d.get('action', 'post')
    slug = d.get('slug', '')

    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'platform'))
        try:
            from staticgen import generate_post, generate_all, generate_category, generate_docs_index
        except ImportError:
            return jsonify({'success': False, 'error': _('Static page generation module is not ready (platform/staticgen not available)')}), 503

        results = []
        if action == 'all':
            results = generate_all()
        elif action == 'category' and d.get('cat_slug'):
            r = generate_category(d['cat_slug'])
            results = [r]
        elif action == 'docs_index':
            r = generate_docs_index()
            results = [r]
        elif slug:
            r = generate_post(slug)
            results = [r]
        else:
            return jsonify({'success': False, 'error': '请指定 slug 或 action=all'}), 400

        ok = sum(1 for r in results if r.get('ok'))
        fail = sum(1 for r in results if not r.get('ok'))
        _log(admin['user_id'], 'cf_static_gen', '', '', f"{action}: {ok} ok, {fail} fail")
        return jsonify({'success': True, 'action': action, 'ok': ok, 'fail': fail,
                        'results': [{'path': r.get('path', ''), 'ok': r.get('ok', False),
                                     'error': r.get('error', '')} for r in results]})
    except Exception as e:
        logger.exception("Static generation failed")
        return jsonify({'success': False, 'error': str(e)})


@cf_bp.route('/push-to-knowledge', methods=['POST'])
def push_processed_to_knowledge():
    admin, err = _require_admin()
    if err: return err
    d = request.get_json() or {}
    pid = d.get('processed_id')
    if not pid:
        return jsonify({'success': False, 'error': _('Processed_id is required')}), 400

    with _get_db() as conn:
        row = conn.execute("SELECT id, title, body, keywords, content_type "
                           "FROM processed_contents WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify({'success': False, 'error': _('Processed Content Does Not Exist')}), 404

    raw = f"标题：{row['title'] or ''}\n关键词：{row['keywords'] or ''}\n类型：{row['content_type'] or ''}\n正文：{row['body'] or ''}"
    try:
        from routes.cleaner_agent import process_clean_content
    except ImportError:
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
            from routes.cleaner_agent import process_clean_content
        except ImportError:
            return jsonify({'success': False, 'error': _('Knowledge base push module is not ready (cleaner_agent is not available)')}), 503
    result = process_clean_content(raw, admin_id=admin['user_id'])
    _log(admin['user_id'], 'cf_to_knowledge', 'processed_content', str(pid),
         f"Knowledge Base ID: {result.get('kb_id', '?')}")
    return jsonify(result)


# =============================================
# 15. 定时采集 Tick 端点
# =============================================

@cf_bp.route('/cron/tick', methods=['POST'])
def cron_tick():
    secret = request.headers.get('X-Cron-Secret', '')
    if secret != os.environ.get('CRON_SECRET', ''):
        return jsonify({'error': 'Unauthorized'}), 403
    from datetime import datetime
    try:
        now = datetime.now()
        triggered = []
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT id, name, crawl_interval, last_crawled_at, is_active "
                "FROM content_sources WHERE is_active=1 AND crawl_interval>0"
            ).fetchall()
        for r in rows:
            last = r['last_crawled_at']
            last_dt = None
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                except (ValueError, TypeError):
                    last_dt = None
            if last_dt is None or (now - last_dt).total_seconds() >= r['crawl_interval']:
                triggered.append(r['id'])

        from plugins.content_factory.services import run_collection
        results = []
        for sid in triggered:
            try:
                run_collection(source_id=sid)
                results.append({'source_id': sid, 'status': 'triggered'})
            except Exception as e:
                results.append({'source_id': sid, 'status': 'error', 'error': str(e)})
        return jsonify({'success': True, 'checked': len(rows), 'triggered': len(triggered), 'results': results})
    except Exception as e:
        logger.exception('cron_tick failed')
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================
# 16. 调度配置与定时采集状态（管理员视图）
# =============================================

@cf_bp.route('/schedules', methods=['GET'])
def list_schedules():
    """列出所有已配置定时采集的源及其调度信息"""
    admin, err = _require_admin()
    if err: return err

    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, source_type, url, crawl_interval, "
            "is_active, last_crawled_at, auto_publish, skip_review, keywords, max_per_run "
            "FROM content_sources WHERE is_active = 1 AND crawl_interval > 0 "
            "ORDER BY name"
        ).fetchall()
    return jsonify({
        'success': True,
        'data': [dict(r) for r in rows]
    })


@cf_bp.route('/cron', methods=['GET'])
def cron_status():
    """查看定时采集任务的历史执行记录"""
    admin, err = _require_admin()
    if err: return err

    with _get_db() as conn:
        rows = conn.execute(
            "SELECT t.*, s.name as source_name "
            "FROM content_tasks t LEFT JOIN content_sources s ON t.source_id=s.id "
            "ORDER BY t.id DESC LIMIT ?",
            [50]
        ).fetchall()
    return jsonify({
        'success': True,
        'data': [dict(r) for r in rows]
    })


# ─── PluginManager 标准化配置 ─────────────────────────────────────────

_CF_CONFIG_KEYS = ['dashscope_text_key', 'max_items_per_run', 'skip_review', 'auto_publish']

_CF_DEFAULTS = {
    'dashscope_text_key': '',
    'max_items_per_run': 10,
    'skip_review': False,
    'auto_publish': False,
}


def _get_cf_pm():
    import flask
    try:
        return flask.current_app.extensions.get('plugin_manager')
    except Exception:
        return None


@cf_bp.route('/settings', methods=['GET'])
def cf_settings_get():
    admin, err = _require_admin()
    if err: return err
    pm = _get_cf_pm()
    if not pm:
        return jsonify({'success': False, 'error': 'PluginManager not available'}), 503
    cfg = pm.get_config('content_factory') or {}
    result = {}
    for k in _CF_CONFIG_KEYS:
        v = cfg.get(k)
        if v is not None:
            result[k] = v
        else:
            result[k] = _CF_DEFAULTS.get(k)
    return jsonify({'success': True, 'data': result})


@cf_bp.route('/settings', methods=['POST'])
def cf_settings_save():
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    pm = _get_cf_pm()
    if not pm:
        return jsonify({'success': False, 'error': 'PluginManager not available'}), 503
    filtered = {}
    for k, v in data.items():
        if k in _CF_CONFIG_KEYS:
            if k in ('max_items_per_run',):
                try:
                    filtered[k] = int(v)
                except (ValueError, TypeError):
                    return jsonify({'success': False, 'error': f'{k} must be integer'}), 400
            elif k in ('skip_review', 'auto_publish'):
                if isinstance(v, str):
                    filtered[k] = v.lower() in ('1', 'true', 'yes')
                else:
                    filtered[k] = bool(v)
            else:
                filtered[k] = str(v) if v is not None else ''
    if not filtered:
        return jsonify({'success': False, 'error': 'No valid config keys provided'}), 400
    result = pm.set_config_batch('content_factory', filtered, coerce=True)
    if result.get('errors'):
        return jsonify({'success': True, 'warning': str(result['errors'])})
    return jsonify({'success': True})