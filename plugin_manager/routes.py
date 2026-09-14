#!/usr/bin/env python3
"""
Plugin Manager — 管理 API（供 Admin 后台调用）
================================================
9 个 REST 端点，返回 JSON。

端点列表:
  GET    /admin/plugins              — 列出所有插件
  GET    /admin/plugins/discover     — 扫描新插件
  POST   /admin/plugins/<id>/install  — 安装
  POST   /admin/plugins/<id>/enable   — 启用
  POST   /admin/plugins/<id>/disable  — 禁用
  POST   /admin/plugins/<id>/activate — 激活
  POST   /admin/plugins/<id>/uninstall— 卸载
  GET    /admin/plugins/<id>/config   — 读取配置
  POST   /admin/plugins/<id>/config   — 保存配置
"""

import json
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime
from flask import Blueprint, jsonify, request

from .manager import PluginManager
from .models import PluginStatus
from .models_store import get_registry_db
from .skill_registry import get_skill_registry
from .exceptions import (
    PluginError,
    PluginNotFoundError, PluginStateError,
    PluginVersionError, PluginDependencyError,
)
from .base import localize_plugin_dict

bp = Blueprint('plugin_manager_api', __name__, url_prefix='/admin/plugins')


def _get_manager() -> PluginManager:
    """从 Flask 扩展中获取 PluginManager 实例"""
    try:
        from flask import current_app
        mgr = current_app.extensions.get('plugin_manager')
        if mgr is None:
            return None
        return mgr
    except Exception:
        return None


def _json_result(success: bool, data=None, error: str = None, code: int = 200):
    """统一 json 响应（内部委托 shared.http，信封兼容）"""
    from shared.http import api_ok, api_err
    if success:
        return api_ok(data)
    return api_err(error or 'error', code)


def _require_admin():
    """VR-SEC-013: 管理端点必须由管理员调用（JWT is_admin 校验）。

    返回 None 表示通过；否则返回 (jsonify, 403) 供视图直接 return。
    """
    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.args.get('token') or request.cookies.get('sso_token') or request.cookies.get('tm_token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    return None


def _require_store_admin():
    """商店运营权：官方版 + super_admin 双条件（VR-SEC-014）。

    用户版无论角色一律 403 —— 无官方 Ed25519 凭证，伪造无效。
    返回 None 表示通过；否则返回 (jsonify, 403) 供视图直接 return。
    """
    err = _require_admin()
    if err:
        return err
    from .license import _is_official_edition
    if not _is_official_edition():
        return jsonify({'success': False, 'error': '商店运营仅官方端可用'}), 403
    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    payload = validate_token(token) if token else None
    if not payload or payload.get('role') != 'super_admin':
        return jsonify({'success': False, 'error': '需要超级管理员'}), 403
    return None


def _parse_positive_int(name: str, default: int, lo: int = 1, hi: int = 100000) -> int:
    """解析正整数查询参数（D-PAGE-500）。

    - 缺省/空 → default
    - 非整数 → 抛 ValueError（由调用方转为 400）
    - 超出 [lo, hi] → 截断到边界
    """
    raw = request.args.get(name, '')
    if raw == '' or raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f'参数 {name} 必须为整数')
    return max(lo, min(hi, v))


def _quota(raw, default: int, lo: int, hi: int):
    """P1-3 配额声明校验（INT-003 修复，模块级以支持单测）。

    仅当字段缺省/空串时回落默认值；0/负/超上限/非整数一律返回 None（调用方转为 400）。
    0 是合法入参但为 falsy，禁止用 `or default` 吞掉导致越界校验失效。
    """
    if raw is None or raw == '':
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    if v < lo or v > hi:
        return None
    return v


def _get_jwt_user():
    """从请求中解析 JWT 用户（管理员/用户 token 双通道）。

    Returns:
        (user_id, user_name, is_admin)；未登录返回 (None, '', False)
    """
    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = (request.args.get('token')
                 or request.cookies.get('sso_token')
                 or request.cookies.get('tm_token')
                 or request.cookies.get('token'))
    payload = validate_token(token) if token else None
    if not payload:
        return None, '', False
    user_name = (payload.get('username')
                 or payload.get('display_name')
                 or payload.get('phone')
                 or str(payload.get('user_id', '')))
    return payload.get('user_id'), user_name, bool(payload.get('is_admin'))


def _info_to_dict(info) -> dict:
    """PluginInfo → dict，用于 JSON 序列化"""
    d = info.to_dict()
    d['status'] = info.status.value if hasattr(info.status, 'value') else info.status
    # 确保 metadata 是 dict
    if isinstance(d.get('metadata'), str):
        d['metadata'] = json.loads(d['metadata'])
    # 处理 config（确保不超长）
    if isinstance(d.get('config'), str):
        d['config'] = json.loads(d['config'])
    # 按当前语言翻译插件显示名/菜单 label（name_i18n_key 机制）
    localize_plugin_dict(d)
    return d


# ── 1. 列出所有插件 ────────────────────────────────────────────────

@bp.route('', methods=['GET'])
def list_plugins():
    """列出所有插件（含状态、版本信息）"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    status_filter = request.args.get('status')
    plugins = [p for p in mgr.list_plugins(status_filter)]
    return _json_result(True, data=[_info_to_dict(p) for p in plugins])


# ── 1b. 聚合统计（§6.4 / §10.5）────────────────────────────────────

@bp.route('/metrics', methods=['GET'])
def plugin_metrics():
    """聚合所有 ACTIVE 插件的 Dashboard 统计指标"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)
    try:
        stats = mgr.get_all_stats()
        return _json_result(True, data={'plugins': stats})
    except Exception as e:
        return _json_result(False, error=str(e), code=500)


# ── 1a. 统一列表：本地 + 商店 ─────────────────────────────────────

@bp.route('/unified', methods=['GET'])
def list_plugins_unified():
    """合并本地已安装插件 + 商店目录中未安装的插件"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    try:
        data = mgr.get_unified_list()
        return _json_result(True, data=data)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=str(e), code=500)


# ── 2. 发现新插件 ──────────────────────────────────────────────────

@bp.route('/discover', methods=['GET'])
def discover_plugins():
    """扫描 plugins/ 目录，返回所有插件（含已安装的）"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    try:
        all_plugins = mgr.discover_all()
        # 标记已安装
        installed_ids = {p.identifier for p in mgr._cache.values()}
        dicts = []
        for p in all_plugins:
            d = _info_to_dict(p)
            d['installed'] = p.identifier in installed_ids
            if p.identifier in mgr._cache:
                cached = mgr._cache[p.identifier]
                d['status'] = cached.status.value if cached.status else 'unknown'
            dicts.append(d)

        return _json_result(True, data={
            'total': len(dicts),
            'plugins': dicts,
        })
    except Exception as e:
        return _json_result(False, error=str(e), code=500)


# ── 3. 安装 ────────────────────────────────────────────────────────

