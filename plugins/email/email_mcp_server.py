#!/usr/bin/env python
# plugins/email/email_mcp_server.py — 系统级 Agent Tools MCP 服务器（Email 域）
#
# 暴露工具（与 plugin.json hooks.provides 对齐，v1.7.0）：
#   email_send            → services.send_email（支持 cc/bcc/attachments）
#   email_send_contact    → services.send_contact_email（联系表单邮件，发送给 CONTACT_TO）
#   email_get_config      → 脱敏后的当前邮件配置（不返回密码明文）
#
# 配置来源：主进程凭据桥（EM-1b，v1.8.0）。本子进程不持有 SMTP/IMAP 凭据；
#           主进程在注册时注入 EMAIL_MCP_BRIDGE_URL + EMAIL_MCP_BRIDGE_TOKEN，
#           工具调用统一由主进程执行（_get_mail_config() 在主进程内存中解密）。
#
# 协议：行分隔 JSON-RPC 2.0 over stdio（平台 plugin_manager/mcp.py 逐行读取；
# 15s 请求超时；子进程 stderr=DEVNULL）。
# 铁律：stdout 只允许协议帧；一切日志走 stderr；工具异常一律以 isError 帧返回，
#       绝不抛出。
import io
import json
import logging
import os
import sys

# ── sys.path：基于本文件位置定位 REPO_ROOT ────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s email.mcp %(message)s")
_log = logging.getLogger("email.mcp")

# ── 主进程凭据桥（EM-1b，v1.8.0）──────────────────────────────────────
# 本子进程**不持有任何邮件凭据**。主进程在注册系统 MCP 时启动一个仅监听
# 127.0.0.1 的凭据桥，并把「桥地址 + 一次性随机令牌」注入 env；每次工具
# 调用由本进程转发给主进程，由主进程解密配置并执行发送。
_BRIDGE_TIMEOUT = 60


def _bridge_call(tool, args):
    """提交一次桥调用，返回 result；桥不可用或鉴权失败时抛 RuntimeError。"""
    import urllib.request
    url = os.environ.get("EMAIL_MCP_BRIDGE_URL", "")
    token = os.environ.get("EMAIL_MCP_BRIDGE_TOKEN", "")
    if not url or not token:
        raise RuntimeError("email bridge not configured（主进程未启动凭据桥）")
    payload = json.dumps({"tool": tool, "args": args}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "X-Email-Bridge-Token": token})
    with urllib.request.urlopen(req, timeout=_BRIDGE_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(body.get("error") or "bridge error")
    return body.get("result")


PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "email", "version": "1.8.0"}

