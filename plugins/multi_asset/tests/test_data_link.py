#!/usr/bin/env python3
"""V-02 / V-03 — orchestration contract of ``data_link``.

The review's remaining structural risks live here:

  * the payload handed to the embedded page must be JSON-safe and must actually
    carry the series (the page renders ``data.bars``);
  * persistence and audit logging are **best-effort**: a storage failure must not
    turn a successful fetch into an error;
  * a provider failure must surface as ``ProviderUnavailable`` (never as empty data)
    and must be recorded in the fetch log.

Everything is mocked: no network, no DB.
"""
import unittest
from datetime import date, datetime  # noqa: F401
from unittest import mock

from plugins.multi_asset import data_link
from plugins.multi_asset.adapters.base import DataCategory, ProviderUnavailable


def _frame():
    import pandas as pd
    idx = pd.to_datetime(["2026-03-04", "2026-03-05", "2026-03-06"])
    return pd.DataFrame({"open": [1.0, 2.0, 3.0], "high": [2.0, 3.0, 4.0],
                         "low": [0.5, 1.5, 2.5], "close": [1.5, 2.5, 3.5],
                         "volume": [10, 20, 30]}, index=idx)


class _Result:
    def __init__(self, data, source="akshare"):
        self.data = data
        self.source = source
        self.as_of = "2026-03-06T15:00:00+08:00"
        self.provenance_id = "abc123"
        self.warnings = []

    @property
    def empty(self):
        return self.data is None or len(self.data) == 0


class DataLinkTest(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(data_link.models, "upsert_bars", return_value=3)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(data_link.models, "record_fetch")
        patcher.start()
        self.addCleanup(patcher.stop)
        ready = mock.patch.object(data_link, "models_ready", return_value=True)
        ready.start()
        self.addCleanup(ready.stop)

    def _patch_fetch(self, data, source="akshare"):
        return mock.patch.object(data_link.adapters, "fetch",
                                 return_value=_Result(data, source))

    def test_payload_shape_and_provenance(self):
        with self._patch_fetch(_frame()):
            payload = data_link.fetch_bars("SA605", asset_type="FUTURE")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["asset_type"], "FUTURE")
        self.assertEqual(payload["code"], "SA605")
        self.assertEqual(payload["exchange"], "CZCE")
        self.assertEqual(payload["freq"], "daily")
        self.assertEqual(payload["rows"], 3)
        self.assertEqual(payload["source"], "akshare")
        self.assertEqual(payload["provenance_id"], "abc123")
        self.assertEqual(payload["as_of"], "2026-03-06T15:00:00+08:00")

    def test_bars_series_is_json_safe_and_oldest_first(self):
        import json
        with self._patch_fetch(_frame()):
            payload = data_link.fetch_bars("SA605", asset_type="FUTURE")
        bars = payload["bars"]
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[0]["trade_date"], "2026-03-04")
        self.assertEqual(bars[-1]["trade_date"], "2026-03-06")
        self.assertEqual(bars[-1]["bar_time"], "00:00")
        self.assertEqual(bars[-1]["source"], "akshare")
        self.assertEqual(bars[-1]["value"], None)      # value only for FUND/BOND
        json.dumps(payload)                            # must not raise

    def test_fund_rows_carry_the_nav_value(self):
        with self._patch_fetch(_frame()):
            payload = data_link.fetch_bars("159915", asset_type="FUND")
        self.assertEqual(payload["bars"][-1]["value"], 3.5)

    def test_trade_date_range_is_reported_as_iso_strings(self):
        with self._patch_fetch(_frame()):
            payload = data_link.fetch_bars("SA605", asset_type="FUTURE")
        self.assertEqual(payload["trade_date_from"], "2026-03-04")
        self.assertEqual(payload["trade_date_to"], "2026-03-06")

    def test_written_count_is_reported(self):
        with self._patch_fetch(_frame()):
            payload = data_link.fetch_bars("SA605", asset_type="FUTURE")
        self.assertEqual(payload["written"], 3)

    def test_persist_failure_does_not_fail_the_fetch(self):
        with self._patch_fetch(_frame()):
            with mock.patch.object(data_link.models, "upsert_bars",
                                   side_effect=RuntimeError("db down")):
                payload = data_link.fetch_bars("SA605", asset_type="FUTURE")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["written"], 0)
        self.assertIn("persist_failed", payload["warnings"])

    def test_persist_is_skipped_when_disabled(self):
        with self._patch_fetch(_frame()):
            payload = data_link.fetch_bars("SA605", asset_type="FUTURE",
                                           persist=False)
        self.assertEqual(payload["written"], 0)

    def test_storage_unavailable_is_reported_not_raised(self):
        with mock.patch.object(data_link, "models_ready", return_value=False):
            with self._patch_fetch(_frame()):
                payload = data_link.fetch_bars("SA605", asset_type="FUTURE")
        self.assertIn("storage_unavailable", payload["warnings"])
        self.assertTrue(payload["ok"])

    def test_provider_failure_propagates_and_is_logged(self):
        with mock.patch.object(data_link.adapters, "fetch",
                               side_effect=ProviderUnavailable("all sources failed")):
            with self.assertRaises(ProviderUnavailable):
                data_link.fetch_bars("SA605", asset_type="FUTURE")
        self.assertTrue(data_link.models.record_fetch.called)
        kwargs = data_link.models.record_fetch.call_args[1]
        self.assertFalse(kwargs.get("ok", True))

    def test_invalid_symbol_raises_before_any_fetch(self):
        with mock.patch.object(data_link.adapters, "fetch") as fetch:
            with self.assertRaises(ValueError):
                data_link.fetch_bars("not-a-symbol")
        self.assertFalse(fetch.called)

    def test_quote_payload(self):
        with self._patch_fetch({"close": 3020.0}):
            payload = data_link.fetch_quote("SA605", asset_type="FUTURE")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["asset_type"], "FUTURE")
        self.assertEqual(payload["data"]["close"], 3020.0)

    def test_profile_payload(self):
        with self._patch_fetch({"name": "Soda Ash"}):
            payload = data_link.fetch_profile("SA605", asset_type="FUTURE")
        self.assertEqual(payload["data"]["name"], "Soda Ash")

    def test_intraday_attribution_uses_the_trade_calendar(self):
        """An intraday stamp must be attributed via trade_calendar, not the raw date."""
        import pandas as pd
        idx = pd.to_datetime(["2026-03-06 21:30:00"])
        frame = pd.DataFrame({"close": [3.5]}, index=idx)
        with self._patch_fetch(frame):
            payload = data_link.fetch_bars("rb2610", asset_type="FUTURE",
                                           freq="30m", persist=False)
        # 2026-03-06 is a Friday -> a 21:30 SHFE trade belongs to Monday 03-09.
        self.assertEqual(payload["bars"][0]["trade_date"], "2026-03-09")
        self.assertEqual(payload["bars"][0]["bar_time"], "21:30")

    def test_resolve_helper(self):
        self.assertEqual(data_link.resolve("rb2610")["key"], "FUTURE:SHFE:rb2610")

    def test_source_order_matches_the_chains(self):
        self.assertEqual(data_link.SOURCE_ORDER["FUTURE"],
                         ["tushare", "akshare", "sina"])

    def test_gateway_available_is_a_boolean(self):
        self.assertIsInstance(data_link.gateway_available(), bool)

    def test_free_source_rows_stay_untagged(self):
        """Free sources carry no license/origin (NULL) rather than a guessed one."""
        with self._patch_fetch(_frame(), source="akshare"):
            payload = data_link.fetch_bars("SA605", asset_type="FUTURE")
        row = payload["bars"][-1]
        self.assertIsNone(row.get("license_id"))
        self.assertIsNone(row.get("origin"))
        self.assertIsNone(row.get("dataset_id"))

    def test_vendor_source_rows_inherit_the_license_tags(self):
        with mock.patch.object(data_link.models, "ensure_license") as lic, \
             mock.patch.object(data_link.models, "ensure_dataset_registry") as ds:
            with self._patch_fetch(_frame(), source="tushare"):
                payload = data_link.fetch_bars("rb2610", asset_type="FUTURE")
        row = payload["bars"][-1]
        self.assertEqual(row["license_id"], "vendor:tushare")
        self.assertEqual(row["origin"], "vendor")
        self.assertEqual(row["dataset_id"], "ma:tushare:FUTURE")
        self.assertTrue(lic.called)
        self.assertTrue(ds.called)

    def test_governance_registration_failure_does_not_break_the_fetch(self):
        with mock.patch.object(data_link.models, "ensure_license",
                               side_effect=RuntimeError("db down")), \
             self._patch_fetch(_frame(), source="tushare"):
            payload = data_link.fetch_bars("rb2610", asset_type="FUTURE")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["written"], 3)


