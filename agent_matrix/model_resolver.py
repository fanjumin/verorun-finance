#!/usr/bin/env python3
"""
Agent Matrix — 模型选择策略解析器（§3）
========================================
按 plugin.json 的 model_policy 三层策略解析模型参数：

  tier     → system_config 查 model_tier_{tier}（provider_model_id），命中即用
  explicit → 策略内显式 provider + model
  inherit / fallback → 全局默认（ai_text_provider / ai_text_model，兜底 PROVIDER_CONFIGS）

统一入口，供各插件复用，避免在 content_factory / enterprise_verify 等处重复实现。
"""
from typing import Dict

import logging

logger = logging.getLogger(__name__)


def resolve_model_args(model_policy: dict,
                       default_provider: str = 'siliconflow',
                       default_model: str = 'deepseek-ai/DeepSeek-V3') -> dict:
    """按 model_policy 三层策略解析模型参数，返回 get_gateway().chat() 可用的 kwargs。

    参数:
        model_policy: plugin.json agents[].model_policy 声明的策略 dict（可为空 dict）
        default_provider: inherit/fallback 兜底用的默认 provider（兼容调用方旧默认值）
        default_model:    inherit/fallback 兜底用的默认 model
    """
    from agent_matrix.engine import _get_system_key
    strategy = (model_policy or {}).get('strategy', 'inherit')
    # 1. tier：读 system_config model_tier_{tier}（provider_model_id），命中则用之
    if strategy == 'tier':
        tier = model_policy.get('tier', 'standard')
        pm_id = _get_system_key(f'model_tier_{tier}')
        if pm_id:
            return {'provider_model_id': pm_id}
        # BUG-8：tier 未配置时不再静默降级，显式告警以便排查
        logger.warning('model_tier_%s 未在 system_config 配置，已降级到 inherit/fallback 路径', tier)
    # 2. explicit：策略内显式 provider+model
    if strategy == 'explicit':
        provider = model_policy.get('provider', '')
        model = model_policy.get('model', '')
        if provider and model:
            return {'provider': provider, 'model': model}
    # 3. inherit / fallback：全局默认（system_config，兜底 PROVIDER_CONFIGS）
    provider = _get_system_key('ai_text_provider') or default_provider
    model = _get_system_key('ai_text_model') or default_model
    return {'provider': provider, 'model': model}
