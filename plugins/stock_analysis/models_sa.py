"""models_sa.py — stock_analysis 插件独立 schema 数据层（标准 v1.7 §9.1/§11.2）

统一经 _base/db.get_pooled_connection() 借连接（禁自建连接工厂），
独立 schema `stock_analysis` 承载标的池 / 批量运行 / 分析结果 / 信号日志四张表。
建表即 SELECT 验证；所有 INSERT 均带幂等锚点（ON CONFLICT DO NOTHING）。
"""

import json
import logging
import threading
from contextlib import contextmanager

from plugins._base.db import get_pooled_connection

SCHEMA = "stock_analysis"

_log = logging.getLogger("stock_analysis.models_sa")

# P2-12：ensure_tables 建表兜底做进程内记忆——成功后本 worker 不再每请求重复全量 DDL/SELECT，
# 仅在全新部署/清库后的首次访问触发一次建表；失败不记忆，下个请求自动重试。
_tables_ready = False
_ready_report: dict = {}
_ensure_lock = threading.Lock()

TABLES = ("sa_watchlist", "sa_analysis_run", "sa_analysis_result",
          "sa_signal_log", "sa_signal_realized", "sa_jobs",
          "sa_alerts", "sa_alert_events", "sa_sse_events",
          "sa_corp_action", "sa_adj_factor", "sa_classification",
          "sa_compliance_audit", "sa_compliance_approval", "sa_compliance_silence")

DDL = [
    # 标的池
    """
    CREATE TABLE IF NOT EXISTS sa_watchlist (
        id          SERIAL PRIMARY KEY,
        symbol      VARCHAR(12)  NOT NULL UNIQUE,
        alias       VARCHAR(64),
        kind        VARCHAR(16)  DEFAULT 'technical',
        enabled     SMALLINT     DEFAULT 1,
        note        TEXT,
        created_at  TIMESTAMPTZ  DEFAULT now()
    )""",
    # 批量运行批次（run_id = 交易日 YYYYMMDD，UNIQUE 天然去重）
    """
    CREATE TABLE IF NOT EXISTS sa_analysis_run (
        id           SERIAL PRIMARY KEY,
        run_id       VARCHAR(10) NOT NULL UNIQUE,
        kind         VARCHAR(16) NOT NULL,
        status       VARCHAR(16) NOT NULL,          -- running / done / degraded
        total        INT DEFAULT 0,
        ok           INT DEFAULT 0,
        failed       INT DEFAULT 0,
        llm_calls    INT DEFAULT 0,
        started_at   TIMESTAMPTZ DEFAULT now(),
        finished_at  TIMESTAMPTZ
    )""",
    # 单标的单次分析结果（幂等锚点 run_id+symbol+kind）
    """
    CREATE TABLE IF NOT EXISTS sa_analysis_result (
        id           SERIAL PRIMARY KEY,
        run_id       VARCHAR(10) NOT NULL,
        symbol       VARCHAR(12) NOT NULL,
        kind         VARCHAR(16) NOT NULL,
        signal       VARCHAR(8),
        confidence   NUMERIC(5, 2),
        score        INT,
        payload      JSONB NOT NULL,
        data_sources JSONB,
        created_at   TIMESTAMPTZ DEFAULT now(),
        CONSTRAINT uq_run_symbol_kind UNIQUE (run_id, symbol, kind)
    )""",
    # 信号历史（逐日去重锚点 symbol+trade_date+kind）
    """
    CREATE TABLE IF NOT EXISTS sa_signal_log (
        id           SERIAL PRIMARY KEY,
        symbol       VARCHAR(12) NOT NULL,
        trade_date   DATE NOT NULL,
        kind         VARCHAR(16) NOT NULL,
        signal       VARCHAR(8),
        confidence   NUMERIC(5, 2),
        reasons      JSONB,
        created_at   TIMESTAMPTZ DEFAULT now(),
        CONSTRAINT uq_symbol_tradedate_kind UNIQUE (symbol, trade_date, kind)
    )""",
    # 信号兑现回算（P0-2；锚点与 sa_signal_log 一致，只读回算，不动写入路径）
    """
    CREATE TABLE IF NOT EXISTS sa_signal_realized (
        id            SERIAL PRIMARY KEY,
        symbol        VARCHAR(12) NOT NULL,
        trade_date    DATE NOT NULL,
        kind          VARCHAR(16) NOT NULL,
        signal        VARCHAR(8),
        signal_close  NUMERIC(12, 4),
        ret_5d        NUMERIC(9, 4),
        ret_20d       NUMERIC(9, 4),
        hit_5d        SMALLINT,
        hit_20d       SMALLINT,
        computed_at   TIMESTAMPTZ DEFAULT now(),
        CONSTRAINT uq_realized_sym_date_kind UNIQUE (symbol, trade_date, kind)
    )""",
    # 桌面端异步分析任务（D1-b，契约 §3 /api/jobs）
    """
    CREATE TABLE IF NOT EXISTS sa_jobs (
        id           SERIAL PRIMARY KEY,
        job_id       VARCHAR(32) NOT NULL UNIQUE,
        type         VARCHAR(16) NOT NULL DEFAULT 'analyze',
        symbol       VARCHAR(12) NOT NULL,
        scope        VARCHAR(16) NOT NULL DEFAULT 'full',
        status       VARCHAR(16) NOT NULL DEFAULT 'queued',
        progress     SMALLINT    NOT NULL DEFAULT 0,
        pct          SMALLINT    NOT NULL DEFAULT 0,
        result       JSONB,
        error_code   VARCHAR(16),
        error        TEXT,
        reuse        BOOLEAN     NOT NULL DEFAULT FALSE,
        created_at   TIMESTAMPTZ DEFAULT now(),
        started_at   TIMESTAMPTZ,
        finished_at  TIMESTAMPTZ
    )""",
    # 桌面端告警规则（D1-c，契约 §3 /api/alerts；type 对齐桌面 ALERT_TYPE_META 6 类）
    """
    CREATE TABLE IF NOT EXISTS sa_alerts (
        id                SERIAL PRIMARY KEY,
        symbol            VARCHAR(12) NOT NULL,
        name              VARCHAR(64),
        type              VARCHAR(20) NOT NULL,
        threshold         NUMERIC(14, 4),
        channel           VARCHAR(32) NOT NULL DEFAULT 'in_app',
        status            VARCHAR(16) NOT NULL DEFAULT 'active',
        silent_from       TIME,
        silent_to         TIME,
        last_triggered_at TIMESTAMPTZ,
        last_value        NUMERIC(14, 4),
        last_signal       VARCHAR(8),
        created_at        TIMESTAMPTZ DEFAULT now()
    )""",
    # 告警触发事件流水（供历史回看；SSE 广播只取实时触发）
    """
    CREATE TABLE IF NOT EXISTS sa_alert_events (
        id         SERIAL PRIMARY KEY,
        alert_id   INT NOT NULL,
        symbol     VARCHAR(12) NOT NULL,
        type       VARCHAR(20) NOT NULL,
        threshold  NUMERIC(14, 4),
        observed   NUMERIC(14, 4),
        message    TEXT,
        created_at TIMESTAMPTZ DEFAULT now()
    )""",
    # SSE 跨 worker 事件出流表（D1-d，契约 §4）：写入方（告警/任务）插行，
    # SSE 连接按 id 游标轮询；id 自增即事件序号（Last-Event-ID 补发依据）。
    """
    CREATE TABLE IF NOT EXISTS sa_sse_events (
        id         BIGSERIAL PRIMARY KEY,
        topic      VARCHAR(16) NOT NULL,
        payload    JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    )""",
    # ---- v2 新增：复权与股本事件 ----
    # 除权除息事件（分红/送转/配股）。锚点 symbol+ex_date+action_type 幂等。
    # 只存 div_proc='实施' 的记录，预案会反复修改导致因子跳变。
    """
    CREATE TABLE IF NOT EXISTS sa_corp_action (
        id            SERIAL PRIMARY KEY,
        symbol        VARCHAR(12)  NOT NULL,
        ex_date       DATE         NOT NULL,
        cash_div      NUMERIC(10, 4) DEFAULT 0,
        split_ratio   NUMERIC(8, 4) DEFAULT 1.0,
        rights_ratio  NUMERIC(8, 4) DEFAULT 0,
        rights_price  NUMERIC(10, 4) DEFAULT 0,
        action_type   VARCHAR(16)  DEFAULT 'dividend',
        source        VARCHAR(32)  DEFAULT 'tushare',
        created_at    TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_corp_action UNIQUE (symbol, ex_date, action_type)
    )""",
    # 复权因子缓存（按日存储，避免每次请求重算）。
    # factor_qfq 锚定最新价=1，factor_hfq 锚定首日价=1。
    """
    CREATE TABLE IF NOT EXISTS sa_adj_factor (
        id            SERIAL PRIMARY KEY,
        symbol        VARCHAR(12)  NOT NULL,
        trade_date    DATE         NOT NULL,
        factor_qfq    NUMERIC(14, 8) DEFAULT 1.0,
        factor_hfq    NUMERIC(14, 8) DEFAULT 1.0,
        computed_at   TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_adj_factor UNIQUE (symbol, trade_date)
    )""",
    # 行业分类（申万/中信/GICS），支持 point-in-time 查询。
    # v2 回测铁律第 5 条：使用时点行业分类，避免前视偏差。
    """
    CREATE TABLE IF NOT EXISTS sa_classification (
        id            SERIAL PRIMARY KEY,
        symbol        VARCHAR(12)  NOT NULL,
        standard      VARCHAR(16)  NOT NULL,
        industry_l1   VARCHAR(32),
        industry_l2   VARCHAR(32),
        industry_l3   VARCHAR(32),
        effective_from DATE        NOT NULL,
        effective_to  DATE,
        source        VARCHAR(32)  DEFAULT 'tushare',
        created_at    TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_classification UNIQUE (symbol, standard, effective_from)
    )""",
    # ---- P2：合规与审计 ----
    # 审计留痕：所有分析请求留 who/when/symbol/evidence_hash/model/cost，保留 ≥3 年
    """
    CREATE TABLE IF NOT EXISTS sa_compliance_audit (
        id              BIGSERIAL PRIMARY KEY,
        who             VARCHAR(64)  NOT NULL,
        symbol          VARCHAR(12),
        action          VARCHAR(32)  NOT NULL,
        evidence_hash   VARCHAR(64),
        model           VARCHAR(64),
        prompt_version  VARCHAR(32),
        indicator_version VARCHAR(32),
        agent_versions  JSONB,
        cost            NUMERIC(10, 4),
        result_summary  JSONB,
        created_at      TIMESTAMPTZ  DEFAULT now()
    )""",
    # 审批流：草稿 → 提交 → 复核（人）→ 发布
    """
    CREATE TABLE IF NOT EXISTS sa_compliance_approval (
        id           SERIAL PRIMARY KEY,
        result_id    INT,
        result_type  VARCHAR(32)  NOT NULL,
        title        VARCHAR(128),
        payload      JSONB        NOT NULL,
        status       VARCHAR(16)  NOT NULL DEFAULT 'draft',
        submitter    VARCHAR(64),
        reviewer     VARCHAR(64),
        review_note  TEXT,
        submitted_at TIMESTAMPTZ,
        reviewed_at  TIMESTAMPTZ,
        created_at   TIMESTAMPTZ  DEFAULT now(),
        updated_at   TIMESTAMPTZ  DEFAULT now()
    )""",
    # 静默期配置：用户登记持仓后，相关标的发布前 N 日与后 N 日禁止出结论
    """
    CREATE TABLE IF NOT EXISTS sa_compliance_silence (
        id           SERIAL PRIMARY KEY,
        user_id      VARCHAR(64)  NOT NULL,
        symbol       VARCHAR(12)  NOT NULL,
        days_before  INT          NOT NULL DEFAULT 1,
        days_after   INT          NOT NULL DEFAULT 1,
        position_date DATE,
        enabled      SMALLINT     DEFAULT 1,
        created_at   TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_silence_user_sym UNIQUE (user_id, symbol, position_date)
    )""",
]


@contextmanager
def get_db():
    """借池连接并切换到插件 schema；with 块退出自动 commit/rollback + 归还池。"""
    with get_pooled_connection() as conn:
        conn.execute("SET search_path TO %s, public" % SCHEMA)
        yield conn


def ensure_tables():
    """建表并逐表 SELECT 验证（建表即验证，任一失败即 raise，调用方决定降级）。

    P2-12：成功后进程内记忆（_tables_ready），避免 #SA-20260831-05 兜底在
    每请求热路径重复执行全量 DDL/SELECT；失败不记忆，下次请求自动重试。
    并发首调用由 _ensure_lock 串行化（CREATE TABLE 并发有竞态风险）。
    """
    global _tables_ready, _ready_report
    if _tables_ready:
        return _ready_report
    with _ensure_lock:
        if _tables_ready:                          # double-checked：并发首调只跑一次
            return _ready_report
        report = {}
        with get_db() as conn:
            conn.execute("CREATE SCHEMA IF NOT EXISTS %s" % SCHEMA)
            for ddl in DDL:
                conn.execute(ddl)
            for table in TABLES:
                conn.execute("SELECT 1 FROM %s LIMIT 1" % table).fetchone()
                report[table] = True
        _tables_ready = True
        _ready_report = report
    _log.info("ensure_tables OK: %s", sorted(report))
    return report


def create_run(run_id: str, kind: str, total: int) -> None:
    """登记批次；run_id 已存在则忽略（幂等锚点）。"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_analysis_run (run_id, kind, status, total) "
            "VALUES (?, ?, 'running', ?) "
            "ON CONFLICT (run_id) DO NOTHING", (run_id, kind, total))


def run_exists(run_id: str) -> bool:
    """该交易日批次是否已完成（避免双 worker 重复跑）。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM sa_analysis_run WHERE run_id = ? AND status = 'done'",
            (run_id,)).fetchone()
        return row is not None


def finish_run(run_id: str, ok: int, failed: int, status: str = "done",
               llm_calls: int = 0) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE sa_analysis_run SET status = ?, ok = ?, failed = ?, "
            "llm_calls = ?, finished_at = now() WHERE run_id = ?",
            (status, ok, failed, llm_calls, run_id))


def get_run_stats(run_id: str):
    """查询批次统计（#SA-20260831-13：already-run 分支返回完整契约用）。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT run_id, kind, status, total, ok, failed "
            "FROM sa_analysis_run WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def record_signal(symbol: str, kind: str, result_json: dict) -> None:
    """信号独立落库（A-S4 全路径闭环）：单次 analyze / jobs 任务 / 告警引擎均可调用。

    与 upsert_result 内的信号写入同锚点幂等：(symbol, trade_date, kind) ON CONFLICT DO NOTHING。
    无信号/信号为空直接返回，不落脏行。
    """
    sig = result_json.get("signal") or {}
    sigval = (str(sig.get("signal") or "")[:8]) or None
    if not sigval:
        return
    confidence = sig.get("confidence")
    reasons = json.dumps(sig.get("reasons") or [], ensure_ascii=False)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_signal_log (symbol, trade_date, kind, signal, confidence, reasons) "
            "VALUES (?, current_date, ?, ?, ?, ?::jsonb) "
            "ON CONFLICT (symbol, trade_date, kind) DO NOTHING",
            (symbol, kind, sigval, confidence, reasons))


def upsert_result(run_id: str, symbol: str, kind: str, result_json: dict) -> None:
    """落单条分析结果 + 信号日志；双表均 ON CONFLICT DO NOTHING 幂等。"""
    sig = result_json.get("signal") or {}
    sigval = (str(sig.get("signal") or "")[:8]) or None
    confidence = sig.get("confidence")
    score = (result_json.get("data") or {}).get("score")
    payload = json.dumps(result_json, ensure_ascii=False, default=str)
    sources = json.dumps(result_json.get("data", {}).get("data_sources"),
                         ensure_ascii=False, default=str)
    reasons = json.dumps(sig.get("reasons") or [], ensure_ascii=False)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_analysis_result "
            "(run_id, symbol, kind, signal, confidence, score, payload, data_sources) "
            "VALUES (?, ?, ?, ?, ?, ?, ?::jsonb, ?::jsonb) "
            "ON CONFLICT (run_id, symbol, kind) DO NOTHING",
            (run_id, symbol, kind, sigval, confidence, score, payload, sources))
        conn.execute(
            "INSERT INTO sa_signal_log (symbol, trade_date, kind, signal, confidence, reasons) "
            "VALUES (?, current_date, ?, ?, ?, ?::jsonb) "
            "ON CONFLICT (symbol, trade_date, kind) DO NOTHING",
            (symbol, kind, sigval, confidence, reasons))


def watchlist_symbols(enabled_only: bool = True):
    """返回标的池 symbol 列表。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT symbol FROM sa_watchlist"
            + (" WHERE enabled = 1" if enabled_only else "")
            + " ORDER BY id").fetchall()
        return [r["symbol"] for r in rows]


# ── P0-2 信号兑现回算（只读）──

def insert_realized(symbol, trade_date, kind, signal, signal_close,
                    ret_5d, ret_20d, hit_5d, hit_20d) -> None:
    """落一条信号兑现回算；锚点 (symbol, trade_date, kind) 幂等（ON CONFLICT DO NOTHING）。"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_signal_realized "
            "(symbol, trade_date, kind, signal, signal_close, ret_5d, ret_20d, hit_5d, hit_20d) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (symbol, trade_date, kind) DO NOTHING",
            (symbol, trade_date, kind, signal, signal_close,
             ret_5d, ret_20d, hit_5d, hit_20d))


def list_unrealized_signals(days_back: int, cutoff) -> list:
    """尚未回算、且信号日 <= cutoff（前向窗口已满）的信号，最多 500 条/次。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT l.symbol, l.trade_date, l.kind, l.signal "
            "FROM sa_signal_log l "
            "LEFT JOIN sa_signal_realized r "
            "  ON r.symbol = l.symbol AND r.trade_date = l.trade_date AND r.kind = l.kind "
            "WHERE r.id IS NULL AND l.trade_date >= current_date - ? AND l.trade_date <= ? "
            "ORDER BY l.trade_date DESC LIMIT 500",
            (days_back, cutoff)).fetchall()
        return [dict(r) for r in rows]


def get_realized_summary(days: int = 90) -> list:
    """按 kind/signal 聚合命中率与平均前向收益。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT kind, signal, COUNT(*) AS n, "
            "  ROUND(AVG(ret_5d)::numeric, 4)  AS avg_ret_5d, "
            "  ROUND(AVG(ret_20d)::numeric, 4) AS avg_ret_20d, "
            "  ROUND(100.0 * SUM(CASE WHEN hit_5d  = 1 THEN 1 ELSE 0 END) "
            "        / NULLIF(COUNT(hit_5d), 0), 1)  AS hit_rate_5d, "
            "  ROUND(100.0 * SUM(CASE WHEN hit_20d = 1 THEN 1 ELSE 0 END) "
            "        / NULLIF(COUNT(hit_20d), 0), 1) AS hit_rate_20d "
            "FROM sa_signal_realized WHERE trade_date >= current_date - ? "
            "GROUP BY kind, signal ORDER BY kind, signal",
            (days,)).fetchall()
        return [dict(r) for r in rows]


# ── D1-b：桌面端异步分析任务（sa_jobs）──
# 状态机：queued → running → done / failed；progress/pct 供客户端轮询展示。

def create_job(job_id: str, symbol: str, scope: str, job_type: str = "analyze") -> None:
    """登记排队任务；job_id 唯一（幂等锚点）。"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_jobs (job_id, type, symbol, scope, status) "
            "VALUES (?, ?, ?, ?, 'queued') "
            "ON CONFLICT (job_id) DO NOTHING", (job_id, job_type, symbol, scope))


def get_job(job_id: str):
    """查询任务行；不存在返回 None。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT job_id, type, symbol, scope, status, progress, pct, "
            "result, error_code, error, created_at "
            "FROM sa_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def find_today_done_job(symbol: str, scope: str):
    """同日同标的同 scope 的成功任务（幂等复用锚点，契约 §3 force=false 语义）。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT job_id, status, result FROM sa_jobs "
            "WHERE symbol = ? AND scope = ? AND status = 'done' "
            "AND created_at::date = current_date "
            "ORDER BY id DESC LIMIT 1", (symbol, scope)).fetchone()
    return dict(row) if row else None


def list_queued_jobs(limit: int = 5) -> list:
    """轮询器取待执行任务（先进先出）。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT job_id FROM sa_jobs WHERE status = 'queued' "
            "ORDER BY id ASC LIMIT ?", (limit,)).fetchall()
        return [r["job_id"] for r in rows]


def claim_job(job_id: str) -> bool:
    """advisory lock 内原子认领（queued→running）；已被认领返回 False。"""
    with get_db() as conn:
        row = conn.execute(
            "UPDATE sa_jobs SET status = 'running', progress = 50, pct = 50, "
            "started_at = now() WHERE job_id = ? AND status = 'queued' RETURNING job_id",
            (job_id,)).fetchone()
        return row is not None


def finish_job(job_id: str, status: str, result: dict = None,
               error_code: str = None, error: str = None) -> None:
    """任务收尾：done（result）或 failed（error_code/error）。"""
    with get_db() as conn:
        if status == "done":
            conn.execute(
                "UPDATE sa_jobs SET status = 'done', progress = 100, pct = 100, "
                "result = ?::jsonb, finished_at = now() WHERE job_id = ?",
                (json.dumps(result, ensure_ascii=False, default=str), job_id))
        else:
            conn.execute(
                "UPDATE sa_jobs SET status = 'failed', error_code = ?, error = ?, "
                "finished_at = now() WHERE job_id = ?",
                (error_code, error, job_id))


def recover_stale_jobs(minutes: int = 30) -> int:
    """worker 崩溃兜底：长时间卡 running 的任务回置 queued（进程消亡时其 advisory
    lock 已随会话释放，可安全重跑）。"""
    with get_db() as conn:
        row = conn.execute(
            "UPDATE sa_jobs SET status = 'queued' "
            "WHERE status = 'running' AND started_at < now() - make_interval(mins => ?) "
            "RETURNING job_id", (minutes,)).fetchall()
        return len(row)


# ── D1-c：桌面端告警规则（sa_alerts / sa_alert_events）──
# status 枚举与桌面零转换：active | triggered | expired | disabled。

def list_alerts(symbol: str = None) -> list:
    """规则列表（时间列已格式化为文本，供 API 直出）。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, symbol, name, type, threshold, channel, status, "
            "to_char(silent_from, 'HH24:MI') AS silent_from, "
            "to_char(silent_to, 'HH24:MI') AS silent_to, "
            "to_char(last_triggered_at, 'YYYY-MM-DD HH24:MI:SS') AS last_triggered_at, "
            "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
            "FROM sa_alerts"
            + (" WHERE symbol = ?" if symbol else "")
            + " ORDER BY id DESC", (symbol,) if symbol else ()).fetchall()
        return [dict(r) for r in rows]


def get_alert_row(alert_id: int):
    """单条规则（时间列已格式化，同 list_alerts 口径），供创建响应直出。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, symbol, name, type, threshold, channel, status, "
            "to_char(silent_from, 'HH24:MI') AS silent_from, "
            "to_char(silent_to, 'HH24:MI') AS silent_to, "
            "to_char(last_triggered_at, 'YYYY-MM-DD HH24:MI:SS') AS last_triggered_at, "
            "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
            "FROM sa_alerts WHERE id = ?", (alert_id,)).fetchone()
    return dict(row) if row else None


def create_alert(symbol: str, name, alert_type: str, threshold, channel: str,
                 silent_from: str = None, silent_to: str = None) -> int:
    """创建规则，返回新 id。"""
    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO sa_alerts (symbol, name, type, threshold, channel, "
            "silent_from, silent_to) "
            "VALUES (?, ?, ?, ?, ?, ?::time, ?::time) RETURNING id",
            (symbol, name, alert_type, threshold, channel,
             silent_from or None, silent_to or None)).fetchone()
        return int(row["id"])


def delete_alert(alert_id: int) -> bool:
    """删除规则；不存在返回 False。

    T4.7 修复：`sa_alert_events.alert_id` 没有外键（见本文件 DDL），硬删除规则会把
    它的事件流水永久留在库里成为孤儿行（R1 实测 sa_alerts=0 而 sa_alert_events=11）。
    因此删除规则时在同一事务内级联清掉其事件；
    需要保留历史回看的场景应改用 status='disabled' 停用，而不是删除。
    """
    with get_db() as conn:
        row = conn.execute(
            "DELETE FROM sa_alerts WHERE id = ? RETURNING id", (alert_id,)).fetchone()
        if row is None:
            return False
        purged = conn.execute(
            "DELETE FROM sa_alert_events WHERE alert_id = ?", (alert_id,)).rowcount
        if purged:
            _log.info("delete_alert #%s 级联清理事件 %d 条", alert_id, purged)
        return True


def duplicate_active_alert(symbol: str, alert_type: str, threshold) -> bool:
    """活跃规则幂等防重（expired/disabled 历史副本不参与）。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM sa_alerts WHERE symbol = ? AND type = ? "
            "AND threshold IS NOT DISTINCT FROM ? AND status IN ('active', 'triggered') "
            "LIMIT 1", (symbol, alert_type, threshold)).fetchone()
        return row is not None


def evaluable_alerts() -> list:
    """引擎扫描对象：active（可触发）+ triggered（可解除复位的规则）。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, symbol, name, type, threshold, channel, status, "
            "silent_from, silent_to, last_signal "
            "FROM sa_alerts WHERE status IN ('active', 'triggered') "
            "ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def set_alert_triggered(alert_id: int, observed, signal: str = None) -> None:
    """触发：status→triggered + 触发时刻/观测值留痕；signal_change 同时推进基线（新信号）。"""
    with get_db() as conn:
        conn.execute(
            "UPDATE sa_alerts SET status = 'triggered', "
            "last_triggered_at = now(), last_value = ?, "
            "last_signal = COALESCE(?, last_signal) "
            "WHERE id = ? AND status = 'active'", (observed, signal, alert_id))


def rearm_alert(alert_id: int) -> None:
    """解除触发：条件已不再满足 → 回 active，供下次穿越再次触发。"""
    with get_db() as conn:
        conn.execute(
            "UPDATE sa_alerts SET status = 'active' "
            "WHERE id = ? AND status = 'triggered'", (alert_id,))


def set_alert_baseline(alert_id: int, signal: str) -> None:
    """signal_change 首扫建档：记录当前信号为基线，不触发。"""
    with get_db() as conn:
        conn.execute(
            "UPDATE sa_alerts SET last_signal = ? "
            "WHERE id = ? AND last_signal IS NULL", (signal, alert_id))


def insert_alert_event(alert_id: int, symbol: str, alert_type: str,
                       threshold, observed, message: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_alert_events "
            "(alert_id, symbol, type, threshold, observed, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (alert_id, symbol, alert_type, threshold, observed, message))


def list_alert_events(alert_id: int = None, limit: int = 50) -> list:
    """事件流水回看（时间列已格式化为文本，供 API 直出），新→旧排序。"""
    sql = ("SELECT id, alert_id, symbol, type, threshold, observed, message, "
           "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
           "FROM sa_alert_events"
           + (" WHERE alert_id = ?" if alert_id else "")
           + " ORDER BY id DESC LIMIT ?")
    params = ((alert_id, limit) if alert_id else (limit,))
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


# ── D1-d：SSE 跨 worker 事件出流（sa_sse_events，契约 §4）──

def insert_sse_event(topic: str, payload: dict) -> None:
    """写入 SSE 出流行（topic ∈ alerts/jobs）。payload 须可 JSON 序列化（Decimal 转 float）。"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_sse_events (topic, payload) VALUES (?, ?::jsonb)",
            (topic, json.dumps(payload, ensure_ascii=False, default=_json_default)))


def list_sse_events(after_id: int = None, topics=None, limit: int = 200) -> list:
    """游标拉取出流事件（id 升序）；payload 还原 dict。topics 为空视为不过滤。"""
    filters, params = [], []
    if after_id is not None:
        filters.append("id > ?")
        params.append(after_id)
    if topics:
        filters.append("topic IN (%s)" % ", ".join("?" * len(topics)))
        params.extend(topics)
    where = (" WHERE " + " AND ".join(filters)) if filters else ""
    sql = ("SELECT id, topic, payload::text AS payload "
           "FROM sa_sse_events%s ORDER BY id ASC LIMIT ?" % where)
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"id": r["id"], "topic": r["topic"], "payload": json.loads(r["payload"])}
            for r in rows]


def prune_sse_events(retention_days: int = 1) -> int:
    """清理超期出流事件防表膨胀；返回删除行数（幂等可重复执行）。"""
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM sa_sse_events WHERE created_at < now() - make_interval(days => ?)",
            (retention_days,))
        return cur.rowcount if cur is not None else 0


# ── v2：复权事件 / 复权因子 / 行业分类 ──

def upsert_corp_action(symbol: str, ex_date, cash_div: float, split_ratio: float,
                       rights_ratio: float = 0, rights_price: float = 0,
                       action_type: str = "dividend", source: str = "tushare") -> None:
    """落一条除权除息事件；锚点 (symbol, ex_date, action_type) 幂等。"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_corp_action "
            "(symbol, ex_date, cash_div, split_ratio, rights_ratio, rights_price, "
            "action_type, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (symbol, ex_date, action_type) DO UPDATE SET "
            "cash_div = EXCLUDED.cash_div, split_ratio = EXCLUDED.split_ratio, "
            "rights_ratio = EXCLUDED.rights_ratio, rights_price = EXCLUDED.rights_price",
            (symbol, ex_date, cash_div, split_ratio, rights_ratio, rights_price,
             action_type, source))


def list_corp_actions(symbol: str, start_date=None, end_date=None) -> list:
    """查询某标的的除权除息事件列表。"""
    sql = ("SELECT symbol, ex_date, cash_div, split_ratio, rights_ratio, "
           "rights_price, action_type, source FROM sa_corp_action WHERE symbol = ?")
    params = [symbol]
    if start_date:
        sql += " AND ex_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND ex_date <= ?"
        params.append(end_date)
    sql += " ORDER BY ex_date"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def upsert_adj_factor(symbol: str, trade_date, factor_qfq: float,
                      factor_hfq: float) -> None:
    """缓存某标的某日的复权因子；锚点 (symbol, trade_date) 幂等。"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_adj_factor (symbol, trade_date, factor_qfq, factor_hfq) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (symbol, trade_date) DO UPDATE SET "
            "factor_qfq = EXCLUDED.factor_qfq, factor_hfq = EXCLUDED.factor_hfq, "
            "computed_at = now()",
            (symbol, trade_date, factor_qfq, factor_hfq))


def get_adj_factors(symbol: str, start_date=None, end_date=None) -> list:
    """查询某标的的复权因子序列。"""
    sql = ("SELECT trade_date, factor_qfq, factor_hfq FROM sa_adj_factor "
           "WHERE symbol = ?")
    params = [symbol]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def upsert_classification(symbol: str, standard: str, industry_l1: str = None,
                          industry_l2: str = None, industry_l3: str = None,
                          effective_from=None, effective_to=None,
                          source: str = "tushare") -> None:
    """落一条行业分类记录；锚点 (symbol, standard, effective_from) 幂等。"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_classification "
            "(symbol, standard, industry_l1, industry_l2, industry_l3, "
            "effective_from, effective_to, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (symbol, standard, effective_from) DO UPDATE SET "
            "industry_l1 = EXCLUDED.industry_l1, industry_l2 = EXCLUDED.industry_l2, "
            "industry_l3 = EXCLUDED.industry_l3, effective_to = EXCLUDED.effective_to",
            (symbol, standard, industry_l1, industry_l2, industry_l3,
             effective_from, effective_to, source))


def get_classification(symbol: str, standard: str = "sw", as_of=None) -> dict:
    """查询某标的在指定时点的行业分类（point-in-time）。"""
    sql = ("SELECT standard, industry_l1, industry_l2, industry_l3, "
           "effective_from, effective_to FROM sa_classification "
           "WHERE symbol = ? AND standard = ?")
    params = [symbol, standard]
    if as_of:
        sql += " AND effective_from <= ? AND (effective_to IS NULL OR effective_to >= ?)"
        params.extend([as_of, as_of])
    sql += " ORDER BY effective_from DESC LIMIT 1"
    with get_db() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else {}


# ── P2：合规与审计 ──

def insert_audit(who: str, action: str, symbol: str = None,
                 evidence_hash: str = None, model: str = None,
                 prompt_version: str = None, indicator_version: str = None,
                 agent_versions: dict = None, cost: float = None,
                 result_summary: dict = None) -> None:
    """记录一条审计留痕（who/when/symbol/evidence_hash/model/cost）。"""
    payload = json.dumps(result_summary, ensure_ascii=False, default=str) if result_summary else None
    agents = json.dumps(agent_versions, ensure_ascii=False) if agent_versions else None
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_compliance_audit "
            "(who, symbol, action, evidence_hash, model, prompt_version, "
            "indicator_version, agent_versions, cost, result_summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?::jsonb)",
            (who, symbol, action, evidence_hash, model, prompt_version,
             indicator_version, agents, cost, payload))


def list_audit_logs(symbol: str = None, who: str = None,
                    limit: int = 100, date_from: str = None,
                    date_to: str = None) -> list:
    """查询审计日志（新→旧），可按 symbol/who/日期范围过滤。"""
    sql = ("SELECT id, who, symbol, action, evidence_hash, model, "
           "prompt_version, indicator_version, agent_versions::text AS agent_versions, "
           "cost, result_summary::text AS result_summary, "
           "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
           "FROM sa_compliance_audit WHERE 1=1")
    params = []
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    if who:
        sql += " AND who = ?"
        params.append(who)
    if date_from:
        sql += " AND created_at >= ?::timestamp"
        params.append(date_from)
    if date_to:
        sql += " AND created_at < (?::timestamp + interval '1 day')"
        params.append(date_to)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("agent_versions"):
                d["agent_versions"] = json.loads(d["agent_versions"])
            if d.get("result_summary"):
                d["result_summary"] = json.loads(d["result_summary"])
            result.append(d)
        return result


def create_approval(result_type: str, payload: dict, title: str = None,
                    result_id: int = None, submitter: str = None) -> int:
    """创建审批记录（初始 status='draft'），返回新 id。"""
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO sa_compliance_approval "
            "(result_id, result_type, title, payload, submitter) "
            "VALUES (?, ?, ?, ?::jsonb, ?) RETURNING id",
            (result_id, result_type, title, payload_json, submitter)).fetchone()
        return int(row["id"])


def submit_approval(approval_id: int, submitter: str) -> bool:
    """提交审批（draft → submitted）。"""
    with get_db() as conn:
        row = conn.execute(
            "UPDATE sa_compliance_approval SET status = 'submitted', "
            "submitter = ?, submitted_at = now(), updated_at = now() "
            "WHERE id = ? AND status = 'draft' RETURNING id",
            (submitter, approval_id)).fetchone()
        return row is not None


def review_approval(approval_id: int, reviewer: str, approved: bool,
                    note: str = None) -> bool:
    """复核审批（submitted → published / rejected）。"""
    new_status = "published" if approved else "rejected"
    with get_db() as conn:
        row = conn.execute(
            "UPDATE sa_compliance_approval SET status = ?, reviewer = ?, "
            "review_note = ?, reviewed_at = now(), updated_at = now() "
            "WHERE id = ? AND status = 'submitted' RETURNING id",
            (new_status, reviewer, note, approval_id)).fetchone()
        return row is not None


def list_approvals(status: str = None, limit: int = 50) -> list:
    """查询审批列表（新→旧），可按 status 过滤。"""
    sql = ("SELECT id, result_id, result_type, title, status, submitter, "
           "reviewer, review_note, "
           "to_char(submitted_at, 'YYYY-MM-DD HH24:MI:SS') AS submitted_at, "
           "to_char(reviewed_at, 'YYYY-MM-DD HH24:MI:SS') AS reviewed_at, "
           "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
           "FROM sa_compliance_approval WHERE 1=1")
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_approval(approval_id: int):
    """查询单条审批详情（含 payload）。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, result_id, result_type, title, payload::text AS payload, "
            "status, submitter, reviewer, review_note, "
            "to_char(submitted_at, 'YYYY-MM-DD HH24:MI:SS') AS submitted_at, "
            "to_char(reviewed_at, 'YYYY-MM-DD HH24:MI:SS') AS reviewed_at, "
            "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
            "FROM sa_compliance_approval WHERE id = ?", (approval_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("payload"):
        d["payload"] = json.loads(d["payload"])
    return d


def upsert_silence(user_id: str, symbol: str, position_date,
                   days_before: int = 1, days_after: int = 1) -> None:
    """登记/更新静默期配置。"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sa_compliance_silence "
            "(user_id, symbol, days_before, days_after, position_date) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, symbol, position_date) DO UPDATE SET "
            "days_before = EXCLUDED.days_before, days_after = EXCLUDED.days_after",
            (user_id, symbol, days_before, days_after, position_date))


def list_silences(user_id: str = None) -> list:
    """查询静默期配置列表。"""
    sql = ("SELECT id, user_id, symbol, days_before, days_after, position_date, "
           "enabled, to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
           "FROM sa_compliance_silence WHERE enabled = 1")
    params = []
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    sql += " ORDER BY id DESC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def check_silence_active(symbol: str, user_id: str, check_date=None) -> bool:
    """检查某标的在某日是否处于静默期（持仓日前 N 日到后 N 日）。"""
    from datetime import date as date_type
    if check_date is None:
        check_date = date_type.today()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, position_date, days_before, days_after "
            "FROM sa_compliance_silence "
            "WHERE user_id = ? AND symbol = ? AND enabled = 1",
            (user_id, symbol)).fetchone()
        if not row:
            return False
        pos_date = row["position_date"]
        if hasattr(pos_date, 'date'):
            pos_date = pos_date.date()
        from datetime import timedelta
        silent_start = pos_date - timedelta(days=row["days_before"])
        silent_end = pos_date + timedelta(days=row["days_after"])
        return silent_start <= check_date <= silent_end


def _json_default(obj):
    """json.dumps 兜底：Decimal/数值转 float（Event 负载从引擎来多为 float，防御兜底）。"""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    try:
        return float(obj)
    except (TypeError, ValueError):
        return str(obj)
