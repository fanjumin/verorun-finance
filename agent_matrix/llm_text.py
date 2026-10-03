#!/usr/bin/env python3
"""LLM 流式产出归一化 —— 内核层单一真源。

背景（DEF-007 / DEF-008）：引擎重构后 `UnifiedLLM.chat_stream()` 产出的是
openai SDK 的 `ChatCompletionChunk` **对象**（见 engine.py `_tracked_stream`，
它 `yield chunk` 而非文本）。所有仍按旧「字符串 token」协议消费的调用方，
都会把该对象的 `repr` 当成回复内容拼给用户——表现为一屏
``ChatCompletionChunk(id='...', choices=[Choice(delta=...)])`` 调试垃圾，
单次请求可达 180KB。

受影响面横跨插件与内核/主站，因此归一化实现必须放在**内核层**，
由所有消费方共用；插件不再各自维护一份（避免双写不同源）。

对外提供：
  * `iter_stream_text()` — 把 chat_stream 产出归一化为文本 token 流。
    字符串与 `Error:` 前缀帧原样透传，保留各调用方既有的错误判定语义。
"""
from __future__ import annotations

from typing import Any, Iterator

ERROR_SENTINEL = 'Error:'


def chunk_text(chunk: Any) -> str:
    """从 ChatCompletionChunk 取本轮增量文本；取不到返回 ''。

    只取 `delta.content`，**不取** `reasoning_content`——deepseek-r1 等推理
    模型的思维链不应出现在给用户的回复里。
    """
    try:
        choices = getattr(chunk, 'choices', None)
        if not choices:
            return ''
        delta = getattr(choices[0], 'delta', None)
        return getattr(delta, 'content', '') or ''
    except Exception:
        return ''


def iter_stream_text(stream, error_sentinel: str = ERROR_SENTINEL) -> Iterator[str]:
    """把 chat_stream 的产出归一化为文本 token 流。

    兼容三种历史形态，因为不同版本的引擎与不同供应商客户端并不一致：
      - ``None``    → 跳过（引擎用它表示空帧）
      - ``str``     → 原样透传（含 `Error:` 前缀的错误帧，交调用方判定）
      - chunk 对象  → 解包 `choices[0].delta.content`，空则跳过
    """
    for item in stream:
        if item is None:
            continue
        if isinstance(item, str):
            yield item
            continue
        content = chunk_text(item)
        if content:
            yield content


def collect_stream_text(stream, error_sentinel: str = ERROR_SENTINEL) -> str:
    """非流式等价物：把整个流拼成一段文本（供意图分类等一次性消费场景）。"""
    return ''.join(iter_stream_text(stream, error_sentinel))
