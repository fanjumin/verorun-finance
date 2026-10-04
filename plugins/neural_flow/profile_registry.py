"""profile_registry.py — Domain Profile 注册中心（方案 v1.3 §7.12）。

模板化铁律：渲染引擎行业无关；行业差异全部由 Profile 声明。

档案来源两级：
  1. 内置档案（本文件 BUILTIN_PROFILES）：platform（基座自带，llm/agent/system 域）、
     stock（金融版首发域——与 stock_analysis P1 埋点对齐）；
  2. 外部档案：其他插件目录下的 domain.yaml（声明 neural_flow.profile 能力即约定），
     启动/插件启停时扫描，schema 校验失败拒载并留痕（对齐 plugin_registry.last_error
     错误留痕惯例，不阻塞其他域）。

Profile Schema 六声明区（v1.3 §7.12）：pipeline / decision+routes / arches /
stage_dict / statusbar / nouns。校验规则：
  - domain 必填且 [a-z][a-z0-9_]{1,31}（ASCII 白名单，2~32 位、首字母开头）；
  - pipeline 至少 1 项，stage 必须落在通用核（sdk.CORE_STAGES）或本档案 stage_dict；
  - arches 扩展拱必填 field（span 取值路径）与 axis；
  - stage_dict 引用的 color 必须在 COLOR_ALIASES 内（禁自由 hex——双主题安全）；
  - decision/routes/statusbar/nouns 可选（NF-11），提供时做基础结构校验。
"""
from __future__ import annotations

import re

from plugin_manager.logger import get_plugin_logger

_log = get_plugin_logger("neural_flow")

from .sdk import CORE_STAGES

# 引擎色板别名（映射桌面端 --nf-* 语义变量；Profile 禁自由 hex，双主题安全）
COLOR_ALIASES = ("data", "ind", "evid", "deci", "llm", "ok", "gold", "grey")
MOTION_LEVELS = ("L1", "L2", "L3")

# domain 命名白名单：ASCII 收紧（str.isalnum/isalpha 为 Unicode 语义，中文域可绕过校验）
_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

BUILTIN_PROFILES = {
    "platform": {
        "domain": "platform",
        "display": {"zh": "平台内核", "en": "Platform Core"},
        "builtin": True,
        "pipeline": [
            {"stage": "data", "card": {"title": {"zh": "平台事件", "en": "Platform Events"},
                                       "icon": "▦", "items": []}},
            {"stage": "llm", "card": {"title": {"zh": "LLM 调用", "en": "LLM Calls"},
                                      "icon": "◉", "items": []}},
            {"stage": "output", "card": {"title": {"zh": "任务产出", "en": "Task Output"},
                                         "icon": "✓", "items": []}},
        ],
        "decision": {"title": {"zh": "决策分流", "en": "Decision Split"},
                     "caption": {"zh": "成本 vs 价值", "en": "Cost vs Value"}},
        "routes": {
            "expensive": {"card": {"title": {"zh": "昂贵 LLM 生成", "en": "Expensive LLM"},
                                   "icon": "◉", "items": []}},
            "cheap": {"card": {"title": {"zh": "廉价复用", "en": "Cheap Reuse"},
                               "icon": "⟲", "items": []}},
        },
        "arches": [{"id": "confidence"}, {"id": "cost"}],
        "stage_dict": {},
        "statusbar": [{"source": "builtin.health"}, {"source": "builtin.latency"}],
        "nouns": {"trace": {"zh": "任务", "en": "Task"},
                  "entity": {"zh": "对象", "en": "Entity"}},
    },
    "stock": {
        "domain": "stock",
        "display": {"zh": "股票分析", "en": "Stock Analysis"},
        "builtin": True,
        "pipeline": [
            {"stage": "data", "card": {"title": {"zh": "数据摄入", "en": "Data Ingestion"},
                                       "icon": "▦",
                                       "items": [{"zh": "行情 · K线", "en": "Market & K-line"},
                                                 {"zh": "财务四表", "en": "Financials"},
                                                 {"zh": "资金流向", "en": "Money Flow"},
                                                 {"zh": "新闻 / 事件", "en": "News & Events"}]}},
            {"stage": "indicator", "card": {"title": {"zh": "技术指标", "en": "Indicators"},
                                            "icon": "◈",
                                            "items": [{"zh": "MACD / KDJ / RSI", "en": "MACD/KDJ/RSI"},
                                                      {"zh": "布林带 · 分位", "en": "Bollinger · Quantile"},
                                                      {"zh": "量价波动", "en": "Volume & Volatility"},
                                                      {"zh": "估值分位", "en": "Valuation Quantile"}]}},
            {"stage": "evidence", "card": {"title": {"zh": "证据链构建", "en": "Evidence Chain"},
                                           "icon": "⛓",
                                           "items": [{"zh": "四源特征打包", "en": "4-Source Bundle"},
                                                     {"zh": "证据关联加权", "en": "Weighting"},
                                                     {"zh": "可解释标注", "en": "Explainability"}]}},
        ],
        "decision": {"title": {"zh": "决策分流", "en": "Decision Split"},
                     "caption": {"zh": "智能评估 · 成本 vs 价值", "en": "Cost vs Value"}},
        "routes": {
            "expensive": {"card": {"title": {"zh": "昂贵 LLM 生成", "en": "Expensive LLM Generation"},
                                   "icon": "◉",
                                   "items": [{"zh": "深度推理生成", "en": "Deep Reasoning"},
                                             {"zh": "复杂场景分析", "en": "Complex Analysis"}]}},
            "cheap": {"card": {"title": {"zh": "廉价缓存复用", "en": "Cheap Cache Reuse"},
                               "icon": "⟲",
                               "items": [{"zh": "历史结果复用", "en": "Historical Reuse"},
                                         {"zh": "低成本快速返回", "en": "Fast Return"}]}},
        },
        "arches": [{"id": "confidence"}, {"id": "cost"}],
        "stage_dict": {},
        "statusbar": [{"source": "builtin.health"}, {"source": "builtin.latency"},
                      {"source": "builtin.throughput"}],
        "nouns": {"trace": {"zh": "任务", "en": "Job"},
                  "entity": {"zh": "标的", "en": "Symbol"}},
    },
}

# platform 档案子域：运行期发射的 llm/agent/system 共享 platform 渲染档案
# （pipeline/decision/routes/arches 同源），仅显示名不同——分别对应
# platform pipeline 的 llm / output / data 三张卡。
PLATFORM_SUBDOMAINS = {
    "llm": {"zh": "LLM 调用", "en": "LLM Calls"},
    "agent": {"zh": "Agent 活动", "en": "Agent Activity"},
    "system": {"zh": "系统事件", "en": "System Events"},
}

_REGISTRY: dict = {}
_ERRORS: list = []


def validate_profile(prof: dict) -> tuple[bool, str]:
    """schema 校验；返回 (ok, reason)。"""
    if not isinstance(prof, dict):
        return False, "profile must be a mapping"
    domain = prof.get("domain")
    if not isinstance(domain, str) or not _DOMAIN_RE.match(domain):
        return False, "domain must be [a-z][a-z0-9_]{1,31}"
    stage_dict = prof.get("stage_dict") or {}
    for key, spec in stage_dict.items():
        if not isinstance(spec, dict) or spec.get("color") not in COLOR_ALIASES:
            return False, "stage_dict.%s.color must be one of %s" % (key, COLOR_ALIASES)
        if spec.get("motion", "L2") not in MOTION_LEVELS:
            return False, "stage_dict.%s.motion must be one of %s" % (key, MOTION_LEVELS)
    pipeline = prof.get("pipeline")
    if not isinstance(pipeline, list) or not pipeline:
        return False, "pipeline must be a non-empty list"
    allowed = set(CORE_STAGES) | set(stage_dict)
    for i, node in enumerate(pipeline):
        if not isinstance(node, dict) or node.get("stage") not in allowed:
            return False, "pipeline[%d].stage must be a core stage or declared in stage_dict" % i
    for arch in prof.get("arches") or []:
        if not isinstance(arch, dict) or not arch.get("id"):
            return False, "arch.id required"
        if arch["id"] not in ("confidence", "cost"):  # 扩展拱
            if not arch.get("field"):
                return False, "extended arch %r requires field" % arch["id"]
            axis = arch.get("axis")
            if not isinstance(axis, dict) or "min" not in axis or "max" not in axis:
                return False, "extended arch %r requires axis {min,max}" % arch["id"]
    # ── 可选声明区（NF-11）：提供才做基础结构校验，防畸形原样下发渲染端 ──
    decision = prof.get("decision")
    if decision is not None:
        if not isinstance(decision, dict):
            return False, "decision must be a mapping"
        for dk in ("title", "caption"):
            dv = decision.get(dk)
            if dv is not None and not isinstance(dv, dict):
                return False, "decision.%s must be a mapping" % dk
    routes = prof.get("routes")
    if routes is not None:
        if not isinstance(routes, dict):
            return False, "routes must be a mapping"
        for rk, route in routes.items():
            if not isinstance(route, dict):
                return False, "routes.%s must be a mapping" % rk
            card = route.get("card")
            if card is not None and not isinstance(card, dict):
                return False, "routes.%s.card must be a mapping" % rk
    statusbar = prof.get("statusbar")
    if statusbar is not None:
        if not isinstance(statusbar, list):
            return False, "statusbar must be a list"
        for i, item in enumerate(statusbar):
            if not isinstance(item, dict) or not str(item.get("source") or "").strip():
                return False, "statusbar[%d] requires a non-empty source" % i
    nouns = prof.get("nouns")
    if nouns is not None:
        if not isinstance(nouns, dict):
            return False, "nouns must be a mapping"
        for nk, noun in nouns.items():
            if not isinstance(noun, dict):
                return False, "nouns.%s must be a mapping" % nk
    return True, ""


def _is_reserved_domain(domain: str) -> bool:
    """内置档案命名空间（platform/stock）保留，禁止外部同名覆盖。

    注意：llm/agent/system 是 platform 的子域别名（NF-04），不在保留范围——
    外部可注册为独立档案并优先于子域视图。
    """
    return domain in BUILTIN_PROFILES


def _scan_external(base_dir=None) -> int:
    """扫描 plugins/*/domain.yaml（声明 neural_flow.profile 能力的行业插件）。

    base_dir 仅供单测注入临时插件目录；生产路径（refresh_registry）不传。
    """
    count = 0
    try:
        import os
        import yaml  # optional 依赖，缺失时仅内置档案
    except ImportError:
        _log.info("pyyaml unavailable — external profiles skipped")
        return 0
    base = base_dir or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))  # plugins/
    try:
        entries = sorted(os.listdir(base))
    except Exception:
        return 0
    for name in entries:
        path = os.path.join(base, name, "domain.yaml")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                prof = yaml.safe_load(fh) or {}
            ok, reason = validate_profile(prof)
            if not ok:
                _ERRORS.append({"plugin": name, "file": "domain.yaml", "reason": reason})
                _log.warning("profile rejected (%s): %s", name, reason)
                continue
            domain = prof["domain"]
            if _is_reserved_domain(domain):
                reason = "domain %r is reserved by a built-in profile" % domain
                _ERRORS.append({"plugin": name, "file": "domain.yaml", "reason": reason})
                _log.warning("profile rejected (%s): %s", name, reason)
                continue
            _REGISTRY[domain] = prof
            count += 1
        except Exception as err:
            _ERRORS.append({"plugin": name, "file": "domain.yaml", "reason": str(err)})
    return count


def refresh_registry() -> int:
    """重建注册表（内置 + 外部）。返回档案总数。"""
    global _REGISTRY, _ERRORS
    _REGISTRY = dict(BUILTIN_PROFILES)  # 内置档案总是可用
    _ERRORS = []
    _scan_external()
    return len(_REGISTRY)


def get_registry() -> dict:
    if not _REGISTRY:
        refresh_registry()
    return _REGISTRY


def get_profile(domain: str):
    registry = get_registry()
    if domain in registry:
        return registry[domain]
    if domain in PLATFORM_SUBDOMAINS:   # 子域 → platform 档案
        return registry.get("platform")
    return None


def list_profiles() -> list:
    """对外档案视图：内置/外部档案 + platform 子域（llm/agent/system）。

    子域项复用 platform 渲染配置，仅 domain/display 不同并以 alias_of 标记；
    外部档案显式占用同名域时优先外部（跳过该子域视图）。
    """
    registry = get_registry()
    views = list(registry.values())
    platform = registry.get("platform")
    if platform:
        for sub, display in PLATFORM_SUBDOMAINS.items():
            if sub in registry:
                continue
            view = dict(platform)
            view["domain"] = sub
            view["display"] = display
            view["alias_of"] = "platform"
            views.append(view)
    return views


def get_errors() -> list:
    return list(_ERRORS)
