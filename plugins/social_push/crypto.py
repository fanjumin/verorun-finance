#!/usr/bin/env python3
"""Social Push — 社媒账号凭证加密（Fernet 对称加密）。

复用系统主密钥 ENCRYPTION_KEY（与 provider_api_keys 同密钥体系），
SHA-256 派生为 Fernet 密钥格式。懒加载避免模块导入即崩溃。

无 ENCRYPTION_KEY 时 fail-open 降级：encrypt 原样返回明文、decrypt 原样返回密文，
保证服务可用；服务器配置 ENCRYPTION_KEY 后自动启用加密。
"""

import os
import base64
import hashlib

_cipher = None


def _get_cipher():
    """懒加载 Fernet 实例（首次调用时才派生密钥）。"""
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
    """ENCRYPTION_KEY 是否已配置（与 crypto 派生长度要求一致）。"""
    raw = os.environ.get('ENCRYPTION_KEY')
    return bool(raw and len(raw) >= 16)


def encrypt(plaintext: str) -> str:
    """加密字符串；空值原样返回，密钥缺失时降级明文（fail-open）。"""
    if not plaintext:
        return ''
    if not _crypto_available():
        return plaintext
    try:
        return _get_cipher().encrypt(plaintext.encode()).decode()
    except Exception:
        return plaintext


def decrypt(ciphertext: str) -> str:
    """解密字符串；空值/明文/解密失败时原样返回。"""
    if not ciphertext:
        return ''
    try:
        return _get_cipher().decrypt(ciphertext.encode()).decode()
    except Exception:
        return ciphertext


def mask(value: str, show_first: int = 4, show_last: int = 4) -> str:
    """脱敏显示：'abc123xyz789' → 'abc1****z789'。"""
    if not value or len(value) <= show_first + show_last:
        return '****'
    return value[:show_first] + '****' + value[-show_last:]
