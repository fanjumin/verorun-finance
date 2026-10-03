"""trade_calendar.py --- Trade-date attribution for multi-asset time series.

Audit V-14 requires the ``ma_bars.trade_date`` attribution rule to be defined in
the data dictionary instead of being inferred in the display layer. This module is
that single definition.

Rule
----
* Night session: futures/options whose product trades a night session open
  at 21:00 and their trades are attributed to the **next trading day**. A trade at
  01:30 also belongs to the night session opened the previous evening and is
  attributed to that same next trading day.
* Day session and all non-night instruments: attributed to the calendar date when
  that date is a trading day, otherwise to the next trading day.

Holiday awareness: when the stock_analysis market calendar is importable we defer
to it; otherwise we fall back to the weekday rule (Mon-Fri) — documented as an
approximation so no silent wrong-date assumption is hidden.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from . import asset_symbol as sym

__all__ = ["NIGHT_SESSION_START", "is_trading_day", "next_trading_day",
           "has_night_session", "assign_trade_date", "session_of"]

# Most commodity night sessions open at 21:00 (CST).
NIGHT_SESSION_START = time(21, 0)
# Anything before this hour is treated as the tail of the previous night session.
MORNING_CUTOFF = time(8, 0)

# CFFEX products WITHOUT a night session (index futures/options).
_CFFEX_NO_NIGHT = frozenset({"IF", "IH", "IC", "IM", "IO", "HO", "MO"})
# Exchanges whose products trade night sessions (commodity + CFFEX treasury).
_NIGHT_EXCHANGES = frozenset({"SHFE", "INE", "DCE", "CZCE", "GFEX"})


def _external_calendar():
    """stock_analysis.market_calendar when available, else None (optional reuse)."""
    try:
        from plugins.stock_analysis import market_calendar
        return market_calendar
    except Exception:
        return None


def is_trading_day(day) -> bool:
    """Trading-day test with graceful degradation to the Mon-Fri rule."""
    d = day.date() if isinstance(day, datetime) else day
    cal = _external_calendar()
    if cal is not None and hasattr(cal, "is_trading_day"):
        try:
            return bool(cal.is_trading_day(d))
        except Exception:
            pass
    return d.weekday() < 5


def next_trading_day(day) -> date:
    """First trading day strictly after ``day``."""
    d = day.date() if isinstance(day, datetime) else day
    nxt = d + timedelta(days=1)
    for _ in range(30):
        if is_trading_day(nxt):
            return nxt
        nxt += timedelta(days=1)
    return nxt


def has_night_session(asset_type, exchange, code: str = None) -> bool:
    """Whether the instrument trades a night session."""
    try:
        at = sym.AssetType(asset_type) if not isinstance(asset_type, sym.AssetType) \
            else asset_type
    except ValueError:
        return False
    ex = str(exchange or "")
    if at in (sym.AssetType.FUTURE, sym.AssetType.OPTION):
        if ex in _NIGHT_EXCHANGES:
            return True
        if ex == sym.Exchange.CFFEX.value:
            product = ""
            if code:
                import re
                m = re.match(r"^([A-Za-z]{1,3})", str(code).strip())
                product = m.group(1).upper() if m else ""
            return product not in _CFFEX_NO_NIGHT
    return False


def session_of(dt: datetime) -> str:
    """Classify a timestamp as ``night`` / ``morning`` / ``day``."""
    t = dt.time()
    if t >= NIGHT_SESSION_START:
        return "night"
    if t < MORNING_CUTOFF:
        return "morning"
    return "day"


def assign_trade_date(asset_type, exchange=None, code: str = None,
                      dt: datetime = None) -> date:
    """Attribution rule (see module docstring). Returns the trade date.

    ``dt`` defaults to now(). Night-session-capable instruments trading at or
    after 21:00 are attributed to the next trading day; everything else is
    attributed to its own calendar date when that is a trading day, else the next.
    """
    if dt is None:
        dt = datetime.now()
    if isinstance(dt, date) and not isinstance(dt, datetime):
        dt = datetime(dt.year, dt.month, dt.day, 15, 0)
    day = dt.date()
    if has_night_session(asset_type, exchange, code) and session_of(dt) == "night":
        return next_trading_day(day)
    return day if is_trading_day(day) else next_trading_day(day)
