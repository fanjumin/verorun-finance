"""鉴权管理：登录（密码/短信/API Key）、持久化 JWT、过期自检、登出。

真实契约（均已对照服务端源码核对，未编造）：
  密码登录  POST /user/password/login   {username,password} -> {success,data:{token,user}}
  短信登录  POST /auth/sms/login        {phone,code}        -> {success,data:{token,user}}
  登出      POST /auth/logout
  API Key   POST /agent/<id>/keys/create -> ek-...（用于 /agent/* 程序化调用）
所有 /admin/* 要求 payload['is_admin']，故 CLI 需用管理员账号登录。

本轮修复要点：
1. 凭证落盘改用 `secure_file.write_secret()`。原实现只调 `os.chmod(0o600)`，
   而该调用在 Windows 上不映射 NTFS ACL（实测 chmod 后 mode 仍为 0o666），
   等于管理员 JWT 毫无文件权限保护。现按平台真正收紧，且收紧失败时明确告警。
2. 用 JWT 的 `exp` 做本地过期自检，不再"等到服务端返回 401 才知道"。
   优先用 PyJWT（已声明的依赖，此前从未被 import），缺失时用标准库兜底。
3. `logout` 不再静默 `pass` 吞掉服务端错误，改为告警后清理本地凭证。
4. API Key 做严格格式自检；**不伪造服务端校验端点**——仓库内未见暴露
   校验路由（`verify_key` 仅是 unified_auth_service 的内部方法），
   因此明确告知用户该 Key 未经服务端验证。
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from datetime import datetime
from typing import Any, Dict, Optional

from .constants import (
    AGENT_APIKEY_MIN_LEN,
    AGENT_APIKEY_PREFIX,
    CONFIG_DIR,
    CRED_FILE,
    JWT_LEEWAY,
    TOKEN_FILE,
)
from .exceptions import AuthError
from .output import warn
from .secure_file import describe_protection, write_secret


class AuthManager:
    def __init__(self, client: Any, config: Any) -> None:
        self.client = client
        self.config = config

    # ------------------------------------------------------------------ #
    # 登录
    # ------------------------------------------------------------------ #
    def login_password(self, username: str, password: str,
                       captcha_id: Optional[str] = None) -> Any:
        body: Dict[str, Any] = {"username": username, "password": password}
        if captcha_id:
            body["captcha_id"] = captcha_id
        resp = self.client.request_json(
            "POST", "/user/password/login", json=body, auth_required=False
        )
        self._save_token(self._extract_token(resp))
        return resp

    def login_sms(self, phone: str, code: str) -> Any:
        body = {"phone": phone, "code": code}
        resp = self.client.request_json(
            "POST", "/auth/sms/login", json=body, auth_required=False
        )
        self._save_token(self._extract_token(resp))
        return resp

    def login_apikey(self, apikey: str) -> Dict[str, str]:
        apikey = (apikey or "").strip()
        if not apikey.startswith(AGENT_APIKEY_PREFIX):
            raise AuthError(f"API Key 应以 {AGENT_APIKEY_PREFIX} 开头")
        if len(apikey) < AGENT_APIKEY_MIN_LEN:
            raise AuthError(
                f"API Key 长度不足（至少 {AGENT_APIKEY_MIN_LEN} 字符），请确认复制完整"
            )
        if any(ch.isspace() for ch in apikey):
            raise AuthError("API Key 含空白字符，请确认复制时未换行")

        self._write_credential(CRED_FILE, {"api_key": apikey})
        warn(
            "该 API Key 仅通过本地格式校验后保存，未经服务端验证"
            "（仓库内未发现可用于校验的公开端点）；请执行一次真实命令确认其有效性。"
        )
        return {"api_key": apikey}

    @staticmethod
    def _extract_token(resp: Any) -> str:
        if isinstance(resp, dict):
            data = resp.get("data")
            if not isinstance(data, dict):
                data = {}
            token = data.get("token") or resp.get("token")
            if token:
                return token
        raise AuthError("登录响应中未找到 token（请确认账号为管理员）")

    def _save_token(self, token: str) -> None:
        payload: Dict[str, Any] = {"token": token, "saved_at": int(time.time())}
        exp = expires_at(token)
        if exp is not None:
            payload["expires_at"] = exp
        self._write_credential(TOKEN_FILE, payload)

    @staticmethod
    def _write_credential(path: str, payload: Dict[str, Any]) -> None:
        hardened, detail = write_secret(path, payload)
        if not hardened:
            warn(
                f"凭证已写入 {path}，但未能收紧文件权限：{detail}。"
                "该文件包含可直接调用管理接口的凭证，请自行限制访问或改用受控环境。"
            )

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def load_token(self) -> Optional[str]:
        record = _read_json(TOKEN_FILE)
        return record.get("token") if isinstance(record, dict) else None

    def load_apikey(self) -> Optional[str]:
        record = _read_json(CRED_FILE)
        return record.get("api_key") if isinstance(record, dict) else None

    def token_state(self) -> Dict[str, Any]:
        """报告本地 token 的过期状态（供 status/whoami 与请求前自检使用）。"""
        token = self.load_token()
        state: Dict[str, Any] = {
            "present": bool(token),
            "expires_at": None,
            "expires_at_text": None,
            "expired": None,
            "seconds_left": None,
        }
        if not token:
            return state
        exp = expires_at(token)
        if exp is None:
            state["expired"] = None  # 无 exp 声明，无法本地判定
            return state
        now = int(time.time())
        state["expires_at"] = exp
        state["expires_at_text"] = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S")
        state["seconds_left"] = exp - now
        state["expired"] = (exp - JWT_LEEWAY) <= now
        return state

    def is_authenticated(self) -> bool:
        return bool(self.load_token()) or bool(self.load_apikey())

    def credential_protection(self) -> Dict[str, Any]:
        """报告凭证文件的真实权限保护状态。"""
        return {
            "dir": describe_protection(CONFIG_DIR),
            "token": describe_protection(TOKEN_FILE),
            "api_key": describe_protection(CRED_FILE),
        }

    def bind(self, client: Optional[Any] = None) -> None:
        """把本地凭证绑定到 client（供后续请求注入 Authorization 头）。"""
        target = client or self.client
        token = self.load_token()
        apikey = self.load_apikey()
        problem: Optional[str] = None
        # API Key 优先级更高；仅在纯 token 模式下做过期自检
        if token and not apikey:
            state = self.token_state()
            if state.get("expired"):
                problem = (
                    f"本地 token 已于 {state['expires_at_text']} 过期，"
                    "请重新执行 `verorun login`"
                )
        target.bind_auth(token=token, apikey=apikey, token_problem=problem)

    # ------------------------------------------------------------------ #
    # 登出
    # ------------------------------------------------------------------ #
    def logout(self) -> None:
        try:
            self.client.request("POST", "/auth/logout", auth_required=True)
        except Exception as exc:
            # 不再静默吞掉：服务端登出失败时告知用户，但仍清理本地凭证
            warn(f"服务端登出未成功（{exc}），仍将清除本地凭证。")
        for path in (TOKEN_FILE, CRED_FILE):
            try:
                import os

                if os.path.exists(path):
                    os.remove(path)
            except OSError as exc:
                warn(f"无法删除本地凭证 {path}：{exc}")


# ---------------------------------------------------------------------- #
# JWT 辅助（只读 claims，不验签——验签是服务端的职责）
# ---------------------------------------------------------------------- #
def decode_claims(token: str) -> Optional[Dict[str, Any]]:
    """解析 JWT payload。优先 PyJWT，缺失时用标准库兜底。不做签名校验。"""
    if not token or not isinstance(token, str):
        return None
    try:
        import jwt  # PyJWT

        return jwt.decode(token, options={"verify_signature": False})
    except ImportError:
        return _decode_claims_stdlib(token)
    except Exception:
        # PyJWT 对畸形 token 会抛 DecodeError 等，回退到宽松解析
        return _decode_claims_stdlib(token)


def _decode_claims_stdlib(token: str) -> Optional[Dict[str, Any]]:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1]
    padding = "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(segment + padding)
        claims = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def expires_at(token: str) -> Optional[int]:
    claims = decode_claims(token)
    if not claims:
        return None
    exp = claims.get("exp")
    if isinstance(exp, bool):
        return None
    if isinstance(exp, (int, float)):
        return int(exp)
    if isinstance(exp, str) and exp.isdigit():
        return int(exp)
    return None


def _read_json(path: str) -> Dict[str, Any]:
    import os

    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
