#!/usr/bin/env python3
"""memory_engine plugin entry point — MemoryEnginePlugin (BasePlugin).

Lifecycle:
  install → schema + migrations
  enable  → kernel patch check + wire services
  activate → event listeners + filter + scheduler jobs
  deactivate → unsubscribe everything
  uninstall → DROP SCHEMA memory_engine CASCADE
"""

import logging
import os

from plugin_manager.base import BasePlugin

from .models import SCHEMA
from .prompt_injector import PromptInjector, FILTER_NAME

logger = logging.getLogger('memory_engine')

# Event name provided by kernel patch B; absent -> plugin refuses to enable.
try:
    from plugin_manager.event_bus import EventName
    AGENT_TASK_COMPLETED = getattr(EventName, 'AGENT_TASK_COMPLETED', None)
except ImportError:
    AGENT_TASK_COMPLETED = None


class MemoryEnginePlugin(BasePlugin):
    name = 'CogEvolution'
    version = '1.1.0'
    description = 'Hierarchical agent memory, Reflexion-based self-evolution and prompt metrics.'
    author = 'VeroRun'

    # ── lifecycle ──────────────────────────────────────────────

    def on_install(self, registry) -> bool:
        """Create schema and run migrations (idempotent)."""
        try:
            return self.migrate('0.0.0', self.version)
        except Exception as e:
            logger.error('schema init failed: %s', e)
            return False

    def on_enable(self, registry) -> bool:
        """Health check kernel patches; register hooks; wire services."""
        if AGENT_TASK_COMPLETED is None:
            logger.error(
                'kernel patch B missing: EventName.AGENT_TASK_COMPLETED undefined'
            )
            return False
        # before_prompt_resolve 过滤器点由内核 prompt_resolver.resolve() 调用
        # （agent_matrix/prompt_resolver.py _apply_prompt_filters），
        # 注入器在 activate() 阶段注册过滤器，此处无需预检。
        self._config = self.get_config_value('config') or {}
        from .services.extractor import MemoryExtractor
        from .services.reflexion import ReflexionService
        from .services.prompt_evolution import PromptEvolutionService
        self._extractor = MemoryExtractor(self._config)
        self._reflexion = ReflexionService(self._config)
        self._injector = PromptInjector(self._config)
        self._evolution = PromptEvolutionService()
        return True

    def activate(self):
        """Subscribe events, filters and scheduler jobs."""
        from plugin_manager.event_bus import get_event_bus
        if AGENT_TASK_COMPLETED:
            if self._reflexion:
                get_event_bus().on(AGENT_TASK_COMPLETED, self._reflexion.on_task_completed)
            if self._extractor:
                get_event_bus().on(AGENT_TASK_COMPLETED, self._on_task_completed)
        if self._injector:
            self._injector.register()
        logger.info('memory_engine activated')

    def _on_task_completed(self, **kwargs):
        """Route AGENT_TASK_COMPLETED to the memory extractor (mirror of reflexion hook)."""
        if not self._extractor:
            return
        task = kwargs.get('task') or {}
        result = kwargs.get('result') or {}
        agent_id = kwargs.get('agent_id') or ''
        if not agent_id or not task:
            return
        self._extractor.submit(task, result, agent_id)

    def deactivate(self):
        """Unsubscribe everything (disable path)."""
        from plugin_manager.event_bus import get_event_bus
        if AGENT_TASK_COMPLETED:
            bus = get_event_bus()
            if self._reflexion:
                try:
                    bus.off(AGENT_TASK_COMPLETED, self._reflexion.on_task_completed)
                except Exception:
                    pass
            if self._extractor:
                try:
                    bus.off(AGENT_TASK_COMPLETED, self._on_task_completed)
                except Exception:
                    pass
        if self._injector:
            self._injector.unregister()
        logger.info('memory_engine deactivated')

    def on_uninstall(self, registry) -> bool:
        """Zero-residue uninstall: drop plugin schema;
        agents auto-unregistered by manager.
        """
        from .models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            conn.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
            conn.commit()
        except Exception as e:
            logger.error('schema drop failed: %s', e)
            conn.rollback()
        finally:
            conn.close()
        return True

    # ── registration hooks (standard) ─────────────────────────

    def register_routes(self) -> list:
        from .routes import bp
        return [bp]

    def register_jobs(self) -> list:
        """Daily 02:10 prompt-metrics aggregation (APScheduler dict)."""
        return [{
            'id': 'memory_engine_daily_evolution',
            'func': self._evolution.run_daily,
            'trigger': 'cron',
            'hour': 2,
            'minute': 10,
        }]

    def get_event_handlers(self) -> dict:
        """Declarative alternative to manual on(); activate() wires directly."""
        return {}

    def get_dashboard_stats(self) -> dict:
        from .models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            memories = conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE status = 'active'"
            ).fetchone()
            reflexions = conn.execute(
                "SELECT COUNT(*) AS n FROM reflexion_logs"
            ).fetchone()
            injected = conn.execute(
                "SELECT COUNT(*) AS n FROM memories"
                " WHERE last_hit_at > CURRENT_DATE"
            ).fetchone()
            suggestions = conn.execute(
                "SELECT COUNT(*) AS n FROM prompt_metrics"
                " WHERE sample_count >= 10"
            ).fetchone()
            return {
                'memories_total': memories['n'] or 0,
                'reflexions_total': reflexions['n'] or 0,
                'injections_today': injected['n'] or 0,
                'evolution_suggestions': suggestions['n'] or 0,
            }
        finally:
            conn.close()

    # ── migrations (standard §10.6) ───────────────────────────

    def get_schema_version(self) -> str:
        from .models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            row = conn.execute(
                "SELECT version FROM schema_version ORDER BY applied_at DESC LIMIT 1"
            ).fetchone()
            return row['version'] if row else '0.0.0'
        except Exception:
            return '0.0.0'
        finally:
            conn.close()

    def migrate(self, from_version: str, to_version: str) -> bool:
        """Apply migrations/ SQL files in order, transaction-wrapped."""
        from .models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            conn.execute("CREATE SCHEMA IF NOT EXISTS %s" % SCHEMA)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                " version    varchar(128) PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.execute("SET search_path TO %s, public" % SCHEMA)
            migrations_dir = os.path.join(os.path.dirname(__file__), 'migrations')
            for fname in sorted(os.listdir(migrations_dir)):
                if not fname.endswith('.sql'):
                    continue
                applied = conn.execute(
                    "SELECT 1 FROM schema_version WHERE version = ?", (fname,)
                ).fetchone()
                if applied:
                    continue
                fpath = os.path.join(migrations_dir, fname)
                with open(fpath, 'r', encoding='utf-8') as f:
                    conn.execute(f.read())
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (fname,)
                )
                logger.info('migration applied: %s', fname)
            conn.commit()
            return True
        except Exception as e:
            logger.error('migration failed: %s', e)
            conn.rollback()
            return False
        finally:
            conn.close()
