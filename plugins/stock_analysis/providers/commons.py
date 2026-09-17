# providers/commons.py — 符号规范化
# 自 stock_skill._market_symbol / _index_symbol 逐字平移（v1.3.0），规则不得改动
import re


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
    return ("sh" if clean in {"000001", "000300"} else "sz") + clean
