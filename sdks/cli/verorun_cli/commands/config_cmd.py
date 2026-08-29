"""本地配置命令：config get / set / list。

仅读写本机 ~/.verorun/config.yaml，不触及任何服务端。
"""

from __future__ import annotations

import json

import click

from ..output import render


def register(group):
    @group.group("config", help="读写本机配置 ~/.verorun/config.yaml")
    def config():
        pass

    @config.command("get")
    @click.argument("key")
    @click.pass_context
    def get(ctx, key):
        """读取配置项（如 server.base_url）"""
        val = ctx.obj["config"].get(key)
        render(val if val is not None else "")

    @config.command("set")
    @click.argument("key")
    @click.argument("value")
    @click.pass_context
    def set_(ctx, key, value):
        """设置配置项（自动识别 bool/int/float/str）"""
        parsed = _coerce(value)
        ctx.obj["config"].set(key, parsed)
        render(f"已设置 {key} = {parsed}")

    @config.command("list")
    @click.pass_context
    def list_(ctx):
        """列出全部配置"""
        render(json.dumps(ctx.obj["config"].as_dict(), ensure_ascii=False, indent=2))


def _coerce(value):
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
