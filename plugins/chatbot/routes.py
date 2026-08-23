from i18n import _
import json
import sys
import os
from datetime import datetime
from flask import Blueprint, request, jsonify, g, current_app

from plugin_manager.logger import get_plugin_logger


chatbot_bp = Blueprint('chatbot_admin', __name__)
logger = get_plugin_logger('chatbot')

# ── 公开 Webhook 蓝图（多渠道）─────────────────────────
webhook_bp = Blueprint('chatbot_webhook', __name__, url_prefix='/api/v1/channels')

# ── 统计报表 ────────────────────────────────────────────

def _stats_import():
    """延迟导入 stats 模块，避免循环依赖。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from stats import log_session, get_today_stats, record_csat
    return log_session, get_today_stats, record_csat


# ── 数据库辅助 ────────────────────────────────────────────

def _get_main_db():
    """主库连接（user_tickets 表在主库）"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
    from models import get_db
    return get_db()


def create_ticket_from_chat(title, content, contact='', user_id=None, session_id=None):
    """AI 转人工时创建工单。
    
    可被 api_v1.py 直接调用（不经过 HTTP）。
    返回 {success, ticket_id, error}。
    """
    try:
        with _get_main_db() as conn:
            cur = conn.execute(
                """INSERT INTO user_tickets
                   (user_id, type, category, title, content, contact,
                    status, priority, created_at, updated_at)
                   VALUES (%s, 'aftersale', 'chatbot_escalation',
                           %s, %s, %s,
                           'open', 'normal',
                           NOW(), NOW()) RETURNING id""",
                (user_id, title, content, contact)
            )
            conn.commit()
            ticket_id = cur.fetchone()['id']
            logger.info(f'[Chatbot] Ticket created: #{ticket_id} — {title}')
        return {'success': True, 'ticket_id': ticket_id}
    except Exception as e:
        logger.error(f'[Chatbot] Create ticket failed: {e}')
        return {'success': False, 'error': str(e)}


def parse_escalation_from_reply(full_reply):
    """从 AI 回复中解析 [TICKET_CREATE] 标记。
    
    返回 (cleaned_reply, ticket_data | None)
    ticket_data = {title, content, contact}
    """
    marker = '[TICKET_CREATE]'
    idx = full_reply.rfind(marker)
    if idx == -1:
        return full_reply, None

    cleaned = full_reply[:idx].rstrip()
    json_part = full_reply[idx + len(marker):].strip()

    # 提取第一对大括号中的 JSON
    brace_start = json_part.find('{')
    brace_end = json_part.rfind('}')
    if brace_start == -1 or brace_end == -1:
        return cleaned, None

    try:
        data = json.loads(json_part[brace_start:brace_end + 1])
        ticket_data = {
            'title': str(data.get('title', _('User Inquiry')))[:200],
            'content': str(data.get('content', '')),
            'contact': str(data.get('contact', '')),
        }
        return cleaned, ticket_data
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f'[Chatbot] Parse escalation JSON failed: {e}')
        return cleaned, None


