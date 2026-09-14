#!/usr/bin/env python3
"""test_alert_engine.py — D1-c 告警评估引擎单元测试（alert_engine.py）。

覆盖：serialize_rule / _fmt_num / _in_silent_window / 6 类 _condition 边界 /
scan_alerts 状态机（active→triggered→rearm、signal_change 建档与触发）/
scheduled_scan 定时驱动（lock 竞争跳过 / DB 不可用降级）。

运行（需 stock 依赖环境，.stock_deps 在 PYTHONPATH）：
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.stock_analysis.tests.test_alert_engine -v

说明：全部 DB/行情/指标/信号取数均为 mock，不触网不连库。
"""
import os
import sys
import unittest
from datetime import datetime, time
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

import plugins.stock_analysis.alert_engine as ae
from plugins.stock_analysis import alert_engine


def _freeze(now_dt):
    """冻结 alert_engine 模块内的 datetime.now。

    只 patch 模块属性（datetime 是 C 类型，实例属性不可 setattr）。
    """
    clock = mock.Mock()
    clock.now.return_value = now_dt
    return mock.patch.object(alert_engine, "datetime", clock)


# 构造规则行（对齐 models_sa.evaluable_alerts 返回：TIME 列→time、threshold→Decimal）
def _row(alert_id=1, symbol="600519", name="贵州茅台", typ="price_above",
         threshold=None, channel="in_app", status="active",
         silent_from=None, silent_to=None, last_signal=None):
    return {
        "id": alert_id, "symbol": symbol, "name": name, "type": typ,
        "threshold": Decimal(str(threshold)) if threshold is not None else None,
        "channel": channel, "status": status,
        "silent_from": silent_from, "silent_to": silent_to,
        "last_signal": last_signal,
    }


class SerializeRuleTest(unittest.TestCase):
    def test_converts_types(self):
        rule = _row(alert_id=3, threshold=1500, last_signal="buy")
        out = ae.serialize_rule(rule)
        self.assertEqual(out["id"], "3")
        self.assertEqual(out["threshold"], 1500.0)
        self.assertEqual(out["symbol"], "600519")
        self.assertEqual(out["name"], "贵州茅台")
        self.assertEqual(out["status"], "active")
        self.assertEqual(out["channel"], "in_app")

    def test_none_fields(self):
        out = ae.serialize_rule(_row(threshold=None))
        self.assertIsNone(out["threshold"])
        self.assertIsNone(out["last_triggered_at"])
        self.assertIsNone(out["created_at"])

    def test_legacy_channel_normalized(self):
        # D-08：旧中文行值读取归一为语义码，存量数据读兼容
        self.assertEqual(ae.serialize_rule(_row(channel="站内信"))["channel"], "in_app")
        self.assertEqual(ae.serialize_rule(_row(channel="邮件"))["channel"], "email")
        self.assertEqual(ae.serialize_rule(_row(channel="IM"))["channel"], "im")


class FormattingTest(unittest.TestCase):
    def test_fmt_num(self):
        self.assertEqual(ae._fmt_num(1350.0), "1350")
        self.assertEqual(ae._fmt_num(3.1200), "3.12")
        self.assertEqual(ae._fmt_num(None), "")


class SilentWindowTest(unittest.TestCase):
    def _rule(self, sf, st):
        return _row(silent_from=sf, silent_to=st)

    def test_no_window_always_false(self):
        self.assertFalse(ae._in_silent_window(_row()))

    def test_same_day_window(self):
        rule = self._rule(time(9, 0), time(15, 0))
        with _freeze(datetime(2026, 9, 2, 10, 30)):
            self.assertTrue(ae._in_silent_window(rule))
        with _freeze(datetime(2026, 9, 2, 16, 0)):
            self.assertFalse(ae._in_silent_window(rule))

    def test_cross_midnight_window(self):
        rule = self._rule(time(22, 0), time(8, 0))
        with _freeze(datetime(2026, 9, 2, 23, 30)):
            self.assertTrue(ae._in_silent_window(rule))
        with _freeze(datetime(2026, 9, 3, 3, 0)):
            self.assertTrue(ae._in_silent_window(rule))
        with _freeze(datetime(2026, 9, 3, 12, 0)):
            self.assertFalse(ae._in_silent_window(rule))


