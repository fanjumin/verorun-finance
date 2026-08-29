#!/usr/bin/env python3
"""API V1 Routes — 统一的API v1端点"""
import json
import os
import sys
import threading
import time
from datetime import datetime
from flask import Blueprint, request, jsonify, Response, stream_with_context
from models import get_db
from services.jwt_service import validate_token
from i18n import _

# 创建蓝图
api_v1_bp = Blueprint('api_v1', __name__, url_prefix='/api/v1')

def api_ok(data=None):
    return jsonify({'success': True, 'data': data})

def api_err(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code

def get_current_user_id(token):
    """验证token并返回用户ID"""
    payload = validate_token(token)
    if not payload:
        return None
    return payload.get('user_id')

def require_auth():
    """认证装饰器辅助函数 — 返回完整 JWT payload（含 role）"""
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None, api_err('未提供有效的Token', 401)
    token = auth.replace('Bearer ', '')
    payload = validate_token(token)
    if not payload:
        return None, api_err('无效或过期的Token', 401)
    return payload, None

# =============================================
# RAG 知识库检索（统一函数）
# =============================================

def _rag_search(query: str, top_k: int = 5, category: str = None, scope: str = None) -> list:
    """检索 knowledge_blocks，返回排序后的知识片段列表
    scope: 可选 'system' | 'user'，默认 None（检索全部）
    已升级为混合检索：向量路(pgvector) + 关键词路(pg_trgm+字符评分) + RRF 融合。
    实现委托 agent_matrix/rag_retriever.py，返回结构向下兼容
    {title, content, category, score}，额外含 id/similarity 字段。
    """
    try:
        from agent_matrix.rag_retriever import rag_search
        return rag_search(query, top_k=top_k, category=category, scope=scope)
    except Exception as e:
        print(f'[RAG] Search error: {e}')
        return []

def _build_rag_context(knowledge: list) -> str:
    """将检索到的知识片段格式化为系统提示上下文"""
    if not knowledge:
        return ''
    ctx = '\n\n以下是与用户问题相关的内部知识库内容，请优先参考这些信息回答：\n'
    for i, k in enumerate(knowledge, 1):
        ctx += f'\n[{i}] {k["title"]}\n{k["content"]}\n'
    ctx += '\n请基于以上知识回答用户问题。如果知识库中没有相关信息，请如实告知用户。'
    return ctx

# ── 简易IP限流 ──
_rate_limit_store = {}
_RATE_LIMIT_CLEANUP_INTERVAL = 300  # 5分钟清理一次
_rate_limit_last_cleanup = time.time()

def _ensure_rate_limit():
    global _rate_limit_last_cleanup
    now = time.time()
    if now - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
        _rate_limit_last_cleanup = now
        cutoff = now - 60
        for k in list(_rate_limit_store.keys()):
            _rate_limit_store[k] = [t for t in _rate_limit_store[k] if t > cutoff]
            if not _rate_limit_store[k]:
                del _rate_limit_store[k]

def _check_rate_limit(key, max_per_minute=10):
    now = time.time()
    if key not in _rate_limit_store:
        _rate_limit_store[key] = []
    _rate_limit_store[key] = [t for t in _rate_limit_store[key] if now - t < 60]
    if len(_rate_limit_store[key]) >= max_per_minute:
        return False
    _rate_limit_store[key].append(now)
    return True

# =============================================
# 会话与聊天相关接口
# =============================================

@api_v1_bp.route('/chat/save', methods=['POST'])
def save_messages():
    """保存用户会话消息（无需登录）"""
    data = request.get_json() or {}
    openid = data.get('openid')
    messages = data.get('messages', [])

    if not openid:
        return api_err('openid是必需的', 400)

    # IP限流
    _ensure_rate_limit()
    if not _check_rate_limit(request.remote_addr or 'unknown'):
        return api_err('请求太频繁', 429)

    from models import get_db
    import json
    with get_db() as conn:
        now = datetime.now().isoformat()
        existing = conn.execute('SELECT created_at FROM chat_messages WHERE openid=%s', (openid,)).fetchone()
        if existing:
            conn.execute(
                'UPDATE chat_messages SET messages=%s, updated_at=%s WHERE openid=%s',
                (json.dumps(messages, ensure_ascii=False), now, openid)
            )
        else:
            conn.execute(
                'INSERT INTO chat_messages (openid, messages, created_at, updated_at) VALUES (%s, %s, %s, %s)',
                (openid, json.dumps(messages, ensure_ascii=False), now, now)
            )
        conn.commit()

    return api_ok({'saved': True})

@api_v1_bp.route('/chat/history', methods=['POST'])
def get_chat_history():
    """获取会话历史（无需登录）"""
    data = request.get_json() or {}
    openid = data.get('openid')
    
    if not openid:
        return api_err('openid是必需的', 400)
    
    from models import get_db
    import json
    with get_db() as conn:
        row = conn.execute('SELECT messages FROM chat_messages WHERE openid=%s', (openid,)).fetchone()
        messages = json.loads(row['messages']) if row else []
    
    return api_ok({'messages': messages})

@api_v1_bp.route('/chat/request', methods=['POST'])
def chat_request():
    """非流式AI对话请求（带RAG知识增强）"""
    payload, error = require_auth()
    if error:
        return error
    user_id = payload['user_id']

    data = request.get_json() or {}
    messages = data.get('messages', [])
    temperature = data.get('temperature', 0.7)
    max_tokens = data.get('max_tokens', 2048)
    skip_rag = data.get('skip_rag', False)  # 可选跳过RAG

    if not messages:
        return api_err('messages是必需的', 400)

    # ── RAG 知识增强 ──
    knowledge_injected = False
    if not skip_rag:
        # 取用户最后一条消息作为查询
        last_user_msg = ''
        for m in reversed(messages):
            if m.get('role') == 'user':
                last_user_msg = m.get('content', '')[:200]
                break
        if last_user_msg:
            knowledge = _rag_search(last_user_msg, top_k=5, scope='user')
            if knowledge:
                ctx = _build_rag_context(knowledge)
                # 追加到已有的 system 消息，或新建一条
                has_system = False
                for m in messages:
                    if m.get('role') == 'system':
                        m['content'] += ctx
                        has_system = True
                        break
                if not has_system:
                    messages.insert(0, {'role': 'system', 'content': '你是一个智能客服助手。' + ctx})
                knowledge_injected = True

    try:
        # 从system_config读取小程序AI配置
        from models import get_db
        with get_db() as conn:
            rows = {r['key']: r['value'] for r in
                    conn.execute("SELECT key, value FROM system_config WHERE key IN "
                                "('mp_ai_provider','mp_ai_model','mp_ai_base_url','mp_ai_api_key')").fetchall()}

        provider = rows.get('mp_ai_provider', 'deepseek') or 'deepseek'
        from models.database import get_active_model
        _, default_model, _ = get_active_model(provider)
        model = rows.get('mp_ai_model') or default_model or ''
        base_url = rows.get('mp_ai_base_url') or 'https://api.deepseek.com'
        api_key = rows.get('mp_ai_api_key', '')

        # 回退
        if not api_key:
            fallback_keys = {
                'deepseek': 'deepseek_api_key',
                'dashscope': 'dashscope_api_key',
                'openai': 'openai_api_key',
                'openrouter': 'openrouter_api_key',
            }
            fallback = fallback_keys.get(provider)
            if fallback:
                with get_db() as conn2:
                    row = conn2.execute("SELECT value FROM system_config WHERE key=%s", (fallback,)).fetchone()
                api_key = row['value'] if row else ''
            if not api_key:
                api_key = os.environ.get(f'{provider.upper()}_API_KEY', '')

        if not api_key:
            return api_err(f'AI API Key 未配置，请在系统设置「小程序 AI 配置」中设置', 500)

        # 调用 AI API
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens
        )

        content = resp.choices[0].message.content

        # 异步记录token消耗
        if hasattr(resp, 'usage') and resp.usage:
            try:
                from agent_matrix.engine import _log_token_usage
                threading.Thread(target=_log_token_usage, args=(
                    0, 'AI客服', model, provider,
                    resp.usage.prompt_tokens or 0,
                    resp.usage.completion_tokens or 0,
                    resp.usage.total_tokens or 0,
                    'chat', 'text', user_id, None, None
                ), daemon=True).start()
            except ImportError:
                pass

        return api_ok({
            'content': content,
            'rag': knowledge_injected,
        })

    except Exception as e:
        return api_err(f'AI对话请求失败: {str(e)}', 500)


