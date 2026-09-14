"""jobs_queue.py — 桌面端异步分析任务队列（契约 §3 /api/jobs）

- sa_jobs 表驱动 + 进程内 daemon 轮询线程（懒启动，请求侧幂等拉起）；
- 跨 worker 安全：每任务先 pg_try_advisory_lock 独占，再原子认领 queued→running；
- 崩溃兜底：poll 循环定期把卡 running 超时的任务回置 queued（会话锁已随进程消亡释放）；
- LLM 空响应重试在 stock_skill._llm_analysis 内完成（SA-N3），本层只做状态流转与
  错误码归类（契约 §2.3：LLM_EMPTY / LLM_TIMEOUT / NO_PROVIDER / DB_ERROR）。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

try:
    from . import models_sa as sa
except ImportError:  # 顶层脚本运行兜底
    from plugins.stock_analysis import models_sa as sa

_log = logging.getLogger("stock_analysis.jobs_queue")

_POLL_INTERVAL = 2.0       # 空队轮询间隔（秒）
_STALE_MINUTES = 30        # running 超时回收阈值
_MAX_CONCURRENT = 2        # 并发封顶（finance 版 admin -w 2 内存约束）
_MAX_DISPATCH = 5          # 每轮扫描候选上限
_JOB_ID_RE_PREFIX = "j-"   # 与路由侧校验保持一致


def _new_job_id() -> str:
    return _JOB_ID_RE_PREFIX + uuid.uuid4().hex[:8]


def _ensure_tables():
    try:
        sa.ensure_tables()
    except Exception as err:
        _log.warning("jobs ensure_tables failed: %s", err)


def _ensure_poller():
    """幂等启动轮询线程。每个 worker 各自持有实例；advisory lock 保证任务只被执行一次。"""
    global _poller
    with _poller_lock:
        if _poller is None or not _poller.is_alive():
            _poller = threading.Thread(target=_poll_loop, name="sa_jobs_poller", daemon=True)
            _poller.start()


def submit_job(symbol: str, scope: str = "full", force: bool = False) -> dict:
    """幂等提交：同日同标的同 scope 已有成功任务且未 force → 复用；否则排队新任务。"""
    if not force:
        existing = sa.find_today_done_job(symbol, scope)
        if existing:
            _ensure_poller()   # 即使只读复用也保证 worker 在场（本进程可能从未跑过任务）
            return {"job_id": existing["job_id"], "status": "done",
                    "reuse": True, "result": existing.get("result")}
    job_id = _new_job_id()
    sa.create_job(job_id, symbol, scope)
    _ensure_poller()
    return {"job_id": job_id, "status": "queued", "reuse": False}


def submit_discuss_job(symbol: str, force: bool = False) -> dict:
    """多空对辩研判任务（接线点①，阶段 B 模式 A）。

    幂等语义与 submit_job 一致：同日同标的 scope='discuss' 已有 done 任务且未 force →
    复用；否则排队新任务（type/scope 均为 discuss，与既有 analyze 任务区分）。
    """
    if not force:
        existing = sa.find_today_done_job(symbol, "discuss")
        if existing:
            _ensure_poller()
            return {"job_id": existing["job_id"], "status": "done",
                    "reuse": True, "result": existing.get("result")}
    job_id = _new_job_id()
    sa.create_job(job_id, symbol, scope="discuss", job_type="discuss")
    _ensure_poller()
    return {"job_id": job_id, "status": "queued", "reuse": False}


def get_job_status(job_id: str) -> dict:
    """组装契约 /api/jobs/{id} 响应体。任务不存在返回 None（调用方回 404/400）。"""
    row = sa.get_job(job_id)
    if row is None:
        return None
    out = {"status": row["status"], "progress": row["progress"], "pct": row["pct"]}
    if row["status"] == "done" and row.get("result") is not None:
        out["result"] = row["result"]
    if row["status"] == "failed":
        out["error"] = {"code": row.get("error_code"), "message": row.get("error")}
    return out


# ── 轮询与执行 ──

def _poll_loop():
    _ensure_tables()
    while True:
        try:
            # 崩溃兜底：回收超时 running
            try:
                sa.recover_stale_jobs(_STALE_MINUTES)
            except Exception as err:
                _log.warning("recover_stale_jobs failed: %s", err)
            dispatched = 0
            for job_id in sa.list_queued_jobs(limit=_MAX_DISPATCH):
                if not _enqueue(job_id):   # 槽位耗尽即停，任务留在 queued 下轮再取
                    break
                dispatched += 1
            if dispatched == 0:
                time.sleep(_POLL_INTERVAL)
        except Exception as err:
            _log.warning("job poller error: %s", err)
            time.sleep(_POLL_INTERVAL * 2)


def _enqueue(job_id: str) -> bool:
    """空闲槽位领取：非阻塞获取信号量，成功才提交执行器（防满载时重复派发）。"""
    if not _slots.acquire(blocking=False):
        return False
    _executor.submit(_dispatch, job_id)
    return True


def _dispatch(job_id: str):
    """独立连接持有 advisory lock 直至任务完成（仿 batch.py 锁语义，避免占住池连接）。"""
    lock_conn = None
    held = False
    try:
        from plugins._base.db import get_pooled_connection
        lock_conn = get_pooled_connection()
        row = lock_conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(?)) AS ok",
            ("sa_job_" + job_id,)).fetchone()
        held = bool(row and row["ok"])
        if not held:
            return                       # 其他 worker 正在执行
        if not sa.claim_job(job_id):
            return                       # 已被认领，放弃
        _emit_job_event(job_id, "running")
        _process(job_id)
    except Exception as err:
        _log.error("job %s dispatch failed: %s", job_id, err)
        try:
            sa.finish_job(job_id, "failed", error_code="DB_ERROR", error=str(err))
        except Exception:
            pass
        try:
            _emit_job_event(job_id, "failed")
        except Exception:
            pass
    finally:
        if held and lock_conn is not None:
            try:
                lock_conn.execute("SELECT pg_advisory_unlock(hashtext(?))",
                                  ("sa_job_" + job_id,))
            except Exception:
                pass
            try:
                lock_conn.close()
            except Exception:
                pass
        _slots.release()


def _discuss_round_emitter(job_id: str, symbol: str):
    """对辩逐轮过程出流回调（B-3 SSE discuss 主题）。

    挂接 run_discussed_research 的 emit；任一失败仅告警，不影响对辩主链路。
    """
    def _emit(phase: str, content: str) -> None:
        try:
            sa.insert_sse_event("discuss", {
                "job_id": job_id,
                "type": "discuss",
                "symbol": symbol,
                "phase": phase,
                "content": content,
            })
        except Exception as err:
            _log.warning("discuss round event failed job=%s phase=%s: %s",
                         job_id, phase, err)
    return _emit


def _run_discuss_job(job: dict):
    """多空对辩研判任务主体：证据 → 四阶段 → 终态 + 信号落库 + KB 沉淀。

    成功：finish done(result)；信号以 kind='discuss' 走 record_signal（16:00 兑现回算
    自动覆盖），结论幂等入 KB（kb_stock_<symbol>_<日期>_discuss）。
    """
    job_id, symbol = job["job_id"], job["symbol"]
    try:
        from .discuss_research import run_discussed_research
        from .deep_research import _default_evidence
        evidence_text = _default_evidence(symbol)
        result = run_discussed_research(
            symbol, evidence_text, emit=_discuss_round_emitter(job_id, symbol))
        sa.finish_job(job_id, "done", result=result)
        try:
            sa.record_signal(symbol, "discuss", result)
        except Exception as err:
            _log.warning("record_signal discuss failed job=%s: %s", job_id, err)
        try:
            from .kb_publish import publish_analysis_kb
            publish_analysis_kb(symbol, "discuss", result)
        except Exception as err:
            _log.warning("kb publish discuss failed job=%s: %s", job_id, err)
        _emit_job_event(job_id, "done")
    except Exception as err:
        _log.error("discuss job %s crashed: %s", job_id, err)
        try:
            sa.finish_job(job_id, "failed", error_code=_classify_error(str(err)),
                          error=str(err))
            _emit_job_event(job_id, "failed")
        except Exception:
            pass


def _process(job_id: str):
    """执行任务并落终态。scope: full→llm 综合研判 / technical→技术面 / discuss→多空对辩。"""
    job = sa.get_job(job_id)
    if job is None:
        return
    try:
        if job["scope"] == "discuss":
            _run_discuss_job(job)
            return
        from .stock_skill import StockAnalysisSkill
        analysis_type = "llm" if job["scope"] == "full" else "technical"
        result = StockAnalysisSkill().analyze(job["symbol"], analysis_type=analysis_type)
        if result.error:
            code = _classify_error(result.error)
            sa.finish_job(job_id, "failed", error_code=code, error=result.error)
            _emit_job_event(job_id, "failed")
        else:
            sa.finish_job(job_id, "done", result=result.to_json())
            try:
                sa.record_signal(job["symbol"], analysis_type, result.to_json())
            except Exception as err:
                _log.warning("record_signal failed job=%s: %s", job_id, err)
            try:
                from .kb_publish import publish_analysis_kb
                publish_analysis_kb(job["symbol"], analysis_type, result.to_json())
            except Exception as err:
                _log.warning("kb publish failed job=%s: %s", job_id, err)
            _emit_job_event(job_id, "done")
    except Exception as err:
        _log.error("job %s crashed: %s", job_id, err)
        try:
            sa.finish_job(job_id, "failed", error_code="DB_ERROR", error=str(err))
            _emit_job_event(job_id, "failed")
        except Exception:
            pass


def _emit_job_event(job_id: str, status: str) -> None:
    """任务流转 → SSE 出流（D1-d；任一失败仅告警，绝不阻断任务主链路）。"""
    try:
        row = sa.get_job(job_id)
        if row is None:
            return
        sa.insert_sse_event("jobs", {
            "job_id": row["job_id"],
            "type": row.get("type") or "analyze",
            "symbol": row["symbol"],
            "scope": row["scope"],
            "status": status,
            "pct": row.get("pct"),
            "error_code": row.get("error_code") if status == "failed" else None,
        })
    except Exception as err:
        _log.warning("sse job event emit failed job=%s: %s", job_id, err)


def _classify_error(message: str) -> str | None:
    """契约 §2.3 错误码归类；未命中返回 None（error 文案原样透出）。"""
    m = str(message or "")
    low = m.lower()
    if "空响应" in m or "empty" in low:
        return "LLM_EMPTY"
    if "timeout" in low or "超时" in m:
        return "LLM_TIMEOUT"
    if "no provider" in low or "数据源暂时不可用" in m or "无可用源" in m:
        return "NO_PROVIDER"
    return None


# 模块级：懒加载的进程内线程与执行器
_poller = None
_poller_lock = threading.Lock()
_slots = threading.BoundedSemaphore(_MAX_CONCURRENT)   # 执行并发闸门（内存约束）
_executor = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT)
