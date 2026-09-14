#!/usr/bin/env python3
"""Plugin Distribution Resolver —— 动态分类 / 动态分流的单一裁决点。

对应设计：《VeroRun 插件动态分类与动态分流 - 轻量增量方案 v1.0》§4.1
对应标准：`docs/plugin-standard-v1.8.md` §18

设计约束
--------
- **只读**：本模块不写库。所有写操作走 routes 层的既有 admin 端点模式。
- **零侵入**：DB 为空 / 查库失败 / 开关关闭 → ``resolve()`` 返回 ``None``，
  调用方回落既有 yaml 语义（``deploy/editions/*.yaml`` + ``compatible_editions``）。
  因此「规则表为空」等价于「本能力未启用」，全系统行为与 v1.7 完全一致。
- **不抛错**：所有异常都被吞掉并打印告警，绝不影响商店浏览与插件装载主链路。

环境变量
--------
- ``VR_DIST_ENABLED``   ``'0'`` = 关闭全部动态规则（kill-switch，回到 yaml 语义）；默认 ``'1'``
- ``VR_DIST_CACHE_TTL`` 快照缓存秒数，默认 30；``'0'`` = 关闭缓存（每次查库）
- ``VR_PROFILE``        当前部署环境画像标签（见标准 §18.3），如 ``cn-public-icp``

事实源（两张卫星表，见 ``models_store.LICENSE_STORE_DDL`` 末尾）
----------------------------------------------------------------
- ``plugin_categories``          分类注册表（动态分类）
- ``plugin_distribution_rules``  分流规则表（与 ``store_plugins`` 严格 1:1，``identifier`` UNIQUE）
"""

import json
import os
import threading
import time

# 内置分类（与 store_importer.CATEGORY_ENUM 同源）。
# 此处为**兜底副本**，避免 import 期循环依赖；表内 builtin=1 行由 models_store 种子写入。
_BUILTIN_CATEGORIES = ('system', 'shop', 'content', 'ai_agent', 'social', 'tools', 'supply_chain')

_CAT_COLUMNS = ('id', 'key', 'label', 'label_i18n_key', 'emoji', 'icon_svg',
                'grad_from', 'grad_to', 'default_tagline', 'sort_order',
                'enabled', 'builtin', 'is_official')
_RULE_COLUMNS = ('id', 'identifier', 'editions', 'profiles', 'categories',
                 'channel', 'priority', 'hidden', 'note', 'updated_by', 'updated_at')
_EDITION_COLUMNS = ('id', 'edition', 'label', 'form_factor', 'enabled',
                    'default_exclude', 'note', 'updated_by', 'updated_at')

_lock = threading.Lock()
_cache = {'ts': 0.0, 'categories': {}, 'rules': {}, 'editions': {}, 'loaded': False}


# ── 开关与工具 ────────────────────────────────────────────────────────

def is_enabled() -> bool:
    """全局开关：``VR_DIST_ENABLED=0`` 时禁用全部动态规则（kill-switch）。"""
    return (os.environ.get('VR_DIST_ENABLED', '1') or '1').strip() != '0'


def _ttl() -> int:
    try:
        return int(os.environ.get('VR_DIST_CACHE_TTL', '30'))
    except (TypeError, ValueError):
        return 30


def _as_list(raw):
    """JSON 数组安全解析：非法 / 空 → ``[]``。"""
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x not in (None, '')]
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x not in (None, '')]
    return []


