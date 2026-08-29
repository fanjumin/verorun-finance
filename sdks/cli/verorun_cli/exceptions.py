"""VeroRun CLI 异常体系。"""


class VeroRunError(Exception):
    """所有 CLI 错误的基类。"""


class AuthError(VeroRunError):
    """认证失败（未登录 / token 失效 / 凭证错误）。"""


class APIError(VeroRunError):
    """HTTP API 调用返回非 2xx。"""

    def __init__(self, status, message, body=None):
        self.status = status
        self.message = message
        self.body = body
        super().__init__(f"[{status}] {message}")


class SSEError(VeroRunError):
    """SSE 流式解析错误。"""
