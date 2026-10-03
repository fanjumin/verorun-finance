# gateway.py — 数据网关：类别路由 + 源前缀缓存 + failover + 健康摘除 + 交叉校验 + 双层限频 + 落盘缓存
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime

import pandas as pd

from plugins._base import ratelimit
from .providers.akshare_provider import AkshareProvider
from .providers.base import BaseProvider, DataCategory, ProviderError
from .providers.commons import index_symbol, market_symbol
from .providers.sina import SinaProvider
from .providers.tencent import TencentProvider
from .providers.tushare_provider import TushareProvider
from .providers.fmp_provider import FMPProvider
from .providers.polygon_provider import PolygonProvider
from .providers.user_supplied import UserSuppliedProvider
from .providers.terminal_provider import WindProvider, ChoiceProvider
# B 段 S1：财报免费备源（tushare 财报需 2000 积分，120 积分账号必失败）
from .providers.akshare_fundamental import AkshareFundamentalProvider
# B 段 S3：中国宏观免费备源（原 MACRO 只有 FMP，未配 key 时 /api/macro 恒 404）
from .providers.akshare_macro import AkshareMacroProvider
from .providers.akshare_consensus import AkshareConsensusProvider

try:
    from .providers.base_v2 import (BaseProviderV2, FetchResult,
                                    ProviderUnavailable, SecretResolver)
except ImportError:
    FetchResult = None
    BaseProviderV2 = object
    SecretResolver = None

    class ProviderUnavailable(RuntimeError):
        pass

_log = logging.getLogger("stock_analysis.gateway")

# ── 阶段2 路由表：tushare 授权主源（积分探针无权限类别自动裁剪）→ 备源 failover ──
# 终端桥（Wind/Choice）挂链尾：DATA_PROVIDER=wind|choice 偏好时经
# apply_preference 前置；桥未启动时连接失败计入冷却，不影响默认主源链路。
ROUTE: dict = {
    DataCategory.KLINE: [TushareProvider, AkshareProvider, SinaProvider, PolygonProvider,
                         WindProvider, ChoiceProvider],
    # akshare 免费备源插在 tushare 之后：有积分走 tushare（字段最全），
    # 无积分自动落到这里，财报页不至于空态。
    DataCategory.FUNDAMENTAL: [TushareProvider, AkshareFundamentalProvider,
                               FMPProvider, UserSuppliedProvider],
    DataCategory.MONEYFLOW: [TushareProvider],
    DataCategory.NEWS: [SinaProvider, PolygonProvider],
    DataCategory.QUOTE: [TencentProvider, SinaProvider, PolygonProvider, WindProvider, ChoiceProvider],
    DataCategory.INDEX: [TencentProvider, WindProvider, ChoiceProvider],
    # S4：A 股一致预期免费源（同花顺，免 key）挂链尾 —— 有 FMP key 或有 Tushare
    # 2000 积分时仍优先走前面两源（字段更全），无 key 自动落到这里。
    DataCategory.CONSENSUS: [FMPProvider, TushareProvider, UserSuppliedProvider,
                             AkshareConsensusProvider],
    DataCategory.PROFILE: [FMPProvider, PolygonProvider],
    DataCategory.FORECAST: [FMPProvider, UserSuppliedProvider],
    DataCategory.TOPLIST: [TushareProvider],
    DataCategory.MARGIN: [TushareProvider],
    DataCategory.NORTHBOUND: [TushareProvider],
    DataCategory.SHAREFLOAT: [TushareProvider],
    DataCategory.HOLDERNUMBER: [TushareProvider],
    # 方案 §4.2/§4.3：五档与分笔目前只有腾讯实现（免 key 公开行情）；
    # 终端桥（wind/choice）将来在 provider 里支持 DEPTH/TICKS 后自动进链，
    # 现在它们的 categories 不含这两类，会被 `category in cls.supports()` 过滤掉。
    DataCategory.DEPTH: [TencentProvider, WindProvider, ChoiceProvider],
    DataCategory.TICKS: [TencentProvider, WindProvider, ChoiceProvider],
    # 方案 §4.6 宏观 EDB：FMP 覆盖多国（需 key）；akshare_macro 补 **中国** 免费源
    # （只在 country=CN 时接管，其余国家明确不支持 → 自动落回 FMP）。
    DataCategory.MACRO: [FMPProvider, AkshareMacroProvider],
}
TTL = {DataCategory.KLINE: 300, DataCategory.QUOTE: 60, DataCategory.INDEX: 60,
       DataCategory.NEWS: 600, DataCategory.FUNDAMENTAL: 300, DataCategory.MONEYFLOW: 300,
       DataCategory.CONSENSUS: 3600, DataCategory.PROFILE: 86400, DataCategory.FORECAST: 3600,
       DataCategory.TOPLIST: 600, DataCategory.MARGIN: 300, DataCategory.NORTHBOUND: 300,
       DataCategory.SHAREFLOAT: 86400, DataCategory.HOLDERNUMBER: 86400,
       DataCategory.MACRO: 86400,
       DataCategory.DEPTH: 10, DataCategory.TICKS: 30}
