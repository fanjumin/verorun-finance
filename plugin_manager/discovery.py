#!/usr/bin/env python3
"""
Plugin Manager — PluginDiscovery 扫描器
=========================================
从 plugins/ 目录扫描发现插件，解析 plugin.json 元信息，
返回 PluginInfo 对象列表。

发现规则:
  1. 必须是 plugins/<name>/ 子目录
  2. 必须包含 __init__.py
  3. 必须包含 plugin.json（有效的 JSON）
  4. 忽略 _ 和 . 开头的目录
"""

from i18n import _
import os
import json
import re
from typing import List, Optional
from dataclasses import dataclass, field

from .models import PluginInfo, PluginStatus
from .exceptions import PluginNotFoundError


# semver 简单解析（用于依赖版本比较）
_SEMVER_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)(-[a-zA-Z0-9.]+)?(\+[a-zA-Z0-9.]+)?$')


def parse_version(version: str) -> Optional[tuple]:
    """解析 semver 版本号 → (major, minor, patch, pre, build)"""
    m = _SEMVER_RE.match(version.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
            m.group(4) or '', m.group(5) or '')


def version_satisfies(version: str, spec: str) -> bool:
    """检查 version 是否满足 spec（支持 >=, <=, >, <, ==, ^, ~ 及逗号分隔复合约束）"""
    v = parse_version(version)
    if v is None:
        return False

    # VR-PLG-001：支持逗号分隔的复合约束，如 ">=1.2.3, <2.0.0"
    for sub_spec in spec.split(','):
        if not _satisfies_single(v, sub_spec.strip()):
            return False
    return True


def _satisfies_single(v: tuple, spec: str) -> bool:
    """单个版本约束判断（内部函数）"""
    if spec.startswith('>='):
        op, target = '>=', spec[2:].strip()
    elif spec.startswith('<='):
        op, target = '<=', spec[2:].strip()
    elif spec.startswith('>'):
        op, target = '>', spec[1:].strip()
    elif spec.startswith('<'):
        op, target = '<', spec[1:].strip()
    elif spec.startswith('=='):
        op, target = '==', spec[2:].strip()
    elif spec.startswith('^'):
        op, target = '^', spec[1:].strip()
    elif spec.startswith('~'):
        op, target = '~', spec[1:].strip()
    else:
        op, target = '==', spec

    tv = parse_version(target)
    if tv is None:
        return False

    # 主版本号差异处理: ^1.2.3 表示 >=1.2.3 <2.0.0
    if op == '^':
        if v[0] == tv[0]:
            return v >= tv
        return False

    # ~1.2.3 表示 >=1.2.3 <1.3.0
    if op == '~':
        if v[0] == tv[0] and v[1] == tv[1]:
            return v >= tv
        return False

    # 数值比较
    cmp_map = {
        '>=': lambda a, b: a >= b,
        '<=': lambda a, b: a <= b,
        '>':  lambda a, b: a > b,
        '<':  lambda a, b: a < b,
        '==': lambda a, b: a == b,
    }
    cmp_fn = cmp_map.get(op)
    if cmp_fn is None:
        return False
    return cmp_fn(v[:3], tv[:3])


