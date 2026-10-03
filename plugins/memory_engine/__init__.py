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
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
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
        self._evolution = PromptEvolutionService(self._config)
        from .services.sedimentation import SedimentationService, set_service
        self._sedimentation = SedimentationService(self._config)
        set_service(self._sedimentation)
        from .services.forgetting import ForgettingService
        self._forgetting = ForgettingService(self._config)
        from .services.abtest import AbTestService
        self._abtest = AbTestService(self._config)
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
        # abtest 结局记录与提取共用同一事件入口（_on_task_completed），无需额外订阅
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
        # P2 A/B：记录结局（含对照臂；同步单行 INSERT，异常静默）
        if getattr(self, '_abtest', None):
            self._abtest.record_outcome(
                task, result, agent_id,
                injected_len=getattr(self._injector, '_last_injected_len', 0))

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
            return True
        except Exception as e:
            logger.error('schema drop failed: %s', e)
            conn.rollback()
            return False
        finally:
            conn.close()

    # ── registration hooks (standard) ─────────────────────────

    def register_routes(self) -> list:
        from .routes import bp, user_bp
        return [bp, user_bp]

    def register_jobs(self) -> list:
        """Daily 02:10 prompt-metrics aggregation (APScheduler dict)."""
        jobs = [{
            'id': 'memory_engine_daily_evolution',
            'func': self._evolution.run_daily,
            'trigger': 'cron',
            'hour': 2,
            'minute': 10,
        }]
        if getattr(self, '_sedimentation', None):
            jobs.append({
                'id': 'memory_engine_daily_sedimentation',
                'func': self._sedimentation.run_daily,
                'trigger': 'cron',
                'hour': 2,
                'minute': 40,
            })
        if getattr(self, '_forgetting', None):
            jobs.append({
                'id': 'memory_engine_daily_forgetting',
                'func': self._forgetting.run_daily,
                'trigger': 'cron',
                'hour': 3,
                'minute': 10,
            })
        return jobs

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
            # CE-D1 方案 A：探测 pgvector，可用 -> vector(<provider dim>)，不可用 -> TEXT 降级关键词检索
            try:
                _vrow = conn.execute(
                    "SELECT 1 FROM pg_available_extensions WHERE name='vector'"
                ).fetchone()
                _vec = _vrow is not None
            except Exception:
                _vec = False
            # 路 A：维度从 embedding provider 动态解析（换模型改配置即可，不写死）
            _dim = 1536
            try:
                from plugins._base.embeddings import EmbeddingService
                # F4 修复：原句引用未定义变量 config（NameError 被 except 吞掉，
                # 动态维度解析从未生效）。migrate() 可能在 on_install 路径被调用
                # （早于 on_enable 设置 _config），故用 getattr 兜底。
                _cfg = {'module': 'memory_engine'}
                _cfg.update(getattr(self, '_config', None) or {})
                _dim = int(EmbeddingService(_cfg).dim or 1536)
            except Exception as e:
                logger.warning('resolve embedding dim for memory_engine failed, fallback 1536: %s', e)
            _col_type = ('vector(%d)' % _dim) if _vec else 'TEXT'
            logger.info('memory_engine embedding column type: %s', _col_type)
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
            # embedding 列动态追加（幂等）
            try:
                conn.execute(
                    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding %s" % _col_type
                )
            except Exception as _e:
                logger.warning('embedding column add failed (fallback TEXT): %s', _e)
                try:
                    conn.execute(
                        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding TEXT"
                    )
                except Exception as _e2:
                    logger.error('embedding column add failed: %s', _e2)
            conn.commit()
            return True
        except Exception as e:
            logger.error('migration failed: %s', e)
            conn.rollback()
            return False
        finally:
            conn.close()
