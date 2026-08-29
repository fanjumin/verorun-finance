#!/usr/bin/env python3
"""Knowledge Base Admin — 知识库管理后台路由

管理端 6 个端点，操作 knowledge_blocks 表（主库 PostgreSQL）。

依赖: 复用 auth-center 的 JWT 鉴权，目标表为主库 knowledge_blocks。
"""
from i18n import _
import sys, os, json, logging, uuid
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from flask import Blueprint, jsonify, request
from models import get_db

logger = logging.getLogger(__name__)

knowledge_bp = Blueprint('knowledge_admin', __name__, url_prefix='/admin/knowledge')


def _require_admin():
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        return None, (jsonify({'success': False, 'error': _('Please login first')}), 401)
    from services.jwt_service import validate_token
    payload = validate_token(token)
    if not payload:
        return None, (jsonify({'success': False, 'error': _('Invalid Token')}), 401)
    if not payload.get('is_admin'):
        return None, (jsonify({'success': False, 'error': _('Requires admin permissions')}), 403)
    return payload, None


def _success(data=None, message='ok'):
    return jsonify({'success': True, 'data': data, 'message': message})


def _error(message, code=400):
    return jsonify({'success': False, 'error': message}), code


_DW = None

def _dw():
    """Return soft-delete WHERE clause; falls back to '1=1' if deleted_at column missing."""
    global _DW
    if _DW is None:
        try:
            with get_db() as db:
                db.execute("SELECT deleted_at FROM knowledge_blocks LIMIT 0")
            _DW = "deleted_at IS NULL"
        except Exception:
            _DW = "1=1"
    return _DW


def _is_private_ip(ip: str) -> bool:
    """SSRF 防护：判断 IP 是否内网/回环/链路本地/CGNAT"""
    if ip.startswith('::ffff:'):
        ip = ip[7:]
    if ':' in ip:  # IPv6
        return ip == '::1' or ip.lower().startswith(('fc', 'fd', 'fe80'))
    parts = ip.split('.')
    if len(parts) != 4:
        return True
    a, b = int(parts[0]), int(parts[1])
    if a == 10 or a == 127: return True
    if a == 169 and b == 254: return True
    if a == 172 and 16 <= b <= 31: return True
    if a == 192 and b == 168: return True
    if a == 100 and 64 <= b <= 127: return True  # CGNAT
    return False


