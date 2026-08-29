#!/usr/bin/env python3
"""
VeroGuard — 文件完整性校验模块（Phase 2）
=============================================
从签名版 manifest.json + manifest.json.sig 加载基准清单（Ed25519 验签），
SHA256 逐一比对核心文件，返回违规列表。

违反级别:
  critical — 核心认证/授权/守护进程文件
  high     — 关键业务逻辑
  warning  — 其他受保护文件
"""
import hashlib
import json
import logging
import os
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .. import config


def load_manifest() -> dict:
    """加载并验签完整性基准清单（manifest.json + manifest.json.sig，Ed25519）
    VR-SEC-007: 清单缺失/验签失败视为「无法校验」，向上抛出由 run() 按 fail-closed 处理。
    """
    manifest_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'manifest.json'
    )
    sig_path = manifest_path + '.sig'
    if not os.path.exists(manifest_path):
        raise ValueError(f"Manifest not found: {manifest_path}")
    if not os.path.exists(sig_path):
        raise ValueError(f"Manifest signature not found: {sig_path}")

    pub_key_hex = config.RELEASE_VERIFY_KEY
    if not pub_key_hex or pub_key_hex == "REPLACE_WITH_REAL_PUBLIC_KEY_HEX":
        raise ValueError("RELEASE_VERIFY_KEY not configured")

    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_key_hex))
        manifest_text = open(manifest_path, encoding='utf-8').read().strip()
        sig_bytes = bytes.fromhex(open(sig_path, encoding='utf-8').read().strip())
        pub.verify(sig_bytes, manifest_text.encode('utf-8'))
        return json.loads(manifest_text)
    except (ValueError, InvalidSignature, json.JSONDecodeError) as e:
        raise ValueError(f"Manifest signature verification failed: {e}")


def run() -> list:
    """
    校验核心文件，返回违规列表。

    返回值:
        [
            {
                'file': 'auth_server.py',
                'type': 'modified',     # modified | deleted
                'severity': 'critical',
                'expected_hash': 'abc123...',
                'actual_hash': 'def456...'   # 仅 modified 有
            },
            ...
        ]
    """
    try:
        manifest = load_manifest()
    except ValueError as e:
        # VR-SEC-007: 无法校验 = 视为违规，上报 critical，禁止静默放行
        logging.error("Integrity check unavailable: %s", e)
        return [{
            'file': 'integrity/unavailable',
            'type': 'unavailable',
            'severity': 'critical',
            'expected_hash': '',
            'actual_hash': '',
            'reason': str(e),
        }]

    if not manifest.get('files'):
        logging.warning("Manifest has no files — integrity check unavailable")
        return [{
            'file': 'integrity/unavailable',
            'type': 'unavailable',
            'severity': 'critical',
            'expected_hash': '',
            'actual_hash': '',
            'reason': 'Manifest has no files',
        }]

    violations = []
    for entry in manifest['files']:
        path = os.path.join(config.PROJECT_DIR, entry['path'])
        if not os.path.exists(path):
            violations.append({
                'file': entry['path'],
                'type': 'deleted',
                'severity': entry.get('severity', 'warning'),
                'expected_hash': entry['hash'],
            })
            logging.warning("Integrity violation: %s DELETED", entry['path'])
            continue

        with open(path, 'rb') as f:
            actual_hash = hashlib.sha256(f.read()).hexdigest()

        if actual_hash != entry['hash']:
            violations.append({
                'file': entry['path'],
                'type': 'modified',
                'severity': entry.get('severity', 'warning'),
                'expected_hash': entry['hash'],
                'actual_hash': actual_hash,
            })
            logging.warning("Integrity violation: %s MODIFIED", entry['path'])

    return violations
