"""研报知识库：桥接 project_workspace 插件，提供股票研报 PDF 上传/检索/Q&A。

设计：不重建 RAG 基础设施，而是在 project_workspace 内维护一个专用项目
"stock_research"，所有研报文档均落入该项目。stock_analysis 路由负责：
  1. 自动创建/获取 stock_research 项目
  2. 附加股票元数据（symbol, report_type, analyst）到 documents.metadata
  3. 提供股票语境下的搜索与 Q&A 接口
"""

import json
import logging
import uuid

_LOGGER = logging.getLogger(__name__)

STOCK_RESEARCH_PROJECT = "stock_research"
STOCK_RESEARCH_PROJECT_NAME = "股票研报库"

# 软依赖提示：本插件的研报库整体寄生在 project_workspace 上（不重建 RAG 地基），
# 因此后端缺失/被发行版门控排除时必须走「可捕获降级」，而不是 ImportError 冒成 500。
_PW_UNAVAILABLE_HINT = (
    'project_workspace 插件不可用（未安装或被发行版 plugins.exclude 门控排除），'
    '研报知识库功能已降级'
)


class KnowledgeBaseUnavailable(RuntimeError):
    """研报知识库后端不可用。

    调用方（routes.py 的 /api/kb/*）须捕获并返回 **503 + 明确错误码**，
    不得冒泡为 500 裸栈：知识库是可选能力，不能拖垮股票主链路。
    """


def _get_pw_db():
    """获取 project_workspace 的数据库连接（软依赖：缺失时抛 KnowledgeBaseUnavailable）。"""
    try:
        from plugins.project_workspace.models import get_db as pw_get_db
    except ImportError as e:
        raise KnowledgeBaseUnavailable(_PW_UNAVAILABLE_HINT) from e
    return pw_get_db()


def _pw_retriever():
    """构造 project_workspace 检索器（软依赖，口径同 _get_pw_db）。

    ★ 已知债（D16，2026-09-30 复核，勿单方面改造）：
      project_workspace 已通过 add_filter 提供标准检索面 `project_workspace/search`
      （project_workspace/__init__.py:212），但存在两点不足以支撑切换：
        1. 该 filter 只透传 (query, project_id, top_k)，**不接收 user_id /
           cross_project**；当前 cross_project=False 时 user_id 不参与过滤尚等价，
           一旦将来开放跨项目检索即丢失成员边界过滤。
        2. 本模块另外还直接依赖 pw 的 get_db / DocProcessor / ResearchService
           三个内部类，pw 均未提供对应钩子 —— 只切检索无法真正解耦。
      结论：维持直接调用并登记为债；待 pw 补齐 user_id 透传与其余钩子后再契约化。
      另注：直接 import 的 ImportError → KnowledgeBaseUnavailable → 路由 503 降级链
      是现有可观测语义，改走 apply_filters 会改变该错误传播路径。
    """
    try:
        from plugins.project_workspace.services.retriever import KnowledgeRetriever
    except ImportError as e:
        raise KnowledgeBaseUnavailable(_PW_UNAVAILABLE_HINT) from e
    return KnowledgeRetriever({})


def pw_get_db():
    """对外的 project_workspace 连接取用入口（routes 侧统一走它，缺失时抛可捕获降级信号）。"""
    return _get_pw_db()


def pw_doc_processor():
    """取用文档处理器与存储目录解析器（软依赖）。

    :return: (DocProcessor, resolve_storage_dir)
    """
    try:
        from plugins.project_workspace.services.doc_processor import (
            DocProcessor, resolve_storage_dir,
        )
    except ImportError as e:
        raise KnowledgeBaseUnavailable(_PW_UNAVAILABLE_HINT) from e
    return DocProcessor, resolve_storage_dir