@bp.route('/<identifier>/install', methods=['POST'])
def install_plugin(identifier: str):
    """安装插件"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    try:
        info = mgr.install(identifier)
        return _json_result(True, data=_info_to_dict(info))
    except PluginError as e:
        return _json_result(False, error=str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Install failed: {e}', code=500)


# ── 4. 启用 ────────────────────────────────────────────────────────

@bp.route('/<identifier>/enable', methods=['POST'])
def enable_plugin(identifier: str):
    """启用插件（执行 setup）"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    try:
        info = mgr.enable(identifier)
        return _json_result(True, data=_info_to_dict(info))
    except PluginError as e:
        return _json_result(False, error=str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Enable failed: {e}', code=500)


# ── 5. 禁用 ────────────────────────────────────────────────────────

@bp.route('/<identifier>/disable', methods=['POST'])
def disable_plugin(identifier: str):
    """禁用插件"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    try:
        info = mgr.disable(identifier)
        return _json_result(True, data=_info_to_dict(info))
    except PluginError as e:
        return _json_result(False, error=str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Disable failed: {e}', code=500)


# ── 6. 激活 ────────────────────────────────────────────────────────

@bp.route('/<identifier>/activate', methods=['POST'])
def activate_plugin(identifier: str):
    """激活插件（加载模块 + 注册路由）"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    try:
        info = mgr.activate(identifier)
        return _json_result(True, data=_info_to_dict(info))
    except PluginError as e:
        return _json_result(False, error=str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Activate failed: {e}', code=500)


# ── 7. 卸载 ────────────────────────────────────────────────────────

@bp.route('/<identifier>/uninstall', methods=['POST'])
def uninstall_plugin(identifier: str):
    """卸载插件（需要确认）"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    # 安全确认: 必须传 confirm=true
    confirm = request.json.get('confirm', False) if request.is_json else False
    if not confirm:
        return _json_result(False, error='请确认卸载（confirm=true）', code=400)

    try:
        mgr.uninstall(identifier)
        return _json_result(True, data={'identifier': identifier, 'status': 'uninstalled'})
    except PluginError as e:
        return _json_result(False, error=str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Uninstall failed: {e}', code=500)


# ── 8. 读取配置 ────────────────────────────────────────────────────

@bp.route('/<identifier>/config', methods=['GET'])
def get_plugin_config(identifier: str):
    """读取插件配置"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    info = mgr.get_info(identifier)
    if not info:
        return _json_result(False, error=f'Plugin "{identifier}" not found', code=404)

    return _json_result(True, data={
        'identifier': identifier,
        'config': info.config,
        'settings_schema': info.settings_schema,
    })


# ── 9. 保存配置 ────────────────────────────────────────────────────

@bp.route('/<identifier>/config', methods=['POST'])
def set_plugin_config(identifier: str):
    """保存插件配置"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    if not request.is_json:
        return _json_result(False, error='请求体必须是 JSON', code=400)

    config = request.json
    if not isinstance(config, dict):
        return _json_result(False, error='配置必须是键值对对象', code=400)

    try:
        for key, value in config.items():
            mgr.set_config(identifier, key, value)
        info = mgr.get_info(identifier)
        return _json_result(True, data={
            'identifier': identifier,
            'config': info.config if info else config,
        })
    except PluginError as e:
        return _json_result(False, error=str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Config save failed: {e}', code=500)


# ── 10. 列出所有 Action 钩子 ─────────────────────────────────

@bp.route('/hooks/actions', methods=['GET'])
def list_hook_actions():
    """列出所有已注册的 Action 钩子"""
    err = _require_admin()
    if err:
        return err
    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)
    hook_name = request.args.get('hook')
    data = mgr._hook_registry.list_actions(hook_name)
    return _json_result(True, data=data)


# ── 11. 列出所有 Filter 钩子 ─────────────────────────────────

@bp.route('/hooks/filters', methods=['GET'])
def list_hook_filters():
    """列出所有已注册的 Filter 钩子"""
    err = _require_admin()
    if err:
        return err
    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)
    hook_name = request.args.get('hook')
    data = mgr._hook_registry.list_filters(hook_name)
    return _json_result(True, data=data)


# ── 12. 依赖拓扑排序 ─────────────────────────────────────

@bp.route('/dependency-order', methods=['GET'])
def dependency_order():
    """返回拓扑排序后的安装/激活顺序"""
    err = _require_admin()
    if err:
        return err
    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)
    try:
        order = mgr.resolve_install_order()
        return _json_result(True, data={'order': order})
    except Exception as e:
        return _json_result(False, error=str(e), code=400)


# ── 13. 依赖树 ──────────────────────────────────────────

@bp.route('/<identifier>/dependencies', methods=['GET'])
def plugin_dependencies(identifier: str):
    """获取插件依赖树"""
    err = _require_admin()
    if err:
        return err
    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)
    tree = mgr.get_dependency_tree(identifier)
    dependents = mgr.get_dependents_tree(identifier)
    return _json_result(True, data={'depends_on': tree, 'depended_by': dependents})


# ── 14. 配置校验（不保存） ───────────────────────────────

@bp.route('/<identifier>/config/validate', methods=['POST'])
def validate_plugin_config(identifier: str):
    """校验插件配置（不保存）"""
    err = _require_admin()
    if err:
        return err
    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    config = request.json if request.is_json else None
    result = mgr.validate_config(identifier, config)
    return _json_result(result['success'], data={
        'errors': result['errors'],
        'schema': result['schema'],
    }, error=result['errors'][0] if result['errors'] else None)


# ── 15. 批量保存配置（带校验） ───────────────────────────

@bp.route('/<identifier>/config/batch', methods=['POST'])
def batch_save_config(identifier: str):
    """批量保存配置（带 Schema 校验 + 类型转换）"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    if not request.is_json:
        return _json_result(False, error='请求体必须是 JSON', code=400)

    config = request.json
    if not isinstance(config, dict):
        return _json_result(False, error='配置必须是键值对对象', code=400)

    result = mgr.set_config_batch(identifier, config)
    if result['success']:
        return _json_result(True, data={
            'errors': result['errors'],
            'config': result['coerced'],
        })
    return _json_result(False, data={
        'errors': result['errors'],
    }, error=result['errors'][0] if result['errors'] else None)


# ── 16. 读取插件日志 ─────────────────────────────────────

@bp.route('/<identifier>/log', methods=['GET'])
def plugin_log(identifier: str):
    """读取插件日志最后 N 行"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    try:
        lines = int(request.args.get('lines', 50))
    except ValueError:
        lines = 50
    if lines < 1:
        lines = 50
    if lines > 500:
        lines = 500

    content = mgr.read_log(identifier, lines)
    return _json_result(True, data={'log': content, 'lines': lines})


# ── 17. 清空插件日志 ─────────────────────────────────────

@bp.route('/<identifier>/log', methods=['DELETE'])
def clear_plugin_log(identifier: str):
    """清空插件日志"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    ok = mgr.clear_log(identifier)
    return _json_result(ok, data={'cleared': ok})


# ====================================================================
# 商店管理 API（仅管理员，字面路由须在通配路由前注册）
# ====================================================================

# ── 36b. 商店管理：手动同步目录（获取插件/一键上架，不等 6h 调度）────

@bp.route('/store/sync', methods=['POST'])
def store_sync():
    """管理员：手动触发商店目录同步。

    Returns: {total, added, source, error}
      - total: 目录插件数；-1 拉取失败但保留本地缓存；0 拉取失败且无缓存
      - added: 本次同步后新增上架的插件 identifier 列表（即"获取到的新插件"）
    """
    err = _require_store_admin()
    if err:
        return err
    mgr = _get_manager()
    if not mgr or not mgr.store_client:
        return _json_result(False, error='Store not available', code=503)
    try:
        from .store import _catalog_urls
        with get_registry_db() as conn:
            before = {r['identifier'] for r in conn.execute(
                'SELECT identifier FROM store_plugins').fetchall()}
        cnt = mgr.store_client.sync_all()
        # ★ 试验：同步远程目录后，从本地插件目录读取 USAGE 使用说明（多命名，中文优先）
        try:
            usage_synced = _sync_local_usage_guides(mgr)
        except Exception as _e:
            print(f'[store] local usage guide sync failed: {_e}')
            usage_synced = []
        with get_registry_db() as conn:
            after = {r['identifier'] for r in conn.execute(
                'SELECT identifier FROM store_plugins').fetchall()}
        added = sorted(after - before)
        error = '' if cnt >= 0 else (
            'catalog fetch failed, kept local cache' if cnt == -1
            else 'catalog fetch failed, no local cache')
        return _json_result(True, data={
            'total': cnt,
            'added': added,
            'source': _catalog_urls(),
            'error': error,
            'usage_synced': usage_synced,
        })
    except Exception as e:
        print(f'[store] sync failed: {e}')
        return _json_result(False, error=f'Sync failed: {e}', code=500)


# ── 37. 商店管理：从 GitHub 仓库导入元数据（预填充 Add Plugin 表单）────

@bp.route('/store/admin/import', methods=['GET'])
def store_admin_import():
    """管理员：从 GitHub 仓库自动提取插件元数据。

    Query: ?url=https://github.com/owner/repo
    Returns: 归一化 store_plugins 字段 + warnings（不落库，前端回填表单后保存）。
    """
    err = _require_store_admin()
    if err:
        return err

    url = request.args.get('url', '').strip()
    if not url:
        return _json_result(False, error='url required', code=400)
    try:
        from .store_importer import import_from_github
        entry, warnings = import_from_github(url)
    except Exception as _e:
        print(f'[store] import failed: {_e}')
        return _json_result(False, error=f'Import failed: {_e}', code=500)
    if entry is None:
        return _json_result(False, error=warnings[0] if warnings else 'Import failed', code=400)
    return _json_result(True, data={'plugin': entry, 'warnings': warnings})


# ── 38. 商店管理：列出所有插件商品 ────────────────────────

@bp.route('/store/admin', methods=['GET'])
def store_admin_list():
    """管理员：列出所有商店插件商品"""
    err = _require_store_admin()
    if err:
        return err

    with get_registry_db() as conn:
        rows = conn.execute('SELECT * FROM store_plugins ORDER BY created_at DESC').fetchall()
        plugins = [dict(r) for r in rows]
    return _json_result(True, data={'plugins': plugins})


# ── 38. 商店管理：创建/更新插件商品 ───────────────────────

# ── 商店 i18n：identifier/键 → 名称 本地化（自动继承系统语言）──────────

_STORE_I18N_CACHE = {}


def _load_store_i18n(locale: str) -> dict:
    """读取 i18n/store/{locale}.yml，返回 {identifier_or_key: text}（带缓存）。"""
    if locale in _STORE_I18N_CACHE:
        return _STORE_I18N_CACHE[locale]
    result = {}
    try:
        import yaml
        base = os.path.join(os.path.dirname(__file__), '..', 'i18n', 'store')
        fpath = os.path.join(base, f'{locale}.yml')
        if os.path.isfile(fpath):
            with open(fpath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                result = data
    except Exception as _e:
        print(f'[store] load i18n failed: {_e}')
    _STORE_I18N_CACHE[locale] = result
    return result


def _localize(p: dict, lang: str = None) -> dict:
    """按当前语言本地化商店插件名称/宣传语（in-place 修改并返回）。"""
    if not lang:
        try:
            from i18n import get_lang
            lang = get_lang()
        except Exception:
            lang = 'zh-CN'
    d = dict(p)
    store_i18n = _load_store_i18n(lang)
    nk = d.get('name_i18n_key')
    if nk and nk in store_i18n:
        d['name'] = store_i18n[nk]
    elif d.get('identifier') and d['identifier'] in store_i18n:
        d['name'] = store_i18n[d['identifier']]
    tk = d.get('tagline_i18n_key')
    if tk and tk in store_i18n:
        d['tagline'] = store_i18n[tk]
    return d


# 内部包下载地址 / 签名哈希：绝不下发给未登录的用户端公开接口
_PUBLIC_SCRUB_FIELDS = ('download_url', 'package_hash')


def _scrub_public(d: dict) -> dict:
    """从公开返回项中剥离内部敏感字段（in-place 修改并返回）。"""
    for _f in _PUBLIC_SCRUB_FIELDS:
        d.pop(_f, None)
    return d


def _readme_text(readme_url: str) -> str:
    """拉取 README 文本（前 3000 字符），失败返回空串。"""
    try:
        import requests
        r = requests.get(readme_url, timeout=8)
        return r.text[:3000] if r.ok else ''
    except Exception:
        return ''


def _ensure_tagline(pdata: dict, lang: str = 'zh-CN') -> str:
    """空 tagline 时由 AI 从 README 提取；失败降级（build_tagline 内部兜底）。"""
    t = (pdata.get('tagline') or '').strip()
    if t:
        return t
    try:
        readme = _readme_text(pdata.get('readme_url', ''))
        from .tagline import build_tagline
        return build_tagline(pdata, readme, lang)
    except Exception as _e:
        print(f'[store] tagline ensure failed: {_e}')
        return ''


def _sync_plugin_usage_kb(identifier: str, name: str, usage_guide: str, enabled: int):
    """插件使用说明 ↔ 系统知识库同步（复用主库 knowledge_blocks，软删除）。

    空内容或未上架 → 软删除 kb_plugin_usage_<identifier>（用户已确认的清理逻辑）。
    知识库行 id 固定为 kb_plugin_usage_<identifier>，写入后 RAG 关键词/向量路均可检索。
    镜像 main_site/routes/api_v1.py 知识块保存模式；embedding 失败静默走关键词路。
    """
    import re as _re
    kb_id = f'kb_plugin_usage_{identifier}'
    html = (usage_guide or '').strip()
    try:
        with get_registry_db() as conn:
            if not html or not enabled:
                conn.execute(
                    "UPDATE knowledge_blocks SET deleted_at=NOW() WHERE id=%s AND deleted_at IS NULL",
                    (kb_id,))
                conn.commit()
                return
            text = _re.sub(r'\s+', ' ', _re.sub(r'<[^>]+>', ' ', html)).strip()[:4000]
            title = f'Plugin {name} — 使用说明'
            keywords = f'{identifier},{name},plugin,usage,插件,使用'
            existing = conn.execute(
                'SELECT id FROM knowledge_blocks WHERE id=%s', (kb_id,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE knowledge_blocks SET title=%s, content=%s, keywords=%s, "
                    "category='plugin', priority=50, scope='system', owner_id=NULL, "
                    "deleted_at=NULL, updated_at=NOW() WHERE id=%s",
                    (title, text, keywords, kb_id))
            else:
                conn.execute(
                    "INSERT INTO knowledge_blocks (id, title, content, keywords, category, "
                    "priority, scope, owner_id) VALUES (%s,%s,%s,%s,'plugin',50,'system',NULL)",
                    (kb_id, title, text, keywords))
            conn.commit()
        try:
            from agent_matrix.rag_retriever import store_embedding
            store_embedding(kb_id, title, text)
        except Exception as e:
            print(f'[store] usage guide embedding failed: {e}')
    except Exception as e:
        print(f'[store] usage guide KB sync failed: {e}')


def _sync_local_usage_guides(mgr) -> list:
    """从本地插件目录读取 USAGE.md（多命名，中文优先）同步到商店 usage_guide 与知识库。

    试验策略（已确认）：存在 USAGE 文件时自动覆盖管理端手写内容；无文件的行保持不变。
    返回本次同步的 identifier 列表。
    """
    plugins_dir = getattr(mgr, 'plugins_dir', '')
    if not plugins_dir or not os.path.isdir(plugins_dir):
        return []
    usage_names = ('USAGE.cn.md', 'USAGE_CN.md', 'USAGE.zh-CN.md', 'USAGE.md')
    synced = []
    for entry in sorted(os.listdir(plugins_dir)):
        plugin_dir = os.path.join(plugins_dir, entry)
        if entry.startswith('_') or entry.startswith('.') or not os.path.isdir(plugin_dir):
            continue
        if not os.path.isfile(os.path.join(plugin_dir, '__init__.py')):
            continue
        text = ''
        for name in usage_names:
            p = os.path.join(plugin_dir, name)
            if os.path.isfile(p):
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        text = f.read().strip()[:20000]
                except (IOError, OSError):
                    text = ''
                break
        if not text:
            continue
        try:
            with get_registry_db() as conn:
                row = conn.execute(
                    'SELECT name, enabled FROM store_plugins WHERE identifier=%s',
                    (entry,)).fetchone()
                if not row:
                    continue
                conn.execute(
                    'UPDATE store_plugins SET usage_guide=%s WHERE identifier=%s',
                    (text, entry))
                conn.commit()
            _sync_plugin_usage_kb(entry, row['name'] or entry, text, row['enabled'])
            synced.append(entry)
        except Exception as _e:
            print(f'[store] usage guide sync failed for {entry}: {_e}')
    return synced


@bp.route('/store/admin', methods=['POST'])
def store_admin_save():
    """管理员：创建或更新商店插件商品"""
    err = _require_store_admin()
    if err:
        return err

    data = request.json if request.is_json else {}
    identifier = data.get('identifier', '')
    if not identifier:
        return _json_result(False, error='identifier required', code=400)

    # 补充校验：semver / category 枚举 / URL 白名单（与 store_importer 标准一致）
    import re as _re
    if data.get('version') and not _re.match(r'^[0-9]+\.[0-9]+\.[0-9]+$', str(data['version'])):
        return _json_result(False, error='version must be x.y.z semver', code=400)
    # v1.8：分类白名单 = 内置 7 类 ∪ plugin_categories 注册表（标准 §18.1）
    # 注册表不可用/为空时回落内置枚举，行为等同 v1.7。
    from .store_importer import CATEGORY_ENUM
    try:
        from .distribution import valid_category_keys
        _cat_keys = set(CATEGORY_ENUM) | valid_category_keys()
    except Exception as _e:
        print(f'[store] ⚠️ 动态分类取数失败，回落内置枚举: {_e}')
        _cat_keys = set(CATEGORY_ENUM)
    if data.get('category') and data['category'] not in _cat_keys:
        return _json_result(False, error=f'category must be one of {sorted(_cat_keys)}', code=400)
    for _f in ('download_url', 'icon_url', 'readme_url', 'author_url'):
        _v = (data.get(_f) or '').strip()
        if _v and not _v.startswith(('http://', 'https://')):
            return _json_result(False, error=f'{_f} must be a valid http(s) URL', code=400)

    # P1-3: 配额声明校验（对齐 Coze 上限：工具 100 / 依赖 250MB / QPS 50）
    # INT-003 修复：0 是合法入参但为 falsy，`or 默认值` 会吞掉 0 导致越界校验失效；
    # 仅当字段缺省/空串时回落默认值；0/负/超上限/非整数一律 400。（_quota 为模块级函数，见文件顶部）
    _max_tools = _quota(data.get('max_tools'), 100, 1, 1000)
    if _max_tools is None:
        return _json_result(False, error='max_tools must be 1-1000', code=400)
    _max_deps_kb = _quota(data.get('max_dependencies_kb'), 204800, 1024, 512 * 1024)
    if _max_deps_kb is None:
        return _json_result(False, error='max_dependencies_kb must be 1024-524288 (KB)', code=400)
    _qps = _quota(data.get('declared_qps'), 50, 1, 10000)
    if _qps is None:
        return _json_result(False, error='declared_qps must be 1-10000', code=400)

    # 适用版本：可选，必须为字符串列表（pro/standard/edge 等）
    _editions = data.get('compatible_editions', [])
    if not isinstance(_editions, list) or not all(isinstance(e, str) and e for e in _editions):
        return _json_result(False, error='compatible_editions must be a list of strings', code=400)

    # 宣传语：开发者手填优先；仅当为空时由 AI 从 README 兜底提取（失败自动降级，不阻断上架）
    tagline = (data.get('tagline') or '').strip()
    if tagline:
        tagline = tagline[:32]   # 长度限制 ≤32 字（双行标语第一行）
    else:
        try:
            from i18n import get_lang
            _lang = get_lang()
        except Exception:
            _lang = 'zh-CN'
        tagline = _ensure_tagline(data, lang=_lang)
    tagline_subtitle = (data.get('tagline_subtitle') or '').strip()[:64]

    with get_registry_db() as conn:
        conn.execute("""
            INSERT INTO store_plugins (
                identifier, name, name_i18n_key, description, version, author,
                author_url, icon_url, price_type, price_amount,
                price_interval, price_quarter_fen, price_year_fen, compatible_editions,
                trial_days, download_url, package_hash,
                file_size, category, tags, screenshots, readme_url,
                tagline, tagline_i18n_key, tagline_font_size, tagline_color,
                tagline_subtitle, tagline_subtitle_font_size, usage_guide,
                readme_cache, min_app_version, depends_on, enabled,
                max_tools, max_dependencies_kb, declared_qps
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(identifier) DO UPDATE SET
                name=excluded.name,
                name_i18n_key=excluded.name_i18n_key,
                description=excluded.description,
                version=excluded.version,
                author=excluded.author,
                author_url=excluded.author_url,
                icon_url=excluded.icon_url,
                price_type=excluded.price_type,
                price_amount=excluded.price_amount,
                price_interval=excluded.price_interval,
                price_quarter_fen=excluded.price_quarter_fen,
                price_year_fen=excluded.price_year_fen,
                compatible_editions=excluded.compatible_editions,
                trial_days=excluded.trial_days,
                download_url=excluded.download_url,
                package_hash=excluded.package_hash,
                file_size=excluded.file_size,
                category=excluded.category,
                tags=excluded.tags,
                screenshots=excluded.screenshots,
                readme_url=excluded.readme_url,
                tagline=excluded.tagline,
                tagline_i18n_key=excluded.tagline_i18n_key,
                tagline_font_size=excluded.tagline_font_size,
                tagline_color=excluded.tagline_color,
                tagline_subtitle=excluded.tagline_subtitle,
                tagline_subtitle_font_size=excluded.tagline_subtitle_font_size,
                usage_guide=excluded.usage_guide,
                readme_cache=COALESCE(NULLIF(excluded.readme_cache,''), store_plugins.readme_cache),
                min_app_version=excluded.min_app_version,
                depends_on=excluded.depends_on,
                enabled=excluded.enabled,
                max_tools=excluded.max_tools,
                max_dependencies_kb=excluded.max_dependencies_kb,
                declared_qps=excluded.declared_qps,
                updated_at=NOW()
        """, (
            identifier,
            data.get('name', ''),
            data.get('name_i18n_key', ''),
            data.get('description', ''),
            data.get('version', '0.1.0'),
            data.get('author', ''),
            data.get('author_url', ''),
            data.get('icon_url', ''),
            data.get('price_type', 'free'),
            int(data.get('price_amount', 0)),
            data.get('price_interval', 'onetime'),
            int(data.get('price_quarter_fen', 0)),
            int(data.get('price_year_fen', 0)),
            json.dumps(data.get('compatible_editions', [])),
            int(data.get('trial_days', 0)),
            data.get('download_url', ''),
            data.get('package_hash', ''),
            int(data.get('file_size', 0)),
            data.get('category', ''),
            json.dumps(data.get('tags', [])),
            json.dumps(data.get('screenshots', [])),
            data.get('readme_url', ''),
            tagline,
            data.get('tagline_i18n_key', ''),
            data.get('tagline_font_size', '16px'),
            data.get('tagline_color', '#ffffff'),
            tagline_subtitle,
            data.get('tagline_subtitle_font_size', '14px'),
            data.get('usage_guide', ''),
            data.get('readme_cache', ''),
            data.get('min_app_version', '0.10.0'),
            json.dumps(data.get('depends_on', {})),
            int(data.get('enabled', 1)),
            _max_tools,
            _max_deps_kb,
            _qps,
        ))
        conn.commit()

    # 同步插件使用说明到系统知识库（RAG 可检索；空/下架即软删）
    _sync_plugin_usage_kb(identifier, data.get('name', ''), data.get('usage_guide', ''), int(data.get('enabled', 1)))

    return _json_result(True, data={'identifier': identifier, 'saved': True})


# ── 39. 商店管理：删除插件商品 ────────────────────────────

@bp.route('/store/admin/<identifier>', methods=['DELETE'])
def store_admin_delete(identifier: str):
    """管理员：删除商店插件商品"""
    err = _require_store_admin()
    if err:
        return err

    with get_registry_db() as conn:
        row = conn.execute(
            'SELECT 1 FROM store_plugins WHERE identifier=%s', (identifier,)).fetchone()
        if not row:
            return _json_result(False, error=f'Plugin "{identifier}" not found', code=404)
        conn.execute('DELETE FROM store_plugins WHERE identifier=%s', (identifier,))
        conn.execute('DELETE FROM plugin_reviews WHERE plugin_identifier=%s', (identifier,))
        # 删除插件时同步软删知识库中的使用说明（用户已确认的清理逻辑）
        conn.execute("UPDATE knowledge_blocks SET deleted_at=NOW() WHERE id=%s AND deleted_at IS NULL",
                     (f'kb_plugin_usage_{identifier}',))
        conn.commit()
    return _json_result(True, data={'deleted': True})


# ── 40. 商店管理：切换上架状态 ────────────────────────────

@bp.route('/store/admin/<identifier>/toggle', methods=['POST'])
def store_admin_toggle(identifier: str):
    """管理员：切换插件上架/下架状态"""
    err = _require_store_admin()
    if err:
        return err

    with get_registry_db() as conn:
        row = conn.execute('SELECT enabled FROM store_plugins WHERE identifier=%s', (identifier,)).fetchone()
        if not row:
            return _json_result(False, error='Plugin not found', code=404)
        new_enabled = 0 if row['enabled'] else 1
        conn.execute('UPDATE store_plugins SET enabled=%s, updated_at=NOW() WHERE identifier=%s',
                     (new_enabled, identifier))
        # 下架时同步软删知识库中的使用说明（用户已确认的清理逻辑）
        if new_enabled == 0:
            conn.execute("UPDATE knowledge_blocks SET deleted_at=NOW() WHERE id=%s AND deleted_at IS NULL",
                         (f'kb_plugin_usage_{identifier}',))
        conn.commit()
    return _json_result(True, data={'identifier': identifier, 'enabled': bool(new_enabled)})


# ── 41. 定价计算器：三档价 / 版本包价预览 ──────────────────

@bp.route('/store/pricing/preview', methods=['POST'])
def store_pricing_preview():
    """定价计算器预览：输入基础月价 → 月/季/年三档；可选版本包对比价。

    请求体 (JSON):
        base_month_fen: int            基础月价（分），必填
        bundle: str                    版本包标识（可选，如 'pro'）
        plugin_month_prices: dict      包内插件月价表 {identifier: 分}（可选，算包价时用）

    响应:
        data.tiered: {'month','quarter','year'}        三档价（分）
        data.bundle: 版本包明细（含 saving_pct 对比）或 null

    说明：仅计算不落库；价格规则来自 plugin_manager.pricing（远端
    pricing_rules.json 优先，失败回退内嵌默认）。
    """
    err = _require_store_admin()
    if err:
        return err
    data = request.json if request.is_json else {}
    base_month_fen_raw = data.get('base_month_fen')
    if base_month_fen_raw is None:
        return _json_result(False, error='缺少必填字段: base_month_fen (int, 单位分)', code=400)
    try:
        base_month_fen = int(base_month_fen_raw)
    except (TypeError, ValueError):
        return _json_result(False, error='base_month_fen must be int (fen)', code=400)
    if base_month_fen < 0:
        return _json_result(False, error='base_month_fen must be >= 0', code=400)

    from .pricing import compute_tiered_price, compute_bundle_price
    tiered = compute_tiered_price(base_month_fen)

    bundle = None
    if data.get('bundle'):
        try:
            month_prices = {str(k): int(v) for k, v in (data.get('plugin_month_prices') or {}).items()}
            bundle = compute_bundle_price(str(data['bundle']), month_prices)
        except Exception as e:
            return _json_result(False, error=f'bundle calculation failed: {e}', code=400)

    return _json_result(True, data={'tiered': tiered, 'bundle': bundle})


# ====================================================================
# 商店 API
# ====================================================================

# ── 18. 浏览商店 ─────────────────────────────────────────

def _check_paid_entitlement(identifier: str, detail: dict):
    """阶段 3 付费闸门：sub/onetime 插件需有效订阅或 License 才能安装。

    官方版（VR_EDITION=official）直接授权；free/trial 不受限。
    返回 None 表示通过；否则返回 (jsonify, code) 供视图直接 return。
    """
    price_type = (detail or {}).get('price_type', 'free')
    if price_type not in ('sub', 'onetime'):
        return None
    try:
        from .license import _is_official_edition
        if _is_official_edition():
            return None
    except Exception:
        pass
    # 1) plugin_subscriptions 有效订阅
    try:
        from .subscription import get_subscription_manager
        sub = get_subscription_manager().get_subscription(identifier)
        if sub and sub.status.value == 'active':
            return None
    except Exception:
        pass
    # 2) plugin_licenses 有效 License（含 bundle 成员授权）
    try:
        with get_registry_db() as conn:
            row = conn.execute(
                "SELECT id FROM plugin_licenses WHERE plugin_id=%s "
                "AND license_status IN ('active','grace')",
                (identifier,)
            ).fetchone()
        if row:
            return None
    except Exception:
        pass
    return _json_result(False,
        error=f'Paid plugin "{identifier}" requires an active subscription',
        code=402)


def _annotate_store_plugins(mgr, plugins: list) -> None:
    """为商店插件批量注入 installed / has_update / latest_version 标记

    就地修改 plugins 中的 dict；内部异常已捕获，不影响原有响应。
    """
    from .store import DEPLOY_EDITION, StoreAPIClient
    if not plugins:
        return

    # 收集本地已安装版本映射（仅针对当前页插件，避免全量查询）
    local_versions = {}
    for p in plugins:
        info = mgr.get_info(p.get('identifier', ''))
        if info:
            local_versions[p['identifier']] = info.version

    updates = {}
    if local_versions and mgr.store_client:
        try:
            updates = mgr.store_client.check_updates(local_versions)
        except Exception as e:
            print(f'[routes] _annotate_store_plugins check_updates failed: {e}')

    for p in plugins:
        p['installed'] = p.get('identifier') in local_versions
        u = updates.get(p.get('identifier'))
        p['has_update'] = bool(u and u.get('has_update'))
        p['latest_version'] = (u or {}).get('latest') or p.get('version')
        # 阶段 3：标记部署版本兼容性（前端可提示"当前版本不适用"）
        p['current_edition'] = DEPLOY_EDITION
        p['compatible_edition'] = StoreAPIClient._edition_compatible(
            p.get('compatible_editions') or [])


@bp.route('/store/browse', methods=['GET'])
def store_browse():
    """浏览商店插件列表"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr or not mgr.store_client:
        return _json_result(False, error='Store not available', code=503)

    query = request.args.get('q', '')
    category = request.args.get('category', '')
    price_type = request.args.get('price_type', '')
    sort_by = request.args.get('sort_by', 'downloads')
    try:
        page = _parse_positive_int('page', 1)
        page_size = _parse_positive_int('page_size', 100, 1, 100)
    except ValueError as e:
        return _json_result(False, error=str(e), code=400)

    data = mgr.store_client.search(query, category, price_type, page, page_size, sort_by)
    # 本地化：名称/宣传语自动继承系统语言
    for _pl in data.get('plugins', []):
        _localize(_pl)
    # 版本发现：标记已安装 / 可升级状态
    try:
        _annotate_store_plugins(mgr, data.get('plugins', []))
    except Exception as e:
        print(f'[routes] store_browse annotate failed: {e}')

    # v1.8：动态分流后置过滤（标准 §18.2）。
    # 刻意放在路由层而非 store.py，以保住既有 SQL 层零改动。
    # 规则表为空 / resolver 关闭 / 取数失败 → 全部插件可见（行为等同 v1.7）。
    # 注：total 为「本页命中数」的兜底修正；规则隐藏项跨页时 total 有轻微偏差（可接受）。
    try:
        from .distribution import resolve_visible_set, current_profile
        from .store import DEPLOY_EDITION
        _plugins = data.get('plugins', [])
        _vis, _hid = resolve_visible_set(
            DEPLOY_EDITION, [p.get('identifier') for p in _plugins], current_profile())
        if _hid:
            _keep = set(_vis)
            data['plugins'] = [p for p in _plugins if p.get('identifier') in _keep]
            if isinstance(data.get('total'), int):
                data['total'] = max(0, data['total'] - len(_hid))
            print(f'[store] 🔀 动态分流隐藏 {len(_hid)} 项: {sorted(_hid)[:5]}')
    except Exception as e:
        print(f'[routes] store_browse distribution filter failed: {e}')

    return _json_result(True, data=data)


# ── 用户端商店公开只读接口（P0-2）────────────────────────
# 现有 /store/* 均为 _require_admin() 的管理员接口；用户端商店
# （site_builder /shop 页面）需公开只读目录，故新增 public 变体，
# 不做管理标注（_annotate_store_plugins）、不暴露下载 URL。

@bp.route('/store/public/browse', methods=['GET'])
def store_public_browse():
    """用户端商店浏览（公开只读，无管理员标注）。"""
    mgr = _get_manager()
    if not mgr or not mgr.store_client:
        return _json_result(False, error='Store not available', code=503)
    query = request.args.get('q', '')
    category = request.args.get('category', '')
    price_type = request.args.get('price_type', '')
    sort_by = request.args.get('sort_by', 'downloads')
    try:
        page = _parse_positive_int('page', 1)
        page_size = _parse_positive_int('page_size', 20, 1, 100)
    except ValueError as e:
        return _json_result(False, error=str(e), code=400)
    data = mgr.store_client.search(query, category, price_type, page, page_size, sort_by)
    for _pl in data.get('plugins', []):
        _localize(_pl)
        _scrub_public(_pl)
    return _json_result(True, data=data)


@bp.route('/store/public/<identifier>', methods=['GET'])
def store_public_detail(identifier: str):
    """用户端插件详情（公开只读）。"""
    mgr = _get_manager()
    if not mgr or not mgr.store_client:
        return _json_result(False, error='Store not available', code=503)
    detail = mgr.store_client.get_detail(identifier)
    if not detail:
        return _json_result(False, error='Plugin not found', code=404)
    _localize(detail)
    _scrub_public(detail)
    return _json_result(True, data=detail)


@bp.route('/mcp/<plugin_id>/manifest', methods=['GET'])
def plugin_mcp_manifest(plugin_id: str):
    """P2-5: 插件 MCP 能力清单（对外暴露，供外部 MCP client 发现）。"""
    err = _require_store_admin()
    if err:
        return err
    from .mcp import build_manifest
    manifest = build_manifest(plugin_id)
    if manifest is None:
        return _json_result(False, error='No MCP servers for this plugin', code=404)
    return _json_result(True, data=manifest)


# ── 19. 商店插件详情 ─────────────────────────────────────

@bp.route('/store/<identifier>', methods=['GET'])
def store_detail(identifier: str):
    """商店插件详情"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr or not mgr.store_client:
        return _json_result(False, error='Store not available', code=503)

    detail = mgr.store_client.get_detail(identifier)
    if not detail:
        return _json_result(False, error=f'Plugin "{identifier}" not found in store', code=404)
    # 本地化：名称/宣传语自动继承系统语言
    _localize(detail)
    # 版本发现：标记已安装 / 可升级状态
    try:
        _annotate_store_plugins(mgr, [detail])
    except Exception as e:
        print(f'[routes] store_detail annotate failed: {e}')
    return _json_result(True, data=detail)


@bp.route('/store/<identifier>/readme', methods=['GET'])
def store_readme_proxy(identifier: str):
    """服务端 README 代理（问题2 方案A）：优先读本地缓存，空则现场抓 readme_url。

    多命名兼容（README.cn.md / README_CN.md / README.zh-CN.md）由导入期多命名抓取落库
    （store_importer），无缓存时按 readme_url 现场抓取（timeout 8s），失败返回空串。
    """
    err = _require_admin()
    if err:
        return err
    try:
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT readme_cache, readme_url FROM store_plugins WHERE identifier=%s',
                (identifier,)
            ).fetchone()
    except Exception as _e:
        print(f'[routes] readme proxy db error: {_e}')
        return _json_result(False, error='Store not available', code=503)
    if not row:
        return _json_result(False, error=f'Plugin "{identifier}" not found in store', code=404)
    cache = (row['readme_cache'] or '').strip()
    if cache:
        return _json_result(True, data={'text': cache, 'from_cache': True})
    url = (row['readme_url'] or '').strip()
    if not url:
        return _json_result(True, data={'text': '', 'from_cache': False})
    text = _readme_text(url)
    return _json_result(True, data={'text': text, 'from_cache': False})


# ── 20. 从商店安装 ───────────────────────────────────────

@bp.route('/store/<identifier>/install', methods=['POST'])
def store_install(identifier: str):
    """从商店下载并安装插件"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)
    if not mgr.store_client:
        return _json_result(False, error='Store not available', code=503)

    # 获取商店插件详情
    detail = mgr.store_client.get_detail(identifier)
    if not detail:
        return _json_result(False, error=f'Plugin "{identifier}" not found in store', code=404)

    # 如果已安装，直接返回
    existing = mgr.get_info(identifier)
    if existing and existing.status.value not in ('unknown', 'uninstalled'):
        return _json_result(True, data={
            'identifier': identifier,
            'status': 'already_installed',
            'version': existing.version,
        })

    # v1.8：动态分流准入闸门（标准 §18.2「展示 / 安装准入 / 运行时门控读同一优先级链」）。
    # 顺序刻意置于 yaml/compatible_editions 校验之前 —— 与优先级链一致。
    # 失败一律放行（fail-open），绝不影响既有安装主链路；无规则时 resolve() 返回 None。
    try:
        from .store import DEPLOY_EDITION as _dist_edition
        from .distribution import resolve as _dist_resolve, current_profile as _dist_profile
        _dres = _dist_resolve(identifier, _dist_edition, _dist_profile())
        if _dres is not None and not _dres[0]:
            return _json_result(False,
                error=f'Plugin "{identifier}" is not available for this deployment ({_dres[1]})',
                code=403)
    except Exception as _de:
        print(f'[routes] store install 分流闸门取数失败（放行）: {_de}')

    # 阶段 3：部署版本兼容校验
    from .store import DEPLOY_EDITION, StoreAPIClient
    if not StoreAPIClient._edition_compatible(detail.get('compatible_editions') or []):
        return _json_result(False,
            error=f'Plugin "{identifier}" is not compatible with current edition ({DEPLOY_EDITION})',
            code=403)
    # 阶段 3：付费闸门（仅付费插件需要有效订阅/授权）
    gate = _check_paid_entitlement(identifier, detail)
    if gate:
        return gate

    # 获取下载地址（含版本兼容校验）
    app_version = getattr(mgr.app, 'version', '')
    download_url, fallback_url = mgr.store_client.get_download_urls(
        identifier, app_version)
    if not download_url:
        # 区分：无下载 URL vs 版本不兼容
        detail_version = detail.get('min_app_version', '')
        if detail_version and app_version:
            from .store import StoreAPIClient
            if not StoreAPIClient._version_compatible(app_version, detail_version):
                return _json_result(False,
                    error=f'App version ({app_version}) < required ({detail_version}). Please upgrade.',
                    code=400)
        return _json_result(False, error=f'No download URL for "{identifier}"', code=404)

    # 下载并解压到 plugins/<identifier>/
    plugin_dest = os.path.join(mgr.plugins_dir, identifier)
    try:
        from .downloader import download_plugin
        package_hash = detail.get('package_hash', '')
        download_plugin(download_url, plugin_dest,
                        expected_hash=package_hash,
                        fallback_url=fallback_url or '')
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Download failed: {e}', code=500)

    # 安装插件
    try:
        info = mgr.install(identifier)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Install failed: {e}', code=500)

    # 安装后自动启用（管理员代装流程）：启用成功后路由需重启服务生效
    enabled = False
    enable_error = None
    try:
        mgr.enable(identifier)
        enabled = True
    except Exception as e:
        traceback.print_exc()
        enable_error = f'Enable failed: {e}'

    data = {
        'identifier': identifier,
        'status': info.status.value if hasattr(info.status, 'value') else str(info.status),
        'version': info.version,
        'auto_enabled': enabled,
    }
    if enabled:
        # 新启用插件的路由需重启后挂载（Flask 运行期无法动态注册蓝图）
        data['needs_restart'] = True
        _schedule_service_restart()
    else:
        data['enable_error'] = enable_error
    return _json_result(True, data=data)


# ── 商店在线升级 ──────────────────────────────────

def _schedule_service_restart(delay: float = 3.0):
    """后台延迟重启所有挂载插件路由的服务，使新启用/升级插件的路由生效。

    Flask 运行期无法动态注册蓝图，插件路由统一在启动时挂载；
    安装/启用/升级后必须重启 admin/main/auth 三个服务。
    用 systemd-run 创建独立 transient unit 执行重启，脱离 admin 服务自身
    cgroup——否则重启 admin 时会连带终止当前进程（含本 sudo 子进程），
    导致后续服务不重启。sudo systemd-run 免密已在服务器配置。
    """
    def _restart():
        time.sleep(delay)
        try:
            subprocess.Popen(
                ['sudo', 'systemd-run', '--collect', '--no-block',
                 'systemctl', 'restart',
                 'verorun-admin', 'verorun-main', 'verorun-auth'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            print(f'[PluginManager] ⚠️ restart services failed: {e}')
    threading.Thread(target=_restart, daemon=True).start()

@bp.route('/store/<identifier>/upgrade', methods=['POST'])
def store_upgrade(identifier: str):
    """从商店在线升级已安装插件到最新版本。

    升级成功后若插件处于启用/激活状态，需要重启 admin 服务
    使新代码生效（返回 needs_restart=true，并触发后台延迟重启）。
    """
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)
    if not mgr.store_client:
        return _json_result(False, error='Store not available', code=503)

    try:
        result = mgr.upgrade(identifier)
    except PluginNotFoundError:
        return _json_result(False, error=f'插件 {identifier} 未安装', code=404)
    except PluginStateError as e:
        return _json_result(False, error=str(e), code=409)
    except PluginVersionError as e:
        return _json_result(False, error=str(e), code=409)
    except PluginDependencyError as e:
        return _json_result(False, error=str(e), code=400)
    except ValueError as e:
        return _json_result(False, error=f'Upgrade rejected: {e}', code=400)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Upgrade failed: {e}', code=500)

    # 需要重启时：后台延迟重启所有挂载插件路由的服务（sudo 免密已配置）
    if result.get('needs_restart'):
        _schedule_service_restart()

    return _json_result(True, data=result)


# ── 版本兼容性检查 ──────────────────────────────────

@bp.route('/store/check-compatibility/<identifier>', methods=['GET'])
def store_check_compatibility(identifier: str):
    """检查插件与当前系统版本的兼容性"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)
    if not mgr.store_client:
        return _json_result(False, error='Store not available', code=503)

    detail = mgr.store_client.get_detail(identifier)
    if not detail:
        return _json_result(False, error=f'Plugin "{identifier}" not found', code=404)

    app_version = getattr(mgr.app, 'version', '')
    min_ver = detail.get('min_app_version', '')
    compatible = True
    if app_version and min_ver:
        from .store import StoreAPIClient
        compatible = StoreAPIClient._version_compatible(app_version, min_ver)

    return _json_result(True, data={
        'identifier': identifier,
        'app_version': app_version,
        'min_app_version': min_ver,
        'compatible': compatible,
        'plugin_version': detail.get('version', ''),
    })


@bp.route('/store/public/check-compatibility/<identifier>', methods=['GET'])
def store_public_check_compatibility(identifier: str):
    """公开兼容性检查 — 无需登录，供用户在商店浏览前预判兼容性"""
    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)
    if not mgr.store_client:
        return _json_result(False, error='Store not available', code=503)

    detail = mgr.store_client.get_detail(identifier)
    if not detail:
        return _json_result(False, error=f'Plugin "{identifier}" not found', code=404)

    app_version = getattr(mgr.app, 'version', '')
    min_ver = detail.get('min_app_version', '')
    compatible = True
    if app_version and min_ver:
        from .store import StoreAPIClient
        compatible = StoreAPIClient._version_compatible(app_version, min_ver)

    return _json_result(True, data={
        'identifier': identifier,
        'app_version': app_version,
        'min_app_version': min_ver,
        'compatible': compatible,
        'plugin_version': detail.get('version', ''),
    })


# ====================================================================
# ★ v1.4 用户上传自研插件
# ====================================================================

_UPLOAD_LIMIT = 5      # 每 token 每分钟最多上传次数
_UPLOAD_WINDOW = 60    # 限流窗口（秒）
_UPLOAD_CLEANUP_TS = 0


def _upload_rate_limited(token: str) -> bool:
    """VR-SEC-012: DB 共享限流（多 worker 生效），True = 超过限制。

    利用现有 system_config 表原子递增计数，替代原先进程内 defaultdict（可被多 worker 绕过）。
    DB 异常时 fail-open 并打印日志，避免限流模块自身导致上传不可用。
    """
    import hashlib as _hashlib
    global _UPLOAD_CLEANUP_TS
    window = int(time.time() // _UPLOAD_WINDOW)
    key = f'upload_rl_{_hashlib.md5(token.encode()).hexdigest()[:16]}_{window}'
    try:
        from .models import get_registry_db
        with get_registry_db() as conn:
            cur = conn.execute(
                "INSERT INTO system_config (key, value) VALUES (%s, '1') "
                "ON CONFLICT (key) DO UPDATE SET value=(system_config.value::int + 1), updated_at=NOW() "
                "RETURNING value::int",
                (key,),
            )
            row = cur.fetchone()
            conn.commit()
        count = int(row['value']) if row else 0

        # 机会式清理过期窗口记录（每分钟至多一次，幂等）
        if time.time() - _UPLOAD_CLEANUP_TS > _UPLOAD_WINDOW:
            _UPLOAD_CLEANUP_TS = time.time()
            with get_registry_db() as conn:
                conn.execute(
                    "DELETE FROM system_config WHERE key LIKE 'upload_rl_%' "
                    "AND updated_at < NOW() - INTERVAL '10 minutes'"
                )
                conn.commit()
        return count > _UPLOAD_LIMIT
    except Exception:
        traceback.print_exc()
        return False


@bp.route('/upload', methods=['POST'])
def upload_plugin():
    """用户上传自研插件 zip 包 → 校验 → 安装。

    Multipart form: file=<plugin.zip>
    """
    err = _require_admin()
    if err:
        return err

    import os as _os
    import zipfile as _zipfile
    import tempfile as _tempfile
    import time as _time

    # ★ VR-SEC-012: DB 共享限流（每 token 每分钟最多 5 次上传，多 worker 生效）
    _token = request.headers.get('Authorization', '')
    if _upload_rate_limited(_token):
        return _json_result(False, error='Too many uploads. Please wait.', code=429)

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    # 1. 检查文件
    if 'file' not in request.files:
        return _json_result(False, error='No file uploaded', code=400)

    file = request.files['file']
    if not file.filename or not file.filename.lower().endswith('.zip'):
        return _json_result(False, error='Only .zip files are accepted', code=400)

    # ★ P1: 文件大小限制 50MB
    file.seek(0, 2)  # SEEK_END
    _file_size = file.tell()
    file.seek(0)
    _max_size = 50 * 1024 * 1024
    if _file_size > _max_size:
        return _json_result(False, error=f'File too large ({_file_size / 1024 / 1024:.1f}MB). Max 50MB.', code=400)

    # ★ P3: 校验 zip magic bytes
    _magic = file.read(4)
    file.seek(0)
    if _magic != b'PK\x03\x04':
        return _json_result(False, error='Invalid zip file (bad magic bytes)', code=400)

    tmp_path = None
    extract_dir = None

    try:
        # 2. 保存上传文件到临时目录
        fd, tmp_path = _tempfile.mkstemp(suffix='.upload.zip')
        _os.close(fd)
        file.save(tmp_path)

        # 3. 读取 zip 内的 plugin.json
        with _zipfile.ZipFile(tmp_path, 'r') as zf:
            # 安全路径检查
            json_entry = None
            for name in zf.namelist():
                cleaned = _os.path.normpath(name).replace('\\', '/')
                if cleaned.endswith('/plugin.json') or cleaned == 'plugin.json':
                    json_entry = name
                    break

            if json_entry is None:
                return _json_result(False, error='plugin.json not found in zip root', code=400)

            json_raw = zf.read(json_entry).decode('utf-8')

        # 4. 解析 plugin.json
        plugin_meta = json.loads(json_raw)
        identifier = (plugin_meta.get('identifier') or '').strip().lower()
        name = (plugin_meta.get('name') or '').strip()
        version = (plugin_meta.get('version') or '').strip()

        # 基本校验
        if not identifier:
            return _json_result(False, error='plugin.json missing required field: identifier', code=400)
        if not name:
            return _json_result(False, error='plugin.json missing required field: name', code=400)
        if not version:
            return _json_result(False, error='plugin.json missing required field: version', code=400)

        # identifier 合法字符
        import re as _re
        if not _re.match(r'^[a-z0-9_]+$', identifier):
            return _json_result(False, error=f'Invalid identifier: "{identifier}". Use only lowercase letters, digits, underscores.', code=400)

        # ★ P7: 校验 min_app_version 兼容性
        min_app_ver = (plugin_meta.get('min_app_version') or '').strip()
        if min_app_ver:
            app_version = getattr(mgr.app, 'version', '')
            if app_version:
                from .store import StoreAPIClient
                if not StoreAPIClient._version_compatible(app_version, min_app_ver):
                    return _json_result(False, error=f'Plugin requires min_app_version={min_app_ver}, but current version is {app_version}', code=400)

        # ★ P1-3: Coze 式配额校验（工具数 = plugin.json capabilities 数量）
        _caps = plugin_meta.get('capabilities') or []
        if not isinstance(_caps, list):
            _caps = []
        _max_tools = int(plugin_meta.get('max_tools') or 100)
        if _max_tools < 1 or _max_tools > 1000:
            _max_tools = 100
        if len(_caps) > _max_tools:
            return _json_result(False,
                error=f'capabilities count ({len(_caps)}) exceeds max_tools ({_max_tools})',
                code=400)

        # 5. 检查插件目录是否已存在
        plugins_root = getattr(mgr, '_plugins_root', _os.path.join(_os.path.dirname(__file__), '..', 'plugins'))
        plugins_root = _os.path.abspath(plugins_root)
        dest_dir = _os.path.join(plugins_root, identifier)

        if _os.path.exists(dest_dir):
            return _json_result(False, error=f'Plugin directory already exists: {identifier}. Remove it first or choose a different identifier.', code=409)

        # 6. 安全解压
        from .downloader import _extract_archive
        _os.makedirs(dest_dir, exist_ok=True)
        _extract_archive(tmp_path, dest_dir)

        # ★ 6a. 解压后护栏（M1 修复）：解压体积/文件数上限，防解压炸弹
        _guard_total = 0
        _guard_count = 0
        for _gdp, _gdns, _gfns in _os.walk(dest_dir):
            for _gfn in _gfns:
                _guard_count += 1
                _guard_total += _os.path.getsize(_os.path.join(_gdp, _gfn))
                if _guard_count > 2000 or _guard_total > 200 * 1024 * 1024:
                    import shutil as _shutil_guard
                    _shutil_guard.rmtree(dest_dir, ignore_errors=True)
                    return _json_result(False, error='插件解压后体积或文件数超限，已拒绝', code=400)

        # ★ 6a1. P1-3: 体积配额校验（声明 max_dependencies_kb，默认 200MB 对齐护栏）
        _max_deps_kb = int(plugin_meta.get('max_dependencies_kb') or 204800)
        if _max_deps_kb < 1024 or _max_deps_kb > 512 * 1024:
            _max_deps_kb = 204800
        if _guard_total > _max_deps_kb * 1024:
            import shutil as _shutil_q
            _shutil_q.rmtree(dest_dir, ignore_errors=True)
            return _json_result(False,
                error=(f'解压体积 {_guard_total / 1024 / 1024:.1f}MB '
                       f'超过配额 {_max_deps_kb / 1024:.0f}MB'),
                code=400)

        # ★ 6b. 官方插件水印检测（VeroRun 官方插件水印体系）
        # M2/M3 修复：仅「签名验签通过」为不可辩驳 → 上传即硬拒；
        # manifest/注释水印/_wm 字段/白名单 → 降级进审核队列，由 AI 复核 + 人工兜底。
        from .watermark import detect_official_watermark, WM_HARD
        _wm = detect_official_watermark(dest_dir)
        if _wm.get('official') and _wm.get('method') in WM_HARD:
            # 回滚：删除已解压的目录
            import shutil as _shutil_wm
            _shutil_wm.rmtree(dest_dir, ignore_errors=True)
            return _json_result(False, error=(
                f'检测到官方插件二次打包'
                f'（identifier={_wm.get("identifier") or "未知"}，'
                f'命中方式：{_wm.get("reason", "")}）。'
                '官方插件请从插件商店安装，禁止重新打包上传。'
            ), code=400)

        # ★ 6c. 进入审核队列（两阶段审核 · 批次2：pending → AI 规则审核 → 批准后安装）
        import shutil as _shutil_sub
        pending_root = _os.path.join(plugins_root, '.pending')
        _os.makedirs(pending_root, exist_ok=True)
        pending_dir = _os.path.join(pending_root, identifier)
        if _os.path.exists(pending_dir):
            _shutil_sub.rmtree(pending_dir, ignore_errors=True)
        _shutil_sub.move(dest_dir, pending_dir)

        # 上传者信息（JWT payload）
        _submitter = ''
        _submitter_id = ''
        try:
            from services.jwt_service import validate_token
            _tok = request.headers.get('Authorization', '').replace('Bearer ', '')
            if not _tok:
                _tok = request.args.get('token') or request.cookies.get('sso_token') or request.cookies.get('tm_token')
            _pl = validate_token(_tok) if _tok else None
            if _pl:
                _submitter = str(_pl.get('username') or _pl.get('name') or '')
                _submitter_id = str(_pl.get('user_id') or '')
        except Exception:
            pass

        # 写入审核记录
        from .models import get_registry_db
        _sub_id = None
        try:
            with get_registry_db() as conn:
                _cur = conn.execute(
                    "INSERT INTO plugin_submissions "
                    "(identifier, name, version, status, submitter, submitter_id, "
                    " file_path, file_size, wm_method, wm_reason) "
                    "VALUES (%s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s) RETURNING id",
                    (identifier, name, version, _submitter, _submitter_id,
                     pending_dir, _file_size, _wm.get('method', ''), _wm.get('reason', ''))
                )
                _row = _cur.fetchone()
                conn.commit()
            _sub_id = _row['id'] if _row else None
        except Exception:
            traceback.print_exc()
            _shutil_sub.rmtree(pending_dir, ignore_errors=True)
            return _json_result(False, error='Failed to create submission record', code=500)

        # ★ P0-C：写入审核记录后立即自动执行 AI 规则审核（只写报告，不改 pending 状态）
        try:
            from .audit import review_plugin
            _auto = review_plugin(pending_dir,
                                  watermark_result=_wm,
                                  plugins_root=plugins_root,
                                  submitted_version=version)
            with get_registry_db() as conn:
                conn.execute(
                    "UPDATE plugin_submissions SET audit_status=%s, audit_report=%s, "
                    "audit_reasons=%s, reviewed_at=NOW(), updated_at=NOW() WHERE id=%s",
                    (_auto['status'],
                     json.dumps(_auto['report'], ensure_ascii=False),
                     json.dumps(_auto['reasons'], ensure_ascii=False),
                     _sub_id))
                conn.commit()
        except Exception:
            traceback.print_exc()   # 自动审核失败不阻断上传，保留 pending 供人工
            # 审计 P2-6 修复：失败不得静默，落 review_comment 标记待人工复核
            try:
                with get_registry_db() as conn:
                    conn.execute(
                        "UPDATE plugin_submissions SET review_comment=%s, updated_at=NOW() "
                        "WHERE id=%s",
                        ('auto audit failed, needs manual review', _sub_id))
                    conn.commit()
            except Exception:
                traceback.print_exc()

        return _json_result(True, data={
            'submission_id': _sub_id,
            'identifier': identifier,
            'name': name,
            'version': version,
            'status': 'pending',
            'message': '插件已提交审核，审核通过后将自动安装。',
        })

    except json.JSONDecodeError:
        return _json_result(False, error='plugin.json is not valid JSON', code=400)
    except ValueError as e:
        return _json_result(False, error=f'Invalid archive: {e!s}', code=400)
    except Exception as e:
        # 出错时尝试清理解压目录
        if identifier:
            try:
                dest = _os.path.join(plugins_root, identifier)
                if _os.path.exists(dest):
                    import shutil as _shutil
                    _shutil.rmtree(dest, ignore_errors=True)
            except Exception:
                pass
        return _json_result(False, error=f'Upload failed: {e!s}', code=500)
    finally:
        if tmp_path and _os.path.exists(tmp_path):
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass


# ====================================================================
# ★ 批次2 插件审核队列（AI 审核网关 · 插件标准 §16）
# ====================================================================

def _submission_row_to_dict(row) -> dict:
    """DB row → dict，并将 JSON 字段解析为对象。"""
    try:
        d = dict(row)
    except Exception:
        d = {k: row[k] for k in row.keys()}
    for key, fallback in (('audit_report', {}), ('audit_reasons', [])):
        raw = d.get(key)
        if isinstance(raw, str):
            try:
                d[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d[key] = fallback
    return d


@bp.route('/submissions', methods=['GET'])
def list_submissions():
    """列出插件审核队列（默认 pending；支持 ?status=pending|approved|rejected）"""
    err = _require_store_admin()
    if err:
        return err
    status = request.args.get('status', 'pending')
    _limit = request.args.get('limit', default=200, type=int)
    _limit = max(1, min(_limit, 500))
    _offset = request.args.get('offset', default=0, type=int)
    _offset = max(0, _offset)
    try:
        from .models import get_registry_db
        with get_registry_db() as conn:
            cur = conn.execute(
                "SELECT * FROM plugin_submissions WHERE status = %s "
                "ORDER BY id DESC LIMIT %s OFFSET %s",
                (status, _limit, _offset),
            )
            rows = cur.fetchall()
        return _json_result(True, data=[_submission_row_to_dict(r) for r in rows])
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed to list submissions: {e}', code=500)


@bp.route('/submissions/<int:sub_id>/review', methods=['POST'])
def review_submission(sub_id):
    """执行规则引擎审核（AI 辅助），产出结构化报告"""
    err = _require_store_admin()
    if err:
        return err
    try:
        from .models import get_registry_db
        with get_registry_db() as conn:
            cur = conn.execute(
                "SELECT * FROM plugin_submissions WHERE id = %s", (sub_id,))
            row = cur.fetchone()
        if not row:
            return _json_result(False, error='Submission not found', code=404)
        if row['status'] != 'pending':
            return _json_result(False, error=f'Submission already {row["status"]}', code=400)

        from .audit import review_plugin
        # 审计（第二轮复测 C.3）：手动复核入口补传 submitted_version，
        # 与 upload_plugin / developer_submit 行为一致，触发 P1-5 包内版本一致性守卫
        result = review_plugin(row['file_path'],
                               submitted_version=str(row.get('version') or ''))
        with get_registry_db() as conn:
            conn.execute(
                "UPDATE plugin_submissions SET audit_status=%s, audit_report=%s, "
                "audit_reasons=%s, reviewed_at=NOW(), updated_at=NOW() WHERE id=%s",
                (result['status'],
                 json.dumps(result['report'], ensure_ascii=False),
                 json.dumps(result['reasons'], ensure_ascii=False),
                 sub_id),
            )
            conn.commit()
        return _json_result(True, data=result)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Review failed: {e}', code=500)


@bp.route('/submissions/<int:sub_id>/approve', methods=['POST'])
def approve_submission(sub_id):
    """批准安装：pending 目录移入正式目录 → discover → install → enable → activate"""
    err = _require_store_admin()
    if err:
        return err
    try:
        import shutil as _shutil_app
        from .models import get_registry_db
        with get_registry_db() as conn:
            cur = conn.execute(
                "SELECT * FROM plugin_submissions WHERE id = %s", (sub_id,))
            row = cur.fetchone()
        if not row:
            return _json_result(False, error='Submission not found', code=404)
        if row['status'] != 'pending':
            return _json_result(False, error=f'Submission already {row["status"]}', code=400)

        # ★ H1 修复：强制审计状态校验
        #  - audit_status='reject'（含危险代码）→ 禁止安装，除非显式 override=true 强制放行
        #  - audit_status='pending'（从未执行 AI 审核）→ 必须先 /review
        _audit_status = row.get('audit_status') or 'pending'
        _req_body = request.get_json(silent=True) or {}
        _override = bool(_req_body.get('override'))
        _auto_publish = bool(_req_body.get('auto_publish'))
        if _audit_status == 'reject' and not _override:
            return _json_result(False, error=(
                '审计未通过（audit_status=reject），含危险代码特征，禁止安装；'
                '如需强制放行请显式传递 override=true'), code=400)
        if _audit_status == 'pending':
            return _json_result(False, error='该提交尚未执行 AI 审核，请先调用 /review 后再批准', code=400)

        mgr = _get_manager()
        if not mgr:
            return _json_result(False, error='PluginManager not initialized', code=503)
        plugins_root = getattr(mgr, '_plugins_root', os.path.join(os.path.dirname(__file__), '..', 'plugins'))
        plugins_root = os.path.abspath(plugins_root)
        identifier = row['identifier']
        pending_dir = row['file_path']
        dest_dir = os.path.join(plugins_root, identifier)

        if os.path.exists(dest_dir):
            return _json_result(False, error=f'Plugin directory already exists: {identifier}', code=409)
        if not os.path.isdir(pending_dir):
            return _json_result(False, error=f'Pending directory missing: {pending_dir}', code=500)

        # 安装前最终安全复核：官方签名验签通过 → 拒绝
        from .watermark import detect_official_watermark, WM_HARD
        _wm_final = detect_official_watermark(pending_dir)
        if _wm_final.get('official') and _wm_final.get('method') in WM_HARD:
            return _json_result(False, error=(
                f'安装前复核命中官方插件签名（{_wm_final.get("reason", "")}），拒绝安装。'), code=400)

        # ★ P0-5 扩展：auto_publish 一键上架
        # 顺序关键：发布工具要求 status=approved 且 pending 目录存在；
        # 故先置 approved → 发布（此时目录未 move）→ 发布成功后再本地安装。
        if _auto_publish:
            with get_registry_db() as conn:
                conn.execute(
                    "UPDATE plugin_submissions SET status='approved', updated_at=NOW() WHERE id=%s",
                    (sub_id,))
                conn.commit()
            _pub = _publish_approved(sub_id)
            if _pub[1] != 200:
                # 发布失败：回滚 approved → pending，保持状态机一致
                with get_registry_db() as conn:
                    conn.execute(
                        "UPDATE plugin_submissions SET status='pending', updated_at=NOW() WHERE id=%s",
                        (sub_id,))
                    conn.commit()
                return _pub

        _shutil_app.move(pending_dir, dest_dir)
        discovered = mgr._discovery.discover_one(identifier)
        if discovered is None:
            _shutil_app.rmtree(dest_dir, ignore_errors=True)
            return _json_result(False, error=f'Failed to discover plugin: {identifier}. Check plugin.json structure.', code=500)

        # 标记为 upload 来源 + 安装
        discovered.source = 'upload'
        mgr.install(discovered.identifier)
        mgr.enable(discovered.identifier)
        mgr.activate(discovered.identifier)

        with get_registry_db() as conn:
            conn.execute(
                "UPDATE plugin_submissions SET status='approved', updated_at=NOW() WHERE id=%s",
                (sub_id,))
            conn.commit()

        installed = mgr.get(identifier)
        result = _info_to_dict(installed) if installed else {'identifier': identifier}
        result['submission_id'] = sub_id
        return _json_result(True, data=result)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Approve failed: {e}', code=500)


@bp.route('/submissions/<int:sub_id>/reject', methods=['POST'])
def reject_submission(sub_id):
    """拒绝：清理 pending 目录，标记 rejected"""
    err = _require_store_admin()
    if err:
        return err
    try:
        import shutil as _shutil_rej
        from .models import get_registry_db
        with get_registry_db() as conn:
            cur = conn.execute(
                "SELECT * FROM plugin_submissions WHERE id = %s", (sub_id,))
            row = cur.fetchone()
        if not row:
            return _json_result(False, error='Submission not found', code=404)
        if row['status'] != 'pending':
            return _json_result(False, error=f'Submission already {row["status"]}', code=400)

        pending_dir = row['file_path']
        if os.path.isdir(pending_dir):
            _shutil_rej.rmtree(pending_dir, ignore_errors=True)

        comment = (request.get_json(silent=True) or {}).get('comment', '')
        with get_registry_db() as conn:
            conn.execute(
                "UPDATE plugin_submissions SET status='rejected', review_comment=%s, "
                "updated_at=NOW() WHERE id=%s",
                (comment, sub_id))
            conn.commit()
        return _json_result(True, data={'submission_id': sub_id, 'status': 'rejected'})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Reject failed: {e}', code=500)


# ====================================================================
# License API
# ====================================================================

# ── 21. 激活 License ─────────────────────────────────────

@bp.route('/license/activate', methods=['POST'])
def license_activate():
    """激活 License"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr or not mgr.license_manager:
        return _json_result(False, error='License manager not available', code=503)

    data = request.json if request.is_json else {}
    plugin_id = data.get('plugin_id', '')
    license_key = data.get('license_key', '')
    customer_email = data.get('customer_email', '')

    if not plugin_id or not license_key:
        return _json_result(False, error='plugin_id and license_key required', code=400)

    result = mgr.license_manager.activate(plugin_id, license_key, customer_email)
    if result.get('success'):
        return _json_result(True, data=result.get('license', {}))
    return _json_result(False, error=result.get('error', 'activation failed'), code=400)


# ── 22. 验证 License ─────────────────────────────────────

@bp.route('/license/<plugin_id>/validate', methods=['GET'])
def license_validate(plugin_id: str):
    """验证 License"""
    mgr = _get_manager()
    if not mgr or not mgr.license_manager:
        return _json_result(False, error='License manager not available', code=503)

    result = mgr.license_manager.validate(plugin_id)
    return _json_result(result.get('valid', False), data=result)


# ── 23. 反激活 License ───────────────────────────────────

@bp.route('/license/<plugin_id>/deactivate', methods=['POST'])
def license_deactivate(plugin_id: str):
    """反激活 License"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr or not mgr.license_manager:
        return _json_result(False, error='License manager not available', code=503)

    result = mgr.license_manager.deactivate(plugin_id)
    if result.get('success'):
        return _json_result(True, data={'deactivated': True})
    return _json_result(False, error=result.get('error', 'deactivation failed'), code=400)


# ── 24. License 列表 ──────────────────────────────────────

@bp.route('/licenses', methods=['GET'])
def license_list():
    """列出所有 License"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr or not mgr.license_manager:
        return _json_result(False, error='License manager not available', code=503)

    licenses = mgr.license_manager.list_licenses()
    return _json_result(True, data={'licenses': licenses})


# ====================================================================
# 优惠券 API
# ====================================================================


# ── 24a. 创建优惠券 ──────────────────────────────────────

@bp.route('/coupons', methods=['POST'])
def coupon_create():
    """创建优惠券"""
    err = _require_admin()
    if err:
        return err

    data = request.json if request.is_json else {}
    code = data.get('code', '').strip()
    if not code:
        return _json_result(False, error='code required', code=400)

    cm = get_coupon_manager()
    result = cm.create(
        code=code,
        discount_type=data.get('discount_type', 'percentage'),
        discount_value=int(data.get('discount_value', 0)),
        max_uses=int(data.get('max_uses', 0)),
        min_amount_fen=int(data.get('min_amount_fen', 0)),
        applicable_plugins=data.get('applicable_plugins', []),
        expires_at=data.get('expires_at', ''),
    )
    if result.get('success'):
        return _json_result(True, data=result)
    return _json_result(False, error=result.get('error', 'creation failed'), code=400)


# ── 24b. 校验优惠券 ──────────────────────────────────────

@bp.route('/coupons/validate', methods=['POST'])
def coupon_validate():
    """校验优惠券"""
    data = request.json if request.is_json else {}
    code = data.get('code', '').strip()
    plugin_id = data.get('plugin_id', '')
    amount_fen = int(data.get('amount_fen', 0))
    if not code:
        return _json_result(False, error='code required', code=400)

    cm = get_coupon_manager()
    result = cm.validate(code, plugin_id, amount_fen)
    return _json_result(result.get('valid', False), data=result,
                        error=result.get('error', ''))


# ── 24c. 优惠券列表 ──────────────────────────────────────

@bp.route('/coupons', methods=['GET'])
def coupon_list():
    """列出所有优惠券"""
    err = _require_admin()
    if err:
        return err

    cm = get_coupon_manager()
    coupons = cm.list_coupons()
    return _json_result(True, data={'coupons': coupons})


# ====================================================================
# 支付 / 购买 API
# ====================================================================

from .payment import (
    get_payment_router, create_payment_order,
    update_payment_order, get_payment_order, OrderStatus,
    PaymentChannelNotConfigured,
)

import os
from i18n import _
from .subscription import get_subscription_manager, SubscriptionStatus
from .coupons import get_coupon_manager


# ── 25. 发起购买 ─────────────────────────────────────────

# 订阅周期 → 商店价格字段映射（quarter/year 字段由管理员在定价器配置，
# 未配置时为 0，走 L1 规则按基础月价自动推导）
_INTERVAL_PRICE_FIELD = {
    'month': 'price_amount',
    'quarter': 'price_quarter_fen',
    'year': 'price_year_fen',
}


def _resolve_price_fen(detail: dict, price_type: str, interval: str) -> int:
    """按订阅周期解析应付金额（分）。

    - onetime：一律取 price_amount（interval 不参与计价）
    - sub：优先取管理员为该周期配置的价格（price_quarter_fen / price_year_fen）；
      未配置（0）时按 L1 规则 compute_tiered_price(基础月价) 自动推导该档。
    """
    base = int(detail.get('price_amount') or 0)
    if price_type != 'sub' or interval == 'month':
        return base
    if interval not in _INTERVAL_PRICE_FIELD:
        return base
    configured = int(detail.get(_INTERVAL_PRICE_FIELD[interval]) or 0)
    if configured > 0:
        return configured
    from .pricing import compute_tiered_price
    return compute_tiered_price(base).get(interval, base)


@bp.route('/store/<identifier>/purchase', methods=['POST'])
def store_purchase(identifier: str):
    """发起购买，返回支付二维码"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    store = mgr.store_client
    if not store:
        return _json_result(False, error='Store not available', code=503)

    detail = store.get_detail(identifier)
    bundle_id = ''
    if not detail:
        # 版本包购买：identifier 命中定价规则 bundles → 按成员月价合成包详情
        from .pricing import get_pricing_rules, compute_bundle_price
        rules = get_pricing_rules()
        bundles = rules.get('bundles') or {}
        if identifier in bundles:
            member_prices = {}
            for pid in bundles[identifier].get('plugins', []):
                d = store.get_detail(pid)
                if d:
                    member_prices[pid] = d.get('price_amount', 0)
            try:
                bp = compute_bundle_price(identifier, member_prices, rules)
            except Exception as e:
                return _json_result(False, error=f'Bundle pricing unavailable: {e}', code=400)
            detail = {
                'name': bundles[identifier].get('display_name', identifier),
                'description': 'VeroRun bundle subscription',
                'price_type': 'sub',
                'price_amount': bp['month'],
                'price_quarter_fen': bp['quarter'],
                'price_year_fen': bp['year'],
            }
            bundle_id = identifier
    if not detail:
        return _json_result(False, error=f'Plugin "{identifier}" not found', code=404)

    if detail.get('price_type') == 'free':
        return _json_result(False, error='This plugin is free, no purchase needed', code=400)

    # 防御：非 JSON 请求体统一按空 dict 处理（避免 Flask 415）
    try:
        body = request.json if request.is_json else {}
    except Exception:
        body = {}
    channel = body.get('channel', '')
    customer_email = body.get('customer_email', '')
    coupon_code = (body.get('coupon_code') or '').strip()
    price_type = detail.get('price_type', 'onetime')
    # 用户版强制全订阅制：非官方版只允许 sub，不允许 onetime（一次性买断）
    from .license import _is_official_edition
    if not _is_official_edition() and price_type != 'sub':
        return _json_result(False, error='Subscription only: paid plugins are subscription-based', code=403)
    # 订阅周期：仅 sub 生效；默认取插件配置周期，缺省 month
    interval = (body.get('interval') or detail.get('price_interval') or 'month').strip()
    if price_type == 'sub' and interval not in ('month', 'quarter', 'year'):
        return _json_result(False, error='interval must be month/quarter/year', code=400)
    tier = (body.get('tier') or '').strip()[:64]
    # 按所选周期计价（quarter/year 优先取配置价，未配置走 L1 规则推导）
    amount_fen = _resolve_price_fen(detail, price_type, interval)

    if amount_fen <= 0:
        return _json_result(False, error='Invalid price', code=400)

    # 优惠券校验
    discount_fen = 0
    if coupon_code:
        cm = get_coupon_manager()
        coupon_result = cm.validate(coupon_code, identifier, amount_fen)
        if not coupon_result.get('valid'):
            return _json_result(False, error=coupon_result.get('error', 'Invalid coupon'), code=400)
        discount_fen = coupon_result.get('discount_fen', 0)
        amount_fen = coupon_result.get('final_fen', amount_fen)

    # 检查是否已有 License（版本包走订阅级判定，不在此拦截）
    if not bundle_id and mgr.license_manager:
        existing = mgr.license_manager.get_license(identifier)
        if existing and existing.get('license_status') in ('active', 'grace'):
            return _json_result(False, data={'license': existing},
                                error='Plugin already licensed', code=409)

    # 创建订单
    order = create_payment_order(
        plugin_id=identifier,
        channel=channel,
        amount_fen=amount_fen,
        subject=detail.get('name', identifier),
        description=detail.get('description', ''),
        customer_email=customer_email,
    )

    # 保存订单参数（周期/档位/优惠券）到 order.extra；
    # 支付回调 _activate_license_after_payment 据此创建/续费/恢复订阅
    extra = order.extra.copy()
    if price_type == 'sub':
        extra['interval'] = interval
        extra['tier'] = tier
    if bundle_id:
        extra['bundle_id'] = bundle_id
    if coupon_code:
        extra['coupon_code'] = coupon_code
    if extra:
        update_payment_order(order.order_no, extra=json.dumps(extra))

    # 调用支付网关
    router = get_payment_router()
    try:
        provider = router.get_provider(channel)
    except PaymentChannelNotConfigured as e:
        return _json_result(False, error=str(e), code=400)
    result = provider.create_order(order)

    if result.success:
        update_payment_order(
            order.order_no,
            trade_no=result.trade_no,
            qr_code=result.qr_code,
        )
        return _json_result(True, data={
            'order_no': order.order_no,
            'qr_code': result.qr_code,
            'redirect_url': result.redirect_url,
            'amount_fen': amount_fen,
            'original_fen': detail.get('price_amount', amount_fen),
            'discount_fen': discount_fen,
            'price_type': price_type,
            'interval': interval,
            'tier': tier,
            'channel': channel,
            'coupon_code': coupon_code or '',
        })

    update_payment_order(order.order_no, status='failed')
    return _json_result(False, error=result.error or 'Payment creation failed', code=502)


# ── 26. 查询订单状态 ─────────────────────────────────────

@bp.route('/payment/<order_no>/status', methods=['GET'])
def payment_order_status(order_no: str):
    """查询订单支付状态"""
    order = get_payment_order(order_no)
    if not order:
        return _json_result(False, error='Order not found', code=404)

    return _json_result(True, data=order.to_dict())


# ── 27. 支付回调 Webhook（统一入口） ─────────────────────

@bp.route('/payment/notify/<channel>', methods=['POST'])
def payment_notify(channel: str):
    """Unified payment webhook entry (alipay / wechat / stripe / paypal / mock)."""
    # mock channel is only available in dev environment
    if channel == 'mock' and os.environ.get('DEPLOY_ENV', '') != 'dev':
        return _json_result(False, error=_('mock channel disabled'), code=403)
    router = get_payment_router()
    try:
        provider = router.get_provider(channel)
    except PaymentChannelNotConfigured as e:
        return _json_result(False, error=str(e), code=400)

    # ── 根据 channel 解析原始数据 ──
    raw_data = None
    if channel == 'alipay':
        raw_data = request.form.to_dict()
    elif channel == 'wechat':
        raw_data = request.get_data(as_text=True)
        # 微信回调需要验签 headers
        try:
            import sys as _sys
            _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            _gw_path = os.path.join(_base, 'auth-center', 'routes', 'subscription', 'gateway')
            if _gw_path not in _sys.path:
                _sys.path.insert(0, _gw_path)
            from wechat import handle_notify as wx_notify
            return wx_notify()
        except Exception:
            pass
        return 'FAIL', 400
    elif channel in ('stripe', 'paypal'):
        raw_data = request.get_data()  # bytes
        headers_dict = dict(request.headers)
        is_valid, parsed = provider.verify_notify(raw_data, headers_dict)
        if not is_valid:
            return 'invalid', 400

        order_no = parsed.get('out_trade_no', '')
        trade_no = parsed.get('trade_no', '')

        if not order_no:
            return _json_result(False, error='Missing order_no', code=400)

        order = get_payment_order(order_no)
        if not order:
            return _json_result(False, error='Order not found', code=404)

        if order.status == OrderStatus.PAID:
            return 'success'

        update_payment_order(order_no, status='paid', trade_no=trade_no,
                             paid_at=datetime.now().isoformat())
        _activate_license_after_payment(order, order_no)
        return 'success'
    elif channel == 'mock':
        raw_data = request.json or request.form.to_dict()
    else:
        return _json_result(False, error=f'Unknown channel: {channel}', code=400)

    if raw_data is None:
        return _json_result(False, error='Failed to parse request data', code=400)

    # ── 验证签名（alipay / mock） ──
    is_valid, parsed = provider.verify_notify(raw_data)
    if not is_valid:
        if channel == 'mock':
            parsed = raw_data
        else:
            return 'failure', 400

    trade_status = parsed.get('trade_status', 'TRADE_SUCCESS')
    out_trade_no = parsed.get('out_trade_no', '')
    trade_no = parsed.get('trade_no', '')

    if not out_trade_no:
        return _json_result(False, error='Missing order_no', code=400)

    order = get_payment_order(out_trade_no)
    if not order:
        return _json_result(False, error='Order not found', code=404)

    if order.status == OrderStatus.PAID:
        return 'success'

    if trade_status in ('TRADE_SUCCESS', 'TRADE_FINISHED'):
        update_payment_order(out_trade_no, status='paid', trade_no=trade_no or parsed.get('trade_no', ''),
                             paid_at=datetime.now().isoformat())
        _activate_license_after_payment(order, out_trade_no)
        return 'success'

    return 'pending', 200


def _activate_license_after_payment(order, order_no: str):
    """支付成功后的 License 激活 + 订阅创建 + 钩子触发"""
    mgr = _get_manager()

    # 核销优惠券
    coupon_code = (order.extra or {}).get('coupon_code', '')
    if coupon_code:
        try:
            from .coupons import get_coupon_manager
            get_coupon_manager().apply(coupon_code, order_no)
        except Exception:
            pass

    # 版本包支付：走包订阅 + 成员 License 链路（不激活单插件 License）
    order_extra = order.extra or {}
    bundle_id = order_extra.get('bundle_id') or ''
    if not bundle_id:
        try:
            from .pricing import get_pricing_rules
            if order.plugin_id in (get_pricing_rules().get('bundles') or {}):
                bundle_id = order.plugin_id
        except Exception:
            bundle_id = ''

    if bundle_id:
        _activate_bundle_after_payment(mgr, order, order_no, bundle_id)
        _fire_payment_hook(order.plugin_id, 'purchase', order_no)
        return

    if mgr and mgr.license_manager:
        lic_result = mgr.license_manager.activate(
            plugin_id=order.plugin_id,
            license_key=order_no,
            customer_email=order.customer_email,
        )
        if not lic_result.get('success'):
            print(f'[Payment] License activation failed for {order.plugin_id}')
        else:
            # 订阅闭环：License 激活成功后自动安装+启用插件，菜单随之注册
            _auto_install_enable_plugin(mgr, order.plugin_id)

        store = mgr.store_client
        if store:
            detail = store.get_detail(order.plugin_id)
            if detail and detail.get('price_type') == 'sub':
                sm = get_subscription_manager()
                existing = sm.get_subscription(order.plugin_id)
                # 优先取订单携带的周期/档位（用户在购买页选择）；缺失回退插件配置默认
                order_extra = order.extra or {}
                interval = (order_extra.get('interval') or detail.get('price_interval') or 'month').strip()
                if interval not in ('month', 'quarter', 'year'):
                    interval = 'month'
                tier = (order_extra.get('tier') or '').strip()[:64]
                # 按所选周期取名义价（优惠券扣减不影响续费基准价）
                amount_fen = _resolve_price_fen(detail, 'sub', interval) or order.amount_fen or 0
                if existing and existing.status == SubscriptionStatus.ACTIVE:
                    # 续费/重复支付回调：延长一个周期并同步续期 License
                    if not sm.renew(order.plugin_id):
                        print(f'[PluginSub] renewal failed for {order.plugin_id}')
                elif existing:
                    # 宽限期已锁定（expired/suspended/canceled）后补缴或重新购买：
                    # 恢复订阅与 License（reactivate），避免静默新建重复订阅记录
                    if not sm.reactivate(
                            order.plugin_id,
                            interval_type=interval,
                            amount_fen=amount_fen):
                        print(f'[PluginSub] reactivate failed for {order.plugin_id}')
                else:
                    sm.create(
                        plugin_id=order.plugin_id,
                        license_key=order_no,
                        order_no=order_no,
                        interval_type=interval,
                        amount_fen=amount_fen,
                        tier=tier,
                    )

    _fire_payment_hook(order.plugin_id, 'purchase', order_no)


def _activate_bundle_after_payment(mgr, order, order_no: str, bundle_id: str):
    """版本包支付确认：创建/续费/恢复包订阅 + 成员 License + 自动安装启用。

    - 已有活跃包订阅 → renew（延长周期 + 刷新成员 License）
    - 已有非活跃包订阅 → reactivate（恢复包与成员 License）
    - 无包订阅 → create_bundle（建包订阅 + 生成 13 成员 License + 取消同名旧独立订阅）
    """
    sm = get_subscription_manager()
    order_extra = order.extra or {}
    interval = (order_extra.get('interval') or 'month').strip()
    if interval not in ('month', 'quarter', 'year'):
        interval = 'month'
    amount_fen = order.amount_fen or 0

    existing = sm.get_subscription(bundle_id)
    if existing and existing.status == SubscriptionStatus.ACTIVE:
        # 包续费/重复支付回调：延长一个周期并同步成员 License
        if not sm.renew(bundle_id):
            print(f'[PluginSub] bundle renewal failed for {bundle_id}')
    elif existing:
        # 包补缴/重新购买：恢复包订阅与成员 License
        if not sm.reactivate(bundle_id, interval_type=interval, amount_fen=amount_fen):
            print(f'[PluginSub] bundle reactivate failed for {bundle_id}')
    else:
        sub = sm.create_bundle(bundle_id, license_key=order_no, order_no=order_no,
                               interval_type=interval, amount_fen=amount_fen)
        # 包权限生效：自动安装+启用包内插件（License 已由 create_bundle 生成）
        if sub and mgr:
            for pid in sm.get_bundle_member_ids(bundle_id):
                _auto_install_enable_plugin(mgr, pid)


# ── 28. 退款 ─────────────────────────────────────────────

@bp.route('/payment/<order_no>/refund', methods=['POST'])
def payment_refund(order_no: str):
    """退款"""
    err = _require_admin()
    if err:
        return err

    order = get_payment_order(order_no)
    if not order:
        return _json_result(False, error='Order not found', code=404)

    if order.status != OrderStatus.PAID:
        return _json_result(False, error='Order not paid or already refunded', code=400)

    router = get_payment_router()
    try:
        provider = router.get_provider(order.channel)
    except PaymentChannelNotConfigured as e:
        return _json_result(False, error=str(e), code=400)
    result = provider.refund(order_no)

    if result.success:
        update_payment_order(order_no, status='refunded')
        if _get_manager() and _get_manager().license_manager:
            _get_manager().license_manager.deactivate(order.plugin_id)
        _fire_payment_hook(order.plugin_id, 'refund', order_no)
        return _json_result(True, data={'refunded': True})

    return _json_result(False, error=result.error or 'Refund failed', code=502)


# ====================================================================
# 订阅管理 API
# ====================================================================

# ── 29. 列出所有订阅 ─────────────────────────────────────

@bp.route('/subscriptions', methods=['GET'])
def list_subscriptions():
    """列出所有订阅"""
    err = _require_admin()
    if err:
        return err

    sm = get_subscription_manager()
    subs = [s.to_dict() for s in sm.list_subscriptions()]

    # ── 官方版直接安装/免费安装的插件均无订阅记录，合并进"我的订阅"展示 ──
    installed = []
    subscribed_ids = {s.get('plugin_id') for s in subs}
    try:
        mgr = _get_manager()
        if mgr:
            for info in mgr.list_plugins():
                if info.identifier in subscribed_ids:
                    continue
                if info.status.value in ('uninstalled', 'unknown'):
                    continue
                installed.append(_info_to_dict(info))
    except Exception as e:
        print(f'[routes] list_subscriptions merge installed failed: {e}')

    # 官方版标记：前端据此显示"官方授权"徽标
    try:
        from .license import _is_official_edition
        is_official = bool(_is_official_edition())
    except Exception:
        is_official = False

    return _json_result(True, data={'subscriptions': subs, 'installed': installed, 'official': is_official})


# ── 30. 取消订阅 ─────────────────────────────────────────

@bp.route('/subscriptions/<plugin_id>/cancel', methods=['POST'])
def cancel_subscription(plugin_id: str):
    """取消订阅"""
    err = _require_admin()
    if err:
        return err

    sm = get_subscription_manager()
    try:
        body = request.json if request.is_json else {}
    except Exception:
        body = {}
    immediate = body.get('immediate', False)

    ok = sm.cancel(plugin_id, immediate=immediate)
    if ok:
        return _json_result(True, data={'canceled': True, 'immediate': immediate})
    return _json_result(False, error='Subscription not found', code=404)


# ── 31. 手动续费 ─────────────────────────────────────────

@bp.route('/subscriptions/<plugin_id>/renew', methods=['POST'])
def renew_subscription(plugin_id: str):
    """手动续费"""
    err = _require_admin()
    if err:
        return err

    sm = get_subscription_manager()
    ok = sm.renew(plugin_id)
    if ok:
        sub = sm.get_subscription(plugin_id)
        return _json_result(True, data=sub.to_dict() if sub else {})
    return _json_result(False, error='Renewal failed', code=400)


# ── 31b. 周期变更报价（升级/降级，带剩余价值折算） ─────────

@bp.route('/subscriptions/<plugin_id>/change/quote', methods=['POST'])
def subscription_change_quote(plugin_id: str):
    """周期变更报价：剩余价值折算抵扣新周期价（L5 平滑折算）。

    请求体: {interval: 'month'|'quarter'|'year'}
    响应: data = {from_interval, to_interval, price_fen, credit_fen,
                  pay_fen, carryover_fen, needs_payment}
    """
    err = _require_admin()
    if err:
        return err

    try:
        body = request.json if request.is_json else {}
    except Exception:
        body = {}
    new_interval = (body.get('interval') or '').strip()
    if new_interval not in ('month', 'quarter', 'year'):
        return _json_result(False, error='interval must be month/quarter/year', code=400)

    sm = get_subscription_manager()
    sub = sm.get_subscription(plugin_id)
    if not sub:
        return _json_result(False, error='Subscription not found', code=404)
    if sub.status != SubscriptionStatus.ACTIVE:
        return _json_result(False, error='Only active subscription can be changed', code=400)

    mgr = _get_manager()
    store = mgr.store_client if mgr else None
    detail = store.get_detail(plugin_id) if store else None
    if not detail:
        return _json_result(False, error='Plugin detail not found', code=404)
    # 目标周期名义价：配置价优先，未配置走 L1 规则
    price_fen = _resolve_price_fen(detail, 'sub', new_interval)

    quote = sm.quote_change(sub, new_interval, price_fen)
    return _json_result(True, data=quote)


# ── 31c. 执行周期变更（升级/降级） ───────────────────────

@bp.route('/subscriptions/<plugin_id>/change', methods=['POST'])
def subscription_change(plugin_id: str):
    """执行周期变更（升级/降级，带按比例折算抵扣）。

    请求体: {interval, amount_fen?, proration_fen?}
      - amount_fen: 目标周期名义价（缺省按配置/L1 规则解析）
      - proration_fen: 剩余价值折算抵扣（由 /change/quote 的 credit_fen 产出）
    说明：升级应付差额（pay_fen）通过既有支付下单链路收取后回调确认；
          本端点负责最终落库变更（幂等，重复调用以新周期为准）。
    """
    err = _require_admin()
    if err:
        return err

    try:
        body = request.json if request.is_json else {}
    except Exception:
        body = {}
    new_interval = (body.get('interval') or '').strip()
    if new_interval not in ('month', 'quarter', 'year'):
        return _json_result(False, error='interval must be month/quarter/year', code=400)

    try:
        proration_fen = int(body.get('proration_fen', 0) or 0)
    except (TypeError, ValueError):
        return _json_result(False, error='proration_fen must be int (fen)', code=400)

    sm = get_subscription_manager()
    sub = sm.get_subscription(plugin_id)
    if not sub:
        return _json_result(False, error='Subscription not found', code=404)

    amount_fen = body.get('amount_fen')
    if amount_fen is None:
        mgr = _get_manager()
        store = mgr.store_client if mgr else None
        detail = store.get_detail(plugin_id) if store else None
        if not detail:
            return _json_result(False, error='Plugin detail not found', code=404)
        amount_fen = _resolve_price_fen(detail, 'sub', new_interval)
    try:
        amount_fen = int(amount_fen)
    except (TypeError, ValueError):
        return _json_result(False, error='amount_fen must be int (fen)', code=400)
    if amount_fen <= 0:
        return _json_result(False, error='Invalid amount_fen', code=400)

    updated = sm.change(plugin_id, new_interval, amount_fen,
                        proration_fen=proration_fen)
    if not updated:
        return _json_result(False, error='Change failed: subscription not active or not found', code=400)
    return _json_result(True, data=updated.to_dict())


# ── 31d. 版本包平滑升级报价 ──────────────────────────────

@bp.route('/subscriptions/bundle/<bundle_id>/upgrade/quote', methods=['POST'])
def bundle_upgrade_quote(bundle_id: str):
    """版本包平滑升级报价（L5）：包价 − 已订插件剩余价值折算 = 应付差额。

    响应: data = quote_bundle_upgrade 结果（bundle / credit_fen /
          upgrade_cost_fen / carryover_fen / needs_payment / items）
    """
    err = _require_admin()
    if err:
        return err

    from .pricing import get_pricing_rules, quote_bundle_upgrade
    sm = get_subscription_manager()
    rules = get_pricing_rules()
    if bundle_id not in (rules.get('bundles') or {}):
        return _json_result(False, error=f'Unknown bundle: {bundle_id}', code=404)

    mgr = _get_manager()
    store = mgr.store_client if mgr else None
    member_prices = {}
    for pid in sm.get_bundle_member_ids(bundle_id):
        detail = store.get_detail(pid) if store else None
        if detail:
            member_prices[pid] = detail.get('price_amount', 0)

    # 当前活跃独立订阅（排除包自身）作为折算来源
    active_subs = [
        s.to_dict() for s in sm.list_subscriptions()
        if s.status == SubscriptionStatus.ACTIVE and s.plugin_id != bundle_id
    ]

    try:
        quote = quote_bundle_upgrade(bundle_id, member_prices, active_subs, rules=rules)
    except Exception as e:
        return _json_result(False, error=f'bundle upgrade quote failed: {e}', code=400)
    return _json_result(True, data=quote)


# ── 32. 插件菜单列表 ─────────────────────────────────────

@bp.route('/menus', methods=['GET'])
def plugin_menus():
    """返回所有已安装+已启用插件的菜单项"""
    err = _require_admin()
    if err:
        return err

    mgr = _get_manager()
    if not mgr:
        return _json_result(False, error='PluginManager not initialized', code=503)

    menus = mgr.get_plugin_menus()
    return _json_result(True, data={'menus': menus})


# ====================================================================
# 评价 API
# ====================================================================

# ── 33. 获取评价列表 ─────────────────────────────────────

@bp.route('/store/<identifier>/reviews', methods=['GET'])
def store_reviews_list(identifier: str):
    """获取插件评价列表（分页，支持排序）"""
    err = _require_admin()
    if err:
        return err

    try:
        page = _parse_positive_int('page', 1)
        page_size = _parse_positive_int('page_size', 20, 1, 100)
    except ValueError as e:
        return _json_result(False, error=str(e), code=400)
    sort = request.args.get('sort', 'newest')  # newest / highest / lowest

    # 注意：SQL 无表别名，ORDER BY 不得带 r. 前缀（D-REVIEWS-500）
    sort_map = {
        'newest': 'created_at DESC',
        'highest': 'rating DESC',
        'lowest': 'rating ASC',
    }
    _ALLOWED_ORDER = ('created_at DESC', 'rating DESC', 'rating ASC')
    order_by = sort_map.get(sort, 'created_at DESC')
    if order_by not in _ALLOWED_ORDER:
        order_by = 'created_at DESC'

    with get_registry_db() as conn:
        total = conn.execute(
            'SELECT COUNT(*) as cnt FROM plugin_reviews WHERE plugin_identifier=%s AND is_active=1',
            (identifier,)
        ).fetchone()['cnt']

        offset = (page - 1) * page_size
        rows = conn.execute(
            f'SELECT * FROM plugin_reviews WHERE plugin_identifier=%s AND is_active=1 ORDER BY {order_by} LIMIT %s OFFSET %s',
            (identifier, page_size, offset)
        ).fetchall()

        reviews = [dict(r) for r in rows]

        return _json_result(True, data={
            'reviews': reviews,
            'total': total,
            'page': page,
            'page_size': page_size,
            'sort': sort,
        })


# ── 34. 创建评价 ─────────────────────────────────────────

@bp.route('/store/<identifier>/reviews', methods=['POST'])
def store_review_create(identifier: str):
    """创建评价（免费插件登录即可评价；付费插件需购买）"""
    if not request.is_json:
        return _json_result(False, error='请求体必须是 JSON', code=400)

    data = request.json
    try:
        rating = int(data.get('rating', 0))
    except (TypeError, ValueError):
        return _json_result(False, error='评分必须为 1-5 的整数', code=400)
    content = (data.get('content') or '').strip()

    if rating < 1 or rating > 5:
        return _json_result(False, error='评分必须在 1-5 之间', code=400)

    # 从 JWT 获取用户信息（管理员/用户 token 双通道）
    user_id, user_name, _is_admin = _get_jwt_user()
    if not user_id:
        return _json_result(False, error='未登录', code=401)

    # 评价资格：免费插件登录即可评价；付费插件需有效 License（D-REVIEW-FREE）
    mgr = _get_manager()
    if mgr and mgr.license_manager:
        price_type = 'free'
        try:
            if mgr.store_client:
                _detail = mgr.store_client.get_detail(identifier)
                price_type = (_detail or {}).get('price_type', 'free')
        except Exception:
            price_type = 'free'
        if price_type != 'free':
            lic = mgr.license_manager.get_license(identifier)
            if not lic or lic.get('license_status') not in ('active', 'grace'):
                return _json_result(False, error='请先购买插件后再评价', code=403)

    with get_registry_db() as conn:
        # 检查是否已评价过
        existing = conn.execute(
            'SELECT id FROM plugin_reviews WHERE plugin_identifier=%s AND user_id=%s',
            (identifier, user_id)
        ).fetchone()
        if existing:
            # 更新已有评价
            conn.execute(
                'UPDATE plugin_reviews SET rating=%s, content=%s, is_active=1 WHERE id=%s',
                (rating, content, existing['id'])
            )
            conn.commit()
            return _json_result(True, data={'id': existing['id'], 'updated': True})

        cur = conn.execute(
            'INSERT INTO plugin_reviews (plugin_identifier, user_id, user_name, rating, content) VALUES (%s,%s,%s,%s,%s) RETURNING id',
            (identifier, user_id, user_name, rating, content)
        )
        conn.commit()
        review_id = cur.fetchone()['id']

        # 更新 store_plugins 的评分聚合
        agg = conn.execute(
            'SELECT COUNT(*) as cnt, AVG(rating) as avg FROM plugin_reviews WHERE plugin_identifier=%s AND is_active=1',
            (identifier,)
        ).fetchone()
        conn.execute(
            'UPDATE store_plugins SET rating=%s, review_count=%s WHERE identifier=%s',
            (round(agg['avg'], 1) if agg['avg'] else 0.0, agg['cnt'], identifier)
        )
        conn.commit()

    return _json_result(True, data={'id': review_id, 'created': True})


# ── 35. 删除自己的评价 ───────────────────────────────────

@bp.route('/store/<identifier>/reviews/<int:review_id>', methods=['DELETE'])
def store_review_delete(identifier: str, review_id: int):
    """删除评价（仅自己或管理员）"""
    user_id, _user_name, is_admin = _get_jwt_user()
    if not user_id and not is_admin:
        return _json_result(False, error='未登录', code=401)

    with get_registry_db() as conn:
        review = conn.execute('SELECT * FROM plugin_reviews WHERE id=%s', (review_id,)).fetchone()
        if not review:
            return _json_result(False, error='评价不存在', code=404)
        if review['user_id'] != user_id and not is_admin:
            return _json_result(False, error='无权删除此评价', code=403)

        conn.execute('UPDATE plugin_reviews SET is_active=0 WHERE id=%s', (review_id,))
        # 重新计算评分
        agg = conn.execute(
            'SELECT COUNT(*) as cnt, AVG(rating) as avg FROM plugin_reviews WHERE plugin_identifier=%s AND is_active=1',
            (identifier,)
        ).fetchone()
        conn.execute(
            'UPDATE store_plugins SET rating=%s, review_count=%s WHERE identifier=%s',
            (round(agg['avg'], 1) if agg['avg'] else 0.0, agg['cnt'], identifier)
        )
        conn.commit()

    return _json_result(True, data={'deleted': True})


# ── 36. 管理员回复评价 ───────────────────────────────────

@bp.route('/store/<identifier>/reviews/<int:review_id>/reply', methods=['POST'])
def store_review_reply(identifier: str, review_id: int):
    """管理员回复评价"""
    err = _require_admin()
    if err:
        return err

    if not request.is_json:
        return _json_result(False, error='请求体必须是 JSON', code=400)

    reply_content = request.json.get('content', '').strip()
    if not reply_content:
        return _json_result(False, error='回复内容不能为空', code=400)

    with get_registry_db() as conn:
        review = conn.execute('SELECT * FROM plugin_reviews WHERE id=%s', (review_id,)).fetchone()
        if not review:
            return _json_result(False, error='评价不存在', code=404)

        conn.execute(
            'UPDATE plugin_reviews SET reply_content=%s, reply_at=NOW() WHERE id=%s',
            (reply_content, review_id)
        )
        conn.commit()

    return _json_result(True, data={'replied': True})


# ── 工具: 触发支付相关钩子 ──────────────────────────────

def _fire_payment_hook(plugin_id: str, event: str, order_no: str):
    try:
        mgr = _get_manager()
        if mgr and mgr._hook:
            mgr._hook.do_action(f'plugin/{event}', {
                'plugin_id': plugin_id,
                'order_no': order_no,
            })
    except Exception as e:
        print(f'[Payment] Hook error: {e}')


def _auto_install_enable_plugin(mgr, identifier: str):
    """支付成功后自动安装 + 启用插件（订阅闭环）。

    License 激活成功后调用，使插件进入 ENABLED 状态——
    PluginManager.get_plugin_menus() 仅收集 ENABLED/ACTIVE 插件，
    启用后插件菜单即自动注册到管理后台侧边栏。

    幂等：已启用直接跳过；失败只记日志，不阻断支付回调（License 已激活）。
    """
    try:
        # 已启用 → 跳过
        if mgr.is_enabled(identifier):
            print(f'[Payment] {identifier} already enabled, skip auto-enable')
            return

        # 未安装 → 从商店下载到 plugins/<id>/
        if mgr.get_info(identifier) is None:
            store = mgr.store_client
            if not store:
                print(f'[Payment] Store client unavailable, skip download for {identifier}')
                return
            detail = store.get_detail(identifier)
            if not detail:
                print(f'[Payment] Plugin "{identifier}" not found in store, skip')
                return
            app_version = getattr(mgr.app, 'version', '')
            download_url, fallback_url = store.get_download_urls(
                identifier, app_version)
            if not download_url:
                print(f'[Payment] No download URL for "{identifier}", skip')
                return
            from .downloader import download_plugin
            plugin_dest = os.path.join(mgr.plugins_dir, identifier)
            download_plugin(download_url, plugin_dest,
                            expected_hash=detail.get('package_hash', ''),
                            fallback_url=fallback_url or '')
            mgr.install(identifier)

        # 启用（License 已在支付回调中激活）
        mgr.enable(identifier)
        print(f'[Payment] ✅ {identifier} auto-enabled after payment')
    except Exception as e:
        traceback.print_exc()
        print(f'[Payment] Auto install/enable failed for {identifier}: {e}')


# ====================================================================
# ★ P0-B：开发者中心（单账号双身份 · 统一 JWT SSO）
# ====================================================================
# 开发者 = 普通用户的身份升级。端点全部挂 /admin/plugins/developer/*，
# 鉴权走 _get_jwt_user() 双通道（用户 token 即可），不做管理员上下文。
# 表：store_developers（见 models_store.py）。

def _now_iso() -> str:
    """当前 UTC ISO 时间串（与 store_developers.verify_expires 同格式比较）。"""
    from datetime import timezone
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _ts_add_hours(hours: int) -> str:
    """hours 小时后的 UTC ISO 时间串。"""
    from datetime import timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _get_user_email(user_id) -> str:
    """按统一账号 user_id 查询主库 email（与 auth-center 同一 PG 公共 schema）。"""
    if not user_id:
        return ''
    try:
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT email FROM public.users WHERE id=%s', (user_id,)).fetchone()
        return (row['email'] or '') if row else ''
    except Exception:
        traceback.print_exc()
        return ''


def _send_dev_verification_email(email: str, token: str) -> bool:
    """复用 email 插件发送通道发开发者邮箱验证链接；失败不阻断注册（日志 stub）。"""
    if not email:
        return False
    try:
        from plugins.email.services import send_email
        # 审计 P1-3 修复：验证链接 origin 用可配置主站地址（PUBLIC_BASE_URL / NOTIFY_BASE），
        # 跨服务部署（plugin_manager 与 main_site 不同 origin）时不再依赖请求 Host。
        origin = (os.environ.get('PUBLIC_BASE_URL')
                  or os.environ.get('NOTIFY_BASE')
                  or request.host_url).rstrip('/')
        link = f'{origin}/user/profile?dev_verify={token}'
        subject = 'VeroRun Developer Email Verification'
        body_text = (
            'Welcome to VeroRun Developer Center.\n\n'
            f'Click the link below to verify your developer email:\n{link}\n\n'
            'The link is valid for 24 hours. If this was not you, please ignore.'
        )
        body_html = (
            '<h3>VeroRun Developer Email Verification</h3>'
            f'<p>Click the link to verify your developer email:</p>'
            f'<p><a href="{link}">{link}</a></p>'
            '<p style="color:#888">The link is valid for 24 hours. If this was not you, please ignore.</p>'
        )
        success, msg = send_email(email, subject, body_text, body_html)
        if not success:
            print(f'[PluginManager] dev verify email send failed: {msg} (stub token: {token})')
        return bool(success)
    except Exception as e:
        traceback.print_exc()
        print(f'[PluginManager] dev verify email exception (stub token: {token}): {e}')
        return False


def _require_developer():
    """要求当前 JWT 用户已注册为开发者且状态 active。

    Returns:
        (dev_dict, None) 通过；否则 (None, (jsonify, code)) 供视图 return。
    """
    user_id, _name, _admin = _get_jwt_user()
    if not user_id:
        return None, _json_result(False, error='Not logged in', code=401)
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                'SELECT * FROM store_developers WHERE user_id=%s', (user_id,))
            row = cur.fetchone()
    except Exception:
        traceback.print_exc()
        return None, _json_result(False, error='DB error', code=500)
    if not row:
        return None, _json_result(False, error='Not a registered developer', code=403)
    if row['status'] != 'active':
        return None, _json_result(False, error='Developer account suspended', code=403)
    return dict(row), None


@bp.route('/developer/register', methods=['POST'])
def developer_register():
    """开发者注册：绑定当前 JWT 用户，幂等（已注册则返回既有记录）。

    P0-3：注册默认 verify_level='free'，随后触发邮箱验证流程；
    验证通过后由 /developer/verify-email 升为 'email'。
    """
    user_id, _user_name, _is_admin = _get_jwt_user()
    if not user_id:
        return _json_result(False, error='Not logged in', code=401)
    # 审计 P2-3 修复：注册接口补限流（复用 _upload_rate_limited DB 共享限流）
    if _upload_rate_limited(request.headers.get('Authorization', '')):
        return _json_result(False, error='Too many requests. Please wait.', code=429)
    data = request.get_json(silent=True) or {}
    display_name = (data.get('display_name') or '').strip()
    slug = (data.get('slug') or '').strip().lower()
    if not display_name or not slug:
        return _json_result(False, error='display_name and slug are required', code=400)
    import re as _re
    if not _re.match(r'^[a-z0-9_-]{2,32}$', slug):
        return _json_result(False, error='slug: 2-32 chars of [a-z0-9_-]', code=400)
    # 审计 P1-3 修复：邮箱一律以主库 users.email 为准，拒绝请求体兜底；
    # 账号无邮箱时要求先补全，保证"验证了哪个邮箱"可审计。
    _email = _get_user_email(user_id)
    if not _email:
        return _json_result(False,
                            error='Your account has no email on file. Please set it in your profile first.',
                            code=400)
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                'SELECT * FROM store_developers WHERE user_id=%s', (user_id,))
            row = cur.fetchone()
            if row:
                return _json_result(True, data=dict(row))
            cur = conn.execute(
                'SELECT 1 FROM store_developers WHERE slug=%s', (slug,))
            if cur.fetchone():
                return _json_result(False, error=f'slug "{slug}" already taken', code=409)
            # 生成一次性验证令牌（哈希存储，复用 stdlib secrets + hashlib）
            import secrets, hashlib
            _token = secrets.token_urlsafe(32)
            _hash = hashlib.sha256(_token.encode()).hexdigest()
            _expires = _ts_add_hours(24)
            cur = conn.execute(
                "INSERT INTO store_developers "
                " (user_id, display_name, slug, email, verify_token, verify_expires) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (user_id, display_name, slug, _email, _hash, _expires))
            _dev_id = cur.fetchone()['id']
            conn.commit()
        # 发送验证邮件（复用 email 插件通道；失败不阻断注册）
        _sent = _send_dev_verification_email(_email, _token)
        return _json_result(True, data={'id': _dev_id, 'user_id': user_id,
                                        'display_name': display_name, 'slug': slug,
                                        'verify_level': 'free',
                                        'email_verified': 0,
                                        'email_sent': _sent})
    except Exception as e:
        traceback.print_exc()
        # 审计 P2-10 修复：并发下唯一约束冲突应返回 409 而非 500
        if ('23505' in str(getattr(getattr(e, 'diag', None), 'sqlstate', '') or '')
                or 'unique constraint' in str(e).lower()
                or 'duplicate key' in str(e).lower()):
            return _json_result(False, error=f'slug "{slug}" already taken', code=409)
        return _json_result(False, error=f'Register failed: {e}', code=500)


@bp.route('/developer/verify-email', methods=['POST'])
def developer_verify_email():
    """P0-3：校验邮箱验证令牌，通过后 verify_level='email'、email_verified=1。

    令牌单次有效：校验成功后清空 verify_token/verify_expires。
    """
    user_id, _name, _admin = _get_jwt_user()
    if not user_id:
        return _json_result(False, error='Not logged in', code=401)
    # 审计 P2-3 修复：邮箱验证接口补限流（防令牌爆破）
    if _upload_rate_limited(request.headers.get('Authorization', '')):
        return _json_result(False, error='Too many requests. Please wait.', code=429)
    data = request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip()
    if not token:
        return _json_result(False, error='token required', code=400)
    import hashlib
    _hash = hashlib.sha256(token.encode()).hexdigest()
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                'SELECT id, verify_level, verify_expires FROM store_developers '
                'WHERE user_id=%s AND verify_token=%s', (user_id, _hash))
            row = cur.fetchone()
            if not row:
                return _json_result(False, error='invalid or expired token', code=400)
            if row['verify_expires'] and row['verify_expires'] < _now_iso():
                return _json_result(False, error='verification link expired', code=400)
            conn.execute(
                "UPDATE store_developers SET verify_level='email', email_verified=1, "
                "verify_token='', verify_expires='', updated_at=NOW() WHERE id=%s",
                (row['id'],))
            conn.commit()
        return _json_result(True, data={'verify_level': 'email', 'email_verified': 1})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Verify failed: {e}', code=500)


@bp.route('/developer/submissions', methods=['GET'])
def developer_submissions():
    """开发者查询自己的提交记录与审核报告（修复提交者不可见问题）。

    注意：plugin_submissions.submitter_id 存 JWT user_id 字符串，
    全链路统一 str(user_id)，防止类型不一致。
    """
    dev, err = _require_developer()
    if err:
        return err
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                "SELECT id, identifier, name, version, status, audit_status, "
                "       audit_report, audit_reasons, review_comment, created_at "
                "FROM plugin_submissions WHERE submitter_id=%s "
                "ORDER BY id DESC LIMIT 100",
                (str(dev['user_id']),))
            rows = [_submission_row_to_dict(r) for r in cur.fetchall()]
        return _json_result(True, data=rows)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


def _publish_approved(submission_id, local_only=False):
    """P0-5 核心：把已审核通过的提交物打包进 catalog 并触发全站同步。

    供 publish_third_party 端点与 approve(auto_publish) 复用，不重复造轮子。
    前提：调用方已确保 submission 状态为 approved（或在本函数内更新）。

    Returns:
        (jsonify, http_code)：成功 http_code=200。
    """
    try:
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT * FROM plugin_submissions WHERE id=%s', (submission_id,)).fetchone()
        if not row:
            return _json_result(False, error='Submission not found', code=404)
        if row['status'] != 'approved':
            return _json_result(False,
                                error=f'submission must be approved first (current: {row["status"]})',
                                code=400)
        if row['audit_status'] not in ('pass', 'manual'):
            return _json_result(False,
                                error=f'audit must be pass/manual (current: {row["audit_status"]})',
                                code=400)
        pending_dir = row['file_path'] or ''
        if not os.path.isdir(pending_dir):
            return _json_result(False, error='submission package dir not found', code=404)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)

    # 调用发布工具打包进 catalog（平台代发）
    import sys as _sys
    _tool = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'tools', 'publish_plugin.py'))
    _cmd = [_sys.executable, _tool, '--third-party', str(submission_id)]
    if local_only:
        _cmd.append('--local-only')
    try:
        _res = subprocess.run(_cmd, capture_output=True, text=True, timeout=180)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'publish tool failed: {e}', code=500)
    if _res.returncode != 0:
        # 完整输出仅落服务器日志，错误响应回通用文案，防内部路径/DB 信息泄露
        print(f'[PluginManager] publish_third_party tool failed '
              f'(submission_id={submission_id}, local_only={local_only}):\n'
              f'--- stdout ---\n{_res.stdout}\n--- stderr ---\n{_res.stderr}')
        return _json_result(False,
                            error='publish tool failed. See server logs for details.',
                            code=500)

    # 触发目录同步
    _n = 0
    try:
        mgr = _get_manager()
        store = getattr(mgr, 'store_client', None)
        if store:
            _n = store.sync_all()
    except Exception:
        traceback.print_exc()
        return _json_result(False,
                            error='Plugin published but catalog sync failed. Please retry sync-all.',
                            code=500)
    if _n <= 0:
        return _json_result(False,
                            error=f'Plugin published but catalog sync returned {_n}. Please retry sync-all.',
                            code=500)
    return _json_result(True, data={'submission_id': submission_id,
                                    'synced': _n,
                                    'local_only': local_only})