def _as_int(raw, default=0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _row_to_dict(row, columns):
    """RealDictRow → 普通 dict；缺列（旧库未迁移）自动跳过。"""
    if row is None:
        return None
    try:
        return {k: row[k] for k in columns if k in row}
    except (TypeError, KeyError):
        try:
            return dict(row)
        except Exception:
            return None


# ── 快照加载与缓存 ────────────────────────────────────────────────────

def _load(conn):
    """读取三张表 → ``(categories, rules, editions)``。

    表不存在 / 查询失败 → 返回空字典（等价于「未启用」），不抛错。
    """
    cats = {}
    try:
        for r in conn.execute('SELECT * FROM plugin_categories ORDER BY sort_order').fetchall():
            d = _row_to_dict(r, _CAT_COLUMNS)
            if d and d.get('key'):
                cats[d['key']] = d
    except Exception as e:
        print(f'[Distribution] ⚠️ plugin_categories 读取失败（按空处理）: {e}')

    rules = {}
    try:
        for r in conn.execute('SELECT * FROM plugin_distribution_rules').fetchall():
            d = _row_to_dict(r, _RULE_COLUMNS)
            if d and d.get('identifier'):
                rules[d['identifier']] = d
    except Exception as e:
        print(f'[Distribution] ⚠️ plugin_distribution_rules 读取失败（按空处理）: {e}')

    editions = {}
    try:
        for r in conn.execute('SELECT * FROM edition_catalog').fetchall():
            d = _row_to_dict(r, _EDITION_COLUMNS)
            if d and d.get('edition'):
                editions[d['edition']] = d
    except Exception as e:
        print(f'[Distribution] ⚠️ edition_catalog 读取失败（按空处理）: {e}')

    return cats, rules, editions


def _snapshot():
    """取缓存快照；TTL 过期则重载（查库失败时保留旧快照，绝不抛错）。"""
    now = time.time()
    ttl = _ttl()
    if ttl > 0 and _cache['loaded'] and (now - _cache['ts']) < ttl:
        return _cache['categories'], _cache['rules'], _cache['editions']
    with _lock:
        now = time.time()
        if ttl > 0 and _cache['loaded'] and (now - _cache['ts']) < ttl:
            return _cache['categories'], _cache['rules'], _cache['editions']
        try:
            from .models_store import get_registry_db
            with get_registry_db() as conn:
                cats, rules, editions = _load(conn)
            _cache.update(ts=now, categories=cats, rules=rules, editions=editions, loaded=True)
        except Exception as e:
            print(f'[Distribution] ⚠️ 规则读取失败（沿用旧快照）: {e}')
            # 记录时间戳，避免每请求都重试打库；保留旧数据不清零
            _cache['ts'] = now
        return _cache['categories'], _cache['rules'], _cache['editions']


def invalidate_cache():
    """写操作后立即失效快照（下次读取强制重载）。"""
    with _lock:
        _cache['ts'] = 0.0
        _cache['loaded'] = False


# ── 分类（动态分类）──────────────────────────────────────────────────

def list_categories(include_disabled: bool = False):
    """分类注册表 → 列表（供 ``GET /store/categories`` 与前端 map 下发）。"""
    if not is_enabled():
        return []
    cats, _, _ = _snapshot()
    out = [dict(c) for c in cats.values()
           if include_disabled or _as_int(c.get('enabled'), 1) == 1]
    out.sort(key=lambda c: (_as_int(c.get('sort_order'), 100), str(c.get('key') or '')))
    return out


def category_map():
    """前端渲染所需的三张映射表（emoji / 渐变 / 图标）+ 展示名。

    前端**必须保留内置 map 作为 fallback**：本端点不可用时不崩、不空白。
    """
    m = {'emoji': {}, 'grad': {}, 'icon': {}, 'label': {}}
    for c in list_categories():
        k = c.get('key')
        if not k:
            continue
        if c.get('emoji'):
            m['emoji'][k] = c['emoji']
        if c.get('grad_from'):
            m['grad'][k] = [c['grad_from'], c.get('grad_to') or c['grad_from']]
        if c.get('icon_svg'):
            m['icon'][k] = c['icon_svg']
        if c.get('label'):
            m['label'][k] = c['label']
    return m


def valid_category_keys():
    """合法分类集合 = 内置 7 类 ∪ 注册表（enabled=1）。

    供 ``routes.py`` / ``store_importer.py`` 的白名单校验替换使用。
    """
    keys = set(_BUILTIN_CATEGORIES)
    if is_enabled():
        for c in list_categories():
            k = c.get('key')
            if k:
                keys.add(k)
    return keys


# ── 分流（动态分流）──────────────────────────────────────────────────

def rule_for(identifier):
    """取某插件的分流规则；无规则 / 开关关闭 → ``None``。"""
    if not is_enabled() or not identifier:
        return None
    _, rules, _ = _snapshot()
    return rules.get(identifier)


def current_profile():
    """当前部署的环境画像标签（标准 §18.3）。

    优先 ``VR_PROFILE``；未声明时**复用** ``region.py`` 的地域判定（不另造一套）。
    两者皆不可得 → ``''``（= 不限，环境维度不参与过滤）。
    """
    p = (os.environ.get('VR_PROFILE') or '').strip().lower()
    if p:
        return p
    try:
        from .region import is_cn_region
        return 'cn' if is_cn_region() else 'os'
    except Exception:
        return ''


def resolve(identifier, edition, profile=None):
    """单一裁决：``(visible, reason)`` 或 ``None``（= 无可判定源，调用方走 yaml）。

    优先级链（标准 §14.9.3 / §18.2）：
      1. ``rule.hidden = 1``             → 隐藏（覆盖一切）
      2. ``rule.editions``/``profiles``  → 单插件覆盖（规则存在即以其为准）
      3. ``edition_catalog`` 命中当前版  → 发行矩阵：default_exclude 判隐藏
      4. 否则 → 可见（未知则回落 yaml，由调用方处理）
      5. ``compatible_editions`` 或 yaml → 调用方兜底

    判定细节（与既有约定一致）：
      - ``edition`` / ``profile`` 为空时**跳过对应检查**（无法判定即不隐藏，
        避免因环境变量缺失把插件全量锁死）。
      - 无单插件规则**且**矩阵无当前版记录 → 返回 ``None``，回落既有 yaml 语义。
    """
    rule = rule_for(identifier)

    # 级1/级2：单插件覆盖（语义与 v1.8 完全一致；无规则则跳过）
    if rule is not None:
        if _as_int(rule.get('hidden')) == 1:
            return False, f"hidden_by_rule: {rule.get('note') or 'manual'}"
        if profile is None:
            profile = current_profile()
        eds = _as_list(rule.get('editions'))
        if eds and edition and edition not in eds:
            return False, f'not_in_rule_editions: {edition}'
        profs = _as_list(rule.get('profiles'))
        if profs and profile and profile not in profs:
            return False, f'profile_mismatch: {profile}'

    # 级3：发行矩阵（edition_catalog）——在 yaml(调用方) 之前
    _, _, editions = _snapshot()
    entry = editions.get(edition) if edition else None
    if entry is not None:
        if _as_int(entry.get('enabled'), 1) != 1:
            # 版本已停用（enabled=0）→ 视同无记录，回落 yaml，避免停用版本全量锁死
            pass
        else:
            excl = _as_list(entry.get('default_exclude'))
            if identifier in excl:
                return False, f'edition_excluded: {edition}'
            return True, 'ok'

    # 无单插件规则且矩阵无当前版记录 → 回落 yaml
    if rule is None:
        return None
    return True, 'ok'


def resolve_visible_set(edition, candidates, profile=None):
    """批量裁决 → ``(visible: list, hidden: dict[identifier, reason])``。

    规则为 ``None``（无规则）的插件默认可见（回落既有 yaml 语义）。
    """
    visible, hidden = [], {}
    for pid in (candidates or []):
        res = resolve(pid, edition, profile)
        if res is None or res[0]:
            visible.append(pid)
        else:
            hidden[pid] = res[1]
    return visible, hidden