def _extract_html(html: str):
    """stdlib 抽取 <title> 与正文文本（去除 script/style/nav/footer 等噪音）"""
    import re
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
    title = re.sub(r'\s+', ' ', m.group(1)).strip() if m else ''
    text = re.sub(r'(?is)<(script|style|nav|footer|header|iframe|form|svg)[^>]*>.*?</\1>', ' ', html)
    text = re.sub(r'(?s)<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return title, text


# ── 0. 知识库概览 ──

@knowledge_bp.route('/', methods=['GET'])
def kb_root():
    """知识库概览：总条目数 + 分类统计"""
    admin, err = _require_admin()
    if err: return err
    try:
        with get_db() as db:
            total = db.execute(
                "SELECT COUNT(*) as c FROM knowledge_blocks WHERE " + _dw()
            ).fetchone()['c']
            categories = db.execute(
                "SELECT category, COUNT(*) as cnt FROM knowledge_blocks WHERE " + _dw() + " GROUP BY category ORDER BY cnt DESC"
            ).fetchall()
        return _success({
            'total_entries': total,
            'categories': [dict(r) for r in categories],
        })
    except Exception as e:
        return _error(str(e))


# ── 1. 知识库统计 ──

@knowledge_bp.route('/stats', methods=['GET'])
def kb_stats():
    """知识库统计"""
    admin, err = _require_admin()
    if err: return err

    try:
        with get_db() as db:
            total = db.execute(
                "SELECT COUNT(*) as c FROM knowledge_blocks WHERE " + _dw()
            ).fetchone()['c']
            by_category = db.execute(
                "SELECT category, COUNT(*) as cnt FROM knowledge_blocks WHERE " + _dw() + " GROUP BY category ORDER BY cnt DESC"
            ).fetchall()
            by_scope = db.execute(
                "SELECT scope, COUNT(*) as cnt FROM knowledge_blocks WHERE " + _dw() + " GROUP BY scope"
            ).fetchall()
            total_hits = db.execute(
                "SELECT COALESCE(SUM(hit_count), 0) as hits FROM knowledge_blocks WHERE " + _dw()
            ).fetchone()['hits']

        return _success({
            'total_entries': total,
            'by_category': [dict(r) for r in by_category],
            'by_scope': [dict(r) for r in by_scope],
            'total_hits': total_hits,
        })
    except Exception as e:
        logger.exception('kb_stats failed')
        return _error(str(e), 500)


# ── 2. 知识条目列表（分页+搜索）──

@knowledge_bp.route('/entries', methods=['GET'])
def kb_list():
    """知识条目列表"""
    admin, err = _require_admin()
    if err: return err

    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 20, type=int)
    keyword = request.args.get('keyword', '').strip()
    category = request.args.get('category', '').strip()
    scope = request.args.get('scope', '').strip()
    sort = request.args.get('sort', 'created_at')

    sort_col = 'created_at' if sort == 'created_at' else 'hit_count'
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    try:
        with get_db() as db:
            where = [_dw()]
            params = []

            if keyword:
                where.append("(title LIKE %s OR content LIKE %s OR keywords LIKE %s)")
                kw = f'%{keyword}%'
                params.extend([kw, kw, kw])
            if category:
                where.append("category=%s")
                params.append(category)
            if scope:
                where.append("scope=%s")
                params.append(scope)

            where_clause = ' AND '.join(where)

            count_sql = f"SELECT COUNT(*) as total FROM knowledge_blocks WHERE {where_clause}"
            total = db.execute(count_sql, params).fetchone()['total']

            offset = (page - 1) * page_size
            data_sql = f"SELECT * FROM knowledge_blocks WHERE {where_clause} ORDER BY {sort_col} DESC, priority DESC LIMIT %s OFFSET %s"
            rows = db.execute(data_sql, params + [page_size, offset]).fetchall()

            items = [dict(r) for r in rows]

        return _success({
            'items': items,
            'total': total,
            'page': page,
            'pageSize': page_size,
            'pages': max(1, (total + page_size - 1) // page_size),
        })
    except Exception as e:
        logger.exception('kb_list failed')
        return _error(str(e), 500)


# ── 3. 新增知识条目 ──

@knowledge_bp.route('/entries', methods=['POST'])
def kb_create():
    """新增知识条目"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True)
    if not data:
        return _error(_('Request body is required'))
    title = (data.get('title') or '').strip()
    content = (data.get('content') or '').strip()
    if not title or not content:
        return _error(_('Title and content are required'))

    kb_id = data.get('id', f'kb_admin_{uuid.uuid4().hex[:12]}')
    keywords = data.get('keywords', '')
    if isinstance(keywords, (list, tuple)):
        keywords = ','.join(keywords)
    category = data.get('category', 'general')
    priority = data.get('priority', 5)
    scope = data.get('scope', 'user')
    source = data.get('source', 'manual')

    # 系统知识库写入仅超级管理员（与 cleaner / 用户端 API 权限对齐）
    if scope == 'system':
        from services.kb_permission import check_kb_permission
        allowed, perr = check_kb_permission('system', None, 'write', admin)
        if not allowed:
            return perr

    try:
        with get_db() as db:
            db.execute(
                """INSERT INTO knowledge_blocks (id, title, content, keywords, category, priority, scope, source, owner_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                       title=excluded.title, content=excluded.content,
                       keywords=excluded.keywords, category=excluded.category,
                       priority=excluded.priority, scope=excluded.scope,
                       updated_at=NOW()""",
                (kb_id, title, content, keywords, category, priority, scope, source, admin.get('user_id'))
            )
            db.commit()
        # 通用向量回写：新建/更新后自动生成 embedding（幂等，失败静默降级）
        from agent_matrix.rag_retriever import store_embedding
        store_embedding(kb_id, title, content)
        return _success({'id': kb_id}, _('Knowledge entry saved'))
    except Exception as e:
        logger.exception('kb_create failed')
        return _error(str(e), 500)


# ── 4. 更新知识条目 ──

@knowledge_bp.route('/entries/<entry_id>', methods=['PUT'])
def kb_update(entry_id):
    """更新知识条目"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True) or {}
    if not data:
        return _error(_('Request body is required'))

    fields = []
    params = []
    for col in ('title', 'content', 'keywords', 'category', 'priority', 'scope'):
        if col in data:
            val = data[col]
            if col == 'keywords' and isinstance(val, (list, tuple)):
                val = ','.join(val)
            fields.append(f"{col}=%s")
            params.append(val)

    if not fields:
        return _error(_('No valid fields to update'))

    # 系统知识库条目（当前或目标 scope=system）仅超级管理员可修改
    target_system = (data.get('scope') == 'system')
    try:
        with get_db() as db:
            cur = db.execute(
                "SELECT scope FROM knowledge_blocks WHERE id=%s AND " + _dw(),
                (entry_id,)
            ).fetchone()
            if target_system or (cur and cur['scope'] == 'system'):
                from services.kb_permission import check_kb_permission
                allowed, perr = check_kb_permission('system', None, 'write', admin)
                if not allowed:
                    return perr
    except Exception as e:
        return _error(str(e), 500)
    fields.append("updated_at=NOW()")
    params.append(entry_id)

    try:
        with get_db() as db:
            sql = f"UPDATE knowledge_blocks SET {', '.join(fields)} WHERE id=%s AND " + _dw()
            db.execute(sql, params)
            if 'title' in data or 'content' in data:
                row = db.execute(
                    "SELECT title, content FROM knowledge_blocks WHERE id=%s AND " + _dw(),
                    (entry_id,)
                ).fetchone()
            else:
                row = None
            db.commit()
        # 通用向量回写：内容变更后重算 embedding（幂等，失败静默降级）
        if row:
            from agent_matrix.rag_retriever import store_embedding
            store_embedding(entry_id, row['title'], row['content'])
        return _success(None, _('Updated'))
    except Exception as e:
        logger.exception('kb_update failed')
        return _error(str(e), 500)


# ── 4.5 批量导入知识条目（通用能力，scope 由调用方指定）──

@knowledge_bp.route('/import', methods=['POST'])
def kb_import():
    """批量导入知识条目。

    Request body:
    {
        "blocks": [{"title": "...", "content": "...", "keywords": "", "category": ""}],
        "scope": "system"      # 默认 system（与单条创建一致）；小程序知识请显式传 "user"
    }
    """
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    blocks = data.get('blocks', [])
    if not isinstance(blocks, list) or not blocks:
        return _error(_('blocks must be a non-empty list'))

    scope = data.get('scope', 'user')
    from agent_matrix.rag_retriever import store_embedding
    count, failed = 0, 0
    with get_db() as db:
        for b in blocks:
            try:
                title = (b.get('title') or '').strip()
                content = (b.get('content') or '').strip()
                if not title or not content:
                    failed += 1
                    continue
                kb_id = b.get('id') or f'kb_admin_{uuid.uuid4().hex[:12]}'
                keywords = b.get('keywords', '')
                if isinstance(keywords, (list, tuple)):
                    keywords = ','.join(keywords)
                category = b.get('category', 'general')
                priority = b.get('priority', 5)
                db.execute(
                    """INSERT INTO knowledge_blocks (id, title, content, keywords, category, priority, scope, source, owner_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET
                           title=excluded.title, content=excluded.content,
                           keywords=excluded.keywords, category=excluded.category,
                           priority=excluded.priority, scope=excluded.scope,
                           updated_at=NOW()""",
                    (kb_id, title, content, keywords, category, priority, scope, 'manual', admin.get('user_id'))
                )
                db.commit()
                store_embedding(kb_id, title, content)
                count += 1
            except Exception:
                failed += 1
    return _success({'imported': count, 'failed': failed})


# ── 4.6 URL 入库（通用能力：抓取网页 → 正文 → 知识块）──

@knowledge_bp.route('/import-url', methods=['POST'])
def kb_import_url():
    """从 URL 抓取网页并入库为知识条目（通用能力，scope 由调用方指定）。"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True, silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return _error(_('url is required'))
    scope = data.get('scope', 'user')
    category = data.get('category', 'general')
    keywords = data.get('keywords', '')

    # ── SSRF 防护：仅 http/https，拒绝内网/回环/链路本地 ──
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return _error(_('invalid url'))
        import socket
        resolved = socket.getaddrinfo(parsed.hostname, 80, proto=socket.IPPROTO_TCP)
        if not resolved:
            return _error(_('invalid url'))
        for _info in resolved:
            if _is_private_ip(_info[4][0]):
                return _error(_('url not allowed'))
    except Exception:
        return _error(_('invalid url'))

    # ── 抓取（超时 + 2MB 上限，重定向后重新校验目标）──
    import requests
    try:
        resp = requests.get(url, timeout=10, stream=True, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        })
        if resp.status_code != 200:
            return _error(_('fetch failed'), 502)
        if urlparse(resp.url).scheme not in ('http', 'https'):
            return _error(_('url not allowed'))
        content = ''
        for chunk in resp.iter_content(4096):
            content += chunk.decode('utf-8', errors='ignore')
            if len(content) > 2 * 1024 * 1024:
                return _error(_('content too large'), 413)
    except Exception:
        return _error(_('fetch failed'), 502)

    # ── 抽取 title/正文（stdlib，零新依赖）──
    title, text = _extract_html(content)
    title = (title or url)[:200]
    text = text.strip()[:50000]
    if len(text) < 20:
        return _error(_('no extractable content'))

    kb_id = f'kb_admin_{uuid.uuid4().hex[:12]}'
    try:
        with get_db() as db:
            db.execute(
                """INSERT INTO knowledge_blocks (id, title, content, keywords, category, scope, source, owner_id)
                   VALUES (%s, %s, %s, %s, %s, %s, 'url', %s)
                   ON CONFLICT (id) DO UPDATE SET
                       title=excluded.title, content=excluded.content,
                       keywords=excluded.keywords, category=excluded.category,
                       scope=excluded.scope, updated_at=NOW()""",
                (kb_id, title, text, keywords, category, scope, admin.get('user_id'))
            )
            db.commit()
        from agent_matrix.rag_retriever import store_embedding
        store_embedding(kb_id, title, text)
        return _success({'id': kb_id, 'title': title, 'chars': len(text)}, _('Knowledge entry saved'))
    except Exception as e:
        logger.exception('kb_import_url failed')
        return _error(str(e), 500)


# ── 4.5 获取单个知识条目 ──

@knowledge_bp.route('/entries/<entry_id>', methods=['GET'])
def kb_get(entry_id):
    """获取单个知识条目"""
    admin, err = _require_admin()
    if err: return err
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM knowledge_blocks WHERE id=%s AND " + _dw(),
                (entry_id,)
            ).fetchone()
        if not row:
            return _error(_('Entry not found'), 404)
        return _success(dict(row))
    except Exception as e:
        logger.exception('kb_get failed')
        return _error(str(e), 500)


# ── 5. 删除知识条目（软删除）──

@knowledge_bp.route('/entries/<entry_id>', methods=['DELETE'])
def kb_delete(entry_id):
    """删除知识条目（软删除）"""
    admin, err = _require_admin()
    if err: return err

    try:
        with get_db() as db:
            cur = db.execute(
                "SELECT scope FROM knowledge_blocks WHERE id=%s AND " + _dw(),
                (entry_id,)
            ).fetchone()
            if cur and cur['scope'] == 'system':
                # 系统知识库禁止删除（与 kb_permission 语义一致）
                from services.kb_permission import check_kb_permission
                allowed, perr = check_kb_permission('system', None, 'delete', admin)
                if not allowed:
                    return perr
            db.execute(
                "UPDATE knowledge_blocks SET deleted_at=NOW() WHERE id=%s AND " + _dw(),
                (entry_id,)
            )
            db.commit()
        return _success(None, _('Deleted'))
    except Exception as e:
        logger.exception('kb_delete failed')
        return _error(str(e), 500)


# ── 6. RAG 检索 ──

@knowledge_bp.route('/search', methods=['POST'])
def kb_search():
    """RAG 检索知识库"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True) or {}
    query = (data.get('query', '') or data.get('q', '')).strip()
    if not query:
        return _error(_('Query is required'))
    top_k = data.get('topK', 5)
    top_k = max(1, min(20, top_k))
    category = data.get('category', '').strip()

    try:
        with get_db() as conn:
            where = [_dw()]
            params = [f'%{query}%', f'%{query}%']
            if category:
                where.append("category=%s")
                params.append(category)
            where_clause = ' AND '.join(where)

            # Full-text + keyword scoring
            rows = conn.execute(
                f"SELECT * FROM knowledge_blocks WHERE {where_clause} AND "
                "(title LIKE %s OR content LIKE %s) ORDER BY priority DESC, hit_count DESC LIMIT %s",
                params + [top_k * 2]
            ).fetchall()

        scored = []
        q_lower = query.lower()
        for row in rows:
            score = 0.0
            title_lower = (row['title'] or '').lower()
            content_lower = (row['content'] or '')[:500].lower()

            if query.lower() in title_lower:
                score += 0.4
            if query.lower() in content_lower:
                score += 0.2

            keywords = (row['keywords'] or '').lower().split(',')
            kw_matches = sum(1 for kw in keywords if kw.strip() and kw.strip() in q_lower)
            if keywords and kw_matches:
                score += min(kw_matches / len(keywords), 1.0) * 0.3

            score += min(row['hit_count'] / 10, 0.1)

            if score > 0:
                scored.append((row, score))

        scored.sort(key=lambda x: -x[1])
        scored = scored[:top_k]

        # Update hit_count
        for row, _ in scored:
            try:
                with get_db() as upd:
                    upd.execute(
                        "UPDATE knowledge_blocks SET hit_count = hit_count + 1 WHERE id=%s",
                        (row['id'],)
                    )
                    upd.commit()
            except Exception:
                pass

        results = [{
            'id': row['id'],
            'title': row['title'],
            'content': row['content'][:300],
            'keywords': row['keywords'].split(',') if row['keywords'] else [],
            'category': row['category'],
            'score': round(score, 3),
        } for row, score in scored]

        return _success({'results': results, 'total': len(results)})
    except Exception as e:
        logger.exception('kb_search failed')
        return _error(str(e), 500)


# ── 7. RAG 查询 ──

@knowledge_bp.route('/query', methods=['POST'])
def kb_rag_query():
    """RAG 检索增强生成：搜索知识库 + LLM 回答"""
    admin, err = _require_admin()
    if err: return err

    data = request.get_json(force=True) or {}
    question = (data.get('question', '') or data.get('q', '')).strip()
    if not question:
        return _error(_('Question is required'))
    top_k = data.get('topK', 5)
    top_k = max(1, min(10, top_k))

    try:
        from agent_matrix.rag_retriever import rag_search

        rows = rag_search(question, top_k=top_k) or []

        if not rows:
            return _success({'answer': _('No relevant knowledge found'), 'sources': []})

        # Build context
        context_parts = []
        sources = []
        for row in rows:
            context_parts.append(f"[{row['title']}]\n{(row.get('content') or '')[:1500]}")
            sources.append({'id': row['id'], 'title': row['title'], 'score': row.get('score')})
        context = '\n\n---\n\n'.join(context_parts)

        # Call LLM
        from agent_matrix.engine import get_gateway, _get_system_key
        provider = _get_system_key('ai_text_provider') or 'deepseek'
        model = _get_system_key('ai_text_model') or 'deepseek-v4-flash'
        gw = get_gateway()
        answer = gw.chat(
            provider=provider,
            model=model,
            messages=[
                {'role': 'system', 'content': 'Based on the knowledge base content below, answer the question accurately in Chinese. If the content does not contain the answer, say so honestly.'},
                {'role': 'user', 'content': f'Knowledge Base:\n{context}\n\nQuestion: {question}'}
            ],
            temperature=0.3,
            max_tokens=2048,
            module='knowledge_base',
        )

        # Update hit_count for sources
        for row in rows:
            try:
                with get_db() as upd:
                    upd.execute("UPDATE knowledge_blocks SET hit_count = hit_count + 1 WHERE id=%s", (row['id'],))
                    upd.commit()
            except Exception:
                pass

        return _success({'answer': answer, 'sources': sources})
    except Exception as e:
        logger.exception('kb_rag_query failed')
        return _error(str(e), 500)
