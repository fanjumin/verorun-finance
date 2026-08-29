#!/usr/bin/env python3
"""
Plugin System — BasePlugin abstract class
==========================================
All plugins must inherit from BasePlugin.

i18n: plugins use their own i18n/{locale}.yml files,
      accessed via self.t() method — completely isolated
      from system i18n _().
"""

import os
import sys
import yaml
from typing import List, Dict, Any, Optional, Callable
from abc import ABC, abstractmethod

_YAML_CACHE: Dict[str, Dict[str, Dict[str, str]]] = {}
"""Cache: {plugin_name: {locale: {source: translation}}}"""


def _load_plugin_yaml(plugin_name: str, i18n_dir: str) -> Dict[str, Dict[str, str]]:
    """Load all yaml files from a plugin's i18n/ directory.

    Returns {locale: {source: translation}}
    """
    if plugin_name in _YAML_CACHE:
        return _YAML_CACHE[plugin_name]

    result = {}
    if not os.path.isdir(i18n_dir):
        _YAML_CACHE[plugin_name] = result
        return result

    for fname in os.listdir(i18n_dir):
        if not fname.endswith(('.yml', '.yaml')):
            continue
        locale = fname.rsplit('.', 1)[0]  # 'zh-CN.yml' → 'zh-CN'
        fpath = os.path.join(i18n_dir, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            result[locale] = data
        except Exception:
            result[locale] = {}

    _YAML_CACHE[plugin_name] = result
    return result


def clear_plugin_yaml_cache(plugin_name: str = None):
    """Clear yaml cache for a plugin (or all if None)."""
    if plugin_name:
        _YAML_CACHE.pop(plugin_name, None)
    else:
        _YAML_CACHE.clear()


def localize_plugin_dict(p: dict, locale: str = None) -> dict:
    """按当前语言翻译插件显示名/菜单 label/仪表盘 stats 标题（in-place 修改 p 并返回）。

    依据 plugin.json 中的 name_i18n_key / menu.label_i18n_key /
    dashboard.stats[].title_i18n_key 字段，
    从插件 i18n/{locale}.yml 查找翻译；未设置 key 或未找到翻译时
    保留原值，对旧插件完全向后兼容。
    """
    if locale is None:
        try:
            from i18n import get_lang
            locale = get_lang()
        except Exception:
            locale = 'zh-CN'

    metadata = p.get('metadata') or {}
    if not isinstance(metadata, dict):
        return p

    identifier = p.get('identifier') or ''
    i18n_dir = os.path.join(p.get('path', ''), 'i18n') if p.get('path') else ''
    translations = _load_plugin_yaml(identifier, i18n_dir).get(locale, {})

    name_key = metadata.get('name_i18n_key')
    if name_key and translations.get(name_key):
        p['name'] = translations[name_key]

    menu = metadata.get('menu')
    if isinstance(menu, dict):
        label_key = menu.get('label_i18n_key')
        if label_key and translations.get(label_key):
            menu['label'] = translations[label_key]

    dashboard = p.get('dashboard_meta')
    if isinstance(dashboard, dict):
        stats = dashboard.get('stats')
        if isinstance(stats, list):
            for stat in stats:
                if isinstance(stat, dict):
                    title_key = stat.get('title_i18n_key')
                    if title_key and translations.get(title_key):
                        stat['title'] = translations[title_key]

    return p


class BasePlugin(ABC):
    """Abstract base class for all plugins."""

    # ── 必须设置的元数据 ──
    name: str = ''
    version: str = '0.1.0'
    description: str = ''
    author: str = ''
    dependencies: Dict[str, str] = {}
    """依赖的其他插件 `{identifier: version_spec}`"""
    config_schema: Dict[str, Any] = {}

    # ── 运行时引用（由 PluginManager 注入） ──
    plugin_info: Any = None
    """plugin_manager.models.PluginInfo 引用"""
    manager: Any = None
    """plugin_manager.PluginManager 引用"""
    _log: Any = None
    """独立日志器（logging.Logger），由 PluginManager 注入"""

    def __init__(self):
        self._config = {}
        self._i18n_data: Dict[str, Dict[str, str]] = {}
        self._load_i18n()

    @property
    def app(self):
        """Flask app 引用（从 manager 获取），供插件注册中间件等使用。"""
        return getattr(self.manager, 'app', None)

    # ── i18n ──

    def _get_i18n_dir(self) -> str:
        """Return absolute path to this plugin's i18n/ directory."""
        module = sys.modules.get(self.__class__.__module__)
        return os.path.join(os.path.dirname(getattr(module, '__file__', '')), 'i18n')

    def _load_i18n(self):
        """Pre-load this plugin's translation files.

        Uses self.plugin_info.name (from PluginManager) as priority,
        falls back to self.name (class attribute).
        """
        plugin_name = self.name
        if self.plugin_info and hasattr(self.plugin_info, 'name') and self.plugin_info.name:
            plugin_name = self.plugin_info.name
        elif not plugin_name:
            plugin_name = getattr(self.plugin_info, 'identifier', self.__class__.__module__.split('.')[0])
        self._i18n_data = _load_plugin_yaml(plugin_name, self._get_i18n_dir())

    def t(self, text: str, locale: str = None) -> str:
        """Plugin i18n: translate text using own i18n/{locale}.yml.

        Falls back to original text if no translation found.
        Locale defaults to system DEPLOY_LANG or 'zh-CN'.
        """
        if locale is None:
            try:
                from i18n import get_lang
                locale = get_lang()
            except Exception:
                locale = 'zh-CN'
        translations = self._i18n_data.get(locale, {})
        return translations.get(text, text)

    # ── 生命周期（新系统 — PluginManager 调用） ──

    def setup(self):
        """[ENABLED 阶段] 插件初始化。

        调用时机: enable() 时调用，在依赖检查通过之后。
        职责: 创建数据库表、注册钩子、初始化配置。
        返回 False 会导致 enable 失败。

        默认桥接到旧系统钩子 on_install() + on_enable()，
        使仅实现旧钩子的插件（如 analytics/health_check）在新系统下也能正确初始化。
        """
        reg = getattr(self, 'manager', None)
        try:
            self.on_install(reg)
        except Exception as e:
            print(f'[Plugin] {getattr(self, "name", "?")} on_install warning: {e}')
        return self.on_enable(reg)

    def activate(self):
        """[ACTIVE 阶段] 插件激活。

        调用时机: activate() 时调用。
        职责: 注册路由(Blueprint)、注册事件监听、启动后台任务。
        路由通过 register_routes() 自动注册，此处只需启动其他运行时资源。
        """
        pass

    def deactivate(self):
        """[DISABLED 阶段] 插件停用。

        调用时机: disable() 时调用。
        职责: 移除路由引用、取消事件监听、停止后台任务。
        默认桥接到旧系统钩子 on_disable()。
        """
        reg = getattr(self, 'manager', None)
        return self.on_disable(reg)

    # ── 生命周期（旧系统兼容 — 被 plugins.registry.PluginRegistry 调用） ──

    def on_install(self, registry) -> bool:
        """Called when plugin is first installed. Override to init DB tables etc."""
        return True

    def on_enable(self, registry) -> bool:
        """Called when plugin is enabled. Override to set up resources."""
        return True

    def on_disable(self, registry) -> bool:
        """Called when plugin is disabled. Override to clean up resources."""
        return True

    def on_uninstall(self, registry) -> bool:
        """Called when plugin is uninstalled. Drop tables, clean config."""
        return True

    # ── 功能注册（可选覆盖） ──

    def register_routes(self) -> List:
        """Return a list of Flask Blueprints to register."""
        return []

    def register_jobs(self) -> List[Dict[str, Any]]:
        """Return a list of APScheduler job config dicts."""
        return []

    def register_dag_nodes(self) -> Dict[str, Any]:
        """Return {node_type: handler_function} for DAG workflow engine."""
        return {}

    def register_health_checks(self) -> List[Dict[str, Any]]:
        """Return a list of health check item dicts."""
        return []

    def get_event_handlers(self) -> Dict[str, Callable]:
        """Return {event_name: handler_function} to subscribe to system events."""
        return {}

    # ── 工具方法 ──

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """Get a plugin config value from plugin.json 'config' section."""
        return self._config.get(key, default)

    def set_config_value(self, key: str, value: Any) -> bool:
        """Set a plugin config value and persist to database.

        需要 manager 引用（由 PluginManager enable 时注入）。
        """
        if self.manager and self.plugin_info:
            return self.manager.set_config(self.plugin_info.identifier, key, value)
        # 降级：仅设置内存值
        self._config[key] = value
        return True

    def log(self, message: str, level: str = 'info'):
        """Log a message with plugin name prefix.

        如果已有注入的独立日志器（_log），使用文件日志；
        否则回退到 print。
        """
        if self._log is not None:
            level_fn = getattr(self._log, level.lower(), self._log.info)
            level_fn(message)
        else:
            print(f'[{self.name}/{level}] {message}')

    def validate_config(self) -> List[str]:
        """Validate _config against config_schema. Return list of errors."""
        errors = []
        for key, rules in self.config_schema.items():
            required = rules.get('required', False)
            if required and key not in self._config:
                errors.append(f'{self.name}: missing required config "{key}"')
        return errors
