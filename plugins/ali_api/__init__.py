#!/usr/bin/env python3
"""
1688 Supply Chain Plugin — AliApiPlugin
=========================================
Provides 1688 product sourcing, AI optimization, and local publishing.

i18n: Uses self.t() for all user-facing strings.
      Translations in:  plugins/ali_api/i18n/{locale}.yml
"""

from plugin_manager.base import BasePlugin
from plugin_manager.logger import get_plugin_logger
from .plugin_i18n import set_plugin

logger = get_plugin_logger('ali_api')


class AliApiPlugin(BasePlugin):
    name = '1688 供应链'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = '1688 供应链采集插件 — 商品搜索、AI 优化、本地商城发布'
    author = 'VeroRun'

    def on_install(self, registry=None):
        """插件安装时: 建表 + 记录 schema 版本（§12.2 + §10.6）"""
        try:
            from .models import init_tables, set_schema_version
            init_tables()
            set_schema_version(self.version)
            return True
        except Exception as e:
            logger.exception('[AliApi] DB init failed in on_install')
            logger.error(f'[AliApi] DB init error: {e}')
            return False

    def on_enable(self, registry):
        """插件启用时: 幂等建表 + 热加载配置 + 注册 Agent + 设置 i18n 桥接 + 注册订单监听"""
        set_plugin(self)
        # §10.6: 幂等补齐 schema（CREATE TABLE IF NOT EXISTS）。
        # 已部署环境升级后自动补上新增表/列（迁移 SQL 仅为文档基线）。
        try:
            from .models import init_tables
            init_tables()
        except Exception as e:
            logger.warning(f'[AliApi] init_tables warning: {e}')
        # §10.3: 启动时从数据库热加载配置，避免模块导入时的陈旧值
        try:
            from .config import reload_config
            reload_config()
        except Exception as e:
            logger.warning(f'[AliApi] Config reload warning: {e}')
        # §4/§6.3: 注册供应链 Agent（读 plugin.json agents 声明 + prompt 文件）
        self.register_agents()
        # 注册订单监听
        try:
            from plugin_manager.event_bus import get_event_bus, EventName
            bus = get_event_bus()
            bus.on(EventName.ORDER_PAID, self._on_order_paid)
        except Exception as e:
            logger.warning(f'[AliApi] EventBus init warning: {e}')
        return True

    def on_disable(self, registry=None):
        """插件禁用时: 注销 Agent + 注销事件监听"""
        try:
            from .models import unregister_agents
            unregister_agents()
        except Exception as e:
            logger.warning(f'[AliApi] Agent unregister warning: {e}')
        try:
            from plugin_manager.event_bus import get_event_bus, EventName
            get_event_bus().off(EventName.ORDER_PAID, self._on_order_paid)
        except Exception as e:
            logger.warning(f'[AliApi] EventBus unregister warning: {e}')

    def _on_order_paid(self, **kwargs):
        """订单支付后：检查是否涉及 1688 货源，自动创建采购单草稿"""
        order_id = kwargs.get('order_id')
        if not order_id:
            return
        try:
            import json
            with get_db() as conn:
                from .models import AliPurchaseOrder
                # 通过主库查询该订单的所有商品
                from .models import get_main_db
                with get_main_db() as main_conn:
                    items = main_conn.execute(
                        """SELECT oi.*, p.features, p.title as prod_title
                           FROM order_items oi
                           JOIN products p ON oi.product_id = p.id
                           WHERE oi.order_id = %s""", (order_id,)
                    ).fetchall()
                for item in items:
                    item = dict(item)
                    try:
                        features = json.loads(item.get('features', '{}'))
                    except (json.JSONDecodeError, TypeError):
                        features = {}
                    if not features.get('ali_source'):
                        continue
                    ali_pid = features.get('ali_product_id', '')
                    if not ali_pid:
                        continue
                    # 检查是否已存在
                    if AliPurchaseOrder.get_by_local_item(conn, item['id']):
                        continue
                    # 从 ali_api_items 获取供应商信息
                    ali_item = main_conn.execute(
                        """SELECT * FROM ali_api_items WHERE product_id=%s""", (ali_pid,)
                    ).fetchone()
                    ali_item = dict(ali_item) if ali_item else {}
                    AliPurchaseOrder.insert(conn, {
                        'local_order_id': item['order_id'],
                        'local_order_item_id': item['id'],
                        'product_id': item['product_id'],
                        'ali_product_id': ali_pid,
                        'quantity': item.get('quantity', 1),
                        'price': float(item.get('unit_price', 0)),
                        'total_fee': float(item.get('subtotal', 0)),
                        'supplier_name': ali_item.get('seller_name', ''),
                        'supplier_id': ali_item.get('seller_id', ''),
                    })
                conn.commit()
                logger.info(f'[AliApi] 已为订单 {order_id} 创建采购单草稿')
        except Exception:
            logger.exception('[AliApi] Order listening exception')

    def register_agents(self):
        """注册供应链 Agent（§4/§6.3）：从 plugin.json agents 声明读取，写入本地 agent_registry 表。"""
        try:
            import os as _os
            import json as _json
            from .models import upsert_agent

            plugin_info = getattr(self, 'plugin_info', None)
            metadata = plugin_info.metadata if plugin_info else {}
            agents = metadata.get('agents', [])
            if not agents:
                logger.info('[AliApi] plugin.json 无 agents 声明，跳过 Agent 注册')
                return []

            registered = []
            base_dir = _os.path.dirname(__file__)
            for agent in agents:
                prompt_path = _os.path.join(base_dir, agent.get('prompt_file', ''))
                system_prompt = ''
                if _os.path.exists(prompt_path):
                    with open(prompt_path, 'r', encoding='utf-8') as f:
                        system_prompt = f.read().strip()
                else:
                    logger.warning(f'[AliApi] Agent prompt 文件不存在: {prompt_path}')
                upsert_agent(
                    name=agent.get('name', ''),
                    identifier=agent.get('identifier', ''),
                    role_type=agent.get('role_type', 'sub'),
                    description=f"{agent.get('name', '')} — {agent.get('domain', 'supply_chain')}",
                    domain=agent.get('domain', 'supply_chain'),
                    provider='',
                    model_name='',
                    system_prompt=system_prompt,
                    capabilities=_json.dumps(agent.get('capabilities', []), ensure_ascii=False),
                    is_active=1 if agent.get('enabled_by_default', True) else 0,
                )
                logger.info(f"[AliApi] Agent registered: {agent.get('identifier', agent.get('name', ''))}")
                registered.append(agent)
            return registered
        except Exception as e:
            logger.warning(f'[AliApi] Register agents failed: {e}')
            return []

    def get_dashboard_stats(self):
        """Dashboard 统计（§2.3/§6.3）：从插件独立库取数。"""
        stats = {'total_items': 0, 'published_today': 0, 'api_calls_24h': 0}
        try:
            from .models import get_db
            with get_db() as conn:
                total = conn.execute('SELECT COUNT(*) FROM ali_api_items').fetchone()
                stats['total_items'] = total['count'] if total else 0
                pub = conn.execute(
                    "SELECT COUNT(*) FROM ali_api_items "
                    "WHERE publish_status='published' AND updated_at::date = CURRENT_DATE"
                ).fetchone()
                stats['published_today'] = pub['count'] if pub else 0
                api = conn.execute(
                    "SELECT COUNT(*) FROM ali_api_logs "
                    "WHERE created_at > NOW() - INTERVAL '24 hours'"
                ).fetchone()
                stats['api_calls_24h'] = api['count'] if api else 0
        except Exception as e:
            logger.warning(f'[AliApi] get_dashboard_stats failed: {e}')
        return stats

    def get_schema_version(self):
        """从插件独立库读取当前 schema 版本（§10.6）"""
        try:
            from .models import get_schema_version as _get_schema_version
            return _get_schema_version()
        except Exception:
            return '0.0.0'

    def migrate(self, from_version: str, to_version: str):
        """版本升级逻辑（§10.6）：运行幂等建表/迁移并更新 schema 版本。"""
        try:
            from .models import init_tables, set_schema_version
            init_tables()
            set_schema_version(to_version)
            logger.info(f'[AliApi] Schema migrated: {from_version} → {to_version}')
            return True
        except Exception as e:
            logger.exception(f'[AliApi] Schema migrate failed: {from_version} → {to_version}')
            return False

    def register_routes(self):
        """注册路由蓝图"""
        from .routes.admin import ali_admin_bp
        return [ali_admin_bp]

    def on_uninstall(self, registry=None):
        """F-010: 卸载清理 — 删除 ali_api schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS ali_api CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            logger.info('[AliApi] ali_api schema dropped')
        except Exception as e:
            logger.error(f'[AliApi] on_uninstall cleanup failed: {e}')
        return True


def get_db():
    """快捷访问独立数据库（供事件监听内使用）"""
    from .models import get_db as _get_db
    return _get_db()
