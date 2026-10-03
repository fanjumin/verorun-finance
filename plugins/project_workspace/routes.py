#!/usr/bin/env python3
"""Admin and API endpoints for project_workspace.

Blueprint prefix: /admin/project_workspace
Includes: projects CRUD, documents management, search, Q&A, citations, and Agent operations.

所有端点需登录（require_user）；项目级操作按语义附加 require_project_role：
  viewer  → 搜索 / 问答 / 文档查看 / 引用 / 历史
  editor  → 上传 / 删除文档
  owner   → 创建/删除项目 / 成员管理
路由内部一律使用 request._project_id / request._project_role（由守卫注入）。
"""

import json
import logging
import os
import uuid

from flask import Blueprint, current_app, jsonify, request

from .auth import _get_locale, _t, require_project_role, require_user
from .models import get_db

logger = logging.getLogger('project_workspace.routes')

bp = Blueprint('project_workspace_admin', __name__, url_prefix='/admin/project_workspace')

# 文档处理完成事件名（由异步任务在完成后发出）
PROJECT_DOCUMENT_READY = 'project_workspace.document_ready'


# -- helper ----------------------------------------------------------------

def _load_config() -> dict:
    """从 PluginManager 读取插件配置；失败时回退空配置（服务自动降级）。"""
    try:
        pm = current_app.extensions.get('plugin_manager')
        if pm:
            cfg = pm.get_config('project_workspace') or {}
            if isinstance(cfg, dict):
                return cfg
    except Exception:
        pass
    return {}


def _storage_dir() -> str:
    """插件文件存储目录（系统根目录 + config.storage_dir）。"""
    from .services.doc_processor import resolve_storage_dir
    return resolve_storage_dir(_load_config())


def _submit_doc_task(doc_id: str, project_id: str, filepath: str, filename: str):
    """通过 orchestrator Worker 池提交异步文档处理任务。

    任务函数为 _process_document_task；Worker 不可用时同步执行兜底，
    保证上传流程不中断。
    """
    config = _load_config()
    worker = current_app.config.get('AUTOMATION_WORKER')
    if worker is None:
        _process_document_task(doc_id, project_id, filepath, filename, config)
        return
    worker.submit_task(
        task_type='python',
        task_data={
            'func': _process_document_task,
            'kwargs': {
                'doc_id': doc_id,
                'project_id': project_id,
                'filepath': filepath,
                'filename': filename,
                'config': config,
            },
        },
        priority='NORMAL',
        task_id='pw_doc_%s' % doc_id,
    )


