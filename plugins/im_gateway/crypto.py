#!/usr/bin/env python3
"""统一网关 — 渠道凭据加密（Phase 1）。

复用系统主密钥 ENCRYPTION_KEY + Fernet，与 social_push/crypto.py 同一密钥体系。
未配置 ENCRYPTION_KEY 时返回原文（fail-open），由上层按需决定是否禁用 OAuth 连接。
"""
import os
import base64
import hashlib

_cipher = None


def _get_cipher():
    global _cipher
    if _cipher is None:
        from cryptography.fernet import Fernet
        raw = os.environ.get('ENCRYPTION_KEY')
        if not raw:
            raise RuntimeError('ENCRYPTION_KEY environment variable is not set')
        key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
        _cipher = Fernet(key)
    return _cipher


def _crypto_available() -> bool:
    raw = os.environ.get('ENCRYPTION_KEY')
    return bool(raw and len(raw) >= 16)


def crypto_enabled() -> bool:
    """上层判断 OAuth 连接是否可用（未配置密钥则禁用并提示）"""
    return _crypto_available()


def encrypt(plaintext: str) -> str:
    if not plaintext or not _crypto_available():
        return plaintext
    try:
        return _get_cipher().encrypt(plaintext.encode()).decode()
    except Exception:
        return plaintext


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ''
    try:
        return _get_cipher().decrypt(ciphertext.encode()).decode()
    except Exception:
        return ciphertext
