#!/usr/bin/env python3
"""Social Push Plugin — /admin/social/* 多平台社媒内容发布路由

迁移自 auth-center/routes/social_push.py（Phase 2 物理解耦）。
- 发布日志读写：插件独立 PG schema `social_push`（get_sp_db）
- 主库只读（cms_posts / system_config）：get_main_db
- 发布 services：经 auth-center sys.path 复用

LLM 说明（Phase 3）：
  AI 文案与 AI 配图走全站【公共 LLM 服务】services.ai_content_generator（配图经统一 AI 网关），
  而非本插件私有能力，也不属于_("Publishing Platform")。
  agent_matrix 内核自身亦依赖该公共服务，故不下沉、不搬动，保持共享。
  概念上：AI = 创作工具（ai_capabilities），社媒号 = 发布渠道（platforms），二者分离。
"""

from i18n import _
import sys, os, json, logging

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request, jsonify, current_app

from .models import get_sp_db

logger = logging.getLogger(__name__)

social_bp = Blueprint('social', __name__, url_prefix='/admin/social')

# 仅"发布渠道"——真实社媒平台。AI 能力不在此列（见文件顶部说明）。
PLATFORM_INFO = {
    'wechat': {'name': _('WeChat Official Account'), 'icon': '💬'},
    'weibo':  {'name': _('Weibo'),       'icon': '📢'},
    'toutiao':{'name': _('Toutiao'),    'icon': '📰'},
    'twitter': {'name': _('X (Twitter)'),   'icon': '𝕏'},
    'linkedin':{'name': _('LinkedIn'),      'icon': 'in'},
    'reddit':  {'name': _('Reddit'),        'icon': 'rd'},
    'telegram':{'name': _('Telegram Channel'), 'icon': '✈'},
}


# ── Helpers ──
def _require_admin():
    from routes.admin import _require_admin as _ra
    return _ra()


def _log(admin_id, action, target_type='', target_id='', detail=''):
    from routes.admin import _log as _l
    _l(admin_id, action, target_type, target_id, detail)


def _get_main_db():
    """主库只读连接（system_config / cms_posts）"""
    from models import get_db
    return get_db()


# =============================================
# 配置检测
# =============================================

def _get_market() -> str:
    """Return current market: 'cn' or 'intl'."""
    return os.environ.get('DEPLOY_MARKET', 'cn')


def _get_international_providers(market: str, configs: dict = None) -> list:
    """Return international platform list filtered by market.

    CN users only see domestic platforms; intl users see all international ones.
    """
    try:
        from .providers import get_provider_info
        return get_provider_info(market, configs)
    except Exception:
        return {}


# ── Config fields needed from system_config for each international provider ──
_INTERNATIONAL_CONFIG_KEYS = [
    'twitter_api_key', 'twitter_api_secret', 'twitter_access_token',
    'twitter_access_secret', 'twitter_bearer_token',
    'linkedin_client_id', 'linkedin_client_secret', 'linkedin_access_token',
    'reddit_client_id', 'reddit_client_secret', 'reddit_username', 'reddit_password',
    'telegram_bot_token', 'telegram_channel',
]