def _process_document_task(doc_id: str, project_id: str, filepath: str,
                           filename: str, config: dict):
    """异步文档处理任务：extract -> chunk -> embed -> store，完成后发事件。

    process_document 只 execute 不 commit，本函数统一提交，
    保证 documents.status 与 projects.doc_count 在同一事务内。
    """
    from .services.doc_processor import DocProcessor
    conn = get_db()
    try:
        processor = DocProcessor(config)
        ok = processor.process_document(doc_id, project_id, filepath, filename, conn)
        conn.execute(
            "UPDATE projects SET doc_count = ("
            " SELECT COUNT(*) FROM documents WHERE project_id = ? AND status = 'ready')"
            " WHERE id = ?",
            (project_id, project_id)
        )
        conn.commit()
        if ok:
            try:
                from plugin_manager.event_bus import get_event_bus
                get_event_bus().emit(PROJECT_DOCUMENT_READY,
                                     document_id=doc_id, project_id=project_id)
            except Exception as e:
                logger.error('document ready event dispatch failed: %s', e)
    except Exception as e:
        logger.error('async document processing failed: %s', e)
        try:
            conn.rollback()
            conn.execute(
                "UPDATE documents SET status = 'failed', error_msg = ?"
                " WHERE id = ?", (str(e)[:500], doc_id)
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


# -- i18n ----------------------------------------------------------------

@bp.route('/i18n')
@require_user
def plugin_i18n():
    """返回当前语言的插件翻译字典（供前端 t() 使用）。"""
    pm = current_app.extensions.get('plugin_manager')
    instance = pm.get_instance('project_workspace') if pm else None
    lang = _get_locale()
    translations = {}
    if instance is not None:
        data = getattr(instance, '_i18n_data', {}) or {}
        translations = data.get(lang, {}) or {}
    return jsonify({'ok': True, 'lang': lang, 'translations': translations})


# -- Projects -------------------------------------------------------------

@bp.route('/projects')
@bp.route('/projects/<owner_type>')
@require_user
def list_projects(owner_type=None):
    """List projects for the current user (owner or member)."""
    user_id = str(request._user.get('user_id', ''))
    if not owner_type:
        owner_type = request.args.get('owner_type', 'user')
    status = request.args.get('status', 'active')
    conn = get_db()
    try:
        sql = ("SELECT p.id, p.name, p.description, p.tags, p.visibility,"
               " p.status, p.member_count, p.doc_count, p.created_at, p.updated_at,"
               " COALESCE(m.role, '') AS my_role"
               " FROM projects p"
               " LEFT JOIN project_members m ON m.project_id = p.id AND m.user_id = ?"
               " WHERE p.status = ?"
               " AND (p.owner_id = ? OR m.user_id = ?)")
        params = [user_id, status, user_id, user_id]
        if owner_type:
            # placeholder order: m.user_id, p.status, p.owner_id, p.owner_type, m.user_id
            sql = sql.replace('p.owner_id = ?', 'p.owner_id = ? AND p.owner_type = ?', 1)
            params = [user_id, status, user_id, owner_type, user_id]
        sql += " ORDER BY p.updated_at DESC"
        rows = conn.execute(sql, params).fetchall()
        return jsonify({'ok': True, 'rows': [dict(r) for r in rows]})
    finally:
        conn.close()


@bp.route('/projects', methods=['POST'])
@bp.route('/projects/<owner_type>', methods=['POST'])
@require_user
def create_project(owner_type=None):
    """Create a new project (owner defaults to the current user)."""
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': _t('auth.error_name_required')}), 400
    owner_type = owner_type or data.get('owner_type', 'user')
    owner_id = (data.get('owner_id') or '').strip() or str(request._user.get('user_id', ''))
    description = data.get('description', '')
    tags = data.get('tags', [])
    conn = get_db()
    try:
        project_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO projects (id, owner_type, owner_id, name, description, tags)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, owner_type, owner_id, name, description, tags)
        )
        conn.execute(
            "INSERT INTO project_members (project_id, user_id, role)"
            " VALUES (?, ?, 'owner')",
            (project_id, owner_id)
        )
        conn.execute(
            "UPDATE projects SET member_count = 1 WHERE id = ?",
            (project_id,)
        )
        conn.commit()
        return jsonify({'ok': True, 'project_id': project_id})
    except Exception as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        conn.close()


@bp.route('/projects/<project_id>', methods=['DELETE'])
@require_user
@require_project_role('owner')
def delete_project(project_id):
    """Delete a project (cascade deletes all related data). Owner only."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM projects WHERE id = ?", (request._project_id,))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        conn.close()


# -- Project Members ------------------------------------------------------

@bp.route('/projects/<project_id>/members')
@require_user
@require_project_role('owner')
def list_members(project_id):
    """List project members. Owner only (成员管理)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, user_id, role, joined_at"
            " FROM project_members WHERE project_id = ?"
            " ORDER BY joined_at",
            (request._project_id,)
        ).fetchall()
        return jsonify({'ok': True, 'rows': [dict(r) for r in rows]})
    finally:
        conn.close()


@bp.route('/projects/<project_id>/members', methods=['POST'])
@require_user
@require_project_role('owner')
def add_member(project_id):
    """Add or update a project member. Owner only (成员管理)."""
    data = request.json or {}
    user_id = data.get('user_id', '')
    role = data.get('role', 'member')
    if not user_id:
        return jsonify({'ok': False, 'error': _t('auth.error_user_id_required')}), 400
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO project_members (project_id, user_id, role)"
            " VALUES (?, ?, ?) ON CONFLICT (project_id, user_id)"
            " DO UPDATE SET role = ?",
            (request._project_id, user_id, role, role)
        )
        conn.execute(
            "UPDATE projects SET member_count = ("
            " SELECT COUNT(*) FROM project_members WHERE project_id = ?)"
            " WHERE id = ?",
            (request._project_id, request._project_id)
        )
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        conn.close()


# -- Documents ------------------------------------------------------------

