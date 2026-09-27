"""
providers/base_v2.py — stock_analysis v2 Provider 契约

设计目标
--------
1. 与 v1.7.1 的 providers/base.py 向后兼容：`supports()` 语义不变，
   gateway 的 ROUTE / COOLDOWN / 两级缓存逻辑可原样复用。
2. 统一返回 FetchResult：每条数据都携带 source / as_of / delay / provenance_id，
   供 evidence_bundle.py 做溯源与防幻觉校验。
3. 令牌桶配额替代固定窗口计数，抗突发；失败主动上报，供 gateway 摘除。
4. 凭据从加密存储解析，不落配置明文，不出现在 url / params 回执中。

落地步骤
--------
1) 将本文件放到 plugins/stock_analysis/providers/base_v2.py
2) 现有 provider（tushare/akshare/sina/tencent）改为继承 BaseProviderV2，
   把原 fetch 逻辑搬进 _do_fetch()，其余由模板方法处理。
3) gateway.py 的 _fetch_with_failover 改为消费 FetchResult，
   并把 result.delay_seconds / as_of 透传给调用方。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

from .base import DataCategory


# v2 扩展类别（尚未接入 gateway ROUTE，待 provider 实现后按需注册）
class DataCategoryV2(str, Enum):
    ADJ_FACTOR = "adj_factor"
    CORP_ACTION = "corp_action"
    CONSENSUS = "consensus"
    FORECAST = "forecast"
    SHAREHOLDER = "shareholder"
    MARGIN = "margin"
    DRAGON_TIGER = "dragon_tiger"
    UNLOCK = "unlock"
    CLASSIFICATION = "classification"
    INDEX_WEIGHT = "index_weight"
    EVENT = "event"
    MACRO = "macro"
    PROFILE = "profile"


# ---------------------------------------------------------------- 凭据


class SecretResolver:
    """
    凭据解析：插件配置（加密）→ 环境变量 → None。
    生产环境建议直接复用内核 provider_api_keys 的同款 AES-GCM 通道，
    这里只定义接口，避免插件层自己造加密轮子。
    """

    def __init__(self, getter: Callable[[str], Optional[str]]):
        self._get = getter

    def resolve(self, provider: str, key: str = "api_key") -> Optional[str]:
        for name in (f"{provider.upper()}_{key.upper()}",
                     f"{provider.upper()}_TOKEN",
                     f"{provider.upper()}_APIKEY"):
            v = self._get(name)
            if v:
                return v
        return None

    @staticmethod
    def mask(secret: Optional[str]) -> str:
        if not secret:
            return ""
        if len(secret) <= 8:
            return "*" * len(secret)
        return f"{secret[:2]}{'*' * (len(secret) - 6)}{secret[-4:]}"

    @staticmethod
    def _maybe_decrypt(value: str) -> str:
        """插件 crypto 密文自动解密；明文原样返回（decrypt fail-open）。"""
        try:
            from ..crypto import decrypt
            return decrypt(value) or value
        except Exception:      # noqa: BLE001 —— 解密失败绝不影响凭据链路
            return value

    @classmethod
    def from_plugin_config(cls, cfg):
        """从插件持久化配置构建解析器：插件配置 → 环境变量 → config.yaml。

        cfg 可为 dict 或零参可调用（返回配置 dict）。调用形式在每次 resolve()
        时重新读取配置，使设置页保存的新凭据立即生效——模块级 gateway 单例在
        Flask 应用上下文外构造，若在构造时快照配置，运行期保存将永远不生效。
        键名兼容插件声明（fmp_api_key / polygon_api_key / tushare_token）：
        resolve() 以 `{PROVIDER}_{KEY}`（如 FMP_API_KEY）询问，getter 归一化后
        命中插件配置键。插件配置中的 password 值若为密文则自动解密（fail-open）。
        """
        source = cfg if callable(cfg) else (lambda: cfg)

        def getter(name: str) -> Optional[str]:
            cfg_now = source() or {}
            if isinstance(cfg_now, dict):
                for k in (name, name.lower(), name.lower().replace("_", "")):
                    v = cfg_now.get(k)
                    if isinstance(v, str) and v.strip():
                        return cls._maybe_decrypt(v.strip())
            env = os.environ.get(name)
            if env and env.strip():
                return env.strip()
            try:
                import yaml
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "config.yaml")
                with open(path, encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                for k in (name, name.lower(), name.upper()):
                    v = loaded.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
            except Exception:      # noqa: BLE001 —— 配置文件缺失/损坏按未配置处理
                pass
            return None

        return cls(getter)


# ---------------------------------------------------------------- 回执


@dataclass
class FetchResult:
    """统一回执。gateway 与 evidence_bundle 消费同一结构。"""

    category: DataCategory
    data: Any                                  # DataFrame / dict / list
    source: str                                # "tushare" / "fmp" ...
    as_of: str                                 # 数据归属时点 ISO8601
    delay_seconds: int = 0                     # 0=实时, 60=延迟1min, 900=延迟15min
    url: str = ""                              # 调用端点（已剔除密钥）
    params: Dict[str, Any] = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    cost_units: float = 1.0                    # 配额消耗权重
    provenance_id: str = ""

    def __post_init__(self):
        if not self.provenance_id:
            raw = f"{self.source}|{self.category.value}|{self.url}|{self.as_of}"
            self.provenance_id = hashlib.sha1(raw.encode()).hexdigest()[:12]

    @property
    def empty(self) -> bool:
        if self.data is None:
            return True
        try:
            return len(self.data) == 0
        except TypeError:
            return False

    @property
    def freshness(self) -> str:
        """新鲜度分级，供 UI 打角标。"""
        try:
            import pandas as pd
            t = pd.Timestamp(self.as_of)
            age = (pd.Timestamp.utcnow().tz_localize(None) - t.tz_localize(None)
                   if t.tz else pd.Timestamp.now() - t).total_seconds()
        except Exception:
            return "unknown"
        if self.delay_seconds == 0 and age < 120:
            return "realtime"
        if age < 3600:
            return "delayed"
        if age < 24 * 3600:
            return "eod"
        return "stale"


class ProviderUnavailable(RuntimeError):
    """provider 不可用（无凭据 / 不支持 / 限流 / 上游错误）。
    gateway 捕获后按 COOLDOWN 摘除并 failover 下一源。
    """


# ---------------------------------------------------------------- 配额


class TokenBucket:
    """线程安全令牌桶。rate=每分钟补充速率，burst=桶容量。"""

    def __init__(self, rate_per_min: float, burst: float):
        self.rate = rate_per_min / 60.0
        self.capacity = burst
        self._tokens = burst
        self._ts = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: float = 1.0, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._ts) * self.rate)
                self._ts = now
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                wait = (n - self._tokens) / self.rate
            if time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 0.25))


# ---------------------------------------------------------------- Provider


def _record_call(provider: str, ok: bool, seconds: float, error: str = "") -> None:
    """指标埋点（方案 §4.4）。采集失败绝不影响取数主链路，异常一律吞掉。"""
    try:
        from ..metrics_collector import record_call
        record_call(provider, ok, seconds * 1000.0, error)
    except Exception:      # noqa: BLE001 —— 埋点必须零副作用
        pass


def _record_cooldown(provider: str) -> None:
    """记录冷却开始（令牌桶耗尽）。同样零副作用。"""
    try:
        from ..metrics_collector import record_cooldown
        record_cooldown(provider)
    except Exception:      # noqa: BLE001
        pass


# ═══════════════════════════════════════════════════════════════════════════
# net_proxy 软接入（2026-09-22）
# ═══════════════════════════════════════════════════════════════════════════
#
# 背景：net_proxy 已实现完整 EgressClient（SSRF 校验 + 规则路由 + 审计落库 +
#   熔断计分），但此前**没有任何插件使用它** —— stock_analysis 的 provider
#   全是裸 requests.get()，出网完全不受治理。实测（2026-09-22）：
#   裸请求只认环境变量代理，境内源被中间层拦成 403 时无通道可切。
#
# 设计取舍（软接入 + 降级直连）：
#   * stock_analysis 与 net_proxy 是**两个独立插件**，不得硬依赖 —— 否则
#     net_proxy 未启用 / 未安装时取数链路整体宕掉。故用惰性 import。
#   * net_proxy 不可用（未安装 / 导入失败 / 规则 DENY / 目标被 SSRF 拦）
#     时**降级为直连**，保证"没有代理也能取数"这一底线不变。
#   * 每次决策都在请求时做，不做进程级缓存 —— 规则是管理员可热改的。
#   * 审计由 net_proxy 侧统一落库；本侧不重复写日志。

_EGRESS_CLIENT = None
_EGRESS_IMPORT_FAILED = False


def _get_egress_client(config_reader=None):
    """惰性取 net_proxy 的 EgressClient；不可用时返回 None（调用方降级直连）。"""
    global _EGRESS_CLIENT, _EGRESS_IMPORT_FAILED
    if _EGRESS_IMPORT_FAILED:
        return None
    if _EGRESS_CLIENT is None:
        try:
            from plugins.net_proxy.egress import EgressClient as _EC
        except Exception:      # noqa: BLE001 —— 未安装 / 路径差异 → 永久降级
            try:
                from ...net_proxy.egress import EgressClient as _EC   # noqa: F401
            except Exception:
                _EGRESS_IMPORT_FAILED = True
                return None
        try:
            _EGRESS_CLIENT = _EC(config_reader)
        except Exception:      # noqa: BLE001
            _EGRESS_IMPORT_FAILED = True
            return None
    return _EGRESS_CLIENT


def egress_get(url, *, caller="stock_analysis", timeout=15, usage_tags=None,
               headers=None, params=None, config_reader=None):
    """统一出站 GET：优先走 net_proxy 治理链路，不可用时降级直连。

    Returns:
        requests.Response

    降级条件（任一命中即直连，且不报错）：
      * net_proxy 未安装 / 导入失败；
      * 规则判定 DENY 或目标被 SSRF 校验拦（本机目标如终端 provider）；
      * net_proxy 侧抛任何异常。

    说明：直连分支**同样**用 trust_env=False 的 Session，避免继承进程环境
      变量里的坏代理（2026-09-22 实测：环境 HTTP_PROXY 会让请求全挂）。
    """
    client = _get_egress_client(config_reader)
    if client is not None:
        kw = {}
        if headers is not None:
            kw["headers"] = headers
        if params is not None:
            kw["params"] = params
        if usage_tags is not None:
            kw["usage_tags"] = usage_tags
        try:
            return client.get(url, caller=caller, timeout=timeout, **kw)
        except Exception:      # noqa: BLE001 —— 治理链路任何失败都降级，不阻断取数
            pass
    return _direct_get(url, timeout=timeout, headers=headers, params=params)


def _direct_get(url, *, timeout=15, headers=None, params=None):
    """降级直连（不继承环境变量代理）。"""
    import requests
    s = requests.Session()
    s.trust_env = False
    try:
        return s.get(url, timeout=timeout, headers=headers, params=params)
    finally:
        try:
            s.close()
        except Exception:      # noqa: BLE001
            pass


class BaseProviderV2(ABC):
    """Provider 抽象 v2。

    子类只需：
      - 声明 name / market / categories / rate_per_min / burst
      - 实现 _do_fetch()
      - 可选实现 health() 做主动探测
    """

    name: str = "base"
    market: str = "GLOBAL"          # CN / US / HK / GLOBAL
    categories: frozenset = frozenset()
    rate_per_min: int = 60
    burst: int = 10
    required_secret: Optional[str] = None   # "api_key" / "token"
    authorized: bool = False                 # v1 兼容：gateway._record_usage 读取

    def __init__(self, secrets: Optional[SecretResolver] = None, session=None):
        self._secrets = secrets or SecretResolver(lambda _: None)
        self._bucket = TokenBucket(self.rate_per_min, self.burst)
        self._session = session
        self._fail_streak = 0
        self._last_ok = 0.0

    # ---- 契约 ----

    @classmethod
    def supports(cls) -> set:
        """v1 兼容：gateway._dispatch 用 `category in cls.supports()` 判类别。"""
        return set(cls.categories)

    def supports_category(self, cat: DataCategory, symbol: Optional[str] = None) -> bool:
        """v2 增强：类别 + 市场双重裁剪。symbol 为 None 时只判类别。"""
        if DataCategory(cat) not in self.categories:
            return False
        if symbol and self.market != "GLOBAL":
            try:
                from ..secmaster import resolve_symbol
                sid = resolve_symbol(symbol)
            except Exception:
                return False
            if sid is None or sid.market != self.market:
                return False
        return True

    def secret(self, key: Optional[str] = None) -> Optional[str]:
        return self._secrets.resolve(self.name, key or self.required_secret or "api_key")

    def fetch(self, cat: DataCategory, *, symbol: Optional[str] = None, **kw) -> FetchResult:
        """模板方法：凭据 → supports → 令牌桶 → _do_fetch → 校验 → 回执。

        ★ 指标埋点（方案 §4.4）：这里是**所有 provider 的唯一入口**，
          埋在此处即全量自动采集，无需逐个 provider 改代码。
          采集失败绝不影响取数主链路（任何异常都被吞掉）。
        """
        cat = DataCategory(cat)
        if not self.supports_category(cat, symbol):
            raise ProviderUnavailable(f"{self.name} 不支持 {cat.value}")
        if self.required_secret and not self.secret():
            raise ProviderUnavailable(f"{self.name} 未配置凭据")

        if not self._bucket.acquire(kw.pop("cost_units", 1.0)):
            self._fail_streak += 1
            _record_cooldown(self.name)              # 令牌桶耗尽 → 记冷却
            raise ProviderUnavailable(f"{self.name} 配额耗尽（令牌桶）")

        _t0 = time.perf_counter()
        try:
            res = self._do_fetch(cat, symbol=symbol, **kw)
        except ProviderUnavailable as e:
            self._fail_streak += 1
            _record_call(self.name, False, time.perf_counter() - _t0, str(e))
            raise
        except Exception as e:                       # 上游异常统一包装
            self._fail_streak += 1
            _record_call(self.name, False, time.perf_counter() - _t0,
                         f"{type(e).__name__}: {e}")
            raise ProviderUnavailable(f"{self.name} 上游异常: {type(e).__name__}: {e}") from e

        if res.empty:
            res.warnings.append("empty_result")      # 空数据不缓存，保留重试能力
        else:
            self._fail_streak = 0
            self._last_ok = time.time()
        # 命中定义：非空结果。空结果对投研等于没命中，必须计入分母。
        _record_call(self.name, not res.empty, time.perf_counter() - _t0,
                     "" if not res.empty else "empty_result")
        res.source = res.source or self.name
        res.category = cat
        res.__post_init__()
        return res

    @abstractmethod
    def _do_fetch(self, cat: DataCategory, *, symbol: Optional[str] = None, **kw) -> FetchResult:
        ...

    def health(self) -> dict:
        """主动探测。gateway 可周期性调用，用于区分"源挂了"与"缓存还热"。"""
        return {
            "provider": self.name,
            "configured": bool(not self.required_secret or self.secret()),
            "fail_streak": self._fail_streak,
            "last_ok": self._last_ok,
            "categories": sorted(c.value for c in self.categories),
            "market": self.market,
        }

    # ---- 子类工具 ----

    @property
    def http(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": "VeroRun-stock_analysis/2.0"})
            # 2026-09-22：不继承进程环境变量里的代理。实测环境存在
            # HTTP_PROXY=http://127.0.0.1:62521 时，trust_env=True 的 Session 会被
            # 强制走该代理（非用户 VPN），导致境内源集体异常。出网走哪条通道由
            # net_proxy 规则决定（见 egress_get），不由环境变量决定。
            self._session.trust_env = False
        return self._session

    def _get_json(self, url: str, params: dict, timeout: int = 15) -> Any:
        r = self.http.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _redact(url: str) -> str:
        """回执里剔除密钥，避免密钥进日志与证据包。"""
        import re
        return re.sub(r"(api[_-]?key|token|apikey)=[^&]+", r"\1=***", url, flags=re.I)
