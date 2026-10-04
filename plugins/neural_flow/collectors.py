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
MAX_INTERVAL_SECONDS = 3600   # NF-13：采集间隔上界（1h），超界钳制并告警
_CURSORS: dict = {}
_CURSOR_READY: dict = {"agent_token_logs": False}
_CURSORS_LOCK = threading.Lock()


def init_cursors(conn=None) -> None:
    """游标初始化：从表当前最大 id 起步（不回放历史，只采集增量）。

    初始化失败时**不写游标**、保持未就绪，采集器跳过本轮——若回落到 0，
    整个 agent_token_logs 历史会被 200 条/轮地全量回放。

    两种模式：
      - conn=None（activate 首次）：自借池连接，用完即归还；
      - 传入 conn（run_once 惰性自愈，NF-06）：复用调用方持有的同一条
        持锁连接，不另开、不替其关闭。先用 SAVEPOINT 包住 MAX(id) 查询——
        若查询失败，ROLLBACK TO SAVEPOINT 恢复事务，保证随后的
        pg_advisory_unlock 仍可执行（失败事务里任何 SQL 都会被拒，
        会让会话锁残留在归还池的连接上）。
    """
    own_conn = conn is None
    if own_conn:
        from plugins._base.db import get_pooled_connection
        conn = get_pooled_connection()
    try:
        if not own_conn:
            conn.execute("SAVEPOINT nf_cursor_init")
        cur = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM public.agent_token_logs")
        row = cur.fetchone()
        with _CURSORS_LOCK:
            _CURSORS["agent_token_logs"] = row["m"] if row else 0
            _CURSOR_READY["agent_token_logs"] = True
    except Exception as err:
        _log.warning("cursor init failed (collector stays idle): %s", err)
        if not own_conn:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT nf_cursor_init")
            except Exception:
                pass
        with _CURSORS_LOCK:
            _CURSOR_READY["agent_token_logs"] = False
    finally:
        if own_conn:
            conn.close()


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
        "FROM public.agent_token_logs WHERE id > %s ORDER BY id ASC LIMIT %s",
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

    conn = get_pooled_connection()
    emitted = 0
    try:
        # NF-12：schema 由 setup()→ensure_tables() 一次性创建（迁移职责），高频
        # 采集循环不再每轮 CREATE SCHEMA（默认 5s/轮）。
        # DEF-24：采集器读的主库表已全部显式 `public.` 限定，本连接不再依赖
        # search_path，故移除每轮 `SET search_path`（省一次往返，也不再受
        # 池化连接遗留 search_path 的影响）。
        cur = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_LOCK_KEY,))
        row = cur.fetchone()
        if not (row and row["ok"]):
            return {"skipped": "lock-busy"}
        with _CURSORS_LOCK:
            ready = _CURSOR_READY.get("agent_token_logs", False)
        if not ready:
            # NF-06 惰性自愈：锁内复用本连接补初始化一次；advisory 锁保证
            # 多 worker 只有一方执行，避免并发初始化。
            init_cursors(conn)
            with _CURSORS_LOCK:
                ready = _CURSOR_READY.get("agent_token_logs", False)
            if not ready:
                return {"skipped": "cursor-init-failed"}
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


def clamp_collector_interval(raw, log=None) -> int:
    """collector_interval_seconds 归一化（NF-13）。

    非数值（TypeError/ValueError）→ 默认 5；<1 → 1（下界，静默，同原口径）；
    > MAX_INTERVAL_SECONDS → 钳到上界并经 log 回调告警——误配不得静默地把
    近实时采集拖成「天级一次」。

    参数:
        raw: 配置原始值（int/str/None）。
        log: 可选告警回调 log(message, level)，由插件 self.log 注入。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 5
    if value < 1:
        return 1
    if value > MAX_INTERVAL_SECONDS:
        if log is not None:
            log("collector_interval_seconds=%s exceeds max %s; clamped"
                % (value, MAX_INTERVAL_SECONDS), "warning")
        return MAX_INTERVAL_SECONDS
    return value