class PluginDiscovery:
    """插件发现扫描器"""

    def __init__(self, plugins_dir: str = None):
        self.plugins_dir = plugins_dir

    def set_plugins_dir(self, plugins_dir: str):
        """设置插件目录（支持延迟初始化）"""
        self.plugins_dir = plugins_dir

    def discover(self) -> List[PluginInfo]:
        """扫描 plugins/ 目录，返回所有发现的插件列表"""
        if not self.plugins_dir or not os.path.isdir(self.plugins_dir):
            return []

        discovered = []
        for entry in sorted(os.listdir(self.plugins_dir)):
            plugin_dir = os.path.join(self.plugins_dir, entry)
            if not os.path.isdir(plugin_dir):
                continue
            if entry.startswith('_(') or entry.startswith(').'):
                continue
            if not os.path.isfile(os.path.join(plugin_dir, '__init__.py')):
                continue

            info = self._parse_plugin_json(entry, plugin_dir)
            if info is not None:
                discovered.append(info)

        return discovered

    def discover_one(self, identifier: str) -> Optional[PluginInfo]:
        """扫描并返回单个插件信息"""
        if not self.plugins_dir:
            return None
        plugin_dir = os.path.join(self.plugins_dir, identifier)
        if not os.path.isdir(plugin_dir):
            return None
        if not os.path.isfile(os.path.join(plugin_dir, '__init__.py')):
            return None
        return self._parse_plugin_json(identifier, plugin_dir)

    def _parse_plugin_json(self, identifier: str, plugin_dir: str) -> Optional[PluginInfo]:
        """解析插件的 plugin.json，返回 PluginInfo"""
        meta_path = os.path.join(plugin_dir, 'plugin.json')
        if not os.path.isfile(meta_path):
            # 没有 plugin.json 的目录视为无效插件
            return None

        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f'[PluginDiscovery] {identifier}: plugin.json parse error: {e}')
            return None

        # 构建 PluginInfo
        info = PluginInfo(
            identifier=identifier,
            name=meta.get('name', identifier),
            version=meta.get('version', '0.1.0'),
            author=meta.get('author', ''),
            description=meta.get('description', ''),
            min_app_version=meta.get('min_app_version', '1.0.0'),
            path=plugin_dir,
            metadata=meta,
            status=PluginStatus.UNKNOWN,
            dependencies=meta.get('dependencies', {}),
            provides_hooks=meta.get('hooks', {}).get('provides', []),
            listens_hooks=meta.get('hooks', {}).get('listens', []),
            permissions=meta.get('permissions', []),
            settings_schema=meta.get('settings_schema', {}),
            config=meta.get('config', {}),
            admin_url=meta.get('admin_url', ''),
            admin_label=meta.get('admin_label', ''),
            # ★ v1.5 展示与分类字段（§5.1/§6.1）
            category=meta.get('category', 'other'),
            icon=meta.get('icon', 'plugin'),
            tags=meta.get('tags', []),
            dashboard_meta=meta.get('dashboard', {}),
        )
        # ★ v1.6 统一网关注册强制校验（插件标准 §2.2/§4）：
        # agent_role 必须为核心角色之一，否则记录校验错误（enable 将拒绝）。
        # 核心角色集由 agent_matrix/roles/*.yaml 动态推导（单一事实源）。
        try:
            from agent_matrix.models import get_core_role_slugs
            _core_roles = get_core_role_slugs()
        except ImportError:
            _core_roles = []
        _ar = meta.get('agent_role', '')
        if _ar not in _core_roles:
            info.last_error = f'missing/invalid agent_role: {_ar!r}（须为核心角色之一）'
        # ★ capabilities 声明→实现 轻量校验（架构评审 §3.4）：
        # 每个 capability 的 namespace（首个点号前）须在插件 .py 实现中出现，
        # 否则视为「假能力」告警（不阻断 enable）。
        _caps = meta.get('capabilities') or []
        if _caps:
            _py_blob = []
            for _root, _dirs, _files in os.walk(plugin_dir):
                for _f in _files:
                    if _f.endswith('.py'):
                        try:
                            _py_blob.append(open(os.path.join(_root, _f), encoding='utf-8').read())
                        except Exception:
                            pass
            _blob = '\n'.join(_py_blob)
            for _cap in _caps:
                if isinstance(_cap, str) and '.' in _cap:
                    _ns = _cap.split('.')[0]
                    if _ns and _ns not in _blob:
                        print(f'[PluginDiscovery] WARNING: {identifier} capability {_cap!r} namespace {_ns!r} not found in implementation')
        if info.admin_url and str(info.admin_url).startswith('/'):
            print(f'[PluginDiscovery] WARNING: {identifier} uses deprecated admin_url field. Use menu.items[].key + l_<key>() instead.')
        return info

    def detect_changes(self, previous: List[PluginInfo]) -> dict:
        """检测变化: 新增/移除/更新的插件列表

        Args:
            previous: 之前已注册的插件列表

        Returns:
            {
                'added': [PluginInfo],     # 新发现的
                'removed': [PluginInfo],    # 已移除的
                'updated': [PluginInfo],    # version 有变化的
            }
        """
        current = self.discover()
        prev_map = {p.identifier: p for p in previous}
        curr_map = {p.identifier: p for p in current}

        added = [c for c in current if c.identifier not in prev_map]
        removed = [p for p in previous if p.identifier not in curr_map]
        updated = [
            c for c in current
            if c.identifier in prev_map and c.version != prev_map[c.identifier].version
        ]

        return {'added': added, 'removed': removed, 'updated': updated}
