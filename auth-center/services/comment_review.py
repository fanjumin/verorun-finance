#!/usr/bin/env python3
"""AI-powered comment review: local word filter + Qwen semantic analysis."""
import logging, json
from services.sensitive_words import check_sensitive, SENSITIVE_WORDS
from services.ai_content_generator import _qwen_chat

logger = logging.getLogger(__name__)

THRESHOLD_AUTO_APPROVE = 0.3
THRESHOLD_MANUAL_REVIEW = 0.7

def review_comment(nickname, content):
    """Review a comment. Returns (status, reason, score).
    status: 'approved' | 'pending' | 'rejected'
    """
    # Step 1: Local sensitive word check
    has_match, category, word = check_sensitive(content)
    if has_match:
        logger.info(f"Comment rejected by word filter: {category} - {word}")
        return 'rejected', f'Contains prohibited content ({category})', 1.0

    # Step 2: Empty/too short check
    if len(content.strip()) < 2:
        return 'rejected', 'Content too short', 0.8

    # Step 3: AI semantic review via Qwen
    try:
        prompt = f"""You are a content moderation AI. Review the following comment and return a JSON response.

Rules:
- Return a JSON object with: {{"score": 0.0-1.0, "reason": "brief explanation in Chinese", "category": "normal/spam/ad/abuse/politics/other"}}
- score 0.0-0.3: normal, safe to publish
- score 0.3-0.7: suspicious, needs manual review
- score 0.7-1.0: must be rejected
- Consider: spam, advertising, harassment, hate speech, illegal content, financial scams

Comment from user "{nickname}":
{content}

JSON response:"""
        result = _qwen_chat([{'role': 'user', 'content': prompt}], temperature=0.1)
        # Parse JSON from response
        result = result.strip()
        if '```' in result:
            result = result.split('```')[1].split('```')[0]
        if result.startswith('json'):
            result = result[4:].strip()
        review = json.loads(result)
        score = float(review.get('score', 0.5))
        reason = review.get('reason', 'AI review')
        
        if score <= THRESHOLD_AUTO_APPROVE:
            return 'approved', reason, score
        elif score <= THRESHOLD_MANUAL_REVIEW:
            return 'pending', reason, score
        else:
            return 'rejected', reason, score
    except Exception as e:
        logger.error(f"AI review failed: {e}")
        return 'pending', f'AI review error: {str(e)[:50]}', 0.5

def batch_review(comments):
    """Review multiple comments. Returns list of (id, status, reason, score)."""
    results = []
    for c in comments:
        status, reason, score = review_comment(c.get('nickname', ''), c.get('content', ''))
        results.append((c['id'], status, reason, score))
    return results
