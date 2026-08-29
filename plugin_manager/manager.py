#!/usr/bin/env python3
"""
Plugin Manager — 插件管理器核心类
====================================
管理插件生命周期（5 状态）、发现、依赖解析、持久化。

生命周期:
    UNKNOWN → INSTALLED → ENABLED → ACTIVE → DISABLED → UNINSTALLED
                           ↑                         │
                           └─── ENABLED ←────────────┘
"""

import os
import sys
import json
import time
import shutil
import importlib
import importlib.util
import threading
import inspect
from contextlib import contextmanager
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime

from .models import (
    PluginInfo, PluginStatus,
    init_plugin_registry_table, get_registry_db,
)
from .discovery import PluginDiscovery, version_satisfies, parse_version
from .exceptions import (
    PluginNotFoundError, PluginNotInstalledError,
    PluginNotEnabledError, PluginDependencyError,
    PluginCircularDependencyError, PluginStateError,
    PluginVersionError, PluginBusyError,
)
from .hooks import HookRegistry, get_hook_registry
from .event_bus import EventBus, get_event_bus, EventName
from . import deps as deps_module
# config_validator 懒加载：避免 jsonschema→uuid→platform 导入链与项目 platform/ 目录冲突
_validate_config = None
_coerce_config = None

def _get_validators():
    global _validate_config, _coerce_config
    if _validate_config is None:
        from .config_validator import validate_config as vc, coerce_config as cc
        _validate_config = vc
        _coerce_config = cc
    return _validate_config, _coerce_config
from .logger import get_plugin_logger, init_plugin_logging
from .license import LicenseManager, get_license_manager
from .store import StoreAPIClient, get_store_client
from .watermark import OFFICIAL_PLUGIN_IDS
from .guard import CIRCUIT_BREAKER_THRESHOLD, should_trip, record_failure

# 敏感权限集合（软执行门卫，§10.2/§11.1）：声明即需管理员启用前审查
SENSITIVE_PERMISSIONS = {
    'network:request',
    'filesystem:read',
    'filesystem:write',
    'admin:access',
}

# 插件能力 → 所需 permissions（P0-1 运行时门控；官方插件自动豁免，第三方必须显式声明）
CAPABILITY_PERMISSIONS = {
    'register_routes': 'routes',
    'register_jobs': 'scheduler',
    'register_dag_nodes': 'dag',
    'register_health_checks': 'health',
    'get_event_handlers': 'events',
}

# 宿主共享运行模块：被插件 import 但本身不是插件（无 plugin.json），由基础系统提供。
# 安装/升级时用于静态校验，防止"安装即损坏"（D-INSTALL-01）。
SHARED_PLUGIN_MODULES = ('_base',)

# 锁获取超时（秒）：超时抛 PluginBusyError，避免单个异常操作挂起全部写操作
LOCK_ACQUIRE_TIMEOUT = 30


