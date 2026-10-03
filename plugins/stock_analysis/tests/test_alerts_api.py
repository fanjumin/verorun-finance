#!/usr/bin/env python3
"""test_alerts_api.py — D1-c 告警 REST 契约测试（契约 §3 /api/alerts，D1-c 扩展 /api/alerts/events）。

覆盖：GET/POST/DELETE /api/alerts 参数校验、重复 409、未命中 404、
信封结构、401/403 鉴权、429 限流；GET /api/alerts/events 过滤与 limit 边界。

运行（需 stock 依赖环境，.stock_deps 在 PYTHONPATH）：
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.stock_analysis.tests.test_alerts_api -v

说明：_require_admin + _require_perm（_perm_required 用）/ _rate_limit / 数据层（_sa）
全部 mock，不连库不鉴真 token。
"""
import json
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

from flask import Flask

from plugins.stock_analysis.routes import stock_analysis_bp

_PAYLOAD = {"sub": 1, "is_admin": True}
_AUTH = {"Authorization": "Bearer test"}


class FakeSA:
    """_sa() 数据层替身：内存行为，行为开关经实例属性控制。"""

    def __init__(self):
        self.duplicate = False
        self.deleted = True
        self.created_id = 7
        self._rule = {
            "id": 7, "symbol": "600519", "name": "贵州茅台", "type": "price_above",
            "threshold": Decimal("1300"), "channel": "in_app", "status": "active",
            "silent_from": None, "silent_to": None,
            "last_triggered_at": None, "created_at": "2026-09-02 09:00:00",
        }
        self._events = [{
            "id": 1, "alert_id": 7, "symbol": "600519", "type": "price_above",
            "threshold": Decimal("1300"), "observed": Decimal("1320"),
            "message": "贵州茅台 价格突破（≥） 1300元",
            "created_at": "2026-09-02 10:30:00",
        }]

    def list_alerts(self, symbol=None):
        if symbol and symbol != self._rule["symbol"]:
            return []
        return [self._rule]

    def duplicate_active_alert(self, symbol, alert_type, threshold):
        return self.duplicate

    def create_alert(self, symbol, name, alert_type, threshold,
                     channel, silent_from, silent_to):
        return self.created_id

    def get_alert_row(self, alert_id):
        return self._rule

    def delete_alert(self, alert_id):
        return self.deleted

    def list_alert_events(self, alert_id=None, limit=50):
        return self._events


class AlertsApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.register_blueprint(stock_analysis_bp)
        cls.client = cls.app.test_client()

    def setUp(self):
        self.fake_sa = FakeSA()
        self.patches = [
            # 路由已从 _admin_required 切到 _perm_required，两个鉴权入口都要 mock：
            # 只 mock 旧入口会让装饰器真的去 import services.jwt_service（测试环境
            # 没有 admin/app.py 的 sys.path）→ 每请求 500，整套 24 例误红。
            mock.patch("plugins.stock_analysis.routes._require_admin",
                       return_value=(_PAYLOAD, None)),
            mock.patch("plugins.stock_analysis.routes._require_perm",
                       return_value=(_PAYLOAD, None)),
            mock.patch("plugins.stock_analysis.routes._rate_limit",
                       return_value=True),
            mock.patch("plugins.stock_analysis.routes._sa",
                       return_value=self.fake_sa),
            mock.patch("plugins.stock_analysis.routes._lookup_alert_name",
                       return_value="贵州茅台"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self._stop)

    def _stop(self):
        for p in self.patches:
            p.stop()

    # ── 信封与鉴权 ──

    def test_envelope_shape(self):
        resp = self.client.get("/admin/stock-analysis/api/alerts", headers=_AUTH)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(set(body.keys()), {"ok", "data", "error", "meta"})
        self.assertTrue(body["ok"])
        self.assertIsNone(body["error"])
        self.assertIn("generated_at", body["meta"])

    def test_unauthorized_401(self):
        with mock.patch("plugins.stock_analysis.routes._require_admin",
                        return_value=(None, "Unauthorized")), \
                mock.patch("plugins.stock_analysis.routes._require_perm",
                           return_value=(None, "Unauthorized")):
            resp = self.client.get("/admin/stock-analysis/api/alerts")
        self.assertEqual(resp.status_code, 401)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "Unauthorized")

    def test_forbidden_403(self):
        with mock.patch("plugins.stock_analysis.routes._require_admin",
                        return_value=({"sub": 1}, "Forbidden")), \
                mock.patch("plugins.stock_analysis.routes._require_perm",
                           return_value=({"sub": 1}, "Forbidden")):
            resp = self.client.get("/admin/stock-analysis/api/alerts")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "Forbidden")

    def test_rate_limited_429(self):
        with mock.patch("plugins.stock_analysis.routes._rate_limit",
                        return_value=False):
            resp = self.client.post("/admin/stock-analysis/api/alerts",
                                    json={"symbol": "600519", "type": "price_above",
                                          "threshold": 1300},
                                    headers=_AUTH)
        self.assertEqual(resp.status_code, 429)

    # ── GET /api/alerts ──

    def test_list_returns_serialized_rules(self):
        resp = self.client.get("/admin/stock-analysis/api/alerts", headers=_AUTH)
        self.assertEqual(resp.status_code, 200)
        alerts = resp.get_json()["data"]["alerts"]
        self.assertEqual(len(alerts), 1)
        rule = alerts[0]
        self.assertEqual(rule["id"], "7")
        self.assertEqual(rule["threshold"], 1300.0)
        self.assertEqual(rule["type"], "price_above")

    def test_list_symbol_filter_no_hit(self):
        resp = self.client.get("/admin/stock-analysis/api/alerts?symbol=000858",
                               headers=_AUTH)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["data"]["alerts"], [])

    # ── POST /api/alerts ──

    def _post(self, payload):
        return self.client.post("/admin/stock-analysis/api/alerts", json=payload,
                                headers=_AUTH)

    def test_create_valid(self):
        resp = self._post({"symbol": "600519", "type": "price_above",
                           "threshold": 1300})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["data"]["alert"]["id"], "7")
        self.assertEqual(body["data"]["alert"]["symbol"], "600519")
        self.fake_sa.created_id = 7
        self.assertEqual(self.fake_sa.created_id, 7)

    def test_create_signal_change_without_threshold(self):
        resp = self._post({"symbol": "600519", "type": "signal_change"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_create_duplicate_409(self):
        self.fake_sa.duplicate = True
        resp = self._post({"symbol": "600519", "type": "price_above",
                           "threshold": 1300})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already exists", resp.get_json()["error"])

    def test_create_bad_symbol_400(self):
        resp = self._post({"symbol": "", "type": "price_above", "threshold": 1300})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "symbol is required")

    def test_create_unsupported_type_400(self):
        resp = self._post({"symbol": "600519", "type": "moon_phase",
                           "threshold": 1})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "unsupported alert type")

    def test_create_missing_threshold_400(self):
        resp = self._post({"symbol": "600519", "type": "price_above"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "threshold is required")

    def test_create_non_numeric_threshold_400(self):
        resp = self._post({"symbol": "600519", "type": "price_above",
                           "threshold": "abc"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "threshold must be a number")

    def test_create_bad_channel_400(self):
        resp = self._post({"symbol": "600519", "type": "price_above",
                           "threshold": 1300, "channel": "wechat"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("channel must be one of", resp.get_json()["error"])

    def test_create_silent_pair_required(self):
        resp = self._post({"symbol": "600519", "type": "price_above",
                           "threshold": 1300, "silent_from": "09:00"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"],
                         "silent_from and silent_to must be set together")

    def test_create_bad_silent_time_400(self):
        resp = self._post({"symbol": "600519", "type": "price_above",
                           "threshold": 1300, "silent_from": "25:00",
                           "silent_to": "15:00"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "silent_from must be HH:MM")

    # ── DELETE /api/alerts ──

    def test_delete_ok(self):
        resp = self.client.delete("/admin/stock-analysis/api/alerts?id=7",
                                  headers=_AUTH)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["data"]["deleted"])
        self.assertEqual(body["data"]["id"], "7")

    def test_delete_missing_id_400(self):
        resp = self.client.delete("/admin/stock-analysis/api/alerts?id=",
                                  headers=_AUTH)
        self.assertEqual(resp.status_code, 400)

    def test_delete_not_found_404(self):
        self.fake_sa.deleted = False
        resp = self.client.delete("/admin/stock-analysis/api/alerts?id=999",
                                  headers=_AUTH)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "alert not found")

    # ── GET /api/alerts/events（D1-c 扩展）──

    def test_events_list(self):
        resp = self.client.get("/admin/stock-analysis/api/alerts/events",
                               headers=_AUTH)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["data"]["events"]), 1)
        ev = body["data"]["events"][0]
        self.assertEqual(ev["alert_id"], 7)
        self.assertEqual(ev["symbol"], "600519")
        self.assertIn("价格突破", ev["message"])

    def test_events_alert_id_filter(self):
        resp = self.client.get("/admin/stock-analysis/api/alerts/events?alert_id=7",
                               headers=_AUTH)
        self.assertEqual(resp.status_code, 200)
        self.fake_sa._events[0]["alert_id"] = 7
        self.assertTrue(resp.get_json()["data"]["events"])

    def test_events_bad_alert_id_400(self):
        resp = self.client.get("/admin/stock-analysis/api/alerts/events?alert_id=abc",
                               headers=_AUTH)
        self.assertEqual(resp.status_code, 400)

    def test_events_limit_bounds(self):
        for qs in ("limit=0", "limit=201", "limit=abc"):
            resp = self.client.get(
                "/admin/stock-analysis/api/alerts/events?" + qs, headers=_AUTH)
            self.assertEqual(resp.status_code, 400, msg=qs)

    def test_events_limit_ok(self):
        resp = self.client.get("/admin/stock-analysis/api/alerts/events?limit=5",
                               headers=_AUTH)
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
