# reflexion_feedback.py — 误信号回流 Reflexion（信号兑现 → 教训记忆）
#
# 接线点②：signal_quality.realize_signals() 回算出与方向相悖的误信号后，
# 插件发射合成 AGENT_TASK_COMPLETED（failed=True）事件，由 memory_engine 的
# ReflexionService 订阅处理：产出 {issue, lesson, action, rating} 落 reflexion_logs
# 并写入长期记忆，随后经 before_prompt_resolve 注入下次研判 prompt。
#
# 安全红线：
# - DAILY_CAP + 按偏离度降序截断，防 LLM 调用风暴（内核 Reflexion 单 worker 池）；
# - 发射侧全静默：任何异常（事件总线缺失/Reflexion 关闭/配置异常）都不影响
#   信号回算主链路。
# - 防递归：ReflexionService 内置 _reflecting 标志，本模块无需处理。
#
# 注：agent.task.completed 不在同步前缀（plugin./app./scheduler./health.）内，
# EventBus.emit 默认放入异步线程池执行，不会阻塞回算线程。

DAILY_CAP = 5        # 每日至多回流的误信号条数（防 LLM 调用风暴）
MIN_ADVERSE = 0.03   # 方向相悖且 5 日幅度超 3% 才值得复盘


def select_misjudged(realized: list) -> list:
    """从当日回算结果中筛出值得复盘的误信号。

    realized 元素：{symbol, trade_date, kind, signal, confidence, ret_5, ret_20}
    仅 buy/sell 参与判定；hold 不判定。返回按 |ret_5| 降序、最多 DAILY_CAP 条。
    """
    out = []
    for r in realized or []:
        sig, ret5 = r.get("signal"), r.get("ret_5")
        if ret5 is None or sig not in ("buy", "sell"):
            continue
        adverse = (sig == "buy" and ret5 < -MIN_ADVERSE) or \
                  (sig == "sell" and ret5 > MIN_ADVERSE)
        if adverse:
            out.append(r)
    out.sort(key=lambda r: abs(r.get("ret_5") or 0), reverse=True)
    return out[:DAILY_CAP]


def emit_misjudged_reflexions(misjudged: list) -> int:
    """向事件总线发射合成 AGENT_TASK_COMPLETED，触发 Reflexion。

    任何异常静默吞掉——绝不影响信号回算主链路。返回实际发射条数。
    """
    if not misjudged:
        return 0
    try:
        from plugin_manager.event_bus import get_event_bus, EventName
        bus = get_event_bus()
    except Exception:
        return 0

    sent = 0
    for sig in misjudged:
        try:
            symbol = sig.get("symbol") or ""
            sigval = sig.get("signal") or ""
            trade_date = str(sig.get("trade_date") or "")
            ret5 = sig.get("ret_5") or 0.0
            bus.emit(
                EventName.AGENT_TASK_COMPLETED,
                task={
                    "type": "stock.signal_review",
                    "instruction": "Review misjudged signal %s %s@%s" % (
                        symbol, sigval, trade_date),
                    "context": "Signal %s for %s on %s, 5-day realized return %.2f%%, "
                               "opposite to the signal direction." % (
                        sigval, symbol, trade_date, float(ret5) * 100),
                },
                result={
                    "failed": True,   # 满足 reflexion_failure_only 默认条件
                    "confidence": float(sig.get("confidence") or 0.0),
                    "retries": 0,
                    "error": "Misjudged signal %s: 5-day return %.2f%%, adverse to signal" % (
                        sigval, float(ret5) * 100),
                },
                agent_id="stock_analysis_agent",  # 与 register_agents 的 identifier 对齐
            )
            sent += 1
        except Exception:
            continue
    return sent
