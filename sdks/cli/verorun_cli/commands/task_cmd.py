"""任务命令：tasks / task(cancel|retry|logs)。

修复要点：
* `task_id` 是自由字符串，经 `seg()` 转义后再拼进 URL，防止目录穿越。
* `--cancel/--retry/--logs` 互斥时显式报错，不再静默按 if/elif 取第一个。
* `--limit` 为服务端真实支持的参数（agent_matrix/routes.py:355）。
"""

from __future__ import annotations

from typing import Any

import click

from ._base import exclusive, get, post, seg

PREFIX = "/admin/agent-matrix/tasks"


def register(group: Any) -> None:
    @group.command("tasks")
    @click.option("--recent", is_flag=True, help="仅查看最近任务（/tasks/recent）")
    @click.option("--limit", type=int, default=None,
                  help="返回条数（服务端默认 50）")
    @click.pass_context
    def tasks(ctx, recent, limit):
        """列出任务（GET /admin/agent-matrix/tasks[/recent]）"""
        path = f"{PREFIX}/recent" if recent else PREFIX
        get(ctx, path, params={"limit": limit})

    @group.command("task")
    @click.argument("task_id")
    @click.option("--cancel", is_flag=True, help="取消任务")
    @click.option("--retry", is_flag=True, help="重试任务")
    @click.option("--logs", "logs", is_flag=True, help="查看任务日志")
    @click.pass_context
    def task(ctx, task_id, cancel, retry, logs):
        """查看 / 取消 / 重试 / 日志 某个任务"""
        action = exclusive(cancel=cancel, retry=retry, logs=logs)
        tid = seg(task_id)
        if action == "cancel":
            post(ctx, f"{PREFIX}/{tid}/cancel", "取消")
        elif action == "retry":
            post(ctx, f"{PREFIX}/{tid}/retry", "重试")
        elif action == "logs":
            get(ctx, f"{PREFIX}/{tid}/logs")
        else:
            get(ctx, f"{PREFIX}/{tid}")
