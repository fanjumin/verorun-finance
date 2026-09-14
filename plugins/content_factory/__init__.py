#!/usr/bin/env python3
"""
Content Factory Plugin — 内容工厂插件
========================================
独立 PostgreSQL schema content_factory
提供多源采集、AI加工、审核发布、Skill推送、静态页面生成
"""

from i18n import _
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.base import BasePlugin
from plugin_manager.logger import get_plugin_logger
from .models import get_cf_db, init_cf_db

logger = get_plugin_logger('content_factory')

# 模块级 i18n 引用，由 on_enable 注入
_t = lambda text: text


def init_i18n(t_fn):
    """供插件启用时注入 i18n 翻译函数"""
    global _t
    _t = t_fn


class ContentFactoryPlugin(BasePlugin):
    name = 'content_factory'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'Content Factory — Collection, AI processing, review, publishing, skill push'
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

    def set_config_value(self, key: str, value) -> bool:
        """优先 PluginManager"""
        try:
            mgr = getattr(self.app.extensions, 'get', lambda x: None)('plugin_manager')
            if mgr:
                mgr.set_config_batch(self.identifier, {key: str(value)}, coerce=True)
                self._config[key] = value
                return True
        except Exception:
            pass
        self._config[key] = value
        return True

    def on_install(self, registry):
        """安装时初始化独立数据库 + 记录 schema 版本（§10.6）"""
        try:
            init_cf_db()
            from .models import set_schema_version
            set_schema_version(self.version)
            return True
        except Exception as e:
            logger.exception('[ContentFactoryPlugin] DB init failed in on_install')
            logger.error(f'[ContentFactoryPlugin] DB init error: {e}')
            return False

    def on_enable(self, registry):
        """启用时初始化数据库 + i18n + 注册 Agent（幂等，§4.1）"""
        init_cf_db()
        init_i18n(self.t)
        self.register_agents()
        logger.info('[ContentFactoryPlugin] Content Factory plugin enabled')
        return True

    def register_routes(self):
        """注册 Flask 路由"""
        from .routes import cf_bp
        return [cf_bp]

    def on_disable(self, registry):
        """禁用时注销 Agent（§4.2）"""
        try:
            from .models import unregister_agents
            unregister_agents()
            logger.info('[ContentFactoryPlugin] Agents unregistered')
        except Exception as e:
            logger.warning(f'[ContentFactoryPlugin] Agent unregister warning: {e}')
        logger.info('[ContentFactoryPlugin] Content Factory plugin disabled')
        return True

    def on_uninstall(self, registry):
        """卸载时注销 Agent（§4.2/§12.5）"""
        try:
            from .models import unregister_agents
            unregister_agents()
            logger.info('[ContentFactoryPlugin] Agents unregistered on uninstall')
        except Exception as e:
            logger.warning(f'[ContentFactoryPlugin] Agent unregister warning: {e}')
        return True

    def register_agents(self):
        """注册 Content Curator Agent 能力到 content 角色 + 本地 agent_registry 表。"""
        try:
            from .models import upsert_agent
            from agent_matrix.models import register_capability_to_role
            plugin_info = getattr(self, 'plugin_info', None)
            metadata = getattr(plugin_info, 'metadata', {}) or {}
            agents = metadata.get('agents', [])
            if not agents:
                logger.info('[ContentFactoryPlugin] plugin.json 无 agents 声明，跳过 Agent 注册')
                return []

            # 注册能力到 content 角色（02-content.yaml）
            for agent in agents:
                register_capability_to_role(
                    domain='content',
                    name=agent.get('name', ''),
                    capabilities=agent.get('capabilities', []),
                    description=f"{agent.get('name', '')} — {agent.get('domain', 'content')}",
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
                    logger.warning(f'[ContentFactoryPlugin] Agent prompt 文件不存在: {prompt_path}')
                upsert_agent(
                    name=agent.get('name', ''),
                    identifier=agent.get('identifier', ''),
                    role_type=agent.get('role_type', 'sub'),
                    description=f"{agent.get('name', '')} — {agent.get('domain', 'content')}",
                    domain=agent.get('domain', 'content'),
                    provider='',
                    model_name='',
                    system_prompt=system_prompt,
                    capabilities=json.dumps(agent.get('capabilities', []), ensure_ascii=False),
                    is_active=1 if agent.get('enabled_by_default', True) else 0,
                )
                logger.info(f"[ContentFactoryPlugin] Agent registered: {agent.get('identifier', agent.get('name', ''))}")
                registered.append(agent)
            return registered
        except Exception as e:
            logger.warning(f'[ContentFactoryPlugin] Register agents failed: {e}')
            return []

    def get_dashboard_stats(self):
        """Dashboard 统计（§2.3/§6.3）：从插件独立库取数。"""
        stats = {'source_count': 0, 'pending': 0, 'processed': 0, 'published': 0, 'failed': 0}
        try:
            with get_cf_db() as conn:
                stats['source_count'] = conn.execute(
                    'SELECT COUNT(*) FROM content_sources WHERE is_active=1'
                ).fetchone()['count'] or 0
                stats['pending'] = conn.execute(
                    "SELECT COUNT(*) FROM raw_contents WHERE status='pending'"
                ).fetchone()['count'] or 0
                stats['processed'] = conn.execute(
                    'SELECT COUNT(*) FROM processed_contents'
                ).fetchone()['count'] or 0
                stats['published'] = conn.execute(
                    'SELECT COUNT(*) FROM processed_contents WHERE is_published=1'
                ).fetchone()['count'] or 0
                stats['failed'] = conn.execute(
                    "SELECT COUNT(*) FROM raw_contents WHERE status='failed'"
                ).fetchone()['count'] or 0
        except Exception as e:
            logger.warning(f'[ContentFactoryPlugin] get_dashboard_stats failed: {e}')
        return stats

    def get_schema_version(self):
        """从插件独立库读取当前 schema 版本（§10.6）"""
        try:
            from .models import get_schema_version as _get_schema_version
            return _get_schema_version()
        except Exception:
            return '0.0.0'

    def migrate(self, from_version: str, to_version: str):
        """版本升级逻辑（§10.6）：幂等建表并更新 schema 版本。"""
        try:
            init_cf_db()
            from .models import set_schema_version
            set_schema_version(to_version)
            logger.info(f'[ContentFactoryPlugin] Schema migrated: {from_version} → {to_version}')
            return True
        except Exception as e:
            logger.exception(f'[ContentFactoryPlugin] Schema migrate failed: {from_version} → {to_version}')
            return False
