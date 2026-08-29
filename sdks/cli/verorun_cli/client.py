"""对 VeroRun 服务端现有 REST 端点的薄封装。

不 import 任何核心模块；只负责拼 URL、注入鉴权头、解析/抛错。

本轮修复要点：
1. **路径消毒**：自由字符串 ID（task_id / session_id / plugin identifier）
   经 `quote_segment()` 转义，并在 `request()` 内拒绝含 `..`/控制字符的路径，
   杜绝拼出 `/admin/agent-matrix/tasks/../../../admin/users/cancel` 这类请求。
2. **流式也检查状态码**：原实现 `if not stream and status >= 400` 导致
   流式 4xx 无法自动捕获，只能靠调用方手动补检查。现已统一。
3. **重定向 fail-closed**：默认 `allow_redirects=False`，3xx 一律按错误处理。
   服务端 client 模式的订阅门禁返回 302 跳转续费页（见方案第 14 节），
   若跟随重定向会把续费页 HTML 当成 200 成功响应，属于静默绕过门禁。
4. 复用单个 `requests.Session`，并允许注入 session 以便离线单元测试。
"""

from __future__ import annotations

import json as _json
import os
from typing import Any, Dict, Mapping, Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry

    _HAS_RETRY = True
except ImportError:  # pragma: no cover - urllib3 随 requests 必装，仅防御
    _HAS_RETRY = False

from .constants import DEFAULT_TIMEOUT, USER_AGENT
from .exceptions import APIError, AuthError, VeroRunError

_DEFAULT_BASE_URL = "http://localhost:8084"


def quote_segment(value: Any) -> str:
    """把单个 URL 路径段安全转义。

    `safe=""` 会编码空格、`/` 等字符，但 `urllib.parse.quote` 始终保留
    `_.-~`（always-safe），因此 `.` 不会被编码——仅靠编码无法中和 `../` 穿越。
    故这里**显式拒绝**任何可能构成目录穿越的字符/序列（/、\\、..），
    不依赖服务端对路径的归一化行为。
    """
    raw = "" if value is None else str(value)
    if not raw:
        raise VeroRunError("路径参数不能为空")
    if raw.strip(".") == "":
        raise VeroRunError(f"非法路径参数（纯点号段）：{raw!r}")
    if "/" in raw or "\\" in raw or ".." in raw:
        raise VeroRunError(f"路径参数含非法字符（/、\\ 或 ..）：{raw!r}")
    return quote(raw, safe="")


def _validate_path(path: str) -> None:
    if not path.startswith("/"):
        raise VeroRunError(f"请求路径必须以 / 开头：{path!r}")
    if any(ord(ch) < 0x20 for ch in path):
        raise VeroRunError("请求路径包含控制字符，已拒绝（防止请求头注入）")
    for segment in path.split("/"):
        if segment in (".", ".."):
            raise VeroRunError(f"请求路径包含目录穿越段：{path!r}")