@api_v1_bp.route('/chat/public', methods=['POST'])
def chat_public():
    """公开AI对话（官网商务机器人/抖音小程序，无需登录，带限流+RAG）"""
    data = request.get_json() or {}
    messages = data.get('messages', [])
    temperature = data.get('temperature', 0.7)
    max_tokens = data.get('max_tokens', 2048)
    source = data.get('source', 'website')  # website / douyin / tiktok

    if not messages:
        return api_err('messages是必需的', 400)

    # 简易IP限流（每IP每分钟10次）
    ip = request.remote_addr or 'unknown'
    _ensure_rate_limit()
    if not _check_rate_limit(ip):
        return api_err('请求太频繁，请稍后再试', 429)

    # ── RAG 知识增强 ──
    last_user_msg = ''
    for m in reversed(messages):
        if m.get('role') == 'user':
            last_user_msg = m.get('content', '')[:200]
            break
    if last_user_msg:
        knowledge = _rag_search(last_user_msg, top_k=5, scope='user')
        if knowledge:
            ctx = _build_rag_context(knowledge)
            has_system = False
            for m in messages:
                if m.get('role') == 'system':
                    m['content'] += ctx
                    has_system = True
                    break
            if not has_system:
                messages.insert(0, {'role': 'system',
                    'content': f'你是VeroRun 维洛智能的商务助手。用户来自: {source}。'
                               f'请用中文友好地回答关于产品、价格、功能的问题。' + ctx})

    try:
        from models import get_db
        with get_db() as conn:
            rows = {r['key']: r['value'] for r in
                    conn.execute("SELECT key, value FROM system_config WHERE key IN "
                                "('mp_ai_provider','mp_ai_model','mp_ai_base_url','mp_ai_api_key')").fetchall()}

        provider = rows.get('mp_ai_provider', 'deepseek') or 'deepseek'
        from models.database import get_active_model
        _, default_model, _ = get_active_model(provider)
        model = rows.get('mp_ai_model') or default_model or ''
        base_url = rows.get('mp_ai_base_url') or 'https://api.deepseek.com'
        api_key = rows.get('mp_ai_api_key', '')

        if not api_key:
            for fk in ['deepseek_api_key', 'dashscope_api_key']:
                with get_db() as c:
                    r = c.execute("SELECT value FROM system_config WHERE key=%s", (fk,)).fetchone()
                if r and r['value']:
                    api_key = r['value']
                    break
            if not api_key:
                api_key = os.environ.get('DEEPSEEK_API_KEY', '')

        if not api_key:
            return api_err('AI 服务暂未配置', 500)

        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens
        )

        return api_ok({'content': resp.choices[0].message.content})

    except Exception as e:
        return api_err(f'请求失败: {str(e)}', 500)