TOOLS = [
    {
        "name": "email_send",
        "description": "发送一封邮件（SMTP）。支持抄送/密送与附件。返回发送结果与服务器消息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人邮箱（多个用英文逗号分隔）"},
                "subject": {"type": "string", "description": "邮件主题"},
                "body": {"type": "string", "description": "纯文本正文"},
                "body_html": {"type": "string", "description": "HTML 正文（可选，与 body 至少一个）"},
                "cc": {"type": "string", "description": "抄送地址，英文逗号分隔（可选）"},
                "bcc": {"type": "string", "description": "密送地址，英文逗号分隔（可选）"},
                "attachments": {
                    "type": "array",
                    "description": "附件列表：[{'filename': 'a.pdf', 'data': '<base64>', 'content_type': 'application/pdf'}]（可选，单附件≤10MB）",
                    "items": {"type": "object"},
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "email_send_contact",
        "description": "以联系表单形式给站点管理员发送一封邮件（发送至系统配置的联系邮箱）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "访客姓名"},
                "email": {"type": "string", "description": "访客邮箱"},
                "subject": {"type": "string", "description": "联系主题"},
                "message": {"type": "string", "description": "留言内容"},
            },
            "required": ["name", "email", "subject", "message"],
        },
    },
    {
        "name": "email_get_config",
        "description": "获取当前邮件服务配置摘要（SMTP/IMAP 主机、端口、账号、发件人；密码脱敏为 ********）。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ── stdout 协议通道隔离 ───────────────────────────────────────────────
_PROTO_OUT = None


def _frame(payload):
    target = _PROTO_OUT if _PROTO_OUT is not None else sys.stdout
    target.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    target.flush()


def _text_result(message, error=False):
    return {"content": [{"type": "text", "text": message}], "isError": error}


# ── 工具实现 ──────────────────────────────────────────────────────────
def _tool_email_send(args):
    to = str(args.get("to", "") or "").strip()
    subject = str(args.get("subject", "") or "").strip()
    body = str(args.get("body", "") or "")
    body_html = args.get("body_html") or None
    cc = args.get("cc") or None
    bcc = args.get("bcc") or None
    attachments = args.get("attachments") or None
    if not to or not subject or (not body and not body_html):
        return _text_result("收件人、主题、内容均不能为空", error=True)
    try:
        ok, msg = _bridge_call("email_send", {
            "to": to, "subject": subject, "body": body, "body_html": body_html,
            "cc": cc, "bcc": bcc, "attachments": attachments})
        if not ok:
            return _text_result(f"发送失败: {msg}", error=True)
        return _text_result(f"发送成功: {msg}")
    except Exception as e:
        return _text_result(f"发送异常: {e}", error=True)


def _tool_email_send_contact(args):
    name = str(args.get("name", "") or "").strip()
    email_addr = str(args.get("email", "") or "").strip()
    subject = str(args.get("subject", "") or "").strip()
    message = str(args.get("message", "") or "")
    if not name or not email_addr or not subject or not message:
        return _text_result("姓名、邮箱、主题、留言均不能为空", error=True)
    try:
        ok, msg = _bridge_call("email_send_contact", {
            "name": name, "email": email_addr, "subject": subject, "message": message})
        if not ok:
            return _text_result(f"发送失败: {msg}", error=True)
        return _text_result(f"发送成功: {msg}")
    except Exception as e:
        return _text_result(f"发送异常: {e}", error=True)


def _tool_email_get_config(args):
    try:
        safe = _bridge_call("email_get_config", {})
        # 主进程侧已脱敏；此处再兜一层，确保密码永不返回明文
        if isinstance(safe, dict) and safe.get('smtp_pass'):
            safe['smtp_pass'] = '********'
        return _text_result(json.dumps(safe, ensure_ascii=False))
    except Exception as e:
        return _text_result(f"读取配置异常: {e}", error=True)


TOOL_HANDLERS = {
    "email_send": _tool_email_send,
    "email_send_contact": _tool_email_send_contact,
    "email_get_config": _tool_email_get_config,
}


# ── JSON-RPC 协议处理 ─────────────────────────────────────────────────
def _success(req_id, result):
    _frame({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message):
    _frame({"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}})


def handle_request(req):
    if not isinstance(req, dict):
        return True
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        _success(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })
    elif method in ("notifications/initialized", "notifications/cancelled"):
        pass
    elif method == "tools/list":
        _success(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            _error(req_id, -32601, f"unknown tool: {tool_name}")
            return True
        try:
            result = handler(tool_args)
        except Exception as e:
            _log.exception("tool %s failed", tool_name)
            result = _text_result(f"Tool {tool_name} execution error: {e}", error=True)
        _success(req_id, result)
    else:
        if req_id is not None:
            _error(req_id, -32601, f"method not found: {method}")
    return True


def main():
    global _PROTO_OUT
    try:
        _PROTO_OUT = os.fdopen(os.dup(sys.stdout.fileno()), "w",
                               encoding="utf-8", errors="replace", buffering=1)
    except (OSError, ValueError):
        _PROTO_OUT = None
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        sys.stdout = io.StringIO()

    _log.info("email mcp server started (pid=%d)", os.getpid())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _error(None, -32700, "parse error")
            continue
        try:
            if not handle_request(req):
                break
        except Exception as err:
            _log.exception("protocol loop error")
            if req.get("id") is not None:
                _error(req.get("id"), -32603, f"internal error: {err}")


if __name__ == "__main__":
    main()