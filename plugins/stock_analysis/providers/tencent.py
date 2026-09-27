# providers/tencent.py — 腾讯：实时快照/指数（免费稳定，公开行情）
# 逐字平移自 stock_skill._get_latest_price（v1.3.0），字段下标保持不变
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

from .base import DataCategory, ProviderError
from .base_v2 import BaseProviderV2, FetchResult, egress_get
from .commons import market_symbol as commons_market_symbol


class TencentProvider(BaseProviderV2):
    name = "tencent"
    authorized = False
    market = "CN"
    categories = frozenset({DataCategory.QUOTE, DataCategory.INDEX,
                            DataCategory.DEPTH,      # 方案 §4.2：快照里本就带买卖五档
                            DataCategory.TICKS})     # 方案 §4.3：逐笔明细接口
    rate_per_min = 120
    burst = 20

    def supports_category(self, cat: DataCategory, symbol: Optional[str] = None) -> bool:
        """境外指数（usDJI/hkHSI/jpN225 等）由腾讯快照接口直接支持（实测 73~78 字段，
        字段布局与 A 股一致），但 secmaster 会把它们归到 US/HK 市场，与本 provider 的
        CN 市场裁剪冲突而被拒。这里对带境外前缀的 INDEX 符号显式放行。"""
        if DataCategory(cat) not in self.categories:
            return False
        if symbol and cat is DataCategory.INDEX:
            low = str(symbol).lower()
            from .commons import FOREIGN_INDEX_PREFIXES
            if low.startswith(FOREIGN_INDEX_PREFIXES):
                return True
        return super().supports_category(cat, symbol)

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if cat is DataCategory.QUOTE:
            data = self._fetch_quote(symbol)
        elif cat is DataCategory.INDEX:
            from .commons import index_symbol
            data = self._fetch_quote(index_symbol(symbol))
        elif cat is DataCategory.DEPTH:
            data = self._fetch_depth(symbol)
        elif cat is DataCategory.TICKS:
            data = self._fetch_ticks(symbol, limit=int(kw.get("limit", 50) or 50))
        else:
            raise NotImplementedError(cat)
        return FetchResult(category=cat, data=data, source=self.name,
                           as_of=time.strftime("%Y-%m-%dT%H:%M:%S"))

    # ---- 公共：快照原始字段 ----

    @staticmethod
    def _market_symbol(symbol: str) -> str:
        s = str(symbol or "").lower()
        if s.startswith(("sh", "sz", "bj")):
            return s
        # 境外指数（us/hk/jp/kr/gb/uk 前缀，如 usDJI / hkHSI / jpN225）：
        # 腾讯行情对境外指数大小写敏感（实测 usdji 返回 v_pv_none_match），
        # 必须保留「小写前缀 + 大写代码」规范形态，不能落入 A 股裸码逻辑（会误判为 szXXX）。
        from .commons import FOREIGN_INDEX_PREFIXES
        for prefix in FOREIGN_INDEX_PREFIXES:
            if s.startswith(prefix):
                raw = str(symbol or "")
                return prefix + raw[len(prefix):].upper()
        return commons_market_symbol(s)

    def _snapshot_parts(self, symbol: str) -> list:
        """拉一次 qt.gtimg.cn 快照并返回 `~` 分隔字段（88 项）。QUOTE 与 DEPTH 共用。"""
        market_symbol = self._market_symbol(symbol)
        for attempt in range(2):
            try:
                # 2026-09-22：改走 egress_get —— 由 net_proxy 规则决定出网通道，
                # 内置降级直连，net_proxy 不可用时行为与改造前一致。
                response = egress_get(
                    "https://qt.gtimg.cn/q=" + market_symbol,
                    caller="stock_analysis.tencent", timeout=5,
                    usage_tags=("market",))
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 1:
                    raise ProviderError(self.name, "quote", "request failed")
                time.sleep(0.5)
        # 名称是 GBK；不显式指定会按 ISO-8859-1 解出乱码（实测已修正）
        response.encoding = "gbk"
        parts = response.text.strip().strip(";").split("~")
        if len(parts) < 40:
            raise ProviderError(self.name, "quote", "实时行情返回数据不完整")
        return parts

    def _fetch_quote(self, symbol: str) -> dict:
        parts = self._snapshot_parts(symbol)

        # 腾讯 ~ 分隔字段位（实测 88 字段，v_sh600519）：
        #   [1]名称 [3]现价 [4]昨收 [5]今开 [6]成交量(手) [30]时间戳
        #   [31]涨跌额 [32]涨跌幅% [33]最高 [34]最低 [37]成交额(万元)
        #   [38]换手率% [39]PE(TTM) [43]振幅% [44]流通市值(亿) [45]总市值(亿) [46]PB
        # 历史缺陷：只取 7 个字段，open/high/low/volume/amount 从未填充 →
        # 前端 quotes 这些列恒为 null（K线页与行情条缺开高低量）。
        def _f(idx: int) -> float:
            if idx >= len(parts):
                return 0.0
            try:
                return float(parts[idx] or 0)
            except (TypeError, ValueError):
                return 0.0

        return {"name": parts[1],
                "price": _f(3),
                "prev_close": _f(4),
                "open": _f(5),
                "high": _f(33),
                "low": _f(34),
                "volume": _f(6),
                # 腾讯成交额单位为万元 → 统一为元，与其他 provider 对齐
                "amount": _f(37) * 10000,
                "change_pct": _f(32),
                "pe_ttm": _f(39),
                "pb": _f(46),
                "turnover_rate": _f(38)}

    # ---- 五档盘口（方案 §4.2）----

    def _fetch_depth(self, symbol: str) -> dict:
        """买卖五档：腾讯快照 88 字段里本就带着，之前只是没解析。

        实测字段位（sh600519，2026-09-17 09:25）：
          [9..18]  买五档（价,量）×5，**买一在前**：
                   [9]买一价 [10]买一量 … [17]买五价 [18]买五量（价格递减）
          [19..28] 卖五档（价,量）×5，**卖一在前**：
                   [19]卖一价 [20]卖一量 … [27]卖五价 [28]卖五量（价格递增）
        量单位为**手**。非交易时段仍返回挂单，只是量可能为 0/1，属正常不是错误。
        """
        parts = self._snapshot_parts(symbol)

        def _pair(price_idx: int, vol_idx: int):
            try:
                price = float(parts[price_idx] or 0)
                volume = int(float(parts[vol_idx] or 0))
            except (IndexError, TypeError, ValueError):
                return None
            if price <= 0:
                return None
            return {"price": price, "volume": volume}

        buy, sell = [], []
        for i in range(5):
            b = _pair(9 + i * 2, 10 + i * 2)
            if b:
                b["level"] = i + 1
                buy.append(b)
            s = _pair(19 + i * 2, 20 + i * 2)
            if s:
                s["level"] = i + 1
                sell.append(s)
        if not buy and not sell:
            raise ProviderError(self.name, "depth", "快照未返回买卖档位", retryable=False)

        as_of = ""
        if len(parts) > 30 and re.match(r"^\d{14}$", parts[30] or ""):
            ts = parts[30]
            as_of = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}"
        bid = max((b["price"] for b in buy), default=None)
        ask = min((s["price"] for s in sell), default=None)
        return {
            "name": parts[1],
            "price": self._f(parts, 3),
            "prev_close": self._f(parts, 4),
            "buy": buy,                       # 买一…买五（价格降序）
            "sell": sell,                     # 卖一…卖五（价格升序）
            "spread": round(ask - bid, 4) if (bid and ask) else None,
            "asOf": as_of,
        }

    # ---- 分笔成交（方案 §4.3）----

    # 末页位置缓存：{market_symbol: (page, ts)}。页按时间升序，末页才是最新成交
    _tick_tail: dict = {}
    _TICK_TAIL_TTL = 300.0   # 秒：期间内从缓存页线性向后找，稳态只多 1 次请求
    _TICK_PROBE_WORKERS = 8  # 末页定位并发度（冷启动 2~3 个 RTT）

    def _tick_page(self, market_symbol: str, page: int) -> list:
        """取第 page 页的原始记录（已按 '|' 切成多条）；空页 / 越界返回 []。

        实测响应（2026-09-17 09:57，sh600519，p=0）：
            v_detail_data_sh600519=[0,"0/09:25:02/1257.98/0.00/140/17611720/S|1/09:30:02/.../S|..."]
        ★ 关键：**记录之间是 `|` 分隔，字段之间是 `/` 分隔**。每页固定 ~70 条。
          （第一版实现按整串 split('/') 只取前 7 段 → 每页只解出第 1 条，实测只返回 1 行。）
        """
        url = ("https://stock.gtimg.cn/data/index.php?appn=detail&action=data"
               f"&c={market_symbol}&p={page}")
        try:
            # 2026-09-22：改走 egress_get（同上，软接入 + 降级直连）
            resp = egress_get(url, caller="stock_analysis.tencent", timeout=5,
                              usage_tags=("market",))
            resp.raise_for_status()
            resp.encoding = "gbk"
        except requests.RequestException as err:
            raise ProviderError(self.name, "ticks", "request failed") from err
        body = re.search(r"=\[(.*)\]", resp.text or "", re.S)
        if not body:
            return []
        out = []
        for chunk in re.findall(r'"([^"]+)"', body.group(1)):
            out.extend([s for s in chunk.split("|") if s.strip()])
        return out

    def _probe_pages(self, market_symbol: str, pages: list) -> dict:
        """并发探测若干页号是否非空 → {page: bool}。

        网络异常按"空"处理：宁可少取一页（拿到次新数据），也不要把整个请求打挂。
        """
        out = {}
        if not pages:
            return out
        workers = max(1, min(self._TICK_PROBE_WORKERS, len(pages)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self._tick_page, market_symbol, p): p for p in pages}
            for fut, p in futs.items():
                try:
                    out[p] = bool(fut.result())
                except Exception:                       # noqa: BLE001 —— 探测失败视为空
                    out[p] = False
        return out

    def _tick_tail_page(self, market_symbol: str) -> int:
        """定位最后一个非空页号（= 最新成交所在页）。

        页按时间**升序**：p=0 是开盘最早（实测 p=0 首条 09:25:02，p=1 首条 seq=70）。
        所以"最新 N 笔"必须翻到末页，直接取 p=0 只会拿到开盘那几笔。

        冷启动：并发指数探测上界 → 并发 k 分收缩区间，**2~3 个 RTT**（串行二分实测 11.7s，
        改为并发后压到 ~1s）；命中缓存（5 分钟内）从上次末页向后线性 ≤6 页，稳态 1~2 次请求。
        返回 -1 表示今日无成交（未开盘）。
        """
        now = time.time()
        cached = self._tick_tail.get(market_symbol)
        if cached and now - cached[1] < self._TICK_TAIL_TTL:
            page = cached[0]
            # 缓存页自身仍非空才算命中；新交易日/上游重置后该页会变空 → 丢弃缓存走冷启动
            if self._tick_page(market_symbol, page):
                hits = self._probe_pages(market_symbol, [page + i for i in range(1, 7)])
                for i in range(1, 7):
                    if not hits.get(page + i):
                        break
                    page += 1
                self._tick_tail[market_symbol] = (page, now)
                return page
            self._tick_tail.pop(market_symbol, None)

        if not self._tick_page(market_symbol, 0):
            return -1

        # ① 并发指数探测：一次 RTT 拿到上界（lo 非空 / hi 空）
        probe = [1, 2, 4, 8, 16, 32, 64, 128]
        hits = self._probe_pages(market_symbol, probe)
        lo, hi = 0, None
        for p in probe:
            if not hits.get(p):
                hi = p
                break
            lo = p
        if hi is None:                       # 128 页仍非空（超出探测能力）→ 取已知最大
            self._tick_tail[market_symbol] = (lo, now)
            return lo

        # ② 并发 k 分收缩 (lo, hi)：每轮等距抽样 8 点，区间缩到 1/8
        while hi - lo > 1:
            span = list(range(lo + 1, hi))
            step = max(1, -(-len(span) // self._TICK_PROBE_WORKERS))   # ceil
            cand = span[::step]
            hits = self._probe_pages(market_symbol, cand)
            new_lo, new_hi = lo, hi
            for p in cand:
                if hits.get(p):
                    new_lo = p
                else:
                    new_hi = p
                    break
            if (new_lo, new_hi) == (lo, hi):      # 无进展（异常上游），防死循环
                break
            lo, hi = new_lo, new_hi
        self._tick_tail[market_symbol] = (lo, now)
        return lo

    def _fetch_ticks(self, symbol: str, limit: int = 50) -> dict:
        """逐笔明细（方案 §4.3）。返回**最新 limit 笔，新 → 旧**。

        行格式：序号/时间/价格/涨跌/成交量(手)/成交额(元)/性质
        性质：S=主动卖 B=主动买 M=中性（集合竞价常见）。
        ★ 按 '|' 切分后性质字段已是纯标志位；仍取一次 '|' 前首字母做容错 —— 未切分时
          该字段形如 "S|1"（后段疑似成交笔数），直接 == "S" 会漏判，主动卖被显示成中性「—」。
        """
        market_symbol = self._market_symbol(symbol)
        tail = self._tick_tail_page(market_symbol)
        if tail < 0:
            raise ProviderError(self.name, "ticks", "无逐笔数据（可能未开盘）", retryable=False)

        # 从末页往前取，凑够 limit（末页通常 ~70 条，一次就够）
        raws: list = []
        page, scanned = tail, 0
        while page >= 0 and len(raws) < limit and scanned < 8:
            raws = self._tick_page(market_symbol, page) + raws
            scanned += 1
            page -= 1

        rows = []
        for raw in raws:
            f = raw.split("/")
            if len(f) < 7:
                continue
            try:
                side_raw = (f[6] or "").strip().upper()
                side_head = side_raw.split("|")[0].strip()[:1]
                rows.append({
                    "seq": int(f[0]) if f[0].isdigit() else len(rows),
                    "time": f[1],
                    "price": float(f[2]),
                    "change": float(f[3] or 0),
                    "volume": int(float(f[4] or 0)),      # 手
                    "amount": float(f[5] or 0),           # 元
                    "side": side_raw,
                    # 前端方向：1=主动买(▲) -1=主动卖(▼) 0=中性
                    "direction": 1 if side_head == "B"
                    else -1 if side_head == "S" else 0,
                })
            except (TypeError, ValueError):
                continue
        if not rows:
            raise ProviderError(self.name, "ticks", "无逐笔数据（解析为空）", retryable=False)

        rows.reverse()                       # 上游升序（旧→新）→ 前端要最新在前
        rows = rows[:limit]
        return {"rows": rows,
                "newestFirst": True,
                "asOf": rows[0]["time"] if rows else None,
                "pagesScanned": scanned}

    @staticmethod
    def _f(parts: list, idx: int) -> float:
        if idx >= len(parts):
            return 0.0
        try:
            return float(parts[idx] or 0)
        except (TypeError, ValueError):
            return 0.0

    def fetch_quote(self, symbol: str) -> dict:
        return self._fetch_quote(symbol)