def _get_chatbot_config():
    """从独立库 chatbot.db 读取 AI Advisor 配置。"""
    defaults = {
        'enabled': '1',
        'auto_escalate': '1',
        'title': '',
        'subtitle': '',
        'welcome_message': '',
        'help_hint': '',
        'avatar_url': '',
        'agent_id': 'chat_assistant',
        'max_history': '20',
        'float_button_text': ''
    }
    try:
        from plugins.chatbot.models import get_all_configs
        db_cfg = get_all_configs('chatbot')
        merged = {**defaults, **db_cfg}
        return merged
    except Exception as e:
        import logging
        logging.warning(f"[chatbot] 读取配置失败，使用默认值: {e}")
        return defaults


def _get_chatbot_agent(agent_id):
    """从 chatbot 独立库 agent_registry 表读取绑定的 Agent 配置。"""
    try:
        from plugins.chatbot.models import get_agent
        return get_agent(agent_id)
    except Exception as e:
        import logging
        logging.warning(f"[chatbot] 读取 Agent 失败: {e}")
        return None


def _route_agent_by_intent(intent):
    """根据意图分类，从 agent_matrix 中路由到最匹配的子 Agent。
    
    返回 agent dict 或 None（走默认）。
    """
    intent_domain_map = {
        'purchase':  'sales',
        'aftersale': 'aftersale',
        'complaint': 'support',
        'consult':   'general',
        'technical': 'technical',
    }
    domain = intent_domain_map.get(intent, '')
    if not domain:
        return None
    try:
        from agent_matrix.models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM agent_matrix WHERE domain=%s AND role_type='sub' AND is_active=1 LIMIT 1",
                (domain,)
            ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        import logging
        logging.warning(f"[chatbot] 意图路由失败: {e}")
        return None


