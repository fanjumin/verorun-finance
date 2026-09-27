"""symbol_master.py — 证券主数据（全市场代码+名称）加载与检索

用途
----
为 `/api/search` 提供"按代码前缀 / 名称"检索标的的能力——补上插件此前缺失的
检索层（此前只能按完整代码查行情，中文名称一律 400）。

数据源与策略
------------
- 来源：akshare `stock_info_a_code_name()`（A 股全市场，实测 5500+ 行、单次约 17s）。
- 缓存：落库 `stock_analysis.sa_symbol_master`，按 TTL 刷新；检索走本地表，
  避免每个请求都打外部源。
- 首次可用性：由路由注册时启动的后台线程预热（不阻塞启动、不阻塞用户请求）；
  预热未完成期间的搜索返回 `warming=True`，由前端提示"标的库正在初始化"。

依赖
----
akshare 为可选导入：缺失时 `ensure()` 返回错误状态，接口降级为"仅支持完整代码"，
不影响插件其余功能。
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata

from . import models_sa
from .secmaster import resolve_symbol

_log = logging.getLogger("stock_analysis.symbol_master")

_TTL_SECONDS = 7 * 86400        # 名称/上市变动低频，7 天刷新一次
_MIN_ROWS = 1000                # 低于此值视为脏数据，触发重拉
_RETRY_COOLDOWN = 300.0         # 抓取失败后的冷却，避免每个请求都重试（拖慢热路径）
_CN_CODE_RE = re.compile(r"^\d{6}$")

_lock = threading.Lock()
_last_attempt = 0.0
_last_error: str | None = None
_warming = False

# 带市场前后缀的 A 股写法：用户从券商软件/研报里复制过来的是这些形态，
# 而主数据里存的是裸码 —— 不归一化就会「查得到名字、查不到代码」。
_CN_PREFIX_RE = re.compile(r"^(SH|SZ|BJ|SS)(\d{6})$")
_CN_SUFFIX_RE = re.compile(r"^(\d{6})\.(SH|SS|SZ|BJ)$")


def normalize_query(query: str) -> str:
    """检索词归一化：全角转半角、去空白、转大写；A 股带前后缀写法收敛为裸码。"""
    if not query:
        return ""
    text = unicodedata.normalize("NFKC", str(query))
    text = re.sub(r"\s+", "", text).upper()
    matched = _CN_PREFIX_RE.match(text) or _CN_SUFFIX_RE.match(text)
    if matched:
        for group in matched.groups():
            if _CN_CODE_RE.match(group):
                return group
    return text


def _fetch_rows() -> list:
    """从 akshare 拉全市场清单 → [(symbol, name, name_norm, market, exchange)]。"""
    import akshare as ak                                        # 可选依赖，按需导入
    frame = ak.stock_info_a_code_name()
    rows = []
    for code, name in zip(frame["code"], frame["name"]):
        symbol = str(code).strip().zfill(6)
        if not _CN_CODE_RE.match(symbol):
            continue
        label = str(name).strip()
        if not label:
            continue
        info = resolve_symbol(symbol)
        exchange = info.exchange if info else ""
        rows.append((symbol, label, normalize_query(label), "CN", exchange))
    return rows


def ensure(force: bool = False) -> dict:
    """确保主数据可用；返回 {'rows': n, 'ready': bool, 'refreshed': bool, 'error': str|None}。

    新鲜度足够则直接返回；过期/为空则拉取并落库。抓取失败进入冷却期，
    冷却期内不重复尝试（避免把外部源的慢/故障传导到每个搜索请求）。
    """
    global _last_attempt, _last_error, _warming
    try:
        models_sa.ensure_tables()                               # 全新部署/清库后首访自愈
        stats = models_sa.symbol_master_stats()
    except Exception as error:                                  # 表未建/库不可用
        _log.warning("symbol master stats failed: %s", error)
        return {"rows": 0, "ready": False, "refreshed": False, "error": "storage unavailable"}

    rows_now = stats["rows"]
    age = stats["age_seconds"]
    fresh = rows_now >= _MIN_ROWS and age is not None and age < _TTL_SECONDS
    if fresh and not force:
        return {"rows": rows_now, "ready": True, "refreshed": False, "error": None}

    now = time.time()
    if not force and (now - _last_attempt) < _RETRY_COOLDOWN:
        return {"rows": rows_now, "ready": rows_now >= _MIN_ROWS,
                "refreshed": False, "error": _last_error}

    # 非阻塞取锁：已有线程在刷新时不排队——单次抓取约 17s，排队会把外部源的慢
    # 传导到每个搜索请求上（前端表现为「卡住」）。此时按现状返回，由 warming 提示。
    if not _lock.acquire(blocking=False):
        return {"rows": rows_now, "ready": rows_now >= _MIN_ROWS,
                "refreshed": False, "error": _last_error}
    try:
        _last_attempt = time.time()
        _warming = True
        try:
            rows = _fetch_rows()
            if len(rows) < _MIN_ROWS:
                raise ValueError("insufficient rows: %d" % len(rows))
            written = models_sa.upsert_symbol_master(rows)
            _last_error = None
            _log.info("symbol master refreshed: %d rows", written)
            return {"rows": written, "ready": True, "refreshed": True, "error": None}
        except Exception as error:
            _last_error = "%s: %s" % (type(error).__name__, error)
            _log.warning("symbol master refresh failed: %s", _last_error)
            return {"rows": rows_now, "ready": rows_now >= _MIN_ROWS,
                    "refreshed": False, "error": _last_error}
        finally:
            _warming = False
    finally:
        _lock.release()

def warm_async() -> None:
    """后台预热主数据（best-effort，不阻塞调用方）。"""
    def _worker():
        try:
            ensure()
        except Exception as error:                              # 预热失败不影响主流程
            _log.warning("symbol master warm failed: %s", error)

    threading.Thread(target=_worker, name="sa-symbol-master-warm", daemon=True).start()


def search(query: str, limit: int = 10) -> tuple:
    """检索标的 → (items, state)。

    items: [{'symbol','name','market','exchange'}]
    state: ensure() 的返回，附 'warming' 供前端提示
    """
    normalized = normalize_query(query)
    if not normalized:
        return [], {"rows": 0, "ready": False, "refreshed": False, "error": None, "warming": False}

    # 预热期间 ensure() 走「冷却期 + 非阻塞取锁」直接返回，不会排队等抓取；
    # main data 尚空时下面的完整代码兜底仍然生效（6 位代码可直达）。
    state = ensure()
    try:
        items = models_sa.search_symbol_master(normalized, limit=limit)
    except Exception as error:
        _log.warning("symbol master search failed: %s", error)
        return [], dict(state, ready=False, error="storage unavailable")

    # 完整代码兜底：主数据未就绪时，仍允许按 6 位代码直达（与既有行情能力对齐）
    if not items and _CN_CODE_RE.match(normalized):
        info = resolve_symbol(normalized)
        items = [{"symbol": normalized, "name": "", "market": info.market if info else "CN",
                  "exchange": info.exchange if info else ""}]

    state = dict(state, warming=bool(_warming))
    return items, state