@bp.route('/store/admin/publish_third_party', methods=['POST'])
def publish_third_party():
    """P0-5：平台代发第三方插件（人工 approve 后触发）。

    校验插件包通过全部门禁（approved + audit pass/manual）→ 调用
    publish_plugin.py --third-party <submission_id> 把审核通过的提交物打包并
    追加进 catalog（第三方无 GitHub 写权限，由平台代发）→ 推送触发 sync 全站同步。
    --local-only 兜底：直写 store_plugins（is_official=0、catalog_managed=0），
    sync 跳过覆盖防清除（默认不启用）。
    """
    err = _require_store_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    submission_id = data.get('submission_id')
    local_only = bool(data.get('local_only'))
    if not submission_id:
        return _json_result(False, error='submission_id required', code=400)
    # 原发布逻辑已抽取为 _publish_approved，端点保持对外行为不变
    return _publish_approved(submission_id, local_only)


# ====================================================================
# ★ P1 版本管理系统（store_plugin_versions）+ 开发者后台
# ====================================================================

_SEMVER_RE_P1 = None  # 延迟初始化（避免重复编译）


def _p1_semver_re():
    global _SEMVER_RE_P1
    if _SEMVER_RE_P1 is None:
        import re as _re
        _SEMVER_RE_P1 = _re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$')
    return _SEMVER_RE_P1


