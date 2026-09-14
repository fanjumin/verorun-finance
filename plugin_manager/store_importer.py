#!/usr/bin/env python3
"""
plugin_manager.store_importer — 商店元数据导入器（GitHub/Gitee 仓库 → Add Plugin 表单预填充）
================================================================================================
为 Store Admin「从 GitHub/Gitee 导入」功能提供服务端代理：
  1. URL 域名白名单校验（防 SSRF，仅允许 github.com / raw.githubusercontent.com / cdn.jsdelivr.net / gitee.com）
  2. Gitee 优先双平台抓取 plugin.json（raw → 平台 API 兜底；github 随时被墙，gitee 作为首选源）
  3. 归一化为 store_plugins 兼容字段，返回字段校验 warnings（不落库，由前端回填表单）

安全说明：
  - 仅返回元数据用于表单回填，不执行任何远程代码；保存仍须管理员人工确认。
  - 所有外部请求均设超时，失败即降级/报错，不阻塞主流程。
"""

import os
import re
import json
import base64
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ── SSRF 防护：仅允许访问以下域名 ──
ALLOWED_NETLOCS = {'github.com', 'raw.githubusercontent.com', 'cdn.jsdelivr.net'}

# 内置分类兜底集（v1.8 起不再是封闭白名单）：
#   实际合法分类 = 本常量 ∪ plugin_categories 注册表（enabled=1）
#   见 plugin_manager/distribution.py::valid_category_keys 与 docs/plugin-standard v1.8 §18.1
# 前端下拉 / docs/plugin-manifest.schema.json 与本常量保持一致的「内置部分」；
# 注册表为空 / 取数失败时，白名单等价于本常量（行为等同 v1.7）。
CATEGORY_ENUM = ['system', 'shop', 'content', 'ai_agent', 'social', 'tools', 'supply_chain']

_SEMVER_RE = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')
_IDENTIFIER_RE = re.compile(r'^[a-z0-9_]+$')

_TIMEOUT = 8


def parse_github_url(url: str) -> Optional[Dict[str, str]]:
    """解析 GitHub/Gitee URL → {host, owner, repo, branch, dir}；非法返回 None。

    支持两种仓库定位形式（两平台路径结构一致）：
      - https://github.com/owner/repo  或 https://gitee.com/owner/repo     → 根目录（独立插件仓库）
      - https://github.com/owner/repo/tree/<branch>/<dir>                  → 子目录（官方插件位于主仓库 plugins/ 下）
      - https://github.com/owner/repo/blob/<branch>/<dir>/plugin.json      → 直指清单文件
    """
    try:
        u = urlparse((url or '').strip())
    except ValueError:
        return None
    if u.scheme not in ('http', 'https') or u.netloc not in SUPPORTED_REPO_HOSTS:
        return None
    host = 'gitee' if u.netloc == 'gitee.com' else 'github'
    parts = [p for p in u.path.split('/') if p]
    if len(parts) < 2:
        return None

    owner, repo = parts[0], parts[1]
    branch = 'main'
    dir_path = ''
    if len(parts) > 2:
        seg = parts[2]
        if seg in ('tree', 'blob') and len(parts) >= 4:
            branch = parts[3]
            dir_path = '/'.join(parts[4:])
            if seg == 'blob' and dir_path.endswith('/plugin.json'):
                dir_path = dir_path.rsplit('/plugin.json', 1)[0]
        else:
            return None  # 不认识的路径结构，统一拒绝
    return {'host': host, 'owner': owner, 'repo': repo, 'branch': branch, 'dir': dir_path}


def _http_get(url: str, timeout: int = _TIMEOUT) -> Optional[str]:
    """GET 文本内容；非 200 或异常返回 None。"""
    try:
        req = Request(url, headers={'User-Agent': 'VeroRun-StoreImporter/1.0'})
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read().decode('utf-8', errors='replace')
    except (URLError, HTTPError, OSError, ValueError):
        return None


def _build_manifest_paths(host: str, owner: str, repo: str, branch: str, manifest_path: str) -> List[str]:
    """按托管平台构建候选抓取 URL（raw 优先，平台 API 兜底）。

    gitee: raw 网页直链 → Gitee API raw（可选 GITEE_TOKEN 访问私有仓库）
    github: raw.githubusercontent → jsDelivr CDN
    """
    if host == 'gitee':
        gitee_token = os.environ.get('GITEE_TOKEN', '').strip()
        api = f'https://gitee.com/api/v5/repos/{owner}/{repo}/raw/{branch}/{manifest_path}'
        if gitee_token:
            api += f'?access_token={gitee_token}'
        return [
            f'https://gitee.com/{owner}/{repo}/raw/{branch}/{manifest_path}',
            api,
        ]
    return [
        f'https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{manifest_path}',
        f'https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{manifest_path}',
    ]


def _fetch_manifest(owner: str, repo: str, branch: str, dir_path: str = '',
                    host: str = 'github') -> Tuple[Optional[dict], str]:
    """抓取 plugin.json：Gitee 优先双平台回退 + 候选分支（显式 → main → master）→ 平台 API。

    dir_path 为插件所在子目录（如 plugins/veroscholar），空表示仓库根目录。
    Gitee 作为首选抓取源（github 随时被墙）；两平台同名仓库均无 raw 时，
    GitHub 私有仓库可在 API 层配置 GITHUB_TOKEN、Gitee 私有仓库可配置 GITEE_TOKEN。

    返回 (manifest, source)：source 为成功 URL，或 '' / 'notoken' / 'api_miss'。
    """
    manifest_path = f'{dir_path}/plugin.json' if dir_path else 'plugin.json'
    branches = [branch] + [b for b in ('main', 'master') if b != branch]
    # Gitee 优先双平台回退（github 随时被墙，gitee 同名仓库作为首选源）
    hosts = ['gitee', 'github']
    for h in hosts:
        for b in branches:
            for u in _build_manifest_paths(h, owner, repo, b, manifest_path):
                text = _http_get(u)
                if text:
                    try:
                        return json.loads(text), u
                    except json.JSONDecodeError:
                        pass
    # GitHub API 兜底（私有仓库读取途径，遍历候选分支）
    token = os.environ.get('GITHUB_TOKEN', '').strip()
    if token:
        headers = {'User-Agent': 'VeroRun-StoreImporter/1.0',
                   'Accept': 'application/vnd.github+json',
                   'Authorization': f'Bearer {token}'}
        for b in branches:
            api_url = f'https://api.github.com/repos/{owner}/{repo}/contents/{manifest_path}?ref={b}'
            try:
                req = Request(api_url, headers=headers)
                with urlopen(req, timeout=_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                return json.loads(base64.b64decode(data['content']).decode('utf-8')), api_url
            except HTTPError:
                continue  # 该分支无此文件，尝试下一个候选分支
            except Exception:
                return None, 'api_miss'
        return None, 'api_miss'
    return None, 'notoken'


def _repo_is_public(owner: str, repo: str, host: str = 'github') -> Optional[bool]:
    """探测仓库公开性（仅当 raw 层失败、无 token 时调用一次）；未知返回 None。

    HTTP 403/404 视为私有或不存在（无匿名访问权限）；网络异常视为未知。
    """
    if host == 'gitee':
        try:
            req = Request(f'https://gitee.com/api/v5/repos/{owner}/{repo}',
                          headers={'User-Agent': 'VeroRun-StoreImporter/1.0'})
            with urlopen(req, timeout=_TIMEOUT) as resp:
                return resp.status == 200
        except HTTPError:
            return False
        except Exception:
            return None
    try:
        req = Request(f'https://api.github.com/repos/{owner}/{repo}',
                      headers={'User-Agent': 'VeroRun-StoreImporter/1.0'})
        with urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.status == 200
    except HTTPError:
        return False
    except Exception:
        return None


def import_from_github(raw_url: str) -> Tuple[Optional[dict], List[str]]:
    """主入口：抓取 + 归一化。

    返回 (catalog_entry, warnings)：
      - 成功：entry 为可回填表单的 dict（与 store_plugins 列兼容），warnings 为非致命提示
      - 失败：entry 为 None，warnings[0] 为可展示的错误消息
    """
    warnings: List[str] = []
    parsed = parse_github_url(raw_url)
    if not parsed:
        return None, ['URL invalid: only https://github.com/owner/repo (or /tree/<branch>/<dir>) and https://gitee.com/owner/repo are supported']

    owner = parsed['owner']
    repo = parsed['repo']
    branch = parsed['branch']
    dir_path = parsed['dir']
    host = parsed['host']
    manifest, _src = _fetch_manifest(owner, repo, branch, dir_path, host)
    if not manifest or not isinstance(manifest, dict):
        if _src == 'notoken':
            public = _repo_is_public(owner, repo, host)
            if public is False:
                return None, ['Repo is private: configure GITHUB_TOKEN (github.com) or GITEE_TOKEN (gitee.com) to import from private repos.']
            if public is True:
                return None, ['No valid plugin.json found (please confirm the plugin manifest exists at the given path)']
            return None, ['Repo not accessible: confirm it is public, or configure GITHUB_TOKEN/GITEE_TOKEN for private repos.']
        return None, ['No valid plugin.json found (please confirm the plugin manifest exists at the given path)']

    # 必填字段校验
    missing = [f for f in ('identifier', 'name', 'version', 'description',
                           'author', 'min_app_version', 'agent_role', 'capabilities')
               if not manifest.get(f)]
    if missing:
        return None, [f'plugin.json missing required fields: {", ".join(missing)}']
    if not _IDENTIFIER_RE.match(manifest.get('identifier', '')):
        return None, ['identifier must match ^[a-z0-9_]+$']

    # 统一网关注册强制校验：agent_role 必须为核心角色之一（插件标准 §2.2）
    # 核心角色集由 agent_matrix/roles/*.yaml 动态推导（单一事实源），不再手写。
    try:
        from agent_matrix.models import get_core_role_slugs
        _core_roles = get_core_role_slugs()
    except ImportError:
        return None, ['agent_matrix unavailable: cannot validate agent_role']
    if manifest.get('agent_role') not in _core_roles:
        return None, [f'plugin.json agent_role must be one of the core roles: {", ".join(_core_roles)}']
    if not isinstance(manifest.get('capabilities'), list) or not manifest.get('capabilities'):
        return None, ['plugin.json capabilities must be a non-empty array of strings']

    if not _SEMVER_RE.match(manifest.get('version', '')):
        warnings.append('version is not x.y.z format, kept as-is, please review manually')

    # v1.8：白名单 = 内置 7 类 ∪ plugin_categories 注册表（标准 §18.1）。
    # CATEGORY_ENUM 保留为内置兜底常量；注册表不可用/为空时行为等同 v1.7。
    cat = manifest.get('category', '')
    if cat:
        try:
            from .distribution import valid_category_keys
            _cat_keys = valid_category_keys()
        except Exception as _e:
            print(f'[store_importer] ⚠️ 动态分类取数失败，回落内置枚举: {_e}')
            _cat_keys = set(CATEGORY_ENUM)
        if cat not in _cat_keys:
            warnings.append(f'category "{cat}" is not in whitelist, cleared, please select from dropdown')
            cat = ''

    readme_path = f'{dir_path}/README.md' if dir_path else 'README.md'

    # README 多命名抓取（问题2 方案A：服务端代理缓存用）
    # 命中优先级：中文命名 > 英文默认；双平台回退；失败静默（readme_cache 留空，不影响导入）
    readme_url = (manifest.get('readme_url') or '').strip()
    readme_cache = ''
    readme_names = ('README.cn.md', 'README_CN.md', 'README.zh-CN.md', 'README.md')
    if dir_path:
        readme_names = tuple(f'{dir_path}/{n}' for n in readme_names)
    for h in ('gitee', 'github'):
        if readme_cache:
            break
        for name in readme_names:
            for u in _build_manifest_paths(h, owner, repo, branch, name):
                text = _http_get(u)
                if text:
                    readme_url = u
                    readme_cache = text[:20000]
                    break
            if readme_cache:
                break

    entry = {
        'identifier': manifest['identifier'],
        'name': manifest.get('name', ''),
        'name_i18n_key': manifest.get('name_i18n_key', ''),
        'description': manifest.get('description', ''),
        'version': manifest.get('version', '0.1.0'),
        'author': manifest.get('author', ''),
        'author_url': manifest.get('author_url', ''),
        'category': cat,
        'tags': (manifest.get('tags') or [])[:10],
        'tagline': (manifest.get('tagline') or '')[:32],
        'tagline_subtitle': (manifest.get('tagline_subtitle') or '')[:64],
        'tagline_i18n_key': manifest.get('tagline_i18n_key', ''),
        'icon_url': manifest.get('icon_url', ''),
        'min_app_version': manifest.get('min_app_version', '0.10.0'),
        'depends_on': manifest.get('depends_on', {}),
        'screenshots': (manifest.get('screenshots') or [])[:12],
        'readme_url': readme_url,
        'readme_cache': readme_cache,
        'download_url': '',      # 无 Release 时留空，管理员可后补
        'package_hash': '',
        'file_size': 0,
        'source': f'https://{"gitee.com" if host == "gitee" else "github.com"}/{owner}/{repo}',
    }
    return entry, warnings
