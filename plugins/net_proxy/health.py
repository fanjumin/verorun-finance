"""net_proxy 健康检查（plugin-standard-v1.8 §7.9 / §12.6）。

本文件被 `plugins/net_proxy/__init__.py::register_health_checks()` 调用。
只做**只读**探测，不写任何表、不改任何状态 —— 巡检可反复执行。

契约为 `dict` 含 `'ok'`（与本仓库 veroscholar / health_check 同款）：

    check() -> {'ok': bool, **detail}

返回结构：

    register_health_checks() -> [
        {'name': 'net_proxy.schema_ok',
         'description': '...',
         'check': <callable>},
        ...
    ]

三项检查（对应方案 §7.9 L402-404）：

1. `net_proxy.schema_ok`        —— schema 可连可查（4 张核心表就位）；
2. `net_proxy.fused_channels`   —— 当前熔断数 > 0 → 不健康（warn 语义）；
3. `net_proxy.recent_success_rate` —— 24h 出站成功率 < 90% → 不健康。

注意 `fused_channels` 与 `recent_success_rate` 的「不健康」是**告警语义**：
设计上熔断与成功率下跌都是需要运维介入的状态，故 ok=False 用于触发告警；
无请求（total=0）时成功率口径为 100.0（见 models.request_stats 注释），
不会误报。
"""

from plugin_manager.logger import get_plugin_logger

from . import models as m

_logger = get_plugin_logger('net_proxy')

# 核心表集合：schema_ok 要求这 4 张业务表全部就位
_REQUIRED_TABLES = (
    'proxy_channels',
    'proxy_rules',
    'proxy_request_log',
    'proxy_probe_log',
)

# 24h 出站成功率告警下限（%）
_SUCCESS_RATE_FLOOR = 90.0

# 成功率统计窗口（小时）
_STATS_WINDOW_HOURS = 24


def register_health_checks():
    """返回本插件的健康检查列表（§7.9）。

    Returns:
        list[dict]: 每项 {name, description, check}，check 为无参可调用，
        返回 {'ok': bool, **detail}。
    """
    return [
        {
            'name': 'net_proxy.schema_ok',
            'description': 'net_proxy schema reachable and core tables present',
            'check': _check_schema_ok,
        },
        {
            'name': 'net_proxy.fused_channels',
            'description': 'No channel is currently fused (fused_until > now)',
            'check': _check_fused_channels,
        },
        {
            'name': 'net_proxy.recent_success_rate',
            'description': 'Egress success rate over the last 24h is >= 90%',
            'check': _check_recent_success_rate,
        },
    ]


# ═══════════════════════════════════════════════════════════════════════════
# 各项检查实现
# ═══════════════════════════════════════════════════════════════════════════

def _check_schema_ok() -> dict:
    """schema 可连可查：逐表确认存在。

    用 models.table_exists()（查 information_schema.tables，自限定
    table_schema='net_proxy'），不依赖 search_path，也不触发建表。
    """
    try:
        present = {t: m.table_exists(t) for t in _REQUIRED_TABLES}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

    missing = sorted(t for t, ok in present.items() if not ok)
    if missing:
        return {
            'ok': False,
            'missing_tables': missing,
            'error': 'missing tables: %s' % ', '.join(missing),
        }
    return {'ok': True, 'tables': list(_REQUIRED_TABLES)}


def _check_fused_channels() -> dict:
    """当前熔断通道数。> 0 视为不健康（需运维介入）。"""
    try:
        fused = m.fused_channel_count()
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    return {'ok': fused <= 0, 'fused_channels': int(fused)}


def _check_recent_success_rate() -> dict:
    """近 24h 出站成功率。低于 90% 视为不健康。

    models.request_stats(24)['success_rate'] 在无请求时返回 100.0，
    因此空库/未启用代理的干净状态不会误报。
    """
    try:
        stats = m.request_stats(hours=_STATS_WINDOW_HOURS)
    except Exception as e:
        return {'ok': False, 'error': str(e)}

    rate = float(stats.get('success_rate') or 0.0)
    total = int(stats.get('total') or 0)
    detail = {
        'ok': rate >= _SUCCESS_RATE_FLOOR,
        'success_rate': rate,
        'threshold': _SUCCESS_RATE_FLOOR,
        'total': total,
        'ok_count': int(stats.get('ok') or 0),
        'avg_latency_ms': stats.get('avg_latency_ms'),
        'window_hours': _STATS_WINDOW_HOURS,
    }
    if detail['ok']:
        return detail
    detail['error'] = ('success rate %.1f%% below %.1f%% over last %dh'
                       % (rate, _SUCCESS_RATE_FLOOR, _STATS_WINDOW_HOURS))
    return detail
