"""email — 邮件账号密码加密（Fernet 对称加密）。

复用系统主密钥 ENCRYPTION_KEY（与 social_push / im_gateway / stock_analysis
同密钥体系），SHA-256 派生为 Fernet 密钥格式。无 ENCRYPTION_KEY 时 fail-open
降级为明文，保证既有部署不因缺密钥而无法收信/发信。
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


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ''
    if not _crypto_available():
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


def mask(value: str, show_first: int = 2, show_last: int = 4) -> str:
    if not value or len(value) <= show_first + show_last:
        return '****'
    return value[:show_first] + '****' + value[-show_last:]
