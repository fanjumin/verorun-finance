#!/usr/bin/env python3
"""Write pipeline: turn completed task traces into durable memories."""

import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger('memory_engine.extractor')

# v1.6 统一网关注册：curator 提示词取自插件内置文件（不再声明独立 Agent 行）。
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CURATOR_PROMPT_FILE = os.path.join(_PLUGIN_DIR, 'agents', 'memory_curator_prompt.md')

# PII guard delegates to shared _base module (S11b).
# F-DEP 自包含兜底：旧内核（如服务器 1.2.x）尚无 plugins._base.pii 时，
# 使用与共享模块逐字一致的内置正则，避免自动提取链因 ImportError 整体宕掉。
# 边界用数字负向断言 (非 \b)：\b 对 CJK 与数字之间不成立，中文紧邻手机/身份证
# 会漏检；与 plugins/_base/pii.py 保持逐字一致，调整覆盖范围时两处必须同步。
_PII_FALLBACK_PATTERNS = (
    re.compile(r'(?i)(password|api[_-]?key|secret|token)\s*[:=]\s*\S+'),
    re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'),          # CN mobile
    re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'),         # CN ID card
)


def _contains_pii_fallback(text: str) -> bool:
    return any(p.search(text or '') for p in _PII_FALLBACK_PATTERNS)


_SKIP_MARKERS = ('hello', 'hi', 'thanks', 'thank you')

# 元任务（规划/意图确认）不含用户侧知识，按子串命中跳过，避免污染记忆池。
_SKIP_SUBSTRINGS = (
    '生成执行计划', '确认指令意图', '执行计划', '任务规划', '意图识别', '任务分解',
    'generate execution plan', 'confirm intent', 'task planning',
    'intent recognition', 'task decomposition',
)


# 讨论轮次（planner/reviewer/decider 往返）不产生长期记忆，按 task_id 前缀整体跳过。
# MEMEXTRACT-/MEMREFLEX- 是本插件自己发起的 curator 调用：AgentRunner 结束时同样会发射
# agent.task.completed，若不拦下则每次提取都会再触发一次提取（无界递归、烧穿 AI 预算）。
_SKIP_TASK_PREFIXES = ('DISCUSS-', 'MEMEXTRACT-', 'MEMREFLEX-')

# curator 单次输入硬上限（防回归护栏）：字段各自封顶，整包再兜一层。
# 背景：_task_query() 可能返回整段 description 且原无长度上限，
# 一旦上游出现递归/嵌套回灌，curator prompt 会随之无界增长。
_CURATOR_FIELD_LIMIT = 2000
_CURATOR_INPUT_LIMIT = 4000


def _cap_curator_input(text, limit: int = _CURATOR_FIELD_LIMIT) -> str:
    """单字段截断 + 告警：只截不丢，保证提取链路不被单条超长输入拖垮。"""
    s = str(text or '')
    if len(s) > limit:
        logger.warning('[memory_engine] curator input truncated: %d -> %d chars',
                       len(s), limit)
        return s[:limit]
    return s


def _cap_curator_payload(payload: str) -> str:
    """整包总量兜底：超上限即截断并告警（仅作 prompt 文本，无需保持 JSON 合法）。"""
    if len(payload) > _CURATOR_INPUT_LIMIT:
        logger.warning('[memory_engine] curator payload over hard cap: %d -> %d chars',
                       len(payload), _CURATOR_INPUT_LIMIT)
        return payload[:_CURATOR_INPUT_LIMIT]
    return payload


def _task_query(task: dict) -> str:
    """真实子任务 dict 顶层没有 query —— 用户指令在 description / input_data 里。"""
    td = task or {}
    inp = td.get('input_data') or {}
    return str(td.get('user_query') or td.get('query')
               or inp.get('original_instruction') or inp.get('query')
               or td.get('description') or td.get('title') or '')


def _task_owner(task: dict) -> str:
    """同样的形状差异：user_id 在 input_data 内，顶层没有。"""
    td = task or {}
    inp = td.get('input_data') or {}
    return str(td.get('user_id') or inp.get('user_id')
               or (td.get('meta') or {}).get('user_id') or '')


