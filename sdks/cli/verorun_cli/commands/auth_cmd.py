"""认证相关命令：login / logout / whoami。

安全改动：新增 `--password-stdin`，并在使用 `--password` 时明确告警——
命令行参数会出现在进程列表（ps / 任务管理器）与 shell 历史中。
"""

from __future__ import annotations

import sys
from typing import Any

import click

from ..output import warn
from ._base import wants_json, show


def register(group: Any) -> None:
    @group.command("login")
    @click.option("--username", "-u", help="管理员账号")
    @click.option("--password", "-p",
                  help="密码（不安全：会暴露在进程列表与 shell 历史中，建议用 --password-stdin）")
    @click.option("--password-stdin", is_flag=True,
                  help="从标准输入读取密码（推荐用于 CI / 脚本）")
    @click.option("--phone", help="手机号（短信登录）")
    @click.option("--code", help="短信验证码")
    @click.option("--api-key", help="agent API Key（ek-...）")
    @click.pass_context
    def login(ctx, username, password, password_stdin, phone, code, api_key):
        """登录 VeroRun（管理员密码 / 短信 / API Key）

        密码来源优先级：--password-stdin > --password > 交互式隐藏输入。
        """
        auth = ctx.obj["auth"]

        if api_key:
            auth.login_apikey(api_key)
            click.echo("已保存 API Key。")
            return

        if phone or code:
            if not (phone and code):
                raise click.UsageError("短信登录需同时提供 --phone 与 --code")
            auth.login_sms(phone, code)
            click.echo("短信登录成功。")
            return

        if password and password_stdin:
            raise click.UsageError("--password 与 --password-stdin 互斥，请只用其中一个")

        if not username:
            username = click.prompt("用户名")

        if password_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
            if not password:
                raise click.UsageError("--password-stdin 未能从标准输入读到密码")
        elif password:
            warn(
                "--password 会把明文密码暴露在进程列表与 shell 历史中，"
                "建议改用 `--password-stdin`（例如 `echo $PWD_VAR | verorun login -u admin --password-stdin`）"
                "或省略该参数以交互方式输入。"
            )
        else:
            password = click.prompt("密码", hide_input=True)

        auth.login_password(username, password)
        state = auth.token_state()
        if state.get("expires_at_text"):
            click.echo(f"登录成功，token 有效期至 {state['expires_at_text']}。")
        else:
            click.echo("登录成功。")

    @group.command("logout")
    @click.pass_context
    def logout(ctx):
        """登出并清除本地凭证"""
        ctx.obj["auth"].logout()
        click.echo("已登出。")

    @group.command("whoami")
    @click.pass_context
    def whoami(ctx):
        """显示当前登录用户信息（GET /user/profile）"""
        client = ctx.obj["client"]
        auth = ctx.obj["auth"]
        data = client.request_json("GET", "/user/profile")
        if wants_json(ctx):
            show(ctx, {"profile": data, "token": auth.token_state()})
            return
        click.echo(f"已登录：{_extract_user(data)}")
        state = auth.token_state()
        if state.get("expires_at_text"):
            suffix = "（已过期）" if state.get("expired") else ""
            click.echo(f"token 有效期至：{state['expires_at_text']}{suffix}")


def _extract_user(payload: Any) -> Any:
    """从 /user/profile 响应中稳健地取出用户信息。

    原实现 `(data.get("data") or data).get("user", data)` 在 `data["data"]`
    为字符串（服务端返回消息文本）时会抛 AttributeError。
    """
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if isinstance(data, dict):
        user = data.get("user")
        return user if user is not None else data
    if data is not None:
        return data
    user = payload.get("user")
    return user if user is not None else payload
