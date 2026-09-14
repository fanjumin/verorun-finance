#!/usr/bin/env python3
"""
Stock Analysis Skill — 标准 Hermes/OpenClaw Skill 接口
自包含股票分析引擎，支持技术面/基本面/情绪面/LLM 多维度分析
"""

import os
import sys
import json
import argparse
import math
import re
import time
from datetime import datetime
from typing import Optional, Union

import pandas as pd
import requests

try:
    from plugin_manager.logger import get_plugin_logger
    _LOGGER = get_plugin_logger("stock_analysis")
except ImportError:
    import logging
    _LOGGER = logging.getLogger("stock_analysis")

# 数据网关：阶段1 起行情/新闻全部经 gateway 统一取数（类别路由 + 源前缀缓存）。
# 相对导入适配插件命名空间包加载；直接以脚本运行时无包上下文，回退绝对导入。
try:
    from .gateway import gateway
    from .providers.base import DataCategory
    from . import indicators as _ind
except ImportError:
    from gateway import gateway
    from providers.base import DataCategory
    import indicators as _ind

# ============================================================
# 配置
# ============================================================

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SKILL_DIR, "config.yaml")
DEFAULT_CONFIG = {
    "DATA_PROVIDER": "sina",
    "DATA_CACHE_DIR": "./data/cache",
}

# SA-N3：UnifiedLLM 返回空响应时的重试上限（含首次共 N 次尝试），逐次留痕日志
_LLM_MAX_ATTEMPTS = 2
_LLM_RETRY_BACKOFF = 1.0    # 秒

# ============================================================
# 分析结果
# ============================================================

DISCLAIMER = ("本报告由 VeroRun 股票分析插件自动生成，数据来源与授权状态见 data.data_sources 段；"
              "内容仅为数据分析参考，不构成任何投资建议。")


def scenario_task_type(now=None) -> str:
    """按当前时段返回场景 task_type（接线点⑤，对齐 PromptResolver 第三层 task_triggers 精确匹配）。

    返回 stock.preopen（9:30 前）/ stock.intraday（交易时段 9:30-15:00）/
    stock.postclose（15:00 后）。非交易日一律按 postclose（复盘语境）。
    """
    from datetime import datetime, time as dtime
    try:
        from . import market_calendar as cal
    except ImportError:          # 顶层脚本运行兜底
        import market_calendar as cal
    now = now or datetime.now()
    if not cal.is_trading_day(now.date()):
        return "stock.postclose"
    t = now.time()
    if t < dtime(9, 30):
        return "stock.preopen"
    if t <= dtime(15, 0):
        return "stock.intraday"
    return "stock.postclose"


def _scene_prompt(task_type: str) -> str:
    """从 agent_prompts 读取命中 task_type 的 active 场景模板。

    与内核 PromptResolver._match_scene 同语义：prompt_type='scene' + is_active +
    task_triggers（JSON 数组）含该 task_type，按 priority DESC, version DESC 取首条。
    任一环节失败返回 ''，调用方自动回退既有 prompt（场景缺失零事故）。
    """
    if not task_type:
        return ""
    try:
        import json
        from agent_matrix.models import get_db as _get_main_db
        with _get_main_db() as mdb:
            rows = mdb.execute(
                "SELECT content, task_triggers AS triggers FROM agent_prompts "
                "WHERE prompt_type = 'scene' AND is_active = TRUE "
                "ORDER BY priority DESC, version DESC").fetchall()
        for r in rows:
            raw = r["triggers"] or "[]"
            try:
                tlist = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except (TypeError, ValueError):
                tlist = []
            if task_type in tlist and r["content"]:
                return str(r["content"])
    except Exception:
        pass
    return ""


