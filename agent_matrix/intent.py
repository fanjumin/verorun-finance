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
  "intent": _("Category (purchase=Purchase Intent, aftersale=After-sales, complaint=Complaint, consult=Consultation, technical=Technical Support, other=Other)"),
  "sentiment": _("Emotion (positive=Positive, neutral=Neutral, negative=Negative, urgent=Urgent)")
}}

消息：{user_query[:500]}"""

        from .models import get_master_agent_config

        config = get_master_agent_config()
        engine = UnifiedLLM(config)
        reply = ''
        for token in engine.chat_stream([
            {'role': 'system', 'content': '你是一个精准的分类器。只输出 JSON。'},
            {'role': 'user', 'content': prompt}
        ], temperature=0.1, max_tokens=128):
            if not token.startswith('Error:'):
                reply += token

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