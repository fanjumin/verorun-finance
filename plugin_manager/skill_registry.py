#!/usr/bin/env python3
"""
SkillRegistry —— 技能注册中心：安装记录 / 依赖反向索引 / 事件联动（P0）
======================================================================
职责：
  - 维护 plugin_slug → set(skill_id) 反向索引（由 store_skills.requirements JSON 构建）
  - 订阅 plugin.* 事件 → 可用性缓存即时失效（F7：回调只清缓存，不重查库）
  - 安装预检（依赖不满足拒绝落安装记录）+ availability_map 批量求值

降级铁律：get_skill_registry() 返回 None 时，所有调用点回退 v0.60.0 行为。
"""
import json
from typing import Dict, List, Optional, Set

from .event_bus import get_event_bus          # F6
from .models_store import get_registry_db     # F2
from .skill_availability import AvailabilityResolver


def _system_config_getter(key: str, default: str = '') -> str:
    """system_config 读取器（F15）——技能求值器的默认配置来源。

    重要：求值器必须从 system_config 读 `skill_circuit_breaker` / `install_type`，
    否则这些开关读到的永远是默认值（熔断成死开关：管理端写入 system_config，
    求值器却无感）。读失败按 default 处理（可用性优先，不因配置读不到而阻塞求值）。
    """
    try:
        with get_registry_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key=%s", (key,)).fetchone()
        return row['value'] if row else default
    except Exception:
        return default


def _config_enabled(key: str) -> bool:
    """system_config 布尔开关（F15 惯例），读失败按启用处理（可用性优先）。"""
    return _system_config_getter(key, '1') not in ('0', 'false', 'off', '')


class SkillRegistry:
    def __init__(self, plugin_manager, license_manager=None, config_getter=None):
        self._pm = plugin_manager
        # config_getter 缺省即 system_config 读取器：任何装配路径都不会漏接开关
        self.resolver = AvailabilityResolver(plugin_manager, license_manager,
                                             config_getter or _system_config_getter)
        self.resolver._req_loader = self
        self._reverse_idx: Dict[str, Set[int]] = {}   # plugin_slug -> skill_ids
        self._init_events()

    # ── 事件订阅（F6/F7：回调只做缓存失效，保持轻量） ────────────────
    def _init_events(self):
        bus = get_event_bus()
        for ev in ('plugin.installed', 'plugin.enabled',
                   'plugin.disabled', 'plugin.uninstalled'):
            bus.on(ev, self._on_plugin_change)

    def _on_plugin_change(self, plugin_id=None, **kwargs):   # F6 负载格式
        if not plugin_id:
            return
        self.resolver.invalidate(plugin_id)

    # ── 依赖反向索引（自愈：直接扫描 requirements JSON，不依赖同步表） ──
    def rebuild_reverse_index(self):
        idx: Dict[str, Set[int]] = {}
        try:
            with get_registry_db() as conn:
                rows = conn.execute(
                    "SELECT id, requirements FROM store_skills").fetchall()
        except Exception:
            rows = []
        for r in rows:
            try:
                reqs = json.loads(r['requirements'] or '{}')
            except Exception:
                continue
            plugins = reqs.get('plugins', {}) or {}
            if isinstance(plugins, dict):
                slugs = list(plugins.keys())
            elif isinstance(plugins, list):
                slugs = [d.get('slug') for d in plugins if isinstance(d, dict) and d.get('slug')]
            else:
                slugs = []
            for slug in slugs:
                if slug:
                    idx.setdefault(slug, set()).add(r['id'])
        self._reverse_idx = idx

    def skill_ids_requiring_plugin(self, plugin_slug: str) -> Set[int]:
        return self._reverse_idx.get(plugin_slug, set())

    # ── 供 resolver 回调的数据加载 ────────────────────────────────────
    def load_skill_by_identifier(self, identifier: str) -> Optional[dict]:
        with get_registry_db() as conn:
            row = conn.execute(
                "SELECT s.*, COALESCE(i.enabled, 1) AS enabled FROM store_skills s "
                "LEFT JOIN skill_installations i ON i.skill_id = s.id "
                "WHERE s.identifier = %s AND s.status = 'approved'",
                (identifier,)).fetchone()
        return dict(row) if row else None

    # ── 安装（含依赖预检） ─────────────────────────────────────────────
    def install(self, skill_id: int, site_id: int, version: str) -> dict:
        row = self._load_by_id(skill_id)
        result = self.resolver.evaluate(row, site_id)
        if not result.available:
            return {'ok': False, 'error': 'requirements_not_met',
                    'missing': result.to_dict()['reasons']}
        with get_registry_db() as conn:
            conn.execute(
                "INSERT INTO skill_installations(skill_id, site_id, version) "
                "VALUES(%s, %s, %s) "
                "ON CONFLICT(skill_id, site_id) DO UPDATE SET version=EXCLUDED.version, "
                "enabled=1, updated_at=NOW()",
                (skill_id, site_id, version))
        self.resolver.invalidate()
        return {'ok': True}

    def _load_by_id(self, skill_id: int) -> dict:
        with get_registry_db() as conn:
            row = conn.execute(
                "SELECT s.*, COALESCE(i.enabled, 1) AS enabled FROM store_skills s "
                "LEFT JOIN skill_installations i ON i.skill_id = s.id "
                "WHERE s.id = %s", (skill_id,)).fetchone()
        return dict(row) if row else {}

    # ── 批量求值（供浏览页） ──────────────────────────────────────────
    def availability_map(self, skill_rows: List[dict], site_id: int = 0) -> dict:
        return {r['id']: self.resolver.evaluate(r, site_id).to_dict()
                for r in skill_rows}


# ── 装配器（避免循环导入：外部传入 manager，勿反向 import routes） ──────
_skill_registry = None


def init_skill_registry(plugin_manager):
    """由应用装配处调用（与 PluginManager(app) 同一位置），全局仅一次。
    注意：main_site / admin / auth_server 三个服务各自进程独立初始化。
    配置来源由 SkillRegistry 默认注入 system_config 读取器，此处无需重复传参。"""
    global _skill_registry
    if _skill_registry is not None:
        return _skill_registry
    if not _config_enabled('skill_registry_enabled'):
        return None
    from .license import get_license_manager        # 延迟导入防循环
    _skill_registry = SkillRegistry(plugin_manager,
                                    license_manager=get_license_manager())
    _skill_registry.rebuild_reverse_index()
    # 注册技能提示词注入器（仅本进程注册一次；未启用 agent 矩阵的服务无害）
    try:
        from agent_matrix.skill_injector import register_skill_injector
        register_skill_injector()
    except Exception:
        pass
    return _skill_registry


def get_skill_registry():
    """供路由/注入器调用；未初始化或开关关闭时返回 None（调用方回退旧行为）。"""
    return _skill_registry
