#!/usr/bin/env python3
"""
Plugin Manager — MCP 集成（stdio + Streamable HTTP 客户端）
=========================================================
插件可在 plugin.json 声明 `mcp_servers`（MCP 标准协议 server），
Agent 会话将其工具并入可用工具集（OpenAI function calling 格式）。

    声明格式（plugin.json）：
    "mcp_servers": [
        {
            "name": "filesystem",
            "transport": "stdio",                  // stdio | http
            "command": "python",                   // stdio 必填
            "args": ["mcp_server.py"],
            "env": {"KEY": "VAL"},                 // 可选，合并进子进程环境
            "url": "http://localhost:8080/mcp",    // http 必填
            "headers": {"Authorization": "Bearer …"}  // http 可选
        }
    ]

工具命名：mcp__<plugin_id>__<server_name>__<tool_name>
  - Agent 工具 schema 通过 get_enabled_mcp_tool_schemas() 合并
  - 调用经 execute_tool → call_mcp_tool() 路由

实现要点：
  - 零新依赖（stdlib：subprocess / json / threading / queue / urllib）
  - MCP stdio transport：JSON-RPC 2.0，每行一个 JSON 消息
  - MCP Streamable HTTP transport：JSON-RPC 2.0 over HTTP POST + SSE
  - reader 线程 + queue：避免 Windows 上 pipe 无法 select 的坑
  - 请求带超时（默认 15s），失败抛异常由调用方兜底
  - 运行时连接缓存：{server_key: (client, last_used_ts)} + 60s 空闲回收
"""

import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .models_store import get_registry_db

# MCP 协议版本（2024-11-05 为广泛兼容的稳定版）
_PROTOCOL_VERSION = '2024-11-05'
_DEFAULT_TIMEOUT = 15
# 空闲连接回收 TTL（秒）：超过该时长未使用的连接关闭
_IDLE_TTL = 60

# 运行中连接缓存：server_key -> (McpStdioClient | McpHttpClient, last_used_ts)
_CLIENTS: Dict[str, Tuple[Any, float]] = {}
_CLIENTS_LOCK = threading.Lock()


class McpError(RuntimeError):
    """MCP 调用异常基类。"""


class McpStdioClient:
    """轻量 MCP stdio client（JSON-RPC 2.0 over stdio）。

    通过子进程 stdin/stdout 通信，每行一个 JSON 消息；
    reader 线程将响应推入队列，_request 按 id 匹配并带超时。
    """

    def __init__(self, server_key: str, command: str, args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None, timeout: int = _DEFAULT_TIMEOUT):
        self._server_key = server_key
        self._timeout = timeout
        self._next_id = 0
        self._queue: 'queue.Queue[Dict[str, Any]]' = queue.Queue()
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        try:
            self._proc = subprocess.Popen(
                [command] + (args or []),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=full_env,
                bufsize=1,  # 行缓冲
            )
        except Exception as e:
            raise McpError(f'failed to start mcp server: {e}')
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        """持续读 stdout，把可解析的 JSON 消息推入队列。"""
        try:
            for line in self._proc.stdout:
                line = line.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(msg, dict):
                    self._queue.put(msg)
        except Exception:
            pass  # 进程退出/管道关闭即结束

    def _read_response(self, req_id: int, method: str) -> Dict[str, Any]:
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            try:
                msg = self._queue.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                continue
            if msg.get('id') == req_id:
                return msg
            # 非本请求响应（如 server 推送）忽略
        raise McpError(f'mcp request timeout ({method})')

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        payload = {'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': params or {}}
        self._proc.stdin.write((json.dumps(payload) + '\n').encode('utf-8'))
        self._proc.stdin.flush()
        resp = self._read_response(req_id, method)
        if 'error' in resp and resp['error']:
            err = resp['error']
            raise McpError(f'mcp {method} error: {err.get("message", err)}')
        return resp.get('result') or {}

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None):
        """发送通知（无 id，不等待响应）。"""
        payload = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        self._proc.stdin.write((json.dumps(payload) + '\n').encode('utf-8'))
        self._proc.stdin.flush()

    # ── MCP 标准方法 ──────────────────────────────────────────

    def initialize(self) -> Dict[str, Any]:
        info = self._request('initialize', {
            'protocolVersion': _PROTOCOL_VERSION,
            'capabilities': {},
            'clientInfo': {'name': 'verorun-plugin-manager', 'version': '1.0'},
        })
        self._notify('notifications/initialized')
        return info

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._request('tools/list')
        return result.get('tools') or []

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request('tools/call', {'name': name, 'arguments': arguments or {}})

    def close(self):
        try:
            self._proc.terminate()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=3)
        except Exception:
            pass
        # 关闭管道，释放 reader 线程持有的文件句柄（消除 ResourceWarning）
        for _pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            try:
                if _pipe is not None:
                    _pipe.close()
            except Exception:
                pass


