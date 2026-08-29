"""Agent 管理命令：agents list / get / toggle / test / capabilities。

对应真实端点 GET|POST /admin/agent-matrix/agents*（agent_matrix/routes.py）。
说明：该组端点服务端未实现分页参数，故此处**不提供** --page/--limit，
      避免给出服务端会忽略的安慰剂选项。
"""

from __future__ import annotations

from typing import Any

import click

from ._base import get, post

PREFIX = "/admin/agent-matrix/agents"


def register(group: Any) -> None:
    @group.group("agents")
    def agents():
        """Agent 管理（/admin/agent-matrix/agents*）"""

    @agents.command("list")
    @click.pass_context
    def list_agents(ctx):
        """列出所有 Agent"""
        get(ctx, PREFIX)

    @agents.command("get")
    @click.argument("agent_id", type=int)
    @click.pass_context
    def get_agent(ctx, agent_id):
        """查看单个 Agent 详情"""
        get(ctx, f"{PREFIX}/{agent_id}")

    @agents.command("toggle")
    @click.argument("agent_id", type=int)
    @click.pass_context
    def toggle_agent(ctx, agent_id):
        """启用 / 停用 Agent"""
        post(ctx, f"{PREFIX}/{agent_id}/toggle", "切换")

    @agents.command("test")
    @click.argument("agent_id", type=int)
    @click.pass_context
    def test_agent(ctx, agent_id):
        """测试 Agent"""
        post(ctx, f"{PREFIX}/{agent_id}/test", "测试")

    @agents.command("capabilities")
    @click.argument("agent_id", type=int)
    @click.pass_context
    def capabilities(ctx, agent_id):
        """查看 Agent 能力"""
        get(ctx, f"{PREFIX}/{agent_id}/capabilities")
