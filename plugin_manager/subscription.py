#!/usr/bin/env python3
"""
Plugin Manager — 订阅管理
===========================
管理插件订阅的创建、续费、取消、到期处理。

订阅类型:
  - month: 按月订阅，自动续费
  - quarter: 按季订阅，自动续费
  - year: 按年订阅，自动续费
"""

# ⚠️ DEPRECATED (auto-renew engine) — 本文件的自动续费调度入口已弃用。
# 主站自动续费链路当前由 auth-center/routes/subscription/renewal.py 承载
# （admin/app.py 每日调度 run_renewal_scan / run_dunning_scan）。
# 上线任务 T07/T11 要求：仅保留订阅 CRUD 能力，勿再启用 _auto_renew_task 引擎。

import json
import threading
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum

from .models import get_registry_db


class SubscriptionStatus(str, Enum):
    ACTIVE = 'active'           # 生效中
    CANCELED = 'canceled'       # 已取消（到期不再续费）
    EXPIRED = 'expired'         # 已过期
    SUSPENDED = 'suspended'     # 暂停（扣款失败）


# 到期后未续费的宽限期（天）。宽限期内 License 保留 active，
# 超过宽限期未续费则由 run_grace_lock_scan() 锁定为 expired。
GRACE_DAYS = 7


# ── DDL ───────────────────────────────────────────────────────────────

PLUGIN_SUBSCRIPTION_DDL = """
CREATE TABLE IF NOT EXISTS plugin_subscriptions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plugin_id       TEXT NOT NULL,
    license_key     TEXT NOT NULL,
    order_no        TEXT NOT NULL,
    interval_type   TEXT NOT NULL CHECK(interval_type IN ('month', 'quarter', 'year')),
    amount_fen      BIGINT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','canceled','expired','suspended')),
    tier            TEXT DEFAULT '',
    bundle_id       TEXT DEFAULT '',
    parent_sub_id   BIGINT,
    proration_fen   BIGINT DEFAULT 0,
    current_period_start TEXT,
    current_period_end   TEXT,
    auto_renew      BIGINT NOT NULL DEFAULT 1,
    retry_count     BIGINT NOT NULL DEFAULT 0,
    last_charge_at  TEXT,
    canceled_at     TEXT,
    created_at      TEXT DEFAULT NOW(),
    updated_at      TEXT DEFAULT NOW(),
    extra           TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_plugin_subs_plugin
    ON plugin_subscriptions(plugin_id);
CREATE INDEX IF NOT EXISTS idx_plugin_subs_status
    ON plugin_subscriptions(status);
"""

