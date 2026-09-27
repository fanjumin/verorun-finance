#!/usr/bin/env python3
"""Token 级上下文预算层（Agent 内核升级 WP-A，对应任务 6.3）。

职责
----
所有进入 LLM 的消息列表先经本模块估算 token 总量；超预算时按
「不可分割单元」做滑动截断，保证 OpenAI 兼容协议的结构合法性。

设计铁律
--------
1. 零依赖硬要求：tiktoken 缺失（未安装，或离线取不到 BPE 词表）时回退启发式
   估算，绝不抛异常。
2. fail-open：本模块任何内部异常一律返回入参原样，绝不阻断对话。
3. 不反向依赖上层：只依赖标准库、可选的 tiktoken，以及运行时才懒导入的主库
   取库函数；不导入 orchestrator / agent_runner / engine。

核心正确性点
------------
OpenAI 兼容 API 对孤儿 tool 消息返回 400；反向同样不合法——保留了
assistant 的 tool_calls 却缺少对应 tool 响应也会报错。因此单元边界定义为：

    assistant 且带 tool_calls + 其后连续的全部 role='tool' 消息 = 1 个单元
    其余消息各自成 1 个单元

截断只允许整单元丢弃，绝不允许切在单元内部。

system_config 开关
------------------
    context_budget_tokens     默认 24000；0 表示关闭（原样直通）
    context_compress_enabled  默认 0；仅在为真且调用方注入 summarize 回调时，
                              被丢弃片段才走摘要。摘要回调需要模型调用，不在本
                              模块内实现，一期只预留接线位。
"""

import logging
import re

logger = logging.getLogger(__name__)

# ── 预算默认值（system_config 无对应 key 时生效）──
CONTEXT_DEFAULTS = {
    'context_budget_tokens': 24000,   # 单次请求上下文预算（估算 token）；0 = 关闭
    'context_compress_enabled': 0,    # 超预算时是否启用摘要（需调用方注入回调）
}

# CJK 及全角区段：按 1 token/字保守估算（真实约 0.6~0.7，宁高勿低）
_CJK_RE = re.compile(
    r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\uff00-\uffef]'
)

_ENCODER = None
_ENCODER_TRIED = False


def _get_encoder():
    """懒加载 tiktoken 编码器；不可用返回 None（不抛异常）。"""
    global _ENCODER, _ENCODER_TRIED
    if _ENCODER_TRIED:
        return _ENCODER
    _ENCODER_TRIED = True
    try:
        import tiktoken
        _ENCODER = tiktoken.get_encoding('cl100k_base')
    except Exception as e:      # 含离线取不到 BPE 词表的情况
        logger.info('[ContextBudget] tiktoken unavailable, fallback to heuristic: %s', e)
        _ENCODER = None
    return _ENCODER


def reset_encoder_cache():
    """重置编码器探测缓存（仅供单测注入桩模块使用）。"""
    global _ENCODER, _ENCODER_TRIED
    _ENCODER = None
    _ENCODER_TRIED = False


def _count(text) -> int:
    """估算单段文本的 token 数；空值返回 0。"""
    if not text:
        return 0
    text = str(text)
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    cjk = len(_CJK_RE.findall(text))
    return cjk + int((len(text) - cjk) * 0.25) + 1


def estimate_tokens(text_or_messages) -> int:
    """估算 token 数。

    传入消息列表时，逐条计 4（角色/分隔等结构开销）再加 content 与
    tool_calls 的 name / arguments。任何异常返回 0。
    """
    try:
        if isinstance(text_or_messages, (list, tuple)):
            total = 0
            for m in text_or_messages:
                if not isinstance(m, dict):
                    continue
                total += 4
                total += _count(m.get('content'))
                for tc in (m.get('tool_calls') or []):
                    fn = tc.get('function') if isinstance(tc, dict) else None
                    if isinstance(fn, dict):
                        total += _count(fn.get('name'))
                        total += _count(fn.get('arguments'))
            return total
        return _count(text_or_messages)
    except Exception:
        return 0


def _drop_orphan_tools(messages):
    """防御性清洗入参中已存在的孤儿 tool 消息。

    上游 history 若已带入孤儿（历史缺陷或外部调用方拼错），任何截断策略都救不了
    协议报错，必须在此拦掉。判定依据：tool_call_id 能否在**其之前**的
    assistant.tool_calls 中找到。
    """
    live_ids, out, dropped = set(), [], 0
    for m in messages:
        role = m.get('role') if isinstance(m, dict) else None
        if role == 'assistant':
            for tc in (m.get('tool_calls') or []):
                tid = tc.get('id') if isinstance(tc, dict) else None
                if tid:
                    live_ids.add(tid)
            out.append(m)
        elif role == 'tool':
            if m.get('tool_call_id') in live_ids:
                out.append(m)
            else:
                dropped += 1
        else:
            out.append(m)
    if dropped:
        logger.warning('[ContextBudget] dropped %d orphan tool message(s)', dropped)
    return out


