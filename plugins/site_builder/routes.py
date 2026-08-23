#!/usr/bin/env python3
"""Site Builder — Flask Blueprint Routes

Endpoints: ~13
Prefix: /admin/site-builder/

v2.1.0 解耦说明：
  - 鉴权/审计自包含（_require_admin / _log），不再依赖 auth-center 路由层。
  - 主库 cms_blocks / cms_posts / 品牌设置改经 main_site 内部 API
    （internal_client）访问；design_tokens 等插件数据位于独立库。
"""

import os, json

from flask import Blueprint, request, jsonify

from services.jwt_service import validate_token
from i18n import _
import logging
logger = logging.getLogger(__name__)

site_builder_bp = Blueprint('site_builder', __name__, url_prefix='/admin/site-builder')


# ── Auth ───────────────────────────────────────────────

def _require_admin():
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        token = request.cookies.get('sso_token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return None, (jsonify({'success': False, 'error': _('Admin access required')}), 401)
    return payload, None


def _log(user_id, action, module, target, detail=''):
    """管理操作审计日志（插件自包含：记录到独立日志，不写主库 admin_log）。"""
    logger.info('[site_builder] audit user=%s action=%s module=%s target=%s detail=%s',
                user_id, action, module, target, detail)


def _success(data=None, message='ok'):
    return jsonify({'success': True, 'data': data, 'message': message})


def _error(message, code=400):
    return jsonify({'success': False, 'error': message}), code


# ── Prompt Template Management ─────────────────────────

@site_builder_bp.route('/prompts', methods=['GET'])
def list_prompts():
    """List all industry prompt templates"""
    admin, err = _require_admin()
    if err: return err

    from .models import list_prompts as _list
    active_only = request.args.get('active_only', '0') == '1'
    industry = request.args.get('industry', '')
    prompts = _list(active_only=active_only, industry=industry if industry else None)
    return _success(prompts)


@site_builder_bp.route('/prompts/<identifier>', methods=['GET'])
def get_prompt(identifier):
    """Get single prompt template details (with full prompt text)"""
    admin, err = _require_admin()
    if err: return err

    from .models import get_prompt as _get
    # Try to parse as id first
    try:
        pid = int(identifier)
        prompt = _get(pid)
    except ValueError:
        prompt = _get(identifier)

    if not prompt:
        return _error(_('Prompt template not found'), 404)
    return _success(prompt)


@site_builder_bp.route('/prompts', methods=['POST'])
def create_prompt():
    """Create custom prompt template"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    name = data.get('name', '').strip()
    if not name:
        return _error(_('Name cannot be empty'))

    from .models import create_prompt as _create
    new_id = _create({
        'identifier': data.get('identifier', ''),
        'name': name,
        'description': data.get('description', ''),
        'icon': data.get('icon', '📄'),
        'industry': data.get('industry', ''),
        'tags': data.get('tags', []),
        'defaults': data.get('defaults', {}),
        'pages': data.get('pages', []),
        'documents': data.get('documents', []),
        'prompts': data.get('prompts', {}),
        'created_by': admin['user_id'],
    })
    return _success({'id': new_id}, _('Created'))


@site_builder_bp.route('/prompts/<int:prompt_id>', methods=['PUT'])
def update_prompt(prompt_id):
    """Update prompt template"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    from .models import update_prompt as _update
    _update(prompt_id, data)
    return _success(message=_('Updated'))


@site_builder_bp.route('/prompts/<int:prompt_id>', methods=['DELETE'])
def delete_prompt(prompt_id):
    """Delete custom prompt template"""
    admin, err = _require_admin()
    if err: return err

    from .models import delete_prompt as _delete
    _delete(prompt_id)
    return _success(message=_('Deleted'))


# ── Site Building Flow ─────────────────────────────────

@site_builder_bp.route('/preview', methods=['POST'])
def preview_plan():
    """Generate site plan preview (no execution, returns plan only)"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    prompt_id = data.get('prompt_id') or data.get('prompt_identifier', '')
    user_input = data.get('message', '').strip()

    if not user_input:
        return _error(_('Message cannot be empty'))

    from .models import get_prompt as _get_prompt
    # Parse prompt_id
    try:
        prompt_id = int(prompt_id)
        prompt_template = _get_prompt(prompt_id)
    except (ValueError, TypeError):
        prompt_template = _get_prompt(prompt_id) if prompt_id else None

    if not prompt_template:
        return _error(_('No available prompt template'), 404)

    import logging, traceback
    logger = logging.getLogger(__name__)
    try:
        from .engine import SiteBuilderEngine
        engine = SiteBuilderEngine()

        # Phase 1: Parse requirement
        parsed = engine.parse_requirement(prompt_template, user_input)

        # Phase 2: Generate plan
        plan = engine.generate_plan(prompt_template, parsed, user_input)

        return _success({
            'parsed': parsed,
            'plan': plan,
            'summary': plan.get('summary', ''),
        })
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Site Builder preview failed: {e}\n{tb}")
        return jsonify({'success': False, 'error': f'Plan generation failed: {str(e)[:500]}'}), 500


@site_builder_bp.route('/execute', methods=['POST'])
def execute_build():
    """Execute build plan (write to draft)"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    plan = data.get('plan', {})
    prompt_id = data.get('prompt_id', '')
    user_input = data.get('message', '')

    if not plan:
        return _error('Plan data cannot be empty')

    from .models import get_prompt as _get_prompt
    try:
        prompt_id = int(prompt_id)
        prompt_template = _get_prompt(prompt_id)
    except (ValueError, TypeError):
        prompt_template = _get_prompt(prompt_id) if prompt_id else None

    if not prompt_template:
        return _error(_('No available prompt template'), 404)

    # Create task record
    from .models import create_task, update_task
    task_id = create_task(
        user_id=admin['user_id'],
        prompt_id=prompt_template.get('id', 0),
        user_input=user_input,
    )
    update_task(task_id, status='executing', current_step='Brand settings')

    try:
        from .engine import SiteBuilderEngine
        engine = SiteBuilderEngine()
        # Always write to draft first; use /site-builder/publish to make it live
        results = engine.execute_plan(plan, prompt_template, draft=True)

        update_task(task_id, status='completed', result_json=results)
        return _success({
            'task_id': task_id,
            'results': results,
            'summary': results.get('_summary', {}),
        }, _('Draft generated — preview and publish via the Publish button'))
    except Exception as e:
        import traceback
        traceback.print_exc()
        update_task(task_id, status='failed', error_message=str(e))
        return _error(_('Build execution failed') + f': {e}', 500)


# ── Publish Draft to Production ────────────────────────

@site_builder_bp.route('/publish', methods=['POST'])
def publish_draft():
    """Promote draft data to production (backup + publish)"""
    admin, err = _require_admin()
    if err: return err

    from .site_settings.models import (
        get_draft_tokens, promote_draft_tokens, backup_tokens
    )
    from .internal_client import get_draft_blocks, publish_draft as _publish_draft

    # 1. Check draft exists
    draft_tokens = get_draft_tokens()
    if draft_tokens is None:
        return _error('No draft to publish', 404)

    # 2. Check draft blocks exist（主库 cms_blocks，经内部 API）
    has_blocks = bool(get_draft_blocks())

    if not has_blocks:
        return _error('No draft content to publish (run /execute first)', 404)

    # 3. Backup current production
    try:
        backup_tokens()
    except Exception as e:
        logger.warning(f'Backup failed (non-critical): {e}')

    # 4. Save version snapshot before promoting
    from .site_settings.models import save_site_version
    version_info = save_site_version()

    # 5. Promote tokens: draft_json → token_json
    promote_draft_tokens()

    # 6. Promote blocks: is_published=0 → is_published=1（经内部 API）
    _publish_draft()

    return _success({
        'published': True,
        'version': version_info,
    }, 'Draft published to production')


# ── Get Draft Data (for preview) ───────────────────────

@site_builder_bp.route('/draft-data', methods=['GET'])
def get_draft_data():
    """Return all draft data for preview rendering"""
    admin, err = _require_admin()
    if err: return err

    from .site_settings.models import get_draft_tokens
    from .internal_client import get_draft_blocks, get_draft_documents

    draft_tokens = get_draft_tokens()
    if draft_tokens is None:
        return _error('No draft found', 404)

    # Get draft blocks grouped by page（经内部 API）
    blocks = {}
    for d in get_draft_blocks():
        page = d.get('page', 'home')
        if page not in blocks:
            blocks[page] = []
        blocks[page].append(d)

    # Get draft documents（经内部 API）
    documents = get_draft_documents()

    return _success({
        'tokens': draft_tokens,
        'blocks': blocks,
        'documents': documents,
    })


# ── Render Preview Page (iframe) ───────────────────────

@site_builder_bp.route('/preview-site', methods=['GET'])
def preview_site_page():
    """Render AI-generated draft site in an iframe-friendly page"""
    admin, err = _require_admin()
    if err: return err

    from .site_settings.models import get_draft_tokens
    from .internal_client import get_brand_settings, get_draft_blocks, get_draft_documents

    raw_tokens = get_draft_tokens()
    if raw_tokens is None:
        raw_tokens = {}

    # Ensure nested keys exist to prevent Jinja2 UndefinedError
    draft_tokens = {
        'brand': raw_tokens.get('brand', {
            'site_name': 'Site Name',
            'slogan': 'Welcome',
            'brand_story': '',
        }),
        'colors': raw_tokens.get('colors', {}),
        'typography': raw_tokens.get('typography', {}),
        'spacing': raw_tokens.get('spacing', {}),
        'navigation': raw_tokens.get('navigation', {'items': []}),
        'footer': raw_tokens.get('footer', {'copyright': '\u00a9 AI Generated Preview'}),
    }

    brand = get_brand_settings()

    # Get draft blocks（经内部 API）
    blocks = {}
    for d in get_draft_blocks():
        page = d.get('page', 'home')
        if page not in blocks:
            blocks[page] = []
        blocks[page].append(d)

    # Get draft documents（经内部 API）
    documents = get_draft_documents()

    from flask import render_template
    return render_template(
        'ai_site_preview.html',
        brand=brand,
        draft_tokens=draft_tokens,
        draft_blocks=blocks,
        draft_docs=documents,
        preview_mode=True,
    )


# ── Minimal Edit ───────────────────────────────────────

@site_builder_bp.route('/modify', methods=['POST'])
def modify_block():
    """Minimal edit: analyze user intent, locate block, execute modification"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    message = data.get('message', '').strip()
    page = data.get('page', 'home')

    if not message:
        return _error(_('Message cannot be empty'))

    try:
        from .engine import SiteBuilderEngine
        engine = SiteBuilderEngine()
        result = engine.modify_block(message, page)
        return _success(result, _('Modified') if result.get('success') else _('Could not locate block to modify'))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _error(str(e), 500)


# ── Task Management ────────────────────────────────────

@site_builder_bp.route('/tasks', methods=['GET'])
def list_tasks():
    """List build tasks"""
    admin, err = _require_admin()
    if err: return err

    from .models import list_tasks as _list
    limit = request.args.get('limit', 20, type=int)
    tasks = _list(user_id=admin['user_id'], limit=limit)
    return _success(tasks)


@site_builder_bp.route('/tasks/<task_id>', methods=['GET'])
def get_task(task_id):
    """Get task details"""
    admin, err = _require_admin()
    if err: return err

    from .models import get_task as _get
    task = _get(task_id)
    if not task:
        return _error('Task not found', 404)
    return _success(task)


# ── Page Summary (for LLM modification context) ────────

@site_builder_bp.route('/page-summary/<page>', methods=['GET'])
def page_summary(page):
    """Get page block summary"""
    admin, err = _require_admin()
    if err: return err

    from .generators.pages import PageGenerator
    summary = PageGenerator.get_page_summary(page)
    return _success(summary)


# ══════════════════════════════════════════════════════════════
# ── Draft Editor API (Preview-as-Editor) ────────────────────
# ══════════════════════════════════════════════════════════════


@site_builder_bp.route('/api/draft/update-block', methods=['POST'])
def update_draft_block():
    """Update a single draft block field (text edit, visibility, etc.)"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    block_id = data.get('block_id')
    field = data.get('field', '')
    value = data.get('value', '')
    scope = data.get('scope', 'block')  # 'block' | 'token'

    if not block_id:
        return _error('block_id is required')

    # Whitelist: allowed fields to update
    allowed_fields = {'title', 'subtitle', 'content', 'link_text', 'link_url', 'image_url', 'icon', 'extra_json'}
    if field not in allowed_fields:
        return _error(f'Field "{field}" is not editable')

    if scope == 'token':
        # Special block_id -> update design_tokens.draft_json
        from .site_settings.models import update_draft_token_field
        ok, tokens = update_draft_token_field(block_id, field, value)
        if not ok:
            return _error(f'Unknown token block_id: {block_id}')
        return _success({'block_id': block_id, 'field': field, 'value': value})
    else:
        # Numeric block_id -> update cms_blocks（经内部 API）
        from .internal_client import update_block as _update_block, get_draft_blocks
        if field == 'extra_json':
            # Merge extra_json (don't overwrite entire field)
            existing = None
            for b in get_draft_blocks():
                if str(b.get('id')) == str(block_id):
                    existing = b
                    break
            if not existing:
                return _error('Block not found', 404)
            current = json.loads(existing.get('extra_json') or '{}')
            if isinstance(value, dict):
                current.update(value)
            else:
                current = value
            value = json.dumps(current, ensure_ascii=False)

        _update_block(block_id, field, value)
        return _success({'block_id': block_id, 'field': field, 'value': value})


@site_builder_bp.route('/api/draft/update-block-order', methods=['POST'])
def update_draft_block_order():
    """Batch update block positions (drag-sort result)"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    order = data.get('order', [])  # [{block_id: 1, position: 0}, ...]

    if not order or not isinstance(order, list):
        return _error('order must be a non-empty array')

    from .internal_client import update_block_order as _update_order
    _update_order(order)

    return _success({'updated': len(order)})


@site_builder_bp.route('/api/draft/delete-block', methods=['POST'])
def delete_draft_block():
    """Soft-delete a draft block (set extra_json.deleted=true)"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    block_id = data.get('block_id')

    if not block_id:
        return _error('block_id is required')

    from .internal_client import delete_block as _delete_block, get_draft_blocks
    existing = None
    for b in get_draft_blocks():
        if str(b.get('id')) == str(block_id):
            existing = b
            break
    if not existing:
        return _error('Block not found', 404)

    _delete_block(block_id)
    return _success({'block_id': block_id, 'deleted': True})


@site_builder_bp.route('/api/draft/add-block', methods=['POST'])
def add_draft_block():
    """Insert a new block at a specified position"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    page = data.get('page', 'home')
    position = data.get('position', 0)
    block_type = data.get('block_type', 'feature-card')
    title = data.get('title', 'New Section')
    content = data.get('content', '')
    icon = data.get('icon', '')

    from .internal_client import add_block
    result = add_block(page=page, position=position, block_type=block_type,
                       title=title, content=content, icon=icon)
    new_id = result.get('block_id')

    return _success({'block_id': new_id, 'page': page, 'position': position})


@site_builder_bp.route('/api/draft/update-tokens', methods=['POST'])
def update_draft_tokens():
    """Update design tokens (colors/spacing/typography/navigation/footer)"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    scope = data.get('scope', '')  # colors | spacing | typography | navigation | footer
    new_data = data.get('data', {})

    allowed_scopes = {'colors', 'spacing', 'typography', 'navigation', 'footer'}
    if scope not in allowed_scopes:
        return _error(f'Invalid scope: {scope}')

    from .site_settings.models import get_draft_tokens, save_draft_tokens

    tokens = get_draft_tokens()
    if tokens is None:
        tokens = {}

    # Deep merge (preserve other scopes unchanged)
    if scope in tokens and isinstance(tokens[scope], dict):
        tokens[scope].update(new_data)
    else:
        tokens[scope] = new_data

    save_draft_tokens('platform', tokens)
    return _success({'scope': scope, 'updated': new_data})


@site_builder_bp.route('/api/draft/upload-image', methods=['POST'])
def upload_draft_image():
    """Upload a replacement image for a draft block"""
    admin, err = _require_admin()
    if err: return err

    if 'file' not in request.files:
        return _error('No file uploaded')

    file = request.files['file']
    block_id = request.form.get('block_id', '')
    field = request.form.get('field', 'image_url')

    # Validate file type
    allowed_ext = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg'}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_ext:
        return _error(f'Unsupported file type: {ext}')

    # Limit size to 5MB
    file.seek(0, 2)
    if file.tell() > 5 * 1024 * 1024:
        return _error('File too large (max 5MB)')
    file.seek(0)

    # Save file
    import uuid
    filename = f"{uuid.uuid4().hex}{ext}"
    upload_dir = os.path.join(
        os.path.dirname(__file__), '..', 'admin', 'static', 'uploads', 'draft'
    )
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)

    url = f"/static/uploads/draft/{filename}"

    # Update data
    if block_id and block_id.isdigit():
        from .internal_client import update_block
        update_block(int(block_id), 'image_url', url)
    else:
        # Update design_tokens (e.g. logo_url, favicon_url)
        from .site_settings.models import get_draft_tokens, save_draft_tokens
        tokens = get_draft_tokens() or {}
        tokens.setdefault('brand', {})[field] = url
        save_draft_tokens('platform', tokens)

    return _success({'url': url, 'block_id': block_id, 'field': field})