class AnalysisResult:
    """一次股票分析的完整结果"""

    def __init__(self, symbol: str, text_report: str = "",
                 json_data: dict = None, signal: dict = None,
                 error: str = None, data_sources: list = None):
        self.symbol = symbol
        self.text_report = text_report
        self.json_data = json_data or {}
        self.signal = signal or {"signal": "hold", "confidence": 0.0, "reasons": []}
        self.error = error
        self.data_sources = data_sources or []
        self.timestamp = datetime.now().isoformat()

    def to_text(self) -> str:
        if self.error:
            return f"[错误] {self.symbol}: {self.error}"
        lines = [
            f"=== {self.symbol} 分析报告 ===",
            f"时间: {self.timestamp}",
            f"信号: {self.signal['signal'].upper()}  (强度: {self.signal['confidence']:.0%})",
            "",
            self.text_report,
        ]
        if self.signal.get("reasons"):
            lines.append("")
            lines.append("理由:")
            for r in self.signal["reasons"]:
                lines.append(f"  \u2022 {r}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        data = dict(self.json_data)
        data["data_sources"] = self.data_sources
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "signal": self.signal,
            "report": self.text_report,
            "disclaimer": DISCLAIMER,
            "error": self.error,
            "data": data,
        }

    def to_signal(self) -> dict:
        return self.signal


def _num(v, nd: int = 3):
    """安全数值格式化：NaN/Inf/空/非法一律落 None，避免 round()/JSON 序列化崩掉。"""
    try:
        if v is None or math.isnan(float(v)) or math.isinf(float(v)):
            return None
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _technical_snapshot(frame: pd.DataFrame) -> dict:
    # 信号空间：后复权收盘价（规避除权/分红造成的均线与趋势假信号）；
    # 各 provider 已写入 price_basis 列（hfq/raw）留痕，穿透落盘缓存往返。
    if "price_basis" in frame:
        basis = str(frame["price_basis"].iloc[-1])
    else:
        basis = "raw"
    close = frame["close_hfq"] if "close_hfq" in frame else frame["close"]
    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    # A2 统一口径：复用 indicators.py 权威 Wilder RSI（与 /api/kline、RSI 告警同口径）
    _rsi_arr = _ind.rsi(close, 14)
    rsi = float(_rsi_arr[-1]) if _rsi_arr and _rsi_arr[-1] is not None else float("nan")

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd = (dif - dea).iloc[-1]

    latest_hfq = float(close.iloc[-1])              # 与均线同轴：评分/趋势判定
    latest = float(frame["close"].iloc[-1])         # 不复权现价：展示 & crosscheck

    score = 50
    reasons = []
    basis_note = "（后复权口径）" if basis == "hfq" else "（不复权口径）"
    if pd.isna(ma20):
        reasons.append("日线数据不足20根，均线评分跳过")
    else:
        if latest_hfq > ma20:
            score += 15; reasons.append("收盘价位于20日均线之上%s" % basis_note)
        else:
            score -= 15; reasons.append("收盘价位于20日均线之下%s" % basis_note)
        if pd.isna(ma5):
            reasons.append("日线数据不足5根，5日线评分跳过")
        elif ma5 > ma20:
            score += 10; reasons.append("短期均线强于中期均线")
        else:
            score -= 10; reasons.append("短期均线弱于中期均线")
    if macd > 0:
        score += 10; reasons.append("MACD柱体为正")
    else:
        score -= 10; reasons.append("MACD柱体为负")
    if not math.isnan(rsi):
        score += 5 if 40 <= rsi <= 70 else -5 if rsi > 75 or rsi < 25 else 0
    score = max(0, min(100, score))
    signal = "buy" if score >= 65 else "sell" if score <= 35 else "hold"

    if pd.isna(ma20) or pd.isna(ma60):
        trend = "sideways"
    elif latest_hfq > ma20 and ma20 >= ma60:
        trend = "uptrend"
    elif latest_hfq < ma20:
        trend = "downtrend"
    else:
        trend = "sideways"

    return {
        "latest": _num(latest), "ma5": _num(ma5), "ma20": _num(ma20),
        "ma60": _num(ma60), "rsi14": _num(rsi, 2), "macd": _num(macd, 4),
        "score": score, "signal": signal, "trend": trend,
        # 支撑/阻力：不复权实际价位，与展示价同轴、可直接对照
        "support": _num(frame["low"].tail(20).min()),
        "resistance": _num(frame["high"].tail(20).max()),
        # 口径留痕：指标基于后复权（hfq）或不复权（raw）
        "price_basis": basis,
        "reasons": reasons,
    }


# ============================================================
# 主 Skill 类
# ============================================================

