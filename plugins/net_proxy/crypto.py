"""net_proxy — 出口通道凭据加密（Fernet 对称加密，fail-closed）。

与 plugins/stock_analysis/crypto.py 同一密钥体系（ENCRYPTION_KEY → SHA-256 →
urlsafe_b64 → Fernet），但**失败策略不同**：

    stock_analysis / social_push / im_gateway / vault 为 fail-open
    （加解密失败或密钥缺失时静默回退原文/明文）；
    **本模块为 fail-closed**（§6.5）—— 代理密码是可横向移动的敏感凭据，
    静默明文化不可接受。密钥不可用时 encrypt() 直接抛 CredentialError，
    由 routes.py 转 **503**（Service Unavailable）并提示配置 ENCRYPTION_KEY，
    绝不明文落库。

    为何是 503 而非 400：密钥缺失/过短、cryptography 未安装、密钥轮换导致解密失败，
    **全是服务端配置或运维问题，与客户端入参无关**。返回 400 会把排查方向
    误导到「用户填错了」。503 + `Retry-After: 0` 明示这是可恢复的配置问题。

解密失败的处理同样是 fail-closed：抛错而非回退密文本身
（回退密文会被当成密码发到代理服务器，产生难以排查的认证失败）。
"""

import os
import base64
import hashlib

__all__ = [
    'CredentialError',
    'crypto_available',
    'encrypt',
    'decrypt',
    'mask',
    'MIN_KEY_LEN',
]

# ENCRYPTION_KEY 最小可用长度（与既有先例 _crypto_available() 一致）
MIN_KEY_LEN = 16

_cipher = None


class CredentialError(RuntimeError):
    """凭据加解密不可用或失败。调用方（routes.py）应转 **503** 并提示配置
    ENCRYPTION_KEY —— 这是服务端配置问题，不是客户端入参错误。"""


def crypto_available() -> bool:
    """ENCRYPTION_KEY 是否可用（存在且长度足够）。"""
    raw = os.environ.get('ENCRYPTION_KEY')
    return bool(raw and len(raw) >= MIN_KEY_LEN)


def _get_cipher():
    """懒加载 Fernet cipher。派生方式与既有先例逐字节一致。"""
    global _cipher
    if _cipher is None:
        raw = os.environ.get('ENCRYPTION_KEY')
        if not raw:
            raise CredentialError(
                'ENCRYPTION_KEY 未配置，无法加密代理凭据')
        if len(raw) < MIN_KEY_LEN:
            raise CredentialError(
                'ENCRYPTION_KEY 长度不足 %d，无法加密代理凭据' % MIN_KEY_LEN)
        try:
            from cryptography.fernet import Fernet
        except ImportError as e:
            raise CredentialError(
                'cryptography 未安装，无法加密代理凭据：%s' % e)
        key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
        _cipher = Fernet(key)
    return _cipher


def encrypt(plaintext: str) -> str:
    """加密明文。空串原样返回（无密码的通道是合法配置）。

    Raises:
        CredentialError: 密钥不可用或加密失败 —— **fail-closed，绝不明文返回**。
    """
    if not plaintext:
        return ''
    try:
        return _get_cipher().encrypt(plaintext.encode()).decode()
    except CredentialError:
        raise
    except Exception as e:
        raise CredentialError('代理凭据加密失败：%s' % e)


def decrypt(ciphertext: str) -> str:
    """解密密文。空串原样返回。

    Raises:
        CredentialError: 解密失败 —— fail-closed，不回退密文
            （回退密文会被当密码发出去，产生难排查的认证失败）。
    """
    if not ciphertext:
        return ''
    try:
        return _get_cipher().decrypt(ciphertext.encode()).decode()
    except CredentialError:
        raise
    except Exception as e:
        raise CredentialError(
            '代理凭据解密失败（密钥可能已更换，请重新登记该通道密码）：%s' % e)


def mask(value: str, show_first: int = 2, show_last: int = 4) -> str:
    """脱敏展示（复用 stock_analysis/crypto.py::mask 语义）。

    前端**永不**回显明文；列表接口只返回本函数结果（`ab****gh` 形态）。
    输入为空或过短统一返回 `****`，不泄露长度信息。
    """
    if not value or len(value) <= show_first + show_last:
        return '****'
    return value[:show_first] + '****' + value[-show_last:]


def mask_channel(channel: dict) -> dict:
    """把通道 dict 的凭据字段替换为掩码，供列表/详情接口返回。

    不改原 dict（浅拷贝后返回）；auth_password_enc 永不出参，
    只出 auth_password_masked；auth_username 保留（便于识别用哪个账号）。
    """
    if not channel:
        return channel
    out = dict(channel)
    enc = out.pop('auth_password_enc', '') or ''
    out['auth_password_masked'] = mask(_safe_decrypt_for_mask(enc)) if enc else ''
    out['has_password'] = bool(enc)
    return out


def _safe_decrypt_for_mask(enc: str) -> str:
    """仅用于生成掩码；解密失败时退化为「已设置但不可读」。

    这不是 fail-open 泄漏路径：返回的字符串只经过 mask() 后出参，
    且永远是脱敏形态，不包含明文。
    """
    try:
        return decrypt(enc)
    except CredentialError:
        return 'set-but-unreadable-secret-value'
