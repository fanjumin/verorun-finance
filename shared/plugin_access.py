# shared/plugin_access.py —— 核心侧唯一许可的插件触点
"""核心 → 插件 的唯一受控访问入口。

规则（verorun-dev-insight 门禁 boundary-violation）：
核心模块禁止出现 `import plugins.*` 静态语句；一切插件调用经本模块动态解析、优雅降级。

两类失败分流：
- 插件缺失（未安装/被物理卸载）→ logger.warning，返回 default，调用方按"未安装"语义降级
- 插件内部错误（已加载但调用抛错）→ logger.error，返回 default，调用方按"本次失败"降级

注意：本模块不改变"停用插件"的既有语义（停用只改注册表状态，模块仍可正常导入）。
"""
from __future__ import annotations

import importlib
import logging
import time

logger = logging.getLogger('plugin_access')

_MISSING_TTL = 300  # 缺失负缓存 5 分钟，避免每请求重复 import 尝试
_missing: dict = {}

# 插件触点登记表：核心允许触达的插件模块全集（新增触点 = 改这张表并写明用途）
PLUGIN_TOUCHPOINTS = {
    'plugins.email.services': '邮件发送（admin / auth / orchestrator）',
    'plugins.subscription.scheduler': '订阅续费调度（admin，P3 收权前过渡）',
    'plugins.oauth_config.services.oauth_service': 'OAuth 初始化（auth_blueprint）',
    'plugins.oauth_config.services.douyin_service': '抖音小程序登录（含精简版 stub 语义）',
    'plugins.health_check.routes': '健康检查蓝图（health_service，精简版裁剪语义）',
    'plugins.health_check.models': '健康巡检数据（health_service / agent tools）',
    'plugins.health_check.alerter': '兜底告警（agent_matrix.failover）',
    'plugins.chatbot.models': '客服机器人配置 / Agent（main_site / api_v1）',
    'plugins.chatbot.routes': '转人工 / 工单（api_v1）',
    'plugins.chatbot.stats': '对话会话落库（main_site 流式对话）',
    'plugins.chatbot.service': '统一对话引擎（site_builder 站点浮窗 site-chat）',
    'plugins.chatbot.threads': '对话会话归属与持久化（main_site 统一引擎桥接，P1-4）',
    'plugins.site_builder.engine': '站点引擎（agent site_build）',
    'plugins.site_builder.models': '提示词模板（agent site_build）',
    'plugins.site_builder.site_settings.models': '设计令牌（internal_api）',
    'plugins.ads.ai_tools': '广告 AI 工具（agent tools / routes）',
    'plugins.analytics.tracker': '统计洞察（agent tools）',
    'plugins.coupons': '优惠券引擎（completion_service）',
    'plugins.payment.gateways.alipay': '支付宝网关（shop 支付）',
}


class PluginTouchpointDenied(ImportError):
    """核心访问了未登记的插件模块 —— 说明有人绕过登记表新增了依赖。"""


def optional_import(dotted: str, feature: str = ''):
    """动态解析插件模块。未登记 → 直接拒绝；缺失 → None（带负缓存）。

    Args:
        dotted: 形如 'plugins.email.services' 的模块路径，必须在 PLUGIN_TOUCHPOINTS 内
        feature: 调用方标签，仅用于日志定位
    """
    if dotted not in PLUGIN_TOUCHPOINTS:
        raise PluginTouchpointDenied(
            f'未登记的插件触点: {dotted}（请更新 PLUGIN_TOUCHPOINTS 并注明用途）')
    ts = _missing.get(dotted)
    if ts and time.time() - ts < _MISSING_TTL:
        return None
    try:
        return importlib.import_module(dotted)
    except ImportError:
        _missing[dotted] = time.time()
        logger.warning('plugin module missing: %s%s', dotted,
                       f' ({feature})' if feature else '')
        return None


def get_attr(dotted: str, name: str, default=None, feature: str = ''):
    """等价替换 `from dotted import name`：模块或属性缺失一律返回 default。"""
    mod = optional_import(dotted, feature=feature)
    if mod is None:
        return default
    try:
        return getattr(mod, name)
    except AttributeError:
        logger.error('[%s] %s 缺少属性 %s', feature or 'n/a', dotted, name)
        return default


def call_plugin(dotted: str, func: str, *args, default=None, feature: str = '', **kwargs):
    """单点调用 → (ok, result)。缺失 = warning + default；内部错误 = error + default。"""
    mod = optional_import(dotted, feature=feature)
    if mod is None:
        return False, default
    try:
        return True, getattr(mod, func)(*args, **kwargs)
    except Exception as e:
        logger.error('[%s] %s.%s failed: %s', feature or 'n/a', dotted, func, e)
        return False, default


def invalidate(dotted: str = ''):
    """插件安装/启用后清负缓存（预留给 PluginManager.install/enable 挂钩）。"""
    if dotted:
        _missing.pop(dotted, None)
    else:
        _missing.clear()