def _require_plugin_owner(identifier: str, dev: dict):
    """P0-4 归属校验：第三方只能操作自己归属的插件；官方插件走官方通道。

    Returns:
        (row_dict, None) 通过；否则 (None, (jsonify, code)) 供视图 return。
    """
    is_admin = _get_jwt_user()[2]
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                'SELECT * FROM store_plugins WHERE identifier=%s', (identifier,))
            row = cur.fetchone()
    except Exception as e:
        traceback.print_exc()
        return None, _json_result(False, error=f'Failed: {e}', code=500)
    if not row:
        return None, _json_result(False, error=f'plugin "{identifier}" not found', code=404)
    if row['is_official']:
        return None, _json_result(False,
                                  error='official plugins are managed via official release pipeline',
                                  code=403)
    if not is_admin and int(row['developer_id'] or 0) != int(dev['id']):
        return None, _json_result(False, error='not owner of this plugin', code=403)
    return dict(row), None


def _version_package_dir() -> str:
    """审计 B2 修复：本地版本包存储目录（plugins/.versions，沿用 .pending 惯例）。"""
    _base = os.environ.get('PLUGIN_VERSIONS_DIR') or os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'plugins', '.versions'))
    os.makedirs(_base, exist_ok=True)
    return _base


@bp.route('/developer/plugins/<identifier>/versions', methods=['POST'])
def developer_submit_version(identifier):
    """P1：开发者提交新版本（真包上传，服务端计算 file_size/package_hash）。

    P0-4：is_owner 归属校验 —— 第三方只能给自己归属的插件提交版本；
    官方插件由 is_admin 走官方发布通道。
    审计 B2（方案 A）修复：改为 multipart 接收真实插件 zip，服务端校验
    zip 内 plugin.json.version 与提交版本一致（版本流 P1-5 守卫），
    计算 file_size + SHA256 并落盘 plugins/.versions/，不再信任请求体的
    file_size / package_hash / download_url。
    """
    dev, err = _require_developer()
    if err:
        return err
    # 审计 P2-3 修复：版本提交接口补限流
    if _upload_rate_limited(request.headers.get('Authorization', '')):
        return _json_result(False, error='Too many requests. Please wait.', code=429)

    version = (request.form.get('version') or '').strip()
    changelog = (request.form.get('changelog') or '').strip()
    if not version or not changelog:
        return _json_result(False, error='version and changelog are required', code=400)
    # P1-5：对齐标准 §13.1，允许 X.Y.Z-prerelease 后缀
    if not _p1_semver_re().match(version):
        return _json_result(False, error='version must be semver (X.Y.Z or X.Y.Z-prerelease)', code=400)

    _row, err = _require_plugin_owner(identifier, dev)
    if err:
        return err

    _f = request.files.get('file')
    if not _f or not getattr(_f, 'filename', ''):
        return _json_result(False, error='plugin package file is required', code=400)
    _raw = _f.read()
    if not _raw:
        return _json_result(False, error='empty file', code=400)
    if len(_raw) > 50 * 1024 * 1024:
        return _json_result(False, error='file too large (max 50MB)', code=400)

    # 审计 B2 修复：校验 zip 内 plugin.json.version == 提交版本
    import io as _io
    import zipfile as _zf
    import hashlib as _hl
    try:
        with _zf.ZipFile(_io.BytesIO(_raw)) as _z:
            _names = _z.namelist()
            _meta = next(
                (n for n in _names if n.replace('\\', '/').endswith('plugin.json')), None)
            if not _meta:
                return _json_result(False, error='plugin.json not found in package', code=400)
            _pkg = json.loads(_z.read(_meta).decode('utf-8', 'replace'))
    except Exception as e:
        return _json_result(False, error=f'invalid package: {e}', code=400)
    _pkg_ver = str((_pkg or {}).get('version') or '').strip()
    if _pkg_ver != version:
        return _json_result(False,
                            error=f'package plugin.json.version({_pkg_ver}) does not match '
                                  f'submitted version({version})',
                            code=400)

    _size = len(_raw)
    _sha = _hl.sha256(_raw).hexdigest()

    # 落盘 plugins/.versions/{identifier}/{version}.zip
    try:
        _ver_dir = os.path.join(_version_package_dir(), identifier)
        os.makedirs(_ver_dir, exist_ok=True)
        _fp = os.path.join(_ver_dir, f'{version}.zip')
        with open(_fp, 'wb') as _w:
            _w.write(_raw)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed to store package: {e}', code=500)

    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                "INSERT INTO store_plugin_versions "
                "(plugin_id, developer_id, version, changelog, package_hash, "
                " file_size, download_url, file_path) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(plugin_id, version) DO NOTHING RETURNING id",
                (identifier, dev['id'], version, changelog, _sha,
                 _size, '', _fp))
            _vid = cur.fetchone()
            conn.commit()
        if not _vid:
            return _json_result(False, error=f'version {version} already exists', code=409)
        return _json_result(True, data={'version_id': _vid['id'], 'status': 'pending'})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


