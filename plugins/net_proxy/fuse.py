"""net_proxy 探活与熔断 —— 通道健康度维护。

方案 §7.6 原文职责：
  * `probe_channel(channel)`：经通道 GET `probe_target`（留空则本 job 跳过主动探活），
    量延迟，写 probe_log，更新 last_probe_at / last_latency_ms。
  * `record_result(channel_id, ok)`：**原子单行 UPDATE**（见 models.record_channel_result），
    失败累加、达 `fuse_threshold` 置 `fused_until`；成功复位。多 worker 并发安全。
  * `pick_healthy(usage_tags)`：候选 = enabled 且未熔断且标签匹配，按 weight 加权随机。
  * `max_concurrent` 为进程内 best-effort 信号量（每 worker 独立计数，近似值，UI 注明）。

信任边界（§9.1 三分法）：
  **探活目标由管理员配置 → 允许内网地址**（LAN 代理场景的硬依赖），
  因此本模块**不做** §9.2 的阻断网段校验；该校验只针对业务目标（egress.py）。

探活用 `requests` 经通道自身作为代理 GET 目标 —— 这恰好验证"通道可用"，
而非"目标可达"。目标留空时跳过（避免无意义流量）。
"""

import threading
import time

import requests

from . import channels as ch
from . import models as m
from . import rules as R

__all__ = [
    'DEFAULT_PROBE_TIMEOUT_S',
    'probe_channel',
    'probe_all_channels',
    'record_result',
    'pick_healthy',
    'fused_channels',
    'MaxConcurrencyGate',
]

# 探活超时上限（探活是低频后台任务，宁可慢也不要留悬挂连接）
DEFAULT_PROBE_TIMEOUT_S = 8

# 探活判定：HTTP < 500 视为通道可用（4xx 说明代理转发成功，目标侧问题）
_PROBE_OK_STATUS_BELOW = 500


def _config_value(key, default=None, plugin=None):
    """读插件 config。优先用插件实例（get_config_value），否则退回默认。"""
    if plugin is not None:
        try:
            val = plugin.get_config_value(key, default)
            if val is not None:
                return val
        except Exception:
            pass
    return default


def _probe_timeout(plugin=None, default_timeout=None):
    """探活超时：取 min(default_timeout_s, DEFAULT_PROBE_TIMEOUT_S)。"""
    raw = default_timeout
    if raw is None:
        raw = _config_value('default_timeout_s', DEFAULT_PROBE_TIMEOUT_S, plugin)
    try:
        t = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PROBE_TIMEOUT_S
    if t <= 0:
        return DEFAULT_PROBE_TIMEOUT_S
    return min(t, DEFAULT_PROBE_TIMEOUT_S)


def probe_channel(channel, probe_target='', plugin=None, timeout=None):
    """对单个通道做一次连通性探测并落库（§7.6）。

    Args:
        channel: 通道行（需含 protocol/host/port，可选凭据）。
        probe_target: 目标 URL；**留空则跳过主动探活**（§7.6 原文）。
        plugin: 插件实例（用于读 config）。
        timeout: 秒；省略按 config 推导。

    Returns:
        dict: `{'ok': bool, 'latency_ms': int|None, 'detail': str, 'skipped': bool}`

    说明：目标留空时返回 `skipped=True` 且**不写日志**（避免刷无意义行）。
    """
    target = (probe_target or '').strip()
    if not target:
        return {'ok': True, 'data_ok': False, 'latency_ms': None,
                'detail': 'probe_target 未配置，跳过主动探活', 'skipped': True}

    channel_id = channel.get('id') if hasattr(channel, 'get') else None
    if channel_id is None:
        return {'ok': False, 'data_ok': False, 'latency_ms': None,
                'detail': '通道缺少 id，无法探活', 'skipped': False}

    proxies = ch.build_proxies(channel)   # 解密失败会抛 CredentialError，由调用方兜
    if not proxies:
        detail = '通道字段不完整，无法构造代理地址'
        try:
            m.record_probe(channel_id, False, None, detail)
        except Exception:
            pass
        return {'ok': False, 'data_ok': False, 'latency_ms': None,
                'detail': detail, 'skipped': False}

    t = timeout if timeout is not None else _probe_timeout(plugin)
    t0 = time.time()
    ok = False
    # data_ok 与 ok 的区别（2026-09-22 新增）：
    #   ok      = 通道转发本身是否可用（语义同旧版，HTTP < 500，4xx 也算转发成功）
    #   data_ok = 目标是否真的返回了可用数据（2xx）
    # 作"数据源可用性"判据时看 data_ok；作"通道健康度"判据时看 ok，口径与
    # egress._counts_as_failure 保持一致，避免两处对同一通道给出矛盾结论。
    data_ok = False
    latency_ms = None
    detail = ''
    try:
        s = requests.Session()
        # 2026-09-22 修复：不继承进程环境变量里的代理（HTTP_PROXY/HTTPS_PROXY）。
        # 实测（2026-09-22，本机 HTTP_PROXY=http://127.0.0.1:62521）：
        # trust_env=True 时 Session 会带上环境代理，与显式传入的 proxies 冲突 →
        # 全部 ProxyError 且每次撞满超时（对照实验：干净 Session 为 HTTP 200 / 2.0s）。
        # 探活语义是"用传入的这条通道出网"，不应被环境变量干扰。
        s.trust_env = False
        try:
            resp = s.get(target, proxies=proxies, timeout=t)
            latency_ms = int(round((time.time() - t0) * 1000))
            status_code = int(resp.status_code)
            ok = status_code < _PROBE_OK_STATUS_BELOW
            data_ok = 200 <= status_code < 300
            detail = 'HTTP %s' % resp.status_code
        finally:
            try:
                s.close()
            except Exception:
                pass
    except requests.exceptions.Timeout:
        detail = 'timeout(%ss)' % t
    except requests.exceptions.ProxyError as e:
        detail = 'proxy error: %s' % str(e)[:200]
    except Exception as e:
        detail = '%s: %s' % (type(e).__name__, str(e)[:200])
    if latency_ms is None:
        latency_ms = int(round((time.time() - t0) * 1000))

    try:
        m.record_probe(channel_id, ok, latency_ms, detail)
    except Exception:
        pass
    return {'ok': ok, 'data_ok': data_ok, 'latency_ms': latency_ms,
            'detail': detail, 'skipped': False}


