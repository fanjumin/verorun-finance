# deep_research.py — 深度研判 DAG 节点处理器（B6）
#
# 平台契约（grep 核实）：
# - register_dag_nodes() 返回 {节点类型: 处理器}（plugins/veroscholar/workflow.py:333-339 同款形状）
# - 处理器必须严格 handler(node_def, input_data) 两参（plugin_manager/manager.py:82 校验，不符即被跳过）
# - 上游输出位于 input_data["node_<上游id>_output"]（orchestrator/workflow_engine.py:328-330）
# - LLM 走 UnifiedLLM.chat()（有实例配置回落；chat_stream 没有，禁用）
PROMPT_TEMPLATE = """你是资深 A 股研究员。基于下列证据材料对 {symbol} 做深度研判。

{evidence}

要求：
1. 结论必须引用证据（如"财报证据#1"），不得虚构数据；
2. 输出 300-600 字中文研判；
3. 最后输出严格 JSON：{{"signal":"buy|sell|hold","confidence":0到1,"reasons":["..."],"summary":"...","evidence_refs":["..."]}}"""

# 测试注入缝（生产保持 None）：LLM_FACTORY(messages) -> str；EVIDENCE_FETCHER(symbol) -> str
LLM_FACTORY = None
EVIDENCE_FETCHER = None


def _default_evidence(symbol: str) -> str:
    # ★ 相对导入优先（插件以 plugins.<id> 命名空间包加载，插件目录**不在** sys.path 上），
    #   直接以脚本运行时无包上下文，回退绝对导入 —— 与 stock_skill.py:29 同款写法。
    #   实测（scripts/research-selftest.py）：只写绝对导入时真实路径抛
    #   "No module named 'gateway'"，被 handle_stock_deep_research 的 try 吞成 success=False，
    #   表现为"研报永远失败且看不出原因"。
    try:
        from .gateway import gateway
        from .evidence import build_evidence_context
    except ImportError:
        from gateway import gateway
        from evidence import build_evidence_context
    try:
        try:
            from .valuation import pe_pb_percentile, valuation_line
        except ImportError:
            from valuation import pe_pb_percentile, valuation_line
        val_text = valuation_line(pe_pb_percentile(symbol)) or ""
    except Exception:
        val_text = ""
    return build_evidence_context(gateway.get_fundamental, gateway.get_moneyflow,
                                  gateway.get_news, valuation_text=val_text, symbol=symbol)


def _chat(prompt: str) -> str:
    if LLM_FACTORY is not None:                     # 测试注入缝
        return LLM_FACTORY([{"role": "user", "content": prompt}])
    from agent_matrix.engine import UnifiedLLM
    from agent_matrix.model_resolver import resolve_model_args
    cfg = resolve_model_args({"strategy": "tier", "tier": "standard"})
    if not cfg.get("model_name") and cfg.get("model"):     # H-1 同款映射
        cfg["model_name"] = cfg["model"]
    return UnifiedLLM(cfg).chat([{"role": "user", "content": prompt}],
                                temperature=0.3, max_tokens=2048,
                                module="stock_analysis")


def _collect_evidence(symbol: str, input_data: dict) -> str:
    # 优先复用上游节点已产出的证据（避免重复取数）
    for key, val in (input_data or {}).items():
        if key.endswith("_output") and isinstance(val, dict) and val.get("evidence_text"):
            return str(val["evidence_text"])
    fetcher = EVIDENCE_FETCHER or _default_evidence
    return fetcher(symbol)


def handle_stock_deep_research(node_def, input_data):
    """DAG 节点：证据收集 → LLM 深度研判 → 结构化信号。"""
    cfg = (node_def or {}).get("config") or {}
    symbol = cfg.get("symbol") or (input_data or {}).get("symbol")
    if not symbol:
        return {"success": False, "error": "symbol required"}
    try:
        evidence_text = _collect_evidence(symbol, input_data or {})
        report = _chat(PROMPT_TEMPLATE.format(symbol=symbol, evidence=evidence_text))
        signal = None
        try:
            try:
                from .evidence import parse_structured_output
            except ImportError:      # 顶层脚本运行兜底
                from evidence import parse_structured_output
            signal = parse_structured_output(report)
        except Exception:
            signal = None
        return {
            "success": True,
            "symbol": symbol,
            "report": report,
            "signal": signal or {"signal": "hold", "confidence": 0.5,
                                 "reasons": ["结构化解析失败，保守观望"]},
            "evidence_chars": len(evidence_text),
        }
    except Exception as err:
        return {"success": False, "symbol": symbol, "error": str(err)}


def get_dag_nodes() -> dict:
    return {"stock_deep_research": handle_stock_deep_research}