def _sync_live_version(conn, plugin_id: str, vid: int) -> None:
    """回写 store_plugins 主版本字段为指定 live 版本（P1 §5.3）。"""
    cur = conn.execute(
        'SELECT version, download_url, package_hash, file_size FROM store_plugin_versions WHERE id=%s',
        (vid,))
    row = cur.fetchone()
    if not row:
        return
    # 审计 B2 修复：download_url 指向本地版本包端点，file_size 一并回写（不再恒 0）
    _dl = row['download_url'] or ''
    if not _dl:
        _origin = (os.environ.get('PUBLIC_BASE_URL')
                   or os.environ.get('NOTIFY_BASE')
                   or request.host_url).rstrip('/')
        _dl = f'{_origin}/admin/plugins/store/version-package/{vid}'
    conn.execute(
        "UPDATE store_plugins SET version=%s, download_url=%s, package_hash=%s, "
        "file_size=%s, updated_at=NOW() WHERE identifier=%s",
        (row['version'], _dl, row['package_hash'] or '', row['file_size'] or 0, plugin_id))


@bp.route('/store/version-package/<int:vid>', methods=['GET'])
def store_version_package(vid):
    """审计 B2 修复：公开下载版本包（仅 approved/live 版本，路径以 DB 为准防穿越）。"""
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                "SELECT file_path, plugin_id, version, status FROM store_plugin_versions WHERE id=%s",
                (vid,))
            row = cur.fetchone()
        if not row or row['status'] not in ('approved', 'live'):
            return _json_result(False, error='not found', code=404)
        if not row['file_path'] or not os.path.isfile(row['file_path']):
            return _json_result(False, error='package missing', code=404)
        from flask import send_file
        return send_file(row['file_path'], as_attachment=True,
                         download_name=f"{row['plugin_id']}-{row['version']}.zip")
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/store/admin/versions/<int:vid>/approve', methods=['POST'])
def approve_plugin_version(vid):
    """P1：管理员审核通过版本 → 置 live，并回写 store_plugins 主版本字段。"""
    err = _require_store_admin()
    if err:
        return err
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                'SELECT * FROM store_plugin_versions WHERE id=%s', (vid,))
            row = cur.fetchone()
            if not row:
                return _json_result(False, error='version not found', code=404)
            if row['status'] == 'live':
                return _json_result(False, error='version already live', code=400)
            # 同一插件先下架其它 live 版本，保证任意时刻至多一个 live
            conn.execute(
                "UPDATE store_plugin_versions SET status='retired', updated_at=NOW() "
                "WHERE plugin_id=%s AND status='live'", (row['plugin_id'],))
            conn.execute(
                "UPDATE store_plugin_versions SET status='live', updated_at=NOW() WHERE id=%s",
                (vid,))
            _sync_live_version(conn, row['plugin_id'], vid)
            conn.commit()
        return _json_result(True, data={'version_id': vid, 'status': 'live'})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/store/admin/versions/<int:vid>/reject', methods=['POST'])
