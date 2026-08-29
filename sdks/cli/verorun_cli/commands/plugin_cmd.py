"""插件管理命令（GET/POST /admin/plugins/*，路径已核对 plugin_manager/routes.py）。

过滤说明：服务端 /admin/plugins 仅支持 status 过滤参数
（见 plugin_manager/routes.py:146），因此这里只暴露 --status，
不伪造其它未实现的过滤开关。
"""

from __future__ import annotations

import click

from ._base import get, parse_json_option, post, seg

PREFIX = "/admin/plugins"


def register(group):
    @group.group("plugins", help="插件管理")
    def plugins():
        pass

    @plugins.command("list")
    @click.option("--status", default=None,
                  help="按状态过滤（服务端仅支持 status 过滤）")
    @click.pass_context
    def list_plugins(ctx, status):
        """列出插件（GET /admin/plugins）"""
        get(ctx, PREFIX, {"status": status})

    @plugins.command("install")
    @click.argument("identifier")
    @click.pass_context
    def install(ctx, identifier):
        """安装插件（POST /admin/plugins/<id>/install）"""
        post(ctx, f"{PREFIX}/{seg(identifier)}/install", "安装")

    @plugins.command("enable")
    @click.argument("identifier")
    @click.pass_context
    def enable(ctx, identifier):
        """启用插件（POST /admin/plugins/<id>/enable）"""
        post(ctx, f"{PREFIX}/{seg(identifier)}/enable", "启用")

    @plugins.command("disable")
    @click.argument("identifier")
    @click.pass_context
    def disable(ctx, identifier):
        """停用插件（POST /admin/plugins/<id>/disable）"""
        post(ctx, f"{PREFIX}/{seg(identifier)}/disable", "停用")

    @plugins.command("config")
    @click.argument("identifier")
    @click.option("--set", "set_json", help="设置配置（JSON 字符串）")
    @click.pass_context
    def config(ctx, identifier, set_json):
        """查看 / 设置插件配置（GET/POST /admin/plugins/<id>/config）"""
        path = f"{PREFIX}/{seg(identifier)}/config"
        if set_json:
            payload = parse_json_option(set_json, "--set")
            post(ctx, path, "配置", payload)
        else:
            get(ctx, path)
