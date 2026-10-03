#!/usr/bin/env python3
"""IM Gateway — 统一出站 HTTP 客户端（批次 D1）。

职责：
  1. 所有出站平台 API 调用统一**超时**（connect=5s / read=15s），杜绝裸 urlopen 永久挂起；
  2. 对**外部输入 URL**（如媒体库 file_url）提供 safe_fetch() —— SSRF 内网拦截 +
     下载体积上限 + 逐跳重定向校验；
  3. 提供进程内 TTLCache，供各平台 access_token 复用（飞书 / 企业微信 / 钉钉等）。

边界：
  - request/request_json 用于**写死的平台 API 域名**（open.feishu.cn、qyapi.weixin.qq.com、
    oapi.dingtalk.com、api.telegram.org、api.line.me、api.sgroup.qq.com …），只强制超时、
    关闭自动重定向；这些主机为常量，不做私网拦截。
  - safe_fetch 用于**任何来自外部内容的 URL**，强制 SSRF 校验。
  - 不引入新 pip 依赖（仅 requests + 标准库 ipaddress/socket）。
"""
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit

import requests
from requests.exceptions import RequestException

# ── 超时 / 体积 / 重定向常量 ──────────────────────────────
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 15.0
DEFAULT_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)

MAX_FETCH_BYTES = 10 * 1024 * 1024   # 外部文件下载上限 10MB
_FETCH_CHUNK = 64 * 1024
MAX_REDIRECTS = 3

_ALLOWED_SCHEMES = ('http', 'https')
_ALLOWED_PORTS = {None, 80, 443}

# 云元数据与保留段（显式列出，便于审计）
_BLOCKED_V4 = (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
    '169.254.0.0/16',          # 含 169.254.169.254（AWS/GCP/Azure 元数据）
    '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16',
    '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24',
    '224.0.0.0/4', '240.0.0.0/4',
)
_BLOCKED_V6 = (
    '::1/128', '::/128', 'fc00::/7', 'fe80::/10', 'ff00::/8', '2001:db8::/32',
    '64:ff9b::/96',          # NAT64（IPv4 嵌入）
    '2002::/16',             # 6to4
    '2001::/32',             # Teredo
)
_BLOCKED_HOST_SUFFIX = ('.local', '.internal', '.localhost', '.lan', '.home.arpa')

_BLOCK_V4_NETS = tuple(ipaddress.ip_network(c) for c in _BLOCKED_V4)
_BLOCK_V6_NETS = tuple(ipaddress.ip_network(c) for c in _BLOCKED_V6)


class HttpError(Exception):
    """出站 HTTP / SSRF 错误（调用方转成适配器错误或用户提示）。"""


class TTLCache:
    """极简进程内 TTL 缓存（线程安全靠 GIL 下的单语句读写，足够 token 场景）。"""

    def __init__(self):
        self._store = {}

    def get(self, key):
        item = self._store.get(key)
        if item is None:
            return None
        value, expire_at = item
        if time.monotonic() >= expire_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key, value, ttl_seconds: float):
        self._store[key] = (value, time.monotonic() + max(1.0, float(ttl_seconds)))

    def clear(self):
        self._store.clear()


# ── 平台 API（固定主机）：只强制超时 ──────────────────────

def request(method: str, url: str, *, timeout=None, allow_redirects: bool = False, **kwargs):
    """发起 HTTP 请求；默认统一超时、不自动跟随重定向（由调用方决定）。"""
    return requests.request(
        method, url,
        timeout=timeout or DEFAULT_TIMEOUT,
        allow_redirects=allow_redirects,
        **kwargs
    )


def request_json(method: str, url: str, *, timeout=None, **kwargs):
    """发起请求并解析 JSON。

    Returns:
        (status_code: int, data: dict)
    Raises:
        HttpError: 网络错误或响应非 JSON。
    """
    try:
        resp = request(method, url, timeout=timeout, **kwargs)
    except RequestException as e:
        raise HttpError(f'HTTP request failed: {e}') from e
    try:
        return resp.status_code, resp.json()
    except ValueError as e:
        raise HttpError(f'Non-JSON response (HTTP {resp.status_code})') from e


# ── 外部 URL：SSRF 拦截 + 限长下载 ────────────────────────

def _ip_is_blocked(ip_str: str) -> bool:
    """单个 IP 是否在禁止段；IPv4-mapped IPv6 先解映射再判定。解析失败视为不安全。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    nets = _BLOCK_V4_NETS if ip.version == 4 else _BLOCK_V6_NETS
    return any(ip in net for net in nets)


def _resolve_and_validate(host: str):
    """DNS 解析并校验**全部**记录（round-robin 下任一内网即拒绝）。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise HttpError(f'DNS resolution failed: {e}') from e
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise HttpError('DNS returned no address')
    for ip in ips:
        if _ip_is_blocked(ip):
            raise HttpError(f'Blocked address for host {host}: {ip}')


def validate_external_url(url: str) -> str:
    """校验外部 URL（scheme / userinfo / 主机后缀 / 端口 / 私网 IP）。

    合法返回原 URL；不合法抛 HttpError。
    """
    try:
        parts = urlsplit((url or '').strip())
    except ValueError as e:
        raise HttpError(f'Invalid URL: {e}') from e
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise HttpError('Only http/https URLs are allowed')
    if parts.username or parts.password:
        raise HttpError('Credentials in URL are not allowed')
    host = (parts.hostname or '').lower()
    if not host:
        raise HttpError('Missing host')
    if any(host.endswith(s) for s in _BLOCKED_HOST_SUFFIX):
        raise HttpError(f'Blocked host suffix: {host}')
    try:
        port = parts.port
    except ValueError as e:
        raise HttpError('Invalid port') from e
    if port not in _ALLOWED_PORTS:
        raise HttpError(f'Port not allowed: {port}')

    # 字面 IP 直接判；域名解析后校验全部记录
    try:
        ipaddress.ip_address(host)
        if _ip_is_blocked(host):
            raise HttpError(f'Blocked IP literal: {host}')
    except ValueError:
        _resolve_and_validate(host)
    return url


def safe_fetch(url: str, *, max_bytes: int = MAX_FETCH_BYTES, timeout=None) -> bytes:
    """安全下载外部资源：SSRF 拦截 + 逐跳重定向校验 + 体积上限。

    Raises:
        HttpError: URL 不合法 / 命中内网 / 超限 / 非 2xx。
    """
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        validate_external_url(current)
        try:
            resp = request('GET', current, timeout=timeout, stream=True)
        except RequestException as e:
            raise HttpError(f'Fetch failed: {e}') from e

        if 300 <= resp.status_code < 400:
            location = resp.headers.get('Location', '')
            resp.close()
            if not location or hop == MAX_REDIRECTS:
                raise HttpError('Too many or invalid redirects')
            current = urljoin(current, location)   # 下一跳重新走 SSRF 校验
            continue

        if resp.status_code != 200:
            resp.close()
            raise HttpError(f'Fetch returned HTTP {resp.status_code}')

        try:
            content_length = resp.headers.get('Content-Length')
            if content_length and int(content_length) > max_bytes:
                raise HttpError('Remote file exceeds size limit')
            buffer = b''
            for chunk in resp.iter_content(_FETCH_CHUNK):
                if not chunk:
                    continue
                buffer += chunk
                if len(buffer) > max_bytes:
                    raise HttpError('Remote file exceeds size limit during download')
            return buffer
        finally:
            resp.close()

    raise HttpError('Redirect limit exceeded')
