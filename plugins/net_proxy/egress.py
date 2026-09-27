"""net_proxy 统一出站客户端 —— 目标校验 + 规则决策 + 执行 + 审计 + 计分。

方案 §7.5 原文流程：
    目标校验 → 规则决策 → 构造 proxies 执行 → 审计落库 → 计分

安全（§9.2）：
  * **业务目标**（插件运行时传入）的解析 IP 任一命中阻断网段 → 拒绝；
  * DIRECT 与代理两条路径**都校验**（防 DNS rebinding）；
  * 通道端点 / 探活目标**不做**该校验（管理员显式登记，§9.1 信任三分法），
    该判定在 fuse.py 侧，本模块只提供 `is_blocked_target()` 供业务目标使用。

计分口径（§7.5）：
  * transport 异常（ProxyError / ConnectionError / Timeout）与 HTTP >= 500 → 失败；
  * 4xx **不计**（调用方问题，不代表通道不健康）。

连接层（§11.2）：requests.Session 进程内复用（连接池友好）；
**不缓存 DB 连接**（models.py 自行借还）。

审计脱敏（§6.3）：只落 host + scheme + action + 状态码 + 耗时 + 字节 + 错误摘要，
**不落** 完整 URL / query / 凭据 / 响应体。
"""

import ipaddress
import socket
import time
from urllib.parse import urlparse

import requests

from . import models as m
from . import rules as R

__all__ = [
    'EgressError',
    'TargetBlockedError',
    'BLOCKED_NETWORKS',
    'is_blocked_target',
    'get_session',
    'close_session',
    'EgressClient',
    'egress_request',
    'resolve_proxy',
]

# ── 阻断网段 ────────────────────────────────────────────────────────────────
# 对齐 orchestrator/workflow_engine.py::_SSRF_BLOCKED_NETWORKS（方案 §9.2 同款清单，
# 并采用内核的完整超集：多出 198.18/15 / 224/4 / 240/4 / ff00::/8 等，
# 更安全且与内核行为一致；「复用同款清单」按超集实现不构成偏离）。
BLOCKED_NETWORKS = (
    ipaddress.ip_network('127.0.0.0/8'),       # 回环 v4
    ipaddress.ip_network('10.0.0.0/8'),        # A 类私网
    ipaddress.ip_network('172.16.0.0/12'),     # B 类私网
    ipaddress.ip_network('192.168.0.0/16'),    # C 类私网
    ipaddress.ip_network('169.254.0.0/16'),    # 链路本地（云元数据）
    ipaddress.ip_network('0.0.0.0/8'),         # 当前网络
    ipaddress.ip_network('100.64.0.0/10'),     # CGNAT
    ipaddress.ip_network('198.18.0.0/15'),     # 基准测试
    ipaddress.ip_network('224.0.0.0/4'),       # 多播 v4
    ipaddress.ip_network('240.0.0.0/4'),       # 保留 v4
    ipaddress.ip_network('::1/128'),           # 回环 v6
    ipaddress.ip_network('fc00::/7'),          # 唯一本地 v6
    ipaddress.ip_network('fe80::/10'),         # 链路本地 v6
    ipaddress.ip_network('ff00::/8'),          # 多播 v6
)

# 默认超时（config 缺省 default_timeout_s = 10）
DEFAULT_TIMEOUT_S = 10


class EgressError(RuntimeError):
    """出站过程的可预期失败（网络异常、通道不可用等）。"""


class TargetBlockedError(ValueError):
    """目标命中阻断网段，拒绝出站（§9.2）。"""


# ═══════════════════════════════════════════════════════════════════════════
# 目标校验（§9.2）
# ═══════════════════════════════════════════════════════════════════════════

def _extract_host(target) -> str:
    """从 URL / host:port / host 中取出 hostname。"""
    if not target:
        return ''
    raw = str(target).strip()
    if '://' in raw:
        try:
            host = urlparse(raw).hostname
        except Exception:
            host = None
        return (host or '').strip()
    # 无 scheme：手工剥离 userinfo / 端口 / 路径
    if '@' in raw:
        raw = raw.rsplit('@', 1)[1]
    for sep in ('/', '?', '#'):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
    if raw.startswith('['):          # IPv6 字面量 [::1]:8080
        end = raw.find(']')
        return raw[1:end] if end != -1 else raw
    if raw.count(':') == 1:
        raw = raw.rsplit(':', 1)[0]
    return raw.strip()


