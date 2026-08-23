from i18n import _
import json
import os
from typing import Any

from plugin_manager.base import BasePlugin


class ChatbotPlugin(BasePlugin):
    name = 'AI Advisor'
    identifier = 'chatbot'
    version = '1.2.0'

    def setup(self):
        # 先执行父类 setup()，触发 on_install（建表/写种子）和 on_enable（注册Agent）
        super().setup()
        # 注册管理后台路由 + 公开 Webhook
        from .routes import chatbot_bp, webhook_bp
        self.app.register_blueprint(chatbot_bp, url_prefix='/admin/chatbot')
        self.app.register_blueprint(webhook_bp)

    def on_install(self, registry=None) -> bool:
        from .models import init_chatbot_tables, seed_defaults, migrate_from_main
        init_chatbot_tables()
        self._seed_default_config()
        # 从主库迁移已有数据（幂等，首次运行自动执行）
        self.log(_('Migrating data from main database...'))
        migrate_from_main()
        return True

    def on_enable(self, registry=None) -> bool:
        self.register_agents()
        return True

    def register_routes(self):
        from .routes import chatbot_bp, webhook_bp
        return [chatbot_bp, webhook_bp]

    def register_agents(self):
        """注册 Advisor Agent 到独立库 agent_registry 表"""
        try:
            from .models import upsert_agent

            plugin_info = getattr(self, 'plugin_info', None)
            metadata = plugin_info.metadata if plugin_info else {}
            agents = metadata.get('agents', [])
            if not agents:
                return

            prompt_path = os.path.join(os.path.dirname(__file__), agents[0].get('prompt_file', ''))
            system_prompt = ''
            if os.path.exists(prompt_path):
                with open(prompt_path, 'r', encoding='utf-8') as f:
                    system_prompt = f.read().strip()

            agent = agents[0]
            upsert_agent(
                name=agent['name'],
                identifier=agent.get('identifier', ''),
                role_type=agent['role_type'],
                description=f"AI Advisor Agent — {agent['domain']}",
                domain=agent.get('domain', 'chatbot'),
                provider='',
                model_name='',
                system_prompt=system_prompt,
                capabilities=json.dumps(agent.get('capabilities', [])),
                is_active=1 if agent.get('enabled_by_default', True) else 0
            )
        except Exception as e:
            self.log(f'Register agents failed: {e}', 'warning')

    def _unregister_agents(self):
        """清理本地 agent_registry（插件禁用/卸载时调用，实现"零残留"）"""
        try:
            from .models import unregister_agents
            n = unregister_agents()
            self.log(f'Agents unregistered: {n}')
        except Exception as e:
            self.log(f'Unregister agents failed: {e}', 'warning')

    def on_disable(self, registry=None) -> bool:
        """禁用时注销本地 Agent 注册，避免残留"""
        self._unregister_agents()
        return True

    def on_uninstall(self, registry=None) -> bool:
        """卸载时注销本地 Agent 注册，实现卸载零残留"""
        self._unregister_agents()
        return True

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
            from .models import init_chatbot_tables, set_schema_version
            init_chatbot_tables()
            set_schema_version(to_version)
            self.log(f'Schema migrated: {from_version} → {to_version}')
            return True
        except Exception as e:
            self.log(f'Schema migrate failed: {from_version} → {to_version}: {e}', 'error')
            return False

    def get_dashboard_stats(self):
        """Dashboard 统计（§2.3）：与 plugin.json dashboard.stats 声明对应"""
        stats = {'today_sessions': 0, 'handoff_rate': 0, 'avg_csat': 0}
        try:
            from .stats import get_today_stats
            data = get_today_stats()
            stats['today_sessions'] = data.get('today_sessions', 0)
            stats['handoff_rate'] = data.get('handoff_rate', 0)
            stats['avg_csat'] = data.get('avg_csat', 0)
        except Exception as e:
            self.log(f'Get dashboard stats failed: {e}', 'warning')
        return stats

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """优先 PluginManager，回退到独立库 plugin_configs 表"""
        try:
            mgr = getattr(self.app.extensions, 'get', lambda x: None)('plugin_manager')
            if mgr:
                pm_cfg = mgr.get_config(self.identifier) or {}
                if key in pm_cfg:
                    return pm_cfg[key]
        except Exception:
            pass
        # 回退旧方法
        try:
            from .models import get_config
            val = get_config(self.identifier, key)
            if val:
                return val
        except Exception:
            pass
        return self._config.get(key, default)

    def set_config_value(self, key: str, value: Any) -> bool:
        """优先 PluginManager，回退到独立库 plugin_configs"""
        try:
            mgr = getattr(self.app.extensions, 'get', lambda x: None)('plugin_manager')
            if mgr:
                mgr.set_config_batch(self.identifier, {key: str(value)}, coerce=True)
                self._config[key] = value
                return True
        except Exception:
            pass
        # 回退旧方法
        try:
            from .models import set_config
            set_config(self.identifier, key, str(value))
            self._config[key] = value
            return True
        except Exception as e:
            self.log(f'Set config failed: {e}', 'error')
            return False

    def _seed_default_config(self):
        """仅当独立库中无该配置行时写入默认值"""
        defaults = self._config or {}
        if not defaults:
            return
        try:
            from .models import seed_defaults
            seed_defaults(self.identifier, defaults)
        except Exception as e:
            self.log(f'Seed default config failed: {e}', 'warning')