#!/usr/bin/env python3
"""
阿里巴巴API数据模型 — PostgreSQL schema: ali_api

包含：
1. ali_api_items - 阿里巴巴商品缓存表
2. ali_api_logs - API调用日志表
3. ali_api_user_stats - 用户调用统计表
"""

import json
import psycopg2
import psycopg2.extras
from datetime import datetime
from typing import Dict, Any, List, Optional
from contextlib import contextmanager

import sys
import os
from plugins._base.db import get_raw_connection
from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('ali_api')


class _PgConnection:
    """psycopg2 connection adapter with sqlite3-compatible interface."""
    def __init__(self, conn):
        self._conn = conn
    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if params is not None:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return cur
    def commit(self):
        self._conn.commit()
    def close(self):
        self._conn.close()


@contextmanager
def get_db():
    """连接插件自有数据库（PG schema: ali_api）。"""
    raw = get_raw_connection()
    raw.autocommit = False
    raw.cursor().execute("CREATE SCHEMA IF NOT EXISTS ali_api")
    raw.commit()
    raw.cursor().execute("SET search_path TO ali_api")
    raw.commit()
    conn = _PgConnection(raw)
    try:
        yield conn
        conn.commit()
    finally:
        raw.close()


@contextmanager
def get_main_db():
    """只读连接主库（用于查询 users/products/api_keys/system_config 等主表）。

    脱离主项目单独运行时（无法 import models）抛出 ImportError，由调用方兜底。
    """
    sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
    from models import get_db as _main_get_db
    with _main_get_db() as conn:
        yield conn

# ===== 数据模型类 =====

