"""SSE（Server-Sent Events）解析。

对应真实端点 POST /admin/agent-matrix/chat/stream（agent_matrix/routes.py:1502）：
    首帧  data: {"role":"assistant"}
    中间  data: {<engine.chat_stream 的 chunk>}
    结束  data: [DONE]

两处关键修复：
1. 原实现用 `obj.get("content") or obj.get("delta") or ...` 取内容，
   会把 `{"content": ""}`、`{"content": 0}`、`{"delta": ""}` 这类**假值但有效**
   的帧误判为 meta 从而丢弃。现改为显式判断键是否存在。
2. 增加 Content-Type 校验：若服务端返回的不是 text/event-stream
   （例如许可门禁把请求重定向到续费页、或网关返回 HTML 错误页），
   立刻抛 SSEError，而不是把 HTML 当成对话内容逐字打印出来。

客户端严格遵守服务端门禁：不绕过重定向、不忽略非 2xx（见方案第 14 节）。
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Optional, Tuple

from .exceptions import SSEError

# 内容键的优先级顺序；靠前者优先
_TEXT_KEYS = ("content", "delta", "text", "message")

_SSE_CONTENT_TYPE = "text/event-stream"


def iter_data_payloads(response: Any, *, strict: bool = True) -> Iterator[str]:
    """从 SSE 响应逐行产出 `data:` 后的原始字符串（已去前导空格）。

    strict=True 时校验 Content-Type，非 SSE 直接抛 SSEError。
    """
    if strict:
        ctype = (response.headers.get("Content-Type") or "").lower()
        if _SSE_CONTENT_TYPE not in ctype:
            raise SSEError(
                "服务端返回的不是 SSE 流"
                f"（Content-Type={ctype or '未提供'}）；"
                "可能是许可/订阅门禁重定向、网关错误页或端点不支持流式。"
            )

    for raw in response.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        # SSE 注释行（以 : 开头）与 event:/id:/retry: 字段一律跳过
        if raw.startswith(":"):
            continue
        if raw.startswith("data:"):
            yield raw[len("data:"):].lstrip()


def parse_payload(data_str: str) -> Tuple[str, Any]:
    """解析单个 data payload。

    返回元组：
      ("done", None)            -> 结束帧 [DONE]
      ("role", "assistant")     -> 首帧角色标识，可忽略
      ("chunk", text)           -> 内容块（含空串，不丢帧）
      ("meta", obj)             -> 其它 JSON 对象（如 usage、status）
      ("raw", str)              -> 非 JSON 文本
    """
    s = (data_str or "").strip()
    if s == "[DONE]":
        return ("done", None)
    if not s:
        return ("meta", {})

    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return ("raw", s)

    if not isinstance(obj, dict):
        return ("raw", s)

    has_text_key = any(key in obj for key in _TEXT_KEYS)

    # 首帧 {"role": "assistant"}：仅当不含任何内容键时才当作角色帧
    if obj.get("role") and not has_text_key:
        return ("role", obj.get("role"))

    for key in _TEXT_KEYS:
        if key not in obj:
            continue
        value = obj[key]
        if value is None:
            continue                      # null 视为该键无效，继续看下一个键
        text = _coerce_text(value)
        if text is None:
            continue
        return ("chunk", text)

    # 兼容 OpenAI 风格 chunk：服务端把 SDK chunk 直接 json.dumps 后下发，
    # 内容位于 choices[].delta.content / choices[].message.content。
    text = _extract_from_choices(obj)
    if text is not None:
        return ("chunk", text)

    return ("meta", obj)


def _extract_from_choices(obj: dict) -> Optional[str]:
    """从 OpenAI 风格 chunk（{"choices":[{"delta":{"content":"x"}}]}）提取文本。"""
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return None
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        for key in ("delta", "message"):
            node = ch.get(key)
            if isinstance(node, dict):
                for tk in _TEXT_KEYS:
                    val = node.get(tk)
                    if val is None:
                        continue
                    text = _coerce_text(val)
                    if text is not None:
                        return text
        # 少数 chunk 把内容直接放在 choice 顶层
        for tk in _TEXT_KEYS:
            val = ch.get(tk)
            if val is None:
                continue
            text = _coerce_text(val)
            if text is not None:
                return text
    return None


def _coerce_text(value: Any) -> Optional[str]:
    """把内容键的值规整为可打印字符串。空串是合法内容，必须保留。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        # 兼容 OpenAI 风格嵌套：{"delta": {"content": "hi"}}
        for key in _TEXT_KEYS:
            inner = value.get(key)
            if isinstance(inner, str):
                return inner
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return None
