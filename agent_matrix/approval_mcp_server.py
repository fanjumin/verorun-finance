#!/usr/bin/env python
# agent_matrix/approval_mcp_server.py — 系统级 Agent Tools MCP 服务器
#
# sys.path 与配置来源：
#   - 路径解析：基于本文件位置定位 REPO_ROOT
#   - 配置来源：环境变量（MCP 子进程无 DB 连接能力）
#
# 协议：行分隔 JSON-RPC 2.0 over stdio（平台 plugin_manager/mcp.py 逐行读取；
# 15s 请求超时；子进程 stderr=DEVNULL）。
# 铁律：stdout 只允许协议帧；一切日志走 stderr；工具异常一律以 isError 帧返回，
#       绝不抛出。
#
# 安全（原方案 §5.3，内核版不变）：
#   file_read / file_list     → auto 档（只读），workspace_roots 内，路径穿越防护
#   file_write / file_edit    → approve_session 档，workspace_roots 内，.bak 保留
#   http_request              → approve_session 档，SSRF 防护（私网默认拒绝），
#                                响应体 ≤512KB，timeout ≤30s
#   code_exec                 → always 档，exec_enabled=false 时拒绝（默认关闭）
#
# fail-closed：workspace_roots 为空时所有 file 工具拒绝；exec_enabled=false
#              时 code_exec 拒绝；ssrf_allowlist 为空时私网全拒。
import io
import json
import logging
import os
import re
import shutil
import socket
import stat
import sys
import ipaddress
import urllib.request
import urllib.error

# ── sys.path：基于本文件位置定位 REPO_ROOT ────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s approval.mcp %(message)s")
_log = logging.getLogger("approval.mcp")


def _env_json(key, default):
    """Parse a JSON array env var (e.g. '["/home/app/workspace"]')."""
    raw = os.environ.get(key, '')
    if not raw:
        return default
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else default
    except (json.JSONDecodeError, TypeError):
        _log.warning("env %s parse failed (not valid JSON array), using default", key)
        return default


def _env_bool(key, default=False):
    raw = os.environ.get(key, '').strip().lower()
    if raw in ('true', '1', 'yes', 'on'):
        return True
    if raw in ('false', '0', 'no', 'off'):
        return False
    return default


# ── 系统级配置：全部从环境变量读 ──────────────────────────────────────
# 由 agent_matrix 内核启动 MCP 子进程时通过 McpStdioClient(env=...) 注入
_WORKSPACE_ROOTS = [os.path.realpath(p) for p in (
    _env_json('AGENT_TOOLS_WORKSPACE_ROOTS', []) or []) if p]
_SSRF_ALLOWLIST = _env_json('AGENT_TOOLS_SSRF_ALLOWLIST', [])
_EXEC_ENABLED = _env_bool('AGENT_TOOLS_EXEC_ENABLED', False)
_MAX_FILE_READ = 2 * 1024 * 1024       # 2MB
_MAX_FILE_WRITE = 1 * 1024 * 1024      # 1MB
_MAX_HTTP_RESPONSE = 512 * 1024        # 512KB
_HTTP_TIMEOUT_DEFAULT = 30
_HTTP_TIMEOUT_MAX = 60
_TEXT_EXTENSIONS = {
    '.txt', '.md', '.py', '.js', '.ts', '.json', '.yaml', '.yml', '.toml',
    '.html', '.css', '.xml', '.csv', '.sql', '.sh', '.bat', '.cfg', '.ini',
    '.env', '.log', '.rst', '.go', '.rs', '.java', '.c', '.h', '.cpp', '.rb',
    '.php', '.r', '.lua', '.pl', '.tex', '.srt', '.vtt',
}

# ── stdout 协议通道隔离 ───────────────────────────────────────────────
_PROTO_OUT = None


def _frame(payload):
    target = _PROTO_OUT if _PROTO_OUT is not None else sys.stdout
    target.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    target.flush()


PROTOCOL_VERSION = "2024-11-05"
# SERVER_INFO.name 保持 'agent_tools' — 工具命名空间 mcp__agent_tools__* 已对外
SERVER_INFO = {"name": "agent_tools", "version": "1.1.0"}


# ── 安全校验 ──────────────────────────────────────────────────────────
def _safe_path(file_path: str) -> str | None:
    """Resolve path and ensure it's within an allowed workspace root. Returns realpath or None."""
    if not _WORKSPACE_ROOTS:
        return None  # fail-closed: no roots configured
    if '\x00' in file_path:
        return None
    resolved = os.path.realpath(file_path)
    for root in _WORKSPACE_ROOTS:
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    return None


def _is_text_file(path: str) -> bool:
    _, ext = os.path.splitext(path)
    return ext.lower() in _TEXT_EXTENSIONS


