#!/usr/bin/env python3
"""统一网关 — 入站 Webhook 统一入口（/webhook/<channel>）"""
import hmac
import logging
import os

from flask import Blueprint, request, jsonify

from .events import normalize_event, dispatch_event

logger = logging.getLogger(__name__)

webhook_bp = Blueprint('im_gateway_webhook', __name__, url_prefix='/webhook')

# events.normalize_event 目前支持的事件来源渠道；未知渠道直接 404
_SUPPORTED_WEBHOOK_CHANNELS = ('telegram', 'line')

# 可选入站签名校验：设置 IM_GATEWAY_WEBHOOK_SECRET 后，请求须携带
# 匹配的 X-IM-Webhook-Secret 头，否则拒绝；未设置则记 WARN 并放行（兼容旧部署）。
_WEBHOOK_SECRET = os.environ.get('IM_GATEWAY_WEBHOOK_SECRET', '')


@webhook_bp.route('/<channel>', methods=['POST'])
def ingest(channel):
    """接收各平台 webhook → 归一化 → 按订阅者分发（chatbot 等）"""
    if channel not in _SUPPORTED_WEBHOOK_CHANNELS:
        logger.warning('[Webhook] unknown channel: %s', channel)
        return jsonify({'success': False, 'error': 'Unknown channel'}), 404
    if _WEBHOOK_SECRET:
        supplied = request.headers.get('X-IM-Webhook-Secret', '')
        if not supplied or not hmac.compare_digest(supplied, _WEBHOOK_SECRET):
            logger.warning('[Webhook] signature mismatch on channel: %s', channel)
            return jsonify({'success': False, 'error': 'Invalid signature'}), 403
    else:
        logger.warning('[Webhook] IM_GATEWAY_WEBHOOK_SECRET not set — ingress webhook has no auth')
    raw = request.get_data(as_text=True)
    try:
        event = normalize_event(channel, raw)
    except Exception as e:
        logger.warning('[Webhook] normalize failed %s: %s', channel, e)
        return jsonify({'success': False, 'error': str(e)[:300]}), 400

    dispatch_event(event)
    # 平台协议要求：Telegram 需返回 200 空响应防重试轰炸
    if channel == 'telegram':
        return '', 200
    return jsonify({'success': True})