class VeroRunClient:
    """VeroRun REST 薄客户端。"""

    def __init__(
        self,
        config: Any,
        base_url: Optional[str] = None,
        verify_ssl: Optional[bool] = None,
        timeout: int = DEFAULT_TIMEOUT,
        session: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self.base_url = (base_url or self._resolve_base_url(config)).rstrip("/")
        if verify_ssl is None:
            verify_ssl = config.get("server.verify_ssl", True)
        self.verify_ssl = verify_ssl
        self._token: Optional[str] = None
        self._apikey: Optional[str] = None
        self._token_problem: Optional[str] = None
        self.session = session if session is not None else self._build_session()

    @staticmethod
    def _build_session() -> "requests.Session":
        """构建带连接池与有限重试的 Session。

        重试仅作用于**连接级**错误（DNS/连接超时、TCP 重置）与 429/5xx 这类
        可安全重放的响应；POST 等写操作默认不在幂等重试范围内，避免重复下单。
        CLI 是一次性进程，连接池收益有限，但合理的重试能消掉偶发网络抖动。
        """
        sess = requests.Session()
        if _HAS_RETRY:
            retry = Retry(
                total=2,
                backoff_factor=0.3,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}),
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            sess.mount("http://", adapter)
            sess.mount("https://", adapter)
        return sess

    @staticmethod
    def _resolve_base_url(config: Any) -> str:
        cfg = config.get("server.base_url")
        if cfg:
            return cfg
        env = os.environ.get("VERORUN_BASE_URL")
        if env:
            return env
        # 默认直连 admin gunicorn（开发环境）
        return _DEFAULT_BASE_URL

    # ---- 鉴权绑定（由 AuthManager 在 login/启动时调用）----
    def bind_auth(
        self,
        token: Optional[str] = None,
        apikey: Optional[str] = None,
        token_problem: Optional[str] = None,
    ) -> None:
        self._token = token
        self._apikey = apikey
        self._token_problem = token_problem

    def _auth_headers(self, auth_required: bool) -> Dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if not auth_required:
            return headers
        if self._apikey:
            headers["Authorization"] = f"Bearer {self._apikey}"
            return headers
        if self._token:
            # 本地已能判定 token 过期时，提前给出明确提示，不必等服务端 401
            if self._token_problem:
                raise AuthError(self._token_problem)
            headers["Authorization"] = f"Bearer {self._token}"
            return headers
        raise AuthError(
            "未登录：请先执行 `verorun login`（或用 `verorun login --api-key ek-...`）"
        )

    # ---- 底层请求 ----
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        auth_required: bool = True,
        stream: bool = False,
        headers: Optional[Mapping[str, str]] = None,
        allow_non_ok: bool = False,
        follow_redirects: bool = False,
        timeout: Optional[int] = None,
    ) -> Any:
        _validate_path(path)
        url = f"{self.base_url}{path}"
        final_headers = dict(headers) if headers else self._auth_headers(auth_required)
        try:
            resp = self.session.request(
                method,
                url,
                json=json,
                params=params,
                headers=final_headers,
                verify=self.verify_ssl,
                timeout=timeout or self.timeout,
                stream=stream,
                allow_redirects=follow_redirects,
            )
        except requests.exceptions.SSLError as exc:
            raise APIError(495, f"SSL 校验失败（开发自签证书可加 --insecure）：{exc}")
        except requests.exceptions.ConnectionError as exc:
            raise APIError(0, f"无法连接服务器 {self.base_url}：{exc}")
        except requests.exceptions.Timeout:
            raise APIError(408, f"请求超时（>{timeout or self.timeout}s）：{path}")

        if allow_non_ok:
            return resp

        status = getattr(resp, "status_code", 0)
        # 3xx：fail-closed。绝不跟随，以免静默绕过订阅/许可门禁（方案第 14 节）
        if 300 <= status < 400 and not follow_redirects:
            location = ""
            try:
                location = resp.headers.get("Location") or ""
            except Exception:
                location = ""
            raise APIError(
                status,
                "服务端返回重定向，已按 fail-closed 拒绝跟随"
                f"（Location={location or '未提供'}）。"
                "常见原因：未登录、会话失效，或实例订阅/许可门禁要求续费。",
            )
        # 4xx/5xx：流式与非流式一视同仁（修复原先流式 4xx 无法自动捕获的问题）
        if status >= 400:
            self._raise_for_status(resp)
        return resp

    def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self.request(method, path, **kwargs)
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    @staticmethod
    def _raise_for_status(resp: Any) -> None:
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        if isinstance(body, dict):
            data_field = body.get("data")
            msg = (
                body.get("message")
                or body.get("msg")
                or body.get("error")
                or (data_field if isinstance(data_field, str) else None)
            )
            if not msg and body.get("success") is False:
                msg = "请求失败"
            if not msg:
                msg = resp.text
        else:
            msg = _json.dumps(body, ensure_ascii=False) if body else resp.text
        raise APIError(resp.status_code, str(msg), body)