@api_v1_bp.route('/chat', methods=['POST'])
def chat_stream():
    """流式AI对话接口（免登录）"""
    import logging
    logging.info(f"[DEBUG] chat_stream called, path={request.path}, method={request.method}")

    cfg = _get_chatbot_config()
    if cfg.get('enabled') == '0':
        def _disabled_stream():
            yield 'data: {"type":"error","content":"AI Advisor is currently disabled"}\n\n'
        return Response(stream_with_context(_disabled_stream()),
                        mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    data = request.get_json() or {}
    # 兼容前端 {message, history} 与标准 {messages}
    messages = data.get('messages', [])
    if not messages and data.get('message'):
        messages = [{'role': 'user', 'content': data.get('message')}]
        for h in (data.get('history') or []):
            if isinstance(h, dict) and 'role' in h and 'content' in h:
                messages.append(h)
            elif isinstance(h, list) and len(h) == 2:
                messages.append({'role': h[0], 'content': h[1]})

    if not messages:
        return api_err('messages是必需的', 400)

    def generate():
        import logging
        yield 'data: {"role":"assistant"}\n\n'

        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

            # 读取转人工规则配置
            handoff_keywords = "人工, 客服, 转人工, 联系真人, 联系工作人员, 商务, 合作, 投诉, 定制, 开发"
            handoff_max_fails = "3"
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
                from models import get_db
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT key, value FROM plugin_configs WHERE plugin_name='chatbot' "
                        "AND key IN ('handoff_keywords', 'handoff_max_fails')"
                    ).fetchall()
                for r in rows:
                    if r['key'] == 'handoff_keywords':
                        kw = json.loads(r['value'])
                        handoff_keywords = ', '.join(kw)
                    elif r['key'] == 'handoff_max_fails':
                        handoff_max_fails = r['value']
            except Exception as e:
                logging.warning(f"[Chatbot] Failed to load handoff rules: {e}")

            handoff_rules_text = f"""
转人工触发规则（当消息包含以下关键词时，必须输出转人工提示）：
{handoff_keywords}

如果连续 {handoff_max_fails} 次无法回答用户问题，也必须转人工。
"""

            # ── 意图分类 + 多 Agent 路由 ──
            route_intent = 'other'
            route_sentiment = 'neutral'
            try:
                from agent_matrix.intent import classify_intent
                route_intent, route_sentiment = classify_intent(user_query)
            except Exception:
                pass

            agent_id = cfg.get('agent_id', 'chat_assistant')
            agent = _route_agent_by_intent(route_intent) or _get_chatbot_agent(agent_id)

            if agent and agent.get('system_prompt'):
                system_prompt = agent['system_prompt']
                # 将转人工规则注入到 agent prompt 末尾
                if '[TICKET_CREATE]' in system_prompt:
                    system_prompt += handoff_rules_text
                provider = agent.get('provider') or cfg.get('provider', 'dashscope')
                model_name = agent.get('model_name') or cfg.get('model_name', 'qwen-turbo')
            else:
                system_prompt = f"""
你是 AI Advisor。请根据用户的问题进行回答。

回答规则：
1. 用你的通用知识回答
2. 回答要友好、专业、简洁

转人工触发规则（当消息包含以下关键词时，必须输出转人工提示）：
{handoff_keywords}

如果连续 {handoff_max_fails} 次无法回答用户问题，也必须转人工。
"""
                provider = cfg.get('provider', 'dashscope')
                model_name = cfg.get('model_name', 'qwen-turbo')

            # 构建消息
            chat_messages = [{"role": "system", "content": system_prompt}]
            for msg in messages:
                chat_messages.append({"role": msg.get('role', 'user'), "content": msg.get('content', '')})

            # 调用AI引擎
            from agent_matrix.engine import UnifiedLLM

            config = {
                'provider': provider,
                'model_name': model_name,
                'system_prompt': system_prompt
            }

            engine = UnifiedLLM(config)
            full_reply = ''

            def _sse_event(event_type, **kwargs):
                """SSE data line with proper JSON encoding to prevent XSS/protocol injection."""
                payload = {'type': event_type}
                payload.update(kwargs)
                return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

            temperature = float(cfg.get('temperature', '0.7'))
            max_tokens = int(cfg.get('max_tokens', '2048'))

            for token in engine.chat_stream(chat_messages, temperature=temperature, max_tokens=max_tokens):
                if token.startswith("Error:"):
                    yield _sse_event('error', content=token)
                    return
                full_reply += token
                yield _sse_event('token', content=token)

            # ── 转人工检测 ──
            cleaned_reply = full_reply
            was_escalated = False
            try:
                from plugins.chatbot.routes import parse_escalation_from_reply, create_ticket_from_chat
                cleaned_reply, ticket_data = parse_escalation_from_reply(full_reply)
                if ticket_data and cfg.get('auto_escalate', '1') != '0':
                    was_escalated = True
                    result = create_ticket_from_chat(
                        title=ticket_data['title'],
                        content=ticket_data['content'],
                        contact=ticket_data['contact'],
                    )
                    if result['success']:
                        yield _sse_event('escalated',
                                         ticket_id=result['ticket_id'],
                                         message='已为您创建工单，客服将尽快联系您')
                        logging.info(f'[Chatbot] Escalation auto-ticket #{result["ticket_id"]} created')
                    else:
                        logging.warning(f'[Chatbot] Escalation auto-ticket failed: {result.get("error")}')
            except Exception as e:
                logging.warning(f'[Chatbot] Escalation detection failed (non-critical): {e}')

            # ── 对话落库（含意图+情绪分类）──
            session_id = data.get('session_id', '')
            try:
                import hashlib
                if not session_id:
                    session_id = hashlib.md5(
                        (user_query + str(datetime.now().timestamp())).encode()
                    ).hexdigest()[:16]
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', 'chatbot'))
                from stats import log_session
                log_session(session_id, user_query=user_query,
                            ai_reply=cleaned_reply, escalated=was_escalated,
                            intent=route_intent, sentiment=route_sentiment)
            except Exception as e:
                logging.warning(f'[Chatbot] Log session failed (non-critical): {e}')

            yield _sse_event('done', reply=cleaned_reply, session_id=session_id,
                             retrievedKnowledge=retrieved_knowledge)

        except Exception as e:
            import logging
            logging.error(f"[API] 流式对话失败: {e}")
            yield _sse_event('error', content=f'对话失败: {str(e)}')

    return Response(stream_with_context(generate()),
                   mimetype='text/event-stream',
                   headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

# =============================================
# 用户画像与会话摘要
# =============================================

@api_v1_bp.route('/profile/save', methods=['POST'])
def save_profile():
    """保存用户画像（无需登录）"""
    data = request.get_json() or {}
    openid = data.get('openid')
    profile = data.get('profile', {})
    
    if not openid:
        return api_err('openid是必需的', 400)
    
    from models import get_db
    import json
    with get_db() as conn:
        now = datetime.now().isoformat()
        existing = conn.execute('SELECT created_at FROM mp_profiles WHERE openid=%s', (openid,)).fetchone()
        if existing:
            conn.execute(
                'UPDATE mp_profiles SET profile=%s, updated_at=%s WHERE openid=%s',
                (json.dumps(profile, ensure_ascii=False), now, openid)
            )
        else:
            conn.execute(
                'INSERT INTO mp_profiles (openid, profile, created_at, updated_at) VALUES (%s, %s, %s, %s)',
                (openid, json.dumps(profile, ensure_ascii=False), now, now)
            )
        conn.commit()
    
    return api_ok({})

@api_v1_bp.route('/profile/get', methods=['POST'])
def get_profile():
    """获取用户画像（无需登录）"""
    data = request.get_json() or {}
    openid = data.get('openid')
    
    if not openid:
        return api_err('openid是必需的', 400)
    
    from models import get_db
    import json
    with get_db() as conn:
        row = conn.execute('SELECT profile FROM mp_profiles WHERE openid=%s', (openid,)).fetchone()
        profile = json.loads(row['profile']) if row else {}
    
    return api_ok({'profile': profile})

@api_v1_bp.route('/profile/summary', methods=['POST'])
def save_summary():
    """保存会话摘要文本（无需登录）"""
    data = request.get_json() or {}
    openid = data.get('openid')
    summary = data.get('summary', '')
    
    if not openid:
        return api_err('openid是必需的', 400)
    
    from models import get_db
    with get_db() as conn:
        now = datetime.now().isoformat()
        existing = conn.execute('SELECT created_at FROM mp_profiles WHERE openid=%s', (openid,)).fetchone()
        if existing:
            conn.execute(
                'UPDATE mp_profiles SET summary=%s, updated_at=%s WHERE openid=%s',
                (summary, now, openid)
            )
        else:
            conn.execute(
                'INSERT INTO mp_profiles (openid, summary, created_at, updated_at) VALUES (%s, %s, %s, %s)',
                (openid, summary, now, now)
            )
        conn.commit()
    
    return api_ok({})

# =============================================
# 知识库与RAG检索
# =============================================

@api_v1_bp.route('/knowledge/list', methods=['POST'])
def list_knowledge():
    """获取知识库列表，支持 scope 过滤"""
    payload, error = require_auth()
    if error:
        return error
    
    data = request.get_json() or {}
    keyword = data.get('keyword')
    category = data.get('category')
    scope = data.get('scope')  # 可选 'system' | 'user'
    page = data.get('page', 1)
    page_size = data.get('pageSize', 10)
    
    # 显式权限校验
    if scope:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
        from services.kb_permission import check_kb_permission
        allowed, err = check_kb_permission(scope, None, 'read', payload)
        if not allowed:
            return err
    
    # 知识库列表获取逻辑
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
        from models import get_db
        
        with get_db() as db:
            query = "SELECT * FROM knowledge_blocks WHERE deleted_at IS NULL"
            params = []
            
            if scope:
                query += " AND scope=%s"
                params.append(scope)
            
            if keyword:
                query += " AND (title LIKE %s OR content LIKE %s OR keywords LIKE %s)"
                params.extend([f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'])
            
            if category:
                query += " AND category=%s"
                params.append(category)
            
            query += " ORDER BY priority DESC, created_at DESC"
            
            # 分页
            offset = (page - 1) * page_size
            query += " LIMIT %s OFFSET %s"
            params.extend([page_size, offset])
            
            rows = db.execute(query, params).fetchall()

            # 科研版密级过滤（仅 edu 生效；非 edu 行为不变）
            if os.getenv('DEPLOY_TYPE', '').strip().lower() == 'edu' and rows:
                from services.kb_permission import check_confidentiality
                role = payload.get('role', 'user')
                user_id = payload.get('user_id')
                # 批量查询项目成员角色（避免逐行查询）
                project_ids = {str(r['project_id']) for r in rows if r.get('project_id')}
                memberships = {}
                if project_ids:
                    try:
                        phs = ','.join(['%s'] * len(project_ids))
                        ms = db.execute(
                            f'SELECT project_id, role FROM project_workspace.project_members '
                            f'WHERE user_id=%s AND project_id IN ({phs})',
                            [str(user_id)] + sorted(project_ids)).fetchall()
                        memberships = {str(m['project_id']): m['role'] for m in ms}
                    except Exception:
                        memberships = {}
                rows = [r for r in rows
                        if r.get('scope') == 'user' and str(r.get('owner_id')) == str(user_id)
                        or role == 'super_admin'
                        or check_confidentiality(r.get('confidentiality') or 'internal',
                                                 role, memberships.get(str(r.get('project_id')), ''))]
            
            # 获取总数
            count_query = "SELECT COUNT(*) as total FROM knowledge_blocks WHERE deleted_at IS NULL"
            count_params = []
            if scope:
                count_query += " AND scope=%s"
                count_params.append(scope)
            if keyword:
                count_query += " AND (title LIKE %s OR content LIKE %s OR keywords LIKE %s)"
                count_params.extend([f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'])
            if category:
                count_query += " AND category=%s"
                count_params.append(category)
            
            total = db.execute(count_query, count_params).fetchone()['total']
            
            result = [{
                'id': row['id'],
                'title': row['title'],
                'content': row['content'],
                'keywords': row['keywords'].split(',') if row['keywords'] else [],
                'category': row['category'],
                'priority': row['priority'],
                'scope': row.get('scope', 'system'),
                'owner_id': row.get('owner_id'),
                'discipline': row.get('discipline', ''),
                'sub_discipline': row.get('sub_discipline', ''),
                'confidentiality': row.get('confidentiality', 'internal'),
                'project_id': str(row['project_id']) if row.get('project_id') else None,
                'updatedAt': str(row['updated_at']) if row.get('updated_at') else None,
                'createdAt': row['created_at']
            } for row in rows]
            
            return api_ok({
                'items': result,
                'total': total,
                'page': page,
                'pageSize': page_size,
                'pages': max(1, (total + page_size - 1) // page_size)
            })
    except Exception as e:
        import logging
        logging.error(f"[API] 获取知识库列表失败: {e}")
        return api_err(f'获取知识库失败: {str(e)}', 500)

@api_v1_bp.route('/knowledge/save', methods=['POST'])
def save_knowledge():
    """新增/更新知识块（含 scope 权限校验）"""
    payload, error = require_auth()
    if error:
        return error
    
    data = request.get_json() or {}
    kb_id = data.get('id')
    title = data.get('title')
    content = data.get('content')
    keywords = data.get('keywords', [])
    category = data.get('category')
    priority = data.get('priority', 0)
    scope = data.get('scope', 'user')  # 默认用户KB
    owner_id = data.get('owner_id') or payload['user_id']
    # 科研版增强字段（可选）
    confidentiality = data.get('confidentiality', 'internal')
    discipline = data.get('discipline', '')
    sub_discipline = data.get('sub_discipline', '')
    project_id = data.get('project_id')  # UUID 字符串或 None
    metadata = data.get('metadata') or {}
    if confidentiality not in ('public', 'internal', 'secret'):
        confidentiality = 'internal'
    
    if not kb_id or not title or not content:
        return api_err('id, title和content是必需的', 400)
    
    # 权限检查
    from services.kb_permission import check_kb_permission
    allowed, err = check_kb_permission(scope, owner_id, 'write', payload)
    if not allowed:
        return err
    
    # 科研版密级约束：secret 仅超级管理员可授予
    if confidentiality == 'secret' and payload.get('role') != 'super_admin':
        return api_err('保密级(secret)知识仅超级管理员可设置', 403)
    
    # 知识块保存逻辑
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
        from models import get_db
        
        keywords_str = ','.join(keywords) if isinstance(keywords, list) else str(keywords)
        
        with get_db() as db:
            # 检查是否已存在
            existing = db.execute("SELECT id, scope, owner_id FROM knowledge_blocks WHERE id=%s", (kb_id,)).fetchone()
            
            if existing:
                # 更新前检查原条目的操作权限
                ex = dict(existing)
                allowed, err = check_kb_permission(ex['scope'], ex['owner_id'], 'write', payload)
                if not allowed:
                    return err
                # 更新
                db.execute("""
                    UPDATE knowledge_blocks 
                    SET title=%s, content=%s, keywords=%s, category=%s, priority=%s,
                        scope=%s, owner_id=%s, discipline=%s, sub_discipline=%s,
                        project_id=%s, confidentiality=%s, metadata=%s::jsonb, updated_at=NOW()
                    WHERE id=%s
                """, (title, content, keywords_str, category, priority,
                      scope, owner_id, discipline, sub_discipline,
                      project_id, confidentiality, json.dumps(metadata, ensure_ascii=False), kb_id))
                db.commit()
                # 更新后重新生成 embedding（向量路；失败静默）
                try:
                    from agent_matrix.rag_retriever import store_embedding
                    store_embedding(kb_id, title, content)
                except Exception:
                    pass
                return api_ok({'id': kb_id, 'message': '知识块已更新'})
            else:
                # 新增
                db.execute("""
                    INSERT INTO knowledge_blocks (id, title, content, keywords, category, priority, scope, owner_id,
                                                 discipline, sub_discipline, project_id, confidentiality, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (kb_id, title, content, keywords_str, category, priority, scope, owner_id,
                      discipline, sub_discipline, project_id, confidentiality,
                      json.dumps(metadata, ensure_ascii=False)))
                db.commit()
                # 新增后生成 embedding（向量路；失败静默）
                try:
                    from agent_matrix.rag_retriever import store_embedding
                    store_embedding(kb_id, title, content)
                except Exception:
                    pass
                return api_ok({'id': kb_id, 'message': _('Knowledge block created')})
    except Exception as e:
        import logging
        logging.error(f"[API] 保存知识块失败: {e}")
        return api_err(f'保存知识块失败: {str(e)}', 500)

@api_v1_bp.route('/knowledge/delete', methods=['POST'])
def delete_knowledge():
    """删除知识块（系统KB禁止删除，用户KB需权限校验）"""
    payload, error = require_auth()
    if error:
        return error
    
    data = request.get_json() or {}
    kb_id = data.get('id')
    
    if not kb_id:
        return api_err(_('id is required'), 400)
    
    # 知识块删除逻辑
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
        from models import get_db
        from services.kb_permission import check_kb_permission
        
        with get_db() as db:
            row = db.execute("SELECT id, scope, owner_id FROM knowledge_blocks WHERE id=%s AND deleted_at IS NULL", (kb_id,)).fetchone()
            if not row:
                return api_err(_('Knowledge block not found'), 404)
            
            row = dict(row)
            allowed, err = check_kb_permission(row['scope'], row['owner_id'], 'delete', payload)
            if not allowed:
                return err
            
            result = db.execute("UPDATE knowledge_blocks SET deleted_at=NOW() WHERE id=%s AND deleted_at IS NULL", (kb_id,)).rowcount
            db.commit()
            
            if result > 0:
                return api_ok({'id': kb_id, 'message': _('Knowledge block deleted')})
            else:
                return api_err(_('Knowledge block not found'), 404)
    except Exception as e:
        import logging
        logging.error(f"[API] {_('Failed to delete knowledge block')}: {e}")
        return api_err(f"{_('Failed to delete knowledge block')}: {str(e)}", 500)


# =============================================
# 系统知识库在线更新（仅超级管理员）
# =============================================

@api_v1_bp.route('/kb/system-version', methods=['GET'])
def system_kb_version():
    """获取当前系统知识库版本信息"""
    payload, error = require_auth()
    if error:
        return error

    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
        from models import get_db
        
        with get_db() as db:
            current = db.execute(
                "SELECT * FROM system_kb_version WHERE applied=TRUE ORDER BY applied_at DESC LIMIT 1"
            ).fetchone()
            block_count = db.execute(
                "SELECT COUNT(*) as c FROM knowledge_blocks WHERE scope='system' AND deleted_at IS NULL"
            ).fetchone()['c']

        return api_ok({
            'current_version': dict(current) if current else None,
            'system_blocks_count': block_count
        })
    except Exception as e:
        return api_err(f'获取版本失败: {str(e)}', 500)


@api_v1_bp.route('/kb/system-update', methods=['POST'])
def system_kb_update():
    """系统知识库在线更新（仅超级管理员）
    接收 JSON: {version, checksum, blocks, release_notes, update_url}
    blocks: [{id, title, content, keywords, category, priority}, ...]
    采用事务覆盖导入：软删除旧系统条目 → 批量 UPSERT 新条目 → 记录版本
    """
    payload, error = require_auth()
    if error:
        return error

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
    from services.kb_permission import check_kb_permission
    allowed, err = check_kb_permission('system', None, 'update_system', payload)
    if not allowed:
        return err

    data = request.get_json() or {}
    version = data.get('version')
    checksum = data.get('checksum')
    blocks = data.get('blocks', [])
    release_notes = data.get('release_notes', '')
    update_url = data.get('update_url', '')

    if not version or not blocks:
        return api_err('version和blocks是必需的', 400)

    # 校验 checksum
    import json, hashlib
    content = json.dumps(blocks, sort_keys=True, ensure_ascii=False)
    computed = hashlib.sha256(content.encode()).hexdigest()
    if checksum and computed != checksum:
        return api_err(_('Update package verification failed, checksum mismatch'), 400)

    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
        from models import get_db
        
        with get_db() as db:
            # 1. 软删除旧系统条目
            db.execute("UPDATE knowledge_blocks SET deleted_at=NOW() WHERE scope='system'")

            # 2. 批量插入新系统条目
            for block in blocks:
                db.execute(
                    """INSERT INTO knowledge_blocks 
                       (id, title, content, keywords, category, priority, 
                        scope, owner_id, source, quality_score, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,'system',NULL,'system_update',1.0,NOW())
                       ON CONFLICT (id) DO UPDATE SET
                       title=EXCLUDED.title, content=EXCLUDED.content,
                       keywords=EXCLUDED.keywords, category=EXCLUDED.category,
                       priority=EXCLUDED.priority, updated_at=NOW()""",
                    (block['id'], block['title'], block['content'],
                     block.get('keywords', ''), block.get('category', ''),
                     block.get('priority', 0))
                )

            # 3. 记录版本
            db.execute(
                """INSERT INTO system_kb_version 
                   (version, checksum, release_notes, update_url, 
                    applied, applied_at, applied_by)
                   VALUES (%s,%s,%s,%s,TRUE,NOW(),%s)""",
                (version, computed, release_notes, update_url, payload['user_id'])
            )

            db.commit()
            return api_ok({
                'version': version,
                'blocks_count': len(blocks),
                'message': _('System knowledge base updated')
            })
    except Exception as e:
        return api_err(f"{_('Update failed')}: {str(e)}", 500)


@api_v1_bp.route('/rag/search', methods=['POST'])
def rag_search():
    """混合语义检索（无需登录，供抖音小程序调用）"""
    
    data = request.get_json() or {}
    query = data.get('query')
    top_k = data.get('topK', 5)
    category = data.get('category')
    
    if not query:
        return api_err(_('query is required'), 400)
    
    try:
        # 从 knowledge_blocks 表中检索匹配的知识块
        from models import get_db
        with get_db() as conn:
            # 关键词匹配：拆分为单个中文字符+双字组合进行模糊匹配
            chars = list(query.replace(' ', ''))
            bigrams = [query[i:i+2] for i in range(len(query)-1)]
            search_terms = set(chars + bigrams)
            
            # 获取系统知识块（公开端点仅检索系统级知识）
            sql = "SELECT * FROM knowledge_blocks WHERE deleted_at IS NULL AND scope='system'"
            params = []
            if category:
                sql += " AND category=%s"
                params.append(category)
            sql += " ORDER BY priority DESC"
            all_blocks = [dict(r) for r in conn.execute(sql, params).fetchall()]
        
        # 评分：计算查询词与关键词+内容的匹配度
        results = []
        for block in all_blocks:
            score = 0.0
            keywords = (block['keywords'] or '').split(',')
            content = block['content'] or ''
            title = block['title'] or ''
            
            # 关键词匹配（权重0.6）
            kw_matches = sum(1 for kw in keywords if kw and kw in query)
            if kw_matches > 0:
                score += min(kw_matches / len(keywords), 1.0) * 0.6
            
            # 内容/标题字符匹配（权重0.4）
            content_chars = set(content)
            title_chars = set(title)
            char_overlap = len(search_terms & content_chars) / max(len(search_terms), 1)
            title_overlap = len(search_terms & title_chars) / max(len(search_terms), 1)
            score += char_overlap * 0.25 + title_overlap * 0.15
            
            # 精确短语匹配加分
            if query in content:
                score += 0.3
            if query in title:
                score += 0.2
            
            if score > 0:
                results.append({'block': {
                    'id': block['id'],
                    'title': block['title'],
                    'content': block['content'],
                    'category': block['category'],
                    'keywords': block['keywords'],
                }, 'score': round(score, 4)})
        
        # 排序取Top-K
        results.sort(key=lambda x: -x['score'])
        results = results[:min(top_k, 20)]
        
        return api_ok(results if results else [])
        
    except Exception as e:
        logger = __import__('logging').getLogger(__name__)
        logger.error(f'[api_v1] RAG检索失败: {e}')
        return api_ok([])

# =============================================
# 其他业务接口
# =============================================

@api_v1_bp.route('/notify/feishu', methods=['POST'])
def send_feishu_notify():
    """飞书卡片通知代理发送"""
    payload, error = require_auth()
    if error:
        return error
    
    data = request.get_json() or {}
    card_data = data.get('cardData', {})
    webhook_url = data.get('webhookUrl')
    
    # 飞书通知发送逻辑
    if webhook_url:
        try:
            import requests
            import json
            headers = {'Content-Type': 'application/json'}
            response = requests.post(webhook_url, headers=headers, json=card_data, timeout=10)
            if response.status_code == 200:
                return api_ok({'result': 'success', 'message': '飞书通知已发送'})
            else:
                return api_ok({'result': 'failed', 'message': f'飞书返回错误: {response.status_code}'})
        except Exception as e:
            import logging
            logging.warning(f"[API] 飞书通知发送失败: {e}")
            return api_ok({'result': 'error', 'message': str(e)})
    
    return api_ok({'result': 'skipped', 'message': '未提供webhookUrl'})

@api_v1_bp.route('/feedback/save', methods=['POST'])
def save_feedback():
    """保存用户反馈（无需登录，供抖音小程序调用）"""
    data = request.get_json() or {}
    openid = data.get('openid')
    message_id = data.get('messageId')
    feedback = data.get('feedback')
    content = data.get('content')
    query = data.get('query')
    retrieved_ids = data.get('retrievedIds', [])
    ai_reply = data.get('aiReply')
    retrieved_knowledge = data.get('retrievedKnowledge', [])
    timestamp = data.get('timestamp')

    # 验证必填字段
    if not openid:
        return api_err('openid是必需的', 400)
    if not message_id:
        return api_err('messageId是必需的', 400)
    if not feedback:
        return api_err('feedback是必需的', 400)
    if not content:
        return api_err('content是必需的', 400)
    if not query:
        return api_err(_('query is required'), 400)
    if not ai_reply:
        return api_err('aiReply是必需的', 400)
    
    # 用户反馈保存逻辑
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
        from models import get_db
        
        with get_db() as db:
            db.execute("""
                INSERT INTO user_feedback (user_id, type, category, title, content, contact, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (
                None,  # user_id (抖音用户无 user_id)
                'rating',  # type
                'chat',  # category
                f'来自抖音小程序的聊天反馈',  # title
                f'问题: {query}\nAI回复: {ai_reply}\n用户反馈: {feedback or ""}\n评价: {content or ""}',  # content
                openid or '',  # contact (使用 openid)
                'pending'
            ))
            db.commit()
            feedback_id = _cur.fetchone()['id']
            return api_ok({'feedbackId': feedback_id, 'message': '反馈已保存'})
    except Exception as e:
        import logging
        logging.error(f"[API] 保存用户反馈失败: {e}")
        return api_ok({'feedbackId': None, 'message': f'保存失败: {str(e)}'})

@api_v1_bp.route('/visit/increment', methods=['POST'])
def increment_visit():
    """递增用户来访次数并返回最新值（无需登录）"""
    data = request.get_json() or {}
    openid = data.get('openid')
    
    if not openid:
        return api_err('openid是必需的', 400)
    
    # 访问计数递增逻辑
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
        from models import get_db
        
        with get_db() as db:
            # 尝试更新现有记录
            result = db.execute("""
                UPDATE mp_profiles 
                SET visit_count = visit_count + 1, 
                    updated_at = NOW()
                WHERE openid = %s
            """, (openid,)).rowcount
            
            # 如果没有更新任何行，说明是首次访问，需要插入新记录
            if result == 0:
                db.execute("""
                    INSERT INTO mp_profiles (openid, visit_count, created_at, updated_at)
                    VALUES (%s, 1, NOW(), NOW())
                """, (openid,))
            
            db.commit()
            
            # 获取最新的访问次数
            row = db.execute("SELECT visit_count FROM mp_profiles WHERE openid=%s", (openid,)).fetchone()
            visit_count = row['visit_count'] if row else 1
            
            return api_ok({'visitCount': visit_count, 'openid': openid})
    except Exception as e:
        import logging
        logging.error(f"[API] 访问计数更新失败: {e}")
        return api_ok({'visitCount': 1, 'openid': openid, 'error': str(e)})


# =============================================
# TTS (Text-to-Speech) — Public endpoint for mobile/bot clients
# =============================================

@api_v1_bp.route('/tts', methods=['POST'])
def api_tts():
    """Public TTS endpoint for bot voice interaction and mobile clients.

    No login required. Rate limited per IP.

    Request JSON: {text: str, voice?: str}
      - text: Text to synthesize (max 2000 chars).
      - voice: Azure neural voice name (default: zh-CN-XiaoxiaoNeural).

    Response: audio/mpeg binary with Content-Length header.
    """
    data = request.get_json(force=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return api_err(_('Text is required'), 400)
    if len(text) > 2000:
        return api_err(_('Text too long'), 400)

    # Rate limit: 20 requests per minute per IP
    client_ip = request.remote_addr or 'unknown'
    if not _check_rate_limit(f'tts:{client_ip}', max_per_minute=20):
        return api_err(_('Rate limit exceeded'), 429)

    voice = data.get('voice', 'zh-CN-XiaoxiaoNeural')

    try:
        import sys as _sys, os as _os
        _sys.path.insert(
            0, _os.path.join(_os.path.dirname(__file__), '..', '..', 'agent_matrix')
        )
        from audio import AudioOutputProcessor
        processor = AudioOutputProcessor(provider='azure_tts', voice=voice)
        audio = processor.synthesize(text)
        if not audio:
            return api_err(_('TTS synthesis failed'), 500)
        return Response(
            audio,
            mimetype='audio/mpeg',
            headers={'Content-Length': str(len(audio))}
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(
            '[TTS] Public TTS failed: %s', e, exc_info=True
        )
        return api_err(str(e), 500)


# ══ i18n 语言切换 API（规范 §5）══
@api_v1_bp.route('/i18n/lang', methods=['GET', 'POST'])
def i18n_set_lang():
    """GET 返回当前语言；POST {lang} 写入 Cookie 并切换。仅支持 en / zh-CN。"""
    from i18n import _normalize_locale, SUPPORTED_LOCALES, get_lang
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        lang = _normalize_locale(data.get('lang') or request.args.get('lang'))
        if not lang or lang not in SUPPORTED_LOCALES:
            return api_err(_('Unsupported language'), 400)
        resp = api_ok({'lang': lang})
        resp.set_cookie('lang', lang, max_age=365 * 24 * 3600, samesite='Lax')
        return resp
    return api_ok({'lang': get_lang()})
