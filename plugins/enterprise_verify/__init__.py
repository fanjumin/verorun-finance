#!/usr/bin/env python3
"""
Enterprise Verification Plugin — 企业认证插件
================================================
独立 PG schema: enterprise_verify（不再使用 SQLite）
提供 OCR 营业执照识别 + AI 自动审核 + 管理端审批
"""

import os
import json
from plugin_manager.base import BasePlugin
from plugin_manager.logger import get_plugin_logger
from .models import get_ev_db, init_ev_db, upsert_agent, unregister_agents, drop_ev_db
from .plugin_i18n import set_plugin

logger = get_plugin_logger('enterprise_verify')


class EnterpriseVerifyPlugin(BasePlugin):
    name = 'enterprise_verify'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'Enterprise Verification — OCR license recognition + AI auto-audit'
    author = 'VeroRun'

    def get_config_value(self, key: str, default=None):
        """优先 PluginManager，回退到 plugin.json 默认值"""
        try:
            mgr = getattr(self.app.extensions, 'get', lambda x: None)('plugin_manager')
            if mgr:
                pm_cfg = mgr.get_config(self.identifier) or {}
                if key in pm_cfg:
                    return pm_cfg[key]
        except Exception:
            pass
        return self._config.get(key, default)

    def on_install(self, registry):
        """安装时初始化独立 schema（幂等）"""
        init_ev_db()
        logger.info('Enterprise verification plugin installed')
        return True

    def on_enable(self, registry):
        """启用时初始化 schema + i18n 桥接 + 注册 Agent（幂等）"""
        init_ev_db()
        set_plugin(self)
        self.register_agents()
        logger.info('Enterprise verification plugin enabled')
        return True

    def register_agents(self):
        """注册 OCR + 审核 Agent 能力到 service 角色 + 本地 agent_registry。"""
        try:
            from agent_matrix.models import register_capability_to_role
            plugin_info = getattr(self, 'plugin_info', None)
            metadata = getattr(plugin_info, 'metadata', {}) or {}
            agents = metadata.get('agents', [])
            if not agents:
                logger.info('plugin.json 无 agents 声明，跳过 Agent 注册')
                return []

            # 注册能力到 service 角色（07-service.yaml）
            for agent in agents:
                register_capability_to_role(
                    domain='service',
                    name=agent.get('name', ''),
                    capabilities=agent.get('capabilities', []),
                    description=f"{agent.get('name', '')} — {agent.get('domain', 'enterprise_verify')}",
                )
            registered = []
            base_dir = os.path.dirname(__file__)
            for agent in agents:
                prompt_path = os.path.join(base_dir, agent.get('prompt_file', ''))
                system_prompt = ''
                if os.path.exists(prompt_path):
                    with open(prompt_path, 'r', encoding='utf-8') as f:
                        system_prompt = f.read().strip()
                else:
                    logger.warning(f'Agent prompt 文件不存在: {prompt_path}')
                policy = agent.get('model_policy', {}) or {}
                upsert_agent(
                    name=agent.get('name', ''),
                    identifier=agent.get('identifier', ''),
                    role_type=agent.get('role_type', 'sub'),
                    description=f"{agent.get('name', '')} — {agent.get('domain', 'enterprise_verify')}",
                    domain=agent.get('domain', 'enterprise_verify'),
                    provider=policy.get('provider', ''),
                    model_name=policy.get('model', ''),
                    system_prompt=system_prompt,
                    capabilities=json.dumps(agent.get('capabilities', []), ensure_ascii=False),
                    is_active=1 if agent.get('enabled_by_default', True) else 0,
                )
                logger.info(f"Agent registered: {agent.get('identifier', agent.get('name', ''))}")
                registered.append(agent)
            return registered
        except Exception as e:
            logger.warning(f'Register agents failed: {e}')
            return []

    def register_routes(self):
        """注册 Flask 路由（管理端 + 用户端）"""
        from .routes_admin import ev_admin_bp
        from .routes_user import ev_user_bp
        return [ev_admin_bp, ev_user_bp]

    def on_disable(self, registry):
        """禁用时注销 Agent（§4.2）"""
        try:
            unregister_agents()
            logger.info('Agents unregistered')
        except Exception as e:
            logger.warning(f'Agent unregister warning: {e}')
        logger.info('Enterprise verification plugin disabled')
        return True

    def on_uninstall(self, registry=None):
        """卸载时清理：注销 Agent + DROP SCHEMA（§12.5 卸载零残留）"""
        try:
            unregister_agents()
        except Exception as e:
            logger.warning(f'Agent unregister warning: {e}')
        try:
            drop_ev_db()
        except Exception as e:
            logger.warning(f'Schema drop warning: {e}')
        return True

    def get_dashboard_stats(self):
        """Dashboard 统计（§2.3）：从独立 schema 取数。"""
        stats = {'pending': 0, 'approved': 0, 'rejected': 0}
        try:
            with get_ev_db() as conn:
                for key, status in (('pending', 'pending'), ('approved', 'approved'), ('rejected', 'rejected')):
                    row = conn.execute(
                        'SELECT COUNT(*) AS c FROM enterprise_verifications WHERE status=%s', (status,)
                    ).fetchone()
                    stats[key] = row['c'] if row else 0
        except Exception as e:
            logger.warning(f'get_dashboard_stats failed: {e}')
        return stats