class EgressGateTest(unittest.TestCase):
    """Fail-closed egress gate over ma_license (GB/T 42775-2023)."""

    def setUp(self):
        patcher = mock.patch.object(data_link.models, "record_fetch")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _with_licences(self, mapping):
        return mock.patch.object(data_link.models, "get_licenses",
                                 return_value=mapping)

    def test_untagged_rows_pass(self):
        decision = data_link.check_egress("export", [])
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "untagged")

    def test_unknown_purpose_is_rejected(self):
        with self.assertRaises(ValueError):
            data_link.check_egress("publish", ["vendor:tushare"])

    def test_granted_permission_allows_egress(self):
        with self._with_licences({"vendor:tushare": {"allow_export": 1}}):
            decision = data_link.check_egress("export", ["vendor:tushare"])
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "ok")

    def test_registered_but_not_permitted_is_denied(self):
        with self._with_licences({"vendor:tushare": {"allow_export": 0}}):
            decision = data_link.check_egress("export", ["vendor:tushare"])
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["denied"], ["vendor:tushare"])
        self.assertEqual(decision["reason"], "not_permitted")

    def test_unregistered_license_is_denied_closed(self):
        with self._with_licences({}):
            decision = data_link.check_egress("llm", ["vendor:unknown"])
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["missing"], ["vendor:unknown"])
        self.assertEqual(decision["reason"], "license_not_registered")

    def test_unreadable_registry_is_denied_closed(self):
        with mock.patch.object(data_link.models, "get_licenses",
                               side_effect=RuntimeError("db down")):
            decision = data_link.check_egress("forward", ["vendor:tushare"])
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "registry_unavailable")

    def test_denial_is_written_to_the_fetch_log(self):
        with self._with_licences({"vendor:tushare": {"allow_llm": 0}}), \
             mock.patch.object(data_link.models, "record_fetch") as log:
            decision = data_link.guard_egress(
                "llm", ["vendor:tushare"], asset_type="BOND",
                symbol="110030", source="tushare")
        self.assertFalse(decision["allowed"])
        self.assertTrue(log.called)
        self.assertFalse(log.call_args.kwargs["ok"])
        self.assertIn("egress_denied:llm:not_permitted",
                      log.call_args.kwargs["warning"])

    def test_allowed_egress_writes_nothing(self):
        with self._with_licences({"vendor:tushare": {"allow_export": 1}}), \
             mock.patch.object(data_link.models, "record_fetch") as log:
            decision = data_link.guard_egress("export", ["vendor:tushare"])
        self.assertTrue(decision["allowed"])
        self.assertFalse(log.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