def reject_plugin_version(vid):
    """P1：管理员驳回版本 → 置 rejected。"""
    err = _require_store_admin()
    if err:
        return err
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                'SELECT id FROM store_plugin_versions WHERE id=%s', (vid,))
            if not cur.fetchone():
                return _json_result(False, error='version not found', code=404)
            conn.execute(
                "UPDATE store_plugin_versions SET status='rejected', updated_at=NOW() WHERE id=%s",
                (vid,))
            conn.commit()
        return _json_result(True, data={'version_id': vid, 'status': 'rejected'})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/store/admin/plugins/<identifier>/rollback', methods=['POST'])
def rollback_plugin_version(identifier):
    """P1：管理员回滚到历史版本（§5.3）——目标版本置 live，原 live 置 retired，回写主表。"""
    err = _require_store_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    version = (data.get('version') or '').strip()
    if not version:
        return _json_result(False, error='version required', code=400)
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                'SELECT id FROM store_plugin_versions WHERE plugin_id=%s AND version=%s',
                (identifier, version))
            row = cur.fetchone()
            if not row:
                return _json_result(False, error=f'version {version} not found', code=404)
            # 原 live 版本下架
            conn.execute(
                "UPDATE store_plugin_versions SET status='retired', updated_at=NOW() "
                "WHERE plugin_id=%s AND status='live'", (identifier,))
            # 目标版本置 live 并回写主表
            conn.execute(
                "UPDATE store_plugin_versions SET status='live', updated_at=NOW() WHERE id=%s",
                (row['id'],))
            _sync_live_version(conn, identifier, row['id'])
            conn.commit()
        return _json_result(True, data={'plugin_id': identifier,
                                        'version': version, 'status': 'live'})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/developer/overview', methods=['GET'])
def developer_overview():
    """P1：开发者后台统计（插件数 / 版本数 / 待审提交 / 待审版本 / 待结算金额）。"""
    dev, err = _require_developer()
    if err:
        return err
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                'SELECT COUNT(*) AS n FROM store_plugins WHERE developer_id=%s', (dev['id'],))
            plugin_count = cur.fetchone()['n'] or 0
            cur = conn.execute(
                'SELECT COUNT(*) AS n FROM store_plugin_versions WHERE developer_id=%s', (dev['id'],))
            version_count = cur.fetchone()['n'] or 0
            cur = conn.execute(
                'SELECT COUNT(*) AS n FROM store_plugin_versions '
                "WHERE developer_id=%s AND status='pending'", (dev['id'],))
            pending_versions = cur.fetchone()['n'] or 0
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM plugin_submissions "
                "WHERE submitter_id=%s AND status='pending'", (str(dev['user_id']),))
            pending_submissions = cur.fetchone()['n'] or 0
            # 审计 P2-1 修复：待结算金额 = 上一自然月已 paid 流水 × 分成比例（fen）。
            # 注意：此为「预估待结算」，实时聚合支付流水，未经 generate_payouts
            # 结算流程；正式金额以 /developer/payouts（developer_payouts 表）
            # 生成的结算记录为准，两者口径不同、可能暂不一致。
            _period = _p2_validate_period('')
            _gross_map = _p2_aggregate(conn, _period)
            _g = _gross_map.get(dev['id'], 0)
            _pending_amount = int(_g * int(dev.get('payout_ratio') or 80) / 100) if _g else 0
        return _json_result(True, data={
            'plugin_count': plugin_count,
            'version_count': version_count,
            'pending_versions': pending_versions,
            'pending_submissions': pending_submissions,
            'pending_amount': _pending_amount,
            'period': _period,
        })
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/developer/plugins', methods=['GET'])
def developer_plugins():
    """P1：我的插件列表（store_plugins 归属当前开发者）。"""
    dev, err = _require_developer()
    if err:
        return err
    try:
        with get_registry_db() as conn:
            rows = conn.execute(
                'SELECT * FROM store_plugins WHERE developer_id=%s '
                'ORDER BY id DESC', (dev['id'],)).fetchall()
        data = [dict(r) for r in rows]
        return _json_result(True, data=data)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/developer/plugins/<identifier>/versions', methods=['GET'])
def developer_plugin_versions(identifier):
    """P1：我的插件版本历史（is_owner 校验）。"""
    dev, err = _require_developer()
    if err:
        return err
    _row, err = _require_plugin_owner(identifier, dev)
    if err:
        return err
    try:
        with get_registry_db() as conn:
            rows = conn.execute(
                'SELECT id, version, changelog, package_hash, file_size, download_url, '
                'status, created_at, updated_at FROM store_plugin_versions '
                'WHERE plugin_id=%s ORDER BY version DESC', (identifier,)).fetchall()
        return _json_result(True, data=[dict(r) for r in rows])
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/developer/profile', methods=['PUT'])
def developer_profile():
    """P1：修改开发者资料（bio / website）。"""
    dev, err = _require_developer()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    bio = (data.get('bio') or '').strip()
    website = (data.get('website') or '').strip()
    try:
        with get_registry_db() as conn:
            conn.execute(
                'UPDATE store_developers SET bio=%s, website=%s, updated_at=NOW() WHERE id=%s',
                (bio, website, dev['id']))
            conn.commit()
        return _json_result(True, data={'bio': bio, 'website': website})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/developer/submit', methods=['POST'])