def probe_all_channels(plugin=None, probe_target=None):
    """遍历全部 enabled 通道探活（含已熔断的 —— 探活可使其恢复，§7.6）。

    Returns:
        list[dict]: 每个通道一条结果，附 channel_id / name。
    """
    if probe_target is None:
        probe_target = _config_value('probe_target', '', plugin) or ''
    # 显式空串 = 未配置 → 不探活（与"参数省略"区分：省略才读 config）
    try:
        rows = m.list_channels_for_probe()
    except Exception as e:
        return [{'channel_id': None, 'name': '', 'ok': False,
                 'latency_ms': None, 'skipped': False,
                 'detail': '读取通道失败：%s' % e}]

    out = []
    for row in rows:
        c = dict(row)
        try:
            res = probe_channel(c, probe_target, plugin=plugin)
        except Exception as e:
            res = {'ok': False, 'latency_ms': None,
                   'detail': '%s: %s' % (type(e).__name__, str(e)[:200]),
                   'skipped': False}
        res['channel_id'] = c.get('id')
        res['name'] = c.get('name', '')
        out.append(res)

        # 探活结果**同时计分**：连续探活失败应当熔断该通道
        if not res.get('skipped') and c.get('id') is not None:
            try:
                thr = _int_config('fuse_threshold', 3, plugin)
                cool = _int_config('fuse_cooldown_minutes', 10, plugin)
                m.record_channel_result(c['id'], bool(res['ok']), thr, cool)
            except Exception:
                pass
    return out


def _int_config(key, default, plugin=None):
    raw = _config_value(key, default, plugin)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


# ═══════════════════════════════════════════════════════════════════════════
# 计分与健康候选
# ═══════════════════════════════════════════════════════════════════════════

def record_result(channel_id, ok, plugin=None):
    """记录一次出站结果并驱动熔断（§7.6）。

    阈值/冷却从插件 config 读取（`fuse_threshold` / `fuse_cooldown_minutes`），
    再交给 `models.record_channel_result` 做**原子单行 UPDATE**。
    """
    try:
        thr = _int_config('fuse_threshold', 3, plugin)
        cool = _int_config('fuse_cooldown_minutes', 10, plugin)
        return m.record_channel_result(channel_id, bool(ok), thr, cool)
    except Exception:
        return None


def pick_healthy(usage_tags=None, exclude_ids=None):
    """健康候选通道（enabled + 未熔断 + 标签匹配），按 weight 加权随机取一个。

    Args:
        usage_tags: 需要的用途标签（非空时经 LIKE 做 JSON 文本包含判定）。
        exclude_ids: 需排除的通道 id 集合。

    Returns:
        dict | None: 选中的通道行；无候选返回 None。
    """
    try:
        rows = m.pick_healthy_channels(usage_tags)
    except Exception:
        return None
    cands = [dict(r) for r in rows]
    if exclude_ids:
        ex = set(exclude_ids)
        cands = [c for c in cands if c['id'] not in ex]
    if not cands:
        return None
    # 复用 rules 的加权选择（同一算法，避免两份实现漂移）
    return R._weighted_pick(cands)


def fused_channels():
    """当前处于熔断冷却期的通道数（供仪表盘 / 巡检用）。"""
    try:
        return m.fused_channel_count()
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════════════════════
# max_concurrent：进程内 best-effort 信号量（§7.6，UI 注明为近似值）
# ═══════════════════════════════════════════════════════════════════════════

class MaxConcurrencyGate:
    """每通道一个计数信号量（**进程内**，多 worker 各自独立计数）。

    ⚠️ 已知局限（§7.6 原文「近似值」）：多 worker 部署下实际并发是
    `限制 × worker 数`。这里只做同 worker 内的保护，用于避免单进程内
    把某条通道打满。UI 需注明该口径。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._gates = {}

    def acquire(self, channel_id, limit):
        """尝试占用一个名额。limit<=0 表示不限，恒成功。

        Returns:
            bool: True 表示已占用（需配对 release）；False 表示已达上限。
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return True
        with self._lock:
            used = self._gates.get(channel_id, 0)
            if used >= limit:
                return False
            self._gates[channel_id] = used + 1
            return True

    def release(self, channel_id, limit):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return
        with self._lock:
            used = self._gates.get(channel_id, 0)
            if used <= 1:
                self._gates.pop(channel_id, None)
            else:
                self._gates[channel_id] = used - 1

    def in_use(self, channel_id):
        with self._lock:
            return self._gates.get(channel_id, 0)


# 模块级单例（进程内共用）
_gate = MaxConcurrencyGate()


def get_gate():
    return _gate
