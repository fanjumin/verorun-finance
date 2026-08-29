#!/usr/bin/env python3
"""
Dashboard Service — Modular dashboard data provider

Provides independent query methods for each dashboard widget,
with per-widget caching and fallback handling.

Usage:
    from services.dashboard_service import DashboardService
    svc = DashboardService()
    data = svc.get_full_dashboard_data()
    widget = svc.get_widget_data('token_spend')
    realtime = svc.get_realtime_data()
"""

import time
import socket
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional

from models import get_db
from plugin_manager.hooks import get_hook_registry


class DashboardService:
    """Modular dashboard data provider with per-widget caching."""

    def __init__(self):
        # Per-widget cache: {key: {'data': ..., 'ts': timestamp}}
        self._cache: Dict[str, Dict[str, Any]] = {}
        
    # ── Cache Management ──────────────────────────────────────────────

    def _get_cached(self, key: str, ttl: int) -> Optional[Any]:
        """Get cached data if still valid."""
        entry = self._cache.get(key)
        if entry and (time.time() - entry['ts']) < ttl:
            return entry['data']
        return None

    def _set_cache(self, key: str, data: Any):
        """Store data in cache."""
        self._cache[key] = {'data': data, 'ts': time.time()}

    def _clear_cache(self):
        """Clear all caches."""
        self._cache.clear()

    # ── Helper Methods ──────────────────────────────────────────────────

    @staticmethod
    def _safe_execute(conn, sql: str, params: tuple = ()) -> Optional[Any]:
        """Execute query safely, returns dict row or None."""
        try:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None
        except:
            try:
                conn._conn.rollback()
            except:
                pass
            return None

    @staticmethod
    def _safe_execute_all(conn, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Execute query safely, returns list of dicts."""
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except:
            try:
                conn._conn.rollback()
            except:
                pass
            return []

    @staticmethod
    def _safe_count(conn, sql: str, params: tuple = ()) -> int:
        """Execute COUNT query safely, returns int."""
        row = DashboardService._safe_execute(conn, sql, params)
        return row.get('c', 0) if row else 0

    # ── Core Metrics (TTL: 30s) ────────────────────────────────────────

    def get_users_data(self) -> Dict[str, Any]:
        """Get user statistics."""
        cache_key = 'users'
        cached = self._get_cached(cache_key, 30)
        if cached is not None:
            return cached

        data = {
            'total_users': 0,
            'active_users': 0,
            'today_new_users': 0,
            'recent_users': [],
        }
        try:
            with get_db() as conn:
                row = self._safe_execute(conn,
                    "SELECT COUNT(*) as c, COALESCE(SUM(active),0) as a, "
                    "COALESCE(SUM(CASE WHEN created_at>=CURRENT_DATE THEN 1 ELSE 0 END),0) as n "
                    "FROM users")
                if row:
                    data['total_users'] = row.get('c', 0)
                    data['active_users'] = row.get('a', 0)
                    data['today_new_users'] = row.get('n', 0)
                data['recent_users'] = self._safe_execute_all(conn,
                    "SELECT id, COALESCE(display_name, username, '') as nickname, phone, created_at "
                    "FROM users ORDER BY created_at DESC LIMIT 5")
        except:
            pass

        self._set_cache(cache_key, data)
        return data

    def get_agents_data(self) -> Dict[str, Any]:
        """Get agent statistics."""
        cache_key = 'agents'
        cached = self._get_cached(cache_key, 30)
        if cached is not None:
            return cached

        data = {
            'total_agents': 0,
            'active_agents': 0,
            'today_calls': 0,
            'total_calls': 0,
            'today_tokens': 0,
            'top_token_agents': [],
        }
        try:
            with get_db() as conn:
                row = self._safe_execute(conn, "SELECT COUNT(*) as c FROM agent_matrix")
                data['total_agents'] = row.get('c', 0) if row else 0

                row = self._safe_execute(conn, "SELECT COUNT(*) as c FROM agent_matrix WHERE is_active=1")
                data['active_agents'] = row.get('c', 0) if row else 0

                row = self._safe_execute(conn,
                    "SELECT COUNT(*) as c FROM agent_token_logs WHERE date(created_at)=CURRENT_DATE")
                data['today_calls'] = row.get('c', 0) if row else 0

                row = self._safe_execute(conn, "SELECT COUNT(*) as c FROM agent_token_logs")
                data['total_calls'] = row.get('c', 0) if row else 0

                row = self._safe_execute(conn,
                    "SELECT COALESCE(SUM(tokens),0) as t FROM agent_token_logs WHERE date(created_at)=CURRENT_DATE")
                data['today_tokens'] = row.get('t', 0) if row else 0

                data['top_token_agents'] = self._safe_execute_all(conn, """
                    SELECT 
                        a.agent_id, 
                        COALESCE(m.name, 'Agent#'||a.agent_id) as agent_name, 
                        SUM(a.tokens) as total 
                    FROM agent_token_logs a 
                    LEFT JOIN agent_matrix m ON a.agent_id = m.id 
                    GROUP BY a.agent_id 
                    ORDER BY total DESC 
                    LIMIT 5
                """)
        except:
            pass

        self._set_cache(cache_key, data)
        return data

    # ── Business Metrics (TTL: 60s) ────────────────────────────────────

    def get_revenue_data(self) -> Dict[str, Any]:
        """Get revenue and subscription statistics."""
        cache_key = 'revenue'
        cached = self._get_cached(cache_key, 60)
        if cached is not None:
            return cached

        data = {
            'monthly_revenue': 0,
            'active_subscriptions': 0,
            'total_orders': 0,
            'recent_orders': [],
            'revenue_trend_30d': [],
        }
        try:
            with get_db() as conn:
                row = self._safe_execute(conn, "SELECT COUNT(*) as c FROM subscription.user_subscriptions WHERE status='active'")
                data['active_subscriptions'] = row.get('c', 0) if row else 0

                row = self._safe_execute(conn, "SELECT COUNT(*) as c FROM billing_orders")
                data['total_orders'] = row.get('c', 0) if row else 0

                thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).strftime('%Y-%m-%d')

                # 近30天收入（三表合并，与 /admin/revenue/dashboard 口径一致）
                revenue = 0.0
                row = self._safe_execute(conn,
                    "SELECT COALESCE(SUM(amount),0) as c FROM billing_orders "
                    "WHERE status='paid' AND paid_at>=%s", (thirty_days_ago,))
                revenue += float(row.get('c', 0)) if row else 0
                row = self._safe_execute(conn,
                    "SELECT COALESCE(SUM(amount_fen)/100.0,0) as c FROM subscription.sub_orders "
                    "WHERE status='paid' AND paid_at>=%s", (thirty_days_ago,))
                revenue += float(row.get('c', 0)) if row else 0
                row = self._safe_execute(conn,
                    "SELECT COALESCE(SUM(subtotal),0) as c FROM order_items "
                    "WHERE status='paid' AND paid_at>=%s", (thirty_days_ago,))
                revenue += float(row.get('c', 0)) if row else 0
                data['monthly_revenue'] = round(revenue, 2)

                data['recent_orders'] = self._safe_execute_all(conn,
                    "SELECT id, user_id, item_desc, amount, status, paid_at "
                    "FROM billing_orders ORDER BY created_at DESC LIMIT 5")

                # 近30天每日收入趋势（三表合并，按日期累加）
                trend_map: Dict[str, float] = {}
                trend_sqls = [
                    "SELECT date(paid_at) as date, SUM(amount) as revenue "
                    "FROM billing_orders WHERE status='paid' AND paid_at>=%s GROUP BY date(paid_at)",
                    "SELECT date(paid_at) as date, COALESCE(SUM(amount_fen)/100.0,0) as revenue "
                    "FROM subscription.sub_orders WHERE status='paid' AND paid_at>=%s GROUP BY date(paid_at)",
                    "SELECT date(paid_at) as date, COALESCE(SUM(subtotal),0) as revenue "
                    "FROM order_items WHERE status='paid' AND paid_at>=%s GROUP BY date(paid_at)",
                ]
                for sql in trend_sqls:
                    for r in self._safe_execute_all(conn, sql, (thirty_days_ago,)):
                        trend_map[r['date']] = trend_map.get(r['date'], 0.0) + float(r['revenue'] or 0)
                data['revenue_trend_30d'] = [
                    {'date': d, 'revenue': round(v, 2)} for d, v in sorted(trend_map.items())
                ]
        except:
            pass

        self._set_cache(cache_key, data)
        return data

    def get_pending_data(self) -> Dict[str, Any]:
        """Get pending items and action items."""
        cache_key = 'pending'
        cached = self._get_cached(cache_key, 30)
        if cached is not None:
            return cached

        data = {
            'pending_posts': 0,
            'pending_reviews': 0,
            'pending_contacts': 0,
            'today_failed_tasks': 0,
            'open_tickets': 0,
            'urgent_tickets': 0,
            'pending_feedback': 0,
            'pending_shipments': 0,
        }
        try:
            with get_db() as conn:
                data['pending_posts'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM agent_experiences WHERE status='pending' OR is_published=0")

                data['pending_reviews'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM processed_contents WHERE status='review'")

                data['pending_contacts'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM contact_messages WHERE status='unread'")

                data['today_failed_tasks'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM execution_logs WHERE status='failed' AND created_at>=CURRENT_DATE")

                data['open_tickets'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM user_tickets WHERE status='open'")

                data['urgent_tickets'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM user_tickets WHERE status='open' AND priority='high'")

                data['pending_feedback'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM user_feedback WHERE status='pending'")

                data['pending_shipments'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM shop.order_items WHERE shipping_status='pending'")
        except:
            pass

        self._set_cache(cache_key, data)
        return data

    def get_content_data(self) -> Dict[str, Any]:
        """Get content and publishing statistics."""
        cache_key = 'content'
        cached = self._get_cached(cache_key, 60)
        if cached is not None:
            return cached

        data = {
            'published_posts': 0,
            'draft_posts': 0,
            'pending_comments': 0,
            'knowledge_docs': 0,
        }
        try:
            with get_db() as conn:
                row = self._safe_execute(conn, "SELECT COUNT(*) as c FROM cms_posts WHERE is_published=true")
                data['published_posts'] = row.get('c', 0) if row else 0

                row = self._safe_execute(conn, "SELECT COUNT(*) as c FROM cms_posts WHERE is_published=false")
                data['draft_posts'] = row.get('c', 0) if row else 0

                data['pending_comments'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM comments WHERE status='pending'")

                data['knowledge_docs'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM knowledge_chunks")
        except:
            pass

        self._set_cache(cache_key, data)
        return data

    def get_products_data(self) -> Dict[str, Any]:
        """Get product statistics."""
        cache_key = 'products'
        cached = self._get_cached(cache_key, 60)
        if cached is not None:
            return cached

        data = {'total_products': 0}
        try:
            with get_db() as conn:
                row = self._safe_execute(conn, "SELECT COUNT(*) as c FROM shop.products")
                data['total_products'] = row.get('c', 0) if row else 0
        except:
            pass

        self._set_cache(cache_key, data)
        return data

    # ── Plugin & System Data (TTL: 120s) ────────────────────────────────

    def get_plugin_status(self) -> Dict[str, Any]:
        """Get plugin status counts via hooks."""
        cache_key = 'plugins'
        cached = self._get_cached(cache_key, 120)
        if cached is not None:
            return cached

        data = {
            'active_plugins': 0,
            'total_plugins': 0,
        }
        try:
            # Use hook system to get plugin count
            with get_db() as conn:
                # We can count from plugin_registry or use hooks
                row = self._safe_execute(conn, "SELECT COUNT(*) as c FROM plugin_registry")
                data['total_plugins'] = row.get('c', 0) if row else 0
                row = self._safe_execute(conn, "SELECT COUNT(*) as c FROM plugin_registry WHERE is_enabled = 1")
                data['active_plugins'] = row.get('c', 0) if row else 0
        except:
            pass

        self._set_cache(cache_key, data)
        return data

    def get_email_queue(self) -> Dict[str, Any]:
        """Get email queue status."""
        cache_key = 'email'
        cached = self._get_cached(cache_key, 60)
        if cached is not None:
            return cached

        data = {
            'pending_emails': 0,
            'sent_today': 0,
        }
        try:
            with get_db() as conn:
                data['pending_emails'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM emails WHERE status='pending'")
                data['sent_today'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM emails WHERE status='sent' AND date(sent_at)=CURRENT_DATE")
        except:
            pass

        self._set_cache(cache_key, data)
        return data

    def get_sms_usage(self) -> Dict[str, Any]:
        """Get SMS usage statistics."""
        cache_key = 'sms'
        cached = self._get_cached(cache_key, 60)
        if cached is not None:
            return cached

        data = {
            'sms_sent_today': 0,
            'sms_total': 0,
        }
        try:
            with get_db() as conn:
                data['sms_sent_today'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM sms_logs WHERE date(created_at)=CURRENT_DATE")
                data['sms_total'] = self._safe_count(conn,
                    "SELECT COUNT(*) as c FROM sms_logs")
        except:
            pass

        self._set_cache(cache_key, data)
        return data

    # ── Service Health (TTL: 10s) ──────────────────────────────────────

    def get_service_status(self) -> List[Dict[str, Any]]:
        """Check service health via socket connections."""
        cache_key = 'services'
        cached = self._get_cached(cache_key, 10)
        if cached is not None:
            return cached

        services = [('Main', 8081), ('Platform', 8083), ('Admin', 8084)]
        data = []

        def _check_service(name, port):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.15)
                result = s.connect_ex(('127.0.0.1', port))
                s.close()
                return {'name': name, 'port': port, 'alive': result == 0}
            except:
                return {'name': name, 'port': port, 'alive': False}

        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(_check_service, n, p): (n, p) for n, p in services}
            for f in as_completed(futures):
                data.append(f.result())

        self._set_cache(cache_key, data)
        return data

    # ── Plugin Data Injection ──────────────────────────────────────────

    def get_plugin_injected_data(self, base_data: Dict[str, Any]) -> Dict[str, Any]:
        """Allow plugins to inject additional dashboard data."""
        try:
            with get_db() as conn:
                return get_hook_registry().apply_filters('dashboard.data', base_data, conn=conn)
        except:
            return base_data

    # ── High-Level API Methods ──────────────────────────────────────────

    def get_full_dashboard_data(self) -> Dict[str, Any]:
        """Get all dashboard data for the full dashboard API."""
        # Note: We do NOT clear cache here. Each widget has its own TTL.
        # Calling this method uses the independent caches properly.
        
        data = {}
        data.update(self.get_users_data())
        data.update(self.get_agents_data())
        data.update(self.get_revenue_data())
        data.update(self.get_pending_data())
        data.update(self.get_content_data())
        data.update(self.get_products_data())
        data['services'] = self.get_service_status()
        
        # Initialize missing keys with defaults
        defaults = {
            'today_pv': None, 'today_uv': None, 'online_now': None,
            'health_score': None, 'health_passed': 0, 'health_warnings': 0,
            'health_errors': 0, 'unread_alerts': 0,
            'top_pages': [], 'trend_30d': [], 'health_trend_7d': [],
        }
        for k, v in defaults.items():
            if k not in data:
                data[k] = v

        # Plugin injection
        data = self.get_plugin_injected_data(data)

        return data

    def get_widget_data(self, widget_name: str) -> Dict[str, Any]:
        """Get data for a specific widget."""
        widget_map = {
            'users': self.get_users_data,
            'agents': self.get_agents_data,
            'revenue': self.get_revenue_data,
            'pending': self.get_pending_data,
            'content': self.get_content_data,
            'products': self.get_products_data,
            'plugins': self.get_plugin_status,
            'email': self.get_email_queue,
            'sms': self.get_sms_usage,
            'services': self.get_service_status,
        }

        func = widget_map.get(widget_name)
        if not func:
            return {'success': False, 'error': f'Unknown widget: {widget_name}'}

        try:
            result = func()
            return {'success': True, 'data': result}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def get_realtime_data(self) -> Dict[str, Any]:
        """Get real-time metrics for the dashboard header."""
        # Call full dashboard to get all data (reuses caches)
        # This ensures plugin hooks receive the full data dict as expected
        full_data = self.get_full_dashboard_data()
        
        return {
            'services': full_data.get('services', []),
            'online_now': full_data.get('online_now', 0),
            'today_pv': full_data.get('today_pv', 0),
            'today_uv': full_data.get('today_uv', 0),
            'health_score': full_data.get('health_score', 0),
        }

    def get_summary_data(self) -> Dict[str, Any]:
        """Get lightweight summary for the admin header bar."""
        users = self.get_users_data()
        agents = self.get_agents_data()
        services = self.get_service_status()
        
        return {
            'total_users': users['total_users'],
            'active_users': users['active_users'],
            'total_agents': agents['total_agents'],
            'active_agents': agents['active_agents'],
            'today_calls': agents['today_calls'],
            'services': services,
        }
