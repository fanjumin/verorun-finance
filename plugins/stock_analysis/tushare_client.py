"""tushare_client.py — Tushare token 解析 + pro_api 单例 + 接口权限探针（阶段2）

- token 为用户自备：优先级 环境变量 TUSHARE_TOKEN > PluginManager 持久化配置
  （管理后台插件设置页 tushare_token）> config.yaml 的 TUSHARE_TOKEN。
  空 = 未配置，探针返回空集，整条 tushare 链自动跳过（行为回退免费源 akshare→sina）。
- 探针：进程启动后首用执行一次，结果缓存至进程生命周期；
  单接口失败（权限不足）只剔除该类别，gateway 据此跳过无权限数据类别。
- token 不进日志（日志仅脱敏 tail 两位）。
"""

import logging
import os

_log = logging.getLogger("stock_analysis.tushare_client")

# ── 探针矩阵：类别 → (pro 方法名, 最小探针参数) ──
# 具体积分档位以 tushare.pro 官方文档实时核对为准，本模块不固化积分数字；
# 探针按 token 实测可用集合裁剪，任何积分档位都能优雅上线。
_PROBE_METHODS = {
    "kline": ("daily", {"ts_code": "600519.SH", "start_date": "20260105", "end_date": "20260109"}),
    # P2-2 修复：探针与 fetch_fundamental 同门槛（income/balance/cashflow/fina_indicator），
    # 避免低积分 token 探针误报可用、运行期四表调用被拒
    "fundamental": ("fina_indicator", {"ts_code": "600519.SH"}),
    "moneyflow": ("moneyflow", {"ts_code": "600519.SH", "start_date": "20260105", "end_date": "20260109"}),
    "consensus": ("forecast_vip", {"ts_code": "600519.SH"}),
    "toplist": ("top_list", {"ts_code": "600519.SH", "start_date": "20260105", "end_date": "20260109"}),
    "margin": ("margin", {"ts_code": "600519.SH", "start_date": "20260105", "end_date": "20260109"}),
    "northbound": ("moneyflow_hsgt", {"start_date": "20260105", "end_date": "20260109"}),
    "sharefloat": ("share_float", {"ts_code": "600519.SH"}),
    "holdernumber": ("stk_holdernumber", {"ts_code": "600519.SH"}),
}

_PRO = None      # pro_api 单例（token 绑定）
_TOKEN = None    # 已解析 token（进程生命周期缓存）
_CAPS = None     # 探针结果缓存


def _read_config_token() -> str:
    """从 config.yaml 读取用户自备 token（兼容大小写键）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
    except Exception as err:
        _log.warning("config.yaml 读取失败: %s", err)
        return ""
    for key in ("TUSHARE_TOKEN", "tushare_token"):
        val = loaded.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _read_persisted_token() -> str:
    """从 PluginManager 持久化配置读取 tushare_token（管理后台插件设置页录入）。

    依赖 Flask app 上下文（pm 挂在 current_app.extensions）；无上下文/未启用时
    返回空串，调用方继续走 config.yaml 兜底。异常一律吞掉不阻塞探针。
    """
    try:
        from flask import current_app
        pm = current_app.extensions.get("plugin_manager")
        if pm is None:
            return ""
        cfg = pm.get_config("stock_analysis") or {}
        val = cfg.get("tushare_token")
        return val.strip() if isinstance(val, str) and val.strip() else ""
    except Exception:
        return ""


def resolve_token() -> str:
    """读取用户自备 Tushare token：优先级 环境变量 > PluginManager 持久化配置（设置页）> config.yaml。

    未配置抛 RuntimeError。
    """
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        token = _read_persisted_token()
    if not token:
        token = _read_config_token()
    if not token:
        raise RuntimeError(
            "Tushare token 未配置：请在你的插件设置/配置中填写 tushare_token"
            "（留空则自动使用免费数据源 akshare→sina）")
    _TOKEN = token
    _log.info("tushare token resolved (len=%s, tail=%s***)", len(token), token[-2:])
    return token


def get_pro():
    """懒加载 pro_api 单例（token 绑定）。"""
    global _PRO
    if _PRO is None:
        import tushare as ts
        _PRO = ts.pro_api(resolve_token())
    return _PRO


def probe_capabilities() -> set:
    """返回当前 token 实测可用的数据类别集合；进程启动后首用执行一次并缓存。

    无 token / DB 不可用时返回空集，调用方（provider.supports）据此跳过 tushare。
    """
    global _CAPS
    if _CAPS is not None:
        return _CAPS
    try:
        pro = get_pro()
    except Exception as err:
        _log.warning("tushare probe skipped (token unavailable): %s", err)
        _CAPS = set()
        return _CAPS
    ok = set()
    for category, (method, params) in _PROBE_METHODS.items():
        try:
            getattr(pro, method)(**params)
            ok.add(category)
        except Exception as err:
            _log.warning("tushare probe %s failed: %s", category, err)
    _CAPS = ok
    _log.info("tushare capabilities: %s", sorted(ok))
    return ok
