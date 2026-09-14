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
    CONSENSUS = "consensus"     # 分析师一致预期（FMP / Tiingo）
    PROFILE = "profile"         # 公司概况（FMP / Polygon）
    FORECAST = "forecast"       # 预测/模型输入（用户自供 / FMP）
    TOPLIST = "toplist"         # 龙虎榜（A 股特色）
    MARGIN = "margin"           # 融资融券（A 股特色）
    NORTHBOUND = "northbound"   # 北向资金（A 股特色）
    SHAREFLOAT = "sharefloat"   # 限售股解禁（A 股特色）
    HOLDERNUMBER = "holdernumber"  # 股东户数（A 股特色）


class ProviderError(RuntimeError):
    """provider 抓取失败（网络/解析/空数据）。gateway 捕获后沿路由表 failover。

    retryable=True  真实数据源故障（网络/接口异常），计入冷却摘除。
    retryable=False 标的不存在/环境性错误/数据陈旧，不计入冷却（#SA-20260830-01）。
    """

    def __init__(self, source: str, category: str, reason: str, retryable: bool = True):
        super().__init__(f"[{source}/{category}] {reason}")
        self.source, self.category, self.reason = source, category, reason
        self.retryable = retryable


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
