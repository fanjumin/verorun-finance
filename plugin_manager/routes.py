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
    """统一 json 响应"""
    resp = {'success': success}
    if data is not None:
        resp['data'] = data
    if error:
        resp['error'] = error
    return jsonify(resp), code


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
    err = _require_admin()
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
    err = _require_admin()
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
    err = _require_admin()
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


@bp.route('/store/admin', methods=['POST'])
def store_admin_save():
    """管理员：创建或更新商店插件商品"""
    err = _require_admin()
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
    from .store_importer import CATEGORY_ENUM
    if data.get('category') and data['category'] not in CATEGORY_ENUM:
        return _json_result(False, error=f'category must be one of {CATEGORY_ENUM}', code=400)
    for _f in ('download_url', 'icon_url', 'readme_url', 'author_url'):
        _v = (data.get(_f) or '').strip()
        if _v and not _v.startswith(('http://', 'https://')):
            return _json_result(False, error=f'{_f} must be a valid http(s) URL', code=400)

    # 适用版本：可选，必须为字符串列表（pro/standard/edge 等）
    _editions = data.get('compatible_editions', [])
    if not isinstance(_editions, list) or not all(isinstance(e, str) and e for e in _editions):
        return _json_result(False, error='compatible_editions must be a list of strings', code=400)

    # 宣传语：开发者手填优先；仅当为空时由 AI 从 README 兜底提取（失败自动降级，不阻断上架）
    tagline = (data.get('tagline') or '').strip()
    if tagline:
        tagline = tagline[:20]   # 长度限制 ≤20 字
    else:
        try:
            from i18n import get_lang
            _lang = get_lang()
        except Exception:
            _lang = 'zh-CN'
        tagline = _ensure_tagline(data, lang=_lang)

    with get_registry_db() as conn:
        conn.execute("""
            INSERT INTO store_plugins (
                identifier, name, name_i18n_key, description, version, author,
                author_url, icon_url, price_type, price_amount,
                price_interval, price_quarter_fen, price_year_fen, compatible_editions,
                trial_days, download_url, package_hash,
                file_size, category, tags, screenshots, readme_url,
                tagline, tagline_i18n_key, min_app_version, depends_on, enabled
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                min_app_version=excluded.min_app_version,
                depends_on=excluded.depends_on,
                enabled=excluded.enabled,
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
            data.get('tagline_font_size', '12px'),
            data.get('tagline_color', '#ffffff'),
            data.get('min_app_version', '0.10.0'),
            json.dumps(data.get('depends_on', {})),
            int(data.get('enabled', 1)),
        ))
        conn.commit()

    return _json_result(True, data={'identifier': identifier, 'saved': True})


# ── 39. 商店管理：删除插件商品 ────────────────────────────

@bp.route('/store/admin/<identifier>', methods=['DELETE'])
def store_admin_delete(identifier: str):
    """管理员：删除商店插件商品"""
    err = _require_admin()
    if err:
        return err

    with get_registry_db() as conn:
        row = conn.execute(
            'SELECT 1 FROM store_plugins WHERE identifier=%s', (identifier,)).fetchone()
        if not row:
            return _json_result(False, error=f'Plugin "{identifier}" not found', code=404)
        conn.execute('DELETE FROM store_plugins WHERE identifier=%s', (identifier,))
        conn.execute('DELETE FROM plugin_reviews WHERE plugin_identifier=%s', (identifier,))
        conn.commit()
    return _json_result(True, data={'deleted': True})


# ── 40. 商店管理：切换上架状态 ────────────────────────────

@bp.route('/store/admin/<identifier>/toggle', methods=['POST'])
def store_admin_toggle(identifier: str):
    """管理员：切换插件上架/下架状态"""
    err = _require_admin()
    if err:
        return err

    with get_registry_db() as conn:
        row = conn.execute('SELECT enabled FROM store_plugins WHERE identifier=%s', (identifier,)).fetchone()
        if not row:
            return _json_result(False, error='Plugin not found', code=404)
        new_enabled = 0 if row['enabled'] else 1
        conn.execute('UPDATE store_plugins SET enabled=%s, updated_at=NOW() WHERE identifier=%s',
                     (new_enabled, identifier))
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
    err = _require_admin()
    if err:
        return err
    data = request.json if request.is_json else {}
    try:
        base_month_fen = int(data.get('base_month_fen', 0))
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
    return _json_result(True, data=data)


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
    download_url = mgr.store_client.get_download_url(identifier, app_version)
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
        return _json_result(True, data={
            'identifier': identifier,
            'status': 'installed',
            'version': info.version,
        })
    except Exception as e:
        traceback.print_exc()
        return _json_result(False, error=f'Install failed: {e}', code=500)


# ── 商店在线升级 ──────────────────────────────────

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

    # 需要重启时：后台延迟 3 秒重启 admin 服务（sudo 免密已配置）
    if result.get('needs_restart'):
        def _restart():
            time.sleep(3)
            try:
                subprocess.Popen(
                    ['sudo', 'systemctl', 'restart', 'verorun-admin'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                print(f'[PluginManager] ⚠️ restart verorun-admin failed: {e}')
        threading.Thread(target=_restart, daemon=True).start()

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
    err = _require_admin()
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
    err = _require_admin()
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
        result = review_plugin(row['file_path'])
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
    err = _require_admin()
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
        _override = bool((request.get_json(silent=True) or {}).get('override'))
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
    err = _require_admin()
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
    return _json_result(True, data={'subscriptions': subs})


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