# P1 分钟线：日内 bar 会变，故不落盘（_disk_get 默认「当日有效」会让盘中整天命中
# 早间旧数据，属 #SA-20260831-14 同类问题的更细粒度版本），只用进程内存短缓存。
# 1m 仍未接入（见 kline_service._UNSUPPORTED_PERIODS）。
_MINUTE_FREQS = frozenset({"5m", "15m", "30m", "60m"})
MINUTE_CACHE_TTL = 60                            # 分钟线内存缓存有效期（秒）
COOLDOWN = 300                                   # 连续失败摘除时长（秒）
# 连续失败触发冷却的次数阈值（原为 _dispatch 内字面量 3；2026-09-20 提为命名常量，
# 供 /api/constants 下发给壳层，避免前端镜像漂移）
COOLDOWN_FAIL_STREAK = 3
# #SA-20260830-02：K 线数据新鲜度阈值（自然日）。10 天可覆盖春节/国庆长假且
# 不误伤正常周末/短假；陈旧超过该阈值的数据一律拒绝输出（failover 下一源）。
MAX_STALE_DAYS = 10
# P2-11：进程内存结果缓存必须有界。写入超限时先逐过期项；仍超限则按最接近过期者逐出。
MAX_MEM_CACHE = 1024
# S1 补完：财报落盘缓存有效期（自然日）。财报按季度更新，7 天足够；
# 命中期内重复访问走磁盘（毫秒级），不再付 akshare 财报约 8s/股 的网络开销。
# 只影响 FUNDAMENTAL；K 线仍走 _disk_get 默认分支（当日有效），行为不变。
FUND_DISK_TTL_DAYS = 7
# S4：一致预期落盘缓存有效期（自然日）。机构预测随研报更新，但同花顺单次 1~7s，
# 3 天足够；与财报同理，没有「当日」概念。
CONSENSUS_DISK_TTL_DAYS = 3


