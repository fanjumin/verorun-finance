#!/usr/bin/env python3
"""
Skill Availability Resolver —— 技能可用性实时求值器（P0 核心）
=============================================================
可用性 = f(requirements, 插件状态, 版本, 许可, 能力开关, 发行版)
纯逻辑模块（无 Flask、无 DB），遵守 skills.py 惯例，可直调单测。

设计约束：
  - 永不持久化结果；结果缓存 60s，事件驱动即时失效（可用性优先）。
  - 全部依赖检查并行收集后一次返回（不短路），前端一次展示所有缺失项。
  - 事件回调只做缓存失效（F7：plugin.* 同步派发，回调必须轻量）。
"""
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from i18n import _                            # F16：用户可见文案必须 _() 包装
from .discovery import version_satisfies      # F8：复合版本约束
from .models import PluginStatus               # F10

_TTL = 60.0  # 结果缓存秒数

# 机器可读失败原因（i18n 中立，供前端行为分支；展示用本地化 message）
PLUGIN_NOT_INSTALLED     = 'PLUGIN_NOT_INSTALLED'
PLUGIN_NOT_ACTIVE        = 'PLUGIN_NOT_ACTIVE'
PLUGIN_VERSION_MISMATCH  = 'PLUGIN_VERSION_MISMATCH'
PLUGIN_LICENSE_INVALID   = 'PLUGIN_LICENSE_INVALID'
SKILL_DEP_UNAVAILABLE    = 'SKILL_DEP_UNAVAILABLE'
CAPABILITY_MISSING       = 'CAPABILITY_MISSING'
EDITION_MISMATCH         = 'EDITION_MISMATCH'
SKILL_DISABLED           = 'SKILL_DISABLED'
CIRCUIT_BREAKER          = 'CIRCUIT_BREAKER'


@dataclass
class Reason:
    code: str
    slug: str = ''
    message: str = ''
    action: str = ''          # enable_plugin / install_plugin / upgrade_plugin / ...


@dataclass
class AvailabilityResult:
    available: bool
    reasons: List[Reason] = field(default_factory=list)

    def to_dict(self):
        return {
            'available': self.available,
            'reasons': [{'code': r.code, 'slug': r.slug,
                         'message': r.message, 'action': r.action}
                        for r in self.reasons],
        }


