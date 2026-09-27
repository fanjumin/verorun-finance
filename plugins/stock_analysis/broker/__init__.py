"""经纪商适配层（方案 §4.7）—— 仿真优先、实盘 fail-closed。

★ 合规边界（务必保持）：
  * v1 **唯一可用的通道是仿真**（`PaperAdapter`，`LIVE=False`），不涉真实资金。
  * 本包**不提供任何可用的实盘适配器**。实盘（QMT/同花顺等）属可选插件，
    需同时满足：用户自备经纪商资质 + 显式开关 + 法务评审通过后才能接入。
  * 因此 `get_adapter(live=True)` 一律 **fail-closed**（抛 `AdapterUnavailable`），
    而不是"悄悄退化成仿真"—— 沉默降级会让用户误以为自己在真钱交易。
"""

from __future__ import annotations

import os

from .adapter import (AdapterError, AdapterUnavailable, BrokerAdapter,
                      ORDER_STATUS, ORDER_TYPES, SIDES)
from .paper import PaperAdapter
from .risk import RiskGate, RiskRejected

__all__ = ["BrokerAdapter", "PaperAdapter", "RiskGate", "RiskRejected",
           "AdapterError", "AdapterUnavailable", "SIDES", "ORDER_TYPES",
           "ORDER_STATUS", "get_adapter", "LIVE_AVAILABLE"]


#: 本包是否含可用的实盘适配器。**当前恒为 False** —— 接入实盘时改这里，
#: 并同步在 README/CHANGELOG 记录法务评审结论。
LIVE_AVAILABLE = False

#: 实盘开关环境变量。即便置 1，没有 LIVE_AVAILABLE 的实现仍然拒绝启用。
LIVE_ENV = "VR_TRADE_LIVE"


def get_adapter(user_id: str = "default", live: bool | None = None) -> BrokerAdapter:
    """取适配器。默认仿真；实盘请求在无可用实现时直接拒绝。

    live=None 时看环境变量 `VR_TRADE_LIVE`，但**环境变量不能凭空变出实盘实现**。
    """
    if live is None:
        live = os.environ.get(LIVE_ENV, "").strip().lower() in ("1", "true", "yes", "on")
    if not live:
        return PaperAdapter(user_id)
    if not LIVE_AVAILABLE:
        raise AdapterUnavailable(
            "实盘通道未启用：当前仅提供仿真账户（不涉真实资金）。"
            "实盘适配器属可选插件，需自备经纪商资质 + 法务评审后方可接入"
            f"（方案 §4.7 合规后置；{LIVE_ENV} 已置位但无可用实现）")
    raise AdapterUnavailable("实盘通道未启用：无已注册的实盘适配器")