def is_blocked_target(target) -> bool:
    """目标是否指向内网/回环/保留地址（SSRF 防护，含 IPv4 / IPv6）。

    **校验所有解析结果**（`getaddrinfo` 全量遍历）—— 防 DNS rebinding：
    只查第一个 IP 会放过攻击者控制的轮询 DNS。

    Args:
        target: 完整 URL、`host:port` 或纯 host。

    Returns:
        bool: True 表示**应拒绝**。

    Raises:
        不抛错：无法解析（域名不存在 / DNS 故障）时返回 False，
        由 requests 层自行失败并进审计；此处只负责"解析成功且命中"的判定。
    """
    host = _extract_host(target)
    if not host:
        return False

    # 字面量 IP 直接判定（含被 0 填充 / 十进制 / 十六进制简写的防御）
    try:
        ip = ipaddress.ip_address(host)
        return _ip_blocked(ip)
    except ValueError:
        pass

    try:
        addrs = socket.getaddrinfo(host, None)
    except Exception:
        return False

    for item in addrs:
        try:
            addr = item[4][0]
        except (IndexError, TypeError):
            continue
        # IPv6 scoped address 形如 'fe80::1%eth0'，去掉 zone id
        if '%' in addr:
            addr = addr.split('%', 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_blocked(ip):
            return True
    return False


def _ip_blocked(ip) -> bool:
    if ip.is_unspecified or ip.is_loopback or ip.is_link_local:
        return True
    for net in BLOCKED_NETWORKS:
        try:
            if ip in net:
                return True
        except TypeError:
            continue
    return False


def _assert_target_allowed(target):
    """业务目标校验：命中阻断网段则抛 TargetBlockedError（§9.2）。"""
    if not target:
        raise TargetBlockedError('目标为空，拒绝出站')
    if is_blocked_target(target):
        raise TargetBlockedError(
            '目标 %s 解析到内网/保留地址，net_proxy 拒绝出站；'
            '内网互调请走既有直连路径，不经 net_proxy' % target)


# ═══════════════════════════════════════════════════════════════════════════
# Session 复用（§11.2：进程内复用，不缓存 DB 连接）
# ═══════════════════════════════════════════════════════════════════════════

_session = None


def get_session() -> requests.Session:
    """进程内复用的 requests.Session（连接池友好）。

    ★ trust_env=False（2026-09-22 修复）：不得继承进程环境变量里的代理。
      实测本机存在 HTTP_PROXY=http://127.0.0.1:62521（WorkBuddy 沙箱代理，非用户 VPN），
      trust_env=True 时它会与显式传入的 proxies 打架 → 全部 ProxyError 且每次撞满超时；
      对照实验：干净 Session 为 HTTP 200 / 2.0s。
      出网走哪条通道由 rules.resolve 决定（§7.4），不由环境变量决定。
    """
    global _session
    if _session is None:
        _session = requests.Session()
        _session.trust_env = False
    return _session


def close_session():
    """释放 Session（插件 disable/uninstall 时调用）。"""
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:
            pass
        _session = None


# ═══════════════════════════════════════════════════════════════════════════
# 计分口径（§7.5）
# ═══════════════════════════════════════════════════════════════════════════

def _counts_as_failure(status_code=None, exc=None) -> bool:
    """transport 异常 或 HTTP >= 500 → 计失败；4xx 不计（调用方问题）。"""
    if exc is not None:
        return isinstance(exc, (requests.exceptions.ProxyError,
                                requests.exceptions.ConnectionError,
                                requests.exceptions.Timeout,
                                requests.exceptions.SSLError))
    if status_code is None:
        return False
    try:
        return int(status_code) >= 500
    except (TypeError, ValueError):
        return False


def _short_error(exc) -> str:
    """错误摘要入库（截断，避免超长堆栈污染审计表）。"""
    if exc is None:
        return ''
    text = '%s: %s' % (type(exc).__name__, exc)
    return text[:400]


# ═══════════════════════════════════════════════════════════════════════════
# 决策（委托 rules.py，便于调用方只依赖本模块）
# ═══════════════════════════════════════════════════════════════════════════

def resolve_proxy(target, caller, usage_tags=None):
    """规则层决策，不执行请求（§7.5）。

    Returns:
        dict: `{'action': str, 'proxies': dict, 'channel': row|None,
                'level': str, 'reason': str}`
    """
    return dict(R.resolve(target, caller, usage_tags))


# ═══════════════════════════════════════════════════════════════════════════
# EgressClient
# ═══════════════════════════════════════════════════════════════════════════

class EgressClient:
    """统一出站客户端。

    插件→插件主路径（§8.1 ①②）：
        client = EgressClient(config_reader)
        resp = client.egress_request('GET', url, caller='veroscholar', timeout=15)

    Args:
        config_reader: 可选，签名 `(key, default=None) -> value`，
            用于读取 `default_timeout_s` / `fuse_threshold` /
            `fuse_cooldown_minutes`。省略则用代码缺省。
    """

    def __init__(self, config_reader=None):
        self._cfg = config_reader

    def _cfg_get(self, key, default=None):
        if self._cfg is None:
            return default
        try:
            val = self._cfg(key, default)
        except Exception:
            return default
        return default if val is None else val

    def default_timeout(self):
        raw = self._cfg_get('default_timeout_s', DEFAULT_TIMEOUT_S)
        try:
            t = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT_S
        return t if t > 0 else DEFAULT_TIMEOUT_S

    def _fuse_params(self):
        def _int(key, dflt):
            raw = self._cfg_get(key, dflt)
            try:
                v = int(raw)
            except (TypeError, ValueError):
                return dflt
            return v if v > 0 else dflt
        return _int('fuse_threshold', 3), _int('fuse_cooldown_minutes', 10)

    # ── 主路径 ────────────────────────────────────────────────────────────

    def egress_request(self, method, url, caller, timeout=None, **kwargs):
        """执行出站请求（§7.5 全流程）。

        Args:
            method: HTTP 方法。
            url: 完整 URL（**业务目标，强制走 §9.2 校验**）。
            caller: 调用方标识（进审计）。
            timeout: 秒；省略取 config `default_timeout_s`。
            **kwargs: 透传 requests（headers / params / json / data 等）。

        Returns:
            requests.Response

        Raises:
            TargetBlockedError: 目标命中阻断网段。
            PermissionError: 规则决策为 DENY（§7.4）。
            EgressError: 通道不可构造 proxies 等可预期失败。
        """
        if not url:
            raise EgressError('url 不能为空')

        # 1) 目标校验（§9.2）：DIRECT 与代理两路径都校验
        _assert_target_allowed(url)

        # 2) 规则决策
        decision = R.resolve(url, caller, kwargs.pop('usage_tags', None))
        action = decision['action']

        if action == 'DENY':
            self._audit(caller, url, action, None, None, None, None,
                        '规则判定 DENY')
            raise PermissionError(
                'net_proxy 规则拒绝该目标：%s（%s）' % (url, decision.get('reason')))

        # 3) 构造 proxies
        proxies = decision.get('proxies') or {}
        channel = decision.get('channel')
        channel_id = channel['id'] if channel else None
        if action.startswith('CHANNEL:') and not proxies:
            raise EgressError(
                '通道 %s 无法构造代理地址（凭据或字段异常）' % action)

        # 4) 执行
        if timeout is None:
            timeout = self.default_timeout()
        method_up = str(method or 'GET').upper()

        t0 = time.time()
        resp = None
        exc = None
        try:
            resp = get_session().request(
                method_up, url, proxies=proxies or None,
                timeout=timeout, **kwargs)
        except Exception as e:            # noqa: BLE001 —— 需分类后计分
            exc = e

        latency_ms = int(round((time.time() - t0) * 1000))
        status_code = getattr(resp, 'status_code', None)

        # 5) 审计（脱敏：只落 host，不落完整 URL / query / 凭据）
        self._audit(caller, url, action, channel_id, status_code, latency_ms,
                    _bytes_down(resp), _short_error(exc))

        # 6) 计分（§7.5 口径）
        if channel_id is not None:
            ok = not _counts_as_failure(status_code, exc)
            # transport 异常 / 5xx 计失败；4xx 与 2xx/3xx 计成功（复位计数）
            try:
                thr, cool = self._fuse_params()
                m.record_channel_result(channel_id, ok, thr, cool)
            except Exception:
                pass  # 计分失败不影响业务返回

        if exc is not None:
            raise exc
        return resp

    def _audit(self, caller, url, action, channel_id, status_code,
               latency_ms, bytes_down, error):
        """写审计（§6.3 脱敏口径；失败静默，不阻断业务）。

        **脱敏**：只解析出 scheme + hostname 落库，完整 URL / query /
        userinfo 一律不落 —— 目标 URL 里可能带 API key。
        """
        scheme, target_host = '', ''
        try:
            parsed = urlparse(url or '')
            scheme = parsed.scheme or ''
            target_host = parsed.hostname or ''
        except Exception:
            pass
        try:
            m.write_request_log(
                caller=caller or '',
                target_host=target_host,
                scheme=scheme,
                action=action or '',
                channel_id=channel_id,
                status_code=status_code,
                latency_ms=latency_ms,
                bytes_up=0,
                bytes_down=bytes_down or 0,
                error=error or '',
            )
        except Exception:
            pass

    # ── 便捷方法 ──────────────────────────────────────────────────────────

    def get(self, url, caller, timeout=None, **kwargs):
        return self.egress_request('GET', url, caller, timeout, **kwargs)

    def post(self, url, caller, timeout=None, **kwargs):
        return self.egress_request('POST', url, caller, timeout, **kwargs)


def _bytes_down(resp):
    """响应体字节数（按 Content-Length 粗估，缺失则 0；§6.3 仅供观测）。"""
    if resp is None:
        return 0
    try:
        raw = resp.headers.get('Content-Length')
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


# 模块级单例（config_reader 由插件实例注入，见 __init__.py）
_default_client = None


def egress_request(method, url, caller, timeout=None, config_reader=None, **kwargs):
    """模块级便捷入口。每次调用用同一 Session，config_reader 可省。"""
    global _default_client
    if config_reader is not None:
        return EgressClient(config_reader).egress_request(
            method, url, caller, timeout, **kwargs)
    if _default_client is None:
        _default_client = EgressClient(None)
    return _default_client.egress_request(method, url, caller, timeout, **kwargs)