def _check_ssrf(url: str) -> str | None:
    """Returns error message if blocked, None if allowed."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return "仅允许 http/https 协议"
        host = parsed.hostname
        if not host:
            return "无效主机名"
        try:
            addrs = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            ips = {a[4][0] for a in addrs}
        except socket.gaierror:
            return f"DNS 解析失败: {host}"
        for ip_str in ips:
            try:
                ip = ipaddress.ip_address(ip_str)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    if not any(host == a or host.endswith('.' + a.lstrip('.'))
                               for a in _SSRF_ALLOWLIST):
                        return f"目标 {host} ({ip_str}) 属于私有/回环地址，SSRF 防护拦截。如需放行请配置 AGENT_TOOLS_SSRF_ALLOWLIST"
            except ValueError:
                pass
        return None
    except Exception as e:
        return f"URL 校验异常: {e}"


# ── 工具定义 ──────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "file_read",
        "description": "Read a text file within allowed workspace roots. Returns file content as text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path to read"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "file_list",
        "description": "List files and directories at a path within allowed workspace roots.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute directory path"},
                "max_depth": {"type": "integer", "default": 1, "maximum": 5,
                              "description": "Recursion depth (1=flat listing)"},
                "max_entries": {"type": "integer", "default": 200, "maximum": 500,
                                "description": "Maximum entries to return"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "file_write",
        "description": "Write (create or overwrite) a text file within allowed workspace roots. Creates parent dirs. Backs up existing file as .bak.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path to write"},
                "content": {"type": "string", "description": "Text content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "file_edit",
        "description": "Replace an exact string occurrence in a file. The old_string must appear exactly once in the file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path to edit"},
                "old_string": {"type": "string", "description": "Exact text to find (must be unique in file)"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "http_request",
        "description": "Make an HTTP request (GET/POST/PUT/DELETE). Response body capped at 512KB. Private IPs blocked by default (SSRF guard).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Target URL (http/https)"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"], "default": "GET"},
                "headers": {"type": "object", "description": "Optional request headers"},
                "body": {"type": "string", "description": "Optional request body (for POST/PUT)"},
                "timeout": {"type": "integer", "default": 30, "maximum": 60,
                            "description": "Request timeout in seconds"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "code_exec",
        "description": "Execute a code snippet in a sandboxed subprocess (Python only). Requires AGENT_TOOLS_EXEC_ENABLED=true. Timeout ≤60s, output ≤64KB.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "integer", "default": 30, "maximum": 60,
                            "description": "Execution timeout in seconds"},
            },
            "required": ["code"],
        },
    },
]


# ── 工具实现 ──────────────────────────────────────────────────────────
def _tool_file_read(args):
    path = args.get("path", "")
    safe = _safe_path(path)
    if safe is None:
        return _text_result("路径不在 workspace_roots 允许范围内（或 roots 未配置）", error=True)
    if not os.path.isfile(safe):
        return _text_result(f"文件不存在: {safe}", error=True)
    if not _is_text_file(safe):
        return _text_result(f"不支持的二进制文件类型: {os.path.splitext(safe)[1]}", error=True)
    size = os.path.getsize(safe)
    if size > _MAX_FILE_READ:
        return _text_result(f"文件过大 ({size} bytes, 上限 {_MAX_FILE_READ})", error=True)
    with open(safe, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return _text_result(content)


def _tool_file_list(args):
    path = args.get("path", "")
    safe = _safe_path(path)
    if safe is None:
        return _text_result("路径不在 workspace_roots 允许范围内（或 roots 未配置）", error=True)
    if not os.path.isdir(safe):
        return _text_result(f"目录不存在: {safe}", error=True)
    max_depth = min(int(args.get("max_depth", 1)), 5)
    max_entries = min(int(args.get("max_entries", 200)), 500)
    entries = []
    root_len = len(safe)
    for dirpath, dirnames, filenames in os.walk(safe):
        depth = dirpath[root_len:].count(os.sep) + (1 if dirpath != safe else 0)
        if depth > max_depth:
            dirnames.clear()
            continue
        for d in sorted(dirnames):
            entries.append(os.path.join(dirpath[root_len:], d + "/").lstrip(os.sep))
            if len(entries) >= max_entries:
                break
        for f_name in sorted(filenames):
            full = os.path.join(dirpath, f_name)
            try:
                sz = os.path.getsize(full)
            except OSError:
                sz = 0
            entries.append(os.path.join(dirpath[root_len:], f_name).lstrip(os.sep) + f"  ({sz}B)")
            if len(entries) >= max_entries:
                break
        if len(entries) >= max_entries:
            entries.append(f"... (已达上限 {max_entries} 条)")
            break
    return _text_result("\n".join(entries) if entries else "(空目录)")


def _tool_file_write(args):
    path = args.get("path", "")
    content = args.get("content", "")
    if len(content.encode("utf-8")) > _MAX_FILE_WRITE:
        return _text_result(f"写入内容过大 ({len(content)} chars, 上限 {_MAX_FILE_WRITE} bytes)", error=True)
    safe = _safe_path(path)
    if safe is None:
        return _text_result("路径不在 workspace_roots 允许范围内（或 roots 未配置）", error=True)
    if not _is_text_file(safe):
        return _text_result(f"拒绝写入非文本文件: {os.path.splitext(safe)[1]}", error=True)
    if os.path.isfile(safe):
        bak = safe + ".bak"
        try:
            shutil.copy2(safe, bak)
        except OSError as e:
            return _text_result(f"备份失败: {e}", error=True)
    os.makedirs(os.path.dirname(safe), exist_ok=True)
    with open(safe, "w", encoding="utf-8") as f:
        f.write(content)
    return _text_result(f"已写入 {len(content)} chars -> {safe}")


def _tool_file_edit(args):
    path = args.get("path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    safe = _safe_path(path)
    if safe is None:
        return _text_result("路径不在 workspace_roots 允许范围内（或 roots 未配置）", error=True)
    if not os.path.isfile(safe):
        return _text_result(f"文件不存在: {safe}", error=True)
    if not _is_text_file(safe):
        return _text_result("不支持编辑二进制文件", error=True)
    with open(safe, "r", encoding="utf-8") as f:
        content = f.read()
    count = content.count(old_string)
    if count == 0:
        return _text_result("old_string 在文件中未找到", error=True)
    if count > 1:
        return _text_result(f"old_string 在文件中出现 {count} 次（要求唯一匹配）", error=True)
    new_content = content.replace(old_string, new_string, 1)
    bak = safe + ".bak"
    try:
        shutil.copy2(safe, bak)
    except OSError:
        pass
    with open(safe, "w", encoding="utf-8") as f:
        f.write(new_content)
    return _text_result(f"已编辑 {safe}（备份: {bak}）")


def _tool_http_request(args):
    url = args.get("url", "")
    method = (args.get("method") or "GET").upper()
    headers = args.get("headers") or {}
    body = args.get("body")
    timeout = min(int(args.get("timeout", _HTTP_TIMEOUT_DEFAULT)), _HTTP_TIMEOUT_MAX)
    ssrf_err = _check_ssrf(url)
    if ssrf_err:
        return _text_result(ssrf_err, error=True)
    try:
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in headers.items():
            req.add_header(str(k), str(v))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(_MAX_HTTP_RESPONSE + 1)
            truncated = len(raw) > _MAX_HTTP_RESPONSE
            if truncated:
                raw = raw[:_MAX_HTTP_RESPONSE]
            resp_headers = dict(resp.getheaders())
            status = resp.getcode()
        output = json.dumps({
            "status": status,
            "headers": resp_headers,
            "body": raw.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }, ensure_ascii=False)
        return _text_result(output)
    except urllib.error.HTTPError as e:
        return _text_result(json.dumps({"status": e.code, "error": str(e.reason),
                                        "body": e.read(2048).decode("utf-8", errors="replace")}))
    except Exception as e:
        return _text_result(f"请求失败: {e}", error=True)


def _tool_code_exec(args):
    if not _EXEC_ENABLED:
        return _text_result("code_exec 未启用（需管理员设置 AGENT_TOOLS_EXEC_ENABLED=true）", error=True)
    code = args.get("code", "")
    timeout = min(int(args.get("timeout", 30)), 60)
    import subprocess
    import tempfile
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False,
                                         encoding="utf-8") as tf:
            tf.write(code)
            tf_path = tf.name
        try:
            result = subprocess.run(
                [sys.executable, tf_path],
                capture_output=True, text=True, timeout=timeout, env=env,
                cwd=tempfile.gettempdir(),
            )
            stdout = result.stdout[:65536] if result.stdout else ""
            stderr = result.stderr[:4096] if result.stderr else ""
            return _text_result(json.dumps({
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }, ensure_ascii=False))
        finally:
            try:
                os.unlink(tf_path)
            except OSError:
                pass
    except subprocess.TimeoutExpired:
        return _text_result(f"执行超时 ({timeout}s)", error=True)
    except Exception as e:
        return _text_result(f"执行异常: {e}", error=True)


# ── 辅助 ─────────────────────────────────────────────────────────────
def _text_result(message, error=False):
    return {"content": [{"type": "text", "text": message}], "isError": error}


TOOL_HANDLERS = {
    "file_read": _tool_file_read,
    "file_list": _tool_file_list,
    "file_write": _tool_file_write,
    "file_edit": _tool_file_edit,
    "http_request": _tool_http_request,
    "code_exec": _tool_code_exec,
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

    _log.info("approval mcp server started (pid=%d, roots=%d, exec=%s)",
              os.getpid(), len(_WORKSPACE_ROOTS), _EXEC_ENABLED)
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
