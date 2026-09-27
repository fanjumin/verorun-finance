"""net_proxy —— 统一的出站网络代理与出口治理插件。

职责（长期目标）：
- 集中管理出口通道（直连 / HTTP 代理 / SOCKS 代理）；
- 按目标域或插件来源做出口路由；
- 记录出站请求日志与连通性探测结果，供运维查看与告警。

当前阶段：P1 —— 仅插件骨架（清单 + 入口 + 任务声明）。
本阶段**不做**任何 DDL、不注册任何 blueprint / 路由、不写任何表；
数据表与路由在后续阶段按方案逐阶段落地。

归属（插件标准 §2.2/§4）：agent_role = ops。
实测 `resolve_agent_roles('net_proxy', meta)` 返回 `['ops']` —— 走**路径①**：
ops 是 roles/ 根目录 YAML 推导出的本版核心角色之一，agent_role 直接命中。
科研版（research-desktop）角色集无 ops → 本版不接纳本插件（符合 §2.2
「禁止为消除校验报错反向扩充角色集」）。
"""

import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.base import BasePlugin  # noqa: E402
from plugin_manager.logger import get_plugin_logger  # noqa: E402

_logger = get_plugin_logger('net_proxy')

# 本插件独立 schema 名（P2 起使用；P1 不创建）
SCHEMA_NAME = 'net_proxy'

__all__ = ['NetProxyPlugin', 'SCHEMA_NAME', 'enrich_dashboard']


