#!/usr/bin/env python3
"""Social Push Plugin — 错误处理与重试机制（P5）

错误分级：
  可重试（retryable=True）：5xx / 429 限流 / token 过期（先刷新）
  不可重试（retryable=False）：4xx 参数错 / 内容违规 / 权限不足

重试策略：指数退避（10s / 30s / 2min），上限 3 次。
实现用纯标准库（time.sleep），不引入 tenacity 等新依赖；
业务侧按需调用 with_retry() 包装发布动作即可。
"""
import logging
import random
import time

logger = logging.getLogger(__name__)

# 重试退避基数（秒）
BACKOFF_BASE = [10, 30, 120]

# 平台 API 常见错误码 → 是否可重试（各渠道适配器可合并补充）
# key 约定：status_code 或 'token_expired' / 'rate_limited'
RETRYABLE_ERRORS = {
    'token_expired': True,
    'rate_limited': True,
}

# HTTP 状态码分级
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_NON_RETRYABLE_STATUS = {400, 401, 403, 404, 422}


def is_retryable(error: str = '', status_code: int = 0) -> bool:
    """判断错误是否可重试。"""
    code = int(status_code or 0)
    if code in _RETRYABLE_STATUS:
        return True
    if code in _NON_RETRYABLE_STATUS:
        return False
    low = (error or '').lower()
    if not error and code:
        return False
    if 'expired' in low or 'rate' in low or 'temporarily' in low or 'timeout' in low:
        return True
    if 'invalid' in low or 'permission' in low or 'forbidden' in low or 'blocked' in low:
        return False
    # 未知错误默认不可重试，避免无限重试放大故障
    return False


def with_retry(func, *args, max_retries: int = 3, **kwargs):
    """指数退避重试包装。

    func 返回标准结果 dict（含 status/success/error）。
    可重试且未达上限时退避重试；否则返回最终结果。
    """
    last_result = None
    for attempt in range(max_retries + 1):
        try:
            result = func(*args, **kwargs)
        except Exception as e:
            logger.exception('[SocialPush] retry attempt %d raised: %s', attempt + 1, e)
            result = {'status': 'failed', 'success': False, 'error': str(e)}

        last_result = result
        success = result.get('status') in ('published', 'draft', 'publishing') or \
            bool(result.get('success'))
        if success:
            return result

        err = result.get('error', '')
        status_code = result.get('status_code', 0) or 0
        if attempt >= max_retries or not is_retryable(err, status_code):
            return result

        delay = BACKOFF_BASE[attempt] if attempt < len(BACKOFF_BASE) else BACKOFF_BASE[-1]
        delay = delay + random.uniform(0, 2)  # 抖动，避免惊群
        logger.info('[SocialPush] retrying in %.0fs (attempt %d/%d): %s',
                    delay, attempt + 1, max_retries, err)
        time.sleep(delay)

    return last_result or {'status': 'failed', 'success': False, 'error': 'retry exhausted'}