@bp.route('/documents')
@require_user
@require_project_role('viewer')
def list_documents():
    """List documents in a project (viewer+)."""
    project_id = request._project_id
    status = request.args.get('status', '')
    keyword = request.args.get('q', '')
    limit = int(request.args.get('limit', 50))
    offset = int(request.args.get('offset', 0))
    from .services.retriever import KnowledgeRetriever
    retriever = KnowledgeRetriever(_load_config())
    rows = retriever.retrieve_documents(project_id, status, limit, offset, keyword)
    return jsonify({'ok': True, 'rows': rows})


@bp.route('/documents', methods=['POST'])
@require_user
@require_project_role('editor')
def upload_document():
    """Upload a document (editor+). 立即返回 pending，实际处理异步进行。"""
    project_id = request._project_id
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': _t('doc.error_no_file')}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'ok': False, 'error': _t('doc.error_empty_filename')}), 400

    uploaded_by = str(request._user.get('user_id', ''))

    from .services.doc_processor import DocProcessor
    processor = DocProcessor(_load_config())

    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)
    ok, err = processor.validate_file(file.filename, file_size)
    if not ok:
        err_key = ('doc.error_unsupported' if err.startswith('Unsupported')
                   else 'doc.error_too_large')
        return jsonify({'ok': False, 'error': _t(err_key),
                        'error_code': err_key}), 400

    conn = get_db()
    try:
        doc_id = str(uuid.uuid4())
        ext = os.path.splitext(file.filename)[1].lstrip('.').lower()
        safe_name = doc_id + '.' + ext

        storage_dir = _storage_dir()
        os.makedirs(storage_dir, exist_ok=True)
        filepath = os.path.join(storage_dir, safe_name)
        file.save(filepath)

        conn.execute(
            "INSERT INTO documents"
            " (id, project_id, filename, original_name, file_ext, file_size,"
            "  mime_type, status, uploaded_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (doc_id, project_id, safe_name, file.filename, ext,
             file_size, file.content_type or '', uploaded_by)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        conn.close()

    # 异步处理：状态已为 pending，任务完成后置为 ready/failed
    _submit_doc_task(doc_id, project_id, filepath, file.filename)
    return jsonify({'ok': True, 'document_id': doc_id, 'status': 'pending',
                    'message': _t('doc.upload_success')})


@bp.route('/documents/<doc_id>', methods=['DELETE'])
@require_user
@require_project_role('editor')
def delete_document(doc_id):
    """Delete a document and its chunks (editor+)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT filename FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row and row['filename']:
            storage_path = os.path.join(_storage_dir(), row['filename'])
            if os.path.exists(storage_path):
                os.remove(storage_path)
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        conn.close()


@bp.route('/documents/<doc_id>/status')
@require_user
@require_project_role('viewer')
def document_status(doc_id):
    """Get document processing status (pending/processing/ready/failed)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status, error_msg, processed_at FROM documents WHERE id = ?",
            (doc_id,)
        ).fetchone()
        if not row:
            return jsonify({'ok': False, 'error': _t('doc.error_not_found')}), 404
        status = row['status'] or ''
        label_key = 'doc.status_%s' % status
        return jsonify({
            'ok': True,
            'document_id': doc_id,
            'status': status,
            'status_label': _t(label_key) if status in ('pending', 'processing', 'ready', 'failed') else status,
            'error_message': row.get('error_msg'),
            'processed_at': row.get('processed_at'),
        })
    finally:
        conn.close()


# -- Search / Q&A ---------------------------------------------------------

@bp.route('/search')
@require_user
@require_project_role('viewer')
def search():
    """Semantic search across project documents (viewer+)."""
    query = request.args.get('q', '')
    top_k = int(request.args.get('top_k', 10))
    cross_project = request.args.get('cross_project', 'false').lower() == 'true'

    if not query:
        return jsonify({'ok': False, 'error': _t('auth.error_query_required')}), 400

    from .services.embedding import EmbeddingService
    from .services.retriever import KnowledgeRetriever
    retriever = KnowledgeRetriever(_load_config())

    # 语义检索不可用时给出降级提示（关键词检索仍正常返回结果）
    notice = ''
    try:
        if not EmbeddingService(_load_config()).is_ready():
            notice = _t('search.fallback_keyword')
    except Exception:
        notice = _t('search.fallback_keyword')

    results = retriever.retrieve(
        query, request._project_id, top_k, cross_project,
        user_id=str(request._user.get('user_id', '')),
    )

    return jsonify({'ok': True, 'results': results, 'notice': notice})


