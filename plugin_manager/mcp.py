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
            "headers": {"Authorization": "Bearer …"}, // http 可选
            "timeout": 60                          // 可选，单请求超时秒数（5-300，默认 15）
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
  - 请求带超时（默认 15s，plugin.json 可声明 "timeout"：5-300），失败抛异常由调用方兜底
  - 调用审计：call_mcp_tool 每次调用写结构化记录到 data/logs/mcp_audit.log（参数脱敏）
  - 运行时连接缓存：{server_key: (client, last_used_ts)} + 60s 空闲回收
"""

import json
import logging
import logging.handlers
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .models_store import get_registry_db

logger = logging.getLogger(__name__)

# MCP 协议版本（2024-11-05 为广泛兼容的稳定版）
_PROTOCOL_VERSION = '2024-11-05'
_DEFAULT_TIMEOUT = 15
# per-server 超时允许范围（秒）：plugin.json "timeout" 超出即 clamp
_TIMEOUT_MIN = 5
_TIMEOUT_MAX = 300
# 调用审计：命中即脱敏的参数键（键名归一化为小写下划线形式后比对）
_SENSITIVE_ARG_KEYS = frozenset({
    'password', 'passwd', 'secret', 'token', 'api_key', 'apikey',
    'authorization', 'auth', 'credential', 'credentials',
    'data', 'attachments', 'body', 'body_html',
})
_ARG_VALUE_MAXLEN = 80
# 空闲连接回收 TTL（秒）：超过该时长未使用的连接关闭
_IDLE_TTL = 60
# PF-03：子进程 stdout 非 JSON 噪声帧（插件 print/迁移日志泄漏）每累计多少条
# 限频告警一次——只丢弃不告警会让"插件把日志写进 stdout"长期无感知。
_NOISE_WARN_EVERY = 20
_NOISE_SAMPLE_MAXLEN = 200

# 运行中连接缓存：server_key -> (McpStdioClient | McpHttpClient, last_used_ts)
_CLIENTS: Dict[str, Tuple[Any, float]] = {}
_CLIENTS_LOCK = threading.Lock()

# ── Phase B: 系统内置 MCP（非插件声明，内核算子注册）─────────────
#   与插件 MCP（存 DB plugin_mcp_servers）不同，系统 MCP 存内存，
#   由 agent_matrix 等内核模块 init 时注册，不受插件 enable/disable 控制。
#   _enabled_records() 合并双来源。
_SYSTEM_MCP_SERVERS: List[Dict[str, Any]] = []
_SYSTEM_MCP_LOCK = threading.Lock()


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
        # PF-03：累计被丢弃的 stdout 非 JSON 噪声帧数（reader 线程内自增，
        # 无其他线程写，无需加锁）
        self._noise_dropped = 0
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
                    # PF-03：非协议帧（插件 print/迁移日志泄漏到 stdout）丢弃，
                    # 但累计计数并限频告警，避免插件把日志写进 stdout 长期无感知。
                    # 第 1 条即告警，之后每 _NOISE_WARN_EVERY 条再告警一次。
                    self._noise_dropped += 1
                    if self._noise_dropped == 1 or self._noise_dropped % _NOISE_WARN_EVERY == 0:
                        logger.warning(
                            '[MCP] server=%s dropped non-JSON stdout line '
                            '(total=%s); latest sample: %s',
                            self._server_key, self._noise_dropped,
                            line[:_NOISE_SAMPLE_MAXLEN],
                        )
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


# ── 超时归一化 / 参数脱敏 / 调用审计 ────────────────────────────────

def _normalize_timeout(value: Any) -> int:
    """将 timeout 配置归一化到 [_TIMEOUT_MIN, _TIMEOUT_MAX]；None/非法回退默认值。"""
    if value is None:
        return _DEFAULT_TIMEOUT
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    return max(_TIMEOUT_MIN, min(timeout, _TIMEOUT_MAX))


def _summarize_arguments(arguments: Any) -> Dict[str, Any]:
    """生成参数摘要：敏感键仅记 <redacted:类型>，字符串截断，集合只记元素数。"""
    if not isinstance(arguments, dict):
        return {'_arg_type': type(arguments).__name__}
    summary: Dict[str, Any] = {}
    for key, value in arguments.items():
        normalized_key = str(key).lower().replace('-', '_')
        if normalized_key in _SENSITIVE_ARG_KEYS:
            summary[key] = f'<redacted:{type(value).__name__}>'
        elif isinstance(value, str):
            summary[key] = (value if len(value) <= _ARG_VALUE_MAXLEN
                            else value[:_ARG_VALUE_MAXLEN] + '…')
        elif isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            summary[key] = value
        elif isinstance(value, list):
            summary[key] = f'<list:{len(value)}>'
        elif isinstance(value, dict):
            summary[key] = f'<dict:{len(value)}>'
        else:
            summary[key] = f'<{type(value).__name__}>'
    return summary


_AUDIT_LOGGER_NAME = 'mcp.audit'
_audit_handler_ready = False
_audit_lock = threading.Lock()


def _get_audit_logger() -> logging.Logger:
    """返回 MCP 调用审计 logger（data/logs/mcp_audit.log，5MB×3 轮转）。

    落盘初始化失败时退化为 NullHandler —— 审计链路永不影响工具调用。
    """
    global _audit_handler_ready
    audit_logger = logging.getLogger(_AUDIT_LOGGER_NAME)
    if _audit_handler_ready:
        return audit_logger
    with _audit_lock:
        if _audit_handler_ready:
            return audit_logger
        audit_logger.setLevel(logging.INFO)
        audit_logger.propagate = False
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_dir = os.path.join(base_dir, 'data', 'logs')
            os.makedirs(log_dir, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, 'mcp_audit.log'),
                maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
            handler.setFormatter(logging.Formatter(
                '%(asctime)s [%(levelname)s] %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'))
            audit_logger.addHandler(handler)
        except Exception:
            audit_logger.addHandler(logging.NullHandler())
        _audit_handler_ready = True
    return audit_logger


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
        'timeout': _normalize_timeout(server.get('timeout')),
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


# ── Phase B: 系统内置 MCP 注册 API ──────────────────────────────

def register_system_mcp_server(plugin_id: str, server_name: str,
                               config: Optional[Dict[str, Any]] = None) -> None:
    """注册一个系统内置 MCP server（内核算子调用，不走 plugin.json 流程）。

    Args:
        plugin_id:   工具前缀用的 plugin_id（如 'agent_tools'），决定 mcp__ 命名空间。
                     注意：此处用 'agent_tools' 保持向后兼容的工具名。
        server_name: MCP server 内部名（如 'agent_tools'）。
        config:      同 plugin.json mcp_servers 结构（command/args/env/transport/url/headers）。
                     缺省：transport='stdio', command='python'。

    幂等：同一 (plugin_id, server_name) 重复注册 → 覆盖 config。
    """
    if not plugin_id or not server_name:
        raise ValueError('plugin_id and server_name are required')
    cfg = dict(config or {})
    cfg.setdefault('transport', 'stdio')
    cfg.setdefault('command', '')
    cfg.setdefault('args', [])
    cfg.setdefault('env', {})
    cfg.setdefault('url', '')
    cfg.setdefault('headers', {})
    cfg.setdefault('timeout', _DEFAULT_TIMEOUT)
    record = {
        'plugin_id': plugin_id,
        'server_name': server_name,
        'config': cfg,
        '_system': True,
    }
    with _SYSTEM_MCP_LOCK:
        for i, existing in enumerate(_SYSTEM_MCP_SERVERS):
            if existing['plugin_id'] == plugin_id and existing['server_name'] == server_name:
                _SYSTEM_MCP_SERVERS[i] = record
                return
        _SYSTEM_MCP_SERVERS.append(record)
    # 关闭旧连接（若 config 变了，下一次调用会用新 config 重建）
    server_key = f'{plugin_id}:{server_name}'
    with _CLIENTS_LOCK:
        if server_key in _CLIENTS:
            try:
                _CLIENTS[server_key][0].close()
            except Exception:
                pass
            del _CLIENTS[server_key]


def unregister_system_mcp_server(plugin_id: str, server_name: str) -> None:
    """移除系统内置 MCP server，关闭其运行时连接。"""
    with _SYSTEM_MCP_LOCK:
        _SYSTEM_MCP_SERVERS[:] = [
            s for s in _SYSTEM_MCP_SERVERS
            if not (s['plugin_id'] == plugin_id and s['server_name'] == server_name)]
    server_key = f'{plugin_id}:{server_name}'
    with _CLIENTS_LOCK:
        if server_key in _CLIENTS:
            try:
                _CLIENTS[server_key][0].close()
            except Exception:
                pass
            del _CLIENTS[server_key]


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
        timeout = _normalize_timeout(config.get('timeout'))
        if transport == 'http':
            client = McpHttpClient(
                server_key,
                config.get('url') or '',
                headers=config.get('headers') or {},
                timeout=timeout,
            )
        else:
            client = McpStdioClient(
                server_key,
                config.get('command') or '',
                args=config.get('args') or [],
                env=config.get('env') or {},
                timeout=timeout,
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
    """返回当前活跃的 MCP server 记录（合并插件 MCP + 系统内置 MCP）。

    返回结构统一为 {'plugin_id', 'server_name', 'config'(JSON string), '_system'(bool)}。
    调用方（get_enabled_mcp_tool_schemas / call_mcp_tool）对 config 执行 json.loads()，
    故此处统一序列化为 JSON 字符串以匹配 DB 记录形态。

    去重：同一 (plugin_id, server_name) 若同时存在于 DB 和系统 MCP，
    系统 MCP 优先（内核已接管的 server 覆盖插件 DB 残留记录）。
    """
    # 按 (plugin_id, server_name) 建 dict 实现覆盖式去重
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # 1. 插件 MCP（从 DB，先入表）
    try:
        with get_registry_db() as conn:
            rows = conn.execute(
                'SELECT plugin_id, server_name, config FROM plugin_mcp_servers '
                'WHERE enabled=1 ORDER BY plugin_id, server_name').fetchall()
        for r in rows:
            rec = dict(r)
            # psycopg2 可能自动把 JSON 列反序列化为 dict —— 统一序列化回字符串
            cfg = rec.get('config')
            if isinstance(cfg, dict):
                rec['config'] = json.dumps(cfg)
            elif cfg is None:
                rec['config'] = '{}'
            rec['_system'] = False
            merged[(rec['plugin_id'], rec['server_name'])] = rec
    except Exception:
        pass  # registry DB 不可用时跳过插件侧

    # 2. 系统内置 MCP（内存注册，后入表 → 覆盖同键 DB 记录）
    with _SYSTEM_MCP_LOCK:
        for s in list(_SYSTEM_MCP_SERVERS):
            rec = {
                'plugin_id': s['plugin_id'],
                'server_name': s['server_name'],
                'config': json.dumps(s['config'] or {}),
                '_system': True,
            }
            merged[(rec['plugin_id'], rec['server_name'])] = rec

    # 按 (system 优先, plugin_id, server_name) 排序返回
    records = list(merged.values())
    records.sort(key=lambda r: (not r.get('_system', False), r['plugin_id'], r['server_name']))
    return records


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


def call_mcp_tool(name: str, arguments: Optional[Dict[str, Any]] = None,
                  context: Optional[Dict[str, Any]] = None) -> str:
    """执行 MCP 工具调用（execute_tool 路由入口）。

    name 格式：mcp__<plugin_id>__<server_name>__<tool_name>
    返回 MCP 响应的文本内容拼接（content[].text）。

    每次调用写一条结构化审计记录（Agent 上下文、脱敏参数摘要、耗时、isError）；
    审计写入异常被吞掉，不影响工具调用结果。
    """
    parsed = _parse_tool_name(name)
    if not parsed:
        return f'Unknown tool: {name}'
    plugin_id, server_name, tool_name = parsed
    ctx = context if isinstance(context, dict) else {}
    started = time.time()
    is_error = False
    error_summary = ''
    try:
        try:
            records = _enabled_records()
            cfg = next((json.loads(r['config']) for r in records
                        if r['plugin_id'] == plugin_id and r['server_name'] == server_name), None)
            if not cfg:
                is_error = True
                error_summary = f'MCP server not enabled: {plugin_id}/{server_name}'
                return error_summary
            client = _get_client(plugin_id, server_name, cfg)
            result = client.call_tool(tool_name, arguments or {})
        except Exception as e:
            is_error = True
            error_summary = str(e)[:200]
            return f'Tool {name} execution error: {e}'
        # 组装文本输出
        parts = []
        for item in result.get('content') or []:
            if isinstance(item, dict) and item.get('type') == 'text':
                parts.append(item.get('text', ''))
        text_output = '\n'.join(parts) if parts else json.dumps(result, ensure_ascii=False)
        if result.get('isError'):
            is_error = True
            error_summary = text_output[:200] if text_output else 'tool returned isError'
        return text_output
    finally:
        elapsed_ms = round((time.time() - started) * 1000, 2)
        try:
            _get_audit_logger().info(
                'mcp_call tool=%s agent_id=%s task_id=%s agent_name=%s '
                'elapsed_ms=%s is_error=%s args=%s%s',
                name,
                ctx.get('agent_id') or '-',
                ctx.get('task_id') or '-',
                ctx.get('name') or '-',
                elapsed_ms,
                is_error,
                json.dumps(_summarize_arguments(arguments), ensure_ascii=False),
                f' error={error_summary}' if error_summary else '')
        except Exception:
            pass


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
