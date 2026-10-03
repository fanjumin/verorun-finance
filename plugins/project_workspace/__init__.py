#!/usr/bin/env python3
"""project_workspace plugin entry point — ProjectWorkspacePlugin (BasePlugin).

Lifecycle:
  install   -> schema + migrations
  enable    -> wire services
  activate  -> event listeners + scheduler jobs
  deactivate -> unsubscribe everything
  uninstall -> DROP SCHEMA project_workspace CASCADE
"""

import logging
import os

from plugin_manager.base import BasePlugin

from .models import SCHEMA, get_db

logger = logging.getLogger('project_workspace')


class ProjectWorkspacePlugin(BasePlugin):
    name = 'Project Workspace'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'AI-powered project workspace with project isolation, document RAG, intelligent search, and research assistant for organizations of all sizes'
    author = 'VeroRun'

    # -- lifecycle ----------------------------------------------------------

    def on_install(self, registry) -> bool:
        """Create schema and run migrations (idempotent)."""
        try:
            return self.migrate('0.0.0', self.version)
        except Exception as e:
            logger.error('schema init failed: %s', e)
            return False

    def on_enable(self, registry) -> bool:
        """Wire services (re-run idempotent migrations to self-heal missing schema)."""
        try:
            self.migrate('0.0.0', self.version)
        except Exception as e:
            logger.error('schema init failed: %s', e)
        self._config = self.get_config_value('config') or {}
        from .services.doc_processor import DocProcessor
        from .services.retriever import KnowledgeRetriever
        from .services.researcher import ResearchService
        self._doc_processor = DocProcessor(self._config)
        self._retriever = KnowledgeRetriever(self._config)
        self._researcher = ResearchService(self._config)
        return True

    def activate(self):
        """Subscribe scheduler jobs."""
        logger.info('project_workspace activated')

    def deactivate(self):
        """Unsubscribe everything (disable path)."""
        logger.info('project_workspace deactivated')

    def on_uninstall(self, registry) -> bool:
        """Zero-residue uninstall: drop plugin schema."""
        conn = get_db()
        try:
            conn.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
            conn.commit()
        except Exception as e:
            logger.error('schema drop failed: %s', e)
            conn.rollback()
        finally:
            conn.close()
        return True

    # -- registration hooks (standard) -------------------------------------

    def register_routes(self) -> list:
        from .routes import bp
        return [bp]

    def register_jobs(self) -> list:
        """Daily 03:00 cleanup expired chunks and stale documents."""
        from .services.doc_processor import DocProcessor
        dp = DocProcessor(self._config if hasattr(self, '_config') else {})
        return [{
            'id': 'project_workspace_cleanup',
            'func': dp.cleanup_stale_documents,
            'trigger': 'cron',
            'hour': 3,
            'minute': 0,
        }]

    def get_event_handlers(self) -> dict:
        return {
            'project_workspace.document_ready': self._on_document_ready,
        }

    def _on_document_ready(self, **payload):
        """文档处理完成 → 通知上传者（event_bus 以 handler(**kwargs) 调用）。"""
        document_id = payload.get('document_id', '')
        project_id = payload.get('project_id', '')
        logger.info('document ready: %s (project=%s)', document_id, project_id)
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT uploaded_by, original_name FROM documents WHERE id = ?",
                (document_id,)
            ).fetchone()
            if row and row['uploaded_by']:
                try:
                    from services.notification_service import create_notification
                    create_notification(
                        row['uploaded_by'],
                        'project_workspace',
                        '文档处理完成',
                        '「%s」已就绪，可进行搜索与问答' % (
                            row.get('original_name') or document_id)
                    )
                except ImportError:
                    logger.debug('notification_service not available, skipping notify')
                except Exception as e:
                    logger.error('notification failed: %s', e)
        finally:
            conn.close()

    def register_dag_nodes(self) -> dict:
        """注册两个工作流节点供 DAG 编排。"""
        return {
            'project_doc_process': self._dag_doc_process,
            'project_qa': self._dag_project_qa,
        }

    def _dag_doc_process(self, params: dict) -> dict:
        """工作流节点：触发指定文档的异步处理。"""
        doc_id = params.get('document_id', '')
        project_id = params.get('project_id', '')
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT filename, original_name FROM documents WHERE id = ?",
                (doc_id,)
            ).fetchone()
            if not row:
                return {'ok': False, 'error': 'document not found'}
            from .routes import _load_config, _submit_doc_task
            from .services.doc_processor import resolve_storage_dir
            config = _load_config()
            filepath = os.path.join(resolve_storage_dir(config), row['filename'])
            _submit_doc_task(doc_id, project_id, filepath, row['original_name'])
            return {'ok': True, 'document_id': doc_id, 'status': 'submitted'}
        finally:
            conn.close()

    def _dag_project_qa(self, params: dict) -> dict:
        """工作流节点：对项目文档提问并返回带来源的答案。"""
        query = params.get('query', '')
        project_id = params.get('project_id', '')
        top_k = int(params.get('top_k', 5))
        if not query or not project_id:
            return {'ok': False, 'error': 'query and project_id required'}
        retriever = self._retriever if hasattr(self, '_retriever') else None
        if not retriever:
            return {'ok': False, 'error': 'retriever not initialized'}
        chunks = retriever.retrieve(query, project_id, top_k)
        if not chunks:
            return {'ok': True, 'answer': '', 'sources': [], 'notice': 'no results'}
        result = (self._researcher.answer_question(query, chunks)
                  if hasattr(self, '_researcher') else {'ok': False, 'answer': ''})
        return {
            'ok': True,
            'answer': result.get('answer', ''),
            'sources': [{'id': c['id'], 'filename': c.get('original_name', ''),
                          'excerpt': c.get('content', '')[:200]} for c in chunks],
        }

    def register_health_checks(self) -> list:
        """注册两个健康检查。"""
        checks = []
        try:
            if hasattr(self, '_retriever') and self._retriever:
                embed = getattr(self._retriever, '_embed', None)
                checks.append({
                    'id': 'project_workspace_embedding',
                    'name': 'PW Embedding Service',
                    'check': lambda: embed.is_ready() if embed else False,
                })
        except Exception:
            pass
        try:
            conn = get_db()
            try:
                conn.execute("SELECT 1 FROM document_chunks LIMIT 0").fetchall()
                checks.append({
                    'id': 'project_workspace_db',
                    'name': 'PW Database',
                    'check': lambda: True,
                })
            except Exception:
                checks.append({
                    'id': 'project_workspace_db',
                    'name': 'PW Database',
                    'check': lambda: False,
                })
            finally:
                conn.close()
        except Exception:
            pass
        return checks

    def register_hooks(self):
        """实现 plugin.json 声明的 project_workspace/search hook。"""
        from plugin_manager.hooks import get_hook_registry
        get_hook_registry().add_filter(
            'project_workspace/search', self._hook_search_filter,
            priority=10, identifier='project_workspace')

    def _hook_search_filter(self, query: str, project_id: str = '',
                            top_k: int = 10, **kwargs):
        """外部插件通过 hook 调用项目搜索。"""
        retriever = self._retriever if hasattr(self, '_retriever') else None
        if not retriever:
            return {'ok': False, 'results': [], 'error': 'retriever not initialized'}
        results = retriever.retrieve(query, project_id, top_k)
        return {'ok': True, 'results': results}

    def get_dashboard_stats(self) -> dict:
        conn = get_db()
        try:
            projects = conn.execute(
                "SELECT COUNT(*) AS n FROM projects WHERE status = 'active'"
            ).fetchone()
            documents = conn.execute(
                "SELECT COUNT(*) AS n FROM documents WHERE status = 'ready'"
            ).fetchone()
            queries = conn.execute(
                "SELECT COUNT(*) AS n FROM qa_logs"
                " WHERE created_at > CURRENT_DATE"
            ).fetchone()
            return {
                'total_projects': projects['n'] or 0,
                'total_documents': documents['n'] or 0,
                'queries_24h': queries['n'] or 0,
                'avg_response_time': 0,
            }
        finally:
            conn.close()

    # -- migrations (standard sec.10.6) ------------------------------------

    def get_schema_version(self) -> str:
        conn = get_db()
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
        conn = get_db()
        try:
            conn.execute("CREATE SCHEMA IF NOT EXISTS %s" % SCHEMA)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                " version    varchar(64) PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            # VR-PLG-003：版本键为迁移文件名（如 v1.0.0_to_v1.1.0.sql 共 20 字符），
            # varchar(16) 会导致 INSERT 超长失败并使整个迁移事务回滚（表全部消失，
            # 运行时「relation projects does not exist」）。幂等加宽兼容旧库。
            conn.execute(
                "ALTER TABLE IF EXISTS schema_version ALTER COLUMN version TYPE varchar(64)"
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