# ══════════════════════════════════════════════════════════════
# ── Site Version History API ──────────────────────────────────
# ══════════════════════════════════════════════════════════════


@site_builder_bp.route('/versions', methods=['GET'])
def list_versions():
    """List all site versions (newest first)."""
    admin, err = _require_admin()
    if err: return err

    from .site_settings.models import list_site_versions
    versions = list_site_versions()
    return _success({'versions': versions})


@site_builder_bp.route('/versions/<int:version_id>', methods=['GET'])
def get_version(version_id):
    """Get full version data (snapshot + blocks) for preview."""
    admin, err = _require_admin()
    if err: return err

    from .site_settings.models import get_site_version
    version = get_site_version(version_id)
    if not version:
        return _error('Version not found', 404)
    return _success(version)


@site_builder_bp.route('/versions/<int:version_id>/restore', methods=['POST'])
def restore_version(version_id):
    """Restore a version snapshot back to draft.
    
    Does NOT auto-publish. User can then edit and publish manually.
    """
    admin, err = _require_admin()
    if err: return err

    from .site_settings.models import restore_site_version
    ok = restore_site_version(version_id)
    if not ok:
        return _error('Version not found or restore failed', 404)
    return _success({'restored': version_id}, 'Version restored to draft. Edit and publish to make it live.')


# ══════════════════════════════════════════════════════════════
# 注：Mini-App 生成与部署（原 /mini-app/* 路由）已解耦至插件
# plugins/mini_app_builder（v2.0.0），本文件不再包含相关代码。
# ══════════════════════════════════════════════════════════════
