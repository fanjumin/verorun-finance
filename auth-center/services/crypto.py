#!/usr/bin/env python3
"""API Key encryption/decryption using Fernet symmetric encryption.
   Requires ENCRYPTION_KEY env var (32-byte hex string, set once)."""
import os, base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# SB-BUG-1 根因修复：salt 必须位于部署树外的持久目录（data/ 不随部署同步），
# 否则部署重建 auth-center/services/ 时 salt 丢失 → 新 salt 派生 key 变化 →
# 已存密文全部 InvalidToken（2026-09-18 事故：全部 LLM 功能静默死亡 3 天）。
# 可用 VR_CRYPTO_SALT_PATH 环境变量显式指定；默认迁移到项目 data/ 目录。
_SALT_PATH = os.environ.get('VR_CRYPTO_SALT_PATH') or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data', '.crypto_salt')
_LEGACY_SALT_PATH = os.path.join(os.path.dirname(__file__), '.crypto_salt')

def _get_or_create_salt() -> bytes:
    """读取已有 salt 或生成新的随机 salt 并持久化。

    优先持久路径；首次运行时若旧部署树内 salt 仍存在则一次性迁移
    （保证派生 key 不变，已存密文可继续解密）。
    """
    if os.path.exists(_SALT_PATH):
        with open(_SALT_PATH, 'rb') as f:
            return f.read()
    # 一次性迁移：旧路径（部署树内）salt 搬到持久路径
    if os.path.exists(_LEGACY_SALT_PATH):
        with open(_LEGACY_SALT_PATH, 'rb') as f:
            salt = f.read()
        try:
            os.makedirs(os.path.dirname(_SALT_PATH), exist_ok=True)
            with open(_SALT_PATH, 'wb') as f:
                f.write(salt)
        except OSError:
            return salt  # 迁移写失败不致命：本轮仍用旧 salt，下次重试
        return salt
    salt = os.urandom(16)
    os.makedirs(os.path.dirname(_SALT_PATH), exist_ok=True)
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