class MemoryExtractor:
    """Extract memory candidates from completed agent tasks."""

    def __init__(self, config: dict):
        self._config = config or {}
        self._embedder = None  # lazy-init from services.embedding
        self._pool = ThreadPoolExecutor(max_workers=2)

    @property
    def _embed(self):
        if self._embedder is None:
            from .embedding import EmbeddingService
            self._embedder = EmbeddingService(self._config)
        return self._embedder

    def submit(self, task: dict, result: dict, agent_id: str):
        """Fire-and-forget extraction; never blocks the request thread."""
        if not self._config.get('enable_auto_extract', True):
            return
        # F-02：写管线隐私门。opt-out / 无主任务在进入线程池前直接丢弃，
        # 与注入读路径（prompt_injector）、沉淀路径（sedimentation）同一同意口径。
        from ..prompt_injector import user_opted_in
        if not user_opted_in(_task_owner(task), self._config):
            return
        if not self._within_daily_budget():
            return
        self._pool.submit(self._extract, task, result, agent_id)

    def _within_daily_budget(self) -> bool:
        """Respect the configured daily cap on produced memories.

        Previous implementation counted `reflexion_logs.trigger='task_completed'`,
        which this service never writes — so the cap never actually triggered.
        Count today's rows in the table extraction really writes.
        """
        cap = int(self._config.get('daily_extract_budget', 200))
        if cap <= 0:
            return False
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM memories"
                " WHERE created_at::date = CURRENT_DATE"
            ).fetchone()
            return (row['n'] or 0) < cap
        finally:
            conn.close()

    def _should_extract(self, task: dict, result: dict) -> bool:
        """Cheap heuristics first; only promising traces reach the LLM."""
        if str(task.get('task_id') or '').startswith(_SKIP_TASK_PREFIXES):
            return False  # discussion round, not user-facing knowledge
        query = _task_query(task)
        low = query.lower()
        if len(query) < 4 or low in _SKIP_MARKERS:
            return False
        if any(m in low for m in _SKIP_SUBSTRINGS):
            return False  # meta/planning sub-task, no user-facing knowledge
        if result.get('failed') and not result.get('retries'):
            return False  # failure without retry is handled by reflexion
        return True

    def _extract(self, task: dict, result: dict, agent_id: str):
        """Run the curator agent in extract mode, then persist candidates."""
        try:
            if not self._should_extract(task, result):
                return
            agent_config = self._load_curator_config()
            if not agent_config:
                logger.warning(
                    '[memory_engine] curator config unavailable (agent_role resolution '
                    'failed); auto-extraction skipped for task %s', task.get('task_id'))
                return
            from agent_matrix.agent_runner import AgentRunner
            runner = AgentRunner(agent_config)
            transcript = {
                'query': _cap_curator_input(_task_query(task)),
                'result': _cap_curator_input(result),
            }
            resp = runner.execute({
                'task_id': 'MEMEXTRACT-%s' % (task.get('task_id') or 'x'),
                'title': 'Memory Extract',
                'description': _cap_curator_payload(
                    json.dumps(transcript, ensure_ascii=False)),
            })
            candidates = self._parse_candidates(resp)
            self._persist(candidates, agent_id, task, source='auto')
        except Exception as e:
            logger.error('extraction failed: %s', e)

    def _parse_candidates(self, resp) -> list:
        """Parse the curator JSON output defensively; malformed output yields [].

        Runner result structure: (result_text, retries, logs).
        """
        text = ''
        if isinstance(resp, tuple) and resp:
            text = str(resp[0])
        elif isinstance(resp, dict):
            text = str(resp.get('response') or resp.get('result') or resp.get('output') or '')
        try:
            data = json.loads(text)
            items = data.get('memories') or []
        except (ValueError, AttributeError):
            return []
        out = []
        for it in items:
            content = str(it.get('content', '')).strip()
            if not content or self._contains_pii(content):
                continue
            out.append({
                'type': it.get('type', 'fact'),
                'content': content[:500],
                'confidence': float(it.get('confidence', 0.5)),
            })
            op = str(it.get('operation', 'add')).lower()
            out[-1]['operation'] = op if op in ('add', 'update', 'noop') else 'add'
        return out

    @staticmethod
    def _contains_pii(text: str) -> bool:
        try:
            from plugins._base.pii import contains_pii
        except ImportError:
            # F-DEP：旧内核缺共享 PII 模块时走自包含回退，提取链不中断。
            return _contains_pii_fallback(text)
        return contains_pii(text)

    def _load_curator_config(self) -> dict:
        """v1.6 统一网关注册：复用承载本插件能力的核心角色行（不再依赖独立 Agent 行）。

        模型配置取自核心角色行（provider_model_id → provider/model/base_url/api_key），
        system_prompt 覆盖为 curator 提示词；解析不到归属角色时返回 {}。
        """
        from agent_matrix.models import get_db
        try:
            from agent_matrix.models import resolve_agent_roles
        except ImportError:
            # F-DEP：旧内核无 resolve_agent_roles，回退归属核心角色 athena，
            # 与下方 `or ['athena']` 运行时兜底同口径，自动提取不恒 0。
            def resolve_agent_roles(_plugin_id, _metadata):
                return ["athena"]
        try:
            with open(_CURATOR_PROMPT_FILE, 'r', encoding='utf-8') as f:
                curator_prompt = f.read().strip()
        except OSError as e:
            logger.warning('[memory_engine] curator prompt unreadable: %s', e)
            return {}
        roles = resolve_agent_roles('memory_engine', {'agent_role': 'athena'}) or ['athena']
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM agent_matrix WHERE slug = %s AND is_system = 1",
                (roles[0],),
            ).fetchone()
        if not row:
            return {}
        cfg = dict(row)
        cfg['name'] = 'memory_curator'          # 仅用于 token 日志归因
        cfg['system_prompt'] = curator_prompt   # 覆盖为核心角色的模型配置 + curator 提示词
        return cfg

    def _persist(self, candidates: list, agent_id: str, task: dict, source: str):
        """Insert with content-hash idempotency and per-owner caps."""
        if not candidates:
            return
        owner_id = _task_owner(task)
        if not owner_id:
            # 写 owner_id='' 是必然检索不到的死数据，且白占 owner 配额，直接不写。
            logger.warning('[memory_engine] persist skipped: task has no user_id'
                           ' (task_id=%s, source=%s)', task.get('task_id'), source)
            return
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            # F8 修复：列类型探测移出循环（原为每候选一次的 N+1 查询）；
            # 并补 table_schema 限定，避免命中其他 schema 的同名表。
            # CE-D1 方案 A：按实际列类型决定是否 vector 强转（无 pgvector 时列为 TEXT）
            try:
                _trow = conn.execute(
                    "SELECT data_type FROM information_schema.columns"
                    " WHERE table_schema='memory_engine'"
                    " AND table_name='memories' AND column_name='embedding'"
                ).fetchone()
                _is_vec = bool(_trow and _trow['data_type'] == 'vector')
            except Exception:
                _is_vec = False
            _embed_col = "?::vector" if _is_vec else "?"
            for c in candidates:
                digest = hashlib.sha256(
                    f"{owner_id}|{c['content']}".encode('utf-8')
                ).hexdigest()
                existing = conn.execute(
                    "SELECT id FROM memories WHERE content_hash = ?", (digest,)
                ).fetchone()
                if existing:
                    continue
                if c.get('operation') == 'noop':
                    continue
                if c.get('operation') == 'update':
                    self._supersede(conn, owner_id, c)
                vec = self._embed.embed(c['content'])
                embedding_literal = None
                if vec:
                    embedding_literal = '[' + ','.join(repr(v) for v in vec) + ']'
                conn.execute(
                    "INSERT INTO memories"
                    " (owner_type, owner_id, agent_id, memory_type, content,"
                    "  keywords, embedding, confidence, source, content_hash, meta)"
                    " VALUES ('user', ?, ?, ?, ?, ?, " + _embed_col + ", ?, ?, ?, ?::jsonb)"
                    " ON CONFLICT (content_hash) DO NOTHING",
                    (owner_id, agent_id, c['type'], c['content'],
                     self._keywords(c['content']), embedding_literal, c['confidence'],
                     source, digest, json.dumps({'task_id': task.get('task_id')})),
                )
            self._enforce_owner_cap(conn, owner_id)
            conn.commit()
        except Exception as e:
            logger.error('persist failed: %s', e)
            conn.rollback()
        finally:
            conn.close()

    @staticmethod
    def _supersede(conn, owner_id: str, candidate: dict):
        """取代语义（保守实现）：

        同 owner、同 memory_type、keywords 数组有重叠的最近一条 active 记忆
        置为 status='superseded'。retriever/sedimentation/forgetting 均硬过滤
        status='active'，被取代记忆自动退出检索与沉淀，零下游改动。
        保守性：每次只取代一条（最近一条），不做批量。
        """
        kws = MemoryExtractor._keywords(candidate['content'])
        if not kws:
            return
        row = conn.execute(
            "SELECT id FROM memories"
            " WHERE owner_type = 'user' AND owner_id = ? AND memory_type = ?"
            " AND status = 'active' AND keywords && ?"
            " ORDER BY updated_at DESC LIMIT 1",
            (owner_id, candidate['type'], kws),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE memories SET status = 'superseded',"
                " meta = meta || ?::jsonb WHERE id = ?",
                (json.dumps({'superseded_reason': 'contradicted_by_newer'}), row['id']),
            )

    @staticmethod
    def _keywords(text: str) -> list:
        """Naive keyword extraction: CJK bigrams + 2+ char latin tokens."""
        kws = set(re.findall(r'[\u4e00-\u9fff]{2,}', text))
        kws.update(w.lower() for w in re.findall(r'[a-z]{2,}', text.lower()))
        return list(kws)[:12]

    def _enforce_owner_cap(self, conn, owner_id: str):
        """Archive oldest auto memories beyond max_memories_per_owner."""
        cap = int(self._config.get('max_memories_per_owner', 500))
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM memories"
            " WHERE owner_type = 'user' AND owner_id = ? AND status = 'active'",
            (owner_id,),
        ).fetchone()
        excess = (row['n'] or 0) - cap
        if excess <= 0:
            return
        conn.execute(
            "UPDATE memories SET status = 'archived'"
            " WHERE id IN ("
            " SELECT id FROM memories"
            " WHERE owner_type = 'user' AND owner_id = ? AND status = 'active'"
            " ORDER BY quality_score ASC, updated_at ASC LIMIT ?)",
            (owner_id, excess),
        )