class NetProxyPlugin(BasePlugin):
    """出站网络代理插件（P1 骨架）。"""

    name = 'net_proxy'

    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '1.0.0'

    description = 'Net Proxy — Unified outbound network proxy & egress governance for plugins'
    author = 'VeroRun'

    # ---------- 生命周期 ----------

    def on_install(self, registry):
        """安装钩子：P1 不建表。

        说明：manager.install() 实际并不调用本方法（它在 enable 链路之外），
        此处保留标准签名，供后续阶段接管 DDL。
        """
        _logger.info('net_proxy: on_install（P1 骨架，无 DB 变更）')
        return True

    def on_enable(self, registry):
        """启用钩子：注册 dashboard filter。

        注：不再读取 `default_channel` —— 该键属 P1 早期自造，方案 §5 定稿的
        config 七键集（default_timeout_s / probe_interval_minutes / probe_target /
        fuse_threshold / fuse_cooldown_minutes / log_retention_days / default_policy）
        中并不存在，读了恒为空，是一个永不进入的日志分支。出口策略由
        `rules.resolve()` 的 L3 兜底（读 `default_policy`）决定，不在启用期缓存。
        """
        self._register_dashboard_filter()
        _logger.info('net_proxy: on_enable 完成')
        return True

    def on_disable(self, registry):
        _logger.info('net_proxy: on_disable')
        return True

    def on_uninstall(self, registry):
        """卸载钩子：P1 无数据，不删任何东西。

        待 P2 建表后如需彻底清理，在此按 health_check 同款写法用
        plugins._base.db.get_raw_connection() 执行 DROP SCHEMA，
        且必须经用户明确同意（铁律：破坏性操作先方案后执行）。
        """
        _logger.info('net_proxy: on_uninstall（P1 无数据可清理，保留以便重装）')
        return True

    def activate(self):
        """激活钩子：执行本插件 schema 迁移（幂等）。

        必须挂在这里而不是 on_install()：manager.install() 并不调用 on_install，
        而 enable()（manager.py:1134）与启动期预热都会调用 activate()。
        run_migrations() 内部持 advisory 锁 + schema_migrations 去重，可安全重复调用；
        失败只告警不抛出，避免拖垮宿主启动。
        """
        try:
            from .models import run_migrations
            applied = run_migrations()
            if applied:
                _logger.info('net_proxy: schema 迁移完成 %s', applied)
        except Exception as e:
            _logger.warning('net_proxy: schema 迁移失败（不阻塞启动）: %s', e)

    # ---------- 能力注册 ----------

    def register_routes(self):
        """路由注册：挂载管理端 API blueprint（P5）。

        前缀规则（`manager._get_route_prefix()` L2661-2666）：Blueprint **未设**
        url_prefix 时由管理器补 `/plugin/<identifier>`，故 routes.py 里刻意不写
        url_prefix，实际路径 = `/plugin/net_proxy/admin/...`（与方案 §8 一致）。

        注意：`_preload_routes()` 对 status IN ('enabled','active') 的插件都会调用
        本方法；路由在 app 首个请求前一次性挂载（Flask 3.x 不支持运行时 register）。
        """
        from .routes import net_proxy_bp
        return [net_proxy_bp]

    def register_jobs(self):
        """定时任务注册：通道连通性探测 + 日志保留清理（§6.3）。"""
        from .scheduler import get_jobs
        return get_jobs(self)

    def register_health_checks(self):
        """健康检查注册（§7.9）：schema 连通、熔断通道数、24h 出站成功率。

        实现在 health.py，只读探测、无副作用，巡检可反复调用。
        """
        from .health import register_health_checks
        return register_health_checks()

    def register_agents(self):
        """§4.1 — 声明本插件提供的 Agent 能力（聚合到 ops 核心角色，不新建角色行）。"""
        return [{
            'id': 'net_proxy_advisor',
            'name': 'Net Proxy Advisor',
            'description': 'Outbound egress diagnostics and channel routing suggestion engine',
            'capabilities': ['net_proxy_status', 'net_proxy_channel_manage',
                             'net_proxy_egress_route'],
        }]

    # ---------- 仪表盘 ----------

    def get_dashboard_stats(self) -> dict:
        """§2.3 — 仪表盘统计。P1 无数据表，返回零值占位。"""
        return {
            'net_proxy_channels': 0,
            'net_proxy_requests_today': 0,
            'net_proxy_probe_healthy': 0,
            'net_proxy_probe_failed': 0,
        }

    # ---------- Schema 版本 ----------

    def get_schema_version(self) -> int:
        """§10.6 — P1 尚未建表，schema 版本 = 0。"""
        return 0

    def migrate(self, from_version: int, to_version: int) -> bool:
        """§10.6 — 迁移入口：P1 无 schema，直接成功返回。"""
        return True

    # ---------- 内部 ----------

    def _register_dashboard_filter(self):
        """幂等注册 dashboard.data 过滤器（与 health_check 同款写法）。"""
        try:
            from plugin_manager.hooks import get_hook_registry
            hooks = get_hook_registry()
            already = any(
                h.get('identifier') == 'net_proxy'
                for hooks_list in hooks.list_filters('dashboard.data').values()
                for h in hooks_list
            )
            if already:
                return True
            hooks.add_filter('dashboard.data', enrich_dashboard,
                             priority=20, identifier='net_proxy')
            _logger.info('net_proxy: dashboard.data filter registered')
            return True
        except Exception as e:
            _logger.warning('net_proxy: dashboard filter registration warning: %s', e)
            return False


def enrich_dashboard(value, conn=None):
    """dashboard.data 过滤器：把 net_proxy 统计扁平注入 Dashboard 数据。

    与 health_check.enrich_dashboard 同款结构 —— **顶层扁平 key**，
    不用嵌套 dict（dashboard 消费方按 `data[<stat key>]` 直取）。
    P1 无数据表，注入零值占位；P2 起改为读真实统计。
    """
    data = value
    if not isinstance(data, dict):
        return value
    try:
        stats = NetProxyPlugin.get_dashboard_stats(_DashboardStatsShim())
        data.update(stats)
    except Exception:  # pragma: no cover - 过滤器不得影响主流程
        pass
    return data


class _DashboardStatsShim:
    """仅为在过滤器内复用 get_dashboard_stats() 的无状态占位实例。

    get_dashboard_stats() 在 P1 不依赖任何实例状态（无 DB、无 config），
    故用空壳调用即可，避免过滤器内重复维护一份零值常量。
    """

    def get_config_value(self, key, default=None):
        return default
