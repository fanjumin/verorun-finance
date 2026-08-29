#!/usr/bin/env python3
"""Ad Management Plugin — PostgreSQL schema: ads"""
from i18n import _
import psycopg2
import hashlib
import json
from datetime import date
from plugins._base.db import get_pooled_connection


def get_ads_db():
    """每次从共享池借取连接并设置 ads schema（用完必须 close() 归还池）"""
    conn = None
    try:
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS ads")
        conn.execute("SET search_path TO ads")
        conn.execute("SELECT 1").fetchone()
        return conn
    except psycopg2.DatabaseError as e:
        if conn is not None:
            try:
                conn.close()  # 归还（坏连接由池丢弃）
            except Exception:
                pass
        print(f'[AdsPlugin] ⚠️ Database damaged, reconnect: {e}')
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS ads")
        conn.execute("SET search_path TO ads")
        return conn


def _column_exists(conn, table, column):
    """检查表中是否存在指定列"""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (table, column)
    ).fetchall()
    return len(rows) > 0


def init_ad_db():
    """初始化广告表（幂等，支持从旧版本升级）"""
    with get_ads_db() as conn:
        # ── 广告位表 ──
        conn.execute('''CREATE TABLE IF NOT EXISTS ad_placements (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name            TEXT NOT NULL,
            site_key        TEXT NOT NULL DEFAULT 'default',
            zone_id         BIGINT DEFAULT 0,
            position        TEXT NOT NULL DEFAULT 'sidebar',
            page            TEXT NOT NULL DEFAULT '*',
            ad_type         TEXT NOT NULL DEFAULT 'image',
            image_url       TEXT DEFAULT '',
            link_url        TEXT DEFAULT '',
            ad_code         TEXT DEFAULT '',
            width           BIGINT DEFAULT 320,
            height          BIGINT DEFAULT 0,
            targeting_rules TEXT DEFAULT '{}',
            schedule_start  TEXT DEFAULT '',
            schedule_end    TEXT DEFAULT '',
            weight          BIGINT DEFAULT 1,
            freq_cap        BIGINT DEFAULT 0,
            click_tag       TEXT DEFAULT '',
            utm_source      TEXT DEFAULT '',
            is_active       BIGINT DEFAULT 1,
            sort_order      BIGINT DEFAULT 0,
            impressions     BIGINT DEFAULT 0,
            clicks          BIGINT DEFAULT 0,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )''')

        # 兼容旧表：动态添加新增列
        new_columns = [
            ('site_key',        "TEXT NOT NULL DEFAULT 'default'"),
            ('zone_id',         "BIGINT DEFAULT 0"),
            ('targeting_rules', "TEXT DEFAULT '{}'"),
            ('schedule_start',  "TEXT DEFAULT ''"),
            ('schedule_end',    "TEXT DEFAULT ''"),
            ('weight',          "BIGINT DEFAULT 1"),
            ('freq_cap',        "BIGINT DEFAULT 0"),
            ('click_tag',       "TEXT DEFAULT ''"),
            ('utm_source',      "TEXT DEFAULT ''"),
            ('impressions',     "BIGINT DEFAULT 0"),
            ('clicks',          "BIGINT DEFAULT 0"),
        ]
        for col, dtype in new_columns:
            if not _column_exists(conn, 'ad_placements', col):
                conn.execute(f"ALTER TABLE ad_placements ADD COLUMN {col} {dtype}")

        conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_page ON ad_placements(page, position)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_site_zone ON ad_placements(site_key, zone_id, is_active)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_render ON ad_placements(is_active, site_key, zone_id, position, page, sort_order, id)')

        # ── 广告位区域表 ──
        conn.execute('''CREATE TABLE IF NOT EXISTS ad_zones (
            id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            site_key    TEXT NOT NULL DEFAULT 'default',
            name        TEXT NOT NULL,
            identifier  TEXT NOT NULL,
            description TEXT DEFAULT '',
            width       BIGINT DEFAULT 0,
            height      BIGINT DEFAULT 0,
            is_active   BIGINT DEFAULT 1,
            sort_order  BIGINT DEFAULT 0,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )''')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_zone_identifier ON ad_zones(site_key, identifier)')

        # ── 每日统计表 ──
        conn.execute('''CREATE TABLE IF NOT EXISTS ad_stats (
            id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            ad_id       BIGINT NOT NULL,
            stat_date   TEXT NOT NULL,
            impressions BIGINT DEFAULT 0,
            clicks      BIGINT DEFAULT 0,
            UNIQUE(ad_id, stat_date)
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_stats_date ON ad_stats(ad_id, stat_date)')

        # ── 点击明细表（采样，用于排查刷量） ──
        conn.execute('''CREATE TABLE IF NOT EXISTS ad_clicks (
            id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            ad_id       BIGINT NOT NULL,
            page        TEXT DEFAULT '',
            position    TEXT DEFAULT '',
            ip          TEXT DEFAULT '',
            user_agent  TEXT DEFAULT '',
            referrer    TEXT DEFAULT '',
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ad_clicks_ad ON ad_clicks(ad_id, created_at)')

        conn.commit()
        print(_('[AdsPlugin] PG schema ads has been initialized'))


def migrate_ad_db():
    """显式迁移入口（与 init_ad_db 相同逻辑，供外部调用）"""
    init_ad_db()


# ── 区域管理辅助函数 ──

def list_zones(site_key='default', active_only=False):
    with get_ads_db() as conn:
        sql = 'SELECT * FROM ad_zones WHERE site_key=%s'
        params = [site_key]
        if active_only:
            sql += ' AND is_active=1'
        sql += ' ORDER BY sort_order, id'
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_zone(zone_id):
    with get_ads_db() as conn:
        row = conn.execute('SELECT * FROM ad_zones WHERE id=%s', (zone_id,)).fetchone()
        return dict(row) if row else None


def create_zone(data):
    with get_ads_db() as conn:
        cur = conn.execute('''INSERT INTO ad_zones
            (site_key, name, identifier, description, width, height, is_active, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (data.get('site_key', 'default'),
             data.get('name', ''),
             data.get('identifier', ''),
             data.get('description', ''),
             data.get('width', 0),
             data.get('height', 0),
             data.get('is_active', 1),
             data.get('sort_order', 0)))
        conn.commit()
        return cur.fetchone()['id']


def update_zone(zone_id, data):
    with get_ads_db() as conn:
        conn.execute('''UPDATE ad_zones SET
            site_key=%s, name=%s, identifier=%s, description=%s, width=%s, height=%s,
            is_active=%s, sort_order=%s, updated_at=NOW()
            WHERE id=%s''',
            (data.get('site_key', 'default'),
             data.get('name', ''),
             data.get('identifier', ''),
             data.get('description', ''),
             data.get('width', 0),
             data.get('height', 0),
             data.get('is_active', 1),
             data.get('sort_order', 0),
             zone_id))
        conn.commit()


def delete_zone(zone_id):
    with get_ads_db() as conn:
        conn.execute('DELETE FROM ad_zones WHERE id=%s', (zone_id,))
        conn.commit()


# ── 共享 CRUD（路由层 routes.py 与 AI 层 ai_tools.py 复用） ──

class AdNotFound(Exception):
    """指定的广告不存在"""


# 更新字段白名单：动态 SQL 列名只允许来自此硬编码集合，杜绝注入
_UPDATE_FIELDS = (
    'name', 'site_key', 'zone_id', 'position', 'page', 'ad_type', 'image_url',
    'link_url', 'ad_code', 'width', 'height', 'schedule_start', 'schedule_end',
    'weight', 'freq_cap', 'click_tag', 'utm_source', 'is_active', 'sort_order',
)


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_targeting(data):
    """解析并校验定向规则 JSON（字符串或 dict 均可）"""
    rules = data.get('targeting_rules') or data.get('targeting') or {}
    if isinstance(rules, str):
        try:
            rules = json.loads(rules)
        except (json.JSONDecodeError, TypeError):
            rules = {}
    if not isinstance(rules, dict):
        rules = {}
    return rules


def create_ad_record(data):
    """创建广告位（routes / ai_tools 共用），返回新 id"""
    with get_ads_db() as conn:
        cur = conn.execute('''INSERT INTO ad_placements
            (name, site_key, zone_id, position, page, ad_type, image_url, link_url, ad_code,
             width, height, targeting_rules, schedule_start, schedule_end, weight, freq_cap,
             click_tag, utm_source, is_active, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (str(data.get('name') or '').strip(),
             str(data.get('site_key') or 'default'),
             _as_int(data.get('zone_id')),
             str(data.get('position') or 'sidebar'),
             str(data.get('page') or '*'),
             str(data.get('ad_type') or 'image'),
             str(data.get('image_url') or ''),
             str(data.get('link_url') or ''),
             str(data.get('ad_code') or ''),
             _as_int(data.get('width'), 320),
             _as_int(data.get('height')),
             json.dumps(_parse_targeting(data), ensure_ascii=False),
             str(data.get('schedule_start') or ''),
             str(data.get('schedule_end') or ''),
             _as_int(data.get('weight'), 1),
             _as_int(data.get('freq_cap')),
             str(data.get('click_tag') or ''),
             str(data.get('utm_source') or ''),
             _as_int(data.get('is_active'), 1),
             _as_int(data.get('sort_order'))))
        conn.commit()
        return cur.fetchone()['id']


def update_ad_record(ad_id, data):
    """动态更新广告位（列名仅来自 _UPDATE_FIELDS 白名单，只更新传入字段）

    成功返回 True；广告不存在抛 AdNotFound；无更新字段抛 ValueError。
    """
    with get_ads_db() as conn:
        existing = conn.execute('SELECT id FROM ad_placements WHERE id=%s', (ad_id,)).fetchone()
        if not existing:
            raise AdNotFound(ad_id)
        fields, params = [], []
        for col in _UPDATE_FIELDS:
            if col in data:
                fields.append(f'{col}=%s')
                params.append(data[col])
        if 'targeting_rules' in data:
            t = data['targeting_rules']
            if isinstance(t, dict):
                t = json.dumps(t, ensure_ascii=False)
            fields.append('targeting_rules=%s')
            params.append(t)
        if not fields:
            raise ValueError('no fields to update')
        fields.append('updated_at=NOW()')
        params.append(ad_id)
        conn.execute(f"UPDATE ad_placements SET {', '.join(fields)} WHERE id=%s", params)
        conn.commit()
        return True


def delete_ad_record(ad_id):
    """删除广告位并级联清理统计数据与点击明细（同一事务，避免孤立数据）"""
    with get_ads_db() as conn:
        conn.execute('DELETE FROM ad_stats WHERE ad_id=%s', (ad_id,))
        conn.execute('DELETE FROM ad_clicks WHERE ad_id=%s', (ad_id,))
        conn.execute('DELETE FROM ad_placements WHERE id=%s', (ad_id,))
        conn.commit()


def count_zone_ads(zone_id):
    """统计引用指定区域的广告数量（删除区域前检查用）"""
    with get_ads_db() as conn:
        row = conn.execute('SELECT COUNT(*) AS c FROM ad_placements WHERE zone_id=%s', (zone_id,)).fetchone()
        return row['c'] if row else 0


# ── 统计辅助函数 ──

def _hash_ip(ip):
    """IP 哈希存储：去除明文 PII，同时保留同源刷量识别能力（哈希值相同）"""
    if not ip:
        return ''
    return hashlib.sha256(ip.encode('utf-8')).hexdigest()


def record_impression(ad_id):
    """记录一次展示（累计 + 每日）"""
    with get_ads_db() as conn:
        today = date.today().isoformat()
        conn.execute('UPDATE ad_placements SET impressions = impressions + 1, updated_at=NOW() WHERE id=%s', (ad_id,))
        conn.execute('''INSERT INTO ad_stats (ad_id, stat_date, impressions, clicks)
            VALUES (%s, %s, 1, 0)
            ON CONFLICT(ad_id, stat_date) DO UPDATE SET
                impressions = impressions + 1''', (ad_id, today))
        conn.commit()


def record_click(ad_id, page='', position='', ip='', user_agent='', referrer=''):
    """记录一次点击（累计 + 每日 + 明细）"""
    with get_ads_db() as conn:
        today = date.today().isoformat()
        conn.execute('UPDATE ad_placements SET clicks = clicks + 1, updated_at=NOW() WHERE id=%s', (ad_id,))
        conn.execute('''INSERT INTO ad_stats (ad_id, stat_date, impressions, clicks)
            VALUES (%s, %s, 0, 1)
            ON CONFLICT(ad_id, stat_date) DO UPDATE SET
                clicks = clicks + 1''', (ad_id, today))
        conn.execute('''INSERT INTO ad_clicks (ad_id, page, position, ip, user_agent, referrer)
            VALUES (%s, %s, %s, %s, %s, %s)''', (ad_id, page, position, _hash_ip(ip), user_agent, referrer))
        conn.commit()


def get_ad_stats(ad_id=None, site_key=None, days=7):
    """查询广告统计，返回每日列表和汇总"""
    with get_ads_db() as conn:
        from datetime import date, timedelta
        end = date.today()
        start = end - timedelta(days=days - 1)

        where = "WHERE stat_date >= %s AND stat_date <= %s"
        params = [start.isoformat(), end.isoformat()]
        if ad_id:
            where += " AND ad_id=%s"
            params.append(ad_id)
        elif site_key:
            where += " AND ad_id IN (SELECT id FROM ad_placements WHERE site_key=%s AND is_active=1)"
            params.append(site_key)

        rows = conn.execute(
            f'SELECT stat_date, SUM(impressions) AS impressions, SUM(clicks) AS clicks FROM ad_stats {where} GROUP BY stat_date ORDER BY stat_date',
            params
        ).fetchall()

        total_imp = sum(r['impressions'] or 0 for r in rows)
        total_clk = sum(r['clicks'] or 0 for r in rows)
        ctr = round(total_clk / total_imp * 100, 2) if total_imp else 0.0

        return {
            'daily': [dict(r) for r in rows],
            'total': {'impressions': total_imp, 'clicks': total_clk, 'ctr': ctr}
        }