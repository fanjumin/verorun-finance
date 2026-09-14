"""
providers/user_supplied.py — 用户自供数据 Provider（CSV / Excel ingest）

选型理由
--------
- 专业用户常有自有数据（卖方模型、另类数据、内部预测），需要一条标准化通道
  注入 stock_analysis 的 schema，供估值 / 因子 / 回测消费。
- 支持 CSV 与 Excel（.xlsx），通过字段映射自动对齐 canonical 字段。
- 无需凭据，无需网络；数据来自用户上传文件。

落地步骤
--------
1. 放到 providers/user_supplied.py
2. gateway.ROUTE 按需追加：
       DataCategory.FUNDAMENTAL: [..., UserSuppliedProvider]
       DataCategory.CONSENSUS:   [..., UserSuppliedProvider]
3. 前端"导入数据"页面调用 ingest() 方法，传入 file_path + category + symbol
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pandas as pd

try:
    from .base_v2 import (BaseProviderV2, DataCategory, FetchResult,
                          ProviderUnavailable, SecretResolver)
except ImportError:
    from base_v2 import (BaseProviderV2, DataCategory, FetchResult,
                         ProviderUnavailable, SecretResolver)

DEFAULT_KLINE_COLS = {"date": "date", "open": "open", "high": "high",
                      "low": "low", "close": "close", "volume": "vol"}

DEFAULT_FIN_COLS = {"revenue": "revenue", "net_income": "net_profit",
                    "total_assets": "total_assets", "total_equity": "equity",
                    "operating_cash_flow": "ocf", "total_debt": "total_debt",
                    "eps": "eps", "roe": "roe", "roa": "roa",
                    "gross_margin": "gross_margin", "net_margin": "net_margin"}


class UserSuppliedProvider(BaseProviderV2):
    name = "user_supplied"
    market = "GLOBAL"
    required_secret = None
    rate_per_min = 9999
    burst = 9999
    categories = frozenset({
        DataCategory.KLINE, DataCategory.FUNDAMENTAL, DataCategory.CONSENSUS,
        DataCategory.QUOTE, DataCategory.FORECAST,
    })

    def __init__(self, secrets=None, session=None):
        super().__init__(secrets=secrets, session=session)
        self._store: Dict[str, Dict[str, Any]] = {}

    def _do_fetch(self, cat: DataCategory, *, symbol: Optional[str] = None, **kw) -> FetchResult:
        sym = (symbol or "").split(":")[-1].upper()
        if not sym:
            raise ProviderUnavailable("UserSupplied 需要 symbol")
        key = f"{sym}:{cat.value}"
        entry = self._store.get(key)
        if entry is None:
            raise ProviderUnavailable(f"UserSupplied: 未找到 {sym} 的 {cat.value} 数据（请先导入）")
        return FetchResult(cat, entry["data"], "user_supplied",
                           as_of=entry.get("as_of", ""),
                           delay_seconds=0,
                           url="",
                           params={"symbol": sym, "source_file": entry.get("source_file", "")},
                           cost_units=0.0)

    def ingest(self, cat: DataCategory, symbol: str, file_path: str,
               col_map: Optional[Dict[str, str]] = None,
               date_col: str = "date", **kw) -> str:
        sym = symbol.upper()
        if not os.path.isfile(file_path):
            raise ProviderUnavailable(f"文件不存在: {file_path}")

        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".csv":
            df = pd.read_csv(file_path)
        elif ext in (".xlsx", ".xls"):
            df = pd.read_excel(file_path)
        else:
            raise ProviderUnavailable(f"不支持的文件格式: {ext}（仅支持 .csv / .xlsx / .xls）")

        cat = DataCategory(cat)

        if cat == DataCategory.KLINE:
            mapping = col_map or DEFAULT_KLINE_COLS
            df = df.rename(columns=mapping)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date").sort_index()
            for c in ("open", "high", "low", "close"):
                if c in df.columns:
                    df[c] = df[c].astype(float)
            if "vol" in df.columns:
                df["vol"] = pd.to_numeric(df["vol"], errors="coerce")
            as_of = str(df.index.max()) if len(df) > 0 else ""

        elif cat == DataCategory.FUNDAMENTAL:
            mapping = col_map or DEFAULT_FIN_COLS
            df = df.rename(columns=mapping)
            as_of = ""
            if date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col])
                as_of = str(df[date_col].max())

        elif cat == DataCategory.CONSENSUS:
            mapping = col_map or {"date": "date", "eps_estimate": "eps_est",
                                  "revenue_estimate": "revenue_est"}
            df = df.rename(columns=mapping)
            as_of = ""
            if date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col])
                as_of = str(df[date_col].max())

        elif cat == DataCategory.FORECAST:
            mapping = col_map or {}
            df = df.rename(columns=mapping)
            as_of = ""

        elif cat == DataCategory.QUOTE:
            mapping = col_map or {}
            df = df.rename(columns=mapping)
            as_of = ""

        else:
            raise ProviderUnavailable(f"UserSupplied 不支持 {cat.value}")

        key = f"{sym}:{cat.value}"
        data = df if cat == DataCategory.KLINE else df.to_dict("records")
        self._store[key] = {"data": data, "as_of": as_of, "source_file": file_path}
        return key

    def list_imported(self) -> List[Dict[str, str]]:
        return [{"symbol": k.split(":")[0], "category": k.split(":")[1],
                 "as_of": v["as_of"], "source_file": v.get("source_file", "")}
                for k, v in self._store.items()]

    def remove(self, symbol: str, cat: DataCategory) -> bool:
        key = f"{symbol.upper()}:{DataCategory(cat).value}"
        return self._store.pop(key, None) is not None

    def health(self) -> dict:
        base = super().health()
        base.update({"ok": True, "imported_count": len(self._store)})
        return base


if __name__ == "__main__":
    p = UserSuppliedProvider()
    print("health:", p.health())
    print("imported:", p.list_imported())