class ConditionTest(unittest.TestCase):
    def test_price_above(self):
        rule = _row(typ="price_above", threshold=100)
        met, observed, msg = ae._condition(rule, quote={"price": 120})
        self.assertTrue(met)
        self.assertEqual(observed, 120.0)
        self.assertIn("贵州茅台 价格突破（≥） 100元", msg)
        met, _, _ = ae._condition(rule, quote={"price": 90})
        self.assertFalse(met)

    def test_price_below(self):
        rule = _row(typ="price_below", threshold=100)
        met, observed, _ = ae._condition(rule, quote={"price": 90})
        self.assertTrue(met)
        self.assertEqual(observed, 90.0)
        met, _, _ = ae._condition(rule, quote={"price": 120})
        self.assertFalse(met)

    def test_change_pct_abs(self):
        rule = _row(typ="change_pct", threshold=3)
        for chg in (3.5, -3.5):
            met, observed, _ = ae._condition(rule, quote={"change_pct": chg})
            self.assertTrue(met)
            self.assertEqual(observed, chg)
        met, _, _ = ae._condition(rule, quote={"change_pct": 1.0})
        self.assertFalse(met)

    def test_rsi_oversold(self):
        rule = _row(typ="rsi_oversold", threshold=30)
        met, observed, _ = ae._condition(rule, rsi=25)
        self.assertTrue(met)
        self.assertEqual(observed, 25)
        met, _, _ = ae._condition(rule, rsi=40)
        self.assertFalse(met)

    def test_rsi_overbought(self):
        rule = _row(typ="rsi_overbought", threshold=70)
        met, _, _ = ae._condition(rule, rsi=75)
        self.assertTrue(met)
        met, _, _ = ae._condition(rule, rsi=50)
        self.assertFalse(met)

    def test_signal_change_first_scan_no_baseline(self):
        rule = _row(typ="signal_change", last_signal=None)
        met, _, _ = ae._condition(rule, signal="buy")
        self.assertFalse(met)

    def test_signal_change_differs(self):
        rule = _row(typ="signal_change", last_signal="hold")
        met, observed, msg = ae._condition(rule, signal="buy")
        self.assertTrue(met)
        self.assertIsNone(observed)
        self.assertIn("贵州茅台 信号变化：hold → buy", msg)

    def test_signal_change_same(self):
        rule = _row(typ="signal_change", last_signal="buy")
        met, _, _ = ae._condition(rule, signal="buy")
        self.assertFalse(met)


class ScanAlertsTest(unittest.TestCase):
    """scan_alerts 状态机：mock 数据层与取数，验证事件产出与复位语义。"""

    def setUp(self):
        self.sa_patch = [
            mock.patch.object(ae.sa, "set_alert_triggered"),
            mock.patch.object(ae.sa, "insert_alert_event"),
            mock.patch.object(ae.sa, "rearm_alert"),
            mock.patch.object(ae.sa, "set_alert_baseline"),
        ]
        for p in self.sa_patch:
            p.start()
        self.addCleanup(self._stop)

    def _stop(self):
        for p in self.sa_patch:
            p.stop()

    @mock.patch.object(ae.gateway, "get_quote", return_value={"price": 1320.0})
    @mock.patch.object(ae.sa, "evaluable_alerts",
                       return_value=[_row(threshold=1300)])
    def test_active_price_met_triggers(self, _eval, _quote):
        events = ae.scan_alerts()
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["alert_id"], "1")
        self.assertEqual(ev["symbol"], "600519")
        self.assertEqual(ev["observed"], 1320.0)
        self.assertEqual(ev["channels"], ["in_app"])
        self.assertIn("价格突破", ev["message"])
        self.assertRegex(ev["at"], r"^\d{2}:\d{2}:\d{2}$")
        ae.sa.set_alert_triggered.assert_called_once_with(1, 1320.0, signal=None)
        ae.sa.insert_alert_event.assert_called_once()

    @mock.patch.object(ae.gateway, "get_quote", return_value={"price": 1200.0})
    @mock.patch.object(ae.sa, "evaluable_alerts",
                       return_value=[_row(threshold=1300, status="triggered")])
    def test_triggered_price_not_met_rearms(self, _eval, _quote):
        events = ae.scan_alerts()
        self.assertEqual(events, [])
        ae.sa.rearm_alert.assert_called_once_with(1)
        ae.sa.set_alert_triggered.assert_not_called()

    @mock.patch.object(ae.sa, "evaluable_alerts", return_value=[])
    def test_no_rules_no_events(self, _eval):
        self.assertEqual(ae.scan_alerts(), [])

    @mock.patch.object(ae.gateway, "get_quote", return_value={"price": 1320.0})
    @mock.patch.object(ae.sa, "evaluable_alerts",
                       return_value=[_row(threshold=1300,
                                          silent_from=time(0, 0), silent_to=time(23, 59))])
    def test_silent_window_skips(self, _eval, _quote):
        with _freeze(datetime(2026, 9, 2, 10, 30)):
            self.assertEqual(ae.scan_alerts(), [])
        ae.sa.set_alert_triggered.assert_not_called()

    @mock.patch.object(ae.gateway, "get_kline",
                       return_value=type("Frame", (), {"empty": False})())
    @mock.patch.object(ae.sa, "evaluable_alerts",
                       return_value=[_row(typ="rsi_oversold", threshold=30)])
    def test_rsi_rule_evaluates(self, _eval, _kline):
        with mock.patch.object(ae, "compute_indicators",
                               return_value={"rsi14": [25]}):
            events = ae.scan_alerts()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "rsi_oversold")
        self.assertEqual(events[0]["observed"], 25)

    @mock.patch.object(ae.sa, "evaluable_alerts",
                       return_value=[_row(typ="signal_change", last_signal=None)])
    def test_signal_change_first_scan_archives_baseline(self, _eval):
        skill = mock.Mock()
        skill.get_signal.return_value = {"signal": "buy"}
        with mock.patch("plugins.stock_analysis.stock_skill.StockAnalysisSkill",
                        return_value=skill):
            events = ae.scan_alerts()
        self.assertEqual(events, [])
        ae.sa.set_alert_baseline.assert_called_once_with(1, "buy")
        ae.sa.set_alert_triggered.assert_not_called()

    @mock.patch.object(ae.sa, "evaluable_alerts",
                       return_value=[_row(typ="signal_change", last_signal="hold")])
    def test_signal_change_triggers(self, _eval):
        skill = mock.Mock()
        skill.get_signal.return_value = {"signal": "buy"}
        with mock.patch("plugins.stock_analysis.stock_skill.StockAnalysisSkill",
                        return_value=skill):
            events = ae.scan_alerts()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "signal_change")
        ae.sa.set_alert_triggered.assert_called_once_with(1, None, signal="buy")

    @mock.patch.object(ae.gateway, "get_quote", return_value={})
    @mock.patch.object(ae.sa, "evaluable_alerts",
                       return_value=[_row(typ="price_below", threshold=100)])
    def test_price_below_missing_quote_not_triggered(self, _eval, _quote):
        # DEF-06：行情缺失时本轮跳过，price_below 不得以 0 元误触发
        events = ae.scan_alerts()
        self.assertEqual(events, [])
        ae.sa.set_alert_triggered.assert_not_called()

    @mock.patch.object(ae.gateway, "get_quote", return_value={"price": 0})
    @mock.patch.object(ae.sa, "evaluable_alerts",
                       return_value=[_row(typ="price_below", threshold=100)])
    def test_price_below_zero_price_not_triggered(self, _eval, _quote):
        # DEF-06：价格为 0（停牌/缺失）不视为跌破阈值
        events = ae.scan_alerts()
        self.assertEqual(events, [])
        ae.sa.set_alert_triggered.assert_not_called()

    @mock.patch.object(ae.gateway, "get_quote", return_value={})
    @mock.patch.object(ae.sa, "evaluable_alerts",
                       return_value=[_row(typ="price_below", threshold=100,
                                          status="triggered")])
    def test_price_below_missing_quote_keeps_triggered(self, _eval, _quote):
        # 数据缺失应跳过整条规则：已 triggered 的规则不得因行情缺失被误复位
        events = ae.scan_alerts()
        self.assertEqual(events, [])
        ae.sa.rearm_alert.assert_not_called()


