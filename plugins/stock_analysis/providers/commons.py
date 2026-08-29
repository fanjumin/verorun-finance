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
    """Return the exchange-qualified symbol for a supported index."""
    return ("sh" if code in {"000001", "000300"} else "sz") + code
