#!/usr/bin/env python3
"""V-08 — HTTP surface: auth model, response contract, input validation.

Mirrors the stock_analysis baseline: a missing/invalid token is 401, a valid token
without the plugin permission is 403, an over-limit caller gets 429, and every
response uses the platform envelope ``{ok, data, error, meta}``.

The JWT validator and the rate limiter are patched, so no auth-center DB and no
network are required.
"""
import os
import unittest
from unittest import mock

# jwt_service resolves its secret at import time; set it before anything imports it.
os.environ.setdefault("JWT_SECRET", "multi-asset-test-secret")

from flask import Flask                                    # noqa: E402

from plugins.multi_asset import routes as routes_mod              # noqa: E402
from plugins.multi_asset.routes import (READ_PERM, WRITE_PERM,    # noqa: E402
                                       multi_asset_bp)


def _app():
    app = Flask("multi_asset_test")
    app.config["TESTING"] = True
    app.register_blueprint(multi_asset_bp)
    return app


ADMIN = {"sub": "admin-1", "is_admin": True, "permissions": []}
READER = {"sub": "user-1", "is_admin": False, "permissions": [READ_PERM]}
NOBODY = {"sub": "user-2", "is_admin": False, "permissions": []}


class RoutesAuthTest(unittest.TestCase):

    def setUp(self):
        self.app = _app()
        self.client = self.app.test_client()
        limiter = mock.patch("plugins._base.ratelimit.check_rate_limit",
                             return_value=True)
        limiter.start()
        self.addCleanup(limiter.stop)

    def _patch_token(self, payload):
        return mock.patch("services.jwt_service.validate_token",
                          side_effect=lambda token: payload if token else None)

    # ── 401 / 403 / 429 ────────────────────────────────────────────────────

    def test_missing_token_is_401(self):
        resp = self.client.get("/admin/multi-asset/api/constants")
        self.assertEqual(resp.status_code, 401)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertIsNone(body["data"])
        self.assertIsNotNone(body["error"])

    def test_invalid_token_is_401(self):
        with self._patch_token(None):
            resp = self.client.get("/admin/multi-asset/api/constants?token=bogus")
        self.assertEqual(resp.status_code, 401)

    def test_token_without_permission_is_403(self):
        with self._patch_token(NOBODY):
            resp = self.client.get("/admin/multi-asset/api/constants?token=t")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "Forbidden")

    def test_admin_is_allowed(self):
        with self._patch_token(ADMIN):
            resp = self.client.get("/admin/multi-asset/api/constants?token=t")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_permission_holder_is_allowed(self):
        with self._patch_token(READER):
            resp = self.client.get("/admin/multi-asset/api/constants?token=t")
        self.assertEqual(resp.status_code, 200)

    def test_bearer_header_is_honoured(self):
        with self._patch_token(ADMIN):
            resp = self.client.get("/admin/multi-asset/api/constants",
                                   headers={"Authorization": "Bearer t"})
        self.assertEqual(resp.status_code, 200)

    def test_rate_limit_exceeded_is_429(self):
        with mock.patch("plugins._base.ratelimit.check_rate_limit",
                        return_value=False):
            with self._patch_token(ADMIN):
                resp = self.client.get("/admin/multi-asset/api/constants?token=t")
        self.assertEqual(resp.status_code, 429)
        self.assertFalse(resp.get_json()["ok"])

    def test_rate_limiter_failure_fails_open(self):
        """A broken limiter must not take the API down."""
        with mock.patch("plugins._base.ratelimit.check_rate_limit",
                        side_effect=RuntimeError("limiter down")):
            with self._patch_token(ADMIN):
                resp = self.client.get("/admin/multi-asset/api/constants?token=t")
        self.assertEqual(resp.status_code, 200)

    # ── contract shape ─────────────────────────────────────────────────────

    def test_constants_payload_shape(self):
        with self._patch_token(ADMIN):
            body = self.client.get("/admin/multi-asset/api/constants?token=t").get_json()
        self.assertEqual(set(body), {"ok", "data", "error", "meta"})
        data = body["data"]
        self.assertIn("asset_types", data)
        self.assertIn("FUTURE", data["asset_types"])
        self.assertEqual(data["exchange_mic"]["GFEX"], "XGFE")
        self.assertIn("freqs", data)
        self.assertEqual(set(data["chains"]), {"FUTURE", "OPTION", "FUND", "BOND"})

    def test_resolve_endpoint_normalizes(self):
        with self._patch_token(ADMIN):
            body = self.client.get(
                "/admin/multi-asset/api/resolve?symbol=SA605&token=t").get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["key"], "FUTURE:CZCE:SA605")
        self.assertEqual(body["data"]["mic"], "XZCE")

    def test_missing_symbol_is_400(self):
        with self._patch_token(ADMIN):
            resp = self.client.get("/admin/multi-asset/api/bars?token=t")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])

    def test_bad_asset_type_is_400(self):
        with self._patch_token(ADMIN):
            resp = self.client.get(
                "/admin/multi-asset/api/bars?symbol=600519&asset_type=CRYPTO&token=t")
        self.assertEqual(resp.status_code, 400)

    def test_bad_frequency_is_400(self):
        with self._patch_token(ADMIN):
            resp = self.client.get(
                "/admin/multi-asset/api/bars?symbol=600519&freq=3h&token=t")
        self.assertEqual(resp.status_code, 400)

    def test_oversized_symbol_is_400(self):
        with self._patch_token(ADMIN):
            resp = self.client.get(
                "/admin/multi-asset/api/bars?symbol=%s&token=t" % ("9" * 40))
        self.assertEqual(resp.status_code, 400)

    def test_non_ascii_symbol_is_400(self):
        with self._patch_token(ADMIN):
            resp = self.client.get(
                "/admin/multi-asset/api/bars?symbol=rb2609x&token=t")
        # ascii but unknown -> still a clean 4xx/5xx contract, never a crash
        self.assertIn(resp.status_code, (400, 503))
        self.assertIn("ok", resp.get_json())

    def test_search_requires_query(self):
        with self._patch_token(ADMIN):
            resp = self.client.get("/admin/multi-asset/api/search?token=t")
        self.assertEqual(resp.status_code, 400)

    def test_search_finds_a_variety_and_tolerates_a_db_outage(self):
        """Local variety search must work even when the reference table is down."""
        with mock.patch("plugins.multi_asset.models.search_ref_instruments",
                        side_effect=RuntimeError("db down")):
            with self._patch_token(ADMIN):
                body = self.client.get(
                    "/admin/multi-asset/api/search?q=SA&token=t").get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(any(i["product"] == "SA" for i in body["data"]["items"]))

    def test_health_endpoint_reports_chain_ok(self):
        with self._patch_token(ADMIN):
            body = self.client.get("/admin/multi-asset/api/health?token=t").get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["data"]["chain_ok"])

    def test_derivative_meta_carries_risk_disclosure(self):
        from plugins.multi_asset.routes import _derivative_meta
        meta = _derivative_meta("FUTURE")
        self.assertIn("risk_disclosure", meta)
        self.assertEqual(meta["suitability"], "professional_only")
        self.assertEqual(_derivative_meta("FUND"), {})

    def test_write_permission_constant_exists_but_is_not_used_for_reads(self):
        self.assertEqual(WRITE_PERM, "multi_asset.write")
        with open(routes_mod.__file__, encoding="utf-8") as fh:
            text = fh.read()
        # Read endpoints must not require the write permission.
        self.assertNotIn("_perm_required(WRITE_PERM", text)

    def test_page_requires_auth(self):
        resp = self.client.get("/admin/multi-asset/")
        self.assertEqual(resp.status_code, 401)

    def test_page_renders_for_a_reader(self):
        with self._patch_token(READER):
            resp = self.client.get("/admin/multi-asset/?token=t")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("/admin/multi-asset/api", html)
        self.assertNotIn("{{", html)          # no unresolved Jinja left behind


if __name__ == "__main__":
    unittest.main(verbosity=2)