@social_bp.route('/check-config', methods=['GET'])
def check_config():
    """Check platform + AI capability config.

    返回结构（Phase 3 拆分）：
      - platforms:       仅【发布渠道】社媒号（wechat/weibo/toutiao）
      - ai_capabilities: 【创作工具】AI 文案/配图（走公共 LLM 服务，非发布平台）
    兼容：仍保留 platforms 字段，但不再混入 AI 项；前端据两字段分区渲染。
    """
    admin, err = _require_admin()
    if err:
        return err
    try:
        # AI 能力配置仍来自 system_config（公共 LLM 服务）
        ai_keys = (
            'dashscope_api_key', 'dashscope_text_key',
            'ai_image_provider', 'ai_image_model', 'ai_text_provider', 'ai_text_model',
        )
        with _get_main_db() as conn:
            rows = conn.execute(
                f"SELECT key, value FROM system_config WHERE key IN ({','.join('%s' for _ in ai_keys)})",
                ai_keys
            ).fetchall()
        cfg = {r['key']: r['value'] for r in rows}

        # 发布渠道：社媒账号以账号表为准（数据库表单化管理）
        from .models import has_account_configured, get_active_account_raw, account_to_config

        # Base domestic platforms
        platforms = [
            {
                'id': 'wechat',
                'name': _('WeChat Official Account'),
                'icon': '💬',
                'configured': has_account_configured('wechat'),
                'fields_needed': [] if has_account_configured('wechat') else ['AppID', 'AppSecret'],
            },
            {
                'id': 'weibo',
                'name': _('Weibo'),
                'icon': '📢',
                'configured': has_account_configured('weibo'),
                'fields_needed': [] if has_account_configured('weibo') else ['App Key', 'Access Token'],
            },
            {
                'id': 'toutiao',
                'name': _('Toutiao'),
                'icon': '📰',
                'configured': has_account_configured('toutiao'),
                'fields_needed': [] if has_account_configured('toutiao') else ['App ID', 'Access Token'],
            },
        ]

        # Add international platforms if market is 'intl' — 凭证从账号表读取
        market = _get_market()
        if market == 'intl':
            acc_cfg = {}
            for pid in ('twitter', 'linkedin', 'reddit', 'telegram'):
                acc = get_active_account_raw(pid)
                if acc:
                    acc_cfg.update(account_to_config(acc))
            intl_providers = _get_international_providers('intl', acc_cfg)
            for pid, info in intl_providers.items():
                platforms.append({
                    'id': pid,
                    'name': info.get('name', pid),
                    'icon': info.get('icon', ''),
                    'configured': info.get('configured', False),
                    'fields_needed': [] if info.get('configured') else [],
                })

        # content_factory 插件是否启用（决定前端是否显示 AI 排版/配图按钮）
        try:
            _pm = current_app.extensions.get('plugin_manager')
            content_factory_available = bool(_pm and _pm.is_enabled('content_factory'))
        except Exception:
            content_factory_available = True

        return jsonify({
            'success': True,
            'data': {
                'platforms': platforms,
                'content_factory_available': content_factory_available,
                'ai_capabilities': [
                    {
                        'id': 'image_gen',
                        'name': _('AI Image Generation'),
                        'icon': '🎨',
                        'configured': bool(cfg.get('ai_image_provider')),
                        'fields_needed': [] if cfg.get('ai_image_provider') else [_('AI Image API Key')],
                    },
                    {
                        'id': 'text_gen',
                        'name': _('AI Copywriting'),
                        'icon': '✍️',
                        'configured': bool(cfg.get('ai_text_provider')),
                        'fields_needed': [] if cfg.get('ai_text_provider') else [_('AI Copywriting API Key')],
                    },
                ],
            }
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Social config check failed: {str(e)}'}), 500


# =============================================
# 统一内容生成
# =============================================

CONTENT_TYPES = {
    'wechat': {
        'label': _('Official Account Article'),
        'types': [{'id': 'article', 'name': _('Article')}, {'id': 'announcement', 'name': _('Notification')}, {'id': 'promotion', 'name': _('Promotion')}],
    },
    'weibo': {
        'label': _('Weibo'),
        'types': [{'id': 'weibo', 'name': _('Weibo')}],
    },
    'twitter': {
        'label': _('X (Twitter)'),
        'types': [{'id': 'tweet', 'name': _('Tweet')}],
    },
    'linkedin': {
        'label': _('LinkedIn'),
        'types': [{'id': 'article', 'name': _('Article')}, {'id': 'post', 'name': _('Post')}],
    },
    'reddit': {
        'label': _('Reddit'),
        'types': [{'id': 'link', 'name': _('Link Post')}, {'id': 'text', 'name': _('Text Post')}],
    },
    'telegram': {
        'label': _('Telegram Channel'),
        'types': [{'id': 'message', 'name': _('Channel Message')}],
    },
}


@social_bp.route('/content-types', methods=['GET'])
def get_content_types():
    """Return available content types per platform."""
    admin, err = _require_admin()
    if err:
        return err
    platform = request.args.get('platform', 'wechat')
    info = CONTENT_TYPES.get(platform, CONTENT_TYPES['wechat'])
    return jsonify({'success': True, 'data': info})


@social_bp.route('/generate', methods=['POST'])
def generate_content():
    """Generate article content using AI."""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    topic = data.get('topic', '').strip()
    content_type = data.get('content_type', 'article')
    temperature = data.get('temperature', 0.7)

    if not topic:
        return jsonify({'success': False, 'error': _('Enter a topic')}), 400

    try:
        from services.ai_content_generator import generate_article
        result = generate_article(topic, content_type, temperature)
        _log(admin['user_id'], 'social_generate', 'social', '', f'Type: {content_type}, Topic: {topic}')
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        logger.exception('AI generate failed')
        return jsonify({'success': False, 'error': f'Generation Failed: {str(e)}'}), 500


# =============================================
# 配图生成
# =============================================

@social_bp.route('/generate-image', methods=['POST'])
def generate_image():
    """Generate an image via the unified AI image service."""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    prompt = data.get('prompt', '').strip()
    title = data.get('title', '').strip()
    use_for_cover = data.get('cover', True)

    if not prompt and not title:
        return jsonify({'success': False, 'error': _('Enter an image description or article title')}), 400

    try:
        from services.ai_content_generator import generate_image as gen_img
        from services.ai_content_generator import generate_cover_image

        if use_for_cover and title:
            oss_url = generate_cover_image(title, prompt or title)
        else:
            oss_url = gen_img(prompt or f'Image: {title}')

        # 下载到本地，不暴露外部 OSS URL
        import uuid, urllib.request
        SAVE_DIR = os.path.join(_auth_dir, '..', 'admin', 'static', 'uploads', 'temp')
        os.makedirs(SAVE_DIR, exist_ok=True)
        img_data = urllib.request.urlopen(oss_url, timeout=30).read()
        ext = '.png'
        if 'jpg' in oss_url or 'jpeg' in oss_url:
            ext = '.jpg'
        elif 'webp' in oss_url:
            ext = '.webp'
        filename = f'{uuid.uuid4().hex}{ext}'
        with open(os.path.join(SAVE_DIR, filename), 'wb') as f:
            f.write(img_data)
        local_url = f'/static/uploads/temp/{filename}'

        _log(admin['user_id'], 'social_gen_image', 'social', '', f'Prompt: {prompt or title}')
        return jsonify({'success': True, 'data': {'image_url': local_url}})
    except Exception as e:
        logger.exception('Image generation failed')
        return jsonify({'success': False, 'error': f'Failed to generate image: {str(e)}'}), 500


# =============================================
# 发布到多平台
# =============================================

@social_bp.route('/publish', methods=['POST'])
def publish_content():
    """Publish content to one or more platforms."""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    title = data.get('title', '').strip()
    body = data.get('body', '').strip()
    body_html = data.get('body_html', '').strip() or body
    summary = data.get('summary', '').strip()
    author = data.get('author', 'admin')
    cover_image_url = data.get('cover_image_url', '').strip()
    platforms = data.get('platforms', ['wechat'])  # list of platform ids
    auto_publish = data.get('auto_publish', False)
    subreddit = data.get('subreddit', '').strip()  # Reddit 目标板块（可选）

    if not title or not body:
        return jsonify({'success': False, 'error': _('Title and Body cannot be empty')}), 400

    admin_id = admin['user_id']
    results = []

    for platform in platforms:
        result = _publish_to_platform(
            platform=platform,
            title=title,
            body=body,
            body_html=body_html,
            summary=summary,
            author=author,
            cover_image_url=cover_image_url,
            auto_publish=auto_publish,
            admin_id=admin_id,
            subreddit=subreddit,
        )
        results.append(result)

    return jsonify({'success': True, 'data': {'results': results}})


def _load_config_for_provider(provider_name: str) -> dict:
    """Load provider credentials from social_push accounts table (decrypted)."""
    try:
        from .models import get_active_account_raw, account_to_config
        account = get_active_account_raw(provider_name)
        if not account:
            return {}
        return account_to_config(account)
    except Exception:
        return {}


def _sync_credentials_to_system_config(platform: str) -> None:
    """把账号表当前启用账号的凭证同步到 system_config（供 auth-center services 读取）。

    社媒账号以插件库 accounts 表为权威；国内平台发布走 auth-center services
    （wechat_push/weibo/toutiao），其内部读取 system_config，故发布前桥接同步一次。
    同步失败不阻塞发布（fail-open）。
    """
    try:
        from .models import get_active_account_raw, account_to_config
        account = get_active_account_raw(platform)
        if not account:
            return
        cfg = account_to_config(account)
        if not cfg:
            return
        with _get_main_db() as conn:
            for key, value in cfg.items():
                conn.execute(
                    "INSERT INTO system_config (key, value, updated_at) VALUES (%s, %s, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, value)
                )
            conn.commit()
    except Exception as e:
        logger.warning(f'[SocialPush] credential sync to system_config failed for {platform}: {e}')


# 发布限流：同一平台在冷却窗口内禁止重复发布（防止误触/重放）
_PUBLISH_COOLDOWN_SECONDS = 60


def _is_rate_limited(platform: str, window_seconds: int = _PUBLISH_COOLDOWN_SECONDS) -> bool:
    """同一平台在冷却窗口内已有发布记录则返回 True。

    限流检查失败时不阻塞发布（fail-open），避免 DB 抖动影响正常流程。
    """
    try:
        with get_sp_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM social_push_logs "
                "WHERE platform=%s AND created_at >= NOW() - INTERVAL '%s seconds'",
                (platform, int(window_seconds))
            ).fetchone()
        return bool(row and row['c'])
    except Exception:
        return False


def _publish_via_provider(platform, title, body, summary, cover_image_url, link_url, admin_id, **kwargs):
    """Publish via an international provider adapter. Returns standard result dict."""
    try:
        from .providers import get_provider
        provider = get_provider(platform)
        if not provider:
            return {'platform': platform, 'status': 'failed', 'error': f'Unknown provider: {platform}'}

        config = _load_config_for_provider(platform)

        # 凭证预检：凭据缺失时直接失败，避免无效 API 调用与不友好的底层异常
        if not provider.is_configured():
            return {
                'platform': platform,
                'status': 'failed',
                'error': _('{platform} credentials not configured').format(
                    platform=getattr(provider, 'DISPLAY_NAME', platform)),
            }

        result = provider.publish(
            title=title, body=body, summary=summary,
            image_url=cover_image_url, link_url=link_url,
            config=config, **kwargs,
        )

        # Log the result to social_push_logs
        with get_sp_db() as conn:
            conn.execute(
                """INSERT INTO social_push_logs
                   (platform, content_type, title, summary, article_json, media_id, status, admin_id, error_msg)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (platform, 'post', title, summary[:100],
                 json.dumps({'body': body[:500], 'cover_url': cover_image_url}, ensure_ascii=False),
                 result.get('post_id', ''), 'published' if result['success'] else 'failed', admin_id,
                 result.get('error', ''))
            )
            conn.commit()

        log_action = 'social_publish_international'
        _log(admin_id, log_action, 'social', result.get('post_id', ''), f'{platform}: {title}')

        if result['success']:
            return {
                'platform': platform,
                'status': 'published',
                'media_id': result.get('post_id', ''),
                'url': result.get('url', ''),
                'message': _('Published'),
            }
        return {'platform': platform, 'status': 'failed', 'error': result.get('error', _('Publish Failed'))}
    except Exception as e:
        logger.exception(f'{platform} publish failed')
        return {'platform': platform, 'status': 'failed', 'error': str(e)}


def _publish_to_platform(platform, title, body, body_html, summary, author,
                         cover_image_url, auto_publish, admin_id, **kwargs):
    """Publish to a single platform. Returns {platform, status, media_id, message, error}."""
    # 发布限流：同一平台冷却期内禁止重复发布
    if _is_rate_limited(platform):
        return {
            'platform': platform,
            'status': 'failed',
            'error': _('Publish too frequent, please try again later'),
        }

    if platform == 'wechat':
        return _publish_wechat(title, body_html, summary, author, cover_image_url, auto_publish, admin_id)
    elif platform == 'weibo':
        return _publish_weibo(title, body, cover_image_url, admin_id)
    elif platform == 'toutiao':
        return _publish_toutiao(title, body_html, summary, cover_image_url, admin_id)
    elif platform in ('twitter', 'linkedin', 'reddit', 'telegram'):
        return _publish_via_provider(platform, title, body, summary, cover_image_url, '', admin_id, **kwargs)
    else:
        return {'platform': platform, 'status': 'failed', 'error': f'Unsupported platform: {platform}'}


def _publish_wechat(title, body_html, summary, author, cover_image_url, auto_publish, admin_id):
    """Publish to WeChat Official Account."""
    try:
        _sync_credentials_to_system_config('wechat')
        from services.wechat_push_service import create_draft, submit_publish, upload_article_image
        thumb_media_id = ''
        publish_id = ''
        status = 'draft'

        if cover_image_url:
            thumb_media_id = upload_article_image(cover_image_url, is_url=True)

        media_id = create_draft(
            title=title, content_html=body_html, author=author,
            digest=summary or title[:100], thumb_media_id=thumb_media_id or '',
        )

        if auto_publish:
            publish_id = submit_publish(media_id)
            status = 'publishing'

        with get_sp_db() as conn:
            conn.execute(
                """INSERT INTO social_push_logs
                   (platform, content_type, title, summary, article_json, media_id, publish_id, status, admin_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ('wechat', 'article', title, summary,
                 json.dumps({'body_html': body_html, 'cover_url': cover_image_url}, ensure_ascii=False),
                 media_id, publish_id or '', status, admin_id)
            )
            conn.commit()

        _log(admin_id, 'social_publish', 'social', media_id, f'WeChat: {title}')
        return {
            'platform': 'wechat',
            'status': status,
            'media_id': media_id,
            'publish_id': publish_id,
            'message': _('WeChat Draft Created') if not auto_publish else _('WeChat Publication Task Submitted'),
        }
    except Exception as e:
        logger.exception('WeChat publish failed')
        with get_sp_db() as conn:
            conn.execute(
                """INSERT INTO social_push_logs
                   (platform, content_type, title, article_json, status, admin_id, error_msg)
                   VALUES (?, ?, ?, ?, 'failed', ?, ?)""",
                ('wechat', 'article', title,
                 json.dumps({'body': body_html[:200]}, ensure_ascii=False), admin_id, str(e))
            )
            conn.commit()
        return {'platform': 'wechat', 'status': 'failed', 'error': str(e)}


def _publish_weibo(title, body, cover_image_url, admin_id):
    """Publish to Weibo."""
    try:
        _sync_credentials_to_system_config('weibo')
        from services.weibo_service import publish_weibo
        text = f'{title}\n\n{body}'[:2000] if title else body[:2000]
        result = publish_weibo(text=text, image_url=cover_image_url or None)

        with get_sp_db() as conn:
            conn.execute(
                """INSERT INTO social_push_logs
                   (platform, content_type, title, summary, article_json, media_id, status, admin_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'published', ?)""",
                ('weibo', 'post', title, body[:100],
                 json.dumps({'body': body, 'cover_url': cover_image_url}, ensure_ascii=False),
                 result.get('id', ''), admin_id)
            )
            conn.commit()

        _log(admin_id, 'social_publish', 'social', result.get('id', ''), f'Weibo: {title}')
        return {'platform': 'weibo', 'status': 'published', 'media_id': result.get('id', ''), 'message': _('Weibo Posted')}
    except Exception as e:
        logger.exception('Weibo publish failed')
        with get_sp_db() as conn:
            conn.execute(
                """INSERT INTO social_push_logs
                   (platform, content_type, title, article_json, status, admin_id, error_msg)
                   VALUES (?, ?, ?, ?, 'failed', ?, ?)""",
                ('weibo', 'post', title,
                 json.dumps({'body': body[:200]}, ensure_ascii=False), admin_id, str(e))
            )
            conn.commit()
        return {'platform': 'weibo', 'status': 'failed', 'error': str(e)}


def _publish_toutiao(title, body_html, summary, cover_image_url, admin_id):
    """Publish to 今日头条."""
    try:
        _sync_credentials_to_system_config('toutiao')
        from services.toutiao_service import publish_article
        result = publish_article(
            title=title,
            content_html=body_html,
            cover_url=cover_image_url or '',
            summary=summary or title[:200],
        )
        with get_sp_db() as conn:
            conn.execute(
                """INSERT INTO social_push_logs
                   (platform, content_type, title, summary, article_json, media_id, status, admin_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'published', ?)""",
                ('toutiao', 'article', title, summary[:100],
                 json.dumps({'body': body_html[:500], 'cover_url': cover_image_url}, ensure_ascii=False),
                 result.get('id', ''), admin_id)
            )
            conn.commit()
        _log(admin_id, 'social_publish', 'social', result.get('id', ''), f'Toutiao: {title}')
        return {'platform': 'toutiao', 'status': 'published', 'media_id': result.get('id', ''), 'message': _('Published on Toutiao')}
    except Exception as e:
        logger.exception('Toutiao publish failed')
        with get_sp_db() as conn:
            conn.execute(
                """INSERT INTO social_push_logs
                   (platform, content_type, title, article_json, status, admin_id, error_msg)
                   VALUES (?, ?, ?, ?, 'failed', ?, ?)""",
                ('toutiao', 'article', title,
                 json.dumps({'body': body_html[:200]}, ensure_ascii=False), admin_id, str(e))
            )
            conn.commit()
        return {'platform': 'toutiao', 'status': 'failed', 'error': str(e)}


# =============================================
# 发布状态查询
# =============================================

@social_bp.route('/publish-status/<publish_id>', methods=['GET'])
def check_publish_status(publish_id):
    """Check WeChat publish status."""
    admin, err = _require_admin()
    if err:
        return err
    try:
        from services.wechat_push_service import get_publish_status
        result = get_publish_status(publish_id)
        status = result.get('publish_status', 'unknown')
        with get_sp_db() as conn:
            conn.execute(
                "UPDATE social_push_logs SET status=?, error_msg=? WHERE publish_id=?",
                (status, result.get('errmsg', ''), publish_id)
            )
            conn.commit()
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================
# 发布历史
# =============================================

@social_bp.route('/history', methods=['GET'])
def push_history():
    """Get social push history."""
    admin, err = _require_admin()
    if err:
        return err
    try:
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 20, type=int)
        platform = request.args.get('platform', '')
        offset = (page - 1) * limit

        where = ''
        params = []
        if platform:
            where = 'WHERE platform=?'
            params.append(platform)

        with get_sp_db() as conn:
            total = conn.execute(f'SELECT COUNT(*) as c FROM social_push_logs {where}', params).fetchone()
            rows = conn.execute(
                f"""SELECT id, platform, content_type, title, summary, media_id, publish_id,
                           status, push_time, error_msg, created_at, admin_id
                   FROM social_push_logs {where}
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                params + [limit, offset]
            ).fetchall()

        return jsonify({
            'success': True,
            'data': {
                'total': total['c'],
                'page': page,
                'limit': limit,
                'items': [dict(r) for r in rows],
            }
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Failed to load publish history: {str(e)}'}), 500


# =============================================
# CMS文章导入
# =============================================

@social_bp.route('/import-from-cms', methods=['GET'])
def import_cms_articles():
    """List published CMS articles for import into social push editor."""
    admin, err = _require_admin()
    if err:
        return err
    with _get_main_db() as conn:
        rows = conn.execute(
            "SELECT id, slug, category, title, excerpt, content, cover_image, author, "
            "is_published, published_at, created_at "
            "FROM cms_posts WHERE is_published=1 "
            "ORDER BY published_at DESC LIMIT 50"
        ).fetchall()
    return jsonify({
        'success': True,
        'data': [dict(r) for r in rows]
    })


# =============================================
# 删除历史
# =============================================

@social_bp.route('/history/<int:log_id>', methods=['DELETE'])
def delete_history(log_id):
    """Delete a push history entry."""
    admin, err = _require_admin()
    if err:
        return err
    with get_sp_db() as conn:
        conn.execute('DELETE FROM social_push_logs WHERE id=?', (log_id,))
        conn.commit()
    _log(admin['user_id'], 'social_delete', 'social', str(log_id))
    return jsonify({'success': True})


# =============================================
# 社媒账号管理（数据库表单化）
# =============================================

def _test_account_connection(platform, cfg):
    """各平台连接测试。返回 (ok, message)。"""
    import requests
    timeout = 8
    try:
        if platform == 'wechat':
            from services.wechat_push_service import _get_access_token
            token = _get_access_token()
            return (True, 'OK') if token else (False, 'Access token failed')
        if platform == 'weibo':
            token = (cfg.get('weibo_access_token') or '').strip()
            if not token:
                return False, 'Missing Access Token'
            r = requests.get('https://api.weibo.com/2/account/get_uid.json',
                             params={'access_token': token}, timeout=timeout)
            data = r.json()
            if r.status_code == 200 and data.get('uid'):
                return True, f"uid={data['uid']}"
            return False, data.get('error', r.text[:200])
        if platform == 'toutiao':
            app_id = cfg.get('toutiao_app_id')
            token = (cfg.get('toutiao_access_token') or '').strip()
            if not (app_id and token):
                return False, 'Missing App ID / Access Token'
            return True, 'OK'
        if platform == 'twitter':
            try:
                import tweepy
            except ImportError:
                return False, 'tweepy not installed'
            auth = tweepy.OAuth1UserHandler(
                cfg.get('twitter_api_key'), cfg.get('twitter_api_secret'),
                cfg.get('twitter_access_token'), cfg.get('twitter_access_secret'))
            me = tweepy.API(auth).verify_credentials()
            return True, f"@{me.screen_name}"
        if platform == 'linkedin':
            token = (cfg.get('linkedin_access_token') or '').strip()
            if not token:
                return False, 'Missing Access Token'
            r = requests.get('https://api.linkedin.com/v2/userinfo',
                             headers={'Authorization': f'Bearer {token}'}, timeout=timeout)
            if r.status_code == 200:
                return True, r.json().get('sub', 'OK')
            return False, r.text[:200]
        if platform == 'reddit':
            try:
                import praw
            except ImportError:
                return False, 'praw not installed'
            reddit = praw.Reddit(
                client_id=cfg.get('reddit_client_id'),
                client_secret=cfg.get('reddit_client_secret'),
                username=cfg.get('reddit_username'),
                password=cfg.get('reddit_password'),
                user_agent='verorun-social-push/1.0')
            me = reddit.user.me()
            return True, f"u/{me.name}"
        if platform == 'telegram':
            token = (cfg.get('telegram_bot_token') or '').strip()
            if not token:
                return False, 'Missing Bot Token'
            r = requests.get(f'https://api.telegram.org/bot{token}/getMe', timeout=timeout)
            if r.status_code == 200 and r.json().get('ok'):
                return True, f"@{r.json()['result'].get('username', '')}"
            return False, r.text[:200]
        return False, _('Unsupported platform')
    except Exception as e:
        return False, str(e)


@social_bp.route('/accounts', methods=['GET'])
def list_accounts_api():
    """社媒账号列表（脱敏）。"""
    admin, err = _require_admin()
    if err:
        return err
    try:
        from .models import list_accounts
        platform = (request.args.get('platform') or '').strip()
        items = list_accounts(platform=platform or None)
        return jsonify({'success': True, 'data': items})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@social_bp.route('/accounts', methods=['POST'])
def create_account_api():
    """新增社媒账号。"""
    admin, err = _require_admin()
    if err:
        return err
    try:
        from .models import create_account
        data = request.get_json(force=True) or {}
        platform = (data.get('platform') or '').strip()
        if platform not in PLATFORM_INFO:
            return jsonify({'success': False, 'error': _('Unsupported platform')}), 400
        if not (data.get('account_name') or '').strip():
            return jsonify({'success': False, 'error': _('Account name is required')}), 400
        account_id = create_account(data)
        _log(admin['user_id'], 'social_account_create', 'social', str(account_id), platform)
        return jsonify({'success': True, 'data': {'id': account_id}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@social_bp.route('/accounts/<int:account_id>', methods=['GET'])
def get_account_api(account_id):
    """单个社媒账号详情（脱敏，敏感字段另附 *_masked）。"""
    admin, err = _require_admin()
    if err:
        return err
    from .models import get_account
    acc = get_account(account_id)
    if not acc:
        return jsonify({'success': False, 'error': _('Account not found')}), 404
    return jsonify({'success': True, 'data': acc})


@social_bp.route('/accounts/<int:account_id>', methods=['PUT'])
def update_account_api(account_id):
    """更新社媒账号（敏感字段空值 = 不修改）。"""
    admin, err = _require_admin()
    if err:
        return err
    try:
        from .models import update_account
        data = request.get_json(force=True) or {}
        platform = (data.get('platform') or '').strip()
        if platform and platform not in PLATFORM_INFO:
            return jsonify({'success': False, 'error': _('Unsupported platform')}), 400
        update_account(account_id, data)
        _log(admin['user_id'], 'social_account_update', 'social', str(account_id))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@social_bp.route('/accounts/<int:account_id>', methods=['DELETE'])
def delete_account_api(account_id):
    """删除社媒账号。"""
    admin, err = _require_admin()
    if err:
        return err
    from .models import delete_account
    delete_account(account_id)
    _log(admin['user_id'], 'social_account_delete', 'social', str(account_id))
    return jsonify({'success': True})


@social_bp.route('/accounts/<int:account_id>/test', methods=['POST'])
def test_account_api(account_id):
    """测试社媒账号连接。"""
    admin, err = _require_admin()
    if err:
        return err
    try:
        from .models import get_account_raw, account_to_config
        acc = get_account_raw(account_id)
        if not acc:
            return jsonify({'success': False, 'error': _('Account not found')}), 404
        ok, message = _test_account_connection(acc['platform'], account_to_config(acc))
        return jsonify({'success': ok, 'message': message})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