@bp.route('/qa')
@require_user
@require_project_role('viewer')
def qa():
    """Ask a question and get an answer with source citations (viewer+)."""
    query = request.args.get('q', '')
    top_k = int(request.args.get('top_k', 10))
    project_id = request._project_id

    if not query:
        return jsonify({'ok': False, 'error': _t('auth.error_query_required')}), 400

    from .services.researcher import ResearchService
    from .services.retriever import KnowledgeRetriever
    retriever = KnowledgeRetriever(_load_config())
    researcher = ResearchService(_load_config())

    chunks = retriever.retrieve(query, project_id, top_k)
    if not chunks:
        return jsonify({
            'ok': True,
            'answer': _t('search.no_results'),
            'sources': [],
            'model_used': '',
            'tokens_used': 0,
        })

    result = researcher.answer_question(query, chunks)

    if result.get('ok'):
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO qa_logs"
                " (project_id, user_id, query, answer, sources, agent_id,"
                "  model_used, tokens_used, latency_ms)"
                " VALUES (?, ?, ?, ?, ?, 'workspace_assistant', ?, ?, ?)",
                (project_id, str(request._user.get('user_id', '')),
                 query, result.get('answer', ''),
                 json.dumps([{
                     'chunk_id': c['id'],
                     'doc_id': c['document_id'],
                     'score': c.get('similarity', 0),
                     'excerpt': c.get('content', '')[:200],
                 } for c in chunks]),
                 result.get('model_used', ''),
                 result.get('tokens_used', 0),
                 0)
            )
            conn.commit()
        except Exception as e:
            logger.error('qa log failed: %s', e)
        finally:
            conn.close()

    return jsonify({
        'ok': True,
        'answer': result.get('answer', ''),
        'sources': [{
            'id': c['id'],
            'document_id': c['document_id'],
            'filename': c.get('original_name', c.get('filename', '')),
            'content': c.get('content', '')[:500],
            'similarity': c.get('similarity', 0),
            'section_title': c.get('section_title', ''),
        } for c in chunks],
        'model_used': result.get('model_used', ''),
        'tokens_used': result.get('tokens_used', 0),
    })


# -- Citations ------------------------------------------------------------

