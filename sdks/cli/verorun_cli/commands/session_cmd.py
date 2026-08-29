"""会话与历史命令：sessions / session / search。"""

from __future__ import annotations

import click

from ..output import emit_json, render
from ._base import exclusive, get, post, seg


def register(group):
    @group.command("sessions")
    @click.pass_context
    def sessions(ctx):
        """列出对话历史会话（GET /admin/agent-matrix/chat/history）

        注：服务端在 agent_matrix/routes.py:945 将 limit 硬编码为 30 并忽略查询
        参数，因此此处不提供 --limit（原实现对服务端无效，属安慰剂标志，已移除）。
        如需变更条数请调整服务端源。
        """
        get(ctx, "/admin/agent-matrix/chat/history")

    @group.command("session")
    @click.argument("session_id")
    @click.option("--clear", is_flag=True, help="清空该会话")
    @click.option("--rm", is_flag=True, help="删除该会话")
    @click.pass_context
    def session(ctx, session_id, clear, rm):
        """查看 / 清空 / 删除某个会话"""
        exclusive(clear=clear, rm=rm)
        sid = seg(session_id)  # 防目录穿越 / 非法字符
        if rm:
            post(ctx, f"/admin/agent-matrix/chat/batch-delete",
                 "删除", {"session_ids": [session_id]})
        elif clear:
            post(ctx, f"/admin/agent-matrix/chat/{sid}/clear", "清空")
        else:
            get(ctx, f"/admin/agent-matrix/chat/{sid}")

    @group.command("search")
    @click.argument("keyword")
    @click.pass_context
    def search(ctx, keyword):
        """搜索对话（GET /admin/agent-matrix/chat/search）"""
        get(ctx, "/admin/agent-matrix/chat/search", {"q": keyword})
