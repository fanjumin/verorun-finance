"""仿真账户适配器（方案 §4.7 ②）—— **默认且 v1 唯一启用**的通道。

合规要点：
  * `LIVE=False`，不涉真实资金；成交是**用真实行情做的模拟撮合**（价格来自 gateway，
    不是随机数），所以报价与真实行情一致，但**资金与持仓完全是假账**。
  * 实盘适配器不得复用这三张表（sa_paper_*），避免仿真与真实混账。

撮合语义（刻意简化，且都写在返回的 message 里，不藏着）：
  * 市价单：按当前现价即时成交。
  * 限价单：买单价 ≥ 现价 / 卖单价 ≤ 现价 → **以现价**成交（优于限价）；
    否则 **rejected**（"仿真不排队"）。真实市场会挂单等待，这里不做队列，
    宁可明确拒绝，也不留下永远 pending 的僵尸单。
  * 费用只算佣金（万三，最低 5 元）。**未含印花税与过户费** —— 见 `fee` 注释，
    仿真结果因此会略优于真实成本。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from .adapter import AdapterError, BrokerAdapter

__all__ = ["PaperAdapter"]


class PaperAdapter(BrokerAdapter):
    """仿真撮合。所有账本落在 sa_paper_account / sa_paper_order / sa_paper_position。"""

    name = "paper"
    LIVE = False

    INITIAL_CASH = 1_000_000.0
    FEE_RATE = 0.0003      # 佣金万三
    FEE_MIN = 5.0          # 最低 5 元
    LOT_SIZE = 100         # A 股最小交易单位（手）

    def __init__(self, user_id: str = "default"):
        # user_id 隔离不同使用者的仿真账本；单人本地部署时恒为 'default'
        self.user_id = (user_id or "default").strip() or "default"
        self._ensure_schema()

    @staticmethod
    def _ensure_schema() -> None:
        """建表兜底。

        ★ 为什么需要：`models_sa.ensure_tables()` 平时只在 alert_engine / batch /
          jobs_queue 等路径被触发，交易路径**不保证**它跑过 —— 实测就出现过
          sa_paper_* 表建了、但后加的 `first_buy_date` 列没生效（缺少 ALTER）。
          ensure_tables 内部有进程内记忆 + 锁，重复调用只是一次字典判断，很便宜。
          失败不在这里 raise：让后续 SQL 报出真实错误，别用建表异常掩盖它。
        """
        try:
            from .. import models_sa as db
            db.ensure_tables()
        except Exception as exc:                                # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "paper schema ensure failed (will surface on first query): %s", exc)

    # ── 内部：账户 / 行情 ──

    def _cash(self) -> float:
        from .. import models_sa as db
        with db.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT cash FROM sa_paper_account WHERE user_id = %s",
                        (self.user_id,))
            row = cur.fetchone()
            if row:
                return float(row[0])
            # 首次使用：开一个初始资金账户
            cur.execute(
                "INSERT INTO sa_paper_account (user_id, cash, initial_cash) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (self.user_id, self.INITIAL_CASH, self.INITIAL_CASH))
            return self.INITIAL_CASH

    @staticmethod
    def _last_price(symbol: str) -> float:
        """取现价（真实行情，腾讯免 key）。失败直接抛，由路由层转 503/400。"""
        from ..gateway import gateway
        q = gateway.get_quote(symbol) or {}
        price = q.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            raise AdapterError(f"无法获取 {symbol} 当前价，仿真下单已中止")
        return price

    @staticmethod
    def _fee(amount: float) -> float:
        # 仅佣金。未含：印花税（卖出 0.05%）、过户费（沪市 0.001%）——
        # 仿真结果会比真实成本略好，这里如实标注，不假装精确。
        return max(round(amount * PaperAdapter.FEE_RATE, 2), PaperAdapter.FEE_MIN)

    # ── 账本运维（复位 / 夹具）──

    #: 仿真账本三张表（复位范围）。实盘适配器不得复用这些表（见模块 docstring）。
    LEDGER_TABLES = ("sa_paper_order", "sa_paper_position", "sa_paper_account")

    def reset(self, user_id: str | None = None) -> dict:
        """复位仿真账本（删除订单/持仓/账户行），使 `_cash()` 回到初始资金。

        归属说明：账本 schema 与不变量由本适配器所有，复位必须是**插件侧操作**。
        此前由 scripts/trade-reset.py 自行 psycopg2 拼 DELETE、broker-selftest
        自行清表（2026-09-21 架构整改），绕过适配器落库；现收敛到这里，
        并对外暴露 `POST /api/trade/reset`。

        user_id=None → 清空全部使用者的账本（本机单人部署即 default）；
        指定 user_id → 只清该使用者。返回 {表名: 删除行数}。
        """
        from .. import models_sa as db
        self._ensure_schema()
        target = (user_id or "").strip() or None
        purged: dict[str, int] = {}
        with db.get_db() as conn:
            cur = conn.cursor()
            for table in self.LEDGER_TABLES:
                if target:
                    cur.execute(f"DELETE FROM {table} WHERE user_id = %s", (target,))
                else:
                    cur.execute(f"DELETE FROM {table}")
                purged[table] = cur.rowcount if cur.rowcount > 0 else 0
        return purged

    def backdate_position(self, symbol: str, days: int = 1,
                          user_id: str | None = None) -> int:
        """把建仓日前移 N 天（解除 T+1 锁定）—— **自测夹具专用**，不参与业务路径。

        归属说明：T+1 语义由本适配器定义，夹具也必须经插件落库 ——
        此前 broker-selftest 直接 `UPDATE sa_paper_position SET first_buy_date`
        触碰账本表（2026-09-21 整改）。返回受影响行数。
        """
        from .. import models_sa as db
        target = (user_id or self.user_id).strip() or self.user_id
        with db.get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE sa_paper_position SET first_buy_date = %s "
                "WHERE user_id = %s AND symbol = %s",
                (date.today() - timedelta(days=max(0, int(days))), target,
                 self._norm_symbol(symbol)))
            return cur.rowcount if cur.rowcount > 0 else 0

    # ── 下单 ──

    def place_order(self, symbol: str, side: str, qty: int,
                    price: float | None = None,
                    order_type: str = "limit") -> dict:
        sym = self._norm_symbol(symbol)
        side = (side or "").strip().lower()
        order_type = (order_type or "limit").strip().lower()

        if side not in ("buy", "sell"):
            raise AdapterError(f"side 必须是 buy/sell，收到 {side!r}")
        if order_type not in ("limit", "market"):
            raise AdapterError(f"order_type 必须是 limit/market，收到 {order_type!r}")
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise AdapterError(f"qty 必须是整数，收到 {qty!r}")
        if qty <= 0:
            raise AdapterError(f"qty 必须为正，收到 {qty}")
        if qty % self.LOT_SIZE != 0:
            raise AdapterError(
                f"qty 必须是 {self.LOT_SIZE} 的整数倍（A 股按手交易），收到 {qty}")

        last = self._last_price(sym)
        # 撮合定价：限价不可达 → 拒绝（仿真不排队），市价 → 现价
        if order_type == "market":
            fill = last
        else:
            if price is None:
                raise AdapterError("限价单必须给 price")
            price = float(price)
            can_fill = price >= last if side == "buy" else price <= last
            if not can_fill:
                return self._reject(sym, side, qty, price, order_type,
                                    f"限价 {price:.2f} 相对现价 {last:.2f} 不可成交；"
                                    f"仿真模式不排队（实盘会挂单等待）")
            fill = last

        amount = round(fill * qty, 4)
        fee = self._fee(amount)

        from .. import models_sa as db
        with db.get_db() as conn:
            cur = conn.cursor()
            cash = self._cash_locked(cur)
            if side == "buy":
                need = amount + fee
                if need > cash:
                    return self._reject(sym, side, qty, fill, order_type,
                                        f"现金不足：需 {need:,.2f}，可用 {cash:,.2f}",
                                        cur)
                self._set_cash(cur, cash - need)
                self._apply_buy(cur, sym, qty, fill)
            else:
                held = self._position_qty(cur, sym)
                if held < qty:
                    return self._reject(sym, side, qty, fill, order_type,
                                        f"持仓不足：{sym} 持有 {held} 股，欲卖 {qty} 股",
                                        cur)
                # A 股 T+1：当日建仓的标的当日不可卖。
                # 简化：不做"分批可卖量"，只要 first_buy_date 是今天就整笔不可卖
                # （保守 —— 宁可少卖，也不伪造可卖数量）。
                cur.execute(
                    "SELECT first_buy_date FROM sa_paper_position "
                    "WHERE user_id = %s AND symbol = %s", (self.user_id, sym))
                row = cur.fetchone()
                fbd = row[0] if row else None
                if fbd and fbd >= date.today():
                    return self._reject(
                        sym, side, qty, fill, order_type,
                        f"T+1 限制：{sym} 于 {fbd.isoformat()} 建仓，当日不可卖出",
                        cur)
                self._set_cash(cur, cash + amount - fee)
                self._apply_sell(cur, sym, qty, fill)

            order = self._insert_order(cur, sym, side, qty, price, order_type,
                                       "filled", qty, fill, amount, fee,
                                       f"仿真成交 @ {fill:.2f}（现价撮合）")
        order["cashAfter"] = self._cash()
        return order

    # ── 持仓 / 订单 ──

    def positions(self) -> list[dict]:
        from .. import models_sa as db
        with db.get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT symbol, qty, avg_cost, first_buy_date "
                "FROM sa_paper_position WHERE user_id = %s AND qty > 0 ORDER BY symbol",
                (self.user_id,))
            rows = cur.fetchall()
        out = []
        for sym, qty, avg_cost, first_buy in rows:
            qty = int(qty); avg_cost = float(avg_cost or 0)
            try:
                last = self._last_price(sym)
            except AdapterError:
                last = avg_cost          # 行情取不到时退化为成本价，不编造涨跌幅
            mv = round(last * qty, 2)
            cost = round(avg_cost * qty, 2)
            out.append({
                "symbol": sym, "qty": qty, "avgCost": round(avg_cost, 4),
                "lastPrice": round(last, 4), "marketValue": mv,
                "cost": cost, "pnl": round(mv - cost, 2),
                "pnlPct": round((last / avg_cost - 1) * 100, 2) if avg_cost else None,
                "firstBuyDate": first_buy.isoformat() if first_buy else None,
                "tPlus1Locked": bool(first_buy and first_buy >= date.today()),
            })
        return out

    def orders(self, status: str | None = None, limit: int = 100) -> list[dict]:
        from .. import models_sa as db
        limit = max(1, min(int(limit or 100), 500))
        sql = ("SELECT order_id, symbol, side, qty, price, order_type, status, "
               "filled_qty, filled_price, amount, fee, reason, created_at "
               "FROM sa_paper_order WHERE user_id = %s")
        args: list = [self.user_id]
        if status:
            sql += " AND status = %s"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT %s"
        args.append(limit)
        with db.get_db() as conn:
            cur = conn.cursor()
            cur.execute(sql, tuple(args))
            rows = cur.fetchall()
        return [{
            "orderId": r[0], "symbol": r[1], "side": r[2], "qty": int(r[3]),
            "price": float(r[4]) if r[4] is not None else None,
            "type": r[5], "status": r[6],
            "filledQty": int(r[7] or 0),
            "filledPrice": float(r[8]) if r[8] is not None else None,
            "amount": float(r[9] or 0), "fee": float(r[10] or 0),
            "reason": r[11], "ts": r[12].isoformat() if r[12] else None,
            "paper": True,
        } for r in rows]

    def account(self) -> dict:
        pos = self.positions()
        mv = round(sum(p["marketValue"] for p in pos), 2)
        cash = round(self._cash(), 2)
        from .. import models_sa as db
        with db.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT initial_cash FROM sa_paper_account WHERE user_id = %s",
                        (self.user_id,))
            row = cur.fetchone()
        init = float(row[0]) if row else self.INITIAL_CASH
        total = round(cash + mv, 2)
        return {
            "cash": cash, "marketValue": mv, "total": total,
            "initialCash": round(init, 2),
            "pnl": round(total - init, 2),
            "pnlPct": round((total / init - 1) * 100, 2) if init else None,
            "adapter": self.name, "live": self.LIVE, "paper": True,
        }

    # ── 账本写入（都在调用方的事务里）──

    def _cash_locked(self, cur) -> float:
        cur.execute("SELECT cash FROM sa_paper_account WHERE user_id = %s FOR UPDATE",
                    (self.user_id,))
        row = cur.fetchone()
        if row:
            return float(row[0])
        cur.execute(
            "INSERT INTO sa_paper_account (user_id, cash, initial_cash) "
            "VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
            (self.user_id, self.INITIAL_CASH, self.INITIAL_CASH))
        return self.INITIAL_CASH

    def _set_cash(self, cur, cash: float) -> None:
        cur.execute(
            "UPDATE sa_paper_account SET cash = %s, updated_at = now() "
            "WHERE user_id = %s", (round(cash, 2), self.user_id))

    def _position_qty(self, cur, sym: str) -> int:
        cur.execute(
            "SELECT qty FROM sa_paper_position WHERE user_id = %s AND symbol = %s",
            (self.user_id, sym))
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def _apply_buy(self, cur, sym: str, qty: int, price: float) -> None:
        cur.execute(
            "SELECT qty, avg_cost, first_buy_date FROM sa_paper_position "
            "WHERE user_id = %s AND symbol = %s FOR UPDATE", (self.user_id, sym))
        row = cur.fetchone()
        if row:
            old_q, old_cost, first_buy = int(row[0]), float(row[1] or 0), row[2]
            new_q = old_q + qty
            new_cost = (old_cost * old_q + price * qty) / new_q
            cur.execute(
                "UPDATE sa_paper_position SET qty = %s, avg_cost = %s, "
                "first_buy_date = COALESCE(%s, first_buy_date), updated_at = now() "
                "WHERE user_id = %s AND symbol = %s",
                (new_q, round(new_cost, 4), first_buy, self.user_id, sym))
        else:
            cur.execute(
                "INSERT INTO sa_paper_position (user_id, symbol, qty, avg_cost, "
                "first_buy_date) VALUES (%s, %s, %s, %s, %s)",
                (self.user_id, sym, qty, round(price, 4), date.today()))

    def _apply_sell(self, cur, sym: str, qty: int, price: float) -> None:
        cur.execute(
            "SELECT qty, avg_cost FROM sa_paper_position "
            "WHERE user_id = %s AND symbol = %s FOR UPDATE", (self.user_id, sym))
        row = cur.fetchone()
        if not row:
            raise AdapterError(f"无 {sym} 持仓，无法卖出")
        old_q, avg_cost = int(row[0]), float(row[1] or 0)
        new_q = old_q - qty
        if new_q > 0:
            # 卖出不改变持仓成本（加权平均法）
            cur.execute(
                "UPDATE sa_paper_position SET qty = %s, updated_at = now() "
                "WHERE user_id = %s AND symbol = %s", (new_q, self.user_id, sym))
        else:
            cur.execute(
                "DELETE FROM sa_paper_position WHERE user_id = %s AND symbol = %s",
                (self.user_id, sym))

    def _insert_order(self, cur, sym, side, qty, price, order_type,
                      status, filled_qty, filled_price, amount, fee,
                      reason) -> dict:
        order_id = f"P-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
        cur.execute(
            "INSERT INTO sa_paper_order (order_id, user_id, symbol, side, qty, price, "
            "order_type, status, filled_qty, filled_price, amount, fee, reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (order_id, self.user_id, sym, side, qty, price, order_type, status,
             filled_qty, filled_price, amount, fee, reason))
        return {
            "orderId": order_id, "symbol": sym, "side": side, "qty": qty,
            "price": price, "type": order_type, "status": status,
            "filledQty": filled_qty, "filledPrice": filled_price,
            "amount": amount, "fee": fee, "message": reason,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "paper": True, "live": self.LIVE, "adapter": self.name,
        }

    def _reject(self, sym, side, qty, price, order_type, reason, cur=None) -> dict:
        """被拒单也要留痕（审计需要"谁在什么时候想干什么、为什么没干成"）。

        ★ 传了 cur 就在**同一事务**内写（拒单与资金操作原子生效，也避免嵌套借连接）；
          未传（在进入事务前就被拒）才自开一个短事务。
        """
        if cur is not None:
            return self._insert_order(cur, sym, side, qty, price, order_type,
                                      "rejected", 0, None, 0, 0, reason)
        from .. import models_sa as db
        with db.get_db() as conn:
            c = conn.cursor()
            return self._insert_order(c, sym, side, qty, price, order_type,
                                      "rejected", 0, None, 0, 0, reason)
