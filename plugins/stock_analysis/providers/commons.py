# providers/commons.py — 符号规范化
# 自 stock_skill._market_symbol / _index_symbol 逐字平移（v1.3.0），规则不得改动
import re

# 境外/港股等指数前缀白名单（小写）；已带这些前缀的符号在 index_symbol 中幂等原样返回
FOREIGN_INDEX_PREFIXES = ("us", "hk", "jp", "kr", "gb", "uk")

# 指数候选清单（设置页「指数选择」多选候选；nameKey 与前端 i18n 对齐，
# symbol 为规范形态——index_symbol() 幂等输出的小写形式，如 usDJI → usdji）
INDEX_CANDIDATES = [
    {"symbol": "sh000001", "name": "上证指数", "nameKey": "stock.indexSh"},
    {"symbol": "sz399001", "name": "深证成指", "nameKey": "stock.indexSz"},
    {"symbol": "sz399006", "name": "创业板指", "nameKey": "stock.indexCy"},
    {"symbol": "sh000300", "name": "沪深300", "nameKey": "stock.indexHs300"},
    {"symbol": "hkhsi", "name": "恒生指数", "nameKey": "stock.indexHsi"},
    {"symbol": "hkhscei", "name": "恒生国企指数", "nameKey": "stock.indexHscei"},
    {"symbol": "usdji", "name": "道琼斯", "nameKey": "stock.indexDji"},
    {"symbol": "usixic", "name": "纳斯达克", "nameKey": "stock.indexIxic"},
    {"symbol": "usinx", "name": "标普500", "nameKey": "stock.indexInx"},
    {"symbol": "jpn225", "name": "日经225", "nameKey": "stock.indexN225"},
]


def market_symbol(symbol: str) -> str:
    clean = re.sub(r"^(?:SH|SZ|BJ)(?=\d)", "", symbol.upper())
    clean = clean.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    # 北交所含 43/83/87/88 段与 2024 启用的 920xxx 新段；
    # '9' 开头需先排除 920，其余 9 开头仍归沪（沪 B 股 900xxx）。
    if clean.startswith("920") or clean.startswith(("4", "8")):
        prefix = "bj"
    elif clean.startswith(("5", "6", "9")):
        prefix = "sh"
    else:
        prefix = "sz"
    return prefix + clean


def index_symbol(code: str) -> str:
    """Return the exchange-qualified symbol for a supported index.

    幂等：已带交易所前缀的代码原样规范化返回，避免二次拼接。
    历史缺陷：gateway._fetch(INDEX) 与本函数的调用方（如 TencentProvider._do_fetch）
    都会调一次本函数，旧实现无条件拼前缀 →
    `000001` 经两层后变成 `szsh000001` → 腾讯返回 v_pv_none_match（字段数 1，低于 40
    阈值）→ ProviderError → 连续 3 次进入 300s 冷却 → INDEX 链路
    [tencent, wind, choice] 全灭 → /api/market 报「数据源暂时不可用」。
    与 market_symbol() 的既有行为对齐（那里本就先剥离前缀）。
    """
    clean = re.sub(r"^(?:SH|SZ|BJ)(?=\d)", "", code.upper())
    clean = clean.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    low = clean.lower()
    # 境外/港股/日韩/英指数：已带白名单前缀的符号幂等返回「小写市场前缀 + 大写代码」
    # （usDJI / hkHSI）——腾讯行情对境外指数大小写敏感，全小写 usdji 返回
    # v_pv_none_match（实测 2026-09-19：usDJI/hkHSI/usIXIC 正常，usdji/hkhsi/usixic 无匹配）。
    for prefix in FOREIGN_INDEX_PREFIXES:
        if low.startswith(prefix):
            return prefix + clean[len(prefix):].upper()
    return ("sh" if clean in {"000001", "000300"} else "sz") + clean
