# kb_publish.py — 分析结论沉淀平台知识库（B2）
#
# 范式来源（grep 核实的两处真实先例）：
# - plugins/memory_engine/services/sedimentation.py:254-271 _write_kb（agent_matrix.models.get_db + commit）
# - plugin_manager/routes.py:806-813 USAGE 同步（scope='system', owner NULL + store_embedding）
# 铁律：写库/向量化任何失败都只留痕、绝不阻断分析主链路。
import logging
import time

_log = logging.getLogger("stock_analysis.kb_publish")

KB_ID_PREFIX = "kb_stock"


def publish_analysis_kb(symbol: str, kind: str, result_json: dict,
                        priority: int = 5, quality_score: float = 0.6):
    """把一次分析结论写入 public.knowledge_blocks 并尽力向量化。

    返回 kb_id；任何失败返回 None（调用方不受影响）。
    幂等锚点：id = kb_stock_<symbol>_<日期>_<kind>，ON CONFLICT (id) DO NOTHING，
    同日同标的同类型只沉淀一条（与批量/任务重跑天然兼容）。
    """
    sig = result_json.get("signal") or {}
    signal = str(sig.get("signal") or "hold")
    confidence = sig.get("confidence")
    title = "%s %s研判: %s" % (symbol, kind, signal.upper())
    reasons = sig.get("reasons") or []
    report = str(result_json.get("report") or "")
    content = "\n".join([
        "标的: %s | 类型: %s | 信号: %s | 置信度: %s" % (symbol, kind, signal, confidence),
        "关键依据: " + "; ".join(str(r) for r in reasons[:5]),
        "",
        report[:1500],
    ])
    keywords = "stock,%s,%s,%s" % (symbol, kind, signal)
    kb_id = "%s_%s_%s_%s" % (KB_ID_PREFIX, symbol, time.strftime("%Y%m%d"), kind)
    try:
        from agent_matrix.models import get_db as _get_main_db
        with _get_main_db() as mdb:
            mdb.execute(
                "INSERT INTO public.knowledge_blocks"
                " (id, title, content, keywords, category, priority, source, quality_score,"
                "  scope, owner_id, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NOW())"
                " ON CONFLICT (id) DO NOTHING",
                (kb_id, title, content, keywords, "plugin", priority,
                 "plugin", float(quality_score), "system"))
            mdb.commit()
    except Exception as err:
        _log.warning("kb publish insert failed %s: %s", symbol, err)
        return None
    try:
        from agent_matrix.rag_retriever import store_embedding
        store_embedding(kb_id, title, content)
    except Exception as err:
        _log.warning("kb embedding skipped %s: %s", kb_id, err)
    return kb_id