class StockAnalysisSkill:
    """A 股分析插件的自包含分析引擎。"""

    def __init__(self, config_path: str = None, config: dict = None):
        self.config_path = config_path or CONFIG_FILE
        self.config = self._load_config()
        if config:
            self.config.update(config)
        # DATA_PROVIDER 语义：首选源覆盖（空值 = 按 gateway ROUTE 顺序）
        gateway.apply_preference(self.config.get("data_provider") or self.config.get("DATA_PROVIDER"))

    # ── 公共 API ──

    def analyze(self, symbol: str, analysis_type: str = "llm",
                months: int = 6) -> AnalysisResult:
        """
        全维度股票分析

        Parameters:
            symbol: 股票代码 (如 600519)
            analysis_type: 分析类型
                - "technical"    技术面 (均线、RSI、MACD、支撑阻力)
                - "fundamental"  基本面 (评分 + 财务)
                - "sentiment"    情绪面 (新闻 + 社交)
                - "llm"          LLM 智能综合研判 (默认, 需 LLM_API_KEY)
            months: 历史数据月数
        """
        analysis_type = analysis_type.lower()

        if analysis_type == "technical":
            return self._technical_analysis(symbol)
        elif analysis_type == "fundamental":
            return self._fundamental_analysis(symbol)
        elif analysis_type == "sentiment":
            return self._sentiment_analysis(symbol)
        elif analysis_type == "llm":
            return self._llm_analysis(symbol, months)
        else:
            # 兼容旧调用方：未知类型回落技术面（原 _combined_analysis 语义，避免伪造综合研判）
            return self._technical_analysis(symbol)

    def get_signal(self, symbol: str) -> dict:
        """快速获取交易信号"""
        result = self.analyze(symbol, analysis_type="technical")
        return result.signal

    def market_overview(self) -> Union[dict, str]:
        """市场概况"""
        indices = {"上证指数": "000001", "深证成指": "399001", "创业板指": "399006"}
        result = {}
        ok = fail = 0
        for name, code in indices.items():
            try:
                quote = gateway.get_quote(code, category=DataCategory.INDEX)
                result[name] = {"price": quote["price"], "change_pct": quote["change_pct"]}
                ok += 1
            except Exception as error:
                result[name] = {"error": str(error)}
                fail += 1
        return {"indices": result, "available": ok, "failed": fail}

    # ── 内部实现 ──

    def _load_config(self) -> dict:
        """加载插件配置文件。"""
        config = dict(DEFAULT_CONFIG)
        config_path = self.config_path
        if not os.path.exists(config_path):
            # 尝试当前位置
            config_path = os.path.join(os.getcwd(), "config.yaml")
        if os.path.exists(config_path):
            try:
                import yaml
                with open(config_path, encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                config.update(loaded)
            except Exception as error:
                print(f"[stock_analysis/warning] 配置加载失败: {error}", file=sys.stderr)
        return config

    # ── 各分析类型实现 ──

    def _technical_analysis(self, symbol: str) -> AnalysisResult:
        """技术面分析"""
        try:
            gateway.usage_snapshot()      # 清空上一轮残留，保证 data_sources 只含本次数据点
            frame = gateway.get_kline(symbol)
            if frame.empty:
                raise RuntimeError("未获取到行情数据")
            data = _technical_snapshot(frame)
            # #SA-20260830-02 兜底：显式标注K线数据截止日期，供前端/用户核对数据新鲜度
            data["data_date"] = str(pd.Timestamp(frame.index[-1]).date())
            score = data["score"]

            return AnalysisResult(
                symbol=symbol,
                text_report=self._fmt_technical(symbol, data),
                json_data=data,
                signal={
                    "signal": data["signal"],
                    "confidence": round(abs(score - 50) / 50.0, 2),
                    "reasons": data.get("reasons", []),
                },
                data_sources=gateway.usage_snapshot(),
            )
        except Exception as e:
            return AnalysisResult(symbol=symbol, error=str(e))

    def _fmt_technical(self, symbol: str, data: dict) -> str:
        lines = [f"股票: {symbol}"]
        if data.get("data_date"):
            lines.append(f"数据截至: {data['data_date']}")
        if "trend" in data:
            lines.append(f"趋势: {data['trend']}")
        lines.append(f"综合评分: {data['score']}/100")
        lines.append(f"信号: {data['signal']}")
        lines.append(f"RSI(14): {data['rsi14']}")
        lines.append(f"支撑位: {data['support']}，阻力位: {data['resistance']}")
        lines.append("风险提示: 技术指标不构成投资建议")
        return "\n".join(lines)

    def _fundamental_analysis(self, symbol: str) -> AnalysisResult:
        """基本面分析"""
        try:
            gateway.usage_snapshot()      # 清空上一轮残留，保证 data_sources 只含本次数据点
            basic = gateway.get_quote(symbol)
            price = basic
            pe = basic.get("pe_ttm", 0)
            # #SA-20260831-06：PB 直接取腾讯 parts[46] 市净率（原 price/naps 的 naps
            # 被错误映射为涨停价，导致 PB 恒<=1、评分恒85、pb>=6 风险分支成死代码）
            pb = basic.get("pb", 0)
            score = 50
            reasons = []
            if 0 < pe < 25:
                score += 20; reasons.append("PE处于相对合理区间")
            elif pe >= 50:
                score -= 15; reasons.append("PE较高，估值风险需关注")
            if pb > 0:
                if pb < 3:
                    score += 15; reasons.append("PB处于相对合理区间")
                elif pb >= 6:
                    score -= 15; reasons.append("PB较高，估值风险需关注")
            else:
                reasons.append("PB数据不可用，未参与估值评分")
            score = max(0, min(100, score))

            # 阶段2：Tushare 可用时叠加近 5 年估值分位；token 缺失/无权限自动静默跳过
            valuation = None
            try:
                from .valuation import pe_pb_percentile, valuation_line
                valuation = pe_pb_percentile(symbol)
            except Exception as err:
                _LOGGER.warning("valuation unavailable %s: %s", symbol, err)
            sources = gateway.usage_snapshot()
            if valuation:
                sources.append({"category": "fundamental", "source": "tushare",
                                "authorized": True, "fetched_at": time.time()})

            lines = [
                f"股票: {symbol}",
                f"名称: {basic.get('name', 'N/A')}",
                f"现价: {price.get('price', 'N/A')}",
                f"PE(TTM): {pe or 'N/A'}",
                f"PB: {round(pb, 3) if pb else 'N/A'}",
                f"估值评分: {score}/100",
            ]
            if valuation:
                lines.append(valuation_line(valuation))
            lines += [
                "数据范围: 实时行情估值字段，未包含完整财报",
                "风险提示: 本分析仅基于实时估值字段，不构成投资建议",
            ]
            if not pb:
                lines.append("PB数据不可用（行情源未提供每股净资产），PB未参与估值评分")

            json_data = {
                "basic": {**basic, "pb": pb}, "price": price, "score": score,
                "scope": "valuation_only",
                "disclaimer": "基于实时行情估值字段，未包含完整财报，不构成投资建议",
            }
            if valuation:
                json_data["valuation"] = valuation

            return AnalysisResult(
                symbol=symbol,
                text_report="\n".join(lines),
                json_data=json_data,
                signal={
                    "signal": "buy" if score >= 70 else ("sell" if score <= 30 else "hold"),
                    "confidence": round(abs(score - 50) / 50.0, 2),
                    "reasons": reasons,
                },
                data_sources=sources,
            )
        except Exception as e:
            return AnalysisResult(symbol=symbol, error=str(e))

    def _sentiment_analysis(self, symbol: str) -> AnalysisResult:
        """情绪面分析"""
        try:
            gateway.usage_snapshot()      # 清空上一轮残留，保证 data_sources 只含本次数据点
            titles = gateway.get_news(symbol)
            positive = ("利好", "增长", "回升", "突破", "增持", "盈利", "上涨")
            negative = ("利空", "下滑", "亏损", "减持", "风险", "下跌", "处罚")
            _NEG_PREFIX = ("不", "未", "无")

            def _hit(keys, title):
                for word in keys:
                    idx = title.find(word)
                    if idx >= 0 and (idx == 0 or title[idx - 1] not in _NEG_PREFIX):
                        return True
                return False

            positive_hits = sum(_hit(positive, title) for title in titles)
            negative_hits = sum(_hit(negative, title) for title in titles)
            sentiment_score = ((positive_hits - negative_hits) / max(len(titles), 1))
            sentiment_score = max(-1.0, min(1.0, sentiment_score))

            sample_titles = "; ".join(t[:60] for t in titles[:3])
            lines = [
                f"股票: {symbol}",
                f"新闻条数: {len(titles)}",
                f"情绪评分: {sentiment_score:+.2f}  (-1 ~ +1)",
                "方法: 基于新浪财经新闻标题关键词统计，可能存在误判",
                "风险提示: 情绪面不构成投资建议",
            ]
            if sample_titles:
                lines.append(f"近期新闻示例: {sample_titles}")

            signal_map = "buy" if sentiment_score > 0.3 else ("sell" if sentiment_score < -0.3 else "hold")

            return AnalysisResult(
                symbol=symbol,
                text_report="\n".join(lines),
                json_data={
                    "sentiment": sentiment_score, "news_count": len(titles),
                    "method": "keyword_counting",
                    "samples": titles[:5],
                },
                signal={
                    "signal": signal_map,
                    "confidence": round(abs(sentiment_score), 2),
                    "reasons": [f"情绪评分 {'积极' if sentiment_score > 0 else '消极'}"],
                },
                data_sources=gateway.usage_snapshot(),
            )
        except Exception as e:
            return AnalysisResult(symbol=symbol, error=str(e))

    def _llm_analysis(self, symbol: str, months: int = 6) -> AnalysisResult:
        """通过 VeroRun UnifiedLLM 执行综合分析。"""
        try:
            gateway.usage_snapshot()      # 清空上一轮残留
            technical = self._technical_analysis(symbol)
            if technical.error:
                return technical
            tech_data = technical.json_data
            fundamental = self._fundamental_analysis(symbol)
            if fundamental.error:
                return fundamental
            basic = fundamental.json_data.get("basic", {})
            bscore = fundamental.json_data.get("score", 50)

            # 获取历史行情摘要
            df = gateway.get_kline(symbol, datalen=max(20, months * 20))
            if df is not None and len(df) > 0:
                latest = df.iloc[-1]
                ma_col = "close_hfq" if "close_hfq" in df.columns else "close"
                ma_basis = "后复权" if ma_col == "close_hfq" else "不复权"
                close_latest = float(latest.get("close", latest.get("收盘价", 0)))
                close_ma5 = float(df.tail(5)[ma_col].mean()) if len(df) >= 5 else close_latest
                close_ma20 = float(df.tail(20)[ma_col].mean()) if len(df) >= 20 else close_latest
                change_pct = ((close_latest - df.iloc[-2]["close"]) / df.iloc[-2]["close"] * 100) if len(df) >= 2 else 0
            else:
                close_latest = close_ma5 = close_ma20 = change_pct = 0
                ma_basis = "不复权"

            # A1：证据链（四表/资金流/新闻/估值分位），各源独立降级
            from .evidence import build_evidence_context as _build_evidence
            try:
                from .valuation import pe_pb_percentile, valuation_line
                _val_text = valuation_line(pe_pb_percentile(symbol)) or ""
            except Exception:
                _val_text = ""
            evidence_text = _build_evidence(
                gateway.get_fundamental, gateway.get_moneyflow, gateway.get_news,
                valuation_text=_val_text, symbol=symbol)

            # 构建 LLM Prompt
            prompt = self._build_llm_prompt(symbol, tech_data, basic, bscore,
                                            close_latest, close_ma5, close_ma20, change_pct,
                                            evidence=evidence_text, ma_basis=ma_basis)

            # 调用 LLM（SA-N3：空响应重试，逐次留痕；仍失败给明确降级文案供错误码归类）
            llm_report = None
            for attempt in range(1, _LLM_MAX_ATTEMPTS + 1):
                llm_report = self._call_llm(prompt)
                if llm_report:
                    break
                _LOGGER.warning("UnifiedLLM 返回空响应 symbol=%s attempt=%d/%d",
                                symbol, attempt, _LLM_MAX_ATTEMPTS)
                if attempt < _LLM_MAX_ATTEMPTS:
                    time.sleep(_LLM_RETRY_BACKOFF)
            if not llm_report:
                return AnalysisResult(symbol=symbol, error="UnifiedLLM 返回空响应")

            # 提取信号（A4：结构化解析优先，失败回落既有兜底链）
            from .evidence import parse_structured_output as _parse_structured
            signal = _parse_structured(llm_report) or self._extract_signal(llm_report)

            # P0-v2：置信度改由代码计算，不再采信 LLM 自报值。
            # 四维加权：证据覆盖 35% + 数据新鲜度 20% + 多信号一致度 25% + 历史命中 20%。
            try:
                from .evidence_bundle import EvidenceBundle, compute_confidence
                _eb = EvidenceBundle(uid=symbol)
                _eb.add(key="technical.score", label="技术评分",
                        value=tech_data.get("score", 50), unit="",
                        source="akshare", freshness="eod")
                if tech_data.get("rsi14") is not None:
                    _eb.add(key="technical.rsi14", label="RSI(14)",
                            value=tech_data["rsi14"], unit="",
                            source="akshare", freshness="eod")
                for _k in ("pe_ttm", "pb"):
                    _v = basic.get(_k)
                    if _v is not None and _v != "N/A":
                        _eb.add(key=f"fundamental.{_k}", label=_k.upper(),
                                value=_v, unit="x",
                                source="tushare", freshness="eod")
                if close_latest:
                    _eb.add(key="quote.price", label="最新价",
                            value=close_latest, unit="CNY",
                            source="akshare", freshness="delayed")
                _tech_sig = tech_data.get("signal", "hold")
                _fund_sig = "buy" if bscore >= 60 else "sell" if bscore <= 40 else "hold"
                _agree = 0.8 if (_tech_sig == _fund_sig and _tech_sig != "hold") else \
                         0.3 if (_tech_sig != _fund_sig and _tech_sig != "hold" and _fund_sig != "hold") else 0.5
                _hit_rate = 0.5
                try:
                    from . import models_sa as _sa_mod
                    _sa_mod.ensure_tables()
                    with _sa_mod.get_db() as _conn:
                        _row = _conn.execute(
                            "SELECT ROUND(100.0 * SUM(CASE WHEN hit_5d = 1 THEN 1 ELSE 0 END) "
                            "/ NULLIF(COUNT(*), 0), 1) AS hr FROM sa_signal_realized "
                            "WHERE signal = ? AND trade_date >= current_date - 90",
                            (signal.get("signal", "hold"),)).fetchone()
                        if _row and _row["hr"] is not None:
                            _hit_rate = float(_row["hr"]) / 100.0
                except Exception:
                    pass
                _cc = compute_confidence(_eb, agreement=_agree, strategy_hit_rate=_hit_rate)
                signal["confidence"] = _cc["confidence"]
                signal["confidence_grade"] = _cc["grade"]
                signal["confidence_why"] = _cc["why"]
                signal["confidence_components"] = _cc["components"]
            except Exception as _cc_err:
                _LOGGER.warning("code confidence failed symbol=%s: %s", symbol, _cc_err)

            # 聚合本次分析全部数据点（技术/基本面子分析 + 本层行情摘要）
            sources = []
            for sub in (technical, fundamental):
                if getattr(sub, "data_sources", None):
                    sources.extend(sub.data_sources)
            sources.extend(gateway.usage_snapshot())

            # B2/D-4b：结论沉淀统一由调用方（routes.analyze / jobs_queue）负责
            return AnalysisResult(
                symbol=symbol,
                text_report=llm_report,
                json_data={"technical": tech_data, "fundamental_score": bscore},
                signal=signal,
                data_sources=sources,
            )
        except Exception as e:
            return AnalysisResult(symbol=symbol, error=str(e))

    def _build_llm_prompt(self, symbol, tech_data, basic, bscore,
                          price, ma5, ma20, change_pct,
                          evidence: str = "", ma_basis: str = "不复权") -> str:
        """构建 LLM 分析 prompt（A1：注入证据链，均线标注复权口径）。"""
        trend = tech_data.get("trend", "unknown")
        score = tech_data.get("score", 50)
        signal_cn = tech_data.get("signal", "hold")
        pe = basic.get("pe_ttm", "N/A")
        pb = basic.get("pb", "N/A")
        evidence_section = ("## 证据材料（分析结论必须引用其中内容，不得虚构数据）\n" + evidence) if evidence else ""

        return f"""你是一个专业的A股股票分析师。请对以下股票进行全面分析，给出投资建议。

## 股票信息
- 代码: {symbol}
- 名称: {basic.get("name", "N/A")}
- 现价: {price:.2f}
- 今日涨幅: {change_pct:+.2f}%
- 5日均线({ma_basis}): {ma5:.2f}
- 20日均线({ma_basis}): {ma20:.2f}

## 技术面
- 趋势: {trend}
- 综合评分: {score}/100
- 信号: {signal_cn}
- RSI(14): {tech_data.get("rsi14", "N/A")}

## 基本面
- PE: {pe}
- PB: {pb}
- 基本面评分: {bscore}/100

{evidence_section}

请从以下角度分析:
1. 技术面形态判断（趋势、支撑位、阻力位）
2. 基本面与证据材料交叉验证（引用具体科目/数据）
3. 可验证的综合判断和风险条件

注意: 分析仅供参考，不构成投资建议。请用中文回答，控制在600字以内。
最后输出严格 JSON：{{"signal":"buy|sell|hold","reasons":["..."],"summary":"...","evidence_refs":["..."]}}。置信度由系统根据证据覆盖度与历史命中率自动计算，无需输出。"""

    def _call_llm(self, prompt: str) -> str:
        """调用 VeroRun 内核网关；插件不解析 provider、model 或 API Key。"""
        from agent_matrix.engine import UnifiedLLM
        from agent_matrix.model_resolver import resolve_model_args

        agent_config = None
        try:
            from agent_matrix.models import get_agent_by_slug
            agent_config = get_agent_by_slug("stock_analysis_agent")
        except Exception as error:
            _LOGGER.warning("读取股票分析 Agent 配置失败，将使用 standard tier: %s", error)
        if not agent_config:
            agent_config = resolve_model_args({"strategy": "tier", "tier": "standard"})
        else:
            policy = {"strategy": "tier", "tier": "standard"}
            try:
                agent_config.update(resolve_model_args(policy))
            except Exception as error:
                _LOGGER.warning("解析 Agent 模型策略失败: %s", error)
        # H-1: tier 降级路径返回的键是 'model'，而 UnifiedLLM 读取 'model_name'，
        # 不映射会导致模型为空，_resolve_model 抛 ValueError，LLM 分析整体失败。
        if not agent_config.get("model_name") and agent_config.get("model"):
            agent_config["model_name"] = agent_config["model"]

        # H-2: UnifiedLLM.chat() 不会自动注入 _system_prompt（仅 ask* 系列会），
        # 必须显式前置系统提示词，否则金融合规护栏（不虚构/中性措辞/注明来源）不生效。
        system_prompt = agent_config.get("system_prompt") or ""
        if not system_prompt:
            prompt_path = os.path.join(SKILL_DIR, "agents", "stock_analysis_agent_prompt.md")
            try:
                with open(prompt_path, encoding="utf-8") as f:
                    system_prompt = f.read().strip()
            except OSError:
                system_prompt = ""
        # 接线点⑤：场景化 Prompt。按当前市场时段（stock.preopen/intraday/postclose）
        # 从 agent_prompts 第三层取 active 场景模板，追加到系统提示词之后；
        # 任一失败静默回退既有 system_prompt（场景缺失零事故，不影响主链路）。
        try:
            scene = _scene_prompt(scenario_task_type())
            if scene:
                system_prompt = ((system_prompt or "") + "\n\n---\n\n" + scene).strip()
        except Exception as error:
            _LOGGER.warning("场景 prompt 挂接失败，使用默认系统提示: %s", error)
        messages = [{"role": "user", "content": prompt}]
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})

        return UnifiedLLM(agent_config).chat(
            messages,
            temperature=0.3,
            max_tokens=800,
            module="stock_analysis",
        )

    def _extract_signal(self, report: str) -> dict:
        """从分析报告中提取交易信号"""
        match = re.search(r"\{\s*\"signal\".*?\}", report, re.DOTALL)
        if match:
            try:
                structured = json.loads(match.group(0))
                signal = structured.get("signal")
                confidence = float(structured.get("confidence", 0.5))
                if signal in {"buy", "sell", "hold"} and 0 <= confidence <= 1:
                    return {
                        "signal": signal,
                        "confidence": round(confidence, 2),
                        "reasons": structured.get("reasons", [])[:5],
                    }
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        text = report.lower()

        buy_words = ["买入", "买", "看多", "上涨", "建仓", "加仓", "推荐", "上行",
                     "积极", "乐观", "强势", "突破", "多头"]
        sell_words = ["卖出", "卖", "看空", "下跌", "减仓", "清仓", "回避", "下行",
                      "消极", "悲观", "弱势", "破位", "空头"]
        hold_words = ["持有", "观望", "中性", "震荡", "盘整", "等待"]

        buy_score = sum(2 for w in buy_words if w in text)
        sell_score = sum(2 for w in sell_words if w in text)
        hold_score = sum(1 for w in hold_words if w in text)

        if buy_score == sell_score == hold_score == 0:
            return {"signal": "hold", "confidence": 0.5,
                    "reasons": ["LLM 输出无法解析，保守观望"]}
        if buy_score > sell_score and buy_score > hold_score:
            confidence = min(buy_score / max(sell_score + hold_score + 1, 1) * 0.25, 0.6)
            return {"signal": "buy", "confidence": round(confidence, 2),
                    "reasons": ["LLM 分析倾向看多"]}
        elif sell_score > buy_score and sell_score > hold_score:
            confidence = min(sell_score / max(buy_score + hold_score + 1, 1) * 0.25, 0.6)
            return {"signal": "sell", "confidence": round(confidence, 2),
                    "reasons": ["LLM 分析倾向看空"]}
        else:
            confidence = max(0.5 + (hold_score - abs(buy_score - sell_score)) * 0.05, 0.1)
            return {"signal": "hold", "confidence": round(min(confidence, 0.9), 2),
                    "reasons": ["LLM 分析倾向中性/观望"]}


