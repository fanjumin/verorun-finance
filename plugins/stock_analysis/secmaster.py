# secmaster.py — 证券主数据：符号解析 + UID 规范化 + 别名缓存
#
# 设计目标
# --------
# 1. 统一 UID 格式 {market}:{code}，跨 CN/HK/US 三市场。
# 2. resolve_symbol() 接受裸码、带前缀码、UID 三种输入，返回 SymbolInfo。
# 3. 别名表（中文简称 → UID）供 LLM 输出或用户输入快速定位。
# 4. 纯函数 + 进程内存缓存，无外部 DB 依赖；别名可由上层 ingest 写入。
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

_log = logging.getLogger("stock_analysis.secmaster")

# ── 市场常量 ──────────────────────────────────────────────────────────────
CN = "CN"
HK = "HK"
US = "US"

_MARKETS = {CN, HK, US}

# ── 交易所推断（CN 市场） ─────────────────────────────────────────────────
# 与 providers/commons.py 规则保持一致，不得改动。
_CN_EXCHANGE: dict = {}
for _p in ("4", "8"):
    _CN_EXCHANGE[_p] = "BJ"
_CN_EXCHANGE["920"] = "BJ"
for _p in ("5", "6", "9"):
    _CN_EXCHANGE[_p] = "SH"
# 剩余 (0/1/2/3) → SZ


def _cn_exchange(code: str) -> str:
    if code.startswith("920"):
        return "BJ"
    for prefix, exch in _CN_EXCHANGE.items():
        if code.startswith(prefix):
            return exch
    return "SZ"


# ── SymbolInfo ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SymbolInfo:
    market: str
    code: str
    exchange: str = ""
    name: str = ""

    @property
    def uid(self) -> str:
        return f"{self.market}:{self.code}"

    def __str__(self) -> str:
        return self.uid


# ── 解析 ──────────────────────────────────────────────────────────────────
_UID_RE = re.compile(r"^([A-Z]{2}):(.+)$")
_PREFIX_RE = re.compile(r"^(sh|sz|bj|hk|us)(\w+)$", re.I)


def _infer_market(code: str) -> tuple[str, str]:
    """裸码 → (market, exchange)。CN 码同时推断交易所。"""
    c = code.upper()
    # 交易所后缀（Yahoo/东财风）：0700.HK → HK/HKEX；AAPL.US → US（v2.1.0）
    sm = re.match(r"^([0-9]{1,5}|[A-Z]{1,6})\.(HK|US)$", c)
    if sm:
        return (HK, "HKEX") if sm.group(2) == "HK" else (US, "US")
    # 港股裸码 1~5 位（0700/00700/9988/9618）必须先于 CN 首字符分支：
    # 9/4/8/5 开头的 4 位港股（9988 阿里、9618 京东、9888 百度）否则会被 CN 分支
    # 因 startswith 截胡。A 股证券代码恒 6 位，6 位码不进此分支，零误伤（v2.1.0）。
    if re.match(r"^\d{1,5}$", c):
        return HK, "HKEX"
    if c.startswith(("4", "8", "920")) or c.startswith(("4", "8")):
        return CN, _cn_exchange(c)
    if c.startswith(("5", "6", "9")):
        return CN, _cn_exchange(c)
    if c.startswith(("0", "1", "2", "3")) and len(c) == 6:
        return CN, _cn_exchange(c)
    if re.match(r"^[A-Z]{1,5}$", c):
        return US, "US"
    return CN, _cn_exchange(c) if len(c) == 6 else ""


def resolve_symbol(symbol: str) -> Optional[SymbolInfo]:
    """解析符号，返回 SymbolInfo；无法识别返回 None。

    接受格式：
      - UID:  "CN:000001", "HK:00700", "US:AAPL"
      - 前缀: "sh000001", "hk00700", "usAAPL"
      - 裸码: "000001", "00700", "AAPL"
      - 别名: "平安银行" → 查别名表
    """
    if not symbol or not isinstance(symbol, str):
        return None
    raw = symbol.strip()
    if not raw:
        return None

    cached = _ALIAS_CACHE.get(raw)
    if cached is not None:
        return cached

    uid_m = _UID_RE.match(raw)
    if uid_m:
        mkt, code = uid_m.group(1).upper(), uid_m.group(2).upper()
        if mkt not in _MARKETS:
            return None
        exch = _cn_exchange(code) if mkt == CN else ("HKEX" if mkt == HK else "US")
        info = SymbolInfo(market=mkt, code=code, exchange=exch)
        _ALIAS_CACHE.put(raw, info)
        return info

    pfx_m = _PREFIX_RE.match(raw)
    if pfx_m:
        prefix = pfx_m.group(1).lower()
        code = pfx_m.group(2).upper()
        if prefix in ("sh", "sz", "bj"):
            mkt = CN
            exch = prefix.upper()
        elif prefix == "hk":
            mkt, exch = HK, "HKEX"
        else:
            mkt, exch = US, "US"
        info = SymbolInfo(market=mkt, code=code, exchange=exch)
        _ALIAS_CACHE.put(raw, info)
        return info

    code_upper = raw.upper()
    mkt, exch = _infer_market(code_upper)
    info = SymbolInfo(market=mkt, code=code_upper, exchange=exch)
    _ALIAS_CACHE.put(raw, info)
    return info


# ── 别名表 ────────────────────────────────────────────────────────────────
class _AliasStore:
    """线程安全别名表。name/uid → SymbolInfo，TTL 过期自动清理。"""

    def __init__(self, ttl: int = 86400):
        self._ttl = ttl
        self._store: Dict[str, tuple[float, SymbolInfo]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[SymbolInfo]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, info = entry
            if time.time() - ts > self._ttl:
                del self._store[key]
                return None
            return info

    def put(self, key: str, info: SymbolInfo) -> None:
        with self._lock:
            self._store[key] = (time.time(), info)

    def register(self, name: str, uid: str) -> Optional[SymbolInfo]:
        """注册别名。name 为中文/英文简称，uid 为 {market}:{code}。"""
        info = resolve_symbol(uid)
        if info is None:
            _log.warning("alias register failed: %s → %s (unresolvable)", name, uid)
            return None
        with self._lock:
            self._store[name] = (time.time(), info)
            self._store[uid] = (time.time(), info)
        return info

    def bulk_register(self, mapping: Dict[str, str]) -> int:
        """批量注册 {alias: uid}。返回成功数。"""
        ok = 0
        for name, uid in mapping.items():
            if self.register(name, uid) is not None:
                ok += 1
        return ok

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return {k: v[1].uid for k, v in self._store.items()}


_ALIAS_CACHE = _AliasStore()


def register_alias(name: str, uid: str) -> Optional[SymbolInfo]:
    return _ALIAS_CACHE.register(name, uid)


def bulk_register_aliases(mapping: Dict[str, str]) -> int:
    return _ALIAS_CACHE.bulk_register(mapping)


# ── 便捷查询 ──────────────────────────────────────────────────────────────
def to_uid(symbol: str) -> Optional[str]:
    info = resolve_symbol(symbol)
    return info.uid if info else None


def parse_uid(uid: str) -> Optional[tuple[str, str]]:
    """UID → (market, code)。非法格式返回 None。"""
    m = _UID_RE.match(uid.strip())
    if not m:
        return None
    market, code = m.group(1).upper(), m.group(2).upper()
    if market not in _MARKETS:
        return None
    return market, code
