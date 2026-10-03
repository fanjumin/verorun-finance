#!/usr/bin/env python3
"""IM Gateway — 入站 Webhook 平台原生验签（批次 D3-a）

统一策略：fail-closed —— 密钥缺失或签名不匹配一律拒绝，绝不"未配置即放行"。
各平台机制：
  - telegram : 常量时间比较 X-Telegram-Bot-Api-Secret-Token（值来自 channel_configs.secret_token 或 env 兜底）
  - line     : X-Line-Signature = base64(HMAC-SHA256(channel_secret, body))
  - slack    : X-Slack-Signature = 'v0=' + HMAC-SHA256(signing_secret, 'v0:{ts}:{body}')，且 ts 新鲜度 ±5 分钟
  - discord  : X-Signature-Ed25519 对 (timestamp + body) 的 Ed25519 验签，公钥来自 channel_configs.application_public_key
  - qq       : Ed25519 验签，公钥由机器人 Secret 派生（官方约定：私钥 = sha256(secret)），
               同时支持回调地址验证（op=13）的签名应答
"""
import base64
import hashlib
import hmac
import json
import time

# Slack 时间戳新鲜度窗口（秒），防重放
_SLACK_MAX_SKEW = 300


def _load_channel_config(channel: str) -> dict:
    """读取该频道已启用配置（失败返回空 dict，交由各验签函数判定 fail-closed）"""
    try:
        from .models import get_im_db
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel=? AND is_enabled=1 LIMIT 1",
                (channel,),
            ).fetchone()
        if not row or not row['config_json']:
            return {}
        return json.loads(row['config_json'])
    except Exception:
        return {}


def verify_telegram(headers, body: str, secret: str) -> tuple:
    if not secret:
        return False, 'telegram secret not configured'
    supplied = headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not supplied or not hmac.compare_digest(supplied, secret):
        return False, 'invalid telegram secret token'
    return True, ''


def verify_line(headers, body: str, channel_secret: str) -> tuple:
    if not channel_secret:
        return False, 'line channel_secret not configured'
    supplied = headers.get('X-Line-Signature', '')
    if not supplied:
        return False, 'missing X-Line-Signature'
    digest = hmac.new(channel_secret.encode('utf-8'), body.encode('utf-8'), hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode('utf-8')
    if not hmac.compare_digest(supplied, expected):
        return False, 'invalid line signature'
    return True, ''


def verify_slack(headers, body: str, signing_secret: str) -> tuple:
    if not signing_secret:
        return False, 'slack signing_secret not configured'
    ts = headers.get('X-Slack-Request-Timestamp', '')
    supplied = headers.get('X-Slack-Signature', '')
    if not ts or not supplied:
        return False, 'missing slack signature headers'
    try:
        if abs(time.time() - int(ts)) > _SLACK_MAX_SKEW:
            return False, 'slack timestamp too old'
    except ValueError:
        return False, 'invalid slack timestamp'
    basestring = 'v0:{}:{}'.format(ts, body)
    digest = hmac.new(signing_secret.encode('utf-8'), basestring.encode('utf-8'), hashlib.sha256).hexdigest()
    expected = 'v0=' + digest
    if not hmac.compare_digest(supplied, expected):
        return False, 'invalid slack signature'
    return True, ''


def _qq_public_key(secret: str):
    """由机器人 Secret 派生 Ed25519 公钥（官方约定：私钥字节 = sha256(secret)）"""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(hashlib.sha256(secret.encode()).digest())
    return private_key.public_key()


def verify_discord(headers, body: str, public_key_hex: str) -> tuple:
    if not public_key_hex:
        return False, 'discord application_public_key not configured'
    sig = headers.get('X-Signature-Ed25519', '')
    ts = headers.get('X-Signature-Timestamp', '')
    if not sig or not ts:
        return False, 'missing discord signature headers'
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.exceptions import InvalidSignature
        pub = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pub.verify(bytes.fromhex(sig), (ts + body).encode('utf-8'))
        return True, ''
    except InvalidSignature:
        return False, 'invalid discord signature'
    except Exception as e:
        return False, 'discord verify error: {}'.format(str(e)[:100])


def verify_qq(headers, body: str, bot_secret: str) -> tuple:
    if not bot_secret:
        return False, 'qq bot_secret not configured'
    sig = headers.get('X-Signature-Ed25519', '')
    ts = headers.get('X-Signature-Timestamp', '')
    if not sig or not ts:
        return False, 'missing qq signature headers'
    try:
        from cryptography.exceptions import InvalidSignature
        _qq_public_key(bot_secret).verify(bytes.fromhex(sig), (ts + body).encode('utf-8'))
        return True, ''
    except InvalidSignature:
        return False, 'invalid qq signature'
    except Exception as e:
        return False, 'qq verify error: {}'.format(str(e)[:100])


def build_qq_validation_response(bot_secret: str, plain_token: str, event_ts: str) -> dict:
    """回调地址验证（op=13）：用派生私钥对 (event_ts + plain_token) 签名"""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    import os as _os
    if not plain_token:
        # 无 token 时为防时序/空值攻击，用随机值避免误签
        plain_token = _os.urandom(8).hex()
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(hashlib.sha256(bot_secret.encode()).digest())
    signature = private_key.sign((event_ts + plain_token).encode('utf-8')).hex()
    return {'plain_token': plain_token, 'signature': signature}


def verify(channel: str, headers, body: str) -> tuple:
    """按频道分派验签。返回 (ok, reason)。任何缺失/异常均 ok=False（fail-closed）。"""
    try:
        cfg = _load_channel_config(channel)
        if channel == 'telegram':
            import os
            secret = (cfg.get('secret_token') or '').strip() or os.environ.get('IM_GATEWAY_WEBHOOK_SECRET', '')
            return verify_telegram(headers, body, secret)
        if channel == 'line':
            return verify_line(headers, body, (cfg.get('channel_secret') or '').strip())
        if channel == 'slack':
            return verify_slack(headers, body, (cfg.get('signing_secret') or '').strip())
        if channel == 'discord':
            return verify_discord(headers, body, (cfg.get('application_public_key') or '').strip())
        if channel == 'qq':
            return verify_qq(headers, body, (cfg.get('bot_secret') or cfg.get('client_secret') or '').strip())
        return False, 'unsupported channel'
    except Exception as e:
        return False, 'verify error: {}'.format(str(e)[:150])