class McpHttpClient:
    """MCP Streamable HTTP client（JSON-RPC 2.0 over HTTP POST + SSE）。

    协议要点（MCP 2025-03-26 Streamable HTTP）：
      - 客户端 POST JSON-RPC 到单一端点
      - 服务端返回 application/json 或 text/event-stream
      - 会话通过 Mcp-Session-Id 头管理
      - SSE 流内 data: 行携带 JSON-RPC 消息
    """

    def __init__(self, server_key: str, url: str,
                 headers: Optional[Dict[str, str]] = None,
                 timeout: int = _DEFAULT_TIMEOUT):
        self._server_key = server_key
        self._url = url.strip()
        if not self._url:
            raise McpError('mcp http transport requires "url"')
        self._timeout = timeout
        self._next_id = 0
        self._session_id: Optional[str] = None
        self._base_headers = dict(headers or {})

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(self._url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Accept', 'application/json, text/event-stream')
        if self._session_id:
            req.add_header('Mcp-Session-Id', self._session_id)
        for k, v in self._base_headers.items():
            req.add_header(k, v)
        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout)
        except urllib.error.HTTPError as e:
            raise McpError(f'mcp http {e.code}: {e.reason}')
        except urllib.error.URLError as e:
            raise McpError(f'mcp http connection error: {e.reason}')
        except Exception as e:
            raise McpError(f'mcp http request failed: {e}')
        sid = resp.headers.get('Mcp-Session-Id')
        if sid:
            self._session_id = sid
        ct = resp.headers.get('Content-Type', '')
        raw = resp.read().decode('utf-8', errors='replace')
        if 'text/event-stream' in ct:
            return self._parse_sse(raw)
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as e:
            raise McpError(f'mcp http invalid json response: {e}')

    @staticmethod
    def _parse_sse(raw: str) -> Dict[str, Any]:
        """从 SSE 流提取最后一个 data: 行的 JSON-RPC 消息。"""
        last: Optional[Dict[str, Any]] = None
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith('data:'):
                data = line[5:].strip()
                if data:
                    try:
                        last = json.loads(data)
                    except (ValueError, TypeError):
                        pass
        if last is None:
            raise McpError('mcp http sse stream contained no data')
        return last

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        payload = {'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': params or {}}
        resp = self._post(payload)
        if resp.get('id') != req_id:
            raise McpError(f'mcp http response id mismatch: expected {req_id}, got {resp.get("id")}')
        if 'error' in resp and resp['error']:
            err = resp['error']
            raise McpError(f'mcp {method} error: {err.get("message", err)}')
        return resp.get('result') or {}

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None):
        payload = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        try:
            self._post(payload)
        except McpError:
            pass  # 通知失败静默

    def initialize(self) -> Dict[str, Any]:
        info = self._request('initialize', {
            'protocolVersion': _PROTOCOL_VERSION,
            'capabilities': {},
            'clientInfo': {'name': 'verorun-plugin-manager', 'version': '1.0'},
        })
        self._notify('notifications/initialized')
        return info

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._request('tools/list')
        return result.get('tools') or []

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request('tools/call', {'name': name, 'arguments': arguments or {}})

    def close(self):
        self._session_id = None


# ── 工具 schema 转换 ────────────────────────────────────────────────

def _to_openai_schema(server_key: str, tool: Dict[str, Any]) -> Dict[str, Any]:
    """MCP 工具 → OpenAI function calling schema。

    工具名带 mcp__<plugin>__<server>__<tool> 前缀，供 execute_tool 路由。
    """
    plugin_id, _, server_name = server_key.partition(':')
    tool_name = tool.get('name', 'tool')
    return {
        'type': 'function',
        'function': {
            'name': f'mcp__{plugin_id}__{server_name}__{tool_name}',
            'description': tool.get('description') or tool_name,
            'parameters': tool.get('inputSchema') or {
                'type': 'object', 'properties': {}, 'required': [],
            },
        },
    }


def _parse_tool_name(name: str) -> Optional[Tuple[str, str, str]]:
    """解析 mcp__<plugin>__<server>__<tool> → (plugin_id, server_name, tool_name)。"""
    parts = name.split('__', 3)
    if len(parts) != 4 or parts[0] != 'mcp':
        return None
    return parts[1], parts[2], parts[3]


# ── 注册 / 生命周期 ────────────────────────────────────────────────

def _config_to_record(plugin_id: str, server: Dict[str, Any]) -> Dict[str, Any]:
    name = (server.get('name') or 'default').strip()
    transport = (server.get('transport') or 'stdio').strip()
    config = {
        'command': (server.get('command') or '').strip(),
        'args': server.get('args') or [],
        'env': server.get('env') or {},
        'transport': transport,
        'url': (server.get('url') or '').strip(),
        'headers': server.get('headers') or {},
    }
    return {'plugin_id': plugin_id, 'server_name': name, 'config': config}


def sync_plugin_mcp(identifier: str, metadata: Optional[Dict[str, Any]] = None) -> int:
    """同步插件声明的 MCP server 到 plugin_mcp_servers（幂等 upsert）。

    从 plugin.json 的 mcp_servers 字段解析；无声明则清空该插件旧记录。
    返回处理条数。enable/install 时调用，将 enabled 置 1。
    """
    servers = (metadata or {}).get('mcp_servers') or []
    records = []
    for s in servers:
        if not isinstance(s, dict):
            continue
        transport = (s.get('transport') or 'stdio').strip()
        if transport == 'http' and (s.get('url') or '').strip():
            records.append(_config_to_record(identifier, s))
        elif (s.get('command') or '').strip():
            records.append(_config_to_record(identifier, s))
    with get_registry_db() as conn:
        conn.execute('DELETE FROM plugin_mcp_servers WHERE plugin_id=%s', (identifier,))
        for r in records:
            conn.execute(
                "INSERT INTO plugin_mcp_servers (plugin_id, server_name, config, enabled) "
                "VALUES (%s,%s,%s,1) "
                "ON CONFLICT (plugin_id, server_name) "
                "DO UPDATE SET config=excluded.config, enabled=1, updated_at=NOW()",
                (r['plugin_id'], r['server_name'], json.dumps(r['config'])))
        conn.commit()
    return len(records)


def stop_plugin_mcp(identifier: str) -> None:
    """停用插件 MCP：关闭运行中连接并置 enabled=0（disable 时调用）。"""
    with _CLIENTS_LOCK:
        dead = [k for k in _CLIENTS if k.startswith(identifier + ':')]
        for k in dead:
            try:
                _CLIENTS[k][0].close()
            except Exception:
                pass
            del _CLIENTS[k]
    try:
        with get_registry_db() as conn:
            conn.execute('UPDATE plugin_mcp_servers SET enabled=0, updated_at=NOW() '
                         'WHERE plugin_id=%s', (identifier,))
            conn.commit()
    except Exception:
        pass


# ── 运行时：连接管理与工具聚合 ─────────────────────────────────────

