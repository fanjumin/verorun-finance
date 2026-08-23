#!/usr/bin/env python3
"""
AI Advisor 多渠道路由核心
=========================
统一处理 Telegram / LINE 等 Webhook 传来的消息：
  1. 从 IM Gateway 独立库读取频道凭证
  2. 调用 AIEngine.chat_stream()
  3. 调用对应平台 API 发送回复
"""
import json
import sys
import os
import ipaddress
import socket
import urllib.request as _ur
from urllib.parse import urlparse

from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('chatbot')


def _assert_public_host(url):
    """§11.3 网络隔离：拒绝内网/回环/链路本地 IP 段，防 SSRF。

    在发起外呼前校验目标主机解析出的所有 IP，命中私网/保留地址直接拒绝。
    """
    host = urlparse(url).hostname
    if not host:
        raise ValueError(f'[Chatbot] 非法 URL，无法解析主机名: {url}')
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f'[Chatbot] 域名解析失败: {host} ({e})')
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise ValueError(f'[Chatbot] 禁止访问内网地址: {host} -> {info[4][0]}')


def _get_channel_config(channel):
    """从 IM Gateway 独立库读取频道配置（已启用）"""
    # 绝对包路径导入，避免 `from models import ...` 因 sys.modules 缓存
    # 解析到 auth-center/models（其无 get_im_db）而 ImportError。
    from plugins.im_gateway.models import get_im_db
    conn = get_im_db()
    row = conn.execute(
        "SELECT config_json FROM channel_configs WHERE channel=%s AND is_enabled=1 LIMIT 1",
        (channel,)
    ).fetchone()
    if not row:
        return None
    return json.loads(row['config_json'])


def _call_ai(user_query, session_id=''):
    """调用 AIEngine，返回 (reply_text, session_id, intent, sentiment)"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    from agent_matrix.engine import UnifiedLLM
    from agent_matrix.intent import classify_intent

    intent, sentiment = classify_intent(user_query)

    # 从独立库读取 chatbot 配置
    from ..models import get_all_configs, get_agent
    cfg = get_all_configs('chatbot')
    if not cfg:
        cfg = {
            'enabled': '1', 'agent_id': 'chat_assistant',
            'provider': 'dashscope', 'model_name': 'qwen-turbo'
        }

    agent = get_agent(cfg.get('agent_id', 'chat_assistant'))

    if agent and agent.get('system_prompt'):
        system_prompt = agent['system_prompt']
        provider = agent.get('provider') or cfg.get('provider', 'dashscope')
        model_name = agent.get('model_name') or cfg.get('model_name', 'qwen-turbo')
    else:
        system_prompt = f"You are AI Advisor. Be friendly, professional and concise. Current user sentiment: {sentiment}"
        provider = cfg.get('provider', 'dashscope')
        model_name = cfg.get('model_name', 'qwen-turbo')

    engine = UnifiedLLM({'provider': provider, 'model_name': model_name, 'system_prompt': system_prompt})
    full_reply = ''
    for token in engine.chat_stream([
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_query[:1000]}
    ]):
        if not token.startswith('Error:'):
            full_reply += token

    if not session_id:
        import hashlib
        session_id = hashlib.md5((user_query + str(__import__('datetime').datetime.now().timestamp())).encode()).hexdigest()[:16]

    return full_reply, session_id, intent, sentiment


# ── Telegram ──

def telegram_handle_webhook(body):
    """处理 Telegram Update，返回 True/False"""
    try:
        data = body if isinstance(body, dict) else json.loads(body)
    except Exception:
        return False

    message = data.get('message') or data.get('edited_message')
    if not message:
        return False

    chat_id = message.get('chat', {}).get('id')
    text = (message.get('text') or '').strip()
    if not chat_id or not text:
        return False

    cfg = _get_channel_config('telegram')
    if not cfg:
        logger.warning('[Telegram] 频道未配置')
        return False
    bot_token = cfg.get('bot_token', '')

    reply, session_id, intent, sentiment = _call_ai(text)

    # 发送回复
    try:
        payload = json.dumps({
            'chat_id': chat_id,
            'text': reply[:4096],
            'parse_mode': 'Markdown'
        }).encode()
        _assert_public_host(f'https://api.telegram.org/bot{bot_token}/sendMessage')
        _ur.urlopen(
            _ur.Request(
                f'https://api.telegram.org/bot{bot_token}/sendMessage',
                data=payload,
                headers={'Content-Type': 'application/json'}
            ),
            timeout=10
        )
    except Exception as e:
        logger.error(f'[Telegram] sendMessage 失败: {e}')

    # 落库到独立库
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from stats import log_session
        log_session(session_id, user_query=text, ai_reply=reply,
                    source='telegram', intent=intent, sentiment=sentiment)
    except Exception:
        pass

    return True


# ── LINE ──

def line_handle_webhook(body):
    """处理 LINE Webhook events，返回 True/False"""
    try:
        data = body if isinstance(body, dict) else json.loads(body)
    except Exception:
        return False

    events = data.get('events', [])
    if not events:
        return False

    cfg = _get_channel_config('line')
    if not cfg:
        logger.warning('[LINE] 频道未配置')
        return False
    token = cfg.get('access_token', '')

    for event in events:
        if event.get('type') != 'message':
            continue
        reply_token = event.get('replyToken')
        msg = event.get('message', {})
        if msg.get('type') != 'text':
            continue
        text = msg.get('text', '').strip()
        if not text or not reply_token:
            continue

        reply, session_id, intent, sentiment = _call_ai(text)

        try:
            payload = json.dumps({
                'replyToken': reply_token,
                'messages': [{'type': 'text', 'text': reply[:2000]}]
            }).encode()
            _assert_public_host('https://api.line.me/v2/bot/message/reply')
            _ur.urlopen(
                _ur.Request(
                    'https://api.line.me/v2/bot/message/reply',
                    data=payload,
                    headers={
                        'Content-Type': 'application/json',
                        'Authorization': f'Bearer {token}'
                    }
                ),
                timeout=10
            )
        except Exception as e:
            logger.error(f'[LINE] replyMessage 失败: {e}')

        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
            from stats import log_session
            log_session(session_id, user_query=text, ai_reply=reply,
                        source='line', intent=intent, sentiment=sentiment)
        except Exception:
            pass

    return True