# ============================================================
# CLI 入口
# ============================================================


def main():
    parser = argparse.ArgumentParser(
        description="Stock Analysis Skill — A 股分析技能"
    )
    parser.add_argument("symbol", nargs="?", help="股票代码 (如 600519)")
    parser.add_argument("--type", "-t", choices=["technical", "fundamental", "sentiment", "llm"],
                        default="llm", help="分析类型 (默认: llm)")
    parser.add_argument("--months", "-m", type=int, default=6, help="历史数据月数")
    parser.add_argument("--format", "-f", choices=["text", "json", "signal"],
                        default="text", help="输出格式")
    parser.add_argument("--market", action="store_true", help="市场概况")

    # 配置
    parser.add_argument("--config", default=None, help="配置文件路径")

    args = parser.parse_args()

    # 初始化 Skill
    skill = StockAnalysisSkill(config_path=args.config)

    # ── 市场概况 ──
    if args.market:
        result = skill.market_overview()
        if isinstance(result, dict) and "indices" in result:
            indices = result["indices"]
            print(f"{'指数':<20} {'涨跌':>8} {'涨幅':>8}")
            print("-" * 40)
            for name, data in (indices.items() if isinstance(indices, dict) else [("", {})]):
                change = data.get("change", 0)
                if not change and data.get("price") is not None:
                    change = data["price"] * data.get("change_pct", 0) / 100
                pct = data.get("change_pct", 0)
                print(f"{name:<20} {change:>+8.2f} {pct:>+7.2f}%")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # ── 个股分析 ──
    if not args.symbol:
        parser.print_help()
        return

    result = skill.analyze(args.symbol, analysis_type=args.type, months=args.months)

    if args.format == "json":
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
    elif args.format == "signal":
        print(json.dumps(result.to_signal(), ensure_ascii=False, indent=2))
    else:
        # Print text output, skip non-JSON noise if any
        text = result.to_text()
        # Find the actual report starting point
        idx = text.find("===")
        if idx >= 0:
            text = text[idx:]
        print(text)
        if result.error:
            print(f"\n[错误] {result.error}", file=sys.stderr)


if __name__ == "__main__":
    main()