def ensure_stock_research_project(user_id: str) -> str:
    """确保 stock_research 项目存在，返回 project_id。幂等。"""
    conn = _get_pw_db()
    try:
        row = conn.execute(
            "SELECT id FROM projects WHERE name = ? AND owner_type = 'system'",
            (STOCK_RESEARCH_PROJECT_NAME,)
        ).fetchone()
        if row:
            return row["id"]
        project_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO projects (id, owner_type, owner_id, name, description, tags)"
            " VALUES (?, 'system', ?, ?, ?, ?)",
            (project_id, user_id, STOCK_RESEARCH_PROJECT_NAME,
             "股票分析插件研报知识库，自动创建", ["stock_analysis", "research"])
            # ★ 实测修正：projects.tags 是 text[]，原来传 json.dumps(...) 的字符串，
            #   触发 malformed array literal → 项目建不出来 → QA 永远返回"研报库尚未初始化"。
            #   psycopg2 会把 Python list 适配成 PG 数组，直接传 list 即可。
        )
        conn.execute(
            "INSERT INTO project_members (project_id, user_id, role)"
            " VALUES (?, ?, 'owner')",
            (project_id, user_id)
        )
        conn.execute(
            "UPDATE projects SET member_count = 1 WHERE id = ?",
            (project_id,)
        )
        conn.commit()
        _LOGGER.info("created stock_research project: %s", project_id)
        return project_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_stock_research_project_id() -> str | None:
    """获取 stock_research 项目 ID，不存在返回 None。"""
    conn = _get_pw_db()
    try:
        row = conn.execute(
            "SELECT id FROM projects WHERE name = ? AND owner_type = 'system'",
            (STOCK_RESEARCH_PROJECT_NAME,)
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def list_research_docs(keyword: str = "", symbol: str = "",
                       status: str = "", limit: int = 50, offset: int = 0) -> list:
    """列出研报文档，支持按关键词/股票代码/状态过滤。"""
    project_id = get_stock_research_project_id()
    if not project_id:
        return []
    conn = _get_pw_db()
    try:
        conditions = ["project_id = ?"]
        params: list = [project_id]
        if status:
            conditions.append("status = ?")
            params.append(status)
        if keyword:
            conditions.append("(original_name ILIKE ? OR summary ILIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if symbol:
            conditions.append("metadata->>'symbol' = ?")
            params.append(symbol)
        where = " AND ".join(conditions)
        rows = conn.execute(
            # ★ 同 get_kb_stats：documents 无 chunk_count 列，改由 document_chunks 子查询统计
            f"SELECT id, original_name, file_ext, file_size, status,"
            f" error_msg,"
            f" (SELECT COUNT(*) FROM document_chunks c WHERE c.document_id = d.id)"
            f"     AS chunk_count,"
            f" page_count, summary, tags, metadata,"
            f" uploaded_by, uploaded_at AS created_at, processed_at"
            f" FROM documents d WHERE {where}"
            f" ORDER BY uploaded_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            meta = d.get("metadata")
            if isinstance(meta, str):
                try:
                    d["metadata"] = json.loads(meta)
                except Exception:
                    d["metadata"] = {}
            result.append(d)
        return result
    finally:
        conn.close()


def get_doc_status(doc_id: str) -> dict | None:
    """获取单个文档的处理状态。"""
    conn = _get_pw_db()
    try:
        row = conn.execute(
            "SELECT id, status, error_msg,"
            " (SELECT COUNT(*) FROM document_chunks c WHERE c.document_id = d.id)"
            "     AS chunk_count,"
            " page_count, processed_at FROM documents d WHERE d.id = ?",
            (doc_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def search_research(query: str, symbol: str = "", top_k: int = 10,
                    user_id: str = "") -> list:
    """在研报库中执行混合检索（向量 + 关键词 RRF）。"""
    project_id = get_stock_research_project_id()
    if not project_id:
        return []
    try:
        retriever = _pw_retriever()
        results = retriever.retrieve(
            query=query,
            project_id=project_id,
            top_k=top_k,
            cross_project=False,
            user_id=user_id,
        )
        if symbol:
            results = [r for r in results if r.get("symbol", "") == symbol
                       or symbol in r.get("filename", "")]
        return results
    except KnowledgeBaseUnavailable:
        raise  # 后端不可用 ≠ 无结果：交由路由返回 503
    except Exception as e:
        _LOGGER.error("research search failed: %s", e)
        return []


def qa_research(query: str, top_k: int = 5, user_id: str = "") -> dict:
    """研报 Q&A：检索相关片段 → LLM 生成带引用的回答。"""
    project_id = get_stock_research_project_id()
    if not project_id:
        return {"answer": "研报库尚未初始化", "sources": []}
    try:
        retriever = _pw_retriever()
        try:
            from plugins.project_workspace.services.researcher import ResearchService
        except ImportError as e:
            raise KnowledgeBaseUnavailable(_PW_UNAVAILABLE_HINT) from e
        chunks = retriever.retrieve(
            query=query, project_id=project_id, top_k=top_k,
            cross_project=False, user_id=user_id,
        )
        if not chunks:
            return {"answer": "未找到相关研报内容", "sources": []}
        researcher = ResearchService({})
        # ★ 实测修正：ResearchService.answer_question(query, context_chunks: **list**) -> **dict**
        #   原实现把拼好的字符串当第二个参数传，并返回整个 dict 当答案 —— 一旦 project
        #   初始化过（此前本机因 project_workspace 未迁移而一直走不到这里），QA 就会
        #   把 dict 直接塞给前端。现改为传 chunks 原列表，并取返回 dict 的 answer 字段。
        result = researcher.answer_question(query, chunks)
        answer = result.get("answer", "") if isinstance(result, dict) else str(result)
        if isinstance(result, dict) and result.get("ok") is False:
            # LLM 侧失败是**可回答的失败**，不当异常处理，但要让用户看得见"为什么"
            answer = f"{answer}（模型调用未成功，以上为降级文案）"
        sources = [
            {
                "document_id": c.get("document_id", ""),
                "filename": c.get("original_name", ""),
                "section": c.get("section_title", ""),
                "page": c.get("page_number"),
                "score": c.get("score"),
            }
            for c in chunks[:top_k]
        ]
        return {"answer": answer, "sources": sources}
    except KnowledgeBaseUnavailable:
        raise  # 后端不可用须回 503，不能伪装成一条普通回答
    except Exception as e:
        _LOGGER.error("research qa failed: %s", e)
        return {"answer": f"检索失败: {e}", "sources": []}


def get_kb_stats() -> dict:
    """研报库统计信息。"""
    project_id = get_stock_research_project_id()
    if not project_id:
        return {"total_docs": 0, "ready_docs": 0, "total_chunks": 0,
                "pending_docs": 0, "failed_docs": 0}
    conn = _get_pw_db()
    try:
        row = conn.execute(
            # ★ 实测修正：documents 表**没有 chunk_count 列**（只有 page/word/char_count），
            #   原 SQL 抛 UndefinedColumn → /kb/stats 500。分片数必须从 document_chunks 统计。
            #   这个 bug 此前一直被掩盖：project 未初始化时本函数走上面的 early return。
            "SELECT COUNT(*) AS total_docs,"
            " COALESCE(SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END), 0) AS ready_docs,"
            " COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_docs,"
            " COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed_docs,"
            " (SELECT COUNT(*) FROM document_chunks WHERE project_id = ?) AS total_chunks"
            " FROM documents WHERE project_id = ?",
            (project_id, project_id)
        ).fetchone()
        return dict(row) if row else {"total_docs": 0, "ready_docs": 0,
                                       "total_chunks": 0, "pending_docs": 0,
                                       "failed_docs": 0}
    finally:
        conn.close()