def _dag_handler_conforms(handler) -> bool:
    """判断 DAG 节点处理器能否被引擎以 handler(node_def, input_data) 调用。

    WorkflowEngine 对已注册节点统一按两个位置参数调用（见 orchestrator/workflow_engine.py
    _execute_node）。签名不匹配（如单参数 params 旧式处理器）跳过并告警，
    避免运行时 TypeError 中断整个工作流。
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return True  # 内建/动态对象无法检查签名 → 按约定注册
    positional = [p for p in sig.parameters.values()
                  if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL
                      for p in sig.parameters.values())
    return has_varargs or len(positional) >= 2


class PluginManager:
    """插件管理器核心类"""

    def __init__(self, app=None, hook_registry: HookRegistry = None,
                 event_bus: EventBus = None):
        self.app = app
        self.plugins_dir = None
        self._discovery = PluginDiscovery()
        # 可重入锁：uninstall→disable 等嵌套调用安全；配合超时避免全局挂起（D-LOCK-01）
        self._lock = threading.RLock()

        # 运行时缓存: {identifier: PluginInfo}
        self._cache: Dict[str, PluginInfo] = {}

        # 运行时实例: {identifier: instance}
        self._instances: Dict[str, Any] = {}

        # 钩子系统 & 事件总线
        self._hook_registry = hook_registry or get_hook_registry()
        self._event_bus = event_bus or get_event_bus()

        # License & 商店
        self._license_mgr: Optional[LicenseManager] = None
        self._store_client: Optional[StoreAPIClient] = None
        # 商店目录最近一次同步时间戳（懒刷新 TTL 依据）
        self._last_store_sync_ts = 0.0

        if app is not None:
            self.init_app(app)

    @contextmanager
    def _lock_timeout(self, timeout: float = LOCK_ACQUIRE_TIMEOUT):
        """带超时的全局写锁（D-LOCK-01）。

        - RLock 保证 uninstall→disable 等嵌套调用不互相死锁
        - 超时抛 PluginBusyError，使单个异常操作快速失败而非挂起全局写操作
        - finally 确保异常路径也释放锁
        """
        acquired = self._lock.acquire(timeout=timeout)
        if not acquired:
            raise PluginBusyError(f'插件系统正忙（{timeout} 秒内未能获得写锁），请稍后重试')
        try:
            yield
        finally:
            self._lock.release()

    # ── 初始化 ──────────────────────────────────────────────────────────

    def init_app(self, app):
        """工厂模式初始化，绑定到 Flask 应用

        调用时机: app 创建后，第一个请求前调用一次。
        """
        self.app = app

        # 确定插件目录
        plugins_dir = getattr(app, 'plugins_dir', None) or \
            os.path.join(app.root_path, 'plugins')
        self.plugins_dir = os.path.abspath(plugins_dir)
        self._discovery.set_plugins_dir(self.plugins_dir)

        # 初始化数据库表
        init_plugin_registry_table()

        # 初始化日志系统
        init_plugin_logging()

        # 初始化 License & Store 表
        from .models_store import init_license_store_tables
        init_license_store_tables()

        # License & Store 客户端（延迟初始化）
        self._license_mgr = get_license_manager()
        self._store_client = get_store_client()

        # ── 商店目录循环同步（P0-2：多源回退 + 指数退避 + 定时刷新）──
        # 启动立即同步一次；成功每 SYNC_SUCCESS_INTERVAL 刷新，
        # 失败按指数退避重试（15min 起、上限 6h），不阻塞 app 启动。
        store_client = self._store_client
        def _sync_loop():
            """商店目录循环同步（P0-2）。

            跨 worker 互斥：仅持 PG advisory lock 的进程执行同步，其余 worker
            每 10 分钟重试接管，避免多 worker 并发重复拉取消耗 GitHub 配额；
            持锁进程崩溃时连接断开自动释放锁，由其他 worker 接管（退避不受影响）。
            """
            import psycopg2
            lock_conn = None
            while lock_conn is None:
                try:
                    lock_conn = psycopg2.connect(
                        host=os.environ.get('PG_HOST', 'localhost'),
                        port=os.environ.get('PG_PORT', '5432'),
                        dbname=os.environ.get('PG_DB', 'appdb'),
                        user=os.environ.get('PG_USER', 'app'),
                        password=os.environ.get('PG_PASSWORD', ''),
                    )
                    cur = lock_conn.cursor()
                    cur.execute('SELECT pg_try_advisory_lock(%s)', (775219,))
                    acquired = bool(cur.fetchone()[0])
                    cur.close()
                    if not acquired:
                        lock_conn.close()
                        lock_conn = None
                except Exception as e:
                    if lock_conn is not None:
                        try:
                            lock_conn.close()
                        except Exception:
                            pass
                        lock_conn = None
                    print(f'[PluginManager] ⚠️ store sync leader election failed: {e}')
                if lock_conn is None:
                    time.sleep(600)  # 未获锁：10 分钟后重试接管
            try:
                while True:
                    try:
                        count = store_client.sync_all()
                        if count > 0:
                            store_client.record_success()
                            self._last_store_sync_ts = time.time()
                            print(f'[PluginManager] Store catalog synced: {count} plugins')
                        else:
                            store_client.record_failure()
                            print(f'[PluginManager] ⚠️ Store catalog sync failed (count={count})')
                    except Exception as e:
                        store_client.record_failure()
                        print(f'[PluginManager] Store sync failed: {e}')
                    try:
                        time.sleep(store_client.next_sync_interval())
                    except KeyboardInterrupt:
                        return
            finally:
                try:
                    lock_conn.close()  # 关闭连接 → advisory lock 自动释放
                except Exception:
                    pass
        threading.Thread(target=_sync_loop, daemon=True, name='store-sync-loop').start()

        # ── 进程内看门狗（P0-4 L3：内存超限告警，仅 Linux）──────────
        # 进程内无法精确归因到单个插件，仅告警不自动禁用；建议结合重启恢复。
        def _watchdog_loop():
            try:
                import resource
            except ImportError:
                return  # 非 Linux（如本机 Windows 无 resource 模块）跳过
            mem_mb_limit = int(os.environ.get('PLUGIN_WATCHDOG_MEM_MB', '1024'))
            while True:
                try:
                    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
                    if rss_mb > mem_mb_limit:
                        print(f'[Watchdog] ⚠️ 进程 RSS {rss_mb:.0f}MB 超限 {mem_mb_limit}MB，'
                              f'可能由插件内存泄漏导致，建议重启')
                except Exception:
                    pass
                try:
                    time.sleep(60)
                except KeyboardInterrupt:
                    return
        threading.Thread(target=_watchdog_loop, daemon=True, name='plugin-watchdog').start()

        # 从数据库加载已注册插件到缓存
        self._load_cache()

        # 自动安装新发现的插件
        # PLUGIN_AUTO_INSTALL=0 时跳过自动安装/自动启用（部署默认关，插件由后台手动安装启用）
        auto_install = os.environ.get('PLUGIN_AUTO_INSTALL', '1').strip().lower() not in ('0', 'false', 'no')
        try:
            discovered = self._discovery.discover()
            if auto_install:
                auto_installed = 0
                for info in discovered:
                    if info.identifier not in self._cache:
                        self.install(info.identifier)
                        auto_installed += 1
                if auto_installed > 0:
                    print(f'[PluginManager] ✅ 自动安装 {auto_installed} 个新插件')
                    self._load_cache()  # 重新加载缓存

                # 自动启用所有 INSTALLED 状态的插件（新安装 + 已安装未启用）
                auto_enabled = 0
                for info in discovered:
                    cached = self._cache.get(info.identifier)
                    if cached and cached.status == PluginStatus.INSTALLED:
                        try:
                            self.enable(info.identifier)
                            auto_enabled += 1
                        except Exception as e:
                            print(f'[PluginManager] ⚠️ auto-enable {info.identifier}: {e}')
                if auto_enabled > 0:
                    print(f'[PluginManager] ✅ 自动启用 {auto_enabled} 个插件')
                    self._load_cache()

            # 用磁盘 plugin.json 刷新已缓存插件的静态元信息（menu/version 等）。
            # 当磁盘版本与数据库不一致时同步写回 DB，确保插件管理器展示最新版本号。
            for disk_info in discovered:
                cached = self._cache.get(disk_info.identifier)
                if cached:
                    needs_db_sync = (
                        cached.version != disk_info.version or
                        cached.metadata.get('version') != disk_info.metadata.get('version') or
                        cached.name != disk_info.name or
                        cached.min_app_version != disk_info.min_app_version
                    )
                    cached.metadata = disk_info.metadata
                    cached.version = disk_info.version
                    cached.name = disk_info.name
                    cached.min_app_version = disk_info.min_app_version
                    if needs_db_sync:
                        self._save_to_db(cached)
                        print(f'[PluginManager] 🔄 {disk_info.identifier}: synced v{disk_info.version} to DB')

            # ── 加载所有插件的 locale 翻译 ─────────────────────
            try:
                from i18n import seed_plugin_translations
                for disk_info in discovered:
                    locale_dir = os.path.join(disk_info.path, 'i18n')
                    if os.path.isdir(locale_dir):
                        seed_plugin_translations(disk_info.identifier, locale_dir)
            except Exception as e:
                print(f'[PluginManager] ⚠️ 加载插件翻译失败: {e}')
        except Exception as e:
            print(f'[PluginManager] ⚠️ 自动安装失败: {e}')

        # 自动禁用已被替代的弃用插件（如旧 dev_accounts）
        try:
            self._auto_disable_deprecated()
        except Exception as e:
            print(f'[PluginManager] ⚠️ 自动禁用弃用插件失败: {e}')

        # ── 预注册已启用/激活插件的蓝图与钩子 ──────────────────
        # Flask 不允许 app 处理首个请求后调用 register_blueprint，
        # 因此必须在此（app 首个请求前）一次性挂载所有 ENABLED/ACTIVE 插件的路由。
        self._preload_routes()

        # 记录到 app 扩展
        if not hasattr(app, 'extensions'):
            app.extensions = {}
        app.extensions['plugin_manager'] = self

        print(f'[PluginManager] ✅ 已初始化 (plugins: {self.plugins_dir}, '
              f'cached: {len(self._cache)})')

    # ── 预注册 ───────────────────────────────────────────────────────────

    def _capability_allowed(self, info: PluginInfo, capability: str) -> bool:
        """能力注册权限门控（P0-1）。

        官方插件（OFFICIAL_PLUGIN_IDS）自动豁免，保持现状不受影响；
        第三方插件必须在其 plugin.json permissions 中声明对应能力所需权限，
        否则跳过该能力注册并降级（记日志、不崩溃、不影响其他插件）。
        """
        if info.identifier in OFFICIAL_PLUGIN_IDS:
            return True
        required = CAPABILITY_PERMISSIONS.get(capability)
        if not required:
            return True
        declared = set(getattr(info, 'permissions', None) or [])
        if required in declared:
            return True
        print(f'[PluginManager] ⚠️ {info.identifier}: 未声明权限 "{required}"，'
              f'跳过能力 {capability}（最小权限降级）')
        # P0-1 审核修复：降级落库（last_error 持久化），管理页可见，避免静默功能缺失
        try:
            reason = f'capability {capability} blocked: missing permission "{required}"'
            info.last_error = ((info.last_error or '') + '; ' + reason
                               if info.last_error else reason)
            with get_registry_db() as conn:
                conn.execute(
                    'UPDATE plugin_registry SET last_error=%s WHERE identifier=%s',
                    (info.last_error, info.identifier))
                conn.commit()
        except Exception as e:
            print(f'[PluginManager] ⚠️ {info.identifier}: persist degradation notice failed: {e}')
        return False

    # ── P0-4 运行时故障隔离 ─────────────────────────────────────────

    def _guard_failure(self, info: PluginInfo, context: str):
        """记录插件一次失败（熔断计数）并检查是否触发熔断。"""
        record_failure(info.identifier)
        self._maybe_trip(info, context)

    def _maybe_trip(self, info: PluginInfo, context: str):
        """熔断检查：连续失败达到阈值 → 自动禁用插件。

        置 ERROR + 落库 last_error（管理页可见根因）+ 卸载实例与钩子。
        官方插件由 guard 豁免，不计入熔断。
        """
        if not should_trip(info.identifier):
            return
        if info.status == PluginStatus.ERROR:
            return
        reason = f'circuit breaker: 连续失败 ≥ {CIRCUIT_BREAKER_THRESHOLD} 次 ({context})'
        info.last_error = reason
        info.status = PluginStatus.ERROR
        info.updated_at = datetime.now().isoformat()
        try:
            self._save_to_db(info)
            self._instances.pop(info.identifier, None)
            if self._hook_registry:
                try:
                    self._hook_registry.remove_all(info.identifier)
                except Exception:
                    pass
        finally:
            print(f'[PluginManager] 🧯 {info.identifier} 熔断自动禁用: {reason}')

    def _preload_routes(self):
        """启动时预注册 DB 中 ENABLED/ACTIVE 插件的蓝图与钩子（幂等）。

        背景: Flask 的 register_blueprint 在 app 处理首个请求后不可用，
        因此插件路由必须在 init_app 阶段（首个请求前）静态挂载。
        运行时 enable()/activate() 只做状态/钩子/任务挂载，
        新启用插件的路由在下次服务重启后生效。
        """
        if self.app is None:
            return
        try:
            with get_registry_db() as conn:
                rows = conn.execute(
                    "SELECT identifier FROM plugin_registry "
                    "WHERE status IN ('enabled','active') ORDER BY identifier"
                ).fetchall()
        except Exception as e:
            print(f'[PluginManager] ⚠️ _preload_routes db query failed: {e}')
            return

        for row in rows:
            pid = row['identifier']
            info = self._cache.get(pid)
            if info is None:
                continue
            # 已加载实例直接复用；否则按启用流程加载（setup）
            instance = self._instances.get(pid)
            if instance is None:
                try:
                    instance = self._load_instance(info)
                    if hasattr(instance, 'setup') and callable(instance.setup):
                        instance.setup()
                    self._instances[pid] = instance
                except SystemExit as e:
                    self._guard_failure(info, 'preload-setup')
                    print(f'[PluginManager] ⚠️ {pid}: preload setup SystemExit: {e}')
                    continue
                except Exception as e:
                    self._guard_failure(info, 'preload-setup')
                    print(f'[PluginManager] ⚠️ {pid}: preload instance failed: {e}')
                    continue
            # ACTIVE 插件重启后恢复运行时订阅（事件监听/过滤器/后台任务）。
            # _preload_routes 每进程启动仅执行一次且实例已缓存（_instances），
            # 补调 activate() 是幂等的；否则 memory_engine 等依赖 activate()
            # 注册 AGENT_TASK_COMPLETED 监听的插件在服务重启后事件订阅丢失。
            if info.status == PluginStatus.ACTIVE and hasattr(instance, 'activate'):
                try:
                    instance.activate()
                except SystemExit as e:
                    self._guard_failure(info, 'preload-activate')
                    print(f'[PluginManager] ⚠️ {pid}: preload activate SystemExit: {e}')
                except Exception as e:
                    self._guard_failure(info, 'preload-activate')
                    print(f'[PluginManager] ⚠️ {pid}: preload activate failed: {e}')
            # 启动时幂等补挂载统一网关注册（插件标准 §4）：ACTIVE 插件不重复
            # 走 enable()，此处确保重启后插件能力仍聚合在所选核心角色。
            if info.status == PluginStatus.ACTIVE:
                try:
                    from agent_matrix.models import attach_plugin_capabilities
                    attach_plugin_capabilities(pid, info.metadata or {})
                except Exception as e:
                    print(f'[PluginManager] ⚠️ {pid}: 启动网关注册补挂载失败: {e}')
            try:
                # 注册路由（如插件提供 Blueprint；P0-1 权限门控）
                if self._capability_allowed(info, 'register_routes') and hasattr(instance, 'register_routes'):
                    for bp in instance.register_routes():
                        if bp.name in self.app.blueprints:
                            continue
                        prefix = self._get_route_prefix(pid, bp)
                        # 与 mount_all_routes 一致：插件不得抢占 admin 核心根前缀
                        if prefix in ('', '/', '/admin', '/admin/'):
                            print(f'[PluginManager] ⚠️ {pid}: 插件前缀 {prefix!r} 抢占 admin 核心域，跳过预挂载')
                            continue
                        self.app.register_blueprint(bp, url_prefix=prefix)
                        print(f'[PluginManager] {pid}: preloaded {prefix}')
                # 注册钩子（P0-1 权限门控）
                if self._hook_registry and self._capability_allowed(info, 'get_event_handlers') and hasattr(instance, 'get_event_handlers'):
                    for event, handler in instance.get_event_handlers().items():
                        self._hook_registry.add_action(event, handler, identifier=pid)
                # ENABLED 状态下预注册成功 → 提升为 ACTIVE
                if info.status == PluginStatus.ENABLED:
                    info.status = PluginStatus.ACTIVE
                    info.updated_at = datetime.now().isoformat()
                    self._save_to_db(info)
                    print(f'[PluginManager] ✅ {pid} active (preloaded)')
            except SystemExit as e:
                self._guard_failure(info, 'preload')
                print(f'[PluginManager] ⚠️ {pid}: preload SystemExit: {e}')
            except Exception as e:
                self._guard_failure(info, 'preload')
                print(f'[PluginManager] ⚠️ {pid}: preload warning: {e}')

    # ── 发现 ────────────────────────────────────────────────────────────

    def discover(self) -> List[PluginInfo]:
        """扫描 plugins/ 目录，返回新发现的插件列表"""
        discovered = self._discovery.discover()
        # 过滤出尚未注册的
        new_plugins = [p for p in discovered if p.identifier not in self._cache]
        return new_plugins

    def discover_all(self) -> List[PluginInfo]:
        """扫描并返回所有插件（含已注册的）"""
        return self._discovery.discover()

    # ── 安装 ────────────────────────────────────────────────────────────

    def install(self, identifier: str) -> PluginInfo:
        """安装插件: 写入 registry 持久化, 状态 → INSTALLED"""
        with self._lock_timeout():
            if identifier in self._cache:
                info = self._cache[identifier]
                if info.status in (PluginStatus.INSTALLED, PluginStatus.ENABLED,
                                   PluginStatus.ACTIVE):
                    print(f'[PluginManager] {identifier} 已安装，跳过')
                    return info

            # 从磁盘扫描
            info = self._discovery.discover_one(identifier)
            if info is None:
                raise PluginNotFoundError(identifier)

            # 宿主共享模块静态校验（D-INSTALL-01）
            self._validate_shared_imports(info.path, identifier)

            info.status = PluginStatus.INSTALLED
            info.installed_at = datetime.now().isoformat()
            info.updated_at = datetime.now().isoformat()

            # 持久化到数据库
            self._save_to_db(info)
            self._cache[identifier] = info

            # 触发事件
            self._emit('plugin.installed', plugin_id=identifier)

            print(f'[PluginManager] ✅ {identifier} v{info.version} installed')
            return info

    # ── 启用 ────────────────────────────────────────────────────────────

    def enable(self, identifier: str) -> PluginInfo:
        """启用插件: 检查依赖 + 执行 setup(), 状态 → ENABLED"""
        with self._lock_timeout():
            # VR-PLG-002：以 DB 状态为权威，避免多 worker 下内存缓存滞后
            # 导致「disable 后 enable 报 cannot transition from active」。
            self.refresh_status_from_db()
            info = self._get_cached(identifier)

            # 幂等：已是 ENABLED/ACTIVE 直接返回当前状态（D-IDEMPOTENT-01）
            if info.status in (PluginStatus.ENABLED, PluginStatus.ACTIVE):
                print(f'[PluginManager] {identifier} 已启用，跳过')
                return info

            # 验证状态转换
            if not info.status.can_transition_to(PluginStatus.ENABLED):
                raise PluginStateError(identifier, info.status.value, 'enabled')

            # 解析依赖
            deps = info.dependencies
            if deps:
                self._resolve_dependencies(identifier, deps)

            # 检查最低应用版本
            if info.min_app_version and hasattr(self.app, 'version'):
                if not version_satisfies(self.app.version, f'>={info.min_app_version}'):
                    raise PluginVersionError(
                        identifier, info.min_app_version,
                        getattr(self.app, 'version', '?')
                    )

            # ── License 检查 ───────────────────────────────────────
            # 付费插件必须有有效 License 才能启用
            if self._license_mgr and self._license_mgr.is_paid_plugin(identifier):
                lic_result = self._license_mgr.validate(identifier)
                if not lic_result.get('valid'):
                    info.last_error = f'License required: {lic_result.get("error", "unlicensed")}'
                    info.status = PluginStatus.ERROR
                    self._save_to_db(info)
                    raise PluginStateError(
                        identifier, 'unlicensed',
                        f'enable failed: {lic_result.get("error", "no license")}'
                    )
                print(f'[PluginManager] {identifier}: license valid ({lic_result.get("status")})')

            # ── 官方签名包完整性校验（VeroRun 水印体系 · 批次3）──
            # 仅校验携带 verorun.manifest 的官方签名包；无清单的旧包放行（向后兼容）。
            # 二次打包/篡改的官方插件 → 完整性校验失败 → 拒绝启用。
            try:
                from .watermark import verify_manifest
                _vm = verify_manifest(info.path)
                if _vm.get('checked') and not _vm.get('valid'):
                    _mism = ', '.join(_vm.get('mismatched', [])[:5])
                    info.last_error = f'Integrity check failed: modified files: {_mism}'
                    info.status = PluginStatus.ERROR
                    self._save_to_db(info)
                    raise PluginStateError(
                        identifier, 'integrity_failed',
                        f'enable failed: official plugin package modified: {_mism}'
                    )
            except PluginStateError:
                raise
            except Exception as _e:
                print(f'[PluginManager] ⚠️ {identifier}: integrity check skipped: {_e}')

            # ── 统一网关注册强制校验（插件标准 §2.2/§4）────────────
            # 官方插件必须声明 agent_role（核心角色之一），否则拒绝启用。
            # 核心角色集由 agent_matrix/roles/*.yaml 动态推导（单一事实源）。
            try:
                from agent_matrix.models import get_core_role_slugs
                _core_roles = get_core_role_slugs()
            except ImportError:
                _core_roles = []
            if (info.metadata or {}).get('agent_role') not in _core_roles:
                info.last_error = ('missing/invalid agent_role: '
                                   f'{(info.metadata or {}).get("agent_role")!r} '
                                   '（须为核心角色之一）')
                info.status = PluginStatus.ERROR
                self._save_to_db(info)
                raise PluginStateError(
                    identifier, 'missing_agent_role',
                    'enable failed: plugin.json 必须声明 agent_role（核心角色之一）'
                )

            # 执行插件 setup()
            # 降级策略：setup() 失败不置 ERROR、不抛异常（如 chatbot 在运行期
            # 调 register_blueprint 会被 Flask 拒绝），保持 ENABLED 并记录
            # last_error；插件路由由启动时 _preload_routes() 预注册，重启后生效。
            try:
                instance = self._load_instance(info)
                if hasattr(instance, 'setup') and callable(instance.setup):
                    setup_result = instance.setup()
                    if setup_result is False:
                        raise RuntimeError('setup() returned False')

                self._instances[identifier] = instance
                info.last_error = None
            except SystemExit as e:
                self._guard_failure(info, 'setup')
                print(f'[PluginManager] ⚠️ {identifier} setup SystemExit: {e}')
            except Exception as e:
                self._guard_failure(info, 'setup')
                info.last_error = f'setup error: {e}'
                self._save_to_db(info)
                print(f'[PluginManager] ⚠️ {identifier} setup degraded (ENABLED, restart to load routes): {e}')

            info.status = PluginStatus.ENABLED
            info.updated_at = datetime.now().isoformat()
            self._save_to_db(info)

            self._emit('plugin.enabled', plugin_id=identifier)
            print(f'[PluginManager] ✅ {identifier} enabled')

            # ── 统一网关注册（§4）：插件能力聚合到所选核心角色 ──────
            _meta = info.metadata or {}
            try:
                from agent_matrix.models import attach_plugin_capabilities
                attach_plugin_capabilities(identifier, _meta)
            except ImportError as e:
                print(f'[PluginManager] ⚠️ {identifier}: agent_matrix.models 不可用, 跳过网关注册 ({e})')
            except Exception as e:
                print(f'[PluginManager] ⚠️ {identifier}: 网关注册失败 ({type(e).__name__}: {e})')

            # ── 敏感权限软检查（软执行：仅警告，不阻断）────────────
            sensitive = [p for p in (_meta.get('permissions') or [])
                         if p in SENSITIVE_PERMISSIONS]
            if sensitive:
                print(f'[PluginManager] ⚠️ {identifier} 声明敏感权限 {sensitive}，'
                      f'请管理员启用前审查（软执行：仅提醒）')

            # ── 自动激活: enable 后立即挂载钩子/任务 ────────────
            # Flask 不允许 app 处理首个请求后动态注册蓝图，因此不再在此 register_blueprint；
            # 插件路由统一由启动时 _preload_routes() 预注册，新启用插件的路由在重启后生效。
            try:
                instance = self._instances.get(identifier)
                if instance:
                    if hasattr(instance, 'activate') and callable(instance.activate):
                        instance.activate()

                    # 注册钩子（如果启用了钩子系统；P0-1 权限门控）
                    if self._hook_registry and self._capability_allowed(info, 'get_event_handlers') and hasattr(instance, 'get_event_handlers'):
                        handlers = instance.get_event_handlers()
                        for event, handler in handlers.items():
                            self._hook_registry.add_action(event, handler, identifier=identifier)

                    info.status = PluginStatus.ACTIVE
                    info.updated_at = datetime.now().isoformat()
                    self._save_to_db(info)
                    print(f'[PluginManager] ✅ {identifier} active (auto)')
            except SystemExit as e:
                self._guard_failure(info, 'auto-activate')
                print(f'[PluginManager] ⚠️ {identifier} auto-activate SystemExit: {e}')
            except Exception as e:
                self._guard_failure(info, 'auto-activate')
                import traceback
                traceback.print_exc()
                print(f'[PluginManager] ⚠️ {identifier} auto-activate warning: {e}')

            return info

    # ── 激活 ────────────────────────────────────────────────────────────

    def activate(self, identifier: str) -> PluginInfo:
        """激活插件: 加载模块 + 注册路由/钩子, 状态 → ACTIVE"""
        with self._lock_timeout():
            # VR-PLG-002：以 DB 状态为权威，避免多 worker 下内存缓存滞后
            self.refresh_status_from_db()
            info = self._get_cached(identifier)

            if not info.status.can_transition_to(PluginStatus.ACTIVE):
                raise PluginStateError(identifier, info.status.value, 'active')

            # 检查依赖是否都已激活
            if identifier in self._instances:
                self._check_deps_active(info)

            instance = self._instances.get(identifier)
            if instance is None:
                # 修复：enable() 瞬时 setup 失败降级后实例未缓存，此处补载重试，
                # 避免"已启用但点激活必 400"；重试仍失败则记录后正常抛错。
                try:
                    instance = self._load_instance(info)
                    if hasattr(instance, 'setup') and callable(instance.setup):
                        instance.setup()
                    self._instances[identifier] = instance
                except SystemExit as e:
                    self._guard_failure(info, 'activate-selfheal')
                    print(f'[PluginManager] ⚠️ {identifier} activate self-heal SystemExit: {e}')
                except Exception as e:
                    self._guard_failure(info, 'activate-selfheal')
                    info.last_error = f'activate self-heal failed: {e}'
                    self._save_to_db(info)
                    print(f'[PluginManager] ⚠️ {identifier} activate self-heal failed: {e}')
            instance = self._instances.get(identifier)
            if instance is None:
                raise PluginNotEnabledError(identifier)

            # 执行 activate()
            try:
                if hasattr(instance, 'activate') and callable(instance.activate):
                    instance.activate()

                # 注册钩子（如果启用了钩子系统；P0-1 权限门控）
                # 注意: Flask 不允许 app 处理首个请求后动态注册蓝图，
                # 插件路由统一由启动时 _preload_routes() 预注册。
                if self._hook_registry and self._capability_allowed(info, 'get_event_handlers') and hasattr(instance, 'get_event_handlers'):
                    handlers = instance.get_event_handlers()
                    for event, handler in handlers.items():
                        self._hook_registry.add_action(event, handler, identifier=identifier)

            except SystemExit as e:
                self._guard_failure(info, 'activate')
                info.last_error = f'activate SystemExit: {e}'
                info.status = PluginStatus.ERROR
                self._save_to_db(info)
                print(f'[PluginManager] ❌ {identifier} activate SystemExit: {e}')
                raise
            except Exception as e:
                self._guard_failure(info, 'activate')
                info.last_error = f'activate error: {e}'
                info.status = PluginStatus.ERROR
                self._save_to_db(info)
                import traceback
                traceback.print_exc()
                print(f'[PluginManager] ❌ {identifier} activate failed: {e}')
                raise

            info.status = PluginStatus.ACTIVE
            info.updated_at = datetime.now().isoformat()
            self._save_to_db(info)

            print(f'[PluginManager] ✅ {identifier} active')
            return info

    # ── 禁用 ────────────────────────────────────────────────────────────

    def disable(self, identifier: str) -> PluginInfo:
        """禁用插件: 反注册路由/钩子, 状态 → DISABLED"""
        with self._lock_timeout():
            # VR-PLG-002：以 DB 状态为权威，避免多 worker 下内存缓存滞后
            self.refresh_status_from_db()
            info = self._get_cached(identifier)

            if not info.status.can_transition_to(PluginStatus.DISABLED):
                raise PluginStateError(identifier, info.status.value, 'disabled')

            # 通知依赖本插件的插件
            self._notify_dependents(identifier, 'disable')

            # 执行 deactivate()
            instance = self._instances.pop(identifier, None)
            if instance:
                try:
                    if hasattr(instance, 'deactivate') and callable(instance.deactivate):
                        instance.deactivate()

                    # 移除路由（Phase 2 完善）
                    if self.app and hasattr(instance, 'register_routes'):
                        bps = instance.register_routes()
                        for bp in bps:
                            self._unregister_blueprint(bp)
                except Exception as e:
                    print(f'[PluginManager] {identifier} deactivate warning: {e}')

            info.status = PluginStatus.DISABLED
            info.updated_at = datetime.now().isoformat()
            self._save_to_db(info)

            self._emit('plugin.disabled', plugin_id=identifier)
            print(f'[PluginManager] ✅ {identifier} disabled')

            # ── 注销插件 Agent（§4）：从核心角色移除聚合能力 ─────
            _meta = info.metadata or {}
            try:
                from agent_matrix.models import detach_plugin_capabilities
                detach_plugin_capabilities(identifier, _meta)
            except ImportError as e:
                print(f'[PluginManager] ⚠️ {identifier}: agent_matrix.models 不可用, 跳过 Agent 清理 ({e})')

            return info

    # ── 卸载 ────────────────────────────────────────────────────────────

    def uninstall(self, identifier: str) -> None:
        """卸载插件: 禁用 + 清理 + 移除 registry 记录"""
        with self._lock_timeout():
            info = self._get_cached(identifier)
            orig_status = info.status
            orig_last_error = info.last_error

            try:
                # 如果处于 ACTIVE 或 ENABLED，先禁用
                if info.status in (PluginStatus.ACTIVE, PluginStatus.ENABLED):
                    self.disable(identifier)

                # 执行 on_uninstall（如果插件有 cleanup）
                instance = self._instances.pop(identifier, None)
                if instance and hasattr(instance, 'on_uninstall'):
                    try:
                        # 兼容两种签名：on_uninstall(registry) 与 on_uninstall()。
                        # 历史版本 manager 曾无参调用，导致含 registry 参数的插件
                        # 抛 TypeError 被吞，DROP SCHEMA 从未执行（数据残留）。
                        _sig = inspect.signature(instance.on_uninstall)
                        if 'registry' in _sig.parameters:
                            instance.on_uninstall(info)
                        else:
                            instance.on_uninstall()
                    except Exception as e:
                        print(f'[PluginManager] {identifier} uninstall warning: {e}')

                # 兜底：注销插件声明的 Agent（即使插件从未 enable 过）
                _meta = info.metadata or {}
                try:
                    from agent_matrix.models import detach_plugin_capabilities
                    detach_plugin_capabilities(identifier, _meta)
                except ImportError:
                    pass

                # 从数据库中移除记录
                self._delete_from_db(identifier)

                # 从缓存中移除
                self._cache.pop(identifier, None)
            except Exception:
                # ── 失败回滚：恢复 DB 状态与缓存（D-UNINSTALL-01）──
                try:
                    info.status = orig_status
                    info.last_error = orig_last_error
                    info.updated_at = datetime.now().isoformat()
                    self._save_to_db(info)
                    self._cache[identifier] = info
                    print(f'[PluginManager] 🔁 {identifier} 卸载失败，已恢复状态 {orig_status.value}')
                except Exception as e:
                    print(f'[PluginManager] ⚠️ {identifier} 卸载回滚持久化失败: {e}')
                import traceback
                traceback.print_exc()
                raise

            self._emit('plugin.uninstalled', plugin_id=identifier)
            print(f'[PluginManager] ✅ {identifier} uninstalled')

    def _validate_shared_imports(self, plugin_dir: str, identifier: str):
        """静态校验插件对宿主共享模块的依赖（D-INSTALL-01）。

        插件源码中出现 ``plugins.<shared>`` import 时，宿主必须真实提供对应
        目录（如 plugins/_base），否则安装/升级直接拒绝并给出明确错误，
        避免"安装即损坏"（ImportError 导致半启用状态残留）。
        """
        referenced = set()
        for root, _dirs, files in os.walk(plugin_dir):
            for fn in files:
                if not fn.endswith('.py'):
                    continue
                path = os.path.join(root, fn)
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        text = f.read()
                except OSError:
                    continue
                for shared in SHARED_PLUGIN_MODULES:
                    if f'plugins.{shared}' in text:
                        referenced.add(shared)

        if not referenced:
            return

        plugins_root = os.path.dirname(os.path.abspath(plugin_dir))
        missing = [
            f'plugins/{shared}'
            for shared in referenced
            if not os.path.isdir(os.path.join(plugins_root, shared))
        ]
        if missing:
            raise PluginDependencyError(
                identifier,
                [f'依赖宿主共享模块缺失: {m}（该模块由 VeroRun 基础系统提供）' for m in missing])

    # ── 在线升级 ─────────────────────────────────────────────────────────

    def upgrade(self, identifier: str) -> dict:
        """从商店升级插件到最新版本（在线更新）。

        流程：版本/兼容/License 校验 → 下载+SHA256 → 解压 staging →
        校验 plugin.json → 备份旧目录 → 原子替换 → 更新 registry →
        清理旧备份（保留最近 N=3 份）。任何一步失败自动回滚。

        Returns:
            {'identifier', 'old_version', 'new_version', 'needs_restart'}

        Raises:
            PluginNotFoundError: 插件未安装
            PluginStateError: 状态不允许升级 / License 无效
            PluginVersionError: 目标版本不高于当前版本 / min_app_version 不满足
            ValueError: 商店无更新包 / 包内 identifier 不一致 / 包损坏
        """
        with self._lock_timeout():
            info = self._get_cached(identifier)
            if info.status not in (PluginStatus.INSTALLED, PluginStatus.ENABLED,
                                   PluginStatus.ACTIVE):
                raise PluginStateError(identifier, info.status.value, 'upgrade')

            # 商店目标信息
            if not self._store_client:
                raise PluginStateError(identifier, 'store', 'store client not available')
            detail = self._store_client.get_detail(identifier)
            if not detail or not detail.get('download_url'):
                raise ValueError(f'商店中不存在 {identifier} 的更新包')
            latest = str(detail.get('version') or '').strip()
            old_version = str(info.version or '').strip()
            if not latest:
                raise ValueError(f'商店未提供 {identifier} 的目标版本号')

            # 版本比较：拒绝降级/同版本
            latest_ver = parse_version(latest)
            installed_ver = parse_version(old_version)
            if latest_ver is not None and installed_ver is not None:
                if latest_ver <= installed_ver:
                    raise PluginVersionError(identifier, f'>{old_version}', latest)
            elif latest == old_version:
                raise PluginVersionError(identifier, f'>{old_version}', latest)

            # min_app_version 兼容校验
            min_app = str(detail.get('min_app_version') or '')
            if min_app and hasattr(self.app, 'version'):
                if not version_satisfies(self.app.version, f'>={min_app}'):
                    raise PluginVersionError(
                        identifier, min_app, getattr(self.app, 'version', '?'))

            # License 校验（付费插件）
            if self._license_mgr and self._license_mgr.is_paid_plugin(identifier):
                lic_result = self._license_mgr.validate(identifier)
                if not lic_result.get('valid'):
                    raise PluginStateError(
                        identifier, 'unlicensed',
                        f'upgrade failed: {lic_result.get("error", "no license")}')

            plugin_dir = os.path.join(self.plugins_dir, identifier)
            staging_dir = os.path.join(self.plugins_dir, '.staging', identifier)
            backup_root = os.path.join(self.plugins_dir, '.backup', identifier)
            backup_dir = os.path.join(
                backup_root, f'v{old_version}-{time.strftime("%Y%m%d%H%M%S")}')
            swapped = False

        try:
            # ── 无锁阶段：网络下载 + SHA256 校验 + 解压到 staging（D-LOCK-01）──
            # 下载/解压只写私有 staging 目录，不持全局锁，避免单插件网络异常挂起全局
            if os.path.exists(staging_dir):
                shutil.rmtree(staging_dir)
            os.makedirs(os.path.dirname(staging_dir), exist_ok=True)
            self._store_client.download_package(identifier, staging_dir)

            # 校验 staging 内 plugin.json（JSON 合法 + identifier 一致）
            json_path = os.path.join(staging_dir, 'plugin.json')
            if not os.path.isfile(json_path):
                raise ValueError('插件包缺少 plugin.json')
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    new_meta = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                raise ValueError(f'插件包 plugin.json 解析失败: {e}')
            if new_meta.get('identifier') != identifier:
                raise ValueError(
                    f'包内 identifier {new_meta.get("identifier")!r} '
                    f'与目标 {identifier!r} 不一致，拒绝替换')

            # 宿主共享模块静态校验（D-INSTALL-01）
            self._validate_shared_imports(staging_dir, identifier)

            # ── 短锁阶段：备份 → 原子替换 → 刷新 registry ──
            with self._lock_timeout():
                # 备份旧目录
                if os.path.isdir(plugin_dir):
                    os.makedirs(backup_root, exist_ok=True)
                    shutil.copytree(plugin_dir, backup_dir)

                # 原子替换
                if os.path.exists(plugin_dir):
                    shutil.rmtree(plugin_dir)
                shutil.move(staging_dir, plugin_dir)
                swapped = True

                # 清理空 staging 根目录
                staging_root = os.path.dirname(staging_dir)
                if os.path.isdir(staging_root) and not os.listdir(staging_root):
                    os.rmdir(staging_root)

                # 用磁盘新 plugin.json 刷新 registry
                disk_info = self._discovery.discover_one(identifier)
                if disk_info is None:
                    raise ValueError(f'替换后无法从磁盘发现 {identifier}')
                info.metadata = disk_info.metadata
                info.version = disk_info.version
                info.name = disk_info.name
                info.min_app_version = disk_info.min_app_version
                info.path = disk_info.path
                info.updated_at = datetime.now().isoformat()
                info.last_error = None
                self._save_to_db(info)

                # 清理旧备份（保留最近 N=3 份）
                self._prune_backups(identifier, keep=3)

                needs_restart = info.status in (PluginStatus.ENABLED, PluginStatus.ACTIVE)
                self._emit('plugin.updated', plugin_id=identifier)
                print(f'[PluginManager] 🔄 {identifier} upgraded '
                      f'{old_version} → {info.version}'
                      + (' (restart required)' if needs_restart else ''))
                return {
                    'identifier': identifier,
                    'old_version': old_version,
                    'new_version': info.version,
                    'needs_restart': needs_restart,
                }

        except Exception:
            # ── 失败回滚：已替换则恢复旧版，未替换则保持现状 ──
            if swapped and os.path.isdir(backup_dir):
                try:
                    if os.path.isdir(plugin_dir):
                        shutil.rmtree(plugin_dir)
                    shutil.copytree(backup_dir, plugin_dir)
                    print(f'[PluginManager] 🔁 {identifier} 升级失败，已回滚到 v{old_version}')
                except Exception as e:
                    print(f'[PluginManager] ⚠️ {identifier} 回滚失败（保留备份 {backup_dir}）: {e}')
            # 清理残留 staging（备份保留供人工处理）
            if os.path.isdir(staging_dir):
                try:
                    shutil.rmtree(staging_dir)
                except OSError:
                    pass
            import traceback
            traceback.print_exc()
            raise

    def _prune_backups(self, identifier: str, keep: int = 3):
        """清理插件旧版本备份，仅保留最近 keep 份（按 v<版本>-<时间戳> 字典序）"""
        backup_root = os.path.join(self.plugins_dir, '.backup', identifier)
        if not os.path.isdir(backup_root):
            return
        entries = sorted(os.listdir(backup_root))
        for name in entries[:-keep]:
            path = os.path.join(backup_root, name)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
                print(f'[PluginManager] 🧹 清理旧备份 {identifier}/{name}')
            except OSError as e:
                print(f'[PluginManager] ⚠️ 清理备份 {path} 失败: {e}')

    # ── Dashboard 统计聚合（§6.4 / §10.5）──────────────────────────

    def get_all_stats(self) -> dict:
        """聚合所有 ACTIVE 插件的 get_dashboard_stats()。

        每插件最多上报 10 个指标；单插件异常不影响整体聚合。
        """
        result = {}
        for pid, instance in self._instances.items():
            info = self._cache.get(pid)
            if not info or info.status != PluginStatus.ACTIVE:
                continue
            if not hasattr(instance, 'get_dashboard_stats'):
                continue
            try:
                stats = instance.get_dashboard_stats() or {}
                if not isinstance(stats, dict):
                    continue
                result[pid] = dict(list(stats.items())[:10])
            except Exception as e:
                result[pid] = {'error': str(e)}
                print(f'[PluginManager] {pid}: get_dashboard_stats() failed: {e}')
        return result

    # ── 插件 DAG 节点注册（register_dag_nodes）─────────────────────────

    def register_all_dag_nodes(self, engine) -> int:
        """将 ACTIVE 插件的 register_dag_nodes() 注册到 WorkflowEngine。

        引擎节点调用约定: handler(node_def, input_data)。
        仅注册符合该签名的处理器；签名不符（旧式单参数处理器）跳过并告警，
        避免运行时 TypeError。返回注册成功数。
        """
        count = 0
        for pid, instance in self._instances.items():
            info = self._cache.get(pid)
            if not info or info.status != PluginStatus.ACTIVE:
                continue
            if not hasattr(instance, 'register_dag_nodes'):
                continue
            if not self._capability_allowed(info, 'register_dag_nodes'):
                continue
            try:
                nodes = instance.register_dag_nodes() or {}
            except SystemExit as e:
                self._guard_failure(info, 'register_dag_nodes')
                print(f'[PluginManager] ⚠️ {pid}: register_dag_nodes() SystemExit: {e}')
                continue
            except Exception as e:
                self._guard_failure(info, 'register_dag_nodes')
                print(f'[PluginManager] ⚠️ {pid}: register_dag_nodes() 调用失败: {e}')
                continue
            for node_type, handler in nodes.items():
                if not callable(handler):
                    print(f'[PluginManager] ⚠️ {pid}: DAG 节点 {node_type} 处理器不可调用，跳过')
                    continue
                if not _dag_handler_conforms(handler):
                    print(f'[PluginManager] ⚠️ {pid}: DAG 节点 {node_type} 处理器签名不符合'
                          f' handler(node_def, input_data) 约定，跳过')
                    continue
                engine.register_node_handler(node_type, handler)
                count += 1
                print(f'[PluginManager] ✅ {pid}: DAG 节点 {node_type} 已注册')
        return count

    def register_all_plugin_jobs(self, scheduler) -> int:
        """将 ACTIVE 插件的 register_jobs() 注册到 SchedulerEngine（APScheduler 进程内）。

        消费插件 register_jobs() 返回的 APScheduler job dict（id/func/trigger...）。
        单个插件失败不影响其他插件。返回注册成功数。
        """
        count = 0
        for pid, instance in self._instances.items():
            info = self._cache.get(pid)
            if not info or info.status != PluginStatus.ACTIVE:
                continue
            if not hasattr(instance, 'register_jobs'):
                continue
            if not self._capability_allowed(info, 'register_jobs'):
                continue
            try:
                jobs = instance.register_jobs() or []
            except SystemExit as e:
                self._guard_failure(info, 'register_jobs')
                print(f'[PluginManager] ⚠️ {pid}: register_jobs() SystemExit: {e}')
                continue
            except Exception as e:
                self._guard_failure(info, 'register_jobs')
                print(f'[PluginManager] ⚠️ {pid}: register_jobs() 调用失败: {e}')
                continue
            for job in jobs:
                if not isinstance(job, dict) or not callable(job.get('func')):
                    print(f'[PluginManager] ⚠️ {pid}: job {job} 无 func 或不可调用，跳过')
                    continue
                if scheduler.add_plugin_job(job):
                    count += 1
                    print(f'[PluginManager] ✅ {pid}: job {job.get("id")} 已注册')
        return count

    # ── 批量操作 ────────────────────────────────────────────────────────

    def install_all(self) -> List[str]:
        """安装所有已发现但未注册的插件"""
        installed = []
        for plugin in self.discover():
            try:
                self.install(plugin.identifier)
                installed.append(plugin.identifier)
            except Exception as e:
                print(f'[PluginManager] ❌ install {plugin.identifier}: {e}')
        return installed

    def enable_all(self) -> List[str]:
        """启用所有已安装的插件"""
        enabled = []
        for identifier, info in self._cache.items():
            if info.status == PluginStatus.INSTALLED:
                try:
                    self.enable(identifier)
                    enabled.append(identifier)
                except Exception as e:
                    print(f'[PluginManager] ❌ enable {identifier}: {e}')
        return enabled

    def activate_all(self) -> List[str]:
        """激活所有已启用的插件"""
        activated = []
        for identifier, info in self._cache.items():
            if info.status == PluginStatus.ENABLED:
                try:
                    self.activate(identifier)
                    activated.append(identifier)
                except Exception as e:
                    print(f'[PluginManager] ❌ activate {identifier}: {e}')
        return activated

    def _activate_enabled(self):
        """启动时自动激活所有状态为 enabled 的插件"""
        for identifier, info in self._cache.items():
            if info.status == PluginStatus.ENABLED:
                try:
                    self.activate(identifier)
                except Exception as e:
                    print(f'[PluginManager] ❌ auto-activate {identifier}: {e}')

    def _auto_disable_deprecated(self):
        """自动禁用已被新插件替代的旧插件（deprecated 兼容机制）。

        兼容两种声明方式（plugin.json）：
          1. 新插件声明 ``"replaces_plugins": ["old_id"]``；
          2. 旧插件自身标记 ``"status": "deprecated"`` + ``"replaced_by": "new_id"``。
        当替代插件已处于 enabled/active 状态时，自动禁用旧插件（防止路由冲突）。
        幂等：重复调用安全。
        """
        replaced: Dict[str, str] = {}  # old_id -> new_id
        for info in list(self._cache.values()):
            meta = info.metadata or {}
            for old_id in meta.get('replaces_plugins') or []:
                replaced[old_id] = info.identifier
            if meta.get('status') == 'deprecated' and meta.get('replaced_by'):
                replaced[info.identifier] = meta['replaced_by']
        for old_id, new_id in replaced.items():
            old = self._cache.get(old_id)
            replacement = self._cache.get(new_id)
            if not old or not replacement:
                continue
            if replacement.status in (PluginStatus.ENABLED, PluginStatus.ACTIVE) and \
                    old.status in (PluginStatus.INSTALLED, PluginStatus.ENABLED, PluginStatus.ACTIVE):
                print(f'[PluginManager] ⚠️ 自动禁用已弃用插件 {old_id}（由 {new_id} 替代）')
                try:
                    self.disable(old_id)
                except Exception as e:
                    print(f'[PluginManager] ⚠️ 禁用弃用插件 {old_id} 失败: {e}')

    def mount_active_routes(self):
        """启动期挂载所有 enabled/active 插件的路由（必须在首个请求前调用）。

        Flask 的 register_blueprint 只能在启动阶段调用，运行时 activate() 挂载的路由
        无法生效。因此在 app 初始化时统一挂载已启用插件的 Blueprint。
        幂等：已挂载的 Blueprint（同名）会跳过，可安全重复调用。
        """
        if not self.app:
            return
        mounted = []
        for identifier, info in self._cache.items():
            if info.status not in (PluginStatus.ENABLED, PluginStatus.ACTIVE):
                continue
            try:
                instance = self._instances.get(identifier)
                if instance is None:
                    instance = self._load_instance(info)
                    if hasattr(instance, 'setup') and callable(instance.setup):
                        instance.setup()
                    self._instances[identifier] = instance
                if hasattr(instance, 'register_routes'):
                    for bp in instance.register_routes():
                        if bp.name in self.app.blueprints:
                            continue  # 已挂载，跳过
                        prefix = self._get_route_prefix(identifier, bp)
                        self.app.register_blueprint(bp, url_prefix=prefix)
                        mounted.append(f'{identifier}:{prefix}')
            except Exception as e:
                print(f'[PluginManager] ⚠️ mount {identifier} failed: {e}')
        if mounted:
            print(f'[PluginManager] ✅ 启动挂载路由: {mounted}')

    def mount_all_routes(self):
        """启动期挂载**所有已安装插件**（含 disabled）的路由 + 注册前缀映射。

        Flask 3.x 运行时无法动态 register_blueprint，因此启用/禁用免重启的做法是：
        启动时把磁盘上所有插件的 Blueprint 全部挂上，运行时由 before_request 门卫
        按 DB/缓存中的实时启用状态放行或拦截（禁用则 404），从而无需重启。

        同时把每个插件的路由前缀记录到 self._plugin_prefixes，供门卫精确匹配。
        幂等：已挂载的 Blueprint（同名）会跳过，可安全重复调用。
        """
        if not self.app:
            return
        # {url_prefix: identifier} —— 供门卫按最长前缀匹配
        if not hasattr(self, '_plugin_prefixes'):
            self._plugin_prefixes = {}
        mounted = []
        for identifier, info in self._cache.items():
            # 只跳过已卸载/未知状态；installed/enabled/active/disabled 都挂载
            if info.status in (PluginStatus.UNINSTALLED, PluginStatus.UNKNOWN):
                continue
            try:
                instance = self._instances.get(identifier)
                if instance is None:
                    instance = self._load_instance(info)
                    if hasattr(instance, 'setup') and callable(instance.setup):
                        instance.setup()
                    self._instances[identifier] = instance
                if self._capability_allowed(info, 'register_routes') and hasattr(instance, 'register_routes'):
                    for bp in instance.register_routes():
                        prefix = self._get_route_prefix(identifier, bp)
                        # 插件不得抢占 admin 核心根前缀（/admin），否则门卫会把整个
                        # /admin 域误判为插件路径（曾导致 admin 登录页/插件管理全部 404）
                        if prefix in ('', '/', '/admin', '/admin/'):
                            print(f'[PluginManager] ⚠️ {identifier}: 插件前缀 {prefix!r} 抢占 admin 核心域，跳过挂载（请改用专属前缀）')
                            continue
                        self._plugin_prefixes[prefix] = identifier
                        if bp.name in self.app.blueprints:
                            continue  # 已挂载，跳过
                        self.app.register_blueprint(bp, url_prefix=prefix)
                        mounted.append(f'{identifier}:{prefix}')
            except Exception as e:
                print(f'[PluginManager] ⚠️ mount-all {identifier} failed: {e}')
        if mounted:
            print(f'[PluginManager] ✅ 启动挂载全部插件路由: {mounted}')

    def is_path_allowed(self, path: str) -> bool:
        """门卫：判断请求路径对应的插件是否处于启用状态。

        Args:
            path: request.path，如 '/plugin/coupons/api/list'

        Returns:
            True  → 非插件路径，或插件已启用（放行）
            False → 命中某个已挂载但当前被禁用的插件（应拦截为 404）
        """
        prefixes = getattr(self, '_plugin_prefixes', None)
        if not prefixes:
            return True
        # 最长前缀匹配，避免 /plugin/a 误伤 /plugin/ab
        matched_id = None
        matched_len = -1
        for prefix, identifier in prefixes.items():
            if prefix and (path == prefix or path.startswith(prefix + '/')):
                if len(prefix) > matched_len:
                    matched_len = len(prefix)
                    matched_id = identifier
        if matched_id is None:
            return True  # 非插件路径
        return self.is_enabled(matched_id)

    def refresh_status_from_db(self):
        """重新从 plugin_registry 读取各插件状态，刷新内存缓存中的 status。

        仅更新 status 字段，不重建实例/不动路由，供门卫读取最新启用状态。
        用于跨进程或非 API 途径改库后同步（可选，由调用方按需调用）。
        """
        try:
            with get_registry_db() as conn:
                rows = conn.execute(
                    'SELECT identifier, status FROM plugin_registry'
                ).fetchall()
        except Exception as e:
            print(f'[PluginManager] ⚠️ refresh_status_from_db failed: {e}')
            return
        for row in rows:
            d = dict(row)
            info = self._cache.get(d['identifier'])
            if info is not None:
                try:
                    info.status = PluginStatus(d['status'])
                except ValueError:
                    pass

    # ── 查询方法 ────────────────────────────────────────────────────────

    def get_info(self, identifier: str) -> Optional[PluginInfo]:
        """获取插件信息（从缓存）"""
        return self._cache.get(identifier)

    def list_plugins(self, status: str = None) -> List[PluginInfo]:
        """列出插件，可按状态筛选"""
        if status:
            return [p for p in self._cache.values() if p.status.value == status]
        return list(self._cache.values())

    def get_unified_list(self) -> dict:
        """合并本地已安装插件 + 商店目录中未安装的插件

        Returns:
            {
                'local': [PluginInfo dict],      # 已安装插件（含 has_update/latest_version 版本发现标记）
                'store': [StorePlugin dict],     # 商店中未安装的插件
                'total_local': int,
                'total_store': int,
            }
        """
        from .models_store import get_registry_db as _get_store_db

        # 懒刷新商店目录（距上次同步超过 TTL 时异步重新拉取，不阻塞请求）
        try:
            self.ensure_store_synced()
        except Exception as e:
            print(f'[PluginManager] get_unified_list ensure_store_synced failed: {e}')

        local_ids = set(self._cache.keys())
        from .base import localize_plugin_dict

        # ── 以 DB 为成员真相源：跨 worker 统一（增/删/状态一致）──────
        # 旧实现以 _cache 派生成员集：启动后新装/卸载的插件在不同 worker
        # 的内存 _cache 不一致（_cache 只在本 worker 弹出/从未进入），
        # 导致 unified 列表跨 worker 抖动。改为成员集 = DB 全表。
        db_plugins = {}
        try:
            with get_registry_db() as conn:
                db_rows = conn.execute(
                    'SELECT * FROM plugin_registry'
                ).fetchall()
            for row in db_rows:
                r = dict(row)
                db_plugins[r['identifier']] = r
            local = []
            for identifier, row in db_plugins.items():
                info = self._cache.get(identifier)
                if info is not None:
                    local.append(info.to_dict())
                else:
                    local.append(self._row_to_info(row).to_dict())
            local_ids = set(db_plugins.keys())
        except Exception as e:
            print(f'[PluginManager] get_unified_list db load failed: {e}')
            local = [p.to_dict() for p in self._cache.values()]

        # 以 DB 状态覆盖 status / last_error（多 worker 下内存状态可能滞后）
        for p in local:
            row = db_plugins.get(p['identifier'])
            if row:
                if row.get('status'):
                    p['status'] = row['status']
                if row.get('last_error'):
                    p['last_error'] = row['last_error']

        for p in local:
            if isinstance(p.get('status'), PluginStatus):
                p['status'] = p['status'].value
            p['_source'] = 'local'
            # i18n: 按当前语言翻译插件显示名/菜单 label（name_i18n_key 机制）
            localize_plugin_dict(p)

        # ── 版本发现：本地已安装版本 vs 商店目录版本 ──────────
        try:
            local_versions = {p['identifier']: str(p.get('version') or '0.0.0') for p in local}
            updates = self._store_client.check_updates(local_versions) if self._store_client else {}
        except Exception as e:
            print(f'[PluginManager] get_unified_list check_updates failed: {e}')
            updates = {}
        for p in local:
            u = updates.get(p['identifier'])
            p['has_update'] = bool(u and u.get('has_update'))
            p['latest_version'] = (u or {}).get('latest') or p.get('version')

        store_plugins = []
        try:
            with _get_store_db() as conn:
                rows = conn.execute(
                    'SELECT * FROM store_plugins WHERE enabled=1 ORDER BY downloads DESC'
                ).fetchall()
                for row in rows:
                    sp = dict(row)
                    sp['_source'] = 'store'
                    # 状态联动（对齐行业：商店=目录全集，本地=子集状态）
                    # 不剔除已安装插件；每个目录条目标注本地安装/激活状态
                    installed = sp['identifier'] in local_ids
                    sp['installed'] = installed
                    if installed:
                        sp['status'] = db_plugins.get(sp['identifier'], {}).get('status') or 'installed'
                    else:
                        sp['status'] = 'available'
                    # 版本发现：可升级状态（未安装项无升级概念）
                    u = updates.get(sp['identifier'])
                    sp['has_update'] = bool(u and u.get('has_update'))
                    sp['latest_version'] = (u or {}).get('latest') or sp.get('version')
                    # 解析 JSON 字段
                    for field in ('tags', 'screenshots', 'depends_on'):
                        if isinstance(sp.get(field), str):
                            try:
                                sp[field] = json.loads(sp[field])
                            except (json.JSONDecodeError, TypeError):
                                pass
                    store_plugins.append(sp)
        except Exception as e:
            print(f'[PluginManager] get_unified_list store query failed: {e}')

        return {
            'local': local,
            'store': store_plugins,
            'total_local': len(local),
            'total_store': len(store_plugins),
        }

    def ensure_store_synced(self, ttl: int = 300) -> bool:
        """懒刷新商店目录：距上次同步超过 TTL 秒时，异步触发重新同步。

        不阻塞当前请求；当前请求继续使用既有数据，下次请求即拿到最新目录。
        失败时重置时间戳，允许下一次请求立即重试。

        Args:
            ttl: 刷新间隔秒数（默认 300 = 5 分钟）

        Returns:
            True（始终返回，不抛错；内部异常已捕获）
        """
        if self._store_client is None:
            return False
        now = time.time()
        if now - self._last_store_sync_ts <= ttl:
            return True
        self._last_store_sync_ts = now  # 先占位，避免并发请求重复触发

        def _do_sync():
            try:
                count = self._store_client.sync_all()
                self._last_store_sync_ts = time.time()
                print(f'[PluginManager] Store catalog lazy-synced: {count} plugins')
            except Exception as e:
                self._last_store_sync_ts = 0.0  # 失败则下次请求重试
                print(f'[PluginManager] Store lazy sync failed: {e}')

        threading.Thread(target=_do_sync, daemon=True).start()
        return True

    def is_enabled(self, identifier: str) -> bool:
        info = self._cache.get(identifier)
        return info is not None and info.status in (
            PluginStatus.ENABLED, PluginStatus.ACTIVE)

    def is_active(self, identifier: str) -> bool:
        info = self._cache.get(identifier)
        return info is not None and info.status == PluginStatus.ACTIVE

    def get_instance(self, identifier: str) -> Optional[Any]:
        """获取插件运行时实例"""
        return self._instances.get(identifier)

    def count(self) -> int:
        return len(self._cache)

    def count_by_status(self) -> Dict[str, int]:
        counts = {}
        for p in self._cache.values():
            s = p.status.value
            counts[s] = counts.get(s, 0) + 1
        return counts

    # ── 配置读写 ────────────────────────────────────────────────────────

    def get_config(self, identifier: str, key: str = None, default=None):
        """读取插件配置"""
        info = self._cache.get(identifier)
        if not info:
            return default
        if key:
            return info.config.get(key, default)
        return info.config

    def set_config(self, identifier: str, key: str, value,
                   validate: bool = True) -> bool:
        """写入单条插件配置并持久化

        Args:
            identifier: 插件标识
            key: 配置键
            value: 配置值
            validate: 是否校验

        校验失败会打印警告但仍会保存（防止前端设置损坏后无法恢复）。
        """
        with self._lock_timeout():
            info = self._cache.get(identifier)
            if not info:
                return False

            if validate and info.settings_schema:
                test_config = dict(info.config)
                test_config[key] = value
                _v, _ = _get_validators()
                errors = _v(test_config, info.settings_schema)
                if errors:
                    print(f'[PluginManager] {identifier}: config validate warnings: {errors}')

            info.config[key] = value
            info.updated_at = datetime.now().isoformat()
            self._save_to_db(info)
            return True

    def set_config_batch(self, identifier: str, config: dict,
                         coerce: bool = True) -> dict:
        """批量保存插件配置（带 Schema 校验 + 类型转换）

        Args:
            identifier: 插件标识
            config: 完整配置 dict
            coerce: 是否自动类型转换

        Returns:
            {'success': bool, 'errors': [str], 'coerced': dict}
        """
        with self._lock_timeout():
            info = self._cache.get(identifier)
            if not info:
                return {'success': False, 'errors': ['Plugin not found'], 'coerced': {}}

            schema = info.settings_schema or {}
            target = config

            # 类型强制转换
            if coerce and schema:
                _, _cc = _get_validators()
                target = _cc(target, schema)

            # 校验
            _v, _ = _get_validators()
            errors = _v(target, schema)

            if errors:
                # 仍保存（宽松模式），但返回错误列表
                print(f'[PluginManager] {identifier}: config warnings: {errors}')

            info.config = target
            info.updated_at = datetime.now().isoformat()
            self._save_to_db(info)

            return {
                'success': True,
                'errors': errors,
                'coerced': target,
            }

    # ── 配置校验 ──────────────────────────────────────────────────────

    def validate_config(self, identifier: str,
                        config: dict = None) -> dict:
        """校验插件配置

        Args:
            identifier: 插件标识
            config: 待校验的配置（None 表示当前已保存的配置）

        Returns:
            {'success': bool, 'errors': [str], 'schema': dict}
        """
        info = self._cache.get(identifier)
        if not info:
            return {'success': False, 'errors': ['Plugin not found'], 'schema': {}}

        schema = info.settings_schema or {}
        target = config if config is not None else info.config
        _v, _ = _get_validators()
        errors = _v(target, schema)

        return {
            'success': len(errors) == 0,
            'errors': errors,
            'schema': schema,
        }

    # ── 依赖解析 ──────────────────────────────────────────────────────

    def resolve_install_order(self) -> List[str]:
        """拓扑排序，返回安装/激活顺序（依赖优先）"""
        plugin_graph = {}
        for pid, pinfo in self._cache.items():
            plugin_graph[pid] = list(pinfo.dependencies.keys())
        return deps_module.topological_sort(plugin_graph)

    def get_dependency_tree(self, identifier: str) -> dict:
        """获取插件依赖树"""
        plugin_graph = {}
        for pid, pinfo in self._cache.items():
            plugin_graph[pid] = list(pinfo.dependencies.keys())
        return deps_module.get_dependency_tree(identifier, plugin_graph)

    def get_dependents_tree(self, identifier: str) -> dict:
        """获取被哪些插件依赖"""
        plugin_graph = {}
        for pid, pinfo in self._cache.items():
            plugin_graph[pid] = list(pinfo.dependencies.keys())
        _, reverse = deps_module.build_dependency_graph(plugin_graph)
        reverse_plugins = {k: list(v) for k, v in reverse.items()}
        return deps_module.get_dependents_tree(identifier, reverse_plugins)

    def get_plugin_menus(self) -> list:
        """收集所有已启用+已激活插件的菜单项

        状态以数据库 plugin_registry 为准（多 worker 下内存缓存可能滞后，
        直接查 DB 保证各 worker 返回一致结果）。
        """
        from .base import localize_plugin_dict
        from i18n import _
        import os
        import json as _json
        deploy_type = os.environ.get('DEPLOY_TYPE', 'production')
        menus = []
        try:
            with get_registry_db() as conn:
                rows = conn.execute(
                    "SELECT identifier, metadata, path FROM plugin_registry "
                    "WHERE status IN ('enabled','active') "
                    "ORDER BY CASE WHEN identifier IN ('shop','subscription') THEN 0 ELSE 1 END, identifier"
                ).fetchall()
        except Exception as e:
            print(f'[PluginManager] get_plugin_menus db query failed: {e}')
            return menus

        for row in rows:
            pid = row['identifier']
            # site_domains 仅网站版需要，企业版（lan/code/edu）不需要子域名管理
            if pid == 'site_domains' and deploy_type in ('lan', 'code', 'edu'):
                continue
            # metadata 优先取数据库中的最新值（插件安装/更新时同步写入）
            try:
                meta = row.get('metadata')
                if isinstance(meta, str):
                    meta = _json.loads(meta or '{}')
                meta = meta or {}
            except Exception:
                meta = {}
            # 从 plugin.json 读取 menu 配置
            menu_cfg = meta.get('menu') if meta else None
            if not menu_cfg:
                # 尝试从插件实例获取
                inst = getattr(self._cache.get(pid), 'instance', None)
                if inst and hasattr(inst, 'get_menu'):
                    menu_cfg = inst.get_menu()
            if not menu_cfg:
                continue
            # i18n: 按当前语言翻译菜单 label（label_i18n_key 机制）
            localize_plugin_dict({
                'identifier': pid,
                'path': row.get('path') or '',
                'metadata': {'menu': menu_cfg},
            })
            # Support items array for sub-menus (e.g., shop plugin with 4 items)
            if 'items' in menu_cfg:
                group = menu_cfg.get('group', 'Plugins')
                items = menu_cfg['items']
                if len(items) > 1:
                    # 多子项插件 → 嵌套父项（前端渲染为可展开插件入口，如 Shop）
                    children = []
                    for it in items:
                        child = dict(it)
                        child['_plugin_id'] = pid
                        child.setdefault('key', pid)
                        # 子菜单项同样支持 label_i18n_key 翻译
                        localize_plugin_dict({
                            'identifier': pid,
                            'path': row.get('path') or '',
                            'metadata': {'menu': child},
                        })
                        # i18n 兜底：缺失 label_i18n_key 时用系统 _() 翻译 label 原文
                        if not child.get('label_i18n_key'):
                            child['label'] = _(child.get('label') or child.get('key') or pid)
                        children.append(child)
                    menus.append({
                        'group': group,
                        '_plugin_id': pid,
                        'key': pid,
                        'label': _(menu_cfg.get('label') or meta.get('name') or pid),
                        'icon': menu_cfg.get('icon') or meta.get('icon') or (items[0].get('icon') or 'plugins'),
                        'plugin_menu': True,
                        'children': children,
                    })
                else:
                    # 单子项插件 → 平铺（避免"父项+子项"重复，如 Subscription）
                    item = dict(items[0])
                    item['group'] = group
                    item['_plugin_id'] = pid
                    item.setdefault('key', pid)
                    # 子菜单项同样支持 label_i18n_key 翻译
                    localize_plugin_dict({
                        'identifier': pid,
                        'path': row.get('path') or '',
                        'metadata': {'menu': item},
                    })
                    # i18n 兜底：缺失 label_i18n_key 时用系统 _() 翻译 label 原文
                    if not item.get('label_i18n_key'):
                        item['label'] = _(item.get('label') or item.get('key') or pid)
                    menus.append(item)
            else:
                menu_cfg['_plugin_id'] = pid
                menu_cfg.setdefault('key', pid)
                # i18n 兜底：缺失 label_i18n_key 时用系统 _() 翻译 label 原文
                if not menu_cfg.get('label_i18n_key'):
                    menu_cfg['label'] = _(menu_cfg.get('label') or menu_cfg.get('key') or pid)
                menus.append(menu_cfg)
        return menus

    # ── 日志 ──────────────────────────────────────────────────────────

    def read_log(self, identifier: str, lines: int = 50) -> str:
        """读取插件日志最后 N 行"""
        from .logger import read_plugin_log
        return read_plugin_log(identifier, lines)

    def clear_log(self, identifier: str) -> bool:
        """清空插件日志"""
        from .logger import clear_plugin_log
        return clear_plugin_log(identifier)

    # ── License & Store 访问器 ───────────────────────────────────────

    @property
    def license_manager(self):
        return self._license_mgr

    @property
    def store_client(self):
        return self._store_client

    # ── 钩子/事件代理（Phase 3 完整实现） ──────────────────────────────

    def register_hook(self, identifier: str, hook_name: str, callback):
        if self._hook_registry:
            self._hook_registry.add_action(hook_name, callback)

    def trigger_action(self, hook_name: str, *args, **kwargs):
        if self._hook_registry:
            self._hook_registry.do_action(hook_name, *args, **kwargs)

    def apply_filter(self, hook_name: str, value, **kwargs):
        if self._hook_registry:
            return self._hook_registry.apply_filters(hook_name, value, **kwargs)
        return value

    # ── 内部方法 ────────────────────────────────────────────────────────

    def _get_cached(self, identifier: str) -> PluginInfo:
        """获取缓存中的插件信息，不存在则抛出异常"""
        info = self._cache.get(identifier)
        if info is None:
            # 尝试从数据库恢复
            info = self._load_from_db(identifier)
            if info:
                self._cache[identifier] = info
            else:
                raise PluginNotFoundError(identifier)
        return info

    def _load_instance(self, info: PluginInfo) -> Any:
        """动态加载插件模块，返回 BasePlugin 子类实例"""
        identifier = info.identifier
        plugin_dir = info.path

        if not os.path.isdir(plugin_dir):
            raise PluginNotFoundError(identifier)

        # 确保项目根在 sys.path（供插件导入根业务模块 analytics/health_check 等）
        project_root = os.path.dirname(os.path.dirname(plugin_dir))  # plugins/ 的父目录
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

        try:
            # 用命名空间包导入（plugins.<identifier>），避免插件包名污染顶层命名空间，
            # 防止如 plugins/analytics 遮蔽项目根 analytics 业务模块。
            mod = importlib.import_module(f'plugins.{identifier}')

            from plugin_manager.base import BasePlugin

            instance = None
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (isinstance(attr, type) and
                        issubclass(attr, BasePlugin) and
                        attr is not BasePlugin):
                    instance = attr()
                    instance._config = info.config
                    # 注入引用
                    instance.plugin_info = info
                    instance.manager = self
                    # 注入独立日志器
                    instance._log = get_plugin_logger(identifier)
                    # 重新加载 i18n
                    if hasattr(instance, '_load_i18n'):
                        instance._load_i18n()
                    break

            if instance is None:
                # 新式插件: 尝试直接实例化 __plugin__.py 中的类
                plugin_mod_path = os.path.join(plugin_dir, '__plugin__.py')
                if os.path.isfile(plugin_mod_path):
                    spec = importlib.util.spec_from_file_location(
                        f'{identifier}.__plugin__', plugin_mod_path)
                    plugin_mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(plugin_mod)
                    for attr_name in dir(plugin_mod):
                        attr = getattr(plugin_mod, attr_name)
                        if (isinstance(attr, type) and
                                issubclass(attr, BasePlugin) and
                                attr is not BasePlugin):
                            instance = attr()
                            instance.plugin_info = info
                            instance.manager = self
                            instance._log = get_plugin_logger(identifier)
                            break

            if instance is None:
                raise RuntimeError(f')No BasePlugin subclass found in {identifier}')

            return instance

        except ImportError as e:
            raise RuntimeError(f'ImportError loading {identifier}: {e}')

    def _resolve_dependencies(self, identifier: str, deps: Dict[str, str]):
        """解析并验证依赖: 检查依赖是否已启用 + 版本满足"""
        if not deps:
            return

        # 检测循环依赖（简单版：两跳内）
        dep_stack = list(deps.keys())
        for dep_id in dep_stack:
            dep_info = self._cache.get(dep_id)
            if dep_info and dep_info.dependencies:
                if identifier in dep_info.dependencies:
                    raise PluginCircularDependencyError([identifier, dep_id, identifier])

        # 检查每个依赖
        # python / python_optional 为"宿主 Python 环境"依赖，由部署环境保证，非插件依赖
        env_dep_keys = ('python', 'python_optional')
        missing = []
        for dep_id, version_spec in deps.items():
            if dep_id in env_dep_keys:
                continue

            # 依赖状态以 DB 为准（多 worker 下内存状态可能滞后）
            dep_info = self._cache.get(dep_id)
            dep_status = None
            dep_version = None
            try:
                with get_registry_db() as conn:
                    row = conn.execute(
                        'SELECT status, version FROM plugin_registry WHERE identifier=?',
                        (dep_id,)
                    ).fetchone()
                if row is None:
                    missing.append(dep_id)
                    continue
                dep_status = row['status']
                dep_version = row['version']
            except Exception as e:
                print(f'[PluginManager] _resolve_dependencies db query failed ({dep_id}): {e}')
                if dep_info is None:
                    missing.append(dep_id)
                    continue
                dep_status = dep_info.status.value
                dep_version = dep_info.version

            if dep_status not in ('enabled', 'active'):
                missing.append(f'{dep_id} (status: {dep_status})')
                continue

            if version_spec and not version_satisfies(dep_version, version_spec):
                raise PluginVersionError(dep_id, version_spec, dep_version)

        if missing:
            raise PluginDependencyError(identifier, missing)

    def _check_deps_active(self, info: PluginInfo):
        """检查依赖插件是否都已激活"""
        for dep_id in info.dependencies:
            dep_info = self._cache.get(dep_id)
            if dep_info and dep_info.status != PluginStatus.ACTIVE:
                # 自动尝试激活
                if dep_info.status == PluginStatus.ENABLED:
                    self.activate(dep_id)

    def _notify_dependents(self, identifier: str, action: str):
        """通知依赖此插件的其他插件"""
        for pid, pinfo in self._cache.items():
            if identifier in pinfo.dependencies:
                print(f'[PluginManager] {pid}: dependency {identifier} {action}')

    def _get_route_prefix(self, identifier: str, bp) -> str:
        """确定路由前缀"""
        # 如果 Blueprint 已自定义 url_prefix，则使用自定义的
        if bp.url_prefix:
            return bp.url_prefix
        return f'/plugin/{identifier}'

    def _unregister_blueprint(self, bp):
        """从 Flask app 移除 Blueprint（实验性）"""
        if not self.app:
            return
        # Flask 没有官方方法来反注册，这里只是从 app 的蓝图中移除引用
        name = bp.name
        if name in self.app.blueprints:
            del self.app.blueprints[name]

    def _emit(self, event_name: str, **data):
        """触发内部事件（由 EventBus + HookRegistry 消费）"""
        # 通过 EventBus 发布（异步，订阅者模式）
        if hasattr(self._event_bus, 'emit'):
            self._event_bus.emit(event_name, **data)

        # 通过 HookRegistry 执行 action
        if self._hook_registry:
            # 约定: event_name = "plugin.installed" → hook = "plugin/installed"
            hook_name = event_name.replace('.', '/')
            self._hook_registry.do_action(hook_name, **data)

    # ── 数据库操作 ──────────────────────────────────────────────────────

    def _load_cache(self):
        """从数据库加载所有已注册插件到缓存"""
        self._cache.clear()
        with get_registry_db() as conn:
            rows = conn.execute(
                'SELECT * FROM plugin_registry ORDER BY identifier'
            ).fetchall()
            for row in rows:
                info = self._row_to_info(dict(row))
                self._cache[info.identifier] = info

    def _load_from_db(self, identifier: str) -> Optional[PluginInfo]:
        """从数据库加载单个插件"""
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT * FROM plugin_registry WHERE identifier = ?',
                (identifier,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_info(dict(row))

    def _save_to_db(self, info: PluginInfo):
        """保存或更新插件记录到数据库"""
        with get_registry_db() as conn:
            conn.execute("""
                INSERT INTO plugin_registry (
                    identifier, name, version, author, description,
                    min_app_version, path, metadata, status, config,
                    dependencies, provides_hooks, listens_hooks,
                    permissions, settings_schema, installed_at,
                    updated_at, last_error, source,
                    category, icon, tags, dashboard_meta
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(identifier) DO UPDATE SET
                    name=excluded.name,
                    version=excluded.version,
                    author=excluded.author,
                    description=excluded.description,
                    min_app_version=excluded.min_app_version,
                    path=excluded.path,
                    metadata=excluded.metadata,
                    status=excluded.status,
                    config=excluded.config,
                    dependencies=excluded.dependencies,
                    provides_hooks=excluded.provides_hooks,
                    listens_hooks=excluded.listens_hooks,
                    permissions=excluded.permissions,
                    settings_schema=excluded.settings_schema,
                    updated_at=excluded.updated_at,
                    last_error=excluded.last_error,
                    source=excluded.source,
                    category=excluded.category,
                    icon=excluded.icon,
                    tags=excluded.tags,
                    dashboard_meta=excluded.dashboard_meta
            """, (
                info.identifier, info.name, info.version,
                info.author, info.description,
                info.min_app_version, info.path,
                json.dumps(info.metadata, ensure_ascii=False, default=str),
                info.status.value,
                json.dumps(info.config, ensure_ascii=False, default=str),
                json.dumps(info.dependencies, ensure_ascii=False),
                json.dumps(info.provides_hooks, ensure_ascii=False),
                json.dumps(info.listens_hooks, ensure_ascii=False),
                json.dumps(info.permissions, ensure_ascii=False),
                json.dumps(info.settings_schema, ensure_ascii=False, default=str),
                info.installed_at or datetime.now().isoformat(),
                info.updated_at or datetime.now().isoformat(),
                info.last_error,
                getattr(info, 'source', 'store'),
                info.category,
                info.icon,
                json.dumps(info.tags, ensure_ascii=False),
                json.dumps(info.dashboard_meta, ensure_ascii=False),
            ))
            conn.commit()

    def _delete_from_db(self, identifier: str):
        """从数据库删除插件记录"""
        with get_registry_db() as conn:
            conn.execute(
                'DELETE FROM plugin_registry WHERE identifier = ?',
                (identifier,)
            )
            conn.commit()

    def _row_to_info(self, row: dict) -> PluginInfo:
        """数据库行 → PluginInfo"""
        meta = json.loads(row.get('metadata', '{}'))
        return PluginInfo(
            identifier=row['identifier'],
            name=row['name'],
            version=row['version'],
            author=row.get('author', ''),
            description=row.get('description', ''),
            min_app_version=row.get('min_app_version', '1.0.0'),
            path=row.get('path', ''),
            metadata=meta,
            status=PluginStatus(row['status']),
            config=json.loads(row.get('config', '{}')),
            dependencies=json.loads(row.get('dependencies', '{}')),
            provides_hooks=json.loads(row.get('provides_hooks', '[]')),
            listens_hooks=json.loads(row.get('listens_hooks', '[]')),
            permissions=json.loads(row.get('permissions', '[]')),
            settings_schema=json.loads(row.get('settings_schema', '{}')),
            admin_url=row.get('admin_url', '') or meta.get('admin_url', ''),
            admin_label=row.get('admin_label', '') or meta.get('admin_label', ''),
            installed_at=row.get('installed_at'),
            updated_at=row.get('updated_at'),
            last_error=row.get('last_error', ''),
            source=row.get('source', 'store'),
            # ★ v1.5 展示与分类字段（列值优先，空则回退 metadata）
            category=row.get('category') or meta.get('category', 'other'),
            icon=row.get('icon') or meta.get('icon', 'plugin'),
            tags=json.loads(row.get('tags', '[]') or '[]'),
            dashboard_meta=json.loads(row.get('dashboard_meta', '{}') or '{}'),
        )
