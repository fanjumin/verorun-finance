#!/usr/bin/env python3
"""V-14 — trade-date attribution for multi-asset time series.

Audit V-14: the ``ma_bars.trade_date`` rule must live in one place instead of being
inferred by the display layer, and it must get the night session right:

  * a futures trade at 21:30 belongs to the **next** trading day;
  * a trade at 01:30 belongs to the same next trading day (still the previous
    evening's night session);
  * CFFEX index futures/options have no night session, so 21:30 belongs to that day;
  * a weekend/holiday timestamp rolls forward to the next trading day.

The external stock_analysis calendar is stubbed out so the expectations are
deterministic on any host.
"""
import unittest
from datetime import date, datetime
from unittest import mock

from plugins.multi_asset import trade_calendar as tc


class TradeCalendarTest(unittest.TestCase):

    def setUp(self):
        # Deterministic Mon-Fri rule; the real holiday feed is exercised elsewhere.
        patcher = mock.patch.object(tc, "_external_calendar", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    # 2026-03-06 is a Friday, 2026-03-07/08 are the weekend, 03-09 is the Monday.
    FRIDAY = date(2026, 3, 6)
    MONDAY = date(2026, 3, 9)

    def test_friday_is_a_trading_day_and_saturday_is_not(self):
        self.assertTrue(tc.is_trading_day(self.FRIDAY))
        self.assertFalse(tc.is_trading_day(date(2026, 3, 7)))
        self.assertFalse(tc.is_trading_day(date(2026, 3, 8)))

    def test_next_trading_day_skips_the_weekend(self):
        self.assertEqual(tc.next_trading_day(self.FRIDAY), self.MONDAY)

    def test_night_session_boundary(self):
        self.assertEqual(tc.NIGHT_SESSION_START.hour, 21)
        self.assertEqual(tc.session_of(datetime(2026, 3, 6, 20, 59)), "day")
        self.assertEqual(tc.session_of(datetime(2026, 3, 6, 21, 0)), "night")
        self.assertEqual(tc.session_of(datetime(2026, 3, 6, 1, 30)), "morning")

    def test_night_session_products(self):
        self.assertTrue(tc.has_night_session("FUTURE", "SHFE", "rb2610"))
        self.assertTrue(tc.has_night_session("FUTURE", "DCE", "m2609"))
        self.assertTrue(tc.has_night_session("FUTURE", "CZCE", "SA605"))
        self.assertTrue(tc.has_night_session("FUTURE", "GFEX", "si2609"))
        self.assertTrue(tc.has_night_session("FUTURE", "CFFEX", "T2603"))   # treasury
        self.assertFalse(tc.has_night_session("FUTURE", "CFFEX", "IF2603"))  # index
        self.assertFalse(tc.has_night_session("OPTION", "CFFEX", "IO2603"))

    def test_commodity_night_trade_belongs_to_next_trading_day(self):
        """A Friday 21:30 commodity trade is attributed to Monday."""
        got = tc.assign_trade_date("FUTURE", "SHFE", "rb2610",
                                   datetime(2026, 3, 6, 21, 30))
        self.assertEqual(got, self.MONDAY)

    def test_early_morning_tail_belongs_to_the_same_next_day(self):
        got = tc.assign_trade_date("FUTURE", "SHFE", "rb2610",
                                   datetime(2026, 3, 7, 1, 30))
        self.assertEqual(got, self.MONDAY)

    def test_day_session_belongs_to_the_same_day(self):
        got = tc.assign_trade_date("FUTURE", "SHFE", "rb2610",
                                   datetime(2026, 3, 6, 14, 0))
        self.assertEqual(got, self.FRIDAY)

    def test_cffex_index_future_has_no_night_session(self):
        """21:30 on an index future is not a night trade -> same trading day."""
        got = tc.assign_trade_date("FUTURE", "CFFEX", "IF2603",
                                   datetime(2026, 3, 6, 21, 30))
        self.assertEqual(got, self.FRIDAY)

    def test_weekend_timestamp_rolls_forward(self):
        got = tc.assign_trade_date("FUND", "SZSE", "159915",
                                   datetime(2026, 3, 7, 12, 0))
        self.assertEqual(got, self.MONDAY)

    def test_date_only_input_is_treated_as_a_day_session(self):
        got = tc.assign_trade_date("BOND", "SSE", "019547", date(2026, 3, 6))
        self.assertEqual(got, self.FRIDAY)

    def test_unknown_asset_type_has_no_night_session(self):
        self.assertFalse(tc.has_night_session("NOT_A_TYPE", "SHFE", "x"))

    def test_funds_have_no_night_session_so_a_friday_stamp_stays_friday(self):
        """Fund NAV belongs to its publication day; 22:00 must not roll to Monday."""
        self.assertFalse(tc.has_night_session("FUND", "SSE", "510300"))
        self.assertEqual(tc.assign_trade_date("FUND", "SSE", "510300",
                                              datetime(2026, 3, 6, 22, 0)),
                         self.FRIDAY)

    def test_cffex_no_night_set_covers_all_index_products(self):
        for product in ("IF", "IH", "IC", "IM", "IO", "HO", "MO"):
            with self.subTest(product=product):
                self.assertIn(product, tc._CFFEX_NO_NIGHT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
