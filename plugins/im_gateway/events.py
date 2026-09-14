#!/usr/bin/env python3
"""入站事件归一化与分发（对标 Chatwoot Channel Event Router）。"""

_EVENT_SUBSCRIBERS = {}  # {'channel': [callable(event)]}


def subscribe(channel: str, handler):
    """消费者订阅：handler(event) -> None"""
    _EVENT_SUBSCRIBERS.setdefault(channel, []).append(handler)


def normalize_event(channel: str, raw_body: str) -> dict:
    """平台原始 body → 统一事件模型：
    {'channel','event_type','sender_id','text','raw'}
    """
    import json
    try:
        data = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        data = {}

    if channel == 'telegram':
        message = data.get('message') or {}
        return {
            'channel': 'telegram',
            'event_type': 'message',
            'sender_id': str((message.get('chat') or {}).get('id', '')),
            'text': (message.get('text') or '').strip(),
            'raw': data,
        }
    if channel == 'line':
        events = data.get('events', [])
        first = events[0] if events else {}
        msg = first.get('message', {})
        return {
            'channel': 'line',
            'event_type': 'message',
            'sender_id': str(first.get('source', {}).get('userId', '')),
            'text': (msg.get('text') or '').strip(),
            'raw': data,
        }
    # 其他平台按需扩展
    return {'channel': channel, 'event_type': 'unknown',
            'sender_id': '', 'text': '', 'raw': data}


def dispatch_event(event: dict):
    """同步分发（后续可升级为异步队列）"""
    for handler in _EVENT_SUBSCRIBERS.get(event['channel'], []):
        try:
            handler(event)
        except Exception:
            import logging
            logging.exception('[Gateway] event dispatch failed: %s', event['channel'])