class AvailabilityResolver:
    """依赖注入：plugin_manager（必需）、license_manager（可选）、config_getter（可选）。"""

    def __init__(self, plugin_manager, license_manager=None, config_getter=None):
        self._pm = plugin_manager                      # F12
        self._lm = license_manager
        self._cfg = config_getter or (lambda key, default='': default)  # F15
        self._cache: Dict[tuple, tuple] = {}           # (skill_id, site_id) -> (ts, result)
        self._lock = threading.RLock()
        self._req_loader = None                        # 由 SkillRegistry 注入

    # ── 缓存 ──────────────────────────────────────────────────────────
    def invalidate(self, plugin_slug: Optional[str] = None):
        """事件回调调用（F7：必须轻量，只清缓存）。plugin_slug=None 清全部。"""
        with self._lock:
            if plugin_slug is None:
                self._cache.clear()
                return
            # 只清依赖该插件的条目（反向索引由 registry 提供）
            affected = self._req_loader.skill_ids_requiring_plugin(plugin_slug) \
                if self._req_loader else []
            self._cache = {k: v for k, v in self._cache.items()
                           if k[0] not in affected}

    # ── 求值 ──────────────────────────────────────────────────────────
    def evaluate(self, skill_row: dict, site_id: int = 0) -> AvailabilityResult:
        key = (skill_row.get('id'), site_id)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < _TTL:
                return hit[1]

        result = self._evaluate_impl(skill_row)
        with self._lock:
            self._cache[key] = (now, result)
        return result

    def _evaluate_impl(self, skill_row: dict) -> AvailabilityResult:
        reasons: List[Reason] = []

        # 0) 技能自身开关与熔断
        if not int(skill_row.get('enabled', 1)):
            return AvailabilityResult(False, [Reason(SKILL_DISABLED, action='enable_skill')])
        breaker = self._cfg('skill_circuit_breaker', '0')
        source = skill_row.get('source', 'community')
        if breaker in ('all', source) and source != 'official':
            reasons.append(Reason(CIRCUIT_BREAKER,
                                  message=_('Community skills are temporarily disabled')))

        # 1) 插件依赖（全部收集，不短路）
        reqs = self._parse_requirements(skill_row.get('requirements', '{}'))
        for dep in reqs.get('plugins', []):
            slug, spec = dep['slug'], dep.get('spec', '')
            info = self._pm.get_info(slug)                    # F10
            if info is None:
                reasons.append(Reason(PLUGIN_NOT_INSTALLED, slug,
                                      _('Required plugin {slug} is not installed', slug=slug),
                                      'install_plugin'))
                continue
            if info.status not in (PluginStatus.ACTIVE, PluginStatus.ENABLED):
                reasons.append(Reason(PLUGIN_NOT_ACTIVE, slug,
                                      _('Required plugin {slug} is not enabled (currently {status})',
                                        slug=slug, status=info.status.value),
                                      'enable_plugin'))
                continue
            if spec and not version_satisfies(info.version, spec):   # F8
                reasons.append(Reason(PLUGIN_VERSION_MISMATCH, slug,
                                      _('Plugin {slug} version {version} does not satisfy {spec}',
                                        slug=slug, version=info.version, spec=spec),
                                      'upgrade_plugin'))
            if self._lm is not None:
                try:
                    lic = self._lm.validate(slug)             # F9
                    if not lic.get('valid'):
                        reasons.append(Reason(PLUGIN_LICENSE_INVALID, slug,
                                              _('Plugin {slug} license is invalid', slug=slug),
                                              'renew_license'))
                except Exception:
                    pass  # 许可查询失败不阻塞可用性（可用性优先）

        # 2) 技能依赖技能（递归 + 环保护由注册时拓扑检查承担）
        for dep_slug in reqs.get('skills', []):
            dep_row = self._req_loader.load_skill_by_identifier(dep_slug) \
                if self._req_loader else None
            if dep_row is None:
                reasons.append(Reason(SKILL_DEP_UNAVAILABLE, dep_slug,
                                      _('Required skill {slug} does not exist', slug=dep_slug)))
            else:
                sub = self.evaluate(dep_row)
                if not sub.available:
                    reasons.append(Reason(SKILL_DEP_UNAVAILABLE, dep_slug,
                                          _('Required skill {slug} is unavailable', slug=dep_slug)))

        # 3) 系统能力与发行版
        for cap in reqs.get('capabilities', []):
            if self._cfg(f'capability_{cap}', '0') not in ('1', 'true', 'on'):
                reasons.append(Reason(CAPABILITY_MISSING, cap,
                                      _('Missing system capability: {cap}', cap=cap)))
        editions = reqs.get('editions', [])
        if editions and self._current_edition() and \
                self._current_edition() not in editions:
            reasons.append(Reason(EDITION_MISMATCH,
                                  message=_('This skill is not supported on the current edition')))

        return AvailabilityResult(len(reasons) == 0, reasons)

    def _current_edition(self) -> str:
        """发行版标识：优先 system_config.install_type，回退 agent_matrix current_edition()。
        对齐 version.py get_edition() 归一化（standard/finance/research 等真实枚举）。"""
        ed = (self._cfg('install_type', '') or '').strip().lower()
        if ed:
            return ed
        try:
            from agent_matrix.models import current_edition
            return (current_edition() or '').lower()
        except Exception:
            return ''

    @staticmethod
    def _parse_requirements(raw) -> dict:
        """requirements 统一为 {'plugins':[{'slug','spec'}], 'skills':[], ...}。
        兼容两种写法：{'payment': '>=2.0.0'} 或 [{'slug':..,'min_version':..}]。
        角色 slug 已由 parse_skill_package 归一化，此处原样透传。"""
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or '{}')
            except Exception:
                return {}
        if not isinstance(raw, dict):
            return {}
        plugins = []
        p = raw.get('plugins', {})
        if isinstance(p, dict):          # plugin.json 风格 {slug: spec}
            plugins = [{'slug': k, 'spec': v or ''} for k, v in p.items()]
        elif isinstance(p, list):
            plugins = [{'slug': d.get('slug', ''),
                        'spec': d.get('spec') or
                                ', '.join(filter(None, [
                                    f">={d['min_version']}" if d.get('min_version') else '',
                                    f"<={d['max_version']}" if d.get('max_version') else '']))}
                       for d in p if d.get('slug')]
        return {'plugins': plugins,
                'skills': raw.get('skills', []) or [],
                'roles': raw.get('roles', []) or [],
                'capabilities': raw.get('capabilities', []) or [],
                'editions': raw.get('editions', []) or []}
