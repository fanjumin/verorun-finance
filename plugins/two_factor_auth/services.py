"""2FA plugin — TOTP / 加密 / 恢复码 核心服务。"""
import logging
from flask import request
import pyotp
import qrcode
import io
import base64
import secrets
import string
import hashlib
import re
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import bcrypt

logger = logging.getLogger(__name__)


def get_encryption_key() -> bytes:
    """从环境变量派生 32 字节密钥（AES-256-GCM 要求 32 字节）。

    必须稳定（重启后不变），否则已加密的 TOTP 密钥无法解密。
    配置方式（.env）：
        TOTP_MASTER_KEY=<至少 32 字符的随机串>
    然后用其 SHA-256 摘要作为密钥，避免直接拿弱口令当密钥。
    """
    raw = (os_environ('TOTP_MASTER_KEY') or '').encode('utf-8')
    if len(raw) < 32:
        # 兜底：不能用随机值（否则重启后旧数据无法解密），因此此处抛错强制配置。
        raise RuntimeError(
            "TOTP_MASTER_KEY 未配置或太短（需 >=32 字符）。"
            "请在 .env 中设置稳定的 TOTP_MASTER_KEY 后重启。"
        )
    return hashlib.sha256(raw).digest()


def os_environ(k):
    import os
    return os.environ.get(k, '')


class TOTPService:
    def __init__(self, encryption_key: bytes, valid_window: int = 1,
                 issuer: str = "VeroRun"):
        if len(encryption_key) != 32:
            raise RuntimeError("加密密钥必须为 32 字节")
        self.cipher = AESGCM(encryption_key)
        self.valid_window = valid_window
        self.issuer = issuer

    # ── 密钥生成与加解密 ──
    def generate_secret(self) -> str:
        return pyotp.random_base32()

    # F-04：AES-GCM 绑定 AAD=user_id，密文与用户上下文关联，防密文被跨用户搬移。
    # 兼容迁移：密文加版本前缀——v1:（遗留格式，aad=None）解密时保持兼容；v2:（新格式，aad=user_id）。
    V1_PREFIX = 'v1:'
    V2_PREFIX = 'v2:'

    def encrypt_secret(self, secret: str, user_id: int) -> tuple:
        iv = secrets.token_bytes(12)
        ct = self.cipher.encrypt(iv, secret.encode(), str(user_id).encode())
        return self.V2_PREFIX + base64.b64encode(ct).decode(), base64.b64encode(iv).decode()

    def decrypt_secret(self, encrypted: str, iv: str, user_id: int) -> str:
        if encrypted.startswith(self.V2_PREFIX):
            aad = str(user_id).encode()
            raw = base64.b64decode(encrypted[len(self.V2_PREFIX):])
        else:
            # v1 遗留格式（aad=None）
            aad = None
            raw = base64.b64decode(encrypted)
        return self.cipher.decrypt(base64.b64decode(iv), raw, aad).decode()

    # ── OTP URI / 二维码 ──
    def get_provisioning_uri(self, secret: str, account: str) -> str:
        # account 用邮箱或用户名；含特殊字符时 pyotp 会自动处理
        return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=self.issuer)

    def generate_qr_code(self, uri: str) -> str:
        qr = qrcode.QRCode(box_size=10, border=4)
        qr.add_data(uri)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode()

    # ── 校验 ──
    def verify_code(self, secret: str, code: str) -> bool:
        code = (code or '').strip().replace(' ', '')
        if not re.fullmatch(r'\d{6}', code):
            return False
        return pyotp.TOTP(secret).verify(code, valid_window=self.valid_window)

    # ── 恢复码 ──
    def generate_recovery_codes(self, count: int) -> list:
        codes = []
        for _ in range(count):
            raw = ''.join(secrets.choice(string.ascii_uppercase + string.digits)
                           for _ in range(10))
            codes.append(f"{raw[:5]}-{raw[5:]}")
        return codes

    def hash_recovery_code(self, code: str) -> str:
        return bcrypt.hashpw(code.replace('-', '').encode(), bcrypt.gensalt()).decode()

    def verify_recovery_code(self, plain: str, hashed: str) -> bool:
        return bcrypt.checkpw(plain.replace('-', '').encode(), hashed.encode())


# ── 登录预检 filter 回调（插件注册到核心扩展点 'auth.before_issue_session'）──
def pre_login_check(**kwargs):
    """登录预检回调：插件启用期间，用户已开启 TOTP 则生成 challenge 并要求第二因子。

    核心在 issue_auth_session 签发前调用
    apply_filters('auth.before_issue_session', None, **kwargs)。
    - scenario != 'login'（如 token refresh）不拦截。
    - 用户未绑定 TOTP 直接放行。
    - 任何异常 fail-open（返回 None，核心原样签发），绝不停在登录。
    返回 {'blocked': True, 'challenge_token': ..., 'redirect': ...} 时，
    核心抛出 TwoFactorRequired，由 app 级 errorhandler 返回 needs_2fa 响应。
    """
    try:
        if kwargs.get('scenario') != 'login':
            return None
        user_id = kwargs.get('user_id')
        if not user_id:
            return None
        app_name = kwargs.get('app_name') or 'main'
        redirect = kwargs.get('redirect') or '/'

        from .models import get_two_factor_db
        with get_two_factor_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_totp WHERE user_id=%s AND is_enabled=true",
                (user_id,)).fetchone()
        if not row:
            return None

        import secrets
        from datetime import datetime, timedelta, timezone
        cid = secrets.token_urlsafe(32)
        exp = datetime.now(timezone.utc) + timedelta(minutes=5)
        # F-06：记录生成时设备上下文，/challenge/verify 时宽松比对（防泄露后被他人完成登录）
        ip = request.remote_addr or ''
        ua = request.headers.get('User-Agent', '')[:200]
        with get_two_factor_db() as conn:
            conn.execute(
                "INSERT INTO two_factor_challenges "
                "(user_id, challenge_id, method, expires_at, app_name, ip_address, user_agent) "
                "VALUES (%s,%s,'totp',%s,%s,%s,%s)",
                (user_id, cid, exp, app_name, ip, ua))
            conn.commit()
        # F-07：审计 challenge 生成
        _audit_challenge_generated(user_id)
        return {'blocked': True, 'challenge_token': cid, 'redirect': redirect}
    except Exception as e:
        # F-03：fail-open 但必须可见——原实现完全静默，DB 故障时 2FA 静默失效无从排查
        logger.warning("2FA precheck failed, fail-open (login proceeds without 2FA): %s", e)
        return None


# ── 审计（F-07：challenge 生成审计；写失败记 error，不再静默吞错）──
def _audit_challenge_generated(user_id):
    try:
        from .models import get_two_factor_db
        with get_two_factor_db() as conn:
            conn.execute(
                "INSERT INTO audit_log (user_id, action, ip_address, user_agent) "
                "VALUES (%s,'challenge_generated',%s,%s)",
                (user_id, request.remote_addr or '',
                 request.headers.get('User-Agent', '')[:500]))
            conn.commit()
    except Exception as e:
        logger.error("2FA audit write failed (challenge_generated): %s", e)