class AliApiItem:
    """阿里巴巴商品缓存"""
    
    @staticmethod
    def create_table(conn):
        """创建商品缓存表"""
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ali_api_items (
                id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id             BIGINT DEFAULT 0,
                product_id          TEXT UNIQUE NOT NULL,
                source_url          TEXT DEFAULT '',
                title               TEXT NOT NULL DEFAULT '',
                original_title      TEXT DEFAULT '',
                price               DECIMAL(10,2) DEFAULT 0,
                original_price      DECIMAL(10,2) DEFAULT 0,
                currency            TEXT DEFAULT 'CNY',
                category            TEXT DEFAULT '',
                images              TEXT DEFAULT '[]',
                specs               TEXT DEFAULT '{}',
                product_sku         TEXT DEFAULT '[]',
                description         TEXT DEFAULT '',
                -- B2B 增强字段
                moq                 BIGINT DEFAULT 0,              -- 最小起订量
                wholesale_price     TEXT DEFAULT '[]',              -- 批发阶梯价 JSON
                is_support_agent    BIGINT DEFAULT 0,              -- 是否支持一件代发
                seller_credit       BIGINT DEFAULT 0,              -- 卖家诚信通等级
                shop_level          BIGINT DEFAULT 0,              -- 店铺等级
                seller_name         TEXT DEFAULT '',                -- 卖家名称
                seller_id           TEXT DEFAULT '',                -- 卖家 ID
                location            TEXT DEFAULT '',                -- 所在地
                -- AI 处理结果
                ai_title            TEXT DEFAULT '',
                ai_title_options    TEXT DEFAULT '[]',
                selected_title      TEXT DEFAULT '',
                ai_description      TEXT DEFAULT '',
                -- 发布状态
                publish_status      TEXT DEFAULT 'draft',
                target_product_id   BIGINT DEFAULT NULL,
                -- 统计
                api_call_count      BIGINT DEFAULT 0,
                -- 原始API响应
                api_response        TEXT DEFAULT '{}',
                status              TEXT DEFAULT 'active',
                last_synced_at      TIMESTAMPTZ DEFAULT NULL,
                error_msg           TEXT DEFAULT '',
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                updated_at          TIMESTAMPTZ DEFAULT NOW(),
                processed_at        TIMESTAMPTZ DEFAULT NULL
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_items_product_id ON ali_api_items(product_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_items_status ON ali_api_items(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_items_category ON ali_api_items(category)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_items_user ON ali_api_items(user_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_items_publish ON ali_api_items(publish_status)')
    
    @staticmethod
    def insert_or_update(conn, item_data: Dict[str, Any]) -> int:
        """插入或更新商品数据"""
        now = datetime.now().isoformat()
        
        # 准备数据
        product_id = item_data.get('product_id')
        if not product_id:
            raise ValueError("product_id 不能为空")
        
        # 检查是否存在
        cursor = conn.execute('SELECT id FROM ali_api_items WHERE product_id = %s', (product_id,))
        existing = cursor.fetchone()
        
        now_iso = datetime.now().isoformat()
        
        # 通用字段处理
        _title = item_data.get('title', '')
        _original_title = item_data.get('original_title', '') or _title
        _ai_title = item_data.get('ai_title', '')
        _ai_title_options = json.dumps(item_data.get('ai_title_options', []), ensure_ascii=False)
        _selected_title = item_data.get('selected_title', '')
        _description = item_data.get('description', '')
        _ai_description = item_data.get('ai_description', '')
        _price = item_data.get('price', 0)
        _original_price = item_data.get('original_price', 0)
        _currency = item_data.get('currency', 'CNY')
        _category = item_data.get('category', '')
        _images = json.dumps(item_data.get('images', []), ensure_ascii=False)
        _specs = json.dumps(item_data.get('specs', {}), ensure_ascii=False)
        _product_sku = json.dumps(item_data.get('product_sku', []), ensure_ascii=False)
        _source_url = item_data.get('source_url', '')
        _api_response = json.dumps(item_data.get('api_response', {}), ensure_ascii=False)
        _status = item_data.get('status', 'active')
        _publish_status = item_data.get('publish_status', 'draft')
        _target_product_id = item_data.get('target_product_id')
        _api_call_count = item_data.get('api_call_count', 0)
        _user_id = item_data.get('user_id', 0)
        _error_msg = item_data.get('error_msg', '')
        _processed_at = item_data.get('processed_at')
        # B2B 字段
        _moq = item_data.get('moq', 0)
        _wholesale_price = json.dumps(item_data.get('wholesale_price', []), ensure_ascii=False)
        _is_support_agent = 1 if item_data.get('is_support_agent') else 0
        _seller_credit = item_data.get('seller_credit', 0)
        _shop_level = item_data.get('shop_level', 0)
        _seller_name = item_data.get('seller_name', '')
        _seller_id = item_data.get('seller_id', '')
        _location = item_data.get('location', '')
        
        if existing:
            # 更新
            conn.execute('''
                UPDATE ali_api_items SET
                    user_id = %s, title = %s, original_title = %s,
                    ai_title = %s, ai_title_options = %s, selected_title = %s,
                    description = %s, ai_description = %s,
                    price = %s, original_price = %s, currency = %s,
                    category = %s, images = %s, specs = %s, product_sku = %s,
                    moq = %s, wholesale_price = %s, is_support_agent = %s,
                    seller_credit = %s, shop_level = %s,
                    seller_name = %s, seller_id = %s, location = %s,
                    source_url = %s, api_response = %s, status = %s,
                    publish_status = %s, target_product_id = %s,
                    api_call_count = api_call_count + %s,
                    last_synced_at = %s, error_msg = %s,
                    processed_at = COALESCE(%s, processed_at),
                    updated_at = %s
                WHERE product_id = %s
            ''', (
                _user_id, _title, _original_title,
                _ai_title, _ai_title_options, _selected_title,
                _description, _ai_description,
                _price, _original_price, _currency,
                _category, _images, _specs, _product_sku,
                _moq, _wholesale_price, _is_support_agent,
                _seller_credit, _shop_level,
                _seller_name, _seller_id, _location,
                _source_url, _api_response, _status,
                _publish_status, _target_product_id,
                _api_call_count,
                now_iso, _error_msg,
                _processed_at,
                now_iso,
                product_id
            ))
            return existing['id']
        else:
            # 插入
            cursor = conn.execute('''
                INSERT INTO ali_api_items (
                    user_id, product_id, source_url,
                    title, original_title,
                    ai_title, ai_title_options, selected_title,
                    description, ai_description,
                    price, original_price, currency,
                    category, images, specs, product_sku,
                    moq, wholesale_price, is_support_agent,
                    seller_credit, shop_level,
                    seller_name, seller_id, location,
                    api_response, status,
                    publish_status, target_product_id,
                    api_call_count, error_msg,
                    last_synced_at, processed_at,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s)
                RETURNING id
            ''', (
                _user_id, product_id, _source_url,
                _title, _original_title,
                _ai_title, _ai_title_options, _selected_title,
                _description, _ai_description,
                _price, _original_price, _currency,
                _category, _images, _specs, _product_sku,
                _moq, _wholesale_price, _is_support_agent,
                _seller_credit, _shop_level,
                _seller_name, _seller_id, _location,
                _api_response, _status,
                _publish_status, _target_product_id,
                _api_call_count, _error_msg,
                now_iso, _processed_at,
                now_iso, now_iso
            ))
            return cursor.fetchone()['id']
    
    @staticmethod
    def get_by_id(conn, item_id: int) -> Optional[Dict[str, Any]]:
        """根据ID获取商品"""
        row = conn.execute('SELECT * FROM ali_api_items WHERE id = %s', (item_id,)).fetchone()
        if not row:
            return None
        return dict(row)
    
    @staticmethod
    def get_by_product_id(conn, product_id: str) -> Optional[Dict[str, Any]]:
        """根据阿里巴巴商品ID获取"""
        row = conn.execute('SELECT * FROM ali_api_items WHERE product_id = %s', (product_id,)).fetchone()
        if not row:
            return None
        return dict(row)
    
    @staticmethod
    def list_items(conn, status: str = 'active', limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """列出商品"""
        query = 'SELECT * FROM ali_api_items WHERE status = %s ORDER BY updated_at DESC LIMIT %s OFFSET %s'
        rows = conn.execute(query, (status, limit, offset)).fetchall()
        return [dict(row) for row in rows]
    
    @staticmethod
    def search_items(conn, keyword: str, limit: int = 50) -> List[Dict[str, Any]]:
        """搜索商品（已转义 LIKE 通配符）"""
        # 转义 LIKE 中的特殊字符 % 和 _
        escaped = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        query = '''
            SELECT * FROM ali_api_items 
            WHERE (title LIKE %s ESCAPE '\\' OR original_title LIKE %s ESCAPE '\\' 
                   OR ai_title LIKE %s ESCAPE '\\' OR description LIKE %s ESCAPE '\\')
            AND status = 'active'
            ORDER BY updated_at DESC LIMIT %s
        '''
        pattern = f'%{escaped}%'
        rows = conn.execute(query, (pattern, pattern, pattern, pattern, limit)).fetchall()
        return [dict(row) for row in rows]
    
    @staticmethod
    def update_publish_status(conn, item_id: int, publish_status: str, target_product_id: int = None) -> bool:
        """更新发布状态"""
        now_iso = datetime.now().isoformat()
        if target_product_id:
            conn.execute('''
                UPDATE ali_api_items SET
                    publish_status = %s, target_product_id = %s,
                    processed_at = %s, updated_at = %s
                WHERE id = %s
            ''', (publish_status, target_product_id, now_iso, now_iso, item_id))
        else:
            conn.execute('''
                UPDATE ali_api_items SET
                    publish_status = %s, processed_at = %s, updated_at = %s
                WHERE id = %s
            ''', (publish_status, now_iso, now_iso, item_id))
        return conn.rowcount > 0
    
    @staticmethod
    def migrate_b2b_fields(conn) -> bool:
        """迁移旧数据，增加 B2B 字段（幂等）

        先通过 information_schema 检查列是否已存在，再执行 ALTER，
        避免用 try/except 静默吞掉连接错误/权限错误。
        """
        existing = {
            r['column_name'] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = CURRENT_SCHEMA() AND table_name = 'ali_api_items'"
            ).fetchall()
        }
        for col, ddl in [
            ('moq',              "ALTER TABLE ali_api_items ADD COLUMN moq BIGINT DEFAULT 0"),
            ('wholesale_price',  "ALTER TABLE ali_api_items ADD COLUMN wholesale_price TEXT DEFAULT '[]'"),
            ('is_support_agent', "ALTER TABLE ali_api_items ADD COLUMN is_support_agent BIGINT DEFAULT 0"),
            ('seller_credit',    "ALTER TABLE ali_api_items ADD COLUMN seller_credit BIGINT DEFAULT 0"),
            ('shop_level',       "ALTER TABLE ali_api_items ADD COLUMN shop_level BIGINT DEFAULT 0"),
            ('seller_name',      "ALTER TABLE ali_api_items ADD COLUMN seller_name TEXT DEFAULT ''"),
            ('seller_id',        "ALTER TABLE ali_api_items ADD COLUMN seller_id TEXT DEFAULT ''"),
            ('location',         "ALTER TABLE ali_api_items ADD COLUMN location TEXT DEFAULT ''"),
        ]:
            if col not in existing:
                conn.execute(ddl)
        conn.commit()
        return True

    @staticmethod
    def update_ai_titles(conn, item_id: int, ai_title_options: list, selected_title: str = '') -> bool:
        """更新AI生成的标题选项"""
        now_iso = datetime.now().isoformat()
        conn.execute('''
            UPDATE ali_api_items SET
                ai_title_options = %s, selected_title = %s,
                ai_title = %s, updated_at = %s
            WHERE id = %s
        ''', (
            json.dumps(ai_title_options, ensure_ascii=False),
            selected_title,
            selected_title or (ai_title_options[0]['title'] if ai_title_options else ''),
            now_iso,
            item_id
        ))
        return conn.rowcount > 0

    @staticmethod
    def list_by_publish_status(conn, publish_status: str = 'draft', limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """按发布状态列出商品"""
        rows = conn.execute(
            'SELECT * FROM ali_api_items WHERE publish_status = %s ORDER BY updated_at DESC LIMIT %s OFFSET %s',
            (publish_status, limit, offset)
        ).fetchall()
        return [dict(row) for row in rows]


class AliApiReview:
    """1688 商品评论"""

    @staticmethod
    def create_table(conn):
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ali_api_reviews (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                product_id      TEXT NOT NULL,
                review_id       TEXT UNIQUE NOT NULL,
                user_id         BIGINT DEFAULT 0,
                buyer_name      TEXT DEFAULT '',
                rating          BIGINT DEFAULT 5,
                content         TEXT DEFAULT '',
                review_time     TEXT DEFAULT '',
                spec_info       TEXT DEFAULT '',
                images          TEXT DEFAULT '[]',
                is_anonymous    BIGINT DEFAULT 0,
                reply_content   TEXT DEFAULT '',
                reply_time      TEXT DEFAULT '',
                raw_data        TEXT DEFAULT '{}',
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_reviews_product ON ali_api_reviews(product_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_reviews_rating ON ali_api_reviews(rating)')

    @staticmethod
    def batch_insert(conn, product_id: str, reviews: list) -> int:
        """批量插入评论，返回插入数量"""
        count = 0
        for r in reviews:
            try:
                conn.execute('''
                    INSERT INTO ali_api_reviews
                        (product_id, review_id, buyer_name, rating, content,
                         review_time, spec_info, images, is_anonymous,
                         reply_content, reply_time, raw_data)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(review_id) DO NOTHING
                ''', (
                    product_id,
                    r.get('review_id', ''),
                    r.get('buyer_name', ''),
                    r.get('rating', 5),
                    r.get('content', ''),
                    r.get('review_time', ''),
                    r.get('spec_info', ''),
                    json.dumps(r.get('images', []), ensure_ascii=False),
                    1 if r.get('is_anonymous') else 0,
                    r.get('reply_content', ''),
                    r.get('reply_time', ''),
                    json.dumps(r.get('raw_data', {}), ensure_ascii=False),
                ))
                count += 1
            except Exception:
                continue
        conn.commit()
        return count

    @staticmethod
    def get_by_product(conn, product_id: str, limit: int = 20, offset: int = 0) -> list:
        rows = conn.execute(
            'SELECT * FROM ali_api_reviews WHERE product_id = %s ORDER BY review_time DESC LIMIT %s OFFSET %s',
            (product_id, limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_stats(conn, product_id: str) -> dict:
        """获取评论统计"""
        row = conn.execute('''
            SELECT COUNT(*) as total,
                   AVG(rating) as avg_rating,
                   SUM(CASE WHEN rating >= 4 THEN 1 ELSE 0 END) as positive,
                   SUM(CASE WHEN rating = 3 THEN 1 ELSE 0 END) as neutral,
                   SUM(CASE WHEN rating <= 2 THEN 1 ELSE 0 END) as negative
            FROM ali_api_reviews WHERE product_id = %s
        ''', (product_id,)).fetchone()
        return dict(row) if row else {'total': 0, 'avg_rating': 0, 'positive': 0, 'neutral': 0, 'negative': 0}


class AliApiLog:
    """API调用日志"""
    
    @staticmethod
    def create_table(conn):
        """创建API日志表"""
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ali_api_logs (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id BIGINT,
                api_key_id BIGINT,
                endpoint TEXT NOT NULL,
                params TEXT DEFAULT '{}',
                response_code BIGINT,
                response_time BIGINT,
                success BIGINT DEFAULT 0,
                error_msg TEXT,
                ip_address TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_logs_user ON ali_api_logs(user_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_logs_endpoint ON ali_api_logs(endpoint)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_logs_created ON ali_api_logs(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_logs_success ON ali_api_logs(success)')
    
    @staticmethod
    def log_request(conn, log_data: Dict[str, Any]) -> int:
        """记录API请求日志"""
        cursor = conn.execute('''
            INSERT INTO ali_api_logs (
                user_id, api_key_id, endpoint, params,
                response_code, response_time, success,
                error_msg, ip_address, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (
            log_data.get('user_id'),
            log_data.get('api_key_id'),
            log_data.get('endpoint', ''),
            json.dumps(log_data.get('params', {}), ensure_ascii=False),
            log_data.get('response_code'),
            log_data.get('response_time'),
            1 if log_data.get('success', False) else 0,
            log_data.get('error_msg', ''),
            log_data.get('ip_address', ''),
            datetime.now().isoformat()
        ))
        return cursor.fetchone()['id']
    
    @staticmethod
    def get_stats(conn, hours: int = 24) -> Dict[str, Any]:
        """获取统计信息"""
        # 总调用次数
        total = conn.execute('SELECT COUNT(*) as count FROM ali_api_logs').fetchone()['count']
        
        # 成功/失败次数
        success = conn.execute('SELECT COUNT(*) as count FROM ali_api_logs WHERE success = 1').fetchone()['count']
        failed = conn.execute('SELECT COUNT(*) as count FROM ali_api_logs WHERE success = 0').fetchone()['count']
        
        # 最近24小时调用
        recent = conn.execute('''
            SELECT COUNT(*) as count FROM ali_api_logs 
            WHERE created_at > NOW() - INTERVAL '%s hours'
        ''', (str(hours),)).fetchone()['count']
        
        # 按端点统计
        endpoint_stats = conn.execute('''
            SELECT endpoint, COUNT(*) as count, 
                   AVG(response_time) as avg_time,
                   SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count
            FROM ali_api_logs 
            GROUP BY endpoint 
            ORDER BY count DESC
        ''').fetchall()
        
        return {
            'total_calls': total,
            'success_calls': success,
            'failed_calls': failed,
            'recent_calls_24h': recent,
            'success_rate': success / total if total > 0 else 0,
            'endpoint_stats': [dict(row) for row in endpoint_stats]
        }

class AliApiUserStats:
    """用户调用统计"""
    
    @staticmethod
    def create_table(conn):
        """创建用户统计表"""
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ali_api_user_stats (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id BIGINT UNIQUE,
                calls_today BIGINT DEFAULT 0,
                calls_total BIGINT DEFAULT 0,
                last_reset_date TEXT,
                last_call_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_user_stats_user ON ali_api_user_stats(user_id)')
    
    @staticmethod
    def increment_user_calls(conn, user_id: int) -> bool:
        """增加用户调用计数，检查是否超限"""
        from .config import config
        daily_limit = config['rate_limit']['user_daily_limit']
        
        today = datetime.now().strftime('%Y-%m-%d')
        
        # 获取或创建用户统计
        row = conn.execute(
            'SELECT * FROM ali_api_user_stats WHERE user_id = %s', 
            (user_id,)
        ).fetchone()
        
        if not row:
            # 新用户
            conn.execute('''
                INSERT INTO ali_api_user_stats (user_id, calls_today, calls_total, last_reset_date, last_call_at)
                VALUES (%s, 1, 1, %s, %s)
            ''', (user_id, today, datetime.now().isoformat()))
            return True
        
        # 检查是否需要重置
        if row['last_reset_date'] != today:
            # 重置每日计数
            conn.execute('''
                UPDATE ali_api_user_stats SET
                    calls_today = 1,
                    calls_total = calls_total + 1,
                    last_reset_date = %s,
                    last_call_at = %s,
                    updated_at = %s
                WHERE user_id = %s
            ''', (today, datetime.now().isoformat(), datetime.now().isoformat(), user_id))
            return True
        
        # 检查是否超限
        if row['calls_today'] >= daily_limit:
            return False
        
        # 增加计数
        conn.execute('''
            UPDATE ali_api_user_stats SET
                calls_today = calls_today + 1,
                calls_total = calls_total + 1,
                last_call_at = %s,
                updated_at = %s
            WHERE user_id = %s
        ''', (datetime.now().isoformat(), datetime.now().isoformat(), user_id))
        return True
    
    @staticmethod
    def get_user_stats(conn, user_id: int) -> Optional[Dict[str, Any]]:
        """获取用户统计信息"""
        row = conn.execute(
            'SELECT * FROM ali_api_user_stats WHERE user_id = %s', 
            (user_id,)
        ).fetchone()
        if not row:
            return None
        return dict(row)


class AliApiToken:
    """1688 OAuth Token 存储"""
    
    @staticmethod
    def create_table(conn):
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ali_api_tokens (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT DEFAULT 0,
                app_key         TEXT NOT NULL DEFAULT '',
                access_token    TEXT NOT NULL DEFAULT '',
                refresh_token   TEXT NOT NULL DEFAULT '',
                ali_id          TEXT DEFAULT '',
                resource_owner  TEXT DEFAULT '',
                expires_in      BIGINT DEFAULT 0,
                token_type      TEXT DEFAULT 'bearer',
                scope           TEXT DEFAULT '',
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
    
    @staticmethod
    def save(conn, token_data: dict, user_id: int = 0):
        now = datetime.now().isoformat()
        conn.execute('DELETE FROM ali_api_tokens WHERE user_id=%s', (user_id,))
        conn.execute('''
            INSERT INTO ali_api_tokens 
                (user_id, app_key, access_token, refresh_token, ali_id, 
                 resource_owner, expires_in, token_type, scope, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (
            user_id,
            token_data.get('app_key', ''),
            token_data.get('access_token', ''),
            token_data.get('refresh_token', ''),
            token_data.get('ali_id', ''),
            token_data.get('resource_owner', ''),
            token_data.get('expires_in', 0),
            token_data.get('token_type', 'bearer'),
            token_data.get('scope', ''),
            now, now
        ))
        conn.commit()
    
    @staticmethod
    def get(conn, user_id: int = 0):
        row = conn.execute(
            'SELECT * FROM ali_api_tokens WHERE user_id=%s ORDER BY id DESC LIMIT 1',
            (user_id,)
        ).fetchone()
        return dict(row) if row else None
    
    @staticmethod
    def delete(conn, user_id: int = 0):
        conn.execute('DELETE FROM ali_api_tokens WHERE user_id=%s', (user_id,))
        conn.commit()


class OAuthState:
    """OAuth state 存储（CSRF 防护）"""
    
    @staticmethod
    def create_table(conn):
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ali_oauth_states (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                state       TEXT UNIQUE NOT NULL,
                redirect_uri TEXT NOT NULL DEFAULT '',
                user_id     BIGINT DEFAULT 0,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                used        BIGINT DEFAULT 0
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_oauth_state ON ali_oauth_states(state)')
    
    @staticmethod
    def save(conn, state: str, redirect_uri: str, user_id: int = 0):
        conn.execute(
            'INSERT INTO ali_oauth_states (state, redirect_uri, user_id) VALUES (%s, %s, %s)',
            (state, redirect_uri, user_id)
        )
        conn.commit()
    
    @staticmethod
    def validate_and_consume(conn, state: str, max_age_seconds: int = 600) -> bool:
        """验证 state 并标记为已使用（防重放攻击）"""
        row = conn.execute(
            'SELECT * FROM ali_oauth_states WHERE state = %s AND used = 0', (state,)
        ).fetchone()
        if not row:
            return False
        # 检查是否过期
        from datetime import datetime, timedelta
        created = datetime.fromisoformat(row['created_at'])
        if datetime.now() - created > timedelta(seconds=max_age_seconds):
            conn.execute('DELETE FROM ali_oauth_states WHERE id = %s', (row['id'],))
            conn.commit()
            return False
        # 标记已使用
        conn.execute('UPDATE ali_oauth_states SET used = 1 WHERE id = %s', (row['id'],))
        conn.commit()
        return True

    @staticmethod
    def validate_and_consume_row(conn, state: str, max_age_seconds: int = 600):
        """验证 state 并返回记录（含 user_id / redirect_uri），失败返回 None。

        用于 OAuth 回调：1688 跨站跳转不携带 admin cookie，
        改由 state 记录中保存的 user_id / redirect_uri 完成鉴权与校验。
        """
        row = conn.execute(
            'SELECT * FROM ali_oauth_states WHERE state = %s AND used = 0', (state,)
        ).fetchone()
        if not row:
            return None
        from datetime import datetime, timedelta
        created = datetime.fromisoformat(row['created_at'])
        if datetime.now() - created > timedelta(seconds=max_age_seconds):
            conn.execute('DELETE FROM ali_oauth_states WHERE id = %s', (row['id'],))
            conn.commit()
            return None
        conn.execute('UPDATE ali_oauth_states SET used = 1 WHERE id = %s', (row['id'],))
        conn.commit()
        return dict(row)

    @staticmethod
    def clean_expired(conn, max_age_seconds: int = 3600):
        """清理过期 state"""
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(seconds=max_age_seconds)).isoformat()
        conn.execute('DELETE FROM ali_oauth_states WHERE created_at < %s', (cutoff,))
        conn.commit()


class AliApiConfig:
    """插件自有配置表——替代旧 system_config 中的 alibaba_* 记录

    设计目的：
      插件化后配置不再依赖主项目的 system_config 表，
      改用自有 ali_api_config 表，卸载时一键清理零残留。
    """

    # 需要从旧 system_config 迁移的 key 列表
    SYSTEM_CONFIG_KEYS = [
        'alibaba_app_key',
        'alibaba_app_secret',
        'alibaba_api_gateway',
        'alibaba_redirect_domains',
    ]

    @staticmethod
    def create_table(conn):
        """创建配置表"""
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ali_api_config (
                key             TEXT PRIMARY KEY,
                value           TEXT NOT NULL DEFAULT '',
                description     TEXT DEFAULT '',
                encrypted       BIGINT DEFAULT 0,
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        ''')

    @staticmethod
    def get(conn, key: str, default: str = '') -> str:
        """获取配置值"""
        row = conn.execute(
            'SELECT value FROM ali_api_config WHERE key = %s', (key,)
        ).fetchone()
        return row['value'] if row else default

    @staticmethod
    def set(conn, key: str, value: str, description: str = '',
            encrypted: int = 0):
        """设置配置值（UPSERT）"""
        conn.execute('''
            INSERT INTO ali_api_config (key, value, description, encrypted, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                description = COALESCE(excluded.description, ali_api_config.description),
                encrypted = COALESCE(excluded.encrypted, ali_api_config.encrypted),
                updated_at = NOW()
        ''', (key, value, description, encrypted))

    @staticmethod
    def delete(conn, key: str):
        """删除配置"""
        conn.execute('DELETE FROM ali_api_config WHERE key = %s', (key,))

    @staticmethod
    def get_all(conn) -> Dict[str, str]:
        """获取全部配置"""
        rows = conn.execute('SELECT key, value FROM ali_api_config').fetchall()
        return {r['key']: r['value'] for r in rows}

    @staticmethod
    def migrate_from_system_config(conn) -> bool:
        """从旧 system_config（主库）迁移 alibaba_* 配置到 ali_api_config（独立库）

        读取走主库只读连接 get_main_db()，写入走传入的独立库 conn。
        幂等：已迁移过的不会重复迁移（检查 _migrated 标记）。
        返回 True 表示发生了迁移，False 表示无需迁移。
        """
        # 检查是否已迁移
        already = conn.execute(
            "SELECT value FROM ali_api_config WHERE key = '_migrated_from_system_config'"
        ).fetchone()
        if already:
            return False

        # 从主库读取 alibaba_* 配置（主库不可用/无表则视为无需迁移）
        rows_map = {}
        try:
            from .models import get_main_db
            with get_main_db() as main_conn:
                table_check = main_conn.execute(
                    "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' AND tablename='system_config'"
                ).fetchone()
                if table_check:
                    for key in AliApiConfig.SYSTEM_CONFIG_KEYS:
                        row = main_conn.execute(
                            'SELECT value, description FROM system_config WHERE key = %s', (key,)
                        ).fetchone()
                        if row and row['value']:
                            rows_map[key] = (row['value'], row['description'] or '')
        except Exception:
            rows_map = {}

        # 写入独立库
        migrated = 0
        for key, (val, desc) in rows_map.items():
            is_encrypted = 1 if key in ('alibaba_app_secret',) else 0
            AliApiConfig.set(conn, key, val, description=desc, encrypted=is_encrypted)
            migrated += 1

        # 打迁移标记
        AliApiConfig.set(conn, '_migrated_from_system_config',
                         '1', '迁移标记（勿删）')
        conn.commit()

        if migrated:
            logger.info(f'[AliApi] √ 已从 system_config 迁移 {migrated} 条配置到 ali_api_config')
        return True


class AliPurchaseOrder:
    """1688 代发采购单 — 本地订单与 1688 采购订单的关联记录"""

    @staticmethod
    def create_table(conn):
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ali_purchase_orders (
                id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                local_order_id      TEXT NOT NULL,
                local_order_item_id BIGINT NOT NULL,
                product_id          BIGINT NOT NULL,
                ali_product_id      TEXT NOT NULL,
                ali_sku_id          TEXT DEFAULT '',
                quantity            BIGINT DEFAULT 1,
                price               DOUBLE PRECISION DEFAULT 0,
                total_fee           DOUBLE PRECISION DEFAULT 0,
                ali_order_id        TEXT DEFAULT '',
                ali_order_status    TEXT DEFAULT 'pending',
                supplier_name       TEXT DEFAULT '',
                supplier_id         TEXT DEFAULT '',
                tracking_company    TEXT DEFAULT '',
                tracking_number     TEXT DEFAULT '',
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                ordered_at          TIMESTAMPTZ,
                shipped_at          TIMESTAMPTZ,
                remark              TEXT DEFAULT '',
                UNIQUE(local_order_item_id, ali_product_id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_po_order ON ali_purchase_orders(local_order_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_po_status ON ali_purchase_orders(ali_order_status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_po_ali_order ON ali_purchase_orders(ali_order_id)')

    @staticmethod
    def insert(conn, data: dict) -> int:
        cursor = conn.execute('''
            INSERT INTO ali_purchase_orders
                (local_order_id, local_order_item_id, product_id,
                 ali_product_id, ali_sku_id, quantity, price, total_fee,
                 supplier_name, supplier_id, remark)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        ''', (
            data['local_order_id'], data['local_order_item_id'], data['product_id'],
            data['ali_product_id'], data.get('ali_sku_id',''), data.get('quantity',1),
            data.get('price',0), data.get('total_fee',0),
            data.get('supplier_name',''), data.get('supplier_id',''),
            data.get('remark',''),
        ))
        return cursor.fetchone()['id']

    @staticmethod
    def get_by_id(conn, po_id: int) -> Optional[dict]:
        row = conn.execute('SELECT * FROM ali_purchase_orders WHERE id=%s', (po_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_by_local_item(conn, local_order_item_id: int) -> Optional[dict]:
        row = conn.execute('SELECT * FROM ali_purchase_orders WHERE local_order_item_id=%s',
                           (local_order_item_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def list_orders(conn, status: str = '', limit: int = 50, offset: int = 0) -> tuple:
        where = ''
        params = []
        if status and status != 'all':
            where = 'WHERE ali_order_status=%s'
            params.append(status)
        total = conn.execute(f'SELECT COUNT(*) as c FROM ali_purchase_orders {where}',
                             params).fetchone()['c']
        rows = conn.execute(
            f'SELECT * FROM ali_purchase_orders {where} ORDER BY created_at DESC LIMIT %s OFFSET %s',
            params + [limit, offset]
        ).fetchall()
        return [dict(r) for r in rows], total

    @staticmethod
    def update_order(conn, po_id: int, **updates) -> bool:
        allowed = {'ali_order_id','ali_order_status','ali_sku_id','price','total_fee',
                   'tracking_company','tracking_number','ordered_at','shipped_at','remark',
                   'supplier_name','supplier_id','quantity'}
        sets = {k:v for k,v in updates.items() if k in allowed}
        if not sets:
            return False
        set_clause = ','.join(f'{k}=%s' for k in sets)
        vals = list(sets.values()) + [po_id]
        conn.execute(f'UPDATE ali_purchase_orders SET {set_clause} WHERE id=%s', vals)
        return conn.rowcount > 0


# ===== 数据库初始化 =====

# 本插件全部数据表（用于遗留数据迁移）
ALI_TABLES = [
    'ali_api_items', 'ali_api_reviews', 'ali_api_logs', 'ali_api_user_stats',
    'ali_api_tokens', 'ali_oauth_states', 'ali_api_config',
]


def migrate_data_from_main_db():
    """一次性把主库中遗留的 ali_api_* 表数据复制到 PG ali_api schema。

    - 幂等：PG schema 中某表已有数据则跳过该表。
    - 非破坏：只读主库、只写 PG，绝不删除或修改主库任何数据。
    - 脱离主项目运行（无法 import 主库）时静默跳过。
    """
    try:
        with get_main_db() as main_conn:
            with get_db() as local_conn:
                for t in ALI_TABLES:
                    # 主库该表存在才迁移
                    if not main_conn.execute(
                        "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' AND tablename=%s", (t,)
                    ).fetchone():
                        continue
                    # PG schema 已有数据 → 跳过（幂等）
                    local_cnt = local_conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()['c']
                    if local_cnt > 0:
                        continue
                    src_rows = main_conn.execute(f"SELECT * FROM {t}").fetchall()
                    if not src_rows:
                        continue
                    # 只复制两库都存在的列，避免结构差异导致失败
                    local_cols = [r['column_name'] for r in local_conn.execute(
                        f"SELECT column_name FROM information_schema.columns WHERE table_name=%s", (t,)
                    ).fetchall()]
                    main_cols = [r['column_name'] for r in main_conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name=%s", (t,)
                    ).fetchall()]
                    cols = [c for c in main_cols if c in local_cols]
                    if not cols:
                        continue
                    collist = ','.join(cols)
                    placeholders = ','.join('%s' for _ in cols)
                    for row in src_rows:
                        local_conn.execute(
                            f"INSERT INTO {t} ({collist}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                            [row[c] for c in cols],
                        )
                    logger.info(f"[AliApi] 迁移遗留数据 {t}: {len(src_rows)} 行 → PG schema ali_api")
                local_conn.commit()
    except ImportError:
        pass  # 脱离主项目，无需迁移
    except Exception as e:
        logger.warning(f"[AliApi] 遗留数据迁移跳过/失败（不影响运行）: {e}")


class AgentRegistry:
    """插件本地 Agent 注册表（替代主库 agent_matrix 写入）"""

    @staticmethod
    def create_table(conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS agent_registry (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name            TEXT NOT NULL,
            identifier      TEXT DEFAULT '',
            role_type       TEXT DEFAULT 'sub',
            description     TEXT DEFAULT '',
            domain          TEXT DEFAULT 'supply_chain',
            provider        TEXT DEFAULT '',
            model_name      TEXT DEFAULT '',
            system_prompt   TEXT DEFAULT '',
            capabilities    TEXT DEFAULT '[]',
            is_active       BIGINT DEFAULT 1,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ali_agent_registry_id ON agent_registry(identifier)')


class SchemaMeta:
    """Schema 版本跟踪表（§10.6）"""

    @staticmethod
    def create_table(conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_meta (
            key         TEXT PRIMARY KEY,
            value       TEXT DEFAULT '',
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )''')


def upsert_agent(name: str, role_type: str, description: str, domain: str,
                 provider: str, model_name: str, system_prompt: str,
                 capabilities: str, is_active: int = 1, identifier: str = ''):
    """注册或更新本地 Agent（幂等，§4）"""
    with get_db() as conn:
        exists = conn.execute(
            'SELECT id FROM agent_registry WHERE name=%s AND role_type=%s',
            (name, role_type)
        ).fetchone()
        if exists:
            conn.execute('''
                UPDATE agent_registry
                SET description=%s, domain=%s, provider=%s, model_name=%s,
                    system_prompt=%s, capabilities=%s, is_active=%s,
                    identifier=%s, updated_at=NOW()
                WHERE id=%s
            ''', (description, domain, provider, model_name,
                  system_prompt, capabilities, is_active, identifier, exists['id']))
        else:
            conn.execute('''
                INSERT INTO agent_registry
                (name, identifier, role_type, description, domain, provider, model_name,
                 system_prompt, capabilities, is_active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ''', (name, identifier, role_type, description, domain, provider, model_name,
                  system_prompt, capabilities, is_active))
        conn.commit()


def unregister_agents():
    """注销插件本地注册的所有 Agent（§4.2 禁用/卸载流程）"""
    with get_db() as conn:
        conn.execute('DELETE FROM agent_registry')
        conn.commit()


def get_schema_version() -> str:
    """从 schema_meta 表读取当前 schema 版本（§10.6）"""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            return row['value'] if row else '0.0.0'
    except Exception:
        return '0.0.0'


def set_schema_version(version: str):
    """写入当前 schema 版本（§10.6）"""
    with get_db() as conn:
        conn.execute('''
            INSERT INTO schema_meta (key, value, updated_at)
            VALUES ('schema_version', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        ''', (version,))
        conn.commit()


def init_tables():
    """初始化 PG schema 所有表，并迁移遗留数据与配置"""
    with get_db() as conn:
        AliApiItem.create_table(conn)
        AliApiItem.migrate_b2b_fields(conn)  # 幂等迁移 B2B 字段
        AliApiReview.create_table(conn)
        AliApiLog.create_table(conn)
        AliApiUserStats.create_table(conn)
        AliApiToken.create_table(conn)
        OAuthState.create_table(conn)
        AliApiConfig.create_table(conn)
        AliPurchaseOrder.create_table(conn)
        AgentRegistry.create_table(conn)
        SchemaMeta.create_table(conn)
        conn.commit()
    # 复制主库遗留的 ali_api_* 数据（幂等、非破坏）
    migrate_data_from_main_db()
    # 迁移旧 system_config 配置到独立库
    with get_db() as conn:
        try:
            AliApiConfig.migrate_from_system_config(conn)
        except Exception as e:
            logger.warning(f"[AliApi] 配置迁移跳过: {e}")
    logger.info("[AliApi] 数据表初始化完成（PG schema ali_api）")

if __name__ == "__main__":
    # 测试数据库初始化
    init_tables()
    print("AliApi 数据表初始化完成")
