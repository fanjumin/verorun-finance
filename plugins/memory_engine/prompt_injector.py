#!/usr/bin/env python3
"""Register the before_prompt_resolve filter and inject the memory block."""

import logging

logger = logging.getLogger('memory_engine.injector')

FILTER_NAME = 'before_prompt_resolve'


def user_opted_in(user_id, config: dict) -> bool:
    """隐私同意门（模块级，注入读路径与 extractor/reflexion 写路径共用）。

    - 空 owner 一律 fail-closed（False），杜绝匿名/无主数据落库；
    - 默认值取 config.memory_opt_in_default，用户可在
      public.user_profiles.meta.memory_opt_in 逐人覆盖；
    - 只读主库；任何异常回退配置默认值（与既有注入容错口径一致）。
    """
    if not user_id:
        return False
    config = config or {}
    default = config.get('memory_opt_in_default', True)
    try:
        from agent_matrix.models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT meta FROM public.user_profiles WHERE user_id = %s",
                (user_id,),
            ).fetchone()
        if not row:
            return default
        import json as _json
        meta = row['meta'] or {}
        if isinstance(meta, str):
            meta = _json.loads(meta)
        return bool(meta.get('memory_opt_in', default))
    except Exception:
        return default


class PromptInjector:
    """Adds the curated memory block to the resolved system prompt."""

    def __init__(self, config: dict):
        self._config = config or {}
        self._retriever = None  # lazy-init
        self._last_injected_len = 0   # 最近一次实际注入块长（abtest 结局记录回查）

    @property
    def _retrieve(self):
        if self._retriever is None:
            from .services.retriever import MemoryRetriever
            self._retriever = MemoryRetriever(self._config)
        return self._retriever

    def register(self):
        """Subscribe to the kernel filter (patch C must be present)."""
        from plugin_manager.hooks import get_hook_registry
        get_hook_registry().add_filter(
            FILTER_NAME, self._inject, priority=10, identifier='memory_engine'
        )

    def unregister(self):
        """Remove the filter subscription."""
        from plugin_manager.hooks import get_hook_registry
        try:
            get_hook_registry().remove_filter(
                FILTER_NAME, callback=self._inject, identifier='memory_engine'
            )
        except Exception:
            pass

    def _inject(self, value, **kwargs):
        """Filter callback: (value, **kwargs) -> value."""
        prompt = value
        ctx = kwargs.get('ctx') or {}
        user_id = ctx.get('user_id')
        agent_id = ctx.get('agent_id')
        query = ctx.get('user_query') or ''
        if not user_id or not self._user_opted_in(user_id):
            return prompt
        # P2 A/B 门控：启用实验时，对照臂用户不注入（分流键=user_id，K2 约束）
        from .services.abtest import assign_arm, ARM_TREATMENT
        if self._config.get('abtest_enabled', False):
            if assign_arm(str(user_id),
                          int(self._config.get('abtest_control_pct', 50))) != ARM_TREATMENT:
                return prompt
        try:
            block = self._retrieve.build_injection_block(user_id, agent_id, query)
            if not block:
                return prompt
            self._last_injected_len = len(block)   # 事件侧回查注入量的缓存
            return f"{prompt}\n\n=== Agent Memory (auto) ===\n{block}\n=== Memory End ==="
        except Exception as e:
            logger.warning('memory injection skipped: %s', e)
            return prompt

    def _user_opted_in(self, user_id: str) -> bool:
        """Privacy gate: delegate to the shared module-level helper (F-02)."""
        return user_opted_in(user_id, self._config)
