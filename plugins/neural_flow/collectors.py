"""collectors.py — 平台表增量游标采集器（方案 v1.3 §6.2，v1 范围：agent_token_logs）。

把内核表的新增行转写为平台域 span，**零内核改动**实现 LLM 全系统可视化
（方案 D2 偏差的对冲路线；缝① llm.call_completed 批准后可直供替代本采集器）。

互斥（D1-c 范式，alert_engine.scheduled_scan 同构）：advisory lock 必须**在
同一连接内**持锁干活——连接归还即释放会话锁，"借连接上锁 + 换连接解锁"
会使互斥形同虚设（本文件 v1 初版的实测教训）。

诚实性口径（方案 §7.6）：
  - agent_token_logs 无 elapsed_ms 列（engine._write_usage_logs INSERT 不含），
    采集 span 的 latency_ms 置 None（UI 显示"—"），绝不伪造；
  - created_at 为 TEXT DEFAULT NOW()（models.py:512）——比较必须 ::timestamptz（R18）；
  - 本表经 search_path public 兜底只读（主库表，§12.3 只读契约）。
"""
from __future__ import annotations

import threading

from plugin_manager.logger import get_plugin_logger

_log = get_plugin_logger("neural_flow")

_LOCK_KEY = 0x6E46_4C57  # "NFLW" — neural_flow 采集器专用 advisory lock 键
_BATCH = 200
_CURSORS: dict = {}
_CURSOR_READY: dict = {"agent_token_logs": False}
_CURSORS_LOCK = threading.Lock()


def init_cursors() -> None:
    """游标初始化：从表当前最大 id 起步（不回放历史，只采集增量）。

    初始化失败时**不写游标**、保持未就绪，采集器跳过本轮——若回落到 0，
    整个 agent_token_logs 历史会被 200 条/轮地全量回放。
    """
    from plugins._base.db import get_pooled_connection
    try:
        conn = get_pooled_connection()
        try:
            cur = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM agent_token_logs")
            row = cur.fetchone()
            with _CURSORS_LOCK:
                _CURSORS["agent_token_logs"] = row["m"] if row else 0
                _CURSOR_READY["agent_token_logs"] = True
        finally:
            conn.close()
    except Exception as err:
        _log.warning("cursor init failed (collector stays idle): %s", err)
        with _CURSORS_LOCK:
            _CURSOR_READY["agent_token_logs"] = False


def _collect_token_logs_conn(conn) -> int:
    """在**持锁连接上**读取增量并发射 llm 域 span。返回发射条数。

    读 agent_token_logs（主库 public，经 search_path 兜底解析，只读）；
    写走 sdk 双通道（借池的其他连接，不影响本连接持有的锁）。
    """
    from .sdk import emit_span

    with _CURSORS_LOCK:
        if not _CURSOR_READY.get("agent_token_logs"):
            return 0
        cursor = _CURSORS.get("agent_token_logs", 0)

    cur = conn.execute(
        "SELECT id, agent_id, agent_name, model_name, provider, "
        "       prompt_tokens, completion_tokens, total_tokens, module, "
        "       call_type "
        "FROM agent_token_logs WHERE id > %s ORDER BY id ASC LIMIT %s",
        (cursor, _BATCH),
    )
    rows = cur.fetchall()

    emitted = 0
    for r in rows:
        cursor = r["id"]
        payload_ok = (r.get("total_tokens") or 0) > 0
        emit_span(
            trace_id=r.get("module") or "platform",
            domain="llm", stage="llm", event="end",
            source="agent_token_logs", source_id=r["id"],
            entity={"type": "module", "id": r.get("module") or "unknown",
                    "label": str(r.get("agent_name") or r.get("module") or "unknown")},
            model=r.get("model_name"),
            tokens={"prompt": r.get("prompt_tokens") or 0,
                    "completion": r.get("completion_tokens") or 0} if payload_ok else None,
            # 金额恒为估算口径（无权威单价源）——前端按 token 估算展示并标 *
            cost_usd=0.0,
            latency_ms=None,  # 列不存在（C1 实证），禁伪造
            message="llm call (%s)" % (r.get("call_type") or "chat"),
            meta={"provider": r.get("provider"), "agent_id": r.get("agent_id")},
        )
        emitted += 1

    with _CURSORS_LOCK:
        _CURSORS["agent_token_logs"] = cursor
    return emitted


def run_once() -> dict:
    """采集器单轮：**同一连接** advisory lock → 读增量 → 解锁。

    失败即上抛（调度器留痕），禁 || true 掩盖；锁被其他 worker 持有时
    本轮回让（skipped），不排队不重试。
    """
    from plugins._base.db import get_pooled_connection
    from .models_nf import SCHEMA

    conn = get_pooled_connection()
    emitted = 0
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS %s" % SCHEMA)
        conn.execute("SET search_path TO %s, public" % SCHEMA)
        cur = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_LOCK_KEY,))
        row = cur.fetchone()
        if not (row and row["ok"]):
            return {"skipped": "lock-busy"}
        with _CURSORS_LOCK:
            ready = _CURSOR_READY.get("agent_token_logs", False)
        if not ready:
            return {"skipped": "cursor-not-ready"}
        try:
            emitted = _collect_token_logs_conn(conn)
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
                conn.commit()
            except Exception as unlock_err:
                _log.warning("advisory unlock failed: %s", unlock_err)
    finally:
        try:
            conn.close()   # 归还池；即使漏解锁，连接关闭也会释放会话锁
        except Exception:
            pass
    return {"emitted": emitted}
