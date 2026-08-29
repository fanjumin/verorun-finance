"""收藏/心愿单插件 — 基于 PostgreSQL wishlist schema 的数据隔离架构"""
from typing import List

from plugin_manager.base import BasePlugin
from plugin_manager.event_bus import get_event_bus
from plugin_manager.logger import get_plugin_logger
from plugins._base.db import get_raw_connection

from .models import get_db, get_main_db, init_db

logger = get_plugin_logger('wishlist')


def _current_user_id(request) -> int:
    """从请求中提取当前用户ID（统一 JWT 校验）。"""
    from services.jwt_service import validate_token
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '')
    if not token:
        return None
    payload = validate_token(token)
    if not payload:
        return None
    return payload.get('user_id')


class WishlistPlugin(BasePlugin):
    name = 'wishlist'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'Wishlist — 收藏/心愿单，用户收藏商品、管理心愿清单'
    author = 'VeroRun'
    dependencies = {}

    # ── 生命周期 ──────────────────────────────────────────────

    def on_install(self, registry) -> bool:
        """安装时创建插件 schema 和表。"""
        try:
            init_db()
            logger.info("Wishlist schema and tables created")
            return True
        except Exception as e:
            logger.error(f"Failed to install wishlist: {e}")
            return False

    def on_enable(self, registry) -> bool:
        """启用时无需重复建表（on_install 已创建，禁用不删表，重启用沿用）。"""
        return True

    def on_disable(self, registry) -> bool:
        """禁用时无需清理持久化数据，仅记录日志。"""
        logger.info("Wishlist plugin disabled")
        return True

    def on_uninstall(self, registry) -> bool:
        """卸载时清理独立 schema，确保零残留。"""
        try:
            conn = get_raw_connection()
            cur = conn.cursor()
            cur.execute("DROP SCHEMA IF EXISTS wishlist CASCADE")
            conn.commit()
            cur.close()
            conn.close()
            logger.info("Wishlist schema dropped on uninstall")
            return True
        except Exception as e:
            logger.error(f"Failed to uninstall wishlist: {e}")
            return False

    # ── 路由注册 ──────────────────────────────────────────────

    def register_routes(self) -> List:
        from flask import Blueprint, jsonify, request
        bp = Blueprint('wishlist', __name__, url_prefix='/plugin/wishlist')
        t = self.t  # 插件自有翻译，英文源串查找

        def _require_auth(f):
            """JWT 鉴权装饰器，注入 uid。"""
            from functools import wraps

            @wraps(f)
            def wrapper(*args, **kwargs):
                uid = _current_user_id(request)
                if uid is None:
                    return jsonify({'success': False, 'error': t('Please login first')}), 401
                return f(uid, *args, **kwargs)
            return wrapper

        @bp.route('/api/list', methods=['GET'])
        @_require_auth
        def get_wishlist(uid):
            """获取我的收藏列表（分页 + 商品实时信息）。"""
            try:
                page = request.args.get('page', 1, type=int)
                size = min(request.args.get('size', 20, type=int), 50)
                offset = (page - 1) * size

                with get_db() as conn:
                    total = conn.execute(
                        'SELECT COUNT(*) AS count FROM wishlist WHERE user_id=%s',
                        (uid,)
                    ).fetchone()['count']
                    rows = conn.execute(
                        'SELECT * FROM wishlist WHERE user_id=%s '
                        'ORDER BY created_at DESC LIMIT %s OFFSET %s',
                        (uid, size, offset)
                    ).fetchall()

                # 跨库查商品信息
                product_ids = [r['product_id'] for r in rows if r['product_id']]
                product_map = {}
                if product_ids:
                    with get_main_db() as main:
                        for pid in set(product_ids):
                            p = main.execute(
                                'SELECT id, title, price, original_price, thumbnail, '
                                'stock, is_active, sales_count FROM products WHERE id=%s',
                                (pid,)
                            ).fetchone()
                            if p:
                                product_map[pid] = dict(p)

                items = []
                for r in rows:
                    d = dict(r)
                    p = product_map.get(d['product_id'], {})
                    d['title'] = p.get('title', '')
                    d['price'] = p.get('price', 0)
                    d['original_price'] = p.get('original_price', 0)
                    d['thumbnail'] = p.get('thumbnail', '')
                    d['stock'] = p.get('stock', 0)
                    d['is_active'] = p.get('is_active', 0)
                    d['sales_count'] = p.get('sales_count', 0)
                    items.append(d)

                logger.info(f"User {uid} fetched wishlist: {len(items)} items (page {page})")
                return jsonify({
                    'success': True,
                    'data': {'items': items, 'total': total, 'page': page, 'size': size}
                })
            except Exception as e:
                logger.error(f"get_wishlist error: {e}")
                return jsonify({'success': False, 'error': t('Internal server error')}), 500

        @bp.route('/api/toggle', methods=['POST'])
        @_require_auth
        def toggle_wishlist(uid):
            """切换收藏/取消收藏状态。"""
            try:
                data = request.get_json() or {}
                pid = data.get('product_id')
                if not pid:
                    return jsonify({'success': False, 'error': t('Missing product ID')}), 400

                with get_db() as conn:
                    existing = conn.execute(
                        'SELECT id FROM wishlist WHERE user_id=%s AND product_id=%s',
                        (uid, pid)
                    ).fetchone()
                    if existing:
                        conn.execute('DELETE FROM wishlist WHERE id=%s', (existing['id'],))
                        conn.commit()
                        logger.info(f"User {uid} removed product {pid} from wishlist")
                        get_event_bus().emit('wishlist.updated', user_id=uid,
                                             product_id=pid, action='removed')
                        return jsonify({'success': True, 'favorited': False,
                                        'message': t('Removed from favorites')})
                    else:
                        conn.execute(
                            'INSERT INTO wishlist (user_id, product_id) VALUES (%s,%s)',
                            (uid, pid)
                        )
                        conn.commit()
                        logger.info(f"User {uid} added product {pid} to wishlist")
                        get_event_bus().emit('wishlist.updated', user_id=uid,
                                             product_id=pid, action='added')
                        return jsonify({'success': True, 'favorited': True,
                                        'message': t('Successfully saved')})
            except Exception as e:
                logger.error(f"toggle_wishlist error: {e}")
                return jsonify({'success': False, 'error': t('Internal server error')}), 500

        @bp.route('/api/check', methods=['POST'])
        @_require_auth
        def check_wishlist(uid):
            """批量检查商品是否已收藏。"""
            try:
                data = request.get_json() or {}
                product_ids = data.get('product_ids', [])
                if not product_ids:
                    return jsonify({'success': True, 'data': {}})

                # 安全构建 IN 子句占位符（不拼接任何用户输入）
                placeholders = ','.join('%s' for _ in product_ids)
                sql = (
                    'SELECT product_id FROM wishlist '
                    'WHERE user_id=%s AND product_id IN (' + placeholders + ')'
                )
                with get_db() as conn:
                    rows = conn.execute(sql, [uid] + list(product_ids)).fetchall()
                favorited = {str(r['product_id']): True for r in rows}
                return jsonify({'success': True, 'data': favorited})
            except Exception as e:
                logger.error(f"check_wishlist error: {e}")
                return jsonify({'success': False, 'error': t('Internal server error')}), 500

        @bp.route('/api/count', methods=['GET'])
        @_require_auth
        def wishlist_count(uid):
            """获取收藏数量。"""
            try:
                with get_db() as conn:
                    count = conn.execute(
                        'SELECT COUNT(*) AS count FROM wishlist WHERE user_id=%s',
                        (uid,)
                    ).fetchone()['count']
                return jsonify({'success': True, 'data': {'count': count}})
            except Exception as e:
                logger.error(f"wishlist_count error: {e}")
                return jsonify({'success': False, 'error': t('Internal server error')}), 500

        return [bp]

    # ── Agent 注册 ────────────────────────────────────────────

    def register_agents(self) -> list:
        """本插件不注册 AI Agent 角色。"""
        return []

    # ── Dashboard 统计 ────────────────────────────────────────

    def get_dashboard_stats(self) -> dict:
        """返回 Dashboard 统计指标（收藏总数、活跃用户数）。"""
        try:
            with get_db() as conn:
                total = conn.execute(
                    'SELECT COUNT(*) AS count FROM wishlist'
                ).fetchone()['count']
                active_users = conn.execute(
                    'SELECT COUNT(DISTINCT user_id) AS count FROM wishlist'
                ).fetchone()['count']
            return {
                'total_favorites': total,
                'active_users': active_users,
            }
        except Exception as e:
            logger.error(f"get_dashboard_stats error: {e}")
            return {'total_favorites': 0, 'active_users': 0}

    # ── 事件处理 ──────────────────────────────────────────────

    def get_event_handlers(self) -> dict:
        """当前无事件订阅。"""
        return {}