def _require_admin():
    """鉴权守卫：优先 Authorization header，回退 cookie，使用 JWT is_admin 声明。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
    from services.jwt_service import validate_token
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return (jsonify({'success': False, 'error': _('Requires management permissions')}), 401)
    return None


def _login_required(f):
    """登录检查装饰器：验证 JWT token 有效即可（不限角色）。"""
    from functools import wraps
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
    from services.jwt_service import validate_token

    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
        if not token:
            token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
        try:
            payload = validate_token(token) if token else None
            if not payload:
                return jsonify({'success': False, 'error': 'Authentication required'}), 401
        except Exception:
            return jsonify({'success': False, 'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return wrapper


def _get_plugin_manager():
    pm = getattr(request, 'plugin_manager', None) or g.get('plugin_manager')
    if pm is None:
        pm = current_app.extensions.get('plugin_manager')
    return pm


@chatbot_bp.route('/chat', methods=['POST'])
def chatbot_chat():
    """Handle chatbot conversation requests using the Master Agent LLM."""
    err = _require_admin()
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({'success': False, 'error': _('Message cannot be empty')}), 400

    try:
        from agent_matrix.engine import UnifiedLLM
        from agent_matrix import models as agent_models

        agents = agent_models.list_agents(role_type='master', active_only=True)
        if not agents:
            return jsonify({'success': False, 'error': 'No active Master Agent'}), 500

        engine = UnifiedLLM(agents[0])
        reply = engine.ask(message)
        return jsonify({'success': True, 'data': {'reply': reply}})
    except Exception as e:
        logger.error(f'[chatbot/chat] LLM call failed: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@chatbot_bp.route('/settings', methods=['GET'])
def get_settings():
    err = _require_admin()
    if err:
        return err

    keys = [
        'enabled', 'auto_escalate', 'title', 'subtitle', 'welcome_message', 'help_hint',
        'avatar_url', 'agent_id', 'max_history', 'float_button_text'
    ]

    defaults = {
        'enabled': '1', 'auto_escalate': '1', 'title': '',
        'subtitle': '',
        'welcome_message': '',
        'help_hint': '',
        'avatar_url': '', 'agent_id': 'chat_assistant', 'max_history': '20',
        'float_button_text': ''
    }

    try:
        # 优先 PluginManager
        mgr = _get_plugin_manager()
        if mgr:
            pm_config = mgr.get_config('chatbot') or {}
            cfg = {}
            for k in keys:
                v = pm_config.get(k)
                cfg[k] = str(v) if v is not None else defaults.get(k, '')
            return jsonify({'success': True, 'data': cfg})

        # 回退旧方法：独立库 plugin_configs 表
        from .models import get_config as _gc
        cfg = {}
        for k in keys:
            val = _gc('chatbot', k)
            cfg[k] = val if val else defaults.get(k, '')
        return jsonify({'success': True, 'data': cfg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@chatbot_bp.route('/log_session', methods=['POST'])
@_login_required
def log_session_route():
    """记录一次 AI 对话回合（由 api_v1.py 内部调用）。"""
    data = request.get_json(silent=True) or {}
    session_id = data.get('session_id', '')
    user_query = data.get('user_query', '')
    ai_reply = data.get('ai_reply', '')
    escalated = data.get('escalated', False)
    source = data.get('source', 'chatbot')
    if not session_id:
        return jsonify({'success': False, 'error': _('Session_id cannot be empty')}), 400
    ls, _, _ = _stats_import()
    ok = ls(session_id, user_query, ai_reply, escalated=escalated, source=source)
    return jsonify({'success': ok})


@chatbot_bp.route('/stats', methods=['GET'])
def stats():
    """获取今日统计概览。"""
    err = _require_admin()
    if err:
        return err
    _, gts, _ = _stats_import()
    data = gts()
    return jsonify({'success': True, 'data': data})


@chatbot_bp.route('/hot_topics', methods=['GET'])
def hot_topics():
    """获取今日热门问题。"""
    err = _require_admin()
    if err:
        return err
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from stats import get_hot_topics
        data = get_hot_topics(limit=10)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@chatbot_bp.route('/agent_performance', methods=['GET'])
def agent_performance():
    """座席绩效数据。"""
    err = _require_admin()
    if err:
        return err
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from stats import get_agent_performance
        data = get_agent_performance()
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@chatbot_bp.route('/qa_check', methods=['POST'])
@_login_required
def qa_check():
    """对话质检：分析一轮对话质量。"""
    data = request.get_json(silent=True) or {}
    session_id = data.get('session_id', '')
    user_query = data.get('user_query', '')
    ai_reply = data.get('ai_reply', '')
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from stats import qa_check_conversation
        result = qa_check_conversation(session_id, user_query, ai_reply)
        if result:
            return jsonify({'success': True, 'data': result})
        return jsonify({'success': False, 'error': _('Quality Inspection Failed')}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@chatbot_bp.route('/copilot_suggest', methods=['POST'])
@_login_required
def copilot_suggest():
    """Agent Copilot：根据对话上下文，为坐席生成回复建议。"""
    data = request.get_json(silent=True) or {}
    user_query = data.get('user_query', '')
    history = data.get('history', '')  # 之前的对话记录
    if not user_query:
        return jsonify({'success': False, 'error': _('Missing user message')}), 400
    try:
        import sys as _sys, os as _os, json as _json
        _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..'))
        from agent_matrix.engine import UnifiedLLM

        context = f"对话历史：\n{history[:500]}\n\n用户最新消息：{user_query[:300]}" if history else f"User Message: {user_query[:300]}"
        prompt = f"""你是一个 AI 坐席助手（Agent Copilot）。根据以下对话，生成 2-3 条回复建议供坐席选择。

要求：
- 每条建议用一句话，简洁专业
- 保持友好语气
- 设计解决方案导向
- 输出 JSON 数组

{context}

输出格式：{{"suggestions": [_("Suggestion 1"), _("Suggestion 2"), _("Suggestion 3")]}}"""

        engine = UnifiedLLM({'provider': 'dashscope', 'model_name': 'qwen-turbo'})
        reply = ''
        for token in engine.chat_stream([
            {'role': 'system', 'content': _('You are an agent assistant. Output only JSON.')},
            {'role': 'user', 'content': prompt}
        ], temperature=0.3, max_tokens=256):
            if not token.startswith('Error:'):
                reply += token

        data = _json.loads(reply.strip())
        suggestions = data.get('suggestions', [])
        return jsonify({'success': True, 'data': {'suggestions': suggestions[:5]}})
    except Exception as e:
        logger.warning(f"[Chatbot] Copilot suggest failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 多渠道 Webhook 端点 ────────────────────────────────

@webhook_bp.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    """Telegram Bot Webhook（带 secret token 认证）"""
    try:
        # 验证 Secret Token
        secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        expected = os.environ.get('TELEGRAM_SECRET_TOKEN', '')
        if expected and secret != expected:
            logger.warning("[Telegram webhook] Invalid secret token")
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403

        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from channels.router import telegram_handle_webhook
        body = request.get_json(silent=True) or {}
        ok = telegram_handle_webhook(body)
        if ok:
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'ignored'}), 200
    except Exception as e:
        logger.error(f"[Telegram webhook] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@webhook_bp.route('/line/webhook', methods=['POST'])
def line_webhook():
    """LINE Messaging Webhook（带签名认证）"""
    try:
        # 验证 LINE 签名
        channel_secret = os.environ.get('LINE_CHANNEL_SECRET', '')
        if channel_secret:
            import hashlib, hmac, base64
            signature = request.headers.get('x-line-signature', '')
            body_raw = request.get_data()
            hash_val = hmac.new(channel_secret.encode(), body_raw, hashlib.sha256).digest()
            expected = base64.b64encode(hash_val).decode()
            if not hmac.compare_digest(signature, expected):
                logger.warning("[LINE webhook] Invalid signature")
                return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from channels.router import line_handle_webhook
        body = request.get_json(silent=True) or {}
        ok = line_handle_webhook(body)
        if ok:
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'ignored'}), 200
    except Exception as e:
        logger.error(f"[LINE webhook] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@chatbot_bp.route('/csat', methods=['POST'])
@_login_required
def csat():
    """提交 CSAT 满意度评分。"""
    data = request.get_json(silent=True) or {}
    session_id = data.get('session_id', '')
    score = data.get('score', 0)
    if not session_id:
        return jsonify({'success': False, 'error': _('Session_id cannot be empty')}), 400
    try:
        score = int(score)
        if score < 1 or score > 5:
            return jsonify({'success': False, 'error': _('Rating range 1-5')}), 400
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': _('Invalid rating')}), 400
    _, _, rc = _stats_import()
    ok = rc(session_id, score)
    return jsonify({'success': ok})


DEFAULT_HANDOFF_KEYWORDS = [
    "human", "agent", "customer service", "live chat", "real person",
    "talk to human", "representative", "support agent", "complaint", "custom"
]


@chatbot_bp.route('/handoff_rules', methods=['GET'])
def get_handoff_rules():
    """获取转人工规则配置。"""
    err = _require_admin()
    if err:
        return err
    try:
        keywords = DEFAULT_HANDOFF_KEYWORDS
        max_fails = 3
        try:
            from .models import get_config as _gc
            kw_raw = _gc('chatbot', 'handoff_keywords')
            if kw_raw:
                keywords = json.loads(kw_raw)
            mf_raw = _gc('chatbot', 'handoff_max_fails')
            if mf_raw:
                max_fails = int(mf_raw)
        except Exception:
            pass
        return jsonify({'success': True, 'data': {
            'keywords': keywords,
            'max_fails': max_fails,
        }})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@chatbot_bp.route('/handoff_rules', methods=['POST'])
def save_handoff_rules():
    """保存转人工规则配置。"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    keywords = data.get('keywords', DEFAULT_HANDOFF_KEYWORDS)
    max_fails = data.get('max_fails', 3)
    try:
        from .models import set_config as _sc
        _sc('chatbot', 'handoff_keywords', json.dumps(keywords, ensure_ascii=False))
        _sc('chatbot', 'handoff_max_fails', str(max_fails))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@chatbot_bp.route('/escalate', methods=['POST'])
@_login_required
def escalate():
    """AI 转人工 — 创建工单。
    
    可由 api_v1.py 内部调用，或由前端直接调用。
    请求体: {title, content, contact, user_id(可选), session_id(可选)}
    """
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    content = (data.get('content') or '').strip()
    contact = (data.get('contact') or '').strip()
    user_id = data.get('user_id')
    session_id = data.get('session_id')

    if not title or not content:
        return jsonify({'success': False, 'error': _('Title and Content cannot be empty')}), 400

    result = create_ticket_from_chat(title, content, contact,
                                     user_id=user_id, session_id=session_id)
    if result['success']:
        return jsonify({'success': True, 'data': {'ticket_id': result['ticket_id']}})
    else:
        return jsonify({'success': False, 'error': result.get('error', _('Ticket creation failed'))}), 500


@chatbot_bp.route('/settings', methods=['POST'])
def save_settings():
    err = _require_admin()
    if err:
        return err

    data = request.get_json() or {}
    allowed = {
        'enabled', 'auto_escalate', 'title', 'subtitle', 'welcome_message', 'help_hint',
        'avatar_url', 'agent_id', 'max_history', 'float_button_text'
    }

    try:
        # 优先 PluginManager
        mgr = _get_plugin_manager()
        if mgr:
            filtered = {k: str(v) for k, v in data.items() if k in allowed}
            if filtered:
                mgr.set_config_batch('chatbot', filtered, coerce=True)
            return jsonify({'success': True})

        # 回退旧方法：独立库 plugin_configs 表
        from .models import set_config as _sc
        for k, v in data.items():
            if k in allowed:
                _sc('chatbot', k, str(v))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
