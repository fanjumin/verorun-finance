#!/usr/bin/env python3
"""统一网关 — 入站 Webhook 统一入口（/webhook/<channel>）

批次 D3-a：由"共享 env 密钥 + 未配置即放行"改为**各平台原生验签 + fail-closed**。
密钥来源：channel_configs 各频道已启用配置（telegram 另支持 env 兜底）。
"""
import json
import logging

from flask import Blueprint, request, jsonify

from .events import normalize_event, dispatch_event
from . import webhook_signing

logger = logging.getLogger(__name__)

webhook_bp = Blueprint('im_gateway_webhook', __name__, url_prefix='/webhook')

# events.normalize_event 支持的入站渠道；未知渠道 404
_SUPPORTED_WEBHOOK_CHANNELS = ('telegram', 'line', 'slack', 'discord', 'qq')


@webhook_bp.route('/<channel>', methods=['POST'])
def ingest(channel):
    """接收各平台 webhook → 验签 → 归一化 → 分发"""
    if channel not in _SUPPORTED_WEBHOOK_CHANNELS:
        logger.warning('[Webhook] unknown channel: %s', channel)
        return jsonify({'success': False, 'error': 'Unknown channel'}), 404

    raw = request.get_data(as_text=True)

    # --- 平台原生验签（fail-closed）---
    ok, reason = webhook_signing.verify(channel, request.headers, raw)
    if not ok:
        logger.warning('[Webhook] signature rejected on %s: %s', channel, reason)
        return jsonify({'success': False, 'error': 'Invalid signature', 'reason': reason}), 401

    # --- 平台协议握手（需在验签之后应答）---
    if channel == 'discord':
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if payload.get('type') == 1:  # PING → PONG
            return jsonify({'type': 1})

    if channel == 'qq':
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if payload.get('op') == 13:  # 回调地址验证
            d = payload.get('d') or {}
            cfg = webhook_signing._load_channel_config('qq')
            secret = (cfg.get('bot_secret') or cfg.get('client_secret') or '').strip()
            if not secret:
                return jsonify({'success': False, 'error': 'qq bot_secret not configured'}), 401
            return jsonify(webhook_signing.build_qq_validation_response(
                secret, str(d.get('plain_token', '')), str(d.get('event_ts', ''))))

    # --- 归一化 + 分发 ---
    try:
        event = normalize_event(channel, raw)
    except Exception as e:
        logger.warning('[Webhook] normalize failed %s: %s', channel, e)
        return jsonify({'success': False, 'error': str(e)[:300]}), 400

    dispatch_event(event)
    # Telegram 需返回 200 空响应防重试轰炸
    if channel == 'telegram':
        return '', 200
    return jsonify({'success': True})