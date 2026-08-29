# providers/base.py — 数据类别枚举、结果载体、Provider 契约
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class DataCategory(str, Enum):
    KLINE = "kline"             # 历史日K（OHLCV）
    QUOTE = "quote"             # 实时快照
    INDEX = "index"             # 指数快照
    NEWS = "news"               # 个股新闻标题列表
    FUNDAMENTAL = "fundamental" # 深财报（阶段2，Tushare 启用后使用）
    MONEYFLOW = "moneyflow"     # 资金流/北向/龙虎榜（阶段2，Tushare 启用后使用）


class ProviderError(RuntimeError):
    """provider 抓取失败（网络/解析/空数据）。gateway 捕获后沿路由表 failover。"""

    def __init__(self, source: str, category: str, reason: str):
        super().__init__(f"[{source}/{category}] {reason}")
        self.source, self.category, self.reason = source, category, reason


@dataclass
class Meta:
    # 合规强制项：每个数据点必须携带来源三元组
    source: str
    authorized: bool
    fetched_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"source": self.source, "authorized": self.authorized,
                "fetched_at": self.fetched_at}


class BaseProvider:
    """类契约：子类声明 supports() 与实现 fetch_*；未支持的类别直接 raise NotImplementedError。"""
    name: str = "base"
    authorized: bool = False

    @classmethod
    def supports(cls) -> set:
        # 返回本 provider 供给的数据类别集合，gateway 据此校验路由表合法性
        raise NotImplementedError

    def _meta(self) -> dict:
        return Meta(source=self.name, authorized=self.authorized).to_dict()
