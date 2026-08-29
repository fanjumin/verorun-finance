#!/usr/bin/env python3
"""Register the before_prompt_resolve filter and inject the memory block."""

import logging

logger = logging.getLogger('memory_engine.injector')

FILTER_NAME = 'before_prompt_resolve'


class PromptInjector:
    """Adds the curated memory block to the resolved system prompt."""

    def __init__(self, config: dict):
        self._config = config or {}
        self._retriever = None  # lazy-init

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
        try:
            block = self._retrieve.build_injection_block(user_id, agent_id, query)
            if not block:
                return prompt
            return f"{prompt}\n\n=== Agent Memory (auto) ===\n{block}\n=== Memory End ==="
        except Exception as e:
            logger.warning('memory injection skipped: %s', e)
            return prompt

    def _user_opted_in(self, user_id: str) -> bool:
        """Privacy gate: default from config; per-user override stored in meta.

        Reads the user's consent flag from public.user_profiles.meta (read-only);
        missing value falls back to config default.
        """
        default = self._config.get('memory_opt_in_default', True)
        try:
            from agent_matrix.models import get_db
            with get_db() as conn:
                row = conn.execute(
                    "SELECT meta FROM public.user_profiles WHERE user_id = ?",
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
