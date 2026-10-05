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
    raw = str(symbol or "").strip()
    # ── 境外前置分支（v2.1.0 新增；与 secmaster UID 规范 {market}:{code} 对齐）──
    # ① UID 形态 "HK:00700" / "US:AAPL"：幂等原样返回（大写归一）
    if re.match(r"^(?:HK|US):", raw, re.I):
        return raw.upper()
    # ② 已带境外前缀 "hk00700" / "usAAPL"：统一转成 UID 形态（下游只认一套规范）
    m = re.match(r"^(hk|us)([A-Za-z0-9]+)$", raw, re.I)
    if m:
        return m.group(1).upper() + ":" + m.group(2).upper()
    # ③ 交易所后缀 "0700.HK"（Yahoo/东财风，数字 1~5 位）/ "AAPL.US" → UID。
    #    港股裸码内部统一补齐 5 位（0700→00700），对外源所需的 4 位 .HK 形态由 provider 层转换。
    q = re.match(r"^([0-9]{1,5}|[A-Za-z]{1,6})\.(HK|US)$", raw)
    if q:
        code, mkt = q.group(1), q.group(2).upper()
        return "%s:%s" % (mkt, code.zfill(5) if code.isdigit() else code.upper())
    # ── 以下为原有 A 股规则，逐字保留，不得改动 ──
    clean = re.sub(r"^(?:SH|SZ|BJ)(?=\d)", "", raw.upper())
    clean = clean.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    # ④ 1~5 位纯数字 → 港股（A 股股票/指数/基金恒 6 位，债券 6/12 位，申购码 7 位，无冲突面）
    if clean.isdigit() and 1 <= len(clean) <= 5:
        return "HK:" + clean.zfill(5)
    # ⑤ 纯字母 → 美股
    if clean.isalpha():
        return "US:" + clean
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