def developer_submit():
    """P3：开发者自助提审（multipart file=<plugin.zip>）。

    与 upload_plugin（本地安装通道，需 admin）不同：本通道仅入
    plugin_submissions 审核队列 + 自动 AI 审核，**不安装到本地**；
    由 _require_developer 鉴权（JWT 普通用户但已注册开发者）。
    审核通过后由商店管理员经 publish_plugin.py --third-party 代发上架。
    """
    dev, err = _require_developer()
    if err:
        return err
    import os as _os
    import zipfile as _zipfile
    import tempfile as _tempfile
    import shutil as _shutil
    import re as _re
    from .watermark import OFFICIAL_PLUGIN_IDS, detect_official_watermark, WM_HARD

    _token = request.headers.get('Authorization', '')
    if _upload_rate_limited(_token):
        return _json_result(False, error='Too many uploads. Please wait.', code=429)

    # 1. 检查文件
    if 'file' not in request.files:
        return _json_result(False, error='No file uploaded', code=400)
    file = request.files['file']
    if not file.filename or not file.filename.lower().endswith('.zip'):
        return _json_result(False, error='Only .zip files are accepted', code=400)
    file.seek(0, 2)
    _file_size = file.tell()
    file.seek(0)
    if _file_size > 50 * 1024 * 1024:
        return _json_result(False, error=f'File too large ({_file_size / 1024 / 1024:.1f}MB). Max 50MB.', code=400)
    _magic = file.read(4)
    file.seek(0)
    if _magic != b'PK\x03\x04':
        return _json_result(False, error='Invalid zip file (bad magic bytes)', code=400)

    tmp_path = None
    pending_dir = None
    try:
        # 2. 暂存 + 读取 plugin.json
        fd, tmp_path = _tempfile.mkstemp(suffix='.submit.zip')
        _os.close(fd)
        file.save(tmp_path)

        with _zipfile.ZipFile(tmp_path, 'r') as zf:
            json_entry = None
            for name in zf.namelist():
                cleaned = _os.path.normpath(name).replace('\\', '/')
                if cleaned.endswith('/plugin.json') or cleaned == 'plugin.json':
                    json_entry = name
                    break
            if json_entry is None:
                return _json_result(False, error='plugin.json not found in zip root', code=400)
            json_raw = zf.read(json_entry).decode('utf-8')

        plugin_meta = json.loads(json_raw)
        identifier = (plugin_meta.get('identifier') or '').strip().lower()
        name = (plugin_meta.get('name') or '').strip()
        version = (plugin_meta.get('version') or '').strip()
        if not identifier or not name or not version:
            return _json_result(False, error='plugin.json requires fields: identifier, name, version', code=400)
        # 审计 P1-1 修复：版本号 semver 校验（对齐标准 §13.1，允许 X.Y.Z-prerelease 后缀）
        if not _p1_semver_re().match(version):
            return _json_result(False, error=f'Invalid version: "{version}". Use semver X.Y.Z or X.Y.Z-prerelease.', code=400)
        if not _re.match(r'^[a-z0-9_]+$', identifier):
            return _json_result(False, error=f'Invalid identifier: "{identifier}". Use only lowercase letters, digits, underscores.', code=400)
        # P0-4：第三方不得占用官方标识
        if identifier in OFFICIAL_PLUGIN_IDS:
            return _json_result(False, error=f'identifier "{identifier}" is reserved for official plugins', code=403)
        # P1-3：必填元数据
        for _f in ('description', 'category'):
            if not str(plugin_meta.get(_f) or '').strip():
                return _json_result(False, error=f'missing required metadata: {_f}', code=400)
        # 审计 P1-1 修复（P1-3 定价申报）：付费插件必填 price_amount / price_interval
        _price_type = str(plugin_meta.get('price_type') or 'free').strip()
        if _price_type and _price_type != 'free':
            if plugin_meta.get('price_amount') in (None, ''):
                return _json_result(False, error='paid plugin requires price_amount', code=400)
            if not str(plugin_meta.get('price_interval') or '').strip():
                return _json_result(False, error='paid plugin requires price_interval', code=400)

        mgr = _get_manager()
        if not mgr:
            return _json_result(False, error='PluginManager not initialized', code=503)
        plugins_root = getattr(mgr, '_plugins_root',
                               _os.path.join(_os.path.dirname(__file__), '..', 'plugins'))
        plugins_root = _os.path.abspath(plugins_root)

        # 3. 安全解压到 .pending（开发者通道不安装，仅入队审核）
        from .downloader import _extract_archive
        pending_root = _os.path.join(plugins_root, '.pending')
        _os.makedirs(pending_root, exist_ok=True)
        pending_dir = _os.path.join(pending_root, f'{identifier}-{dev["id"]}')
        if _os.path.exists(pending_dir):
            _shutil.rmtree(pending_dir, ignore_errors=True)
        _extract_archive(tmp_path, pending_dir)

        # 解压护栏（防解压炸弹）
        _guard_total = 0
        _guard_count = 0
        for _gdp, _gdns, _gfns in _os.walk(pending_dir):
            for _gfn in _gfns:
                _guard_count += 1
                _guard_total += _os.path.getsize(_os.path.join(_gdp, _gfn))
                if _guard_count > 2000 or _guard_total > 200 * 1024 * 1024:
                    _shutil.rmtree(pending_dir, ignore_errors=True)
                    return _json_result(False, error='插件解压后体积或文件数超限，已拒绝', code=400)

        # 4. 官方水印检测（硬拒即回滚）
        _wm = detect_official_watermark(pending_dir)
        if _wm.get('official') and _wm.get('method') in WM_HARD:
            _shutil.rmtree(pending_dir, ignore_errors=True)
            return _json_result(False, error=(
                f'检测到官方插件二次打包（identifier={_wm.get("identifier") or "未知"}）。'
                '官方插件请从插件商店安装，禁止重新打包提交。'), code=400)

        # 5. 写入审核队列（含元数据 + developer_id）
        from .models import get_registry_db
        _sub_id = None
        try:
            with get_registry_db() as conn:
                _cur = conn.execute(
                    "INSERT INTO plugin_submissions "
                    "(identifier, name, version, status, submitter, submitter_id, "
                    " file_path, file_size, wm_method, wm_reason, developer_id, "
                    " description, category, tagline, screenshots, readme_url, "
                    " compatible_editions, min_app_version, agent_role, capabilities, "
                    " price_type, price_amount, price_interval) "
                    "VALUES (%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "RETURNING id",
                    (identifier, name, version,
                     str(dev.get('display_name') or dev.get('name') or ''),
                     str(dev['user_id']),
                     pending_dir, _file_size,
                     _wm.get('method', ''), _wm.get('reason', ''), dev['id'],
                     str(plugin_meta.get('description') or ''),
                     str(plugin_meta.get('category') or ''),
                     str(plugin_meta.get('tagline') or ''),
                     json.dumps(plugin_meta.get('screenshots') or [], ensure_ascii=False),
                     str(plugin_meta.get('readme_url') or ''),
                     json.dumps(plugin_meta.get('compatible_editions') or [], ensure_ascii=False),
                     str(plugin_meta.get('min_app_version') or ''),
                     str(plugin_meta.get('agent_role') or ''),
                     json.dumps(plugin_meta.get('capabilities') or [], ensure_ascii=False),
                     _price_type,
                     plugin_meta.get('price_amount') if _price_type != 'free' else 0,
                     str(plugin_meta.get('price_interval') or 'onetime')))
                _row = _cur.fetchone()
                conn.commit()
            _sub_id = _row['id'] if _row else None
        except Exception:
            traceback.print_exc()
            if pending_dir:
                _shutil.rmtree(pending_dir, ignore_errors=True)
            return _json_result(False, error='Failed to create submission record', code=500)

        # 6. 自动 AI 审核（只写报告，不改 pending 状态）
        try:
            from .audit import review_plugin
            _auto = review_plugin(pending_dir,
                                  watermark_result=_wm,
                                  plugins_root=plugins_root,
                                  submitted_version=version)
            with get_registry_db() as conn:
                conn.execute(
                    "UPDATE plugin_submissions SET audit_status=%s, audit_report=%s, "
                    "audit_reasons=%s, reviewed_at=NOW(), updated_at=NOW() WHERE id=%s",
                    (_auto['status'],
                     json.dumps(_auto['report'], ensure_ascii=False),
                     json.dumps(_auto['reasons'], ensure_ascii=False),
                     _sub_id))
                conn.commit()
        except Exception:
            traceback.print_exc()   # 自动审核失败不阻断提交，保留 pending 供人工
            # 审计 P2-6 修复：失败不得静默，落 review_comment 标记待人工复核
            try:
                with get_registry_db() as conn:
                    conn.execute(
                        "UPDATE plugin_submissions SET review_comment=%s, updated_at=NOW() "
                        "WHERE id=%s",
                        ('auto audit failed, needs manual review', _sub_id))
                    conn.commit()
            except Exception:
                traceback.print_exc()

        return _json_result(True, data={
            'submission_id': _sub_id,
            'identifier': identifier,
            'name': name,
            'version': version,
            'status': 'pending',
            'message': '插件已提交审核，请前往开发者中心查看进度。',
        })

    except json.JSONDecodeError:
        return _json_result(False, error='plugin.json is not valid JSON', code=400)
    except ValueError as e:
        return _json_result(False, error=f'Invalid archive: {e!s}', code=400)
    except Exception as e:
        return _json_result(False, error=f'Submit failed: {e!s}', code=500)
    finally:
        if tmp_path and _os.path.exists(tmp_path):
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass


# ====================================================================
# ★ P2 收益分成（developer_payouts · 80/20）
# ====================================================================

import re as _p2_re


def _p2_validate_period(period: str) -> str:
    """校验/推导结算周期：'YYYY-MM'；缺省取上一自然月。"""
    period = (period or '').strip()
    if period:
        if not _p2_re.match(r'^[0-9]{4}-(0[1-9]|1[0-2])$', period):
            raise ValueError('period must be YYYY-MM')
        return period
    from datetime import timedelta
    _now = datetime.now()
    _prev = _now.replace(day=1) - timedelta(days=1)
    return _prev.strftime('%Y-%m')


def _p2_aggregate(conn, period: str) -> dict:
    """按月聚合第三方插件支付流水（fen）。

    数据源（P0-2 修正）：plugin_payment_orders（paid 订单 paid_at 命中周期）
    + plugin_subscriptions（active 订阅 last_charge_at 命中周期）。
    仅统计 store_plugins.developer_id > 0 的第三方插件。
    返回 {developer_id: gross_fen}
    """
    gross: dict = {}
    # 1) 一次性买断订单
    rows = conn.execute(
        "SELECT sp.developer_id AS dev_id, SUM(po.amount_fen) AS gross "
        "FROM plugin_payment_orders po "
        "JOIN store_plugins sp ON sp.identifier = po.plugin_id "
        "WHERE sp.developer_id > 0 AND po.status = 'paid' "
        "  AND po.paid_at LIKE %s "
        "GROUP BY sp.developer_id",
        (f'{period}%',)).fetchall()
    for r in rows:
        gross[r['dev_id']] = gross.get(r['dev_id'], 0) + (r['gross'] or 0)
    # 2) 订阅续费流水（本月有扣款记录的 active 订阅）
    rows = conn.execute(
        "SELECT sp.developer_id AS dev_id, SUM(ps.amount_fen) AS gross "
        "FROM plugin_subscriptions ps "
        "JOIN store_plugins sp ON sp.identifier = ps.plugin_id "
        "WHERE sp.developer_id > 0 AND ps.status = 'active' "
        "  AND ps.last_charge_at LIKE %s "
        "GROUP BY sp.developer_id",
        (f'{period}%',)).fetchall()
    for r in rows:
        gross[r['dev_id']] = gross.get(r['dev_id'], 0) + (r['gross'] or 0)
    return gross


@bp.route('/store/admin/payouts/generate', methods=['POST'])
def generate_payouts():
    """P2：按周期聚合支付流水生成结算记录（幂等，可重跑）。

    金额口径统一 fen；ratio 取 store_developers.payout_ratio（默认 80）。
    UNIQUE(developer_id, period) → ON CONFLICT 覆盖更新。
    """
    err = _require_store_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        period = _p2_validate_period(str(data.get('period') or ''))
    except ValueError as e:
        return _json_result(False, error=str(e), code=400)
    try:
        with get_registry_db() as conn:
            gross = _p2_aggregate(conn, period)
            if not gross:
                return _json_result(True, data={'period': period,
                                                'developers': [], 'total': 0})
            result = []
            total_fen = 0
            for dev_id, g in sorted(gross.items()):
                row = conn.execute(
                    'SELECT payout_ratio FROM store_developers WHERE id=%s', (dev_id,)).fetchone()
                ratio = row['payout_ratio'] if row else 80
                amount = int(g * ratio / 100)
                total_fen += amount
                cur = conn.execute(
                    "INSERT INTO developer_payouts "
                    "(developer_id, period, gross_fen, ratio, amount_fen) "
                    "VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT(developer_id, period) DO UPDATE SET "
                    "gross_fen=EXCLUDED.gross_fen, ratio=EXCLUDED.ratio, "
                    "amount_fen=EXCLUDED.amount_fen "
                    "RETURNING id, status",
                    (dev_id, period, g, ratio, amount))
                _p = cur.fetchone()
                result.append({'developer_id': dev_id,
                               'gross_fen': g, 'ratio': ratio,
                               'amount_fen': amount,
                               'payout_id': _p['id'], 'status': _p['status']})
            conn.commit()
        return _json_result(True, data={'period': period,
                                        'developers': result,
                                        'total_amount_fen': total_fen})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/developer/payouts', methods=['GET'])
def developer_payouts():
    """P2：开发者查看自己的结算记录。"""
    dev, err = _require_developer()
    if err:
        return err
    try:
        with get_registry_db() as conn:
            rows = conn.execute(
                'SELECT * FROM developer_payouts WHERE developer_id=%s '
                'ORDER BY period DESC', (dev['id'],)).fetchall()
        return _json_result(True, data=[dict(r) for r in rows])
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/developer/payout-account', methods=['PUT'])
def developer_payout_account():
    """P2-1：开发者绑定/更新结算账户。

    写入 store_developers.payout_account（JSON）。
    注意：生产环境该字段应加密存储（当前明文落库，属已知缺口，见方案标注）。
    """
    dev, err = _require_developer()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    account_type = (data.get('account_type') or '').strip()
    account_no = (data.get('account_no') or '').strip()
    account_name = (data.get('account_name') or '').strip()
    if not account_type or not account_no:
        return _json_result(False, error='account_type and account_no are required', code=400)
    if account_type not in ('alipay', 'wechat', 'bank', 'paypal'):
        return _json_result(False, error=f'unsupported account_type: {account_type}', code=400)
    try:
        payload = json.dumps({
            'account_type': account_type,
            'account_no': account_no,
            'account_name': account_name,
        }, ensure_ascii=False)
        with get_registry_db() as conn:
            conn.execute(
                'UPDATE store_developers SET payout_account=%s, updated_at=NOW() WHERE id=%s',
                (payload, dev['id']))
            conn.commit()
        # 脱敏回显：只返回尾 4 位，防完整账号泄露
        masked = '****' + account_no[-4:] if len(account_no) >= 4 else '****'
        return _json_result(True, data={'account_type': account_type,
                                        'account_no': masked})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/developer/withdrawals', methods=['POST'])
def developer_withdrawal_apply():
    """P2-2：开发者发起提现申请。

    可提现余额 = 已结算(paid)合计 - 已在途(pending/processing)提现合计。
    幂等：存在在途申请时拒绝重复申请。
    """
    dev, err = _require_developer()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        amount_fen = int(data.get('amount_fen') or 0)
    except (TypeError, ValueError):
        return _json_result(False, error='amount_fen must be integer', code=400)
    if amount_fen <= 0:
        return _json_result(False, error='amount_fen must be positive', code=400)
    try:
        with get_registry_db() as conn:
            paid = conn.execute(
                "SELECT COALESCE(SUM(amount_fen),0) AS t FROM developer_payouts "
                "WHERE developer_id=%s AND status='paid'",
                (dev['id'],)).fetchone()['t']
            inflight = conn.execute(
                "SELECT COALESCE(SUM(amount_fen),0) AS t FROM developer_withdrawals "
                "WHERE developer_id=%s AND status IN ('pending','processing')",
                (dev['id'],)).fetchone()['t']
            available = (paid or 0) - (inflight or 0)
            if amount_fen > available:
                return _json_result(False,
                                    error=f'insufficient balance: available={available}, requested={amount_fen}',
                                    code=400)
            # 幂等：存在在途申请即拒绝
            dup = conn.execute(
                "SELECT id FROM developer_withdrawals "
                "WHERE developer_id=%s AND status IN ('pending','processing') LIMIT 1",
                (dev['id'],)).fetchone()
            if dup:
                return _json_result(False, error='a withdrawal is already in progress', code=400)
            account_snapshot = conn.execute(
                'SELECT payout_account FROM store_developers WHERE id=%s', (dev['id'],)).fetchone()
            cur = conn.execute(
                "INSERT INTO developer_withdrawals (developer_id, amount_fen, account) "
                "VALUES (%s,%s,%s) RETURNING id",
                (dev['id'], amount_fen, account_snapshot['payout_account'] or '{}'))
            wid = cur.fetchone()['id']
            conn.commit()
        return _json_result(True, data={'withdrawal_id': wid, 'amount_fen': amount_fen,
                                        'status': 'pending'})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/developer/withdrawals', methods=['GET'])
def developer_withdrawals():
    """P2-2：开发者查看自己的提现历史。"""
    dev, err = _require_developer()
    if err:
        return err
    try:
        with get_registry_db() as conn:
            rows = conn.execute(
                'SELECT * FROM developer_withdrawals WHERE developer_id=%s '
                'ORDER BY applied_at DESC LIMIT 100', (dev['id'],)).fetchall()
        return _json_result(True, data=[dict(r) for r in rows])
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


# ── P0-1：轻量技能层（社区 UGC，Markdown 低门槛）─────────────────────

def _skill_row_dict(row) -> dict:
    """技能行 → dict（tags JSON 反序列化）。"""
    d = dict(row)
    try:
        d['tags'] = json.loads(d.get('tags') or '[]')
    except Exception:
        d['tags'] = []
    return d


def _submit_skill_record(dev: dict, content_md: str):
    """技能提交核心：校验 + 自动审核 + 幂等入库（submit / import 共用）。

    Returns:
        (ok, payload, http_code)：ok=True → payload 为成功 data；
        ok=False → payload 为错误文案，http_code 区分 400/500。
    """
    from .skills import validate_skill, audit_skill
    errors, meta = validate_skill(content_md)
    if errors:
        return False, '; '.join(errors), 400
    audit_status, reasons = audit_skill(content_md)
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                'SELECT id FROM store_skills WHERE identifier=%s', (meta['identifier'],))
            existing = cur.fetchone()
            if existing:
                cur = conn.execute(
                    'UPDATE store_skills SET content_md=%s, name=%s, description=%s, '
                    'tagline=%s, tags=%s, version=%s, audit_status=%s, audit_note=%s, '
                    "status='pending', updated_at=NOW() WHERE id=%s RETURNING id",
                    (content_md, meta['name'], meta['description'], meta['tagline'],
                     json.dumps(meta['tags']), meta['version'], audit_status,
                     json.dumps(reasons), existing['id']))
            else:
                cur = conn.execute(
                    "INSERT INTO store_skills "
                    " (identifier, name, description, tagline, tags, content_md, "
                    "  author_developer_id, version, status, audit_status, audit_note) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s) RETURNING id",
                    (meta['identifier'], meta['name'], meta['description'], meta['tagline'],
                     json.dumps(meta['tags']), content_md, dev['id'], meta['version'],
                     audit_status, json.dumps(reasons)))
            skill_id = cur.fetchone()['id']
            conn.commit()
        return True, {'id': skill_id, 'identifier': meta['identifier'],
                      'status': 'pending', 'audit_status': audit_status}, 200
    except Exception as e:
        traceback.print_exc()
        return False, f'Failed: {e}', 500


@bp.route('/skills/submit', methods=['POST'])
def skill_submit():
    """P0-1：开发者提交技能（幂等：重复 identifier 更新未审版本为 pending）。"""
    dev, err = _require_developer()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    content_md = (data.get('content_md') or '').strip()
    if not content_md:
        return _json_result(False, error='content_md is required', code=400)
    ok, payload, code = _submit_skill_record(dev, content_md)
    return _json_result(ok, data=payload if ok else None,
                        error=None if ok else payload, code=code)


@bp.route('/skills/import', methods=['POST'])
def skill_import():
    """P1-4：导入外部 SKILL.md（粘贴内容或 URL 拉取），复用 submit 校验/审核/入库。"""
    dev, err = _require_developer()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    content_md = (data.get('content_md') or '').strip()
    url = (data.get('url') or '').strip()
    if not content_md and url:
        if not url.startswith(('http://', 'https://')):
            return _json_result(False, error='url must be http(s)', code=400)
        try:
            from urllib.request import urlopen, Request as _Req
            with urlopen(_Req(url, headers={'User-Agent': 'VeroRun-PluginManager/1.0'}),
                         timeout=10) as _resp:
                content_md = _resp.read().decode('utf-8', errors='replace').strip()
        except Exception as e:
            return _json_result(False, error=f'Failed to fetch url: {e}', code=400)
    if not content_md:
        return _json_result(False, error='content_md or url is required', code=400)
    ok, payload, code = _submit_skill_record(dev, content_md)
    return _json_result(ok, data=payload if ok else None,
                        error=None if ok else payload, code=code)


@bp.route('/skills/<identifier>/export', methods=['GET'])
def skill_export(identifier: str):
    """P1-4：导出标准 SKILL.md（兼容 agentskills.io / Hermes 格式）。"""
    try:
        with get_registry_db() as conn:
            row = conn.execute(
                "SELECT * FROM store_skills WHERE identifier=%s AND status='approved'",
                (identifier,)).fetchone()
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)
    if not row:
        return _json_result(False, error='Skill not found', code=404)
    from .skills import export_skill_md
    from flask import make_response
    md = export_skill_md(dict(row))
    resp = make_response(md)
    resp.headers['Content-Type'] = 'text/markdown; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{identifier}.md"'
    return resp


# ── 可观测性：深度健康检查（admin 域，供 Nginx/负载均衡判活） ────────

