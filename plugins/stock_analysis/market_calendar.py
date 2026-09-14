"""market_calendar.py — A 股交易日历（离线 CSV 优先 + 周末规则兜底）

- data/calendar/{year}.csv 存在时以 CSV 为准（可精确剔除法定节假日休市日）
- CSV 缺失时回退周末规则：周一至周五视为交易日（节假日会多跑，数据源无新行情自动失败计数）
- 法定节假日休市日需按上交所/深交所年度公告逐年补齐 CSV
"""

import csv
import logging
import os
from datetime import date, datetime, timedelta

_log = logging.getLogger("stock_analysis.market_calendar")

_CALENDAR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "calendar")

_YEAR_CACHE = {}


def _load_year(year: int):
    """读取指定年份交易日集合；文件缺失返回 None（触发周末兜底）。进程内缓存，避免重复文件 IO。"""
    if year in _YEAR_CACHE:
        return _YEAR_CACHE[year]
    path = os.path.join(_CALENDAR_DIR, "%d.csv" % year)
    if not os.path.exists(path):
        _log.warning("calendar %s.csv missing, fallback to weekend rule", year)
        _YEAR_CACHE[year] = None
        return None
    days = set()
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                days.add(datetime.strptime(row["date"].strip(), "%Y-%m-%d").date())
            except (KeyError, ValueError):
                continue
    _YEAR_CACHE[year] = days
    return days


def is_trading_day(d: date | None = None) -> bool:
    """当天是否为交易日。"""
    d = d or date.today()
    days = _load_year(d.year)
    if days is not None:
        return d in days
    return d.weekday() < 5  # 周末兜底


def latest_trading_day(d: date | None = None) -> date:
    """往回找最近交易日（含 d 自身），用于 K 线缓存新鲜度判定。"""
    d = d or date.today()
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def recent_trading_days(d: date | None = None):
    """返回 (最近交易日, 上一交易日)。"""
    d = d or date.today()
    latest = latest_trading_day(d)
    prev = latest_trading_day(latest - timedelta(days=1))
    return latest, prev


def next_trading_day(d: date) -> date:
    """下一个交易日（含非交易日跨越）。"""
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt
