"""AI Advisor 对话统计与报表"""
from i18n import _
import json
from datetime import datetime

from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('chatbot')


def _get_db():
    """获取插件独立数据库连接"""
    from .models import get_chatbot_db
    return get_chatbot_db()


def _get_main_db():
    """获取主库连接（仅用于查询 user_tickets）"""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center'))
    from models import get_db
    return get_db()


INTENT_CATEGORIES = ['purchase', 'aftersale', 'complaint', 'consult', 'technical', 'other']
SENTIMENT_LABELS = ['positive', 'neutral', 'negative', 'urgent']


# classify_intent 已移至 agent_matrix.intent（消除 platform→plugin 反向依赖）
# 保留此别名以兼容现有调用方
def classify_intent(user_query):
    import sys as _s, os as _o
    _s.path.insert(0, _o.path.join(_o.path.dirname(__file__), '..', '..'))
    from agent_matrix.intent import classify_intent as _ci
    return _ci(user_query)


def log_session(session_id, user_query='', ai_reply='', escalated=False,
                source='chatbot', intent='', sentiment=''):
    """记录一次 AI 对话回合到独立库 chatbot_sessions"""
    try:
        with _get_db() as conn:
            conn.execute(
                """INSERT INTO chatbot_sessions
                   (session_id, user_query, ai_reply, escalated, source,
                    intent, sentiment, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())""",
                (session_id, user_query, ai_reply, 1 if escalated else 0, source,
                 intent, sentiment)
            )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"[Chatbot Stats] log_session failed: {e}")
        return False


def get_today_stats():
    """获取今日统计概览，含意图分布"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        with _get_db() as conn:
            # 今日对话数（唯一 session）
            total = conn.execute(
                "SELECT COUNT(DISTINCT session_id) as cnt FROM chatbot_sessions "
                "WHERE source='chatbot' AND date(created_at)=%s",
                (today,)
            ).fetchone()['cnt'] or 0

            # 今日转人工数
            escalated = conn.execute(
                "SELECT COUNT(DISTINCT session_id) as cnt FROM chatbot_sessions "
                "WHERE source='chatbot' AND escalated=1 AND date(created_at)=%s",
                (today,)
            ).fetchone()['cnt'] or 0

            # 今日 CSAT 平均分
            avg_csat = conn.execute(
                "SELECT COALESCE(AVG(csat_score),0) as avg FROM chatbot_sessions "
                "WHERE source='chatbot' AND csat_score>0 AND date(created_at)=%s",
                (today,)
            ).fetchone()['avg'] or 0

            # 意图分布
            intent_raw = conn.execute(
                "SELECT intent, COUNT(*) as cnt FROM chatbot_sessions "
                "WHERE source='chatbot' AND date(created_at)=%s AND intent!='' "
                "GROUP BY intent ORDER BY cnt DESC",
                (today,)
            ).fetchall()
            intent_dist = {r['intent']: r['cnt'] for r in intent_raw}

            # 情绪分布
            sentiment_raw = conn.execute(
                "SELECT sentiment, COUNT(*) as cnt FROM chatbot_sessions "
                "WHERE source='chatbot' AND date(created_at)=%s AND sentiment!='' "
                "GROUP BY sentiment ORDER BY cnt DESC",
                (today,)
            ).fetchall()
            sentiment_dist = {r['sentiment']: r['cnt'] for r in sentiment_raw}

            # 本周趋势
            trend_raw = conn.execute(
                "SELECT date(created_at) as d, COUNT(DISTINCT session_id) as cnt "
                "FROM chatbot_sessions WHERE source='chatbot' "
                "AND created_at >= NOW() - INTERVAL '7 days' "
                "GROUP BY d ORDER BY d"
            ).fetchall()

        # 工单数据仍从主库读取（user_tickets 在主库）
        tickets = 0
        resolved = 0
        try:
            with _get_main_db() as mc:
                tickets = mc.execute(
                    "SELECT COUNT(*) as cnt FROM user_tickets "
                    "WHERE category='chatbot_escalation' AND date(created_at)=date('now')"
                ).fetchone()['cnt'] or 0
                resolved = mc.execute(
                    "SELECT COUNT(*) as cnt FROM user_tickets "
                    "WHERE category='chatbot_escalation' AND status='closed' "
                    "AND date(created_at)=date('now')"
                ).fetchone()['cnt'] or 0
        except Exception as e:
            logger.warning(f"[Chatbot Stats] 读主库工单失败: {e}")

        handoff_rate = round(escalated / total * 100, 1) if total > 0 else 0
        resolve_rate = round(resolved / tickets * 100, 1) if tickets > 0 else 0
        trend = [{'date': r['d'], 'count': r['cnt']} for r in trend_raw]

        return {
            'today_sessions': total,
            'today_escalated': escalated,
            'handoff_rate': handoff_rate,
            'today_tickets': tickets,
            'resolve_rate': resolve_rate,
            'avg_csat': round(avg_csat, 1),
            'intent_distribution': intent_dist,
            'sentiment_distribution': sentiment_dist,
            'trend': trend,
        }
    except Exception as e:
        logger.error(f"[Chatbot Stats] get_today_stats failed: {e}")
        return {'today_sessions': 0, 'today_escalated': 0, 'handoff_rate': 0,
                'today_tickets': 0, 'resolve_rate': 0, 'avg_csat': 0,
                'intent_distribution': {}, 'sentiment_distribution': {},
                'trend': []}


def record_csat(session_id, score):
    """记录 CSAT 评分到独立库"""
    try:
        with _get_db() as conn:
            conn.execute(
                "UPDATE chatbot_sessions SET csat_score=%s WHERE session_id=%s",
                (score, session_id)
            )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"[Chatbot Stats] record_csat failed: {e}")
        return False


def get_hot_topics(limit=10):
    """热门问题分析"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT user_query, COUNT(*) as cnt FROM chatbot_sessions "
                "WHERE source='chatbot' AND date(created_at)=%s "
                "AND user_query!='' "
                "GROUP BY user_query ORDER BY cnt DESC LIMIT %s",
                (today, limit)
            ).fetchall()
        return [{'query': r['user_query'], 'count': r['cnt']} for r in rows]
    except Exception as e:
        logger.error(f"[Chatbot Stats] get_hot_topics failed: {e}")
        return []


def get_agent_performance():
    """座席绩效：从主库 user_tickets 查询"""
    try:
        with _get_main_db() as conn:
            rows = conn.execute(
                """SELECT
                    assigned_name,
                    assigned_to,
                    COUNT(*) as total_tickets,
                    SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) as resolved,
                    ROUND(AVG(
                        CASE WHEN replied_at IS NOT NULL AND replied_at!=''
                        THEN EXTRACT(EPOCH FROM (replied_at::timestamp - created_at::timestamp))
                        ELSE NULL END
                    ), 0) as avg_response_sec
                  FROM user_tickets
                  WHERE assigned_to > 0
                  GROUP BY assigned_to
                  ORDER BY total_tickets DESC"""
            ).fetchall()
        result = []
        for r in rows:
            resolve_rate = round(r['resolved'] / r['total_tickets'] * 100, 1) if r['total_tickets'] > 0 else 0
            avg_resp = f"{round(r['avg_response_sec'] / 60, 1)}min" if r['avg_response_sec'] else '--'
            result.append({
                'agent_name': r['assigned_name'] or f"Agent #{r['assigned_to']}",
                'agent_id': r['assigned_to'],
                'total_tickets': r['total_tickets'],
                'resolved': r['resolved'],
                'resolve_rate': resolve_rate,
                'avg_response': avg_resp,
            })
        return result
    except Exception as e:
        logger.error(f"[Chatbot Stats] get_agent_performance failed: {e}")
        return []


def qa_check_conversation(session_id, user_query, ai_reply):
    """对话质检：用 LLM 分析一轮对话的质量"""
    if not user_query or not ai_reply:
        return None
    try:
        import sys as _sys, os as _os, json as _json
        _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..'))
        from agent_matrix.engine import UnifiedLLM

        prompt = f"""分析以下 AI 客服对话，从以下维度打分（1-5），输出 JSON：

{{
  "score": _("Overall Rating 1-5"),
  "accuracy": _("Accuracy 1-5"),
  "helpfulness": _("Helpfulness (1-5)"),
  "politeness": _("Politeness Level 1-5"),
  "suggestion": _("Improvement suggestion (in one sentence)")
}}

用户：{user_query[:300]}
AI：{ai_reply[:500]}"""

        engine = UnifiedLLM({'provider': 'dashscope', 'model_name': 'qwen-turbo'})
        reply = ''
        for token in engine.chat_stream([
            {'role': 'system', 'content': _('You are a conversation quality reviewer. Output JSON only.')},
            {'role': 'user', 'content': prompt}
        ], temperature=0.1, max_tokens=256):
            if not token.startswith('Error:'):
                reply += token

        data = _json.loads(reply.strip())
        return {
            'score': min(5, max(1, int(data.get('score', 3)))),
            'accuracy': min(5, max(1, int(data.get('accuracy', 3)))),
            'helpfulness': min(5, max(1, int(data.get('helpfulness', 3)))),
            'politeness': min(5, max(1, int(data.get('politeness', 3)))),
            'suggestion': str(data.get('suggestion', ''))[:200],
        }
    except Exception as e:
        logger.warning(f"[Chatbot] QA check failed: {e}")
        return None