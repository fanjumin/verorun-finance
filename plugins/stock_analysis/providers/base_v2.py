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
        """模板方法：凭据 → supports → 令牌桶 → _do_fetch → 校验 → 回执。"""
        cat = DataCategory(cat)
        if not self.supports_category(cat, symbol):
            raise ProviderUnavailable(f"{self.name} 不支持 {cat.value}")
        if self.required_secret and not self.secret():
            raise ProviderUnavailable(f"{self.name} 未配置凭据")

        if not self._bucket.acquire(kw.pop("cost_units", 1.0)):
            raise ProviderUnavailable(f"{self.name} 配额耗尽（令牌桶）")

        try:
            res = self._do_fetch(cat, symbol=symbol, **kw)
        except ProviderUnavailable:
            self._fail_streak += 1
            raise
        except Exception as e:                       # 上游异常统一包装
            self._fail_streak += 1
            raise ProviderUnavailable(f"{self.name} 上游异常: {type(e).__name__}: {e}") from e

        if res.empty:
            res.warnings.append("empty_result")      # 空数据不缓存，保留重试能力
        else:
            self._fail_streak = 0
            self._last_ok = time.time()
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