# 订阅事件审计表：生命周期变更落库（幂等键 sub_id+event_type+order_no）
SUBSCRIPTION_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS subscription_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sub_id      BIGINT,
    plugin_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,          -- created/renewed/canceled/expired/suspended/
                                        -- reactivated/upgraded/downgraded/bundle_created/refunded
    order_no    TEXT DEFAULT '',        -- 幂等键组成部分
    payload     TEXT DEFAULT '{}',
    created_at  TEXT DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sub_events_sub ON subscription_events(sub_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sub_events_idem
    ON subscription_events(sub_id, event_type, order_no);
"""

# 存量库幂等迁移（启动自动执行，重复执行无副作用）
PLUGIN_SUB_MIGRATIONS = [
    "ALTER TABLE plugin_subscriptions ADD COLUMN IF NOT EXISTS tier TEXT DEFAULT ''",
    "ALTER TABLE plugin_subscriptions ADD COLUMN IF NOT EXISTS bundle_id TEXT DEFAULT ''",
    "ALTER TABLE plugin_subscriptions ADD COLUMN IF NOT EXISTS parent_sub_id BIGINT",
    "ALTER TABLE plugin_subscriptions ADD COLUMN IF NOT EXISTS proration_fen BIGINT DEFAULT 0",
]

# interval_type 约束迁移：支持 quarter（存量行仅 month/year，DROP 后重建约束安全）
PLUGIN_SUB_CONSTRAINT_MIGRATION = [
    "ALTER TABLE plugin_subscriptions DROP CONSTRAINT IF EXISTS plugin_subscriptions_interval_type_check",
    "ALTER TABLE plugin_subscriptions ADD CONSTRAINT plugin_subscriptions_interval_type_check "
    "CHECK(interval_type IN ('month','quarter','year'))",
]


def init_subscription_tables():
    with get_registry_db() as conn:
        conn.executescript(PLUGIN_SUBSCRIPTION_DDL)
        conn.executescript(SUBSCRIPTION_EVENTS_DDL)
        for stmt in PLUGIN_SUB_MIGRATIONS + PLUGIN_SUB_CONSTRAINT_MIGRATION:
            conn.execute(stmt)
        conn.commit()


@dataclass
class PluginSubscription:
    plugin_id: str
    license_key: str
    order_no: str
    interval_type: str          # 'month' | 'quarter' | 'year'
    amount_fen: int
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    tier: str = ''
    bundle_id: str = ''
    parent_sub_id: Optional[int] = None
    proration_fen: int = 0
    current_period_start: Optional[str] = None
    current_period_end: Optional[str] = None
    auto_renew: bool = True
    retry_count: int = 0
    last_charge_at: Optional[str] = None
    canceled_at: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    id: Optional[int] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d['status'] = self.status.value
        return d

    @classmethod
    def from_row(cls, row: dict) -> 'PluginSubscription':
        return cls(
            id=row['id'],
            plugin_id=row['plugin_id'],
            license_key=row['license_key'],
            order_no=row['order_no'],
            interval_type=row['interval_type'],
            amount_fen=row['amount_fen'],
            status=SubscriptionStatus(row['status']),
            tier=row.get('tier', ''),
            bundle_id=row.get('bundle_id', ''),
            parent_sub_id=row.get('parent_sub_id'),
            proration_fen=row.get('proration_fen', 0),
            current_period_start=row.get('current_period_start'),
            current_period_end=row.get('current_period_end'),
            auto_renew=bool(row.get('auto_renew', 1)),
            retry_count=row.get('retry_count', 0),
            last_charge_at=row.get('last_charge_at'),
            canceled_at=row.get('canceled_at'),
            extra=json.loads(row.get('extra', '{}')),
            created_at=row.get('created_at'),
            updated_at=row.get('updated_at'),
        )


class SubscriptionManager:
    """订阅管理器"""

    def __init__(self):
        self._lock = threading.Lock()
        init_subscription_tables()

    # ── 创建订阅 ──────────────────────────────────────────────────

    def create(self, plugin_id: str, license_key: str, order_no: str,
               interval_type: str, amount_fen: int,
               tier: str = '', bundle_id: str = '') -> PluginSubscription:
        """购买成功后创建订阅记录"""
        now = datetime.now()
        period_end = self._calc_period_end(now, interval_type)

        sub = PluginSubscription(
            plugin_id=plugin_id,
            license_key=license_key,
            order_no=order_no,
            interval_type=interval_type,
            amount_fen=amount_fen,
            tier=tier,
            bundle_id=bundle_id,
            current_period_start=now.isoformat(),
            current_period_end=period_end.isoformat(),
        )

        with get_registry_db() as conn:
            cur = conn.execute("""
                INSERT INTO plugin_subscriptions
                    (plugin_id, license_key, order_no, interval_type,
                     amount_fen, tier, bundle_id,
                     current_period_start, current_period_end,
                     auto_renew, extra)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                sub.plugin_id, sub.license_key, sub.order_no,
                sub.interval_type, sub.amount_fen, sub.tier, sub.bundle_id,
                sub.current_period_start, sub.current_period_end,
                int(sub.auto_renew), json.dumps(sub.extra),
            ))
            conn.commit()
            sub.id = cur.fetchone()['id']

        self.record_event(sub.plugin_id, 'created', sub_id=sub.id, order_no=sub.order_no,
                          payload={'interval': sub.interval_type, 'amount_fen': sub.amount_fen,
                                   'tier': sub.tier, 'bundle_id': sub.bundle_id})
        return sub

    def record_event(self, plugin_id: str, event_type: str,
                     sub_id: Optional[int] = None, order_no: str = '',
                     payload: Optional[dict] = None) -> None:
        """记录订阅生命周期事件（审计）。

        幂等键 = (sub_id, event_type, order_no)：
          - 支付关联事件（created/renewed）携带真实订单号 → 防支付回调重复落库
          - 到期类事件（expired）以 `expired@{period_end}` 作订单键 → 每周期一条
          - 重复插入自动跳过（ON CONFLICT DO NOTHING），重跑扫描安全
        """
        with get_registry_db() as conn:
            conn.execute(
                "INSERT INTO subscription_events "
                "    (sub_id, plugin_id, event_type, order_no, payload) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (sub_id, event_type, order_no) DO NOTHING",
                (sub_id, plugin_id, event_type, order_no,
                 json.dumps(payload or {}, ensure_ascii=False))
            )
            conn.commit()

    # ── 取消订阅 ──────────────────────────────────────────────────

    def cancel(self, plugin_id: str, immediate: bool = False) -> bool:
        """取消订阅

        Args:
            plugin_id: 插件标识
            immediate: 是否立即取消（否则到期不再续费）

        Returns:
            bool: 是否成功
        """
        sub = self.get_subscription(plugin_id)
        if not sub:
            return False

        with self._lock:
            with get_registry_db() as conn:
                if immediate:
                    conn.execute("""
                        UPDATE plugin_subscriptions SET
                            status='expired', auto_renew=0,
                            canceled_at=NOW(),
                            updated_at=NOW()
                        WHERE plugin_id=%s
                    """, (plugin_id,))
                    # 同步标记 License 已过期
                    conn.execute("""
                        UPDATE plugin_licenses SET
                            license_status='expired',
                            updated_at=NOW()
                        WHERE plugin_id=%s
                    """, (plugin_id,))
                else:
                    conn.execute("""
                        UPDATE plugin_subscriptions SET
                            auto_renew=0, canceled_at=NOW(),
                            updated_at=NOW()
                        WHERE plugin_id=%s
                    """, (plugin_id,))
                conn.commit()

        # 审计事件：immediate → expired；否则 canceled（到期不再续费）
        self.record_event(
            plugin_id,
            'expired' if immediate else 'canceled',
            sub_id=sub.id,
            payload={'mode': 'immediate' if immediate else 'at_period_end'},
        )
        # 版本包：立即取消 → 成员 License 一并过期
        if immediate and sub.bundle_id:
            self._sync_bundle_members(sub.bundle_id, status='expired')
        return True

    # ── 续费处理 ──────────────────────────────────────────────────

    def renew(self, plugin_id: str) -> bool:
        """手动续费（延长一个周期）

        续费成功后：
          - 清除待支付续费订单标记（防止 _ensure_renewal_order 幂等误判已支付订单仍待支付）
          - 记录 last_renewed_at
          - 仅对仍处于 active 的 License 延长有效期（避免覆盖其他状态）
        """
        sub = self.get_subscription(plugin_id)
        if not sub:
            return False

        if sub.status != SubscriptionStatus.ACTIVE:
            return False

        now = datetime.now()
        current_end = datetime.fromisoformat(sub.current_period_end) if sub.current_period_end else now
        new_start = max(now, current_end)
        new_end = self._calc_period_end(new_start, sub.interval_type)

        extra = dict(sub.extra or {})
        renew_order_no = extra.get('pending_renew_order', '')
        extra.pop('pending_renew_order', None)
        extra['last_renewed_at'] = now.isoformat()

        with get_registry_db() as conn:
            conn.execute("""
                UPDATE plugin_subscriptions SET
                    current_period_start=%s,
                    current_period_end=%s,
                    last_charge_at=NOW(),
                    retry_count=0,
                    extra=%s,
                    updated_at=NOW()
                WHERE id=%s
            """, (new_start.isoformat(), new_end.isoformat(),
                  json.dumps(extra, ensure_ascii=False), sub.id))
            # 同步续期 License（仅限 active，避免覆盖其他状态）
            conn.execute("""
                UPDATE plugin_licenses SET
                    expires_at=%s,
                    license_status='active',
                    updated_at=NOW()
                WHERE plugin_id=%s AND license_status='active'
            """, (new_end.isoformat(), plugin_id))
            conn.commit()

        # 版本包：同步刷新成员 License 有效期
        if sub.bundle_id:
            self._sync_bundle_members(sub.bundle_id, new_end.isoformat())

        # 审计事件：以续费支付订单号作幂等键（防支付回调重复续费处理）
        self.record_event(
            plugin_id, 'renewed', sub_id=sub.id, order_no=renew_order_no,
            payload={'interval': sub.interval_type, 'amount_fen': sub.amount_fen,
                     'new_period_end': new_end.isoformat()},
        )
        return True

    def reactivate(self, plugin_id: str, interval_type: str = None,
                   amount_fen: int = None) -> bool:
        """补缴/重新购买后恢复订阅

        适用：订阅已 expired / suspended / canceled，用户完成支付后恢复。
        行为：
          - 订阅恢复 active + auto_renew=1，重新计算一个完整周期
          - 同步恢复 License 为 active 并刷新 expires_at
        """
        sub = self.get_subscription(plugin_id)
        if not sub:
            return False

        if interval_type:
            sub.interval_type = interval_type
        if amount_fen is not None:
            sub.amount_fen = amount_fen

        now = datetime.now()
        new_end = self._calc_period_end(now, sub.interval_type)

        extra = dict(sub.extra or {})
        renew_order_no = extra.get('pending_renew_order', '')
        extra.pop('pending_renew_order', None)
        extra.pop('grace_since', None)
        extra['last_renewed_at'] = now.isoformat()

        with get_registry_db() as conn:
            conn.execute("""
                UPDATE plugin_subscriptions SET
                    status='active',
                    auto_renew=1,
                    interval_type=%s,
                    amount_fen=%s,
                    current_period_start=%s,
                    current_period_end=%s,
                    last_charge_at=NOW(),
                    retry_count=0,
                    extra=%s,
                    updated_at=NOW()
                WHERE id=%s
            """, (sub.interval_type, sub.amount_fen,
                  now.isoformat(), new_end.isoformat(),
                  json.dumps(extra, ensure_ascii=False), sub.id))
            # 恢复 License（订阅恢复意味着用户已补缴，License 一并恢复）
            conn.execute("""
                UPDATE plugin_licenses SET
                    expires_at=%s,
                    license_status='active',
                    updated_at=NOW()
                WHERE plugin_id=%s
            """, (new_end.isoformat(), plugin_id))
            conn.commit()

        # 版本包：同步刷新成员 License 有效期
        if sub.bundle_id:
            self._sync_bundle_members(sub.bundle_id, new_end.isoformat())

        # 审计事件：订阅恢复（携带补缴/重新购买订单号作幂等键）
        self.record_event(
            plugin_id, 'reactivated', sub_id=sub.id, order_no=renew_order_no,
            payload={'interval': sub.interval_type, 'amount_fen': sub.amount_fen,
                     'new_period_end': new_end.isoformat()},
        )
        return True

    # ── 周期变更（升级/降级，带按比例折算） ─────────────────────────────

    _INTERVAL_RANK = {'month': 1, 'quarter': 2, 'year': 3}

    def quote_change(self, sub: PluginSubscription, new_interval: str,
                     new_price_fen: int) -> Dict[str, Any]:
        """周期变更报价：当前周期剩余价值折算抵扣新周期价（L5）。

        Args:
            sub: 当前订阅（需 active）
            new_interval: 目标周期（month/quarter/year）
            new_price_fen: 目标周期名义价（分，由路由按配置/L1 规则解析）

        Returns:
            {'from_interval', 'to_interval', 'price_fen', 'credit_fen',
             'pay_fen', 'carryover_fen', 'needs_payment'}
        """
        from .pricing import compute_upgrade_credit
        credit_fen = compute_upgrade_credit([sub.to_dict()])['credit_fen']
        pay = int(new_price_fen) - credit_fen
        return {
            'from_interval': sub.interval_type,
            'to_interval': new_interval,
            'price_fen': int(new_price_fen),
            'credit_fen': credit_fen,
            'pay_fen': max(pay, 0),
            'carryover_fen': max(-pay, 0),
            'needs_payment': pay > 0,
        }

    def change(self, plugin_id: str, new_interval: str, amount_fen: int,
               proration_fen: int = 0, order_no: str = '') -> Optional['PluginSubscription']:
        """执行周期变更（升级/降级）。

        语义：剩余价值折算抵扣已由调用方确认（proration_fen 来自
        quote_change 的 credit_fen，路由在支付确认后传入）；新周期自
        max(now, 当前周期末) 起算，同步刷新 License 有效期。

        Returns:
            更新后的订阅；订阅不存在/非 active/参数非法 → None
        """
        sub = self.get_subscription(plugin_id)
        if not sub or sub.status != SubscriptionStatus.ACTIVE:
            return None
        if new_interval not in self._INTERVAL_RANK:
            return None

        rank = self._INTERVAL_RANK
        old_rank = rank[sub.interval_type]
        new_rank = rank[new_interval]
        if new_rank > old_rank:
            event_type = 'upgraded'
        elif new_rank < old_rank:
            event_type = 'downgraded'
        else:
            event_type = 'changed'

        now = datetime.now()
        current_end = datetime.fromisoformat(sub.current_period_end) if sub.current_period_end else now
        new_start = max(now, current_end)
        new_end = self._calc_period_end(new_start, new_interval)

        extra = dict(sub.extra or {})
        extra['last_changed_at'] = now.isoformat()
        extra.pop('pending_renew_order', None)

        with get_registry_db() as conn:
            conn.execute("""
                UPDATE plugin_subscriptions SET
                    interval_type=%s,
                    amount_fen=%s,
                    proration_fen=%s,
                    current_period_start=%s,
                    current_period_end=%s,
                    last_charge_at=NOW(),
                    retry_count=0,
                    extra=%s,
                    updated_at=NOW()
                WHERE id=%s
            """, (new_interval, int(amount_fen), int(proration_fen),
                  new_start.isoformat(), new_end.isoformat(),
                  json.dumps(extra, ensure_ascii=False), sub.id))
            # 同步刷新 License 有效期
            conn.execute("""
                UPDATE plugin_licenses SET
                    expires_at=%s,
                    license_status='active',
                    updated_at=NOW()
                WHERE plugin_id=%s
            """, (new_end.isoformat(), plugin_id))
            conn.commit()

        # 审计事件：升级/降级（携带变更订单号作幂等键）
        self.record_event(
            plugin_id, event_type, sub_id=sub.id, order_no=order_no,
            payload={'from': sub.interval_type, 'to': new_interval,
                     'amount_fen': int(amount_fen), 'proration_fen': int(proration_fen),
                     'new_period_end': new_end.isoformat()},
        )
        return self.get_subscription(plugin_id)

    # ── 版本包订阅 ─────────────────────────────────────────────────

    def get_bundle_member_ids(self, bundle_id: str) -> List[str]:
        """返回版本包内插件标识列表（来自定价规则 bundles 配置）。"""
        try:
            from .pricing import get_pricing_rules
            cfg = (get_pricing_rules().get('bundles') or {}).get(bundle_id)
            return list(cfg.get('plugins', [])) if cfg else []
        except Exception as e:
            print(f'[PluginSub] get_bundle_member_ids failed: {e}')
            return []

    def _sync_bundle_members(self, bundle_id: str,
                             new_end: Optional[str] = None,
                             status: str = 'active') -> None:
        """同步版本包成员 License：active 刷新有效期 / 其他状态标记过期。

        按 plugin_licenses.metadata->>'bundle_id' 批量定位成员 License。
        """
        if not bundle_id:
            return
        with get_registry_db() as conn:
            if status == 'active' and new_end:
                conn.execute(
                    "UPDATE plugin_licenses SET expires_at=%s, license_status='active', updated_at=NOW() "
                    "WHERE metadata::jsonb->>'bundle_id'=%s",
                    (new_end, bundle_id)
                )
            else:
                conn.execute(
                    "UPDATE plugin_licenses SET license_status='expired', updated_at=NOW() "
                    "WHERE metadata::jsonb->>'bundle_id'=%s AND license_status='active'",
                    (bundle_id,)
                )
            conn.commit()

    def create_bundle(self, bundle_id: str, license_key: str, order_no: str,
                      interval_type: str, amount_fen: int) -> PluginSubscription:
        """创建版本包订阅：包订阅记录 + 包成员 License 生成。

        - 包订阅：plugin_id=bundle_id，tier='bundle'，bundle_id=bundle_id
        - 成员 License：license_key=f'{bundle_id}:{order_no}'，metadata 带 bundle_id 标记
        - 已被包覆盖的成员旧订阅 → cancel（到期不再续费，权限由包接管）
        """
        sub = self.create(plugin_id=bundle_id, license_key=license_key, order_no=order_no,
                          interval_type=interval_type, amount_fen=amount_fen,
                          tier='bundle', bundle_id=bundle_id)

        member_ids = self.get_bundle_member_ids(bundle_id)
        from .license import get_license_manager
        lic = get_license_manager()
        for pid in member_ids:
            try:
                lic.grant_bundle_member(pid, bundle_id, order_no,
                                        sub.current_period_end, sub.id)
            except Exception as e:
                print(f'[PluginSub] grant bundle license for {pid} failed: {e}')
            # 包权限接管：同名成员若有活跃独立订阅 → 取消续费（保留至当期周期末）
            old = self.get_subscription(pid)
            if old and old.status == SubscriptionStatus.ACTIVE:
                self.cancel(pid)

        self.record_event(bundle_id, 'bundle_created', sub_id=sub.id, order_no=order_no,
                          payload={'members': member_ids, 'interval': sub.interval_type,
                                   'amount_fen': sub.amount_fen})
        return sub

    # ── 到期检查 ──────────────────────────────────────────────────

    def check_expired(self) -> List[PluginSubscription]:
        """检查并处理所有到期的订阅

        - auto_renew=0 → 立即标记 expired 并同步过期 License
        - auto_renew=1 → 进入宽限期（GRACE_DAYS 内 License 保留 active），
          并生成续费订单供支付；超过宽限期由 run_grace_lock_scan() 锁定。
        """
        now = datetime.now().isoformat()
        expired = []

        with get_registry_db() as conn:
            rows = conn.execute(
                "SELECT * FROM plugin_subscriptions WHERE status='active' AND current_period_end < %s",
                (now,)
            ).fetchall()
            for row in rows:
                sub = PluginSubscription.from_row(dict(row))
                if sub.auto_renew:
                    # 进入宽限期：生成续费订单（幂等，已生成则跳过）
                    try:
                        self._ensure_renewal_order(sub)
                    except Exception as e:
                        print(f'[PluginSub] renewal order failed for {sub.plugin_id}: {e}')
                    expired.append(sub)
                else:
                    # 不续费：标记过期
                    conn.execute(
                        "UPDATE plugin_subscriptions SET status='expired', updated_at=NOW() WHERE id=%s",
                        (sub.id,)
                    )
                    conn.execute(
                        "UPDATE plugin_licenses SET license_status='expired', updated_at=NOW() "
                        "WHERE plugin_id=%s AND license_status='active'",
                        (sub.plugin_id,)
                    )
                    conn.commit()
                    # 审计事件：按到期周期号区分（每周期一条，重扫幂等）
                    self.record_event(
                        sub.plugin_id, 'expired', sub_id=sub.id,
                        order_no=f"expired@{sub.current_period_end or ''}",
                        payload={'reason': 'period_end_no_renew'},
                    )
                    # 版本包：成员 License 一并过期
                    if sub.bundle_id:
                        self._sync_bundle_members(sub.bundle_id, status='expired')
                    expired.append(sub)

        return expired

    def _ensure_renewal_order(self, sub: PluginSubscription) -> Optional[str]:
        """为到期自动续费订阅生成续费订单（幂等：已生成则跳过）"""
        extra = dict(sub.extra or {})
        if extra.get('pending_renew_order'):
            return extra['pending_renew_order']

        # 从原支付订单获取渠道与客户邮箱
        from .payment import get_payment_order, create_payment_order, get_payment_router, update_payment_order
        src = get_payment_order(sub.order_no)
        if not src:
            extra['renewal_error'] = 'source_order_missing'
            self._update_extra(sub.plugin_id, extra, sub.id)
            return None

        order = create_payment_order(
            plugin_id=sub.plugin_id,
            channel=src.channel or 'alipay',
            amount_fen=sub.amount_fen,
            subject=f'{sub.plugin_id} renewal ({sub.interval_type})',
            description='Plugin subscription renewal',
            customer_email=src.customer_email or '',
        )
        # 标记续费订单：支付回调据此识别（审计追踪），并关联到订阅
        try:
            update_payment_order(order.order_no, extra=json.dumps({
                'renewal': True,
                'subscription_id': sub.id,
            }, ensure_ascii=False))
        except Exception as e:
            print(f'[PluginSub] mark renewal order {order.order_no} failed: {e}')
        try:
            provider = get_payment_router().get_provider(order.channel)
            result = provider.create_order(order)
        except Exception as e:
            extra['renewal_error'] = f'gateway: {e}'
            self._update_extra(sub.plugin_id, extra, sub.id)
            return None
        if not result.success:
            extra['renewal_error'] = f'gateway: {result.error}'
            self._update_extra(sub.plugin_id, extra, sub.id)
            return None

        extra['pending_renew_order'] = order.order_no
        if not extra.get('grace_since'):
            extra['grace_since'] = datetime.now().isoformat()
        self._update_extra(sub.plugin_id, extra, sub.id)
        print(f'[PluginSub] renewal order {order.order_no} created for {sub.plugin_id}')
        return order.order_no

    def _update_extra(self, plugin_id: str, extra: dict, sub_id: int = None) -> None:
        with get_registry_db() as conn:
            if sub_id:
                conn.execute(
                    "UPDATE plugin_subscriptions SET extra=%s, updated_at=NOW() WHERE id=%s",
                    (json.dumps(extra, ensure_ascii=False), sub_id)
                )
            else:
                conn.execute(
                    "UPDATE plugin_subscriptions SET extra=%s, updated_at=NOW() WHERE plugin_id=%s",
                    (json.dumps(extra, ensure_ascii=False), plugin_id)
                )
            conn.commit()

    def run_grace_lock_scan(self) -> List[PluginSubscription]:
        """宽限期锁定：到期超过 GRACE_DAYS 仍未续费的自动续费订阅
        → 订阅 expired + License 过期"""
        locked = []
        with get_registry_db() as conn:
            rows = conn.execute(
                "SELECT * FROM plugin_subscriptions "
                "WHERE status='active' AND auto_renew=1 "
                "  AND current_period_end::timestamp < NOW() - (%s * INTERVAL '1 day')",
                (GRACE_DAYS,)
            ).fetchall()
            for row in rows:
                sub = PluginSubscription.from_row(dict(row))
                conn.execute(
                    "UPDATE plugin_subscriptions SET status='expired', auto_renew=0, updated_at=NOW() WHERE id=%s",
                    (sub.id,)
                )
                conn.execute(
                    "UPDATE plugin_licenses SET license_status='expired', updated_at=NOW() "
                    "WHERE plugin_id=%s AND license_status='active'",
                    (sub.plugin_id,)
                )
                conn.commit()
                # 审计事件：宽限期超期锁定（按到期周期号区分）
                self.record_event(
                    sub.plugin_id, 'expired', sub_id=sub.id,
                    order_no=f"expired@{sub.current_period_end or ''}",
                    payload={'reason': 'grace_period_exceeded', 'grace_days': GRACE_DAYS},
                )
                # 版本包：成员 License 一并过期
                if sub.bundle_id:
                    self._sync_bundle_members(sub.bundle_id, status='expired')
                locked.append(sub)
        if locked:
            print(f'[PluginSub] grace-locked {len(locked)} expired subscription(s)')
        return locked

    # ── 查询 ──────────────────────────────────────────────────────

    def get_subscription(self, plugin_id: str) -> Optional[PluginSubscription]:
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT * FROM plugin_subscriptions WHERE plugin_id=%s ORDER BY id DESC LIMIT 1',
                (plugin_id,)
            ).fetchone()
            if row:
                return PluginSubscription.from_row(dict(row))
        return None

    def list_subscriptions(self) -> List[PluginSubscription]:
        with get_registry_db() as conn:
            rows = conn.execute(
                'SELECT * FROM plugin_subscriptions ORDER BY created_at DESC'
            ).fetchall()
            return [PluginSubscription.from_row(dict(r)) for r in rows]

    def list_expiring(self, within_days: int = 7) -> List[PluginSubscription]:
        """返回 N 天内到期（含当日）的活跃自动续费订阅。

        用于续费提醒邮件扫描（T-7/T-3/T-1）。
        """
        now = datetime.now()
        end = (now + timedelta(days=within_days)).strftime('%Y-%m-%d 23:59:59')
        with get_registry_db() as conn:
            rows = conn.execute(
                "SELECT * FROM plugin_subscriptions "
                "WHERE status='active' AND auto_renew=1 "
                "  AND current_period_end IS NOT NULL "
                "  AND current_period_end::timestamp <= %s::timestamp",
                (end,)
            ).fetchall()
            return [PluginSubscription.from_row(dict(r)) for r in rows]

    # ── 内部工具 ──────────────────────────────────────────────────

    def _calc_period_end(self, start: datetime, interval: str) -> datetime:
        if interval == 'month':
            # 加一个月（考虑闰月）
            month = start.month + 1
            year = start.year
            if month > 12:
                month -= 12
                year += 1
            try:
                return start.replace(year=year, month=month)
            except ValueError:
                # 月末截断
                import calendar
                last_day = calendar.monthrange(year, month)[1]
                return start.replace(year=year, month=month, day=last_day)
        elif interval == 'quarter':
            # 加三个月（跨年 + 月末截断）
            month = start.month + 3
            year = start.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            try:
                return start.replace(year=year, month=month)
            except ValueError:
                import calendar
                last_day = calendar.monthrange(year, month)[1]
                return start.replace(year=year, month=month, day=last_day)
        elif interval == 'year':
            try:
                return start.replace(year=start.year + 1)
            except ValueError:
                return start.replace(year=start.year + 1, month=2, day=28)
        return start + timedelta(days=30)


