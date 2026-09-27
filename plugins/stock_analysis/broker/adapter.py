"""经纪商适配接口（方案 §4.7 ①）—— 策略模式。

设计约束（合规）：
  * v1 **只提供仿真实现**（`PaperAdapter`），`LIVE=True` 的适配器一律不随包启用；
    实盘适配器（QMT/同花顺）属可选插件，需用户自备经纪商资质 + 显式开关 + 法务评审。
  * 本模块只定义接口与统一返回结构，**不含任何真实资金通道代码**。

统一订单返回结构（对齐真实经纪商字段，前端不区分仿真/实盘渲染差异）：
    {
      "orderId": str, "symbol": str, "side": "buy"|"sell",
      "qty": int, "price": float|None, "type": "limit"|"market",
      "status": "filled"|"rejected"|"pending"|"cancelled",
      "filledQty": int, "filledPrice": float|None,
      "amount": float, "fee": float, "ts": str(ISO),
      "paper": bool,        # True = 仿真，UI 必须打仿真角标
      "message": str
    }
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

__all__ = ["BrokerAdapter", "AdapterUnavailable", "AdapterError",
           "SIDES", "ORDER_TYPES", "ORDER_STATUS"]


SIDES = ("buy", "sell")
ORDER_TYPES = ("limit", "market")
ORDER_STATUS = ("filled", "rejected", "pending", "cancelled")


class AdapterError(RuntimeError):
    """适配器业务失败（下单被拒、查询失败等）。message 直接给前端展示。"""


class AdapterUnavailable(RuntimeError):
    """适配器不可用（未启用 / 缺资质 / 缺依赖）。

    与 AdapterError 的区别：这是「能力不存在」而非「这次操作失败」，
    路由层应回 503 而不是 400/403，前端据此提示"该通道未启用"。
    """


class BrokerAdapter(ABC):
    """经纪商适配器基类。

    子类必须声明：
      name    —— 适配器标识（tencent 风格小写，用于日志与前端展示）
      LIVE    —— 是否真实资金。**v1 唯一实现为 False（仿真）**。
    """

    name: str = "abstract"
    LIVE: bool = False

    # ---- 必须实现 ----

    @abstractmethod
    def place_order(self, symbol: str, side: str, qty: int,
                    price: float | None = None,
                    order_type: str = "limit") -> dict:
        """下单。返回统一订单结构（见模块 docstring）。

        实现类**不得**自行做风控：风控由 `RiskGate` 在路由层统一把关，
        保证"禁止裸下单"在单一位置成立（改一处即全局生效）。
        """

    @abstractmethod
    def positions(self) -> list[dict]:
        """当前持仓。字段：symbol/qty/avgCost/lastPrice/marketValue/pnl/pnlPct。"""

    @abstractmethod
    def orders(self, status: str | None = None, limit: int = 100) -> list[dict]:
        """订单列表，按时间倒序（新→旧）。status 为 None 时不筛选。"""

    # ---- 可选能力：默认不支持，子类按需覆盖 ----

    def cancel(self, order_id: str) -> dict:
        """撤单。默认不支持（仿真即时成交，无需撤单）。"""
        raise AdapterError(f"{self.name} 不支持撤单")

    def account(self) -> dict:
        """账户概览：cash / marketValue / total / pnl。默认由持仓推导。"""
        pos = self.positions()
        mv = sum(float(p.get("marketValue") or 0) for p in pos)
        pnl = sum(float(p.get("pnl") or 0) for p in pos)
        return {"cash": None, "marketValue": round(mv, 2),
                "total": None, "pnl": round(pnl, 2),
                "adapter": self.name, "live": self.LIVE}

    # ---- 工具 ----

    @staticmethod
    def _norm_symbol(symbol: str) -> str:
        """统一成 6 位纯数字代码（'sh600519' / 'CN:600519' / '600519.SH' → '600519'）。"""
        s = (symbol or "").strip().upper()
        for prefix in ("SH", "SZ", "BJ"):
            if s.startswith(prefix) and len(s) > 2 and s[2:].isdigit():
                s = s[2:]
                break
        if ":" in s:
            s = s.split(":", 1)[1]
        if s.endswith((".SH", ".SZ", ".BJ")):
            s = s.rsplit(".", 1)[0]
        if s.startswith(("SH", "SZ", "BJ")) and len(s) == 8 and s[2:].isdigit():
            s = s[2:]
        return s

    def describe(self) -> dict[str, Any]:
        """给前端/审计用：当前通道是不是真钱。"""
        return {"adapter": self.name, "live": self.LIVE,
                "paper": not self.LIVE,
                "disclaimer": ("" if self.LIVE else "仿真账户：不涉真实资金，成交为模拟撮合")}
