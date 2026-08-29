#!/usr/bin/env python3
"""API Key encryption/decryption using Fernet symmetric encryption.
   Requires ENCRYPTION_KEY env var (32-byte hex string, set once)."""
import os, base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# 固定 salt 文件路径，确保同一密钥每次派生结果一致
_SALT_PATH = os.path.join(os.path.dirname(__file__), '.crypto_salt')

def _get_or_create_salt() -> bytes:
    """读取已有 salt 或生成新的随机 salt 并持久化。"""
    if os.path.exists(_SALT_PATH):
        with open(_SALT_PATH, 'rb') as f:
            return f.read()
    salt = os.urandom(16)
    with open(_SALT_PATH, 'wb') as f:
        f.write(salt)
    return salt

def _get_key():
    raw = os.environ.get('ENCRYPTION_KEY') or os.environ.get('DEV_ACCOUNTS_ENCRYPTION_KEY')
    if not raw:
        raise RuntimeError("ENCRYPTION_KEY environment variable is not set")
    # VR-SEC-011: 校验主密钥强度，杜绝空/过短密钥静默使用
    if len(raw) < 16:
        raise RuntimeError("ENCRYPTION_KEY is too weak (minimum 16 characters). "
                           "Generate with: python -c \"import secrets; print(secrets.token_hex(32))\"")
    salt = _get_or_create_salt()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600000)
    return base64.urlsafe_b64encode(kdf.derive(raw.encode()))

_fernet = None

def _get_fernet():
    """懒加载 Fernet 实例，避免模块导入时因缺少 ENCRYPTION_KEY 崩溃。"""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_get_key())
    return _fernet

def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ''
    return _get_fernet().encrypt(plaintext.encode()).decode()

def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ''
    return _get_fernet().decrypt(ciphertext.encode()).decode()