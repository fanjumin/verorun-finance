"""意图分类器 — 独立于 chatbot 插件，供 platform 和 plugin 共用"""
from i18n import _
import json
import logging

logger = logging.getLogger(__name__)

INTENT_CATEGORIES = ['purchase', 'aftersale', 'complaint', 'consult', 'technical', 'other']
SENTIMENT_LABELS = ['positive', 'neutral', 'negative', 'urgent']


def classify_intent(user_query):
    """轻量级 LLM 调用，将用户消息分类为意图+情绪。
    
    返回 (intent, sentiment)
    intent ∈ ['purchase','aftersale','complaint','consult','technical','other']
    sentiment ∈ ['positive','neutral','negative','urgent']
    """
    if not user_query or not user_query.strip():
        return 'other', 'neutral'
    try:
        from .engine import UnifiedLLM

        prompt = f"""分析以下用户消息，输出 JSON，不要多余文字：
{{
  "intent": "意图类别（purchase=购买意向, aftersale=售后, complaint=投诉, consult=咨询, technical=技术支持, other=其他）",
  "sentiment": "情绪（positive=正面, neutral=中性, negative=负面, urgent=紧急）"
}}

消息：{user_query[:500]}"""

        from .models import get_master_agent_config

        config = get_master_agent_config()
        engine = UnifiedLLM(config)
        # DEF-008：chat_stream 产出 ChatCompletionChunk 对象，直接拼接会得到
        # 对象 repr，导致下面的 json.loads 必然失败、意图恒为 other。
        from .llm_text import iter_stream_text
        reply = ''.join(
            t for t in iter_stream_text(engine.chat_stream([
                {'role': 'system', 'content': '你是一个精准的分类器。只输出 JSON。'},
                {'role': 'user', 'content': prompt}
            ], temperature=0.1, max_tokens=128))
            if not t.startswith('Error:'))

        data = json.loads(reply.strip())
        intent = data.get('intent', 'other')
        sentiment = data.get('sentiment', 'neutral')
        if intent not in INTENT_CATEGORIES:
            intent = 'other'
        if sentiment not in SENTIMENT_LABELS:
            sentiment = 'neutral'
        return intent, sentiment
    except Exception as e:
        logger.warning(f"[Intent] classify_intent failed, using defaults: {e}")
        return 'other', 'neutral'