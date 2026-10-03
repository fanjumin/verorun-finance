#!/usr/bin/env python3
"""
Agent Tool Approval System (系统级内核定域，v1.1)
==================================================
职责：
  - TOOL_RISK 风险档定义（auto / approve_session / always）
  - ApprovalService 审批单读写（读/写独立 schema agent_tools）
  - resolve_tool_risk()   工具名 → 风险档匹配（含 MCP 前缀剥壳）
  - approval_pre_execute() `agent_tool.pre_execute` 钩子回调（fail-open 铁律）

数据库：审批单存独立 schema agent_tools (agent_tools_approvals) —— schema 名
       保留历史命名，Python 代码已全部迁回 agent_matrix 内核。
"""
import hashlib
import json
import logging

logger = logging.getLogger('agent_matrix.approval')

# ── 风险档 ──────────────────────────────────────────────
#   auto            = 直接放行（仍受 A4 白名单约束）
#   approve_session = 首次落单并软阻塞；同任务内已批准则放行
#   always          = 每次调用都要审批
TOOL_RISK = {
    'file_read': 'auto',
    'file_list': 'auto',
    'file_write': 'approve_session',
    'file_edit': 'approve_session',
    'http_request': 'approve_session',
    'code_exec': 'always',
    # EM-3：Agent 对外发信属外发副作用，首次落单并软阻塞（同任务内批准后放行）
    'email_send': 'approve_session',
    'email_send_contact': 'approve_session',
}

# ── 钩子点 & 审批模式 ──────────────────────────────────
FILTER_PRE_EXECUTE = 'agent_tool.pre_execute'
APPROVAL_MODE_KEY = 'agent_tool_approval_mode'
DEFAULT_APPROVAL_MODE = 'approve_session'
VALID_MODES = ('off', 'session', 'strict')

# ── 单例管理 ───────────────────────────────────────────
# agent_matrix 启动时调用 init_approval() 构造；插件 activate() 也会调一次（幂等）
_approval_svc = None
_approval_config = {}


def init_approval(config=None):
    """Initialize the approval system singleton. Idempotent."""
    global _approval_svc, _approval_config
    if config is not None:
        _approval_config = config if isinstance(config, dict) else {}
    if _approval_svc is None:
        _approval_svc = ApprovalService(_approval_config)
    return _approval_svc


def get_approval():
    """Return the singleton ApprovalService, or None if not initialized."""
    return _approval_svc


# ── 审批服务 ───────────────────────────────────────────
class ApprovalService:
    """审批单读写。操作独立 schema agent_tools，不触碰内核其他表。"""

    def __init__(self, config: dict):
        self._config = config or {}

    # ── 配置 ──────────────────────────────────────────────
    def approval_mode(self) -> str:
        """审批模式判定。

        读取顺序：system_config(agent_tool_approval_mode) -> plugin.json config.approval_mode -> 默认。
        任何异常一律回退默认值（默认是「审批开启」而非「关闭」：
        降级到关闭会让高风险工具失去门禁，方向不安全）。
        """
        mode = ''
        try:
            from models import get_db
            with get_db() as conn:
                row = conn.execute(
                    "SELECT value FROM system_config"
                    " WHERE key = 'agent_tool_approval_mode'").fetchone()
            if row:
                try:
                    raw = row['value']
                except (TypeError, KeyError, IndexError):
                    raw = row[0]
                mode = str(raw or '').strip().lower()
        except Exception as e:
            logger.warning('approval mode read failed, use default: %s', e)
        if mode not in VALID_MODES:
            mode = str(self._config.get('approval_mode') or '').strip().lower()
        if mode not in VALID_MODES:
            mode = DEFAULT_APPROVAL_MODE
        return mode

    def _ttl_minutes(self) -> int:
        try:
            return max(1, min(1440, int(self._config.get('approval_ttl_minutes', 30))))
        except (TypeError, ValueError):
            return 30

    @staticmethod
    def _digest(args) -> str:
        """入参摘要：只存摘要，不入全量入参，避免敏感内容落库。"""
        try:
            payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            payload = repr(type(args))
        return hashlib.sha256(payload.encode('utf-8', 'replace')).hexdigest()

    # ── 读写 ──────────────────────────────────────────────
    def find_grant(self, task_id, agent_id, tool_name):
        """approve_session 语义：同任务内是否已有未过期的批准单。"""
        if not task_id:
            return None
        try:
            from agent_matrix.approval_db import get_approval_db
            conn = get_approval_db()
        except Exception as e:
            logger.warning('approval lookup unavailable: %s', e)
            return None
        try:
            row = conn.execute(
                "SELECT id FROM agent_tools_approvals"
                " WHERE task_id = ? AND agent_id = ? AND tool_name = ?"
                "   AND status = 'approved'"
                "   AND (expires_at IS NULL OR expires_at > now())"
                " ORDER BY decided_at DESC LIMIT 1",
                (task_id, agent_id, tool_name)).fetchone()
            return row['id'] if row else None
        except Exception as e:
            logger.warning('approval lookup failed: %s', e)
            return None
        finally:
            conn.close()

    def request(self, task_id, agent_id, tool_name, args):
        """落一条 pending 审批单；同一三元组已有 pending 则复用。返回单号或 None。

        注意：PgConnection.close() 会回滚未提交事务，故此处必须显式 commit()。
        """
        digest = self._digest(args)
        try:
            from agent_matrix.approval_db import get_approval_db
            conn = get_approval_db()
        except Exception as e:
            logger.warning('approval record unavailable: %s', e)
            return None
        try:
            row = conn.execute(
                "SELECT id FROM agent_tools_approvals"
                " WHERE task_id = ? AND agent_id = ? AND tool_name = ?"
                "   AND status = 'pending'"
                " ORDER BY requested_at DESC LIMIT 1",
                (task_id, agent_id, tool_name)).fetchone()
            if row:
                return row['id']
            row = conn.execute(
                "INSERT INTO agent_tools_approvals"
                " (task_id, agent_id, tool_name, args_digest, status, reason, expires_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?, now() + make_interval(mins => ?))"
                " RETURNING id",
                (task_id, agent_id, tool_name, digest, 'pending approval', self._ttl_minutes()),
            ).fetchone()
            conn.commit()
            return row['id'] if row else None
        except Exception as e:
            logger.warning('approval record failed: %s', e)
            try:
                conn.rollback()
            except Exception:
                pass
            return None
        finally:
            conn.close()


# ── 工具名 → 风险档匹配 ──────────────────────────────────
def resolve_tool_risk(tool_name: str):
    """Match TOOL_RISK by exact name or MCP-prefixed suffix.

    MCP-routed tool names look like 'mcp__agent_tools__<server>__<tool>'.
    This resolves bare names, prefixed names, and unknown tools uniformly.
    """
    risk = TOOL_RISK.get(tool_name)
    if risk is not None:
        return risk
    # MCP suffix extraction: last '__' segment is the actual tool name
    if tool_name.startswith('mcp__agent_tools__'):
        bare = tool_name.rsplit('__', 1)[-1]
        return TOOL_RISK.get(bare)
    return None


# ── pre_execute 钩子回调 ────────────────────────────────
def approval_pre_execute(gate):
    """`agent_tool.pre_execute` 过滤器回调（fail-open 铁律）。

    本回调抛出的任何异常都不得阻断工具执行 ——
    异常时原样返回 gate（内核侧另有一层兜底）。
    """
    try:
        if not isinstance(gate, dict):
            return gate
        tool = gate.get('tool') or ''
        risk = resolve_tool_risk(tool)
        if risk is None or risk == 'auto':
            return gate                       # 非本系统工具 / 低风险档 -> 不干预
        svc = get_approval()
        if svc is None:
            return gate                       # 服务未构造 -> 不干预
        mode = svc.approval_mode()
        if mode == 'off':
            return gate
        # 两列均为 varchar(64)（migrations/0001_init.sql:10-11）。调用方可能传
        # int（如 agent_runner 的 context['agent_id']），直接比较/写入会报
        # 「操作符不存在: character varying = integer」，导致落单失败并 fail-open
        # 放行，故此处统一归一为字符串并截断到列宽。
        task_id = str(gate.get('task_id') or '')[:64]
        agent_id = str(gate.get('agent_id') or '')[:64]
        if mode != 'strict' and risk == 'approve_session':
            if svc.find_grant(task_id, agent_id, tool):
                return gate                   # 同任务内已批准 -> 放行
        approval_id = svc.request(task_id, agent_id, tool, gate.get('args'))
        if not approval_id:
            return gate                       # 落单失败 -> 放行（fail-open）
        blocked = dict(gate)
        blocked['allowed'] = False
        blocked['deny_reason'] = (
            f'Tool {tool} requires admin approval (ticket #{approval_id}). '
            f'Please go to "Agent Tools" page to approve, then retry.')
        return blocked
    except Exception as e:
        logger.warning('approval gate failed, fail-open: %s', e)
        return gate
