"""命令层共用助手。

存在原因：`agents_cmd` / `task_cmd` / `session_cmd` / `automation_cmd` / `plugin_cmd`
此前各自复制了一份几乎相同的 `_get()` / `_post()` / `_post_json()`（合计约 88 行），
且每个都自行 `except (AuthError, APIError)` + `raise SystemExit(1)`——这既是重复，
也让 `cli.main()` 的统一异常处理沦为死代码。

现在统一为：
  * helper 只负责发请求与渲染，**不捕获异常**；
  * 所有 CLI 异常统一收敛到 `cli.main()` 处理并决定退出码。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import click

from ..client import quote_segment
from ..output import emit_json, render


def client_of(ctx: click.Context) -> Any:
    return ctx.obj["client"]


def wants_json(ctx: click.Context) -> bool:
    return bool(ctx.obj.get("as_json"))


def show(ctx: click.Context, data: Any) -> None:
    if wants_json(ctx):
        emit_json(data)
    else:
        render(data)


def get(ctx: click.Context, path: str,
        params: Optional[Mapping[str, Any]] = None) -> Any:
    data = client_of(ctx).request_json("GET", path, params=clean_params(params))
    show(ctx, data)
    return data


def post(ctx: click.Context, path: str, action: Optional[str] = None,
         payload: Any = None) -> Any:
    data = client_of(ctx).request_json("POST", path, json=payload)
    if wants_json(ctx):
        emit_json(data)
    elif action:
        render(f"{action}成功：{data}")
    else:
        render(data)
    return data


def seg(value: Any) -> str:
    """把自由字符串 ID 转成安全的 URL 路径段。"""
    return quote_segment(value)


def clean_params(params: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """剔除值为 None 的查询参数，避免向服务端发送 `?page=None`。"""
    if not params:
        return None
    cleaned = {k: v for k, v in params.items() if v is not None}
    return cleaned or None


def exclusive(**flags: bool) -> Optional[str]:
    """互斥选项校验：多选时显式报错，而不是静默按 if/elif 优先级取第一个。"""
    chosen = [name for name, enabled in flags.items() if enabled]
    if len(chosen) > 1:
        opts = "、".join(f"--{name.replace('_', '-')}" for name in sorted(chosen))
        raise click.UsageError(f"以下选项互斥，一次只能使用一个：{opts}")
    return chosen[0] if chosen else None


def parse_json_option(raw: Optional[str], option_name: str) -> Any:
    """解析 JSON 字符串选项，非法时给出友好报错而不是裸 ValueError。"""
    if raw in (None, ""):
        return None
    import json as _json

    try:
        return _json.loads(raw)
    except ValueError as exc:
        raise click.BadParameter(f"{option_name} 需为合法 JSON：{exc}")
