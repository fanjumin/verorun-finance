"""providers/terminal_provider.py — 本地金融终端桥接（Wind / Choice）。

架构：桌面端 Electron 内运行 loopback HTTP 桥（terminal-bridge.ts，端口默认
18090），Wind/Choice 均安装在本机（WindPy / Choice 量化接口）。Python 侧
provider 是桥的薄 HTTP 客户端，把 Wind/Choice 实时数据接入 gateway failover
链——本地终端数据最实时，用户可通过 DATA_PROVIDER=wind|choice 置顶为头源。

数据能力（v1 范围）：
  - KLINE / INDEX：历史序列 wsd(open,high,low,close,volume,amt)
  - QUOTE：快照 wsq(rt_last, rt_*)；快照不可用时回退最近两日 wsd 收盘价计算

桥接口信封（与 desktop 端 terminal-bridge.ts 对齐）：
  POST /api/terminal/fetch {"terminal":"wind|choice","kind":"history|snapshot",
       "codes":str,"fields":str,"start_date":str,"end_date":str}
  → {"ok":true,"data":{...}} 或 {"ok":false,"error":str}
  GET  /api/terminal/health → {ok,data:{status,terminals:{wind,choice},...}}
"""
from __future__ import annotations

import re
import threading
import time

import pandas as pd

from .base import DataCategory, ProviderError
from .base_v2 import BaseProviderV2, FetchResult

_BRIDGE_HOST = "127.0.0.1"
_BRIDGE_PORT = 18090
_BRIDGE_TIMEOUT = 20.0
_HISTORY_FIELDS = "open,high,low,close,volume,amt"
_SNAPSHOT_FIELDS = "rt_open,rt_high,rt_low,rt_last,rt_vol,rt_amt,rt_pct_chg,rt_prev_close"

# 桥不可达探测负缓存：5 秒内不再重复 connect（每请求都失败会拖慢全链 failover）
_avail_lock = threading.Lock()
_avail_cache = {"ok": False, "ts": 0.0}
_NEG_CACHE_SECS = 5.0

_DATE_COLS = ("date", "time", "day", "datetime", "trade_date", "index", "Date")


def _to_terminal_code(symbol: str) -> str:
    """sh600519 → 600519.SH；纯数字按交易所规则推断；指数 sh000300 → 000300.SH。"""
    s = str(symbol).strip().lower()
    m = re.match(r"^(sh|sz|bj)(\d{6})$", s)
    if m:
        ex, num = m.group(1), m.group(2)
        return "%s.%s" % (num, ex.upper())
    if re.match(r"^\d{6}$", s):
        head = s[0]
        if head in ("5", "6", "9"):
            return s + ".SH"
        if head in ("4", "8"):
            return s + ".BJ"
        return s + ".SZ"
    if re.match(r"^\d+$", s):          # 指数等裸代码（如 000001 被当指数传）
        return s + ".SH"
    return symbol


class _BridgeClient:
    """桥 HTTP 客户端：健康探测 + 统一 fetch，带负缓存。"""

    def __init__(self, source: str, terminal: str):
        self.source = source
        self.terminal = terminal

    def available(self) -> bool:
        global _avail_cache
        now = time.time()
        with _avail_lock:
            if _avail_cache["ok"] and now - _avail_cache["ts"] < 10.0:
                return True
            if not _avail_cache["ok"] and now - _avail_cache["ts"] < _NEG_CACHE_SECS:
                return False
            ok = self._ping()
            _avail_cache = {"ok": ok, "ts": now}
            return ok

    def _ping(self) -> bool:
        try:
            import requests
            r = requests.get("http://%s:%d/api/terminal/health"
                             % (_BRIDGE_HOST, _BRIDGE_PORT), timeout=2.0)
            if r.status_code != 200:
                return False
            payload = r.json()
            data = payload.get("data") or {}
            terminals = data.get("terminals") or {}
            # 检测表非空时按表判定；为空（旧桥版本）则宽松放行
            return bool(terminals.get(self.terminal)) if terminals else True
        except Exception:
            return False

    def fetch(self, kind: str, codes: str, fields: str,
              start_date: str = "", end_date: str = "") -> dict:
        import requests
        payload = {
            "terminal": self.terminal,
            "kind": kind,
            "codes": codes,
            "fields": fields,
            "start_date": start_date,
            "end_date": end_date,
        }
        r = requests.post(
            "http://%s:%d/api/terminal/fetch" % (_BRIDGE_HOST, _BRIDGE_PORT),
            json=payload, timeout=_BRIDGE_TIMEOUT)
        r.raise_for_status()
        return r.json()


class TerminalBridgeProvider(BaseProviderV2):
    """桥接 provider 公共逻辑；子类只需声明 terminal 名与 bridge 检测字段。"""

    market: str = "CN"
    categories: frozenset = frozenset({DataCategory.KLINE, DataCategory.QUOTE,
                                       DataCategory.INDEX})
    rate_per_min: int = 600
    burst: int = 30
    required_secret: None = None        # 本地桥无云端凭据
    authorized: bool = True             # 终端已授权数据以本机为凭
    terminal: str = "wind"

    def __init__(self, secrets=None, session=None):
        super().__init__(secrets, session)
        self._bridge = _BridgeClient(self.name, self.terminal)

    # ---- 工具 ----

    @staticmethod
    def _parse_hist(payload: dict, datalen: int = 120) -> pd.DataFrame:
        """桥 history 响应 → 标准 K 线 DataFrame（date 索引，open/high/low/close/volume/amount）。

        兼容两种行布局：rows=[{列:值}]（WindPy usedf 记录）或
        rows=[{date, ...}]+columns（Choice 适配器列布局），用"可解析为日期的列"
        识别日期列，避免依赖列命名。
        """
        data = payload.get("data") or {}
        records = data.get("rows") if isinstance(data, dict) else None
        if records is None and isinstance(payload.get("data"), list):
            records = payload["data"]
        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records)
        date_col = None
        for col in _DATE_COLS:
            if col in df.columns:
                cand = pd.to_datetime(df[col], errors="coerce", format="mixed")
                if cand.notna().sum() >= max(1, len(df) // 2):
                    date_col, parsed = col, cand
                    break
        if date_col is None:
            for col in df.columns:
                cand = pd.to_datetime(df[col], errors="coerce", format="mixed")
                if cand.notna().sum() >= max(1, len(df) // 2):
                    date_col, parsed = col, cand
                    break
        if date_col is None:
            return pd.DataFrame()
        df = df.copy()
        df["date"] = parsed
        df = df.dropna(subset=["date"])
        rename = {c: c.lower() for c in df.columns}
        df = df.rename(columns=rename)
        if "amt" in df.columns and "amount" not in df.columns:
            df = df.rename(columns={"amt": "amount"})
        for c in ("open", "high", "low", "close", "volume", "amount"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.set_index("date").sort_index()
        if {"open", "high", "low", "close"}.issubset(df.columns):
            df = df.dropna(subset=["open", "high", "low", "close"])
        for c in ("open", "high", "low", "close"):
            df[c + "_hfq"] = df[c]
        df["price_basis"] = "raw"
        return df.tail(datalen)

    def _fetch_history(self, code: str, datalen: int = 120,
                       category: DataCategory = DataCategory.KLINE) -> pd.DataFrame:
        if not self._bridge.available():
            raise ProviderError(self.name, category.value,
                                "本地终端桥不可达（请确认终端已登录且桥已启动）")
        end = pd.Timestamp.now().strftime("%Y-%m-%d")
        start = (pd.Timestamp.now() - pd.Timedelta(days=datalen * 2.2 + 90)).strftime("%Y-%m-%d")
        try:
            payload = self._bridge.fetch("history", code, _HISTORY_FIELDS, start, end)
        except Exception as err:
            raise ProviderError(self.name, category.value,
                                "桥请求失败: %s" % type(err).__name__, retryable=True) from err
        if not payload.get("ok"):
            raise ProviderError(self.name, category.value,
                                payload.get("error") or "终端返回错误", retryable=True)
        frame = self._parse_hist(payload, datalen)
        if frame.empty:
            raise ProviderError(self.name, category.value,
                                "终端无历史数据（可能非交易代码）", retryable=False)
        return frame

    def _fetch_quote(self, code: str) -> dict:
        """实时快照；wsq 失败（字段/权限）回退最近两日 wsd 收盘价计算。"""
        if not self._bridge.available():
            raise ProviderError(self.name, "quote", "本地终端桥不可达")
        quote = {}
        try:
            payload = self._bridge.fetch("snapshot", code, _SNAPSHOT_FIELDS)
            if payload.get("ok"):
                data = payload["data"]
                if isinstance(data, dict) and data.get("quote"):
                    quote = {k.lower(): v for k, v in data["quote"].items()}
        except Exception:
            quote = {}
        if quote.get("rt_last") is not None:
            try:
                last = float(quote["rt_last"])
            except (TypeError, ValueError):
                last = 0.0
            prev_close = quote.get("rt_prev_close")
            pct = quote.get("rt_pct_chg")
            try:
                prev_close = float(prev_close) if prev_close is not None else 0.0
                pct = float(pct) if pct is not None else 0.0
            except (TypeError, ValueError):
                prev_close, pct = 0.0, 0.0
            if (prev_close <= 0) and last > 0 and pct != 0:
                prev_close = last / (1 + pct / 100.0)
            if (pct == 0 or pct is None) and prev_close > 0:
                pct = (last - prev_close) / prev_close * 100.0
            return {"name": quote.get("rt_name", ""), "price": last,
                    "prev_close": prev_close, "change_pct": round(pct, 2),
                    "pe_ttm": 0.0, "pb": 0.0, "turnover_rate": 0.0,
                    "bridge": "snapshot"}
        # 回退：最近两个交易日收盘
        end = pd.Timestamp.now().strftime("%Y-%m-%d")
        start = (pd.Timestamp.now() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        payload = self._bridge.fetch("history", code, "close", start, end)
        if not payload.get("ok"):
            raise ProviderError(self.name, "quote",
                                payload.get("error") or "终端快照与回退均失败", retryable=True)
        frame = self._parse_hist(payload, datalen=2)
        if len(frame) < 2:
            raise ProviderError(self.name, "quote", "终端回退数据不足", retryable=False)
        prev_close = float(frame["close"].iloc[-2])
        last = float(frame["close"].iloc[-1])
        return {"name": "", "price": last, "prev_close": prev_close,
                "change_pct": round((last - prev_close) / prev_close * 100.0, 2)
                if prev_close else 0.0,
                "pe_ttm": 0.0, "pb": 0.0, "turnover_rate": 0.0,
                "bridge": "eod-fallback"}

    # ---- BaseProviderV2 契约 ----

    def _do_fetch(self, cat: DataCategory, *, symbol=None, **kw) -> FetchResult:
        cat = DataCategory(cat)
        code = _to_terminal_code(str(symbol or ""))
        if cat is DataCategory.KLINE:
            frame = self._fetch_history(code, datalen=int(kw.get("datalen", 120)), category=cat)
            return FetchResult(category=cat, data=frame, source=self.name,
                               as_of=time.strftime("%Y-%m-%dT%H:%M:%S"))
        if cat is DataCategory.INDEX:
            frame = self._fetch_history(code, datalen=int(kw.get("datalen", 120)), category=cat)
            return FetchResult(category=cat, data=frame, source=self.name,
                               as_of=time.strftime("%Y-%m-%dT%H:%M:%S"))
        if cat is DataCategory.QUOTE:
            quote = self._fetch_quote(code)
            return FetchResult(category=cat, data=quote, source=self.name,
                               as_of=time.strftime("%Y-%m-%dT%H:%M:%S"))
        raise ProviderError(self.name, cat.value,
                            "本地终端暂不支持该类别", retryable=False)

    def health(self) -> dict:
        base = super().health()
        base["bridge"] = "http://%s:%d" % (_BRIDGE_HOST, _BRIDGE_PORT)
        base["bridge_online"] = self._bridge.available()
        try:
            import requests
            r = requests.get("http://%s:%d/api/terminal/health"
                             % (_BRIDGE_HOST, _BRIDGE_PORT), timeout=2.0)
            data = r.json().get("data") or {}
            base["terminals"] = (data.get("terminals") or {})
            base["bridge_status"] = data.get("status")
            base["wind_enabled"] = data.get("wind_enabled")
            base["choice_enabled"] = data.get("choice_enabled")
        except Exception:
            pass
        return base


class WindProvider(TerminalBridgeProvider):
    """Wind 终端桥（WindPy w.wsd/w.wsq）。本地须安装 Wind 且已登录。"""

    name: str = "wind"
    terminal: str = "wind"


class ChoiceProvider(TerminalBridgeProvider):
    """Choice 量化终端桥（东方财富本地接口，经桥 HTTP 转发）。"""

    name: str = "choice"
    terminal: str = "choice"


if __name__ == "__main__":  # pragma: no cover
    import sys
    w = WindProvider()
    print(w.health())
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    print("KLINE head:")
    print(w._fetch_history(_to_terminal_code(code), datalen=10).head(3))
    print("QUOTE:", w._fetch_quote(_to_terminal_code(code)))
