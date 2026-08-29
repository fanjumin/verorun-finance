"""自动化 / 工作流命令（GET/POST /admin/automation/*，路径已核对 orchestrator/routes.py）。

分页说明：jobs / workflows / instances 端点均支持 page + limit 查询参数
（见 orchestrator/routes.py:150 / 302 / 458），因此这里提供真实可用的
--page / --limit；其余子命令为动作型，不接收分页。
"""

from __future__ import annotations

import click

from ._base import exclusive, get, post, seg

PREFIX = "/admin/automation"


def register(group):
    @group.group("automation", help="自动化作业 / 工作流 / 实例")
    def automation():
        pass

    @automation.command("jobs")
    @click.option("--page", type=int, default=None, help="页码（从 1 开始）")
    @click.option("--limit", type=int, default=None, help="每页条数")
    @click.pass_context
    def jobs(ctx, page, limit):
        """列出自动化作业（GET /admin/automation/jobs）"""
        get(ctx, f"{PREFIX}/jobs", {"page": page, "limit": limit})

    @automation.command("job-run")
    @click.argument("job_id", type=int)
    @click.pass_context
    def job_run(ctx, job_id):
        """立即运行作业（POST /admin/automation/jobs/<id>/run）"""
        post(ctx, f"{PREFIX}/jobs/{job_id}/run", "运行")

    @automation.command("job-toggle")
    @click.argument("job_id", type=int)
    @click.pass_context
    def job_toggle(ctx, job_id):
        """启停作业（POST /admin/automation/jobs/<id>/toggle）"""
        post(ctx, f"{PREFIX}/jobs/{job_id}/toggle", "切换")

    @automation.command("workflows")
    @click.option("--page", type=int, default=None, help="页码（从 1 开始）")
    @click.option("--limit", type=int, default=None, help="每页条数")
    @click.pass_context
    def workflows(ctx, page, limit):
        """列出工作流（GET /admin/automation/workflows）"""
        get(ctx, f"{PREFIX}/workflows", {"page": page, "limit": limit})

    @automation.command("workflow-run")
    @click.argument("workflow_id", type=int)
    @click.pass_context
    def workflow_run(ctx, workflow_id):
        """运行工作流（POST /admin/automation/workflows/<id>/run）"""
        post(ctx, f"{PREFIX}/workflows/{workflow_id}/run", "运行")

    @automation.command("instances")
    @click.option("--page", type=int, default=None, help="页码（从 1 开始）")
    @click.option("--limit", type=int, default=None, help="每页条数")
    @click.pass_context
    def instances(ctx, page, limit):
        """列出运行实例（GET /admin/automation/instances）"""
        get(ctx, f"{PREFIX}/instances", {"page": page, "limit": limit})

    @automation.command("instance")
    @click.argument("instance_id", type=int)
    @click.option("--pause", is_flag=True, help="暂停")
    @click.option("--resume", is_flag=True, help="恢复")
    @click.option("--cancel", is_flag=True, help="取消")
    @click.pass_context
    def instance(ctx, instance_id, pause, resume, cancel):
        """控制实例：暂停 / 恢复 / 取消"""
        exclusive(pause=pause, resume=resume, cancel=cancel)
        iid = seg(instance_id)
        if pause:
            post(ctx, f"{PREFIX}/instances/{iid}/pause", "暂停")
        elif resume:
            post(ctx, f"{PREFIX}/instances/{iid}/resume", "恢复")
        elif cancel:
            post(ctx, f"{PREFIX}/instances/{iid}/cancel", "取消")
        else:
            get(ctx, f"{PREFIX}/instances/{iid}")
