"""对话命令：chat（同步 / SSE 流式）、dispatch。

修复要点：
* `--agent` 改为 `type=int`，由 click 给出友好报错，不再由 `int(agent)` 抛裸 ValueError。
* `--input` 的 JSON 解析失败改报 BadParameter（同类缺陷，审计未覆盖）。
* 删除对 `VeroRunClient._raise_for_status` 的私有方法调用——客户端现已统一
  在流式与非流式路径上检查状态码。
* 异常统一由 `cli.main()` 处理，本模块不再自行 try/except + SystemExit。
"""

from __future__ import annotations

from typing import Any, Optional

import click

from ..output import emit_json, emit_text
from ..sse import iter_data_payloads, parse_payload
from ._base import parse_json_option, show, wants_json

CHAT_PATH = "/admin/agent-matrix/chat"
STREAM_PATH = "/admin/agent-matrix/chat/stream"
DISPATCH_PATH = "/admin/agent-matrix/dispatch"


def register(group: Any) -> None:
    @group.command("chat")
    @click.argument("message")
    @click.option("--session", "-s", "session_id", help="会话 ID（续接历史）")
    @click.option("--agent", "-a", "agent_id", type=int,
                  help="指定 Sub Agent 的数字 ID（走 /chat/stream）")
    @click.option("--stream/--no-stream", default=None,
                  help="是否流式输出；默认：有 --agent 时流式，否则同步 Master 编排")
    @click.pass_context
    def chat(ctx, message, session_id, agent_id, stream):
        """与 VeroRun AI 对话。

        不带 --agent：调用 Master Agent（POST /admin/agent-matrix/chat，同步编排）。
        带 --agent：调用指定 Sub Agent（POST /admin/agent-matrix/chat/stream，SSE 逐字输出）。
        """
        if stream is None:
            stream = agent_id is not None
        if stream:
            _chat_stream(ctx, message, session_id, agent_id)
        else:
            _chat_sync(ctx, message, session_id)

    @group.command("dispatch")
    @click.argument("description")
    @click.option("--agent", "-a", "agent_id", type=int, required=True,
                  help="目标 Sub Agent 的数字 ID")
    @click.option("--title", "-t", default="", help="任务标题")
    @click.option("--input", "input_data", default="", help="输入数据（JSON 字符串）")
    @click.pass_context
    def dispatch(ctx, description, agent_id, title, input_data):
        """直接下发任务给指定 Sub Agent（POST /admin/agent-matrix/dispatch）"""
        payload = {
            "target_agent_id": agent_id,
            "title": title or description[:40],
            "description": description,
            "input_data": parse_json_option(input_data, "--input") or {},
        }
        data = ctx.obj["client"].request_json("POST", DISPATCH_PATH, json=payload)
        show(ctx, data)


def _chat_sync(ctx: click.Context, message: str, session_id: Optional[str]) -> None:
    payload = {"message": message}
    if session_id:
        payload["session_id"] = session_id
    # request_json 在非 2xx / 3xx 时抛错，不会把续费重定向页当成成功响应
    data = ctx.obj["client"].request_json("POST", CHAT_PATH, json=payload)
    show(ctx, data)


def _chat_stream(ctx: click.Context, message: str, session_id: Optional[str],
                 agent_id: Optional[int]) -> None:
    payload: dict = {"message": message}
    if session_id:
        payload["session_id"] = session_id
    if agent_id is not None:
        payload["agent_id"] = agent_id

    # 客户端已统一在流式路径上检查 3xx/4xx/5xx，无需此处手动补检查
    resp = ctx.obj["client"].request("POST", STREAM_PATH, json=payload, stream=True)

    if wants_json(ctx):
        collected = []
        for data_str in iter_data_payloads(resp):
            kind, value = parse_payload(data_str)
            if kind == "chunk":
                collected.append(value)
            elif kind == "done":
                break
        emit_json({"content": "".join(collected)})
        return

    got_output = False
    for data_str in iter_data_payloads(resp):
        kind, value = parse_payload(data_str)
        if kind == "chunk":
            emit_text(value)
            got_output = got_output or value != ""
        elif kind == "done":
            break
    emit_text("\n")
    if not got_output:
        emit_text("")  # 保持 stdout 干净；无内容时不额外提示，交由调用方判断