class _FakeLockConn:
    """模拟 advisory lock 连接：try-lock 返回 ok；unlock/close 无副作用。"""

    def __init__(self, ok):
        self._ok = ok
        self.closed = False
        self.unlock_called = False

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return {"ok": self._ok}

    def close(self):
        self.closed = True


class ScheduledScanTest(unittest.TestCase):
    def test_lock_busy_skips(self):
        conn = _FakeLockConn(ok=False)
        with mock.patch("plugins._base.db.get_pooled_connection",
                        return_value=conn):
            result = ae.scheduled_scan()
        self.assertEqual(result, {"skipped": True, "reason": "lock-busy", "events": 0})
        self.assertTrue(conn.closed)

    def test_db_unavailable_degrades(self):
        def _boom():
            raise RuntimeError("pg down")
        with mock.patch("plugins._base.db.get_pooled_connection", side_effect=_boom):
            result = ae.scheduled_scan()
        self.assertEqual(result["skipped"], True)
        self.assertEqual(result["reason"], "db-unavailable")

    def test_scan_runs_when_lock_held(self):
        conn = _FakeLockConn(ok=True)
        with mock.patch("plugins._base.db.get_pooled_connection",
                        return_value=conn), \
             mock.patch.object(ae.sa, "ensure_tables"), \
             mock.patch.object(ae, "scan_alerts",
                               return_value=[{"alert_id": "1"}]):
            result = ae.scheduled_scan()
        self.assertEqual(result, {"skipped": False, "events": 1})
        self.assertTrue(conn.closed)


class LookupNameTest(unittest.TestCase):
    @mock.patch.object(ae.gateway, "get_quote", return_value={"name": "贵州茅台"})
    def test_name_from_quote(self, _q):
        self.assertEqual(ae.lookup_name("600519"), "贵州茅台")

    @mock.patch.object(ae.gateway, "get_quote", side_effect=RuntimeError("boom"))
    def test_lookup_failure_returns_none(self, _q):
        self.assertIsNone(ae.lookup_name("600519"))


if __name__ == "__main__":
    unittest.main()