@bp.route('/health/ready', methods=['GET'])
def health_ready():
    """插件管理器就绪检查：registry DB + 商店目录 + MCP 工具。"""
    from shared.observability import measure, build_health_payload
    from .models_store import get_registry_db

    def _pg_ok():
        with get_registry_db() as conn:
            conn.execute('SELECT 1')
        return True, 'ok'

    def _store_ok():
        mgr = _get_manager()
        if not mgr or not mgr.store_client:
            return False, 'store client unavailable'
        try:
            mgr.store_client._last_sync_ts  # noqa: B018 存在性探测
        except Exception:
            pass
        return True, 'available'

    def _mcp_ok():
        from .mcp import _enabled_records
        try:
            n = len(_enabled_records())
            return True, f'{n} mcp server(s) enabled'
        except Exception as e:
            return False, str(e)

    checks = [measure(_pg_ok, 'registry_db'),
              measure(_store_ok, 'store_catalog'),
              measure(_mcp_ok, 'mcp_servers')]
    payload = build_health_payload('plugin_manager', checks)
    return _json_result(True, data=payload)


@bp.route('/skills/browse', methods=['GET'])
def skill_browse():
    """P0-1：商店浏览已上架技能（q 搜索 + 分页，按安装量排序）。"""
    q = (request.args.get('q') or '').strip()
    page = _parse_positive_int('page', 1, 1, 100000)
    per_page = _parse_positive_int('per_page', 20, 1, 100)
    where = ["status='approved'"]
    params: list = []
    if q:
        where.append('(name ILIKE %s OR description ILIKE %s OR tags ILIKE %s)')
        like = f'%{q}%'
        params += [like, like, like]
    sql_where = ' AND '.join(where)
    try:
        with get_registry_db() as conn:
            total = conn.execute(
                f'SELECT COUNT(*) AS c FROM store_skills WHERE {sql_where}', params
            ).fetchone()['c']
            rows = conn.execute(
                f'SELECT id, identifier, name, description, tagline, tags, version, '
                f'rating, installs, author_developer_id, created_at, requirements, source '
                f'FROM store_skills '
                f'WHERE {sql_where} ORDER BY installs DESC, created_at DESC '
                f'LIMIT %s OFFSET %s',
                params + [per_page, (page - 1) * per_page]).fetchall()
        items = [_skill_row_dict(r) for r in rows]
        reg = get_skill_registry()
        if reg is not None:                       # 降级铁律：registry 未初始化则不带 availability
            avail = reg.availability_map([dict(r) for r in rows], site_id=0)  # P0 单站点
            for it in items:
                it['availability'] = avail.get(it['id'], {'available': True, 'reasons': []})
        return _json_result(True, data={'items': items,
                                        'total': total, 'page': page, 'per_page': per_page})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/skills/<identifier>', methods=['GET'])
def skill_detail(identifier):
    """P0-1：技能详情（含 content_md，公开）。"""
    try:
        with get_registry_db() as conn:
            row = conn.execute(
                "SELECT * FROM store_skills WHERE identifier=%s AND status='approved'",
                (identifier,)).fetchone()
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)
    if not row:
        return _json_result(False, error='Skill not found', code=404)
    return _json_result(True, data=_skill_row_dict(row))


@bp.route('/skills/<identifier>/install', methods=['POST'])
def skill_install(identifier):
    """P0-1：安装技能到本地 data/skills/<identifier>/SKILL.md（登录用户）。"""
    user_id, _name, _admin = _get_jwt_user()
    if not user_id:
        return _json_result(False, error='Not logged in', code=401)
    try:
        with get_registry_db() as conn:
            row = conn.execute(
                "SELECT id, identifier, content_md, version FROM store_skills "
                "WHERE identifier=%s AND status='approved'", (identifier,)).fetchone()
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)
    if not row:
        return _json_result(False, error='Skill not found', code=404)
    # 依赖预检（P0）：registry 启用时先过 AvailabilityResolver，不满足则拒绝落盘
    reg = get_skill_registry()
    if reg is not None:
        pre = reg.install(int(row['id']), 0, row['version'])   # site_id=0（P0 单站点）
        if not pre['ok']:
            return _json_result(False, data={'missing': pre['missing']},
                                error='requirements_not_met', code=200)
    from .skills import install_skill
    try:
        path = install_skill(row['identifier'], row['content_md'])
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Install failed: {e}', code=500)
    try:
        with get_registry_db() as conn:
            conn.execute('UPDATE store_skills SET installs=installs+1 WHERE id=%s',
                         (row['id'],))
            conn.commit()
    except Exception:
        pass
    return _json_result(True, data={'identifier': row['identifier'], 'path': path})


@bp.route('/skills/<identifier>/uninstall', methods=['POST'])
def skill_uninstall(identifier):
    """P0-1：卸载本地技能（删 SKILL.md）。"""
    user_id, _name, _admin = _get_jwt_user()
    if not user_id:
        return _json_result(False, error='Not logged in', code=401)
    from .skills import uninstall_skill
    try:
        removed = uninstall_skill(identifier)
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Uninstall failed: {e}', code=500)
    return _json_result(True, data={'removed': removed})


@bp.route('/skills/mine', methods=['GET'])
def skill_mine():
    """P0-1：开发者自己的技能列表。"""
    dev, err = _require_developer()
    if err:
        return err
    try:
        with get_registry_db() as conn:
            rows = conn.execute(
                'SELECT * FROM store_skills WHERE author_developer_id=%s '
                'ORDER BY created_at DESC', (dev['id'],)).fetchall()
        return _json_result(True, data=[_skill_row_dict(r) for r in rows])
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/skills/admin', methods=['GET'])
def skill_admin_list():
    """P0-1：管理员技能列表（默认待审，?status= 过滤）。"""
    err = _require_store_admin()
    if err:
        return err
    status = (request.args.get('status') or 'pending').strip()
    try:
        with get_registry_db() as conn:
            rows = conn.execute(
                'SELECT * FROM store_skills WHERE status=%s ORDER BY created_at ASC',
                (status,)).fetchall()
        return _json_result(True, data=[_skill_row_dict(r) for r in rows])
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/skills/admin/<int:skill_id>/review', methods=['POST'])
def skill_admin_review(skill_id):
    """P0-1：管理员审核技能（approve 上架 / reject 驳回）。"""
    err = _require_store_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    action = (data.get('action') or '').strip()
    if action not in ('approve', 'reject'):
        return _json_result(False, error='action must be approve|reject', code=400)
    new_status = 'approved' if action == 'approve' else 'rejected'
    note = (data.get('note') or '').strip()
    try:
        with get_registry_db() as conn:
            cur = conn.execute(
                "UPDATE store_skills SET status=%s, audit_note=%s, "
                "published_at=CASE WHEN %s='approved' THEN NOW()::text ELSE published_at END, "
                "updated_at=NOW()::text WHERE id=%s RETURNING id, identifier",
                (new_status, note, new_status, skill_id))
            row = cur.fetchone()
            conn.commit()
        if not row:
            return _json_result(False, error='Skill not found', code=404)
        return _json_result(True, data={'id': row['id'], 'identifier': row['identifier'],
                                        'status': new_status})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/skills/available', methods=['GET'])
def skill_available():
    """P0：仅返回当前可用技能（供 Agent 编排器/前端下拉用）。
    支持 ?role= & task_type= 过滤；registry 关闭时降级返回空列表。"""
    role = (request.args.get('role') or '').strip()
    task_type = (request.args.get('task_type') or '').strip()
    reg = get_skill_registry()
    if reg is None:
        return _json_result(True, data=[])   # 降级铁律
    try:
        with get_registry_db() as conn:
            rows = conn.execute(
                "SELECT * FROM store_skills WHERE status='approved'").fetchall()
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)
    out = []
    for r in rows:
        row = dict(r)
        res = reg.resolver.evaluate(row, site_id=0)   # P0 单站点
        if not res.available:
            continue
        reqs = reg.resolver._parse_requirements(row.get('requirements', '{}'))
        if role and role not in reqs.get('roles', []):
            continue
        if task_type and task_type not in _skill_task_types(row):
            continue
        out.append(_skill_row_dict(r))
    return _json_result(True, data=out)


@bp.route('/skills/<identifier>/requirements', methods=['GET'])
def skill_requirements(identifier):
    """P0：返回技能依赖解析树（每条依赖的满足状态 + 恢复路径）。"""
    reg = get_skill_registry()
    if reg is None:
        return _json_result(True, data={'identifier': identifier, 'available': True,
                                        'requirements': {}, 'reasons': []})   # 降级铁律
    try:
        with get_registry_db() as conn:
            row = conn.execute(
                "SELECT * FROM store_skills WHERE identifier=%s AND status='approved'",
                (identifier,)).fetchone()
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)
    if not row:
        return _json_result(False, error='Skill not found', code=404)
    r = dict(row)
    res = reg.resolver.evaluate(r, site_id=0)
    reqs = reg.resolver._parse_requirements(r.get('requirements', '{}'))
    return _json_result(True, data={
        'identifier': identifier,
        'available': res.available,
        'requirements': reqs,
        'reasons': res.to_dict()['reasons'],
    })


@bp.route('/skills/admin/circuit-breaker', methods=['POST'])
def skill_circuit_breaker():
    """P0：熔断开关（scope: community | user | all），写 system_config。"""
    err = _require_store_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get('enabled', False))
    scope = (data.get('scope') or 'community').strip()
    if scope not in ('community', 'user', 'all'):
        return _json_result(False, error='scope must be community|user|all', code=400)
    value = scope if enabled else '0'
    try:
        with get_registry_db() as conn:
            conn.execute(
                "INSERT INTO system_config (key, value, description, updated_at) "
                "VALUES ('skill_circuit_breaker', %s, 'skill circuit breaker scope', NOW()::text) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()::text",
                (value,))
            conn.commit()
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)
    reg = get_skill_registry()
    if reg is not None:
        reg.resolver.invalidate()   # 熔断变化即时生效
    return _json_result(True, data={'enabled': enabled, 'scope': scope, 'value': value})


def _skill_task_types(row: dict) -> list:
    """从 requirements JSON 提取 task_types（0.9 技能返回空）。"""
    try:
        reqs = json.loads((row.get('requirements') or '{}'))
        return reqs.get('task_types') or []
    except Exception:
        return []


@bp.route('/store/admin/payouts', methods=['GET'])
def admin_payouts():
    """P2：管理员查看全部结算记录（可过滤 ?period= / ?status=）。"""
    err = _require_store_admin()
    if err:
        return err
    period = (request.args.get('period') or '').strip()
    status = (request.args.get('status') or '').strip()
    sql = 'SELECT p.*, d.display_name AS developer_name FROM developer_payouts p ' \
          'LEFT JOIN store_developers d ON d.id = p.developer_id WHERE 1=1'
    params = []
    if period:
        sql += ' AND p.period=%s'
        params.append(period)
    if status:
        sql += ' AND p.status=%s'
        params.append(status)
    sql += ' ORDER BY p.period DESC, p.id DESC LIMIT 500'
    try:
        with get_registry_db() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return _json_result(True, data=[dict(r) for r in rows])
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/store/admin/payouts/<int:pid>/pay', methods=['POST'])
def pay_payout(pid):
    """P2：管理员标记结算已支付。"""
    err = _require_store_admin()
    if err:
        return err
    try:
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT status FROM developer_payouts WHERE id=%s', (pid,)).fetchone()
            if not row:
                return _json_result(False, error='payout not found', code=404)
            if row['status'] == 'paid':
                return _json_result(False, error='payout already paid', code=400)
            if row['status'] == 'void':
                return _json_result(False, error='payout is void', code=400)
            conn.execute(
                "UPDATE developer_payouts SET status='paid', paid_at=NOW() WHERE id=%s",
                (pid,))
            conn.commit()
        return _json_result(True, data={'payout_id': pid, 'status': 'paid'})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


# ══════════════════════════════════════════════════════════════════════════
# v1.8 动态分类与动态分流
#   设计：《VeroRun 插件动态分类与动态分流 - 轻量增量方案 v1.0》§4.3 / §5
#   标准：docs/plugin-standard v1.8 §18
#   命名空间并入既有 /store/admin/*，不新建并列管理面。
#   ⚠️ 本段为**纯追加**：不改动上方任何既有端点与函数。
# ══════════════════════════════════════════════════════════════════════════

import re as _dist_re

_DIST_KEY_RE = _dist_re.compile(r'^[a-z0-9_]+$')


def _dist_int(raw, default: int) -> int:
    """宽容整数解析：缺失/非法 → default（供分流规则写入使用）。"""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _dist_admin_name() -> str:
    """尽力解析当前管理员标识（写入 distribution 审计列）；失败返回 ''。"""
    try:
        from services.jwt_service import validate_token
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            token = (request.args.get('token') or request.cookies.get('sso_token')
                     or request.cookies.get('tm_token'))
        payload = validate_token(token) if token else None
        if not payload:
            return ''
        return str(payload.get('username') or payload.get('email') or payload.get('sub') or '')
    except Exception:
        return ''


@bp.route('/store/categories', methods=['GET'])
def store_categories():
    """公开只读：分类注册表（供商店前台/后台渲染，替代硬编码 map）。

    前端**必须保留内置 map 作为 fallback**：本端点不可用时不崩、不空白。
    失败时返回空集而非 5xx —— 前端据此走内置 map。
    """
    try:
        from .distribution import list_categories, category_map
        return _json_result(True, data={
            'categories': list_categories(),
            'map': category_map(),
        })
    except Exception as e:
        print(f'[store] ⚠️ /store/categories 取数失败（前端将走内置 fallback）: {e}')
        return _json_result(True, data={'categories': [], 'map': {}})


@bp.route('/store/admin/categories', methods=['POST'])
def store_admin_category_save():
    """管理员：新增/更新分类（动态分类写入通道）。

    - ``builtin=1`` 的内置分类**可改展示名/emoji/渐变/排序，但不可删除**
    - ``ON CONFLICT(key) DO UPDATE`` 幂等；写后立即失效 resolver 快照
    """
    err = _require_store_admin()
    if err:
        return err

    d = request.get_json(silent=True) or {}
    key = (d.get('key') or '').strip().lower()
    if not key or not _DIST_KEY_RE.match(key):
        return _json_result(False, error='key must match [a-z0-9_]+', code=400)

    try:
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT builtin FROM plugin_categories WHERE key=%s', (key,)).fetchone()
            _builtin = _dist_int(row['builtin'], 0) if row else 0
            conn.execute(
                "INSERT INTO plugin_categories "
                "(key, label, label_i18n_key, emoji, icon_svg, grad_from, grad_to, "
                " default_tagline, sort_order, enabled, builtin, is_official, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,NOW()) "
                "ON CONFLICT(key) DO UPDATE SET "
                "  label=excluded.label, label_i18n_key=excluded.label_i18n_key, "
                "  emoji=excluded.emoji, icon_svg=excluded.icon_svg, "
                "  grad_from=excluded.grad_from, grad_to=excluded.grad_to, "
                "  default_tagline=excluded.default_tagline, "
                "  sort_order=excluded.sort_order, enabled=excluded.enabled, updated_at=NOW()",
                (key, d.get('label', ''), d.get('label_i18n_key', ''),
                 d.get('emoji', ''), d.get('icon_svg', ''),
                 d.get('grad_from') or '#3b82f6', d.get('grad_to') or '#8b5cf6',
                 d.get('default_tagline', ''), _dist_int(d.get('sort_order'), 100),
                 _dist_int(d.get('enabled'), 1), _builtin))
            conn.commit()
        from .distribution import invalidate_cache
        invalidate_cache()
        return _json_result(True, data={'key': key})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/store/admin/distribution/<identifier>', methods=['GET', 'PUT'])
def store_admin_distribution(identifier: str):
    """管理员：读取 / 保存插件分流规则（动态分流写入通道）。

    与 ``store_plugins`` 严格 1:1（``identifier`` UNIQUE），仅承载分流附加语义。
    ``editions`` / ``profiles`` 均为 ``[]`` 时 = 不参与对应维度过滤（回落 yaml）。
    """
    err = _require_store_admin()
    if err:
        return err

    from .distribution import rule_for, invalidate_cache

    if request.method == 'GET':
        try:
            # 归一化对外形状：editions/profiles/categories 为 JSON 文本列，
            # 统一转数组返回，与 PUT 入参对称，避免消费方各自 json.loads。
            from .distribution import _as_list as _dist_list
            _rule = dict(rule_for(identifier) or {'identifier': identifier})
            for _f in ('editions', 'profiles', 'categories'):
                _rule[_f] = _dist_list(_rule.get(_f))
            _rule['hidden'] = _dist_int(_rule.get('hidden'), 0)
            _rule['priority'] = _dist_int(_rule.get('priority'), 100)
            return _json_result(True, data=_rule)
        except Exception as e:
            traceback.print_exc()
            return _json_result(False, error=f'Failed: {e}', code=500)

    d = request.get_json(silent=True) or {}

    def _str_list(name):
        v = d.get(name) or []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            return None
        return [x.strip() for x in v if x and x.strip()]

    _eds, _profs, _cats = _str_list('editions'), _str_list('profiles'), _str_list('categories')
    if _eds is None or _profs is None or _cats is None:
        return _json_result(
            False, error='editions/profiles/categories must be lists of strings', code=400)

    _channel = (d.get('channel') or 'stable').strip().lower()
    if _channel not in ('stable', 'beta', 'canary'):
        return _json_result(False, error='channel must be stable|beta|canary', code=400)

    # 分类归属必须命中合法集合（内置 ∪ 注册表）
    if _cats:
        try:
            from .distribution import valid_category_keys
            _bad = [c for c in _cats if c not in valid_category_keys()]
        except Exception:
            _bad = []
        if _bad:
            return _json_result(False, error=f'unknown categories: {_bad}', code=400)

    try:
        with get_registry_db() as conn:
            conn.execute(
                "INSERT INTO plugin_distribution_rules "
                "(identifier, editions, profiles, categories, channel, priority, "
                " hidden, note, updated_by, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()) "
                "ON CONFLICT(identifier) DO UPDATE SET "
                "  editions=excluded.editions, profiles=excluded.profiles, "
                "  categories=excluded.categories, channel=excluded.channel, "
                "  priority=excluded.priority, hidden=excluded.hidden, "
                "  note=excluded.note, updated_by=excluded.updated_by, updated_at=NOW()",
                (identifier,
                 json.dumps(_eds, ensure_ascii=False),
                 json.dumps(_profs, ensure_ascii=False),
                 json.dumps(_cats, ensure_ascii=False),
                 _channel, _dist_int(d.get('priority'), 100),
                 1 if d.get('hidden') else 0,
                 (d.get('note') or '')[:500],
                 _dist_admin_name()))
            conn.commit()
        invalidate_cache()
        return _json_result(True, data={'identifier': identifier})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


# ── 发行版矩阵（方案Ⅰ：业务版 × 形态 拆行；官方版/网站版「只排不白」）──
# 定位：动态分流的「发行版默认范围」权威源（标准 §18.2 优先级链级3）。
# 与 plugin_distribution_rules（单插件覆盖层，级1/2）互补；矩阵无命中时回落 yaml。
# 仅承载 default_exclude（本版默认排除），官方版/网站版=全量可选 → 不设 include。

@bp.route('/store/admin/editions', methods=['GET'])
def store_admin_editions_list():
    """管理员：读取发行版矩阵（全部版本及其默认排除清单）。"""
    err = _require_store_admin()
    if err:
        return err
    try:
        with get_registry_db() as conn:
            rows = conn.execute(
                'SELECT edition, label, form_factor, enabled, default_exclude, '
                '       note, updated_by, updated_at '
                'FROM edition_catalog ORDER BY form_factor, edition'
            ).fetchall()
        items = []
        for r in rows:
            try:
                excludes = json.loads(r['default_exclude'] or '[]')
                if not isinstance(excludes, list):
                    excludes = []
            except (TypeError, ValueError):
                excludes = []
            items.append({
                'edition': r['edition'],
                'label': r['label'],
                'form_factor': r['form_factor'],
                'enabled': _dist_int(r['enabled'], 1),
                'default_exclude': excludes,
                'note': r['note'],
                'updated_by': r['updated_by'],
                'updated_at': r['updated_at'],
            })
        return _json_result(True, data={'items': items})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)


@bp.route('/store/admin/editions/<edition>', methods=['PUT'])
def store_admin_edition_save(edition: str):
    """管理员：新增 / 更新发行版矩阵单行（发行版默认范围，ON CONFLICT 幂等）。

    入参：``label`` / ``form_factor`` / ``enabled`` / ``default_exclude``(list) / ``note``。
    写后立即失效 resolver 快照；matrix 无记录时回落 yaml 语义，绝不影响既有行为。
    """
    err = _require_store_admin()
    if err:
        return err

    edition = (edition or '').strip().lower()
    if not edition or not _DIST_KEY_RE.match(edition):
        return _json_result(False, error='edition must match [a-z0-9_]+', code=400)

    d = request.get_json(silent=True) or {}
    form = (d.get('form_factor') or '').strip().lower()
    if form not in ('desktop', 'web', 'edge', ''):
        return _json_result(False, error='form_factor must be desktop|web|edge', code=400)

    excludes = d.get('default_exclude')
    if excludes is None:
        excludes = []
    if not isinstance(excludes, list) or not all(isinstance(x, str) for x in excludes):
        return _json_result(False, error='default_exclude must be a list of strings', code=400)
    _excl = [x.strip() for x in excludes if x and x.strip()]

    try:
        with get_registry_db() as conn:
            conn.execute(
                "INSERT INTO edition_catalog "
                "(edition, label, form_factor, enabled, default_exclude, note, "
                " updated_by, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,NOW()) "
                "ON CONFLICT(edition) DO UPDATE SET "
                "  label=excluded.label, form_factor=excluded.form_factor, "
                "  enabled=excluded.enabled, default_exclude=excluded.default_exclude, "
                "  note=excluded.note, updated_by=excluded.updated_by, updated_at=NOW()",
                (edition,
                 (d.get('label') or '').strip()[:64],
                 form,
                 _dist_int(d.get('enabled'), 1),
                 json.dumps(_excl, ensure_ascii=False),
                 (d.get('note') or '')[:500],
                 _dist_admin_name()))
            conn.commit()
        from .distribution import invalidate_cache
        invalidate_cache()
        return _json_result(True, data={'edition': edition})
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Failed: {e}', code=500)
