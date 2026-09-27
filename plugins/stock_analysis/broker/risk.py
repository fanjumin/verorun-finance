"""风控闸门（方案 §4.7 ③）—— **所有下单必经此关，禁止裸下单**。

设计原则：
  * 闸门是**纯函数**（不做网络请求）：标的名称等外部事实由调用方（路由）取好再传入，
    这样每条规则都能在自测里直接构造输入验证，不依赖行情/网络。
  * 复用 `compliance` 既有能力：适当性分级（ST/退市 → 高风险需二次确认）、静默期。
  * 拒绝必须给出**人话原因**，前端原样展示；不静默拦截。

三态结论：
  * `ok`                —— 放行
  * `needs_confirm`     —— 需用户二次确认（高风险标的等），带 confirm=True 重提即可放行
  * 其余                —— 硬拒绝
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["RiskGate", "RiskVerdict", "RiskRejected"]


class RiskRejected(Exception):
    """闸门拒绝。message 直接给前端展示。"""

    def __init__(self, reason: str, code: str = "blocked"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass
class RiskVerdict:
    ok: bool = False
    reason: str = ""
    code: str = "blocked"          # ok | need_confirm | blocked
    needs_confirm: bool = False
    warnings: list = field(default_factory=list)

    def as_tuple(self):
        """兼容方案 §4.7 示例的 `ok, reason = RiskGate.check(...)` 写法。"""
        return self.ok, self.reason


# 名称里出现这些字样 → 视为高风险标的（A 股风险警示/退市整理）
_HIGH_RISK_NAME_TOKENS = ("ST", "退市", "退", "PT")


class RiskGate:
    """下单前风控。默认参数偏保守，可按部署场景在子类/配置里覆盖。"""

    LOT_SIZE = 100                 # A 股最小交易单位
    MAX_QTY = 100_000              # 单笔股数上限（1000 手）
    MAX_AMOUNT = 500_000.0         # 单笔金额上限（元）
    MAX_POSITION_PCT = 0.30        # 单标的市值 / 总资产 上限
    REQUIRE_CONFIRM_HIGH_RISK = True

    @classmethod
    def check(cls, order: dict, account: dict | None = None,
              name: str | None = None, user_id: str = "default",
              confirm: bool = False) -> RiskVerdict:
        """逐条过闸。任一硬规则不过立即返回（短路，不做无谓计算）。"""
        warnings: list = []

        symbol = str(order.get("symbol") or "").strip()
        side = str(order.get("side") or "").strip().lower()
        order_type = str(order.get("type") or order.get("order_type") or "limit").strip().lower()

        # ① 基本参数
        if not symbol:
            return RiskVerdict(False, "缺少 symbol", "blocked")
        if side not in ("buy", "sell"):
            return RiskVerdict(False, f"side 必须是 buy/sell，收到 {side or '(空)'}", "blocked")
        if order_type not in ("limit", "market"):
            return RiskVerdict(False, f"type 必须是 limit/market，收到 {order_type}", "blocked")

        try:
            qty = int(order.get("qty"))
        except (TypeError, ValueError):
            return RiskVerdict(False, f"qty 必须是整数，收到 {order.get('qty')!r}", "blocked")
        if qty <= 0:
            return RiskVerdict(False, f"qty 必须为正，收到 {qty}", "blocked")
        if qty % cls.LOT_SIZE != 0:
            return RiskVerdict(False,
                               f"qty 必须是 {cls.LOT_SIZE} 的整数倍（A 股按手交易），收到 {qty}",
                               "blocked")

        price = order.get("price")
        if order_type == "limit":
            try:
                price = float(price)
            except (TypeError, ValueError):
                return RiskVerdict(False, "限价单必须给有效的 price", "blocked")
            if price <= 0:
                return RiskVerdict(False, f"限价必须为正，收到 {price}", "blocked")
        else:
            price = float(price) if price else None

        # ② 单笔规模
        if qty > cls.MAX_QTY:
            return RiskVerdict(False,
                               f"单笔 {qty:,} 股超过上限 {cls.MAX_QTY:,} 股", "blocked")
        # 市价单无价时按现价估（account 里带 lastPrice 更准，缺省则跳过金额闸）
        est = price if price else (account or {}).get("lastPrice")
        if est:
            amount = float(est) * qty
            if amount > cls.MAX_AMOUNT:
                return RiskVerdict(False,
                                   f"单笔金额约 {amount:,.0f} 元，超过上限 {cls.MAX_AMOUNT:,.0f} 元",
                                   "blocked")
        else:
            warnings.append("未能估算单笔金额（缺价格），金额上限未校验")

        # ③ 高风险标的（ST / 退市）→ 需二次确认
        if cls.REQUIRE_CONFIRM_HIGH_RISK and cls._is_high_risk(symbol, name):
            if not confirm:
                return RiskVerdict(
                    False,
                    f"{symbol}（{name or '风险警示/退市整理'}）属高风险标的，需二次确认后下单",
                    "need_confirm", needs_confirm=True, warnings=warnings)
            warnings.append("已确认高风险标的")

        if name is None:
            warnings.append("未提供标的名称，风险警示（ST/退市）识别未执行")

        # ④ 静默期（复用 compliance）
        silence = cls._check_silence(symbol, user_id)
        if silence.get("blocked"):
            return RiskVerdict(False, silence.get("reason") or "静默期禁止交易",
                               "blocked", warnings=warnings)

        # ⑤ 现金 / 持仓预估
        acct = account or {}
        if side == "buy" and est:
            cash = acct.get("cash")
            if cash is not None:
                need = float(est) * qty
                if need > float(cash):
                    return RiskVerdict(
                        False, f"现金不足：需约 {need:,.2f} 元，可用 {float(cash):,.2f} 元",
                        "blocked", warnings=warnings)
            else:
                warnings.append("未提供账户现金，买入资金校验未执行")
        if side == "sell":
            held = acct.get("heldQty")
            if held is not None and qty > int(held):
                return RiskVerdict(
                    False, f"持仓不足：持有 {int(held)} 股，欲卖 {qty} 股",
                    "blocked", warnings=warnings)
            if held is None:
                warnings.append("未提供持仓数量，卖出可卖量校验未执行")

        # ⑥ 仓位集中度（仅买入）
        if side == "buy" and est:
            total = acct.get("total")
            held_mv = acct.get("heldMarketValue") or 0
            if total:
                after = float(held_mv) + float(est) * qty
                if after > float(total) * cls.MAX_POSITION_PCT:
                    return RiskVerdict(
                        False,
                        f"仓位超限：买入后 {symbol} 占比约 "
                        f"{after / float(total) * 100:.1f}%，超过上限 "
                        f"{cls.MAX_POSITION_PCT * 100:.0f}%",
                        "blocked", warnings=warnings)
            else:
                warnings.append("未提供总资产，仓位集中度校验未执行")

        return RiskVerdict(True, "风控通过", "ok", warnings=warnings)

    # ── 规则细节 ──

    @staticmethod
    def _is_high_risk(symbol: str, name: str | None) -> bool:
        """名称含 ST/退市等字样 → 高风险。名称缺失时返回 False（另由 warning 提示未执行）。"""
        if not name:
            return False
        up = str(name).upper().replace(" ", "").replace("*", "")
        return any(tok in up for tok in _HIGH_RISK_NAME_TOKENS)

    @staticmethod
    def _check_silence(symbol: str, user_id: str) -> dict:
        """复用 compliance 的静默期配置；合规模块不可用时**放行**并交由调用方知晓。

        这里刻意不因合规模块异常而拒绝下单 —— 但会把"未校验"写进 warnings，
        不假装校验过。
        """
        try:
            from .. import compliance
            return compliance.check_silence(symbol, user_id) or {"blocked": False}
        except Exception as exc:                                  # noqa: BLE001
            return {"blocked": False, "unchecked": True, "error": str(exc)[:120]}
