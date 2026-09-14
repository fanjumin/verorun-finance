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

try:
    from .providers.base_v2 import FetchResult, ProviderUnavailable
except ImportError:
    FetchResult = None

    class ProviderUnavailable(RuntimeError):
        pass

_log = logging.getLogger("stock_analysis.gateway")

# ── 阶段2 路由表：tushare 授权主源（积分探针无权限类别自动裁剪）→ 备源 failover ──
# 终端桥（Wind/Choice）挂链尾：DATA_PROVIDER=wind|choice 偏好时经
# apply_preference 前置；桥未启动时连接失败计入冷却，不影响默认主源链路。
ROUTE: dict = {
    DataCategory.KLINE: [TushareProvider, AkshareProvider, SinaProvider, PolygonProvider,
                         WindProvider, ChoiceProvider],
    DataCategory.FUNDAMENTAL: [TushareProvider, FMPProvider, UserSuppliedProvider],
    DataCategory.MONEYFLOW: [TushareProvider],
    DataCategory.NEWS: [SinaProvider, PolygonProvider],
    DataCategory.QUOTE: [TencentProvider, SinaProvider, PolygonProvider, WindProvider, ChoiceProvider],
    DataCategory.INDEX: [TencentProvider, WindProvider, ChoiceProvider],
    DataCategory.CONSENSUS: [FMPProvider, TushareProvider, UserSuppliedProvider],
    DataCategory.PROFILE: [FMPProvider, PolygonProvider],
    DataCategory.FORECAST: [FMPProvider, UserSuppliedProvider],
    DataCategory.TOPLIST: [TushareProvider],
    DataCategory.MARGIN: [TushareProvider],
    DataCategory.NORTHBOUND: [TushareProvider],
    DataCategory.SHAREFLOAT: [TushareProvider],
    DataCategory.HOLDERNUMBER: [TushareProvider],
}
TTL = {DataCategory.KLINE: 300, DataCategory.QUOTE: 60, DataCategory.INDEX: 60,
       DataCategory.NEWS: 600, DataCategory.FUNDAMENTAL: 300, DataCategory.MONEYFLOW: 300,
       DataCategory.CONSENSUS: 3600, DataCategory.PROFILE: 86400, DataCategory.FORECAST: 3600,
       DataCategory.TOPLIST: 600, DataCategory.MARGIN: 300, DataCategory.NORTHBOUND: 300,
       DataCategory.SHAREFLOAT: 86400, DataCategory.HOLDERNUMBER: 86400}
COOLDOWN = 300                                   # 连续失败摘除时长（秒）
# #SA-20260830-02：K 线数据新鲜度阈值（自然日）。10 天可覆盖春节/国庆长假且
# 不误伤正常周末/短假；陈旧超过该阈值的数据一律拒绝输出（failover 下一源）。
MAX_STALE_DAYS = 10
# P2-11：进程内存结果缓存必须有界。写入超限时先逐过期项；仍超限则按最接近过期者逐出。
MAX_MEM_CACHE = 1024


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


def _disk_get(key: str):
    path = _disk_path(key)
    if os.path.exists(path):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                payload = json.load(f)
            if payload["day"] == time.strftime("%Y-%m-%d"):   # 当日有效
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


class DataGateway:
    def __init__(self):
        self._instances = {cls: cls() for chain in ROUTE.values() for cls in chain}
        self._cache: dict[str, tuple[float, object]] = {}
        self._fail_streak: dict[type, int] = {}
        self._cooldown_until: dict[type, float] = {}
        # F3：_usage 线程局部——模块级单例在 batch 多线程下共享，若用普通列表，
        # 并发请求的 data_sources 归属会串数据（合规输出事故）。threading.local
        # 使每个请求线程只看到自己的数据点记录。
        self._usage: threading.local = threading.local()

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
    def get_kline(self, symbol: str, datalen: int = 120) -> pd.DataFrame:
        return self._dispatch(DataCategory.KLINE, symbol, datalen=datalen)

    def get_quote(self, symbol: str, category: DataCategory = DataCategory.QUOTE) -> dict:
        return self._dispatch(category, symbol)

    def get_news(self, symbol: str) -> list:
        return self._dispatch(DataCategory.NEWS, symbol)

    def get_fundamental(self, symbol: str) -> dict:
        """深财报四表合一（Tushare income/balance/cashflow/fina_indicator）。无权限/无 token 抛 ProviderError。"""
        return self._dispatch(DataCategory.FUNDAMENTAL, symbol)

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
            disk_key = f"kline:{symbol}:{kwargs.get('datalen', '120')}"
            disk = _disk_get(disk_key)
            if disk is not None:
                value, source_name, disk_provenance = disk
                try:
                    # #SA-20260830-02：陈旧当日缓存不返回，落回网络链重取
                    self._check_freshness(symbol, value, source_name)
                    # #SA-20260831-14：交易日已收盘后，K 线末行必须已更新到最近交易日，
                    # 否则视为缓存过期（当日早间缓存整天命中旧数据的问题）
                    if self._kline_cache_stale(value):
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
        # 积分探针驱动裁剪：provider.supports() 不含本类别（如 tushare 无权限）则跳过；
        # supports() 内部有进程级缓存，仅 token 解析/首次探针会产生一次开销。
        available = [cls for cls in self._available_chain(category)
                     if category in cls.supports()]
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
                if category is DataCategory.KLINE:
                    # #SA-20260830-02：陈旧数据视为本源不可用（不计成功、不冷却），触发 failover
                    self._check_freshness(symbol, value, provider_cls.name)
                self._fail_streak[provider_cls] = 0
                if category is DataCategory.KLINE:
                    self._crosscheck(symbol, value)
                self._record_usage(category, provider_cls, provenance=prov_meta)
                if category is DataCategory.KLINE:
                    _disk_put(f"kline:{symbol}:{kwargs.get('datalen', '120')}",
                              value, provider_cls.name, provenance=prov_meta)
                self._cache_put(cache_key,
                                (time.time() + TTL[category], value, provider_cls))
                return value
            except (ProviderError, ProviderUnavailable, NotImplementedError) as err:
                # #SA-20260830-01：仅真实源故障（retryable=True）计入冷却，
                # 标的不存在/环境错误/数据陈旧等用户侧问题不摘除数据源
                if getattr(err, "retryable", True):
                    streak = self._fail_streak.get(provider_cls, 0) + 1
                    self._fail_streak[provider_cls] = streak
                    if streak >= 3:                         # 连续3败 → 冷却摘除
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