@bp.route('/citations')
@require_user
@require_project_role('viewer')
def list_citations():
    """List citations in a project (viewer+)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT c.id, c.citation_key, c.title, c.authors, c.year,"
            " c.journal, c.doi, c.url, c.confidence,"
            " d.original_name AS document_name"
            " FROM citations c"
            " JOIN documents d ON d.id = c.document_id"
            " WHERE c.project_id = ?"
            " ORDER BY c.year DESC NULLS LAST, c.citation_key",
            (request._project_id,)
        ).fetchall()
        return jsonify({'ok': True, 'rows': [dict(r) for r in rows]})
    finally:
        conn.close()


# -- Q&A History ----------------------------------------------------------

@bp.route('/qa/history')
@require_user
@require_project_role('viewer')
def qa_history():
    """Get Q&A history for a project (viewer+)."""
    limit = int(request.args.get('limit', 50))
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, query, answer, sources, model_used, tokens_used,"
            " latency_ms, feedback, created_at"
            " FROM qa_logs WHERE project_id = ?"
            " ORDER BY created_at DESC LIMIT ?",
            (request._project_id, limit)
        ).fetchall()
        return jsonify({'ok': True, 'rows': [dict(r) for r in rows]})
    finally:
        conn.close()


@bp.route('/qa/feedback', methods=['POST'])
@require_user
@require_project_role('viewer')
def qa_feedback():
    """Submit feedback for a Q&A entry (viewer+)."""
    data = request.json or {}
    qa_id = data.get('qa_id', '')
    feedback = data.get('feedback', 0)
    if not qa_id:
        return jsonify({'ok': False, 'error': _t('auth.error_qa_id_required')}), 400
    conn = get_db()
    try:
        conn.execute(
            "UPDATE qa_logs SET feedback = ? WHERE id = ? AND project_id = ?",
            (feedback, qa_id, request._project_id)
        )
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        conn.close()


# -- Agent Operations -----------------------------------------------------

@bp.route('/agent/summarize', methods=['POST'])
@require_user
@require_project_role('viewer')
def agent_summarize():
    """Summarize a document using the Workspace Assistant Agent (viewer+)."""
    data = request.json or {}
    doc_id = data.get('document_id', '')
    if not doc_id:
        return jsonify({'ok': False, 'error': _t('auth.error_document_id_required')}), 400
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, original_name, summary FROM documents WHERE id = ?",
            (doc_id,)
        ).fetchone()
        if not row:
            return jsonify({'ok': False, 'error': _t('doc.error_not_found')}), 404
        if row['summary']:
            return jsonify({'ok': True, 'summary': row['summary']})
        chunks = conn.execute(
            "SELECT content FROM document_chunks WHERE document_id = ?"
            " ORDER BY chunk_index", (doc_id,)
        ).fetchall()
        if not chunks:
            return jsonify({'ok': False, 'error': _t('doc.error_no_content')}), 400
        full_text = '\n\n'.join(c['content'] for c in chunks)
        from .services.researcher import ResearchService
        researcher = ResearchService(_load_config())
        result = researcher.summarize_document(full_text, row['original_name'])
        if result.get('ok'):
            summary = result['answer'][:2000]
            conn.execute(
                "UPDATE documents SET summary = ? WHERE id = ?",
                (summary, doc_id)
            )
            conn.commit()
            return jsonify({'ok': True, 'summary': summary})
        return jsonify({'ok': False, 'error': _t('agent.error_summarize_failed')}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        conn.close()


@bp.route('/agent/compare', methods=['POST'])
@require_user
@require_project_role('viewer')
def agent_compare():
    """Compare multiple documents using the Workspace Assistant Agent (viewer+)."""
    data = request.json or {}
    doc_ids = data.get('document_ids', [])
    if len(doc_ids) < 2:
        return jsonify({'ok': False, 'error': _t('agent.error_compare_min_2')}), 400
    conn = get_db()
    try:
        docs = []
        for did in doc_ids:
            row = conn.execute(
                "SELECT original_name, summary FROM documents WHERE id = ?",
                (did,)
            ).fetchone()
            if row:
                chunks = conn.execute(
                    "SELECT content FROM document_chunks WHERE document_id = ?"
                    " ORDER BY chunk_index LIMIT 5",
                    (did,)
                ).fetchall()
                text = '\n'.join(c['content'][:2000] for c in chunks) if chunks else ''
                docs.append({'title': row['original_name'], 'text': text})
        if len(docs) < 2:
            return jsonify({'ok': False, 'error': _t('agent.error_compare_not_enough')}), 400
        from .services.researcher import ResearchService
        researcher = ResearchService(_load_config())
        result = researcher.compare_documents(docs)
        return jsonify({
            'ok': result.get('ok', False),
            'comparison': result.get('answer', ''),
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        conn.close()


# -- Dashboard Stats ------------------------------------------------------

@bp.route('/stats')
@require_user
@require_project_role('viewer')
def stats():
    """项目级统计：仅返回当前用户有权限项目的聚合数据。"""
    pid = request._project_id
    conn = get_db()
    try:
        # 项目总数仍为全站 active 项目（管理指标），文档/查询/引用按项目隔离
        projects = conn.execute(
            "SELECT status, COUNT(*) AS n FROM projects"
            " WHERE status = 'active' GROUP BY status"
        ).fetchall()
        docs = conn.execute(
            "SELECT status, COUNT(*) AS n FROM documents"
            " WHERE project_id = ? GROUP BY status",
            (pid,)
        ).fetchall()
        queries = conn.execute(
            "SELECT COUNT(*) AS n FROM qa_logs"
            " WHERE project_id = ? AND created_at > CURRENT_DATE",
            (pid,)
        ).fetchone()
        total_queries = conn.execute(
            "SELECT COUNT(*) AS n FROM qa_logs WHERE project_id = ?",
            (pid,)
        ).fetchone()
        citations = conn.execute(
            "SELECT COUNT(*) AS n FROM citations WHERE project_id = ?",
            (pid,)
        ).fetchone()
        return jsonify({
            'ok': True,
            'projects': {r['status']: r['n'] for r in projects},
            'documents': {r['status']: r['n'] for r in docs},
            'queries_today': queries['n'] or 0,
            'total_queries': total_queries['n'] or 0,
            'total_citations': citations['n'] or 0,
        })
    finally:
        conn.close()
