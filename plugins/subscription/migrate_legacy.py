#!/usr/bin/env python3
"""
一次性迁移脚本：主库 public schema 的 A 套餐制订阅表 → 插件 subscription schema（V20260812 完整迁移）

用法:
    python plugins/subscription/migrate_legacy.py            # 迁移 + 打印各表计数对比
    python plugins/subscription/migrate_legacy.py --drop     # 迁移后 DROP 主库 A 表（破坏性，需确认）

安全说明:
    - 所有写入 INSERT ... ON CONFLICT DO NOTHING，可重复执行（幂等）
    - DROP 前检查外部外键依赖，存在依赖则拒绝删除并打印清单
    - 迁移数据前先做源表存在性检查，缺表自动跳过
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from plugins._base.db import PgConnection, get_raw_connection

# ── 常量 ────────────────────────────────────────────────────────────────
PUBLIC = 'public'
SCHEMA = 'subscription'

# 可迁移的主库 A 表
TABLES = [
    'subscription_plans',
    'module_pricing',
    'subscriptions',
    'subscription_orders',
    'payment_events',
    'subscription_audit_log',
    'deployment_codes',
    'invoices',
]


def _count(conn, table):
    """表行数（表不存在返回 None）"""
    try:
        row = conn.execute(f'SELECT COUNT(*) AS c FROM {table}').fetchone()
        return dict(row)['c']
    except Exception:
        return None


def _table_exists(conn, table):
    return _count(conn, table) is not None


def migrate(conn):
    """执行迁移（幂等），返回每表 (源, 目标) 计数"""
    results = []

    # ── 1. subscription_plans → sub_items ──────────────────────────
    src_count = _count(conn, f'{PUBLIC}.subscription_plans')
    if src_count is not None:
        conn.execute(f"""
            INSERT INTO {SCHEMA}.sub_items
                (item_key, category, name_zh, name_en, description_zh, description_en,
                 price_month, price_year, tier, features_json, trial_days, currency,
                 sort_order, is_active, created_at, updated_at)
            SELECT plan_key, 'plugin', name, name, description, description,
                   price_month, price_year, tier, features_json, trial_days,
                   COALESCE(currency, 'CNY'), sort_order, is_active,
                   created_at::text, updated_at::text
            FROM {PUBLIC}.subscription_plans
            ON CONFLICT (item_key) DO NOTHING
        """)
    dst_count = _count(conn, f'{SCHEMA}.sub_items')
    results.append(('subscription_plans -> sub_items', src_count, dst_count))

    # ── 2. module_pricing → sub_items（并入 SKU，同 key 保留 plans 优先） ──
    src_count = _count(conn, f'{PUBLIC}.module_pricing')
    if src_count is not None:
        conn.execute(f"""
            INSERT INTO {SCHEMA}.sub_items
                (item_key, category, name_zh, name_en, description_zh, description_en,
                 price_month, price_year, tier, features_json, trial_days, currency,
                 billing_mode, trial_daily_limit, post_trial_action, refund_days,
                 limit_even_byok, is_active, sort_order, created_at, updated_at)
            SELECT module_key, 'module', name, name, description, description,
                   price_month_fen, price_year_fen, 'premium', '[]', trial_days,
                   'CNY', pattern, trial_daily_limit, post_trial_action, refund_days,
                   limit_even_byok, is_active, sort_order, created_at::text, updated_at::text
            FROM {PUBLIC}.module_pricing
            ON CONFLICT (item_key) DO NOTHING
        """)
    dst_count = _count(conn, f'{SCHEMA}.sub_items')
    results.append(('module_pricing -> sub_items', src_count, dst_count))

    # ── 3. subscriptions → user_subscriptions ──────────────────────
    src_count = _count(conn, f'{PUBLIC}.subscriptions')
    if src_count is not None:
        conn.execute(f"""
            INSERT INTO {SCHEMA}.user_subscriptions
                (user_id, item_key, interval_type, status, period_start, period_end,
                 auto_renew, auto_activate, tier, module_states, retry_count,
                 last_charge_at, grace_end, payment_method, agreement_id,
                 canceled_at, cancel_reason, created_at, updated_at)
            SELECT user_id, plan_key, period, status,
                   current_period_start, current_period_end,
                   auto_renew, 0, '', module_states, 0, '', '',
                   COALESCE(payment_method, ''), COALESCE(alipay_agreement_id, ''),
                   canceled_at::text, cancel_reason, created_at::text, updated_at::text
            FROM {PUBLIC}.subscriptions
            ON CONFLICT (user_id, item_key) DO NOTHING
        """)
    dst_count = _count(conn, f'{SCHEMA}.user_subscriptions')
    results.append(('subscriptions -> user_subscriptions', src_count, dst_count))

    # ── 4. subscription_orders → sub_orders ────────────────────────
    src_count = _count(conn, f'{PUBLIC}.subscription_orders')
    if src_count is not None:
        conn.execute(f"""
            INSERT INTO {SCHEMA}.sub_orders
                (order_no, user_id, item_key, interval_type, amount_fen, currency,
                 channel, status, trade_no, fail_reason, notify_id, notify_raw,
                 paid_at, created_at, updated_at)
            SELECT order_no, user_id, plan_key, period, amount_fen,
                   COALESCE(currency, 'CNY'), COALESCE(payment_method, 'alipay'),
                   status, COALESCE(channel_order_id, ''), COALESCE(fail_reason, ''),
                   COALESCE(notify_id, ''), COALESCE(notify_raw, ''),
                   paid_at::text, created_at::text, updated_at::text
            FROM {PUBLIC}.subscription_orders
            ON CONFLICT (order_no) DO NOTHING
        """)
    dst_count = _count(conn, f'{SCHEMA}.sub_orders')
    results.append(('subscription_orders -> sub_orders', src_count, dst_count))

    # ── 5. payment_events → sub_payment_events ─────────────────────
    src_count = _count(conn, f'{PUBLIC}.payment_events')
    if src_count is not None:
        conn.execute(f"""
            INSERT INTO {SCHEMA}.sub_payment_events
                (user_id, sub_id, event_type, channel, channel_event_id,
                 amount_fen, result, fail_reason, raw_response, created_at)
            SELECT user_id, sub_id, event_type, channel,
                   COALESCE(channel_event_id, ''), amount_fen,
                   COALESCE(result, ''), COALESCE(fail_reason, ''),
                   COALESCE(raw_response, ''), created_at::text
            FROM {PUBLIC}.payment_events
        """)
    dst_count = _count(conn, f'{SCHEMA}.sub_payment_events')
    results.append(('payment_events -> sub_payment_events', src_count, dst_count))

    # ── 6. subscription_audit_log → sub_audit_log ──────────────────
    src_count = _count(conn, f'{PUBLIC}.subscription_audit_log')
    if src_count is not None:
        conn.execute(f"""
            INSERT INTO {SCHEMA}.sub_audit_log
                (user_id, sub_id, action, detail, ip_address, admin_id, created_at)
            SELECT user_id, sub_id, action, COALESCE(detail, ''),
                   COALESCE(ip_address, ''), admin_id, created_at::text
            FROM {PUBLIC}.subscription_audit_log
        """)
    dst_count = _count(conn, f'{SCHEMA}.sub_audit_log')
    results.append(('subscription_audit_log -> sub_audit_log', src_count, dst_count))

    # ── 7. deployment_codes → deploy_codes ─────────────────────────
    src_count = _count(conn, f'{PUBLIC}.deployment_codes')
    if src_count is not None:
        conn.execute(f"""
            INSERT INTO {SCHEMA}.deploy_codes
                (code, code_hash, user_id, item_key, duration_days, expires_at,
                 status, last_heartbeat, last_hostname, last_version, created_at, updated_at)
            SELECT code, code_hash, user_id, plan_key, duration_days, expires_at,
                   status, last_heartbeat, COALESCE(last_hostname, ''),
                   COALESCE(last_version, ''), created_at::text, updated_at::text
            FROM {PUBLIC}.deployment_codes
            ON CONFLICT (code) DO NOTHING
        """)
    dst_count = _count(conn, f'{SCHEMA}.deploy_codes')
    results.append(('deployment_codes -> deploy_codes', src_count, dst_count))

    # ── 8. invoices → sub_invoices ─────────────────────────────────
    src_count = _count(conn, f'{PUBLIC}.invoices')
    if src_count is not None:
        conn.execute(f"""
            INSERT INTO {SCHEMA}.sub_invoices
                (invoice_no, order_no, user_id, amount_fen, amount_yuan,
                 plan_name, period_text, status, pdf_path, created_at)
            SELECT invoice_no, order_no, user_id, amount_fen, amount_yuan,
                   COALESCE(plan_name, ''), COALESCE(period_text, ''),
                   status, COALESCE(pdf_path, ''), created_at::text
            FROM {PUBLIC}.invoices
            ON CONFLICT (invoice_no) DO NOTHING
        """)
    dst_count = _count(conn, f'{SCHEMA}.sub_invoices')
    results.append(('invoices -> sub_invoices', src_count, dst_count))

    conn.commit()
    return results


def _check_fk_dependencies(conn):
    """检查是否有外部表外键引用主库 A 表（外部 = 不在可删清单内的表）"""
    fk_list = ', '.join(f"'{t}'" for t in TABLES)
    rows = conn.execute(f"""
        SELECT tc.table_schema, tc.table_name, ccu.table_name AS ref_table
        FROM information_schema.table_constraints tc
        JOIN information_schema.constraint_column_usage ccu
             ON tc.constraint_name = ccu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND ccu.table_schema = '{PUBLIC}'
          AND ccu.table_name IN ({fk_list})
    """).fetchall()
    return [dict(r) for r in rows]


def drop_tables(conn):
    """删除主库 A 表（DROP 前检查外部依赖，有依赖则拒绝）"""
    deps = _check_fk_dependencies(conn)
    external = [d for d in deps
                if d['table_schema'] != PUBLIC or d['table_name'] not in TABLES]
    if external:
        print('[Drop] WARN: 外部外键依赖存在，拒绝删除 A 表：')
        for d in external:
            print(f"    {d['table_schema']}.{d['table_name']} -> {d['ref_table']}")
        return False

    # 先删子表（引用 subscriptions 的表），再删父表
    order = [
        'subscription_orders', 'payment_events', 'subscription_audit_log',
        'subscriptions', 'subscription_plans', 'module_pricing',
        'deployment_codes', 'invoices',
    ]
    for t in order:
        conn.execute(f'DROP TABLE IF EXISTS {PUBLIC}.{t} CASCADE')
    conn.commit()

    # 验证
    remaining = [t for t in TABLES if _table_exists(conn, f'{PUBLIC}.{t}')]
    if remaining:
        print(f'[Drop] WARN: 仍有表未删除: {remaining}')
        return False
    print('[Drop] 主库 8 张 A 表已全部删除')
    return True


def main():
    do_drop = '--drop' in sys.argv
    conn = PgConnection(get_raw_connection())

    print('═══ 订阅数据迁移：主库 public → 插件 subscription schema ═══')
    print()

    # 源表存在性检查
    for t in TABLES:
        if not _table_exists(conn, f'{PUBLIC}.{t}'):
            print(f'[Info] 源表不存在，跳过: {PUBLIC}.{t}')

    results = migrate(conn)

    print()
    print('迁移结果（源表 -> 目标表 : 源行数 / 目标行数）:')
    for label, src, dst in results:
        src_txt = '-' if src is None else str(src)
        dst_txt = '-' if dst is None else str(dst)
        print(f'  {label:<40s} {src_txt:>8s} / {dst_txt:>8s}')

    if do_drop:
        print()
        print('═══ 执行 DROP 主库 A 表 ═══')
        drop_tables(conn)
    else:
        print()
        print('[完成] 迁移完成（未执行 DROP）。核对计数后运行: python plugins/subscription/migrate_legacy.py --drop')

    conn.close()


if __name__ == '__main__':
    main()