# ── 模块级单例 ──────────────────────────────────────────────────────

_SUB_MGR = None
_SUB_MGR_LOCK = threading.Lock()


def get_subscription_manager() -> SubscriptionManager:
    global _SUB_MGR
    if _SUB_MGR is None:
        with _SUB_MGR_LOCK:
            if _SUB_MGR is None:
                _SUB_MGR = SubscriptionManager()
    return _SUB_MGR


# ── 定时任务包装（由 admin/app.py APScheduler 调度） ──────────────────

def run_plugin_sub_scan() -> None:
    """每日调度：插件订阅到期扫描（生成续费订单 / 标记过期 / 续费提醒邮件）"""
    mgr = get_subscription_manager()
    expired = mgr.check_expired()
    if expired:
        print(f'[PluginSub] {len(expired)} subscription(s) past due')
    try:
        run_plugin_sub_reminder_scan()
    except Exception as e:
        print(f'[PluginSub] reminder scan failed: {e}')


def run_plugin_sub_grace_scan() -> None:
    """每日调度：插件订阅宽限期锁定（超期未续费 → License 过期）"""
    mgr = get_subscription_manager()
    locked = mgr.run_grace_lock_scan()
    if locked:
        print(f'[PluginSub] grace-locked {len(locked)} subscription(s)')


def run_plugin_sub_reminder_scan() -> None:
    """续费提醒邮件扫描：T-7 / T-3 / T-1 天到期发送提醒。

    幂等：事件键 (sub_id, event_type, 'reminder@{period_end}') 已存在则跳过，
    同一订阅同一到期周期只发一封。
    """
    from .payment import get_payment_order
    try:
        from plugins.email.services import send_email
    except Exception:
        send_email = None

    mgr = get_subscription_manager()
    for days, ev in ((7, 'reminder_t7'), (3, 'reminder_t3'), (1, 'reminder_t1')):
        try:
            subs = mgr.list_expiring(days)
        except Exception as e:
            print(f'[PluginSub] expiring scan failed (days={days}): {e}')
            continue
        for sub in subs:
            try:
                key = f'reminder@{sub.current_period_end}'
                with get_registry_db() as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM subscription_events "
                        "WHERE sub_id=%s AND event_type=%s AND order_no=%s",
                        (sub.id, ev, key)
                    ).fetchone()
                if exists:
                    continue
                po = get_payment_order(sub.order_no)
                email = (po.customer_email or '').strip() if po else ''
                if not email or not send_email:
                    continue
                subject = f'[VeroRun] Plugin subscription expires in {days} day(s)'
                body = (f'Your subscription for "{sub.plugin_id}" will expire on '
                        f'{sub.current_period_end}.\n'
                        f'Auto-renewal amount: {sub.amount_fen / 100:.2f} '
                        f'({sub.interval_type}).\n'
                        f'Manage it in Admin > Plugin Store > My Subscriptions.')
                ok, msg = send_email(email, subject, body)
                if not ok:
                    print(f'[PluginSub] reminder mail failed: {sub.plugin_id}: {msg}')
                    continue
                mgr.record_event(sub.plugin_id, ev, sub_id=sub.id, order_no=key,
                                 payload={'days_left': days})
            except Exception as e:
                print(f'[PluginSub] reminder error for {sub.plugin_id}: {e}')
