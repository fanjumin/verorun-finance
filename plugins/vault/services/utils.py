#!/usr/bin/env python3
"""
Vault Utilities — Shared helpers for backup/restore services.

Provides common functions to eliminate code duplication across modules.
"""

import os
import re
import base64
from typing import Dict

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')
BACKUP_DIR = os.path.join(BASE_DIR, 'data', 'vault')

_SCHEMA_ENSURED = False

# 敏感字段名清单 — 存储时加密、读取时解密/脱敏
SENSITIVE_FIELDS = {
    'access_key', 'secret_key', 'password', 'connection_string',
    'api_key', 'token', 'smtp_password', 'credentials',
}

# 备份标签白名单 — 仅允许字母、数字、下划线、连字符
_LABEL_RE = re.compile(r'^[a-zA-Z0-9_-]+$')

# 惰性初始化的密钥加密器（复用 VaultEncryptor 的 AES-256-GCM，密钥来自 VAULT_ENCRYPTION_KEY）
_SECRET_CIPHER = None
_SECRET_WARNED = False


def _get_secret_cipher():
    """获取凭据加密器；未配置 VAULT_ENCRYPTION_KEY 时返回 False。

    仅在成功创建时缓存实例；失败时每次重试，确保密钥配置后立即生效。
    """
    global _SECRET_CIPHER
    if _SECRET_CIPHER is not None:
        return _SECRET_CIPHER
    try:
        from .encryptor import VaultEncryptor
        _SECRET_CIPHER = VaultEncryptor(key_source='env')
        return _SECRET_CIPHER
    except Exception:
        return False  # 密钥未配置，不缓存


def encrypt_secret(value) -> str:
    """加密敏感字段。格式: enc:<base64(nonce+ciphertext)>。

    未配置 VAULT_ENCRYPTION_KEY 时原样返回并告警（避免功能不可用）。
    """
    if value is None or value == '':
        return value
    cipher = _get_secret_cipher()
    if not cipher:
        global _SECRET_WARNED
        if not _SECRET_WARNED:
            print('[Vault] WARNING: VAULT_ENCRYPTION_KEY not set — '
                  'sensitive fields stored in plaintext')
            _SECRET_WARNED = True
        return value

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aesgcm = AESGCM(cipher._key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, str(value).encode('utf-8'), None)
    return 'enc:' + base64.b64encode(nonce + ct).decode('ascii')


def decrypt_secret(value) -> str:
    """解密敏感字段。非 enc: 前缀或解密失败时回退原值（兼容旧明文数据）。"""
    if not isinstance(value, str) or not value.startswith('enc:'):
        return value
    cipher = _get_secret_cipher()
    if not cipher:
        return value
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aesgcm = AESGCM(cipher._key)
        raw = base64.b64decode(value[4:])
        nonce, ct = raw[:12], raw[12:]
        return aesgcm.decrypt(nonce, ct, None).decode('utf-8')
    except Exception:
        return value


def encrypt_config_secrets(config: dict) -> dict:
    """加密 config 字典中的敏感字段（用于写入数据库前）。"""
    if not config:
        return config
    masked = dict(config)
    for key in SENSITIVE_FIELDS:
        if key in masked and masked[key]:
            masked[key] = encrypt_secret(masked[key])
    return masked


def decrypt_config_secrets(config: dict) -> dict:
    """解密 config 字典中的敏感字段（用于读取数据库后使用）。"""
    if not config:
        return config
    unmasked = dict(config)
    for key in SENSITIVE_FIELDS:
        if key in unmasked and unmasked[key]:
            unmasked[key] = decrypt_secret(unmasked[key])
    return unmasked


def mask_config_secrets(config: dict) -> dict:
    """脱敏 config 中的敏感字段（用于 API 返回给前端展示）。"""
    if not config:
        return config
    masked = dict(config)
    for key in SENSITIVE_FIELDS:
        if key in masked and masked[key]:
            masked[key] = '******'
    return masked


def safe_backup_path(label: str, suffix: str = '.tar.gz') -> str:
    """安全构造备份文件路径，防路径遍历。

    - 标签白名单校验（仅字母/数字/下划线/连字符）
    - realpath 归属校验，确保最终路径仍在 BACKUP_DIR 下（含符号链接防护）
    """
    if not label or not _LABEL_RE.match(label):
        raise ValueError('Invalid backup label')
    base_dir = os.path.realpath(BACKUP_DIR)
    full_path = os.path.realpath(os.path.join(base_dir, label + suffix))
    if not full_path.startswith(base_dir + os.sep):
        raise ValueError('Path traversal detected')
    return full_path


def safe_join(base_dir: str, rel_path: str) -> str:
    """在 base_dir 下安全拼接相对路径，防目录穿越（用于静态资源等）。"""
    base = os.path.realpath(base_dir)
    full = os.path.realpath(os.path.join(base, rel_path))
    if not full.startswith(base + os.sep):
        raise ValueError('Path traversal detected')
    return full


def get_vault_conn():
    """Return a raw psycopg2 connection pinned to the vault schema.

    Follows plugin-standard v1.3 §9.1 (single DB, per-plugin schema):
    plugin tables live in the `vault` schema; unqualified system tables
    (public) still resolve through the trailing 'public'.
    """
    from plugins._base.db import get_raw_connection
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute('SET search_path TO vault, public')
    cur.close()
    return conn


def ensure_schema():
    """Idempotently apply vault migrations so all vault_* tables exist.

    Safe to call on every request: the migration SQL only uses
    CREATE SCHEMA / CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS,
    and a module flag short-circuits after the first successful run per process.
    """
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return True
    try:
        migration_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', 'migrations', '001_initial.sql')
        if not os.path.exists(migration_path):
            print('[Vault] ensure_schema: migration file missing: %s' % migration_path)
            return False
        with open(migration_path, 'r', encoding='utf-8') as f:
            sql = f.read()
        conn = get_vault_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            conn.commit()
            cur.close()
        finally:
            conn.close()
        print('[Vault] Database schema ensured (migrations/001_initial.sql)')
        _SCHEMA_ENSURED = True
        return True
    except Exception as e:
        print('[Vault] ensure_schema failed: %s' % e)
        return False


def get_pg_env() -> Dict[str, str]:
    """Read .env for PostgreSQL connection info."""
    env = {}
    env_path = os.path.join(BASE_DIR, '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env
