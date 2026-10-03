"""llm_cache.py — LLM 响应缓存（跨用户复用 + 交易时段 TTL）

为什么做在业务层而不是网关层
----------------------------
stock_skill._call_llm 与 deep_research._chat 均传 temperature=0.3，输出**非确定性**；
网关层响应缓存的硬闸条件（temperature=0 + seed）不满足，做在网关层命中率≈0。
因此缓存下沉到插件侧，键由业务语义构成。

为什么键用「量化指纹」而不是 prompt hash
--------------------------------------
_build_llm_prompt 把现价/涨幅/均线/RSI/评分全部拼进 prompt，直接对 prompt 取 hash 时
盘中每次价格跳动都会换键 → 缓存永不命中。故键只含量化后的**档位**：
    - 涨幅 0.5pct 档、技术评分 5 分档、RSI 3 点档、基本面评分 5 分档
    - 证据文本取「去数字指纹」（剥除数字后 hash）—— 数值微调不失效，结构变化才失效
行情大幅变动会自动换档 → 自动失效；小幅波动复用（结论本就不会变）。

TTL 为什么分时段
----------------
盘中行情在变 → 15 分钟；收盘后行情不再变 → 跨天复用到次日开盘前；
非交易日 → 12 小时。原日级判定（created_at::date = current_date）在盘中过于宽松、
在盘后过于严格（次日 9:30 明明还能用却失效）。

契约
----
全部函数 fail-open：任何异常都吞掉并告警，表现为「未命中」，绝不阻断主链路。
force=true 由调用方绕过本模块，不在此处理。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, time as dtime, timedelta

_log = logging.getLogger("stock_analysis.llm_cache")

_TTL_INTRADAY = 15 * 60        # 交易时段（9:30-15:00）
_TTL_PREOPEN = 30 * 60         # 盘前（< 9:30 的交易日）
_TTL_NON_TRADING = 12 * 3600   # 非交易日

# 剥数字（含百分号/千分位），用于证据结构指纹；与 evidence_bundle.verify_against_evidence
# 同款思路（那里剥日期，这里剥全部数值）。
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?%?")


# ────────────────────────────────────────────── 指纹与键


def _quantize(value, step: float) -> float:
    """连续值量化到档位；None / 非数值归 0.0（不参与区分）。"""
    try:
        return round(float(value) / step) * step
    except (TypeError, ValueError):
        return 0.0


def evidence_fingerprint(text: str) -> str:
    """证据结构指纹：剥除数字后取 hash。

    数值变化（价格/指标微调）不改变指纹，结构变化（科目增减、换数据源）才改变。
    注意：不能用 EvidenceBundle.hash —— 它对 to_json() 取 hash，包含 as_of 时间戳，
    每次调用结果都不同，等同永不命中。
    """
    return hashlib.sha256(_NUM_RE.sub("#", text or "").encode("utf-8")).hexdigest()[:16]


def build_key(scope: str, symbol: str, model_name: str,
              scene: str, fingerprint: str) -> str:
    """缓存键。model_name / scene 必须参与：换模型或换时段 prompt 内容不同。"""
    raw = "%s|%s|%s|%s|%s" % (scope or "", symbol or "", model_name or "",
                               scene or "", fingerprint or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────── TTL


def reuse_ttl_seconds(now: datetime = None) -> int:
    """复用有效期（秒），按交易时段分档。判定失败一律返回最保守的盘中值。"""
    now = now or datetime.now()
    try:
        try:
            from . import market_calendar as cal
        except ImportError:          # 顶层脚本运行兜底
            import market_calendar as cal
    except Exception as err:
        _log.warning("market_calendar unavailable, fallback intraday ttl: %s", err)
        return _TTL_INTRADAY

    try:
        if not cal.is_trading_day(now.date()):
            return _TTL_NON_TRADING
        t = now.time()
        if t < dtime(9, 30):
            return _TTL_PREOPEN
        if t <= dtime(15, 0):
            return _TTL_INTRADAY
        # 盘后：跨天复用到次日 9:25（开盘前 5 分钟）
        target = datetime.combine((now + timedelta(days=1)).date(), dtime(9, 25))
        return max(3600, int((target - now).total_seconds()))
    except Exception as err:
        _log.warning("reuse_ttl failed, fallback intraday: %s", err)
        return _TTL_INTRADAY


# ────────────────────────────────────────────── 读写


def get(scope: str, symbol: str, model_name: str,
        scene: str, fingerprint: str) -> dict | None:
    """命中返回 payload dict；未命中或任何异常返回 None（fail-open）。"""
    key = build_key(scope, symbol, model_name, scene, fingerprint)
    try:
        try:
            from . import models_sa as sa
        except ImportError:
            from plugins.stock_analysis import models_sa as sa
        sa.ensure_tables()
        with sa.get_db() as conn:
            row = conn.execute(
                "SELECT payload FROM sa_llm_cache "
                "WHERE cache_key = ? AND expires_at > now()", (key,)).fetchone()
            if not row:
                return None
            payload = row["payload"]
            # 命中计数（失败静默，不因埋点影响命中语义）
            try:
                conn.execute(
                    "UPDATE sa_llm_cache SET hit_count = hit_count + 1 "
                    "WHERE cache_key = ?", (key,))
            except Exception:
                pass
        return payload if isinstance(payload, dict) else None
    except Exception as err:
        _log.warning("llm cache get failed scope=%s symbol=%s: %s", scope, symbol, err)
        return None


def put(scope: str, symbol: str, model_name: str, scene: str,
        fingerprint: str, payload: dict, prompt_len: int = 0,
        now: datetime = None) -> None:
    """写入/覆盖缓存。任何异常静默（缓存写失败不影响主链路）。

    空 payload 直接跳过：上游返回空响应或降级文案时不得落缓存，否则污染后续结果。
    """
    if not payload:
        return
    key = build_key(scope, symbol, model_name, scene, fingerprint)
    ttl = reuse_ttl_seconds(now)
    try:
        try:
            from . import models_sa as sa
        except ImportError:
            from plugins.stock_analysis import models_sa as sa
        sa.ensure_tables()
        with sa.get_db() as conn:
            conn.execute(
                "INSERT INTO sa_llm_cache"
                " (cache_key, scope, symbol, model_name, scene, fingerprint,"
                "  prompt_chars, payload, expires_at)"
                # payload 必须 ?::jsonb 显式转型（同 models_sa.finish_job:773）——
                # psycopg2 传 str 会被当 text，插 jsonb 列需显式 cast。
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb,"
                "         now() + INTERVAL '1 second' * ?)"
                " ON CONFLICT (cache_key) DO UPDATE SET"
                "  payload = EXCLUDED.payload,"
                "  prompt_chars = EXCLUDED.prompt_chars,"
                "  expires_at = EXCLUDED.expires_at,"
                "  hit_count = 0,"
                "  created_at = now()",
                (key, scope or "", symbol or "", model_name or "", scene or "",
                 fingerprint or "", int(prompt_len or 0),
                 json.dumps(payload, ensure_ascii=False), int(ttl)))
    except Exception as err:
        _log.warning("llm cache put failed scope=%s symbol=%s: %s", scope, symbol, err)