def _get_client(plugin_id: str, server_name: str, config: Dict[str, Any]):
    server_key = f'{plugin_id}:{server_name}'
    now = time.time()
    with _CLIENTS_LOCK:
        cached = _CLIENTS.get(server_key)
        if cached:
            _CLIENTS[server_key] = (cached[0], now)
            return cached[0]
        transport = (config.get('transport') or 'stdio').strip()
        if transport == 'http':
            client = McpHttpClient(
                server_key,
                config.get('url') or '',
                headers=config.get('headers') or {},
            )
        else:
            client = McpStdioClient(
                server_key,
                config.get('command') or '',
                args=config.get('args') or [],
                env=config.get('env') or {},
            )
        client.initialize()
        _CLIENTS[server_key] = (client, now)
        return client


def _reap_idle_clients() -> None:
    """回收空闲连接（超 _IDLE_TTL 未使用）。"""
    now = time.time()
    with _CLIENTS_LOCK:
        dead = [k for k, (c, ts) in _CLIENTS.items() if now - ts > _IDLE_TTL]
        for k in dead:
            try:
                _CLIENTS[k][0].close()
            except Exception:
                pass
            del _CLIENTS[k]


def _enabled_records() -> List[Dict[str, Any]]:
    with get_registry_db() as conn:
        rows = conn.execute(
            'SELECT plugin_id, server_name, config FROM plugin_mcp_servers '
            'WHERE enabled=1 ORDER BY plugin_id, server_name').fetchall()
    return [dict(r) for r in rows]


def get_enabled_mcp_tool_schemas() -> List[Dict[str, Any]]:
    """聚合所有已启用插件的 MCP 工具 schema（供 Agent 工具集合并）。"""
    _reap_idle_clients()
    schemas: List[Dict[str, Any]] = []
    try:
        records = _enabled_records()
    except Exception:
        return schemas  # registry DB 不可用时静默降级（不影响 Agent 基础工具）
    for r in records:
        try:
            config = json.loads(r.get('config') or '{}')
            client = _get_client(r['plugin_id'], r['server_name'], config)
            for tool in client.list_tools():
                schemas.append(_to_openai_schema(f"{r['plugin_id']}:{r['server_name']}", tool))
        except Exception:
            continue  # 单 server 失败不影响其他
    return schemas


def call_mcp_tool(name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
    """执行 MCP 工具调用（execute_tool 路由入口）。

    name 格式：mcp__<plugin_id>__<server_name>__<tool_name>
    返回 MCP 响应的文本内容拼接（content[].text）。
    """
    parsed = _parse_tool_name(name)
    if not parsed:
        return f'Unknown tool: {name}'
    plugin_id, server_name, tool_name = parsed
    try:
        records = _enabled_records()
        cfg = next((json.loads(r['config']) for r in records
                    if r['plugin_id'] == plugin_id and r['server_name'] == server_name), None)
        if not cfg:
            return f'MCP server not enabled: {plugin_id}/{server_name}'
        client = _get_client(plugin_id, server_name, cfg)
        result = client.call_tool(tool_name, arguments or {})
    except Exception as e:
        return f'Tool {name} execution error: {e}'
    # 组装文本输出
    parts = []
    for item in result.get('content') or []:
        if isinstance(item, dict) and item.get('type') == 'text':
            parts.append(item.get('text', ''))
    return '\n'.join(parts) if parts else json.dumps(result, ensure_ascii=False)


# ── 对外 manifest ──────────────────────────────────────────────────

def build_manifest(plugin_id: str) -> Optional[Dict[str, Any]]:
    """插件 MCP 能力清单（供外部 MCP client 发现）。"""
    try:
        records = _enabled_records()
    except Exception:
        return None
    mine = [r for r in records if r['plugin_id'] == plugin_id]
    if not mine:
        return None
    servers = []
    for r in mine:
        config = json.loads(r.get('config') or '{}')
        tools = []
        try:
            client = _get_client(r['plugin_id'], r['server_name'], config)
            for tool in client.list_tools():
                tools.append({
                    'name': tool.get('name'),
                    'description': tool.get('description'),
                    'inputSchema': tool.get('inputSchema'),
                })
        except Exception:
            pass
        servers.append({'server_name': r['server_name'], 'tools': tools})
    return {'plugin_id': plugin_id, 'servers': servers}