def _group_units(messages):
    """切成不可分割单元：assistant 且带 tool_calls + 其后连续的全部 tool 消息。

    非 dict 条目各自成单元，避免后续访问属性时抛异常。
    """
    units, i, n = [], 0, len(messages)
    while i < n:
        m = messages[i]
        if isinstance(m, dict) and m.get('role') == 'assistant' and m.get('tool_calls'):
            j = i + 1
            while j < n and isinstance(messages[j], dict) and messages[j].get('role') == 'tool':
                j += 1
            units.append(list(messages[i:j]))
            i = j
        else:
            units.append([m])
            i += 1
    return units


def fit_messages(messages, budget, *, keep_system=True, keep_recent=4, summarize=None):
    """在预算内裁剪消息列表。

    Args:
        messages: 原始消息列表（OpenAI 兼容格式）
        budget: token 预算；<= 0 表示关闭，原样直通
        keep_system: 是否恒保留开头的 system 消息（不参与裁剪）
        keep_recent: 至少保留的尾部**单元**数（预算再紧也不裁穿）
        summarize: 可选回调，接收被丢弃的消息列表并返回摘要字符串

    Returns:
        裁剪后的消息列表；未超预算 / 关闭 / 任意异常时返回**入参原对象**。
    """
    try:
        if not isinstance(messages, list) or not messages:
            return messages
        try:
            budget = int(budget)
        except (TypeError, ValueError):
            return messages
        if budget <= 0:
            logger.info('[ContextBudget] disabled (budget=%s), passthrough', budget)
            return messages
        if estimate_tokens(messages) <= budget:
            logger.info('[ContextBudget] passthrough (estimated<=%d)', budget)
            return messages

        msgs = _drop_orphan_tools(messages)
        units = _group_units(msgs)

        head, body = [], []
        for u in units:
            first = u[0] if u else None
            if (keep_system and not body and isinstance(first, dict)
                    and first.get('role') == 'system'):
                head.append(u)
            else:
                body.append(u)

        floor = max(1, int(keep_recent))
        dropped, dropped_n, summary_text = [], 0, ''

        def _assemble():
            """按当前 head / body / 丢弃计数组装结果（含摘要与省略提示）。"""
            out = [m for u in head for m in u]
            if dropped_n:
                if summary_text:
                    out.append({'role': 'system',
                                'content': '【较早对话摘要】' + str(summary_text)})
                out.append({'role': 'system',
                            'content': '【已省略较早的 %d 条消息以控制上下文长度】' % dropped_n})
            out.extend([m for u in body for m in u])
            return out

        # 第一轮：按单元从前向后丢弃，直到落入预算或触达 keep_recent 保底
        while len(body) > floor:
            if estimate_tokens(_assemble()) <= budget:
                break
            victim = body.pop(0)
            dropped.extend(victim)
            dropped_n += len(victim)

        # 摘要回调：仅在确有丢弃时调用一次；异常不影响主流程
        if dropped_n and summarize is not None:
            try:
                summary_text = summarize(dropped) or ''
            except Exception as e:
                logger.warning('[ContextBudget] summarize failed: %s', e)
                summary_text = ''

        # 第二轮：摘要与省略提示本身有开销，若因此再度超预算则继续丢弃
        while estimate_tokens(_assemble()) > budget and len(body) > floor:
            victim = body.pop(0)
            dropped_n += len(victim)

        out = _assemble()
        logger.info('[ContextBudget] trimmed %d msg(s) -> estimated %d (budget=%d)',
                    dropped_n, estimate_tokens(out), budget)
        return out
    except Exception as e:
        logger.warning('[ContextBudget] fit failed, passthrough: %s', e)
        return messages


def get_context_config() -> dict:
    """读取 system_config 中的上下文预算配置，缺失或异常回退默认值。

    读取与降级范式对齐 agent_matrix/engine.py 的 _get_ai_budget_config。
    """
    cfg = dict(CONTEXT_DEFAULTS)
    try:
        from models import get_db as _main_get_db
        with _main_get_db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM system_config WHERE key IN "
                "('context_budget_tokens','context_compress_enabled')"
            ).fetchall()
        for r in (rows or []):
            key = r['key'] if not isinstance(r, tuple) else r[0]
            val = r['value'] if not isinstance(r, tuple) else r[1]
            if key not in cfg or val is None or str(val).strip() == '':
                continue
            try:
                cfg[key] = int(val)
            except (ValueError, TypeError):
                logger.warning('[ContextBudget] bad value for %s=%r, keep default', key, val)
    except Exception as e:
        logger.warning('[ContextBudget] read config failed, use defaults: %s', e)
    return cfg


def get_context_budget() -> int:
    """返回单次请求的 token 预算；0 表示关闭。"""
    try:
        return int(get_context_config().get('context_budget_tokens') or 0)
    except Exception:
        return int(CONTEXT_DEFAULTS['context_budget_tokens'])


def _fit(messages, keep_recent=4, summarize=None):
    """engine 层门面：读 system_config 预算后裁剪。

    命名沿用已批准方案（v1.1 §3.4 A-1）中的 `_fit`，虽以下划线开头，但它是
    供 engine 与 agent_runner 复用的模块门面。摘要回调仅在
    context_compress_enabled 打开且调用方显式注入时透传。
    """
    cfg = get_context_config()
    budget = int(cfg.get('context_budget_tokens') or 0)
    if not int(cfg.get('context_compress_enabled') or 0):
        summarize = None
    return fit_messages(messages, budget, keep_recent=keep_recent, summarize=summarize)
