#!/usr/bin/env python3
"""
Plugin Manager — 商店客户端
=============================
从 GitHub Raw 拉取 store_catalog.json，本地缓存插件目录。
"""

import os
import json
import time
import threading
from typing import Dict, List, Optional, Any
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError
from urllib.parse import urlparse

from .models_store import (
    StorePlugin, init_license_store_tables, get_registry_db,
)
from .discovery import parse_version

# ── 商店目录源（P0-2 多源回退）────────────────────────────────────
# STORE_CATALOG_URLS: 逗号分隔的多源列表（主源优先，逐个回退）
# STORE_CATALOG_URL:  兼容单源配置（APP_STORE_CATALOG_URL 为更早别名）
_DEFAULT_CATALOG_URL = 'https://raw.githubusercontent.com/fanjumin/verorun-store/main/store_catalog.json'


def _catalog_urls() -> List[str]:
    """解析目录源列表（环境变量逗号分隔，主源优先）。"""
    urls = (os.environ.get('STORE_CATALOG_URLS')
            or os.environ.get('STORE_CATALOG_URL')
            or os.environ.get('APP_STORE_CATALOG_URL')
            or _DEFAULT_CATALOG_URL)
    return [u.strip() for u in urls.split(',') if u.strip()]

# 下载镜像前缀（P0-2）：设置后对 GitHub Raw 下载地址做 host 替换，走 CDN/镜像
DOWNLOAD_MIRROR_PREFIX = os.environ.get('DOWNLOAD_MIRROR_PREFIX', '').strip()

# 部署版本（阶段 3）：商店按 compatible_editions 过滤插件（空数组=全版本兼容）。
# 统一走 agent_matrix.current_edition()（单一事实源：VR_EDITION→RELEASE_EDITION→DEPLOY_TYPE，
# 新旧名归一 edu→research / pro→finance）；agent_matrix 不可用时兜底旧 DEPLOY_EDITION 环境变量。
def _resolve_deploy_edition() -> str:
    try:
        from agent_matrix.models import current_edition
        return current_edition()
    except Exception:
        return os.environ.get('DEPLOY_EDITION', 'standard').strip().lower()

DEPLOY_EDITION = _resolve_deploy_edition()

# 同步调度参数（P0-2）：成功固定间隔 6h；失败指数退避 15min 起、上限 6h
SYNC_SUCCESS_INTERVAL = 6 * 3600
SYNC_RETRY_BASE = 15 * 60
SYNC_RETRY_MAX = 6 * 3600


class StoreAPIClient:
    """插件商店 API 客户端"""

    def __init__(self):
        self._cache_lock = threading.Lock()
        init_license_store_tables()
        # P0-2：同步退避状态（连续失败指数退避，成功复位）
        self._sync_failures = 0
        self._last_sync_ts = 0.0
        self._last_sync_error = ''

    def _fetch_catalog(self) -> dict:
        """Fetch store_catalog.json 多源回退（P0-2）。

        按 STORE_CATALOG_URLS 顺序依次尝试，第一个成功即返回；
        全失败返回空 dict（保留本地缓存），并记录退避状态供调度。
        """
        urls = _catalog_urls()
        last_err = ''
        for url in urls:
            try:
                req = Request(url, headers={
                    'User-Agent': 'VeroRun-PluginManager/1.0',
                })
                with urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
                if isinstance(data, dict):
                    return data
                last_err = f'bad catalog payload from {url}'
            except Exception as e:
                last_err = f'{url}: {e}'
                print(f'[StoreAPIClient] fetch catalog failed: {last_err}')
        self._last_sync_error = last_err
        return {}

    # ── 同步调度状态（P0-2）─────────────────────────────────────────

    def record_success(self):
        """同步成功：复位失败计数，记录时间戳。"""
        self._sync_failures = 0
        self._last_sync_ts = time.time()
        self._last_sync_error = ''

    def record_failure(self):
        """同步失败：失败计数 +1（上限防溢出）。"""
        self._sync_failures = min(self._sync_failures + 1, 32)

    def next_sync_interval(self) -> int:
        """返回距离下次同步的等待秒数（指数退避）。

        成功（失败计数为 0）→ 固定 SYNC_SUCCESS_INTERVAL；
        失败 n 次 → min(SYNC_RETRY_BASE * 2^(n-1), SYNC_RETRY_MAX)。
        """
        if self._sync_failures <= 0:
            return SYNC_SUCCESS_INTERVAL
        delay = SYNC_RETRY_BASE * (2 ** (self._sync_failures - 1))
        return min(int(delay), SYNC_RETRY_MAX)

    # ── 搜索/列表 ──────────────────────────────────────────────────

    def search(self, query: str = '', category: str = '',
               price_type: str = '', page: int = 1,
               page_size: int = 20, sort_by: str = 'downloads') -> dict:
        """搜索商店插件

        从本地缓存查询（缓存由 sync_all() 定期刷新）。
        """
        return self._search_local(query, category, price_type, page, page_size, sort_by)

    def list_by_category(self, category: str = '') -> List[dict]:
        """按分类列出（从本地缓存）"""
        with self._cache_lock:
            with get_registry_db() as conn:
                if category:
                    rows = conn.execute(
                        'SELECT * FROM store_plugins WHERE enabled=1 AND category=%s '
                        "AND (compatible_editions = '[]' OR compatible_editions LIKE %s) "
                        'ORDER BY downloads DESC',
                        (category, f'%"{DEPLOY_EDITION}"%')
                    ).fetchall()
                else:
                    rows = conn.execute(
                        'SELECT * FROM store_plugins WHERE enabled=1 '
                        "AND (compatible_editions = '[]' OR compatible_editions LIKE %s) "
                        'ORDER BY downloads DESC',
                        (f'%"{DEPLOY_EDITION}"%',)
                    ).fetchall()
                return [StorePlugin.from_row(dict(r)).to_dict() for r in rows]

    def get_detail(self, identifier: str) -> Optional[dict]:
        """获取插件详情（从本地缓存）"""
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT * FROM store_plugins WHERE identifier=%s',
                (identifier,)
            ).fetchone()
            if row:
                plugin = StorePlugin.from_row(dict(row)).to_dict()
                plugin['_reviews'] = self._get_review_summary(identifier, conn)
                return plugin
        return None

    def _get_review_summary(self, identifier: str, conn=None) -> dict:
        """获取评价统计摘要"""
        if conn is None:
            with get_registry_db() as c:
                return self._get_review_summary(identifier, c)
        row = conn.execute(
            "SELECT COUNT(*) as cnt, AVG(rating) as avg_rating FROM plugin_reviews "
            "WHERE plugin_identifier=%s AND is_active=1",
            (identifier,)
        ).fetchone()
        return {
            'total': row['cnt'] if row else 0,
            'average': round(row['avg_rating'], 1) if row and row['avg_rating'] else 0.0,
        }

    # ── 下载 ────────────────────────────────────────────────────────

    def get_download_url(self, identifier: str, app_version: str = '') -> Optional[str]:
        """获取插件下载地址（从本地缓存，含版本兼容校验）

        Args:
            identifier: 插件标识符
            app_version: 当前系统版本号（如 '0.44.2'），用于兼容性校验

        Returns:
            下载 URL，或 None（不兼容时返回 None）
        """
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT download_url, min_app_version, compatible_editions '
                'FROM store_plugins WHERE identifier=%s',
                (identifier,)
            ).fetchone()
            if not row or not row['download_url']:
                return None
            if app_version and row['min_app_version']:
                if not self._version_compatible(app_version, row['min_app_version']):
                    return None
            # 阶段 3：部署版本过滤
            if not self._edition_compatible(json.loads(row.get('compatible_editions') or '[]')):
                return None
            return self._apply_mirror(row['download_url'])

    def get_download_urls(self, identifier: str,
                          app_version: str = '') -> tuple:
        """获取下载地址对 (primary, fallback)。

        primary  = 当前环境首选源（配置 DOWNLOAD_MIRROR_PREFIX 时为镜像，
                   否则为 catalog 原始 GitHub 地址）
        fallback = 主源网络失败时的降级源（未启用镜像时为 None）
        """
        primary = self.get_download_url(identifier, app_version)
        if not primary:
            return (None, None)
        parsed = urlparse(primary)
        if parsed.netloc in ('raw.githubusercontent.com', 'github.com'):
            # primary 即原始源（未启用镜像或 URL 非 GitHub），无需回退
            return (primary, None)
        # primary 为镜像地址 → 回退到 catalog 原始 URL
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT download_url FROM store_plugins WHERE identifier=%s',
                (identifier,)
            ).fetchone()
        original = row['download_url'] if row else None
        if original and original != primary:
            return (primary, original)
        return (primary, None)

    def download_package(self, identifier: str, dest_dir: str) -> str:
        """下载插件包并解压到 dest_dir（自动读取 download_url + package_hash 强校验）。

        Args:
            identifier: 插件标识符（从 store_plugins 表读取下载地址与 SHA256）
            dest_dir: 解压目标目录（应为独立 staging 目录）

        Returns:
            解压后的插件目录绝对路径

        Raises:
            ValueError: 商店无下载地址 / SHA256 不匹配 / 压缩包不合法
            URLError: 网络错误
            HTTPError: 远端 HTTP 错误
        """
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT download_url, package_hash FROM store_plugins WHERE identifier=%s',
                (identifier,)
            ).fetchone()
        if not row or not row.get('download_url'):
            raise ValueError(f'商店中不存在 {identifier} 的下载地址')
        from .downloader import download_plugin
        primary = self._apply_mirror(row['download_url'])
        fallback = None
        if primary and primary != row['download_url']:
            fallback = row['download_url']
        return download_plugin(
            primary, dest_dir,
            expected_hash=row.get('package_hash') or '',
            fallback_url=fallback or '')

    @staticmethod
    def _apply_mirror(url: str) -> str:
        """P0-2：按 DOWNLOAD_MIRROR_PREFIX 替换 GitHub Raw 下载 host 走镜像/CDN。

        仅对 raw.githubusercontent.com / github.com 域名替换，其余 URL 原样返回。
        """
        if not url or not DOWNLOAD_MIRROR_PREFIX:
            return url
        parsed = urlparse(url)
        if parsed.netloc not in ('raw.githubusercontent.com', 'github.com'):
            return url
        return DOWNLOAD_MIRROR_PREFIX.rstrip('/') + parsed.path

    @staticmethod
    def _edition_compatible(compatible_editions: list) -> bool:
        """阶段 3：按部署版本（DEPLOY_EDITION）过滤插件。

        空数组 = 全版本兼容（旧插件未标注不拦截）；
        非空时必须包含当前版本（大小写不敏感，双侧新旧名归一，
        旧 catalog 的 edu/pro 与现行 research/finance 视为同版本）。
        """
        if not compatible_editions:
            return True
        try:
            from agent_matrix.models import normalize_edition
            current = normalize_edition(DEPLOY_EDITION)
            return current in [normalize_edition(str(e)) for e in compatible_editions]
        except Exception:
            return DEPLOY_EDITION in [str(e).strip().lower() for e in compatible_editions]

    @staticmethod
    def _version_compatible(current: str, required: str) -> bool:
        """检查当前版本是否满足最低版本要求（semver）"""
        try:
            from packaging.version import Version
            return Version(current) >= Version(required)
        except ImportError:
            def _parse(v):
                try:
                    return tuple(int(x) for x in v.split('.'))
                except (ValueError, AttributeError):
                    return (0,)
            return _parse(current) >= _parse(required)

    # ── 本地缓存 ────────────────────────────────────────────────────

    def _search_local(self, query: str, category: str,
                      price_type: str, page: int,
                      page_size: int, sort_by: str = 'downloads') -> dict:
        """从本地缓存搜索"""
        with self._cache_lock:
            with get_registry_db() as conn:
                sql = 'SELECT s.* FROM store_plugins s WHERE s.enabled=1'
                params = []

                if query:
                    sql += ' AND (s.name LIKE %s OR s.description LIKE %s OR s.identifier LIKE %s)'
                    like = f'%{query}%'
                    params.extend([like, like, like])
                if category:
                    sql += ' AND s.category=%s'
                    params.append(category)
                if price_type:
                    sql += ' AND s.price_type=%s'
                    params.append(price_type)

                # 阶段 3：按部署版本过滤（compatible_editions 空数组=全兼容）
                sql += " AND (s.compatible_editions = '[]' OR s.compatible_editions LIKE %s)"
                params.append(f'%"{DEPLOY_EDITION}"%')

                # 排序
                sort_map = {
                    'downloads': 's.downloads DESC',
                    'rating': 's.rating DESC',
                    'newest': 's.created_at DESC',
                    'price_asc': 's.price_amount ASC',
                    'price_desc': 's.price_amount DESC',
                }
                sql += f' ORDER BY {sort_map.get(sort_by, "s.downloads DESC")}'

                # 总数（去掉 ORDER BY，PG 不允许 count 查询带排序列）
                count_sql = sql.replace('SELECT s.* FROM', 'SELECT COUNT(*) as cnt FROM')
                order_pos = count_sql.find(' ORDER BY ')
                if order_pos != -1:
                    count_sql = count_sql[:order_pos]
                total = conn.execute(count_sql, params).fetchone()['cnt']

                # 分页
                offset = (page - 1) * page_size
                sql += ' LIMIT %s OFFSET %s'
                params.extend([page_size, offset])

                rows = conn.execute(sql, params).fetchall()
                plugins = []
                for r in rows:
                    p = StorePlugin.from_row(dict(r)).to_dict()
                    p['_reviews'] = self._get_review_summary(p['identifier'], conn)
                    plugins.append(p)

                return {
                    'plugins': plugins,
                    'total': total,
                    'page': page,
                    'page_size': page_size,
                }

    def _upsert_cache(self, pdata: dict):
        """插入或更新本地缓存"""
        with self._cache_lock:
            with get_registry_db() as conn:
                conn.execute("""
                    INSERT INTO store_plugins (
                        identifier, name, name_i18n_key, description, version, author,
                        author_url, icon_url, price_type, price_amount,
                        price_interval, price_quarter_fen, price_year_fen, compatible_editions,
                        trial_days, download_url, package_hash,
                        file_size, category, tags, min_app_version, depends_on,
                        screenshots, readme_url, tagline, tagline_i18n_key,
                        tagline_font_size, tagline_color, tagline_subtitle, tagline_subtitle_font_size,
                        downloads, rating, review_count, readme_cache, enabled
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
                    -- ★ ON CONFLICT: 更新商店侧管理的字段 + 展示资源 URL（icon_url/readme_url/
                    --    screenshots）。展示资源由发布工具自动生成真实 CDN URL，需随同步覆盖。
                    --    tagline 用 COALESCE 保护：目录有值才覆盖，AI 生成/手写的 tagline 得以保留。
                    --    author/author_url/trial_days/min_app_version/depends_on 不在更新列表中，
                    --    以防止缓存同步覆盖 Store Admin 手动录入的内容。
                    ON CONFLICT(identifier) DO UPDATE SET
                        name=excluded.name,
                        name_i18n_key=excluded.name_i18n_key,
                        description=excluded.description,
                        version=excluded.version,
                        price_type=excluded.price_type,
                        price_amount=excluded.price_amount,
                        price_interval=excluded.price_interval,
                        price_quarter_fen=excluded.price_quarter_fen,
                        price_year_fen=excluded.price_year_fen,
                        compatible_editions=excluded.compatible_editions,
                        download_url=excluded.download_url,
                        package_hash=excluded.package_hash,
                        file_size=excluded.file_size,
                        category=excluded.category,
                        tags=excluded.tags,
                        downloads=excluded.downloads,
                        rating=excluded.rating,
                        review_count=excluded.review_count,
                        icon_url=excluded.icon_url,
                        screenshots=excluded.screenshots,
                        readme_url=excluded.readme_url,
                        tagline=COALESCE(NULLIF(excluded.tagline,''), store_plugins.tagline),
                        tagline_i18n_key=excluded.tagline_i18n_key,
                        tagline_font_size=COALESCE(NULLIF(excluded.tagline_font_size,''), store_plugins.tagline_font_size),
                        tagline_color=COALESCE(NULLIF(excluded.tagline_color,''), store_plugins.tagline_color),
                        tagline_subtitle=COALESCE(NULLIF(excluded.tagline_subtitle,''), store_plugins.tagline_subtitle),
                        tagline_subtitle_font_size=COALESCE(NULLIF(excluded.tagline_subtitle_font_size,''), store_plugins.tagline_subtitle_font_size),
                        readme_cache=COALESCE(NULLIF(excluded.readme_cache,''), store_plugins.readme_cache),
                        updated_at=NOW()
                """, (
                    pdata.get('identifier', ''),
                    pdata.get('name', ''),
                    pdata.get('name_i18n_key', ''),
                    pdata.get('description', ''),
                    pdata.get('version', '0.1.0'),
                    pdata.get('author', ''),
                    pdata.get('author_url', ''),
                    pdata.get('icon_url', ''),
                    pdata.get('price_type', 'free'),
                    pdata.get('price_amount', 0),
                    pdata.get('price_interval', 'onetime'),
                    pdata.get('price_quarter_fen', 0),
                    pdata.get('price_year_fen', 0),
                    json.dumps(pdata.get('compatible_editions', [])),
                    pdata.get('trial_days', 0),
                    pdata.get('download_url', ''),
                    pdata.get('package_hash', ''),
                    pdata.get('file_size', 0),
                    pdata.get('category', ''),
                    json.dumps(pdata.get('tags', [])),
                    pdata.get('min_app_version', '0.10.0'),
                    json.dumps(pdata.get('depends_on', {})),
                    json.dumps(pdata.get('screenshots', [])),
                    pdata.get('readme_url', ''),
                    pdata.get('tagline', ''),
                    pdata.get('tagline_i18n_key', ''),
                    pdata.get('tagline_font_size', '16px'),
                    pdata.get('tagline_color', '#ffffff'),
                    pdata.get('tagline_subtitle', ''),
                    pdata.get('tagline_subtitle_font_size', '14px'),
                    pdata.get('downloads', 0),
                    pdata.get('rating', 0.0),
                    pdata.get('review_count', 0),
                    pdata.get('readme_cache', ''),
                ))
                conn.commit()

    def sync_all(self) -> int:
        """从 GitHub Raw 同步全部插件目录到本地缓存

        Returns:
            同步的插件数量；-1 表示目录源拉取失败但本地已有缓存
            （保留缓存继续可用），0 表示拉取失败且本地无缓存（首次拉取）。
        """
        catalog = self._fetch_catalog()
        if not catalog:
            # 目录源拉取失败：保留本地缓存，向上层明确反馈失败状态
            cached = 0
            try:
                with get_registry_db() as conn:
                    cached = conn.execute(
                        'SELECT COUNT(*) AS cnt FROM store_plugins'
                    ).fetchone()['cnt']
            except Exception as e:
                print(f'[StoreAPIClient] sync_all: 查询本地缓存失败: {e}')
            if cached and cached > 0:
                print(f'[StoreAPIClient] sync_all: 目录拉取失败，保留本地缓存 {cached} 条 (return -1)')
                return -1
            print('[StoreAPIClient] sync_all: 目录拉取失败，且无本地缓存 (return 0)')
            return 0

        plugins_data = catalog.get('plugins', [])
        for pdata in plugins_data:
            self._upsert_cache(pdata)
        # P0-2：记录本次同步时间戳（供 /health 与管理页观测）
        try:
            with get_registry_db() as conn:
                conn.execute("UPDATE store_plugins SET last_sync_ts=NOW()::text")
                conn.commit()
        except Exception as e:
            print(f'[StoreAPIClient] sync_all: 更新 last_sync_ts 失败: {e}')
        return len(plugins_data)

    def check_updates(self, local_versions: dict) -> dict:
        """对比本地已安装版本与商店目录版本，返回各插件的更新状态。

        Args:
            local_versions: {identifier: installed_version}，
                            如 {'ads': '1.0.0'}（来源：plugin_registry）

        Returns:
            {identifier: {installed, latest, has_update, min_app_version}}
            仅包含"本地已安装 且 商店目录上架"的插件。
        """
        print(f'[StoreAPIClient] check_updates: 收到本地已安装插件 {len(local_versions)} 个: {local_versions}')
        if not local_versions:
            print('[StoreAPIClient] check_updates: local_versions 为空，无可对比项，直接返回空结果')
            return {}

        # 读取商店目录（本地缓存表 store_plugins，仅 enabled=1 上架项）
        try:
            with get_registry_db() as conn:
                rows = conn.execute(
                    'SELECT identifier, version, min_app_version FROM store_plugins WHERE enabled=1'
                ).fetchall()
        except Exception as e:
            print(f'[StoreAPIClient] check_updates: 查询 store_plugins 失败: {e}，返回空结果')
            return {}
        print(f'[StoreAPIClient] check_updates: 商店目录上架插件 {len(rows)} 条')

        result = {}
        matched = 0
        skipped = 0
        for r in rows:
            identifier = r['identifier']
            latest = r['version']
            installed = local_versions.get(identifier)
            if installed is None:
                skipped += 1
                print(f'[StoreAPIClient] check_updates: 跳过 {identifier} — 本地未安装（商店 v{latest}）')
                continue
            matched += 1

            # 解析版本号；任一非法版本退化为字符串比较
            latest_ver = parse_version(latest)
            installed_ver = parse_version(installed)
            if latest_ver is None or installed_ver is None:
                print(f'[StoreAPIClient] check_updates: ⚠️ {identifier} 版本号无法解析 '
                      f'(installed={installed!r}, latest={latest!r})，退化为字符串比较')
                has_update = latest != installed
            else:
                has_update = latest_ver > installed_ver

            print(f'[StoreAPIClient] check_updates: {identifier} '
                  f'installed={installed} latest={latest} has_update={has_update} '
                  f'min_app_version={r["min_app_version"]}')
            result[identifier] = {
                'installed': installed,
                'latest': latest,
                'has_update': has_update,
                'min_app_version': r['min_app_version'],
            }

        updatable = sum(1 for v in result.values() if v['has_update'])
        print(f'[StoreAPIClient] check_updates: 完成 — 匹配 {matched} 个已安装插件，跳过 {skipped} 个未安装，'
              f'其中可更新 {updatable} 个')
        return result


# ── 模块级单例 ──────────────────────────────────────────────────────

_STORE_CLIENT = None
_STORE_CLIENT_LOCK = threading.Lock()


def get_store_client() -> StoreAPIClient:
    global _STORE_CLIENT
    if _STORE_CLIENT is None:
        with _STORE_CLIENT_LOCK:
            if _STORE_CLIENT is None:
                _STORE_CLIENT = StoreAPIClient()
    return _STORE_CLIENT
