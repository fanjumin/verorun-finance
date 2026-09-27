"""net_proxy 定时任务声明与转发。

本文件被 `plugins/net_proxy/__init__.py::register_jobs()` 调用。
真正的实现在 fuse.py（探活）与 models.py（日志清理）。

约定（见 plugin-standard-v1.8 §6.3 / 参考 plugins/risk_control/scheduler.py）：
- 返回 job 字典列表，字段 `{id, func, trigger, ...}`；
- `func` 必须是可调用对象；
- 注册走 SchedulerEngine.add_plugin_job()，为进程内 APScheduler，
  不写 `cron_jobs` 表，`replace_existing=True` 保证幂等。

多 worker 幂等（§7.8）：状态更新全部走原子单行 UPDATE；重复探活只产生
重复 probe_log 行与重复探活流量，无脏数据。
"""

from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('net_proxy')

# 插件实例句柄：register_jobs(plugin_instance) 时注入，供 job 读 config。
# 进程内单例，插件卸载时清空。
_plugin_handle = None

# 探活间隔缺省值（config `probe_interval_minutes` 缺省 5）
DEFAULT_PROBE_INTERVAL_MINUTES = 5


def set_plugin_instance(plugin_instance):
    """由 __init__.py 在 register_jobs 时注入插件实例。"""
    global _plugin_handle
    _plugin_handle = plugin_instance


def _probe_interval_minutes():
    """读 config `probe_interval_minutes`，缺省 5 分钟。"""
    if _plugin_handle is None:
        return DEFAULT_PROBE_INTERVAL_MINUTES
    try:
        raw = _plugin_handle.get_config_value(
            'probe_interval_minutes', DEFAULT_PROBE_INTERVAL_MINUTES)
        val = int(raw)
    except Exception:
        return DEFAULT_PROBE_INTERVAL_MINUTES
    return val if val > 0 else DEFAULT_PROBE_INTERVAL_MINUTES


def run_probe_all_channels():
    """探测全部出口通道的连通性（§7.6 / §7.8 job1）。"""
    try:
        from . import fuse
    except ImportError as e:
        logger.warning('net_proxy.fuse 导入失败，跳过通道探测：%s', e)
        return
    try:
        results = fuse.probe_all_channels(plugin=_plugin_handle)
    except Exception:
        logger.exception('net_proxy 通道探活失败')
        return
    probed = [r for r in results if not r.get('skipped')]
    ok = sum(1 for r in probed if r.get('ok'))
    logger.info('net_proxy 通道探活完成：参与 %d，成功 %d，跳过 %d',
                len(probed), ok, len(results) - len(probed))


def run_cleanup_logs():
    """按保留策略清理请求日志与探测日志（§7.8 job2）。"""
    try:
        from . import models as m
    except ImportError as e:
        logger.warning('net_proxy.models 导入失败，跳过日志清理：%s', e)
        return
    try:
        deleted = m.cleanup_logs()
    except Exception:
        logger.exception('net_proxy 日志清理失败')
        return
    logger.info('net_proxy 日志清理完成：%s', deleted)


def get_jobs(plugin_instance=None):
    """返回本插件的定时任务列表。

    Args:
        plugin_instance: BasePlugin 实例，用于 job 运行时读插件 config。

    Returns:
        list[dict]: job 定义列表。
    """
    if plugin_instance is not None:
        set_plugin_instance(plugin_instance)
    return [
        {
            'id': 'net_proxy_probe_channels',
            'func': run_probe_all_channels,
            'trigger': 'interval',
            'minutes': _probe_interval_minutes(),
        },
        {
            'id': 'net_proxy_cleanup_logs',
            'func': run_cleanup_logs,
            'trigger': 'cron',
            'hour': 3,
            'minute': 10,
        },
    ]