def _resolve_disk_dir() -> str:
    """P3-3 修复：缓存目录解析——env STOCK_DATA_CACHE_DIR 优先，其次 config.yaml 的 DATA_CACHE_DIR。"""
    env = os.environ.get("STOCK_DATA_CACHE_DIR")
    if env:
        return env
    try:
        import yaml
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        with open(path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        val = loaded.get("DATA_CACHE_DIR") or loaded.get("data_cache_dir")
        if isinstance(val, str) and val.strip():
            return val.strip()
    except Exception:
        pass
    return "./data/cache"


DISK_DIR = _resolve_disk_dir()   # env STOCK_DATA_CACHE_DIR > config.yaml DATA_CACHE_DIR
_PG_LIMIT = {"tushare": 300, "akshare": 120, "sina": 180, "tencent": 600}  # 跨 worker 60s 窗口总量

# 进程内信号量：单源并发上限（akshare 东财接口保守取值；tushare 按 120 次/min 档取 4）
_SEMS = {"tushare": threading.BoundedSemaphore(4),
         "akshare": threading.BoundedSemaphore(2),
         "sina": threading.BoundedSemaphore(4),
         "tencent": threading.BoundedSemaphore(8)}

_conflict_log: list = []                         # conflict tag 环形留痕（同时打结构化日志）


# ── 落盘缓存（阶段3）：DataFrame 经 to_json(orient='split') 序列化，当日有效 ──
def _serialize(data):
    if isinstance(data, pd.DataFrame):
        return {"__df__": True, "value": data.to_json(orient="split")}
    return {"__df__": False, "value": data}


def _deserialize(payload):
    if payload.get("__df__"):
        # pandas 该版本 read_json 对字符串一律按文件路径处理（不解析内联 JSON），
        # 这里手动解析 to_json(orient='split') 布局重建 DataFrame。
        # 注意：to_json(orient='split') 将 DatetimeIndex 序列化为毫秒时间戳整数，
        # 必须 unit="ms" 解析（默认按纳秒解释会得到 1970-01-01 的畸形索引）。
        raw = json.loads(payload["value"])
        frame = pd.DataFrame(raw["data"], columns=raw["columns"],
                             index=pd.to_datetime(raw["index"], unit="ms"))
        frame.index.name = "date"
        return frame
    return payload["value"]


def _disk_path(key: str) -> str:
    return os.path.join(DISK_DIR, key.replace(":", "_").replace("/", "_") + ".json.gz")


def _disk_get(key: str, max_age_days: float = None):
    """读落盘缓存。

    max_age_days=None（默认，K 线行为，不可改）：沿用 payload["day"] 判「当日有效」。
    max_age_days=N：按 payload["ts"] 判「N 天内有效」——财报等低频数据用，
    避免每天首次访问都重新付费级取数（akshare 财报约 8s/股）。
    """
    path = _disk_path(key)
    if os.path.exists(path):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                payload = json.load(f)
            if max_age_days is None:
                fresh = payload["day"] == time.strftime("%Y-%m-%d")
            else:
                ts = payload.get("ts")
                fresh = ts is not None and \
                    (time.time() - float(ts)) <= max_age_days * 86400
            if fresh:
                return _deserialize(payload["data"]), payload.get("source"), payload.get("provenance")
        except Exception as err:
            _log.warning("disk cache read failed: %s", err)   # 坏缓存走网络，不阻塞
    return None


def _unwrap_result(result):
    """v2 FetchResult → (raw_data, provenance_meta)。v1 原始数据原样返回。"""
    if FetchResult is not None and isinstance(result, FetchResult):
        meta = {"provenance_id": result.provenance_id, "as_of": result.as_of,
                "delay_seconds": result.delay_seconds, "url": result.url,
                "warnings": result.warnings}
        return result.data, meta
    return result, None


def _disk_put(key: str, data, source_name: str, provenance: dict = None):
    try:
        os.makedirs(DISK_DIR, exist_ok=True)
        payload = {"day": time.strftime("%Y-%m-%d"),
                   # S1 补完：epoch 时间戳。K 线沿用 day 判「当日有效」；
                   # 财报按季度更新，用 ts 支持跨天有效期（见 _disk_get max_age_days）。
                   "ts": time.time(),
                   "source": source_name,
                   "data": _serialize(data)}
        if provenance:
            payload["provenance"] = provenance
        # #SA-20260831-14：K 线缓存记录数据末行日期，供命中时做交易日新鲜度校验。
        # 当日早间（收盘前）写入的缓存其末行停留在上一交易日，若整天命中会输出
        # data_date 陈旧的数据（ANALYZE-01/EDGE-08 根因）。
        if isinstance(data, pd.DataFrame) and len(data):
            payload["data_date"] = str(pd.Timestamp(data.index[-1]).date())
        with gzip.open(_disk_path(key), "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as err:
        _log.warning("disk cache write failed: %s", err)      # 落盘失败不阻塞主链路


def _ensure_adjusted(symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
    """自报 raw 且存在复权事件时，用 corporate_actions.AdjustmentEngine 补齐真 hfq。

    修复 S0-1 的第二半：口径诚实化只是停止误标，真正恢复 hfq 能力需要接线引擎
    （此前 corporate_actions 是"实现了但全库零引用"的死代码）。
    """
    if frame is None or frame.empty or "close_hfq" in frame.columns \
            and str(frame.get("price_basis", pd.Series(["raw"])).iloc[0]).lower() == "hfq":
        return frame
    try:
        from .corporate_actions import AdjustmentEngine, load_events
        events = load_events(symbol)
        if not events:
            return frame                          # 无事件 → 保持 raw，由 meta 如实告知
        engine = AdjustmentEngine()
        out = engine.apply(frame, events)
        out["price_basis"] = "hfq"
        out.attrs["basis_source"] = "corporate_actions"
        errs = engine.validate(out)               # 返回问题列表（不抛异常）
        if errs:
            _log.warning("adjust validate %s: %s", symbol, "; ".join(errs))
        return out
    except Exception as err:
        _log.warning("adjust failed %s, fallback to raw: %s", symbol, err)
        return frame


class DataGateway:
    def __init__(self):
        self._secret_resolver = self._build_secret_resolver()
        self._instances = {}
        for chain in ROUTE.values():
            for cls in chain:
                if SecretResolver is not None and issubclass(cls, BaseProviderV2):
                    self._instances[cls] = cls(secrets=self._secret_resolver)
                else:
                    self._instances[cls] = cls()
        self._cache: dict[str, tuple[float, object]] = {}
        self._fail_streak: dict[type, int] = {}
        self._cooldown_until: dict[type, float] = {}
        # F3：_usage 线程局部——模块级单例在 batch 多线程下共享，若用普通列表，
        # 并发请求的 data_sources 归属会串数据（合规输出事故）。threading.local
        # 使每个请求线程只看到自己的数据点记录。
        self._usage: threading.local = threading.local()

    @staticmethod
    def _build_secret_resolver():
        """统一凭据解析器：插件持久化配置 → env → config.yaml。

        模块级单例 gateway 在 Flask 应用上下文外构造，因此配置读取必须推迟到
        resolve() 调用时（传可调用源给 from_plugin_config），否则设置页保存的
        新凭据在下次解析时不会生效。无上下文/未启用时返回空 dict，解析器自动
        回退 env → config.yaml（与现状一致：未配置 token 时免费源回退不变）。
        """
        def plugin_cfg() -> dict:
            try:
                from flask import current_app
                pm = current_app.extensions.get("plugin_manager")
                if pm is not None and pm.is_enabled("stock_analysis"):
                    return pm.get_config("stock_analysis") or {}
            except Exception:      # noqa: BLE001 —— 无上下文按未配置处理
                pass
            return {}
        return SecretResolver.from_plugin_config(plugin_cfg)

    # P2-11：进程内存缓存有界写入。读路径已跳过过期项；写入仅在超限时触发一次清理，
    # 先逐过期项，仍超限再按「最接近过期」逐出约 1/4，避免长跑进程无界增长。
    def _cache_put(self, key: str, entry: tuple) -> None:
        now = time.time()
        if len(self._cache) >= MAX_MEM_CACHE:
            expired = [k for k, v in self._cache.items() if v[0] <= now]
            for k in expired:
                del self._cache[k]
            if len(self._cache) >= MAX_MEM_CACHE:
                oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:MAX_MEM_CACHE // 4]
                for k, _ in oldest:
                    self._cache.pop(k, None)
        self._cache[key] = entry

    # ── 对外入口（阶段2 增 get_fundamental/get_moneyflow）──
    def get_kline(self, symbol: str, datalen: int = 120, freq: str = "daily") -> pd.DataFrame:
        """K 线取数；freq ∈ {daily,weekly,monthly}（分钟线未接入）。
        自报 raw 的帧按 sa_corp_action 事件补齐真 hfq（见 §5.3）。"""
        return _ensure_adjusted(symbol, self._dispatch(DataCategory.KLINE, symbol,
                                                       datalen=datalen, freq=freq))

    def get_quote(self, symbol: str, category: DataCategory = DataCategory.QUOTE) -> dict:
        return self._dispatch(category, symbol)

    def get_news(self, symbol: str) -> list:
        return self._dispatch(DataCategory.NEWS, symbol)

    def get_fundamental(self, symbol: str, periods: int = 8) -> dict:
        """深财报四表合一（Tushare income/balance/cashflow/fina_indicator）。无权限/无 token 抛 ProviderError。

        periods 透传给 provider 的 head(N)；默认 8 与 provider 默认一致，老调用方行为不变。
        """
        return self._dispatch(DataCategory.FUNDAMENTAL, symbol, periods=periods)

    def get_moneyflow(self, symbol: str, days: int = 5) -> pd.DataFrame:
        """个股资金流（Tushare moneyflow 最近 N 日，日期索引）。"""
        return self._dispatch(DataCategory.MONEYFLOW, symbol, days=days)

    def get_consensus(self, symbol: str) -> dict:
        """分析师一致预期（营收/利润/EPS/目标价）。FMP（美股）/ Tushare（A 股业绩预告）。"""
        return self._dispatch(DataCategory.CONSENSUS, symbol)

    def get_profile(self, symbol: str) -> dict:
        """公司概况（行业、市值、描述）。FMP / Polygon。"""
        return self._dispatch(DataCategory.PROFILE, symbol)

    def get_forecast(self, symbol: str) -> dict:
        """预测/模型输入（用户自供 / FMP price target）。"""
        return self._dispatch(DataCategory.FORECAST, symbol)

    def get_toplist(self, symbol: str, **kwargs) -> list:
        """龙虎榜（Tushare top_list/top_inst）。"""
        return self._dispatch(DataCategory.TOPLIST, symbol, **kwargs)

    def get_margin(self, symbol: str, **kwargs) -> dict:
        """融资融券（Tushare margin 明细 + 最新汇总）。"""
        return self._dispatch(DataCategory.MARGIN, symbol, **kwargs)

    def get_northbound(self, **kwargs) -> list:
        """北向资金流向（Tushare moneyflow_hsgt，市场级别）。"""
        return self._dispatch(DataCategory.NORTHBOUND, "", **kwargs)

    def get_sharefloat(self, symbol: str, **kwargs) -> list:
        """限售股解禁（Tushare share_float）。"""
        return self._dispatch(DataCategory.SHAREFLOAT, symbol, **kwargs)

    def get_holdernumber(self, symbol: str, **kwargs) -> list:
        """股东户数（Tushare stk_holdernumber）。"""
        return self._dispatch(DataCategory.HOLDERNUMBER, symbol, **kwargs)

    def get_macro(self, indicator: str, country: str = "US", **kwargs) -> pd.DataFrame:
        """宏观 EDB 指标时序（方案 §4.6）：date 索引 + value 列的 DataFrame。

        宏观指标不属于任何个股，symbol 传空串（与北向资金同款处理）。
        无可用源（FMP 未配 key）抛 ProviderError，由路由转成 404 + 明确 error。
        """
        return self._dispatch(DataCategory.MACRO, "", indicator=indicator,
                              country=country, **kwargs)

    def get_depth(self, symbol: str) -> dict:
        """五档盘口（方案 §4.2）：{name, price, prev_close, buy[5], sell[5], spread, asOf}。"""
        return self._dispatch(DataCategory.DEPTH, symbol)

    def get_ticks(self, symbol: str, limit: int = 50) -> dict:
        """分笔成交（方案 §4.3）：{rows:[{seq,time,price,change,volume,amount,side,direction}]}。"""
        return self._dispatch(DataCategory.TICKS, symbol, limit=limit)

    def apply_preference(self, preferred: str):
        """DATA_PROVIDER 语义：首选源覆盖——将指定 provider 排到其所在链头；空值按 ROUTE 顺序。"""
        if not preferred:
            return
        for chain in ROUTE.values():
            for i, cls in enumerate(chain):
                if cls.name == preferred:
                    chain.remove(cls)
                    chain.insert(0, cls)
                    break

    def _usage_entries(self) -> list:
        """当前线程的 usage 记录列表（惰性初始化）。"""
        if not hasattr(self._usage, "val"):
            self._usage.val = []
        return self._usage.val

    def usage_snapshot(self) -> list:
        """取走并清空本次分析已用数据点；供 stock_skill 聚合 data_sources。"""
        usage = self._usage_entries()
        self._usage.val = []
        return usage

    # ── 内部：进程缓存 → 落盘缓存 → 网络链 ──
    def _available_chain(self, category: DataCategory) -> list:
        now = time.time()
        return [cls for cls in ROUTE[category]
                if self._cooldown_until.get(cls, 0) <= now]

    def _dispatch(self, category: DataCategory, symbol: str, **kwargs):
        chain = ROUTE[category]
        kwargs_key = "|".join(f"{k}:{v}" for k, v in sorted(kwargs.items()))
        cache_key = f"{chain[0].name}:{category.value}:{symbol}:{kwargs_key}"
        hit = self._cache.get(cache_key)
        if hit and hit[0] > time.time():
            self._record_usage(category, hit[2])      # 缓存命中仍记合规来源
            return hit[1]
        if category is DataCategory.KLINE:
            freq_now = kwargs.get("freq", "daily")
            disk_key = f"kline:{symbol}:{freq_now}:{kwargs.get('datalen', '120')}"
            disk = None if freq_now in _MINUTE_FREQS else _disk_get(disk_key)
            if disk is not None:
                value, source_name, disk_provenance = disk
                try:
                    # 仅日线走严格新鲜度：周/月线末根天然「陈旧」（月线末根可 >10 天）
                    if freq_now == "daily":
                        # #SA-20260830-02：陈旧当日缓存不返回，落回网络链重取
                        self._check_freshness(symbol, value, source_name)
                    # #SA-20260831-14：交易日已收盘后，K 线末行必须已更新到最近交易日，
                    # 否则视为缓存过期（当日早间缓存整天命中旧数据的问题）
                    if freq_now == "daily" and self._kline_cache_stale(value):
                        raise ProviderError("gateway", "kline",
                                            "disk cache data_date stale, refetching",
                                            retryable=False)
                except ProviderError:
                    _log.warning("disk cache stale (source=%s, symbol=%s), refetching",
                                 source_name, symbol)
                else:
                    source_cls = next((c for c in ROUTE[category] if c.name == source_name),
                                      chain[0])
                    self._cache_put(cache_key,
                                    (time.time() + TTL[category], value, source_cls))
                    self._record_usage(category, source_cls, provenance=disk_provenance)
                    return value
        elif category is DataCategory.FUNDAMENTAL:
            # S1 补完：财报落盘缓存（7 天）。财报不跟盘口走，没有「当日」概念，
            # 因此不做 _check_freshness / _kline_cache_stale（那是 K 线的新鲜度逻辑）。
            disk_key = f"fundamental:{symbol}:{kwargs.get('periods', 8)}"
            disk = _disk_get(disk_key, max_age_days=FUND_DISK_TTL_DAYS)
            if disk is not None:
                value, source_name, disk_provenance = disk
                source_cls = next((c for c in ROUTE[category] if c.name == source_name),
                                  chain[0])
                self._cache_put(cache_key,
                                (time.time() + TTL[category], value, source_cls))
                self._record_usage(category, source_cls, provenance=disk_provenance)
                return value
        elif category is DataCategory.CONSENSUS:
            # S4：一致预期落盘缓存（3 天）。前瞻预测不随盘中变动，
            # 不套 K 线的新鲜度判定。
            disk = _disk_get(f"consensus:{symbol}", max_age_days=CONSENSUS_DISK_TTL_DAYS)
            if disk is not None:
                value, source_name, disk_provenance = disk
                source_cls = next((c for c in ROUTE[category] if c.name == source_name),
                                  chain[0])
                self._cache_put(cache_key,
                                (time.time() + TTL[category], value, source_cls))
                self._record_usage(category, source_cls, provenance=disk_provenance)
                return value
        # 积分探针驱动裁剪：provider.supports() 不含本类别（如 tushare 无权限）则跳过；
        # supports() 内部有进程级缓存，仅 token 解析/首次探针会产生一次开销。
        freq = kwargs.get("freq", "daily") if category is DataCategory.KLINE else None
        available = [cls for cls in self._available_chain(category)
                     if category in cls.supports()
                     and (freq is None or freq in getattr(cls, "kline_freqs", {"daily"}))]
        if not available and freq and freq != "daily":
            # 整条链都未声明该周期能力（未改造的源默认只认 daily）：明确不可用，
            # 不套用「暂时不可用，请稍后重试」——那会暗示重试有意义。
            raise ProviderError("gateway", category.value,
                                f"周期 {freq} 暂无可用数据源", retryable=False)
        if not available:
            # #SA-20260830-01：全链耗尽给出可理解降级提示（而非 "no provider supports kline"）
            cooled = [c.name for c in ROUTE[category]
                      if self._cooldown_until.get(c, 0) > time.time()]
            raise ProviderError("gateway", category.value,
                                f"数据源暂时不可用（{'、'.join(cooled) or '无可用源'}），请稍后重试",
                                retryable=False)
        last_err = None
        for provider_cls in available:
            provider = self._instances[provider_cls]
            sem = _SEMS.get(provider_cls.name)
            try:
                if sem is not None:
                    sem.acquire()
                if not ratelimit.check_rate_limit(
                        f"stock_analysis:{provider_cls.name}:{category.value}",
                        limit=_PG_LIMIT.get(provider_cls.name, 60), window=60):
                    continue                            # 限流：换下一源，不计失败
                value = self._fetch(provider, category, symbol, **kwargs)
                value, prov_meta = _unwrap_result(value)
                freq_now = kwargs.get("freq", "daily")
                if category is DataCategory.KLINE and freq_now == "daily":
                    # #SA-20260830-02：陈旧数据视为本源不可用（不计成功、不冷却），触发 failover
                    self._check_freshness(symbol, value, provider_cls.name)
                self._fail_streak[provider_cls] = 0
                if category is DataCategory.KLINE and freq_now == "daily":
                    # _crosscheck 是「末行 vs quote」口径，对周/月线不成立
                    self._crosscheck(symbol, value)
                self._record_usage(category, provider_cls, provenance=prov_meta)
                if category is DataCategory.KLINE and freq_now not in _MINUTE_FREQS:
                    # 分钟线不落盘（见 _MINUTE_FREQS 注释），只写内存短缓存
                    _disk_put(f"kline:{symbol}:{freq_now}:{kwargs.get('datalen', '120')}",
                              value, provider_cls.name, provenance=prov_meta)
                elif category is DataCategory.FUNDAMENTAL:
                    _disk_put(f"fundamental:{symbol}:{kwargs.get('periods', 8)}",
                              value, provider_cls.name, provenance=prov_meta)
                elif category is DataCategory.CONSENSUS:
                    _disk_put(f"consensus:{symbol}", value, provider_cls.name,
                              provenance=prov_meta)
                self._cache_put(cache_key,
                                (time.time() + (MINUTE_CACHE_TTL if freq_now in _MINUTE_FREQS
                                                else TTL[category]), value, provider_cls))
                return value
            except (ProviderError, ProviderUnavailable, NotImplementedError) as err:
                # #SA-20260830-01：仅真实源故障（retryable=True）计入冷却，
                # 标的不存在/环境错误/数据陈旧等用户侧问题不摘除数据源
                if getattr(err, "retryable", True):
                    streak = self._fail_streak.get(provider_cls, 0) + 1
                    self._fail_streak[provider_cls] = streak
                    if streak >= COOLDOWN_FAIL_STREAK:      # 连续 N 败 → 冷却摘除
                        self._cooldown_until[provider_cls] = time.time() + COOLDOWN
                        _log.warning("provider %s cooldown %ss (%s)",
                                     provider_cls.name, COOLDOWN, err)
                last_err = err
            finally:
                if sem is not None:
                    sem.release()
        raise ProviderError("gateway", category.value, f"all sources failed: {last_err}")

    def _check_freshness(self, symbol: str, frame: pd.DataFrame, source: str):
        """#SA-20260830-02：K 线新鲜度校验。末行日期陈旧超过阈值即拒绝输出（failover）。

        以自然日计（MAX_STALE_DAYS=10），覆盖春节/国庆长假且不误伤正常周末/短假。
        陈旧数据不写缓存、不计冷却、不计成功，交由 failover 尝试下一源。
        """
        if frame is None or len(frame) == 0:
            return
        last = pd.Timestamp(frame.index[-1]).date()
        gap = (date.today() - last).days
        if gap > MAX_STALE_DAYS:
            raise ProviderError("gateway", "kline",
                                f"{source} 返回陈旧K线（last={last}，{gap}天前），已拒绝输出",
                                retryable=False)

    def _kline_cache_stale(self, frame: pd.DataFrame) -> bool:
        """落盘 K 线缓存是否过期（#SA-20260831-14）。

        仅当「今天是交易日且本地时间已收盘（≥15:00）」时，强制要求 K 线末行
        已更新到最近交易日；盘中/节假日沿用 gap 判定（允许上一交易日数据，
        避免每次请求都触发网络重取）。
        """
        if frame is None or len(frame) == 0:
            return False
        from . import market_calendar as cal
        last = pd.Timestamp(frame.index[-1]).date()
        today = date.today()
        if cal.is_trading_day(today) and datetime.now().hour >= 15:
            return last < cal.latest_trading_day()
        return False

    @staticmethod
    def _fetch(provider: BaseProvider, category: DataCategory, symbol: str, **kwargs):
        # v2 路径：provider 继承 BaseProviderV2 并实现 _do_fetch()
        if hasattr(provider, "_do_fetch"):
            code = symbol
            if category is DataCategory.KLINE:
                code = symbol if re.match(r"^(?:sh|sz|bj)", symbol.lower()) \
                    else market_symbol(symbol)
            elif category is DataCategory.NEWS:
                code = market_symbol(symbol)
            elif category in (DataCategory.FUNDAMENTAL, DataCategory.MONEYFLOW,
                              DataCategory.TOPLIST, DataCategory.MARGIN,
                              DataCategory.SHAREFLOAT, DataCategory.HOLDERNUMBER):
                code = market_symbol(symbol)
            elif category is DataCategory.NORTHBOUND:
                code = symbol
            elif category is DataCategory.INDEX:
                code = index_symbol(symbol)
            elif category in (DataCategory.CONSENSUS, DataCategory.PROFILE, DataCategory.FORECAST):
                code = symbol
            return provider.fetch(category, symbol=code, **kwargs)
        # v1 路径：逐个 fetch_* 方法
        if category is DataCategory.KLINE:
            # 已带交易所前缀的符号（如 sh000001 上证指数）保留原样，避免 market_symbol
            # 把指数错当股票段重判前缀；纯数字股票码才走 market_symbol 规范化
            code = symbol if re.match(r"^(?:sh|sz|bj)", symbol.lower()) \
                else market_symbol(symbol)
            return provider.fetch_kline(code, **kwargs)
        if category in (DataCategory.QUOTE, DataCategory.INDEX):
            return provider.fetch_quote(symbol if category is DataCategory.QUOTE
                                        else index_symbol(symbol))
        if category is DataCategory.NEWS:
            return provider.fetch_news(market_symbol(symbol))
        if category is DataCategory.FUNDAMENTAL:
            return provider.fetch_fundamental(market_symbol(symbol))
        if category is DataCategory.MONEYFLOW:
            return provider.fetch_moneyflow(market_symbol(symbol), **kwargs)
        raise NotImplementedError(category)

    def _record_usage(self, category: DataCategory, provider_cls: type,
                      provenance: dict = None):
        entry = {"category": category.value, "source": provider_cls.name,
                 "authorized": bool(provider_cls.authorized),
                 "fetched_at": time.time()}
        if provenance:
            entry["provenance_id"] = provenance.get("provenance_id", "")
            entry["as_of"] = provenance.get("as_of", "")
            entry["delay_seconds"] = provenance.get("delay_seconds", 0)
        if category is DataCategory.NEWS and provider_cls.name == "sina":
            entry["note"] = "非授权兜底源，新闻标题未经交叉校验"
        self._usage_entries().append(entry)

    def _crosscheck(self, symbol: str, frame: pd.DataFrame):
        """kline 收盘价与 quote 互验。

        #SA-20260830-02 修复：
        - 比较基准按 K 线末行日期选择：当日 K 线对齐 quote 现价（允许抓取时间差，>5% 才标冲突）；
          历史 K 线末行对齐 quote 昨收（应近乎一致，>0.1% 标冲突）——避免交易时段现价波动误报。
        - quote 侧故障一律旁路（return），不阻塞、不计入 K 线源失败；
          仅极端冲突（>20%）抛 retryable=False 触发 failover/明确失败。
        """
        # #SA-20260901-02：akshare hfq-only 降级帧（无不复权价）无法与 quote 互验，
        # 跳过 crosscheck，避免 hfq 价 vs 实时价必然 >20% 的假冲突。
        if "_raw_unavailable" in frame.columns:
            return
        try:
            q = self.get_quote(symbol)                  # 走 quote 链（tencent）
        except Exception:
            return                                      # quote 侧故障：旁路校验，不阻塞主链路
        close = float(frame["close"].iloc[-1])
        last = pd.Timestamp(frame.index[-1]).date()
        if last >= date.today():
            ref = q.get("price") or q.get("prev_close") or 0
            threshold = 0.05     # 当日K线 vs 实时价：容忍抓取时间差
        else:
            ref = q.get("prev_close") or q.get("price") or 0
            # #SA-20260831-09：历史K线末行对齐昨收，跨源（新浪K线 vs 腾讯昨收）存在
            # 复权口径/四舍五入差异（实测健康标的最大 0.6%），0.1% 阈值误报冲突；
            # 放宽至 1%，仍能拦截 430047 这类 200% 级真陈旧冲突
            threshold = 0.01
        if ref > 0 and abs(close - ref) / ref > threshold:
            diff = abs(close - ref) / ref
            tag = {"symbol": symbol, "kline_close": close, "ref": ref,
                   "diff_pct": round(diff, 5)}
            _conflict_log.append(tag)
            _conflict_log[:] = _conflict_log[-100:]
            # #SA-20260831-08：冲突留痕补齐 authorized/fetched_at 合规三元组，
            # 避免破坏 data_sources 每条须含 source/authorized/fetched_at 的约束
            self._usage_entries().append({"category": "kline", "source": "crosscheck",
                                          "authorized": False, "fetched_at": time.time(),
                                          "conflict": tag})
            _log.warning("crosscheck conflict: %s", tag)
            if diff > 0.20:                              # 极端冲突：数据不可信，阻断输出
                raise ProviderError("gateway", "kline",
                                    f"kline 与 quote 严重不符（{tag['diff_pct']:.1%}），数据不可信",
                                    retryable=False)


gateway = DataGateway()     # 模块级单例：stock_skill 各调用点直接 import 使用
