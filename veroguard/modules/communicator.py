#!/usr/bin/env python3
"""
VeroGuard — 加密通信模块（Phase 3）
=============================================
心跳数据 AES-256-GCM 加密 → HMAC-SHA256 签名 → HTTPS POST 上报。

加密层次:
  传输层: TLS 1.3 (HTTPS)
  请求签名: HMAC-SHA256 (防篡改)
  载荷加密: AES-256-GCM (防嗅探)
  防重放: Nonce + Timestamp (5 分钟窗口)
"""
import hashlib
import hmac
import json
import logging
import secrets
import time as _time
import urllib.request
import base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .. import config


def _derive_key(secret: str, purpose: str) -> bytes:
    """从预共享密钥派生 AES 密钥"""
    return hashlib.sha256(f"{secret}:{purpose}".encode()).digest()


def send_heartbeat(fingerprint: dict, integrity_status: dict,
                   runtime_info: dict) -> dict:
    """
    发送加密心跳到 VeroRun 官方 API。

    返回:
        {
            "status": "ok",
            "commands": [...]    # 远程命令（如有）
        }
        或 {"status": "error", "message": "..."}
    """
    if not config.PROBE_SECRET:
        logging.warning("PROBE_SECRET not set — skipping heartbeat")
        return {"status": "skipped", "message": "PROBE_SECRET not configured"}

    if not config.REMOTE_URL:
        logging.warning("REMOTE_URL not set — skipping heartbeat")
        return {"status": "skipped", "message": "REMOTE_URL not configured"}

    # ── 构建载荷 ──
    payload = json.dumps({
        "deployment_code": config.DEPLOYMENT_CODE,
        "fingerprint": fingerprint,
        "integrity": integrity_status,
        "runtime": runtime_info,
        "timestamp": int(_time.time()),
    }, ensure_ascii=False)

    # ── AES-256-GCM 加密 ──
    nonce = secrets.token_bytes(12)
    key = _derive_key(config.PROBE_SECRET, 'communication')
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, payload.encode(), None)

    # ── HMAC-SHA256 签名 ──
    signature = hmac.new(
        config.PROBE_SECRET.encode(),
        ciphertext + nonce,
        hashlib.sha256
    ).hexdigest()

    # ── 发送 ──
    url = f"{config.REMOTE_URL.rstrip('/')}/api/veroguard/heartbeat"
    try:
        req = urllib.request.Request(
            url,
            data=ciphertext,
            headers={
                'Content-Type': 'application/octet-stream',
                'X-VG-Nonce': base64.b64encode(nonce).decode(),
                'X-VG-Signature': signature,
                'X-VG-Timestamp': str(int(_time.time())),
                'User-Agent': 'VeroGuard/2.0',
            },
            method='POST'
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode())

        # 检查是否更新心跳间隔
        if 'next_heartbeat_seconds' in result:
            # 不直接修改 config（只读模块变量），仅日志记录
            logging.info("Server suggested interval: %ds",
                        result['next_heartbeat_seconds'])

        return result

    except urllib.error.HTTPError as e:
        logging.error("Heartbeat HTTP %s: %s", e.code, e.read()[:200])
        return {"status": "error", "http_code": e.code}
    except urllib.error.URLError as e:
        logging.error("Heartbeat network error: %s", e.reason)
        return {"status": "error", "message": str(e.reason)}
    except Exception as e:
        logging.error("Heartbeat failed: %s", e)
        return {"status": "error", "message": str(e)}


def send_ack(command_id: str, status: str, result: str = '') -> dict:
    """
    发送命令执行确认回执（Phase 4 会用到）。

    参数:
        command_id: 命令 ID
        status: executed | failed
        result: 执行结果描述
    """
    if not config.PROBE_SECRET or not config.REMOTE_URL:
        return {"status": "skipped"}

    payload = json.dumps({
        "deployment_code": config.DEPLOYMENT_CODE,
        "command_id": command_id,
        "status": status,
        "result": result,
        "timestamp": int(_time.time()),
    }, ensure_ascii=False)

    nonce = secrets.token_bytes(12)
    key = _derive_key(config.PROBE_SECRET, 'communication')
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, payload.encode(), None)
    signature = hmac.new(
        config.PROBE_SECRET.encode(),
        ciphertext + nonce,
        hashlib.sha256
    ).hexdigest()

    url = f"{config.REMOTE_URL.rstrip('/')}/api/veroguard/ack"
    try:
        req = urllib.request.Request(
            url, data=ciphertext,
            headers={
                'Content-Type': 'application/octet-stream',
                'X-VG-Nonce': base64.b64encode(nonce).decode(),
                'X-VG-Signature': signature,
                'X-VG-Timestamp': str(int(_time.time())),
                'User-Agent': 'VeroGuard/2.0',
            },
            method='POST'
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    except Exception as e:
        logging.error("ACK failed: %s", e)
        return {"status": "error", "message": str(e)}
