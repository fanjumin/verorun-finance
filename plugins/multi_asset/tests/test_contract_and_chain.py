#!/usr/bin/env python3
"""V-04 / V-11 — provider contract conformance, source chains, failover, cooldown.

V-04 (audit P1-3): the adapters must match the *real* ``BaseProviderV2`` contract
    (keyword-only ``symbol``, single-symbol ``_do_fetch``, ``FetchResult`` return,
    declared ``categories``, ``SecretResolver`` injection) instead of the draft's
    invented ``_do_fetch(self, category, symbols, **kwargs)`` with a plural symbol
    list and a fabricated ``required_secret``.
V-11: every asset class must have >= 2 real sources wired into a failover chain.

No network, no DB: providers are faked where the chain is exercised.
"""
import inspect
import types
import unittest

from plugins.multi_asset import adapters
from plugins.multi_asset.adapters import providers as providers_mod
from plugins.multi_asset.asset_data import load_data
from plugins.multi_asset.adapters.base import (CONTRACT_AVAILABLE, DataCategory,
                                               ProviderUnavailable, cats,
                                               make_result, secret_resolver)
from plugins.multi_asset.adapters.providers import (AkshareProvider,
                                                    SaGatewayProvider,
                                                    SinaFinanceProvider,
                                                    normalize_ohlcv)
from plugins.multi_asset.adapters.tushare_ma import TushareMaProvider, _bond_ref_rows


class ContractShapeTest(unittest.TestCase):
    """V-04 — the adapter layer matches the real contract."""

    @unittest.skipUnless(CONTRACT_AVAILABLE, "stock_analysis contract not importable")
    def test_subclasses_derive_from_base_provider_v2(self):
        from plugins.stock_analysis.providers.base_v2 import BaseProviderV2
        for cls in (AkshareProvider, SinaFinanceProvider, SaGatewayProvider,
                    TushareMaProvider):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, BaseProviderV2))

    def test_do_fetch_signature_is_keyword_only_symbol(self):
        """P1-3: keyword-only ``symbol`` (singular), not a positional symbol list."""
        for cls in (AkshareProvider, SinaFinanceProvider, SaGatewayProvider,
                    TushareMaProvider):
            with self.subTest(cls=cls.__name__):
                params = inspect.signature(cls._do_fetch).parameters
                self.assertEqual(list(params)[0], "self")
                self.assertEqual(list(params)[1], "cat")
                self.assertEqual(params["symbol"].kind,
                                 inspect.Parameter.KEYWORD_ONLY)
                self.assertTrue(any(p.kind is inspect.Parameter.VAR_KEYWORD
                                    for p in params.values()))

    def test_categories_declared_not_empty(self):
        """A provider with an empty category set can never pass ``fetch()``."""
        for cls, expected in ((AkshareProvider, {"kline", "quote", "profile"}),
                              (SinaFinanceProvider, {"kline"}),
                              (SaGatewayProvider, {"kline", "quote"}),
                              (TushareMaProvider, {"kline"})):
            with self.subTest(cls=cls.__name__):
                self.assertEqual(set(str(c.value) for c in cls.categories), expected)

    def test_market_is_global_so_secmaster_does_not_narrow(self):
        for cls in (AkshareProvider, SinaFinanceProvider, SaGatewayProvider,
                    TushareMaProvider):
            self.assertEqual(cls.market, "GLOBAL")

    def test_provider_instances_are_constructed_with_a_secret_resolver(self):
        provider = adapters.build_provider("FUTURE", "akshare")
        self.assertEqual(provider.name, "akshare")
        self.assertIsNotNone(provider._secrets)      # never a bare cls()

    def test_make_result_and_secret_resolver_survive_contract_absence(self):
        result = make_result(DataCategory.KLINE, [1], "akshare", url="x")
        self.assertEqual(result.source, "akshare")
        self.assertIsNotNone(secret_resolver())

    def test_cats_helper_returns_members(self):
        got = set(str(c.value) for c in cats("KLINE", "QUOTE"))
        self.assertEqual(got, {"kline", "quote"})


class ChainWiringTest(unittest.TestCase):
    """V-11 — every asset class has a real dual-source chain."""

    def test_every_chain_has_at_least_two_sources(self):
        self.assertTrue(adapters.CHAINS)
        for asset_type, chain in adapters.CHAINS.items():
            with self.subTest(asset_type=asset_type):
                self.assertGreaterEqual(len(chain), 2)
                for name, at in chain:
                    self.assertEqual(at, asset_type)
                    self.assertIn((name, asset_type), adapters.REGISTRY)

    def test_expected_asset_classes_are_covered(self):
        self.assertEqual(set(adapters.CHAINS), {"FUTURE", "OPTION", "FUND", "BOND"})

    def test_chain_sources_reflects_declaration(self):
        self.assertEqual(adapters.chain_sources("FUTURE"),
                         ["tushare", "akshare", "sina"])
        self.assertEqual(adapters.chain_sources("OPTION"),
                         ["tushare", "akshare", "sina"])
        self.assertEqual(adapters.chain_sources("FUND"), ["akshare", "sa_gateway"])
        self.assertEqual(adapters.chain_sources("BOND"),
                         ["tushare", "akshare", "sa_gateway"])

    def test_build_provider_rejects_unknown_pair(self):
        with self.assertRaises(ProviderUnavailable):
            adapters.build_provider("EQUITY", "akshare")

    def test_no_invented_gateway_helper_in_the_plugin(self):
        """P1-1: the invented gateway helper must not exist; the real path is the
        stock_analysis DataGateway singleton. The needle is assembled at runtime so
        this test file does not match itself."""
        import pathlib
        root = pathlib.Path(adapters.__file__).resolve().parents[1]
        blob = "\n".join(p.read_text(encoding="utf-8")
                         for p in root.rglob("*.py")
                         if "tests" not in p.parts)
        invented_helper = "shared_fetch" + "_via_gateway"
        invented_table = "sa" + "_bars"
        self.assertNotIn(invented_helper, blob)
        self.assertNotIn(invented_table, blob)     # P1-2: fabricated table name
        self.assertIn("ma_bars", blob)             # the real, single table name


class _FakeProvider:
    """Minimal duck-typed provider used to drive the failover state machine."""

    def __init__(self, name, *, empty=False, error=None, data=None):
        self.name = name
        self._empty = empty
        self._error = error
        self._data = data if data is not None else [{"close": 1}]
        self.calls = 0

    def fetch(self, cat, *, symbol=None, **kw):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return types.SimpleNamespace(empty=self._empty, data=self._data,
                                     source=self.name)


class FailoverTest(unittest.TestCase):

    def setUp(self):
        self._saved_instances = dict(adapters._INSTANCES)
        self._saved_streak = dict(adapters._FAIL_STREAK)
        self._saved_until = dict(adapters._COOLDOWN_UNTIL)
        adapters._INSTANCES.clear()
        adapters.reset_cooldown()
        self.addCleanup(self._restore)

    def _restore(self):
        adapters._INSTANCES.clear()
        adapters._INSTANCES.update(self._saved_instances)
        adapters._FAIL_STREAK.clear()
        adapters._FAIL_STREAK.update(self._saved_streak)
        adapters._COOLDOWN_UNTIL.clear()
        adapters._COOLDOWN_UNTIL.update(self._saved_until)

    def _wire(self, first, second):
        adapters._INSTANCES[("akshare", "FUTURE")] = first
        adapters._INSTANCES[("sina", "FUTURE")] = second

    def test_falls_over_to_the_second_source(self):
        first = _FakeProvider("akshare", error=ProviderUnavailable("boom"))
        second = _FakeProvider("sina")
        self._wire(first, second)
        result = adapters.fetch("FUTURE", DataCategory.KLINE, "rb2610")
        self.assertEqual(result.source, "sina")
        self.assertEqual((first.calls, second.calls), (1, 1))

    def test_empty_result_counts_as_failure_and_moves_on(self):
        first = _FakeProvider("akshare", empty=True)
        second = _FakeProvider("sina")
        self._wire(first, second)
        self.assertEqual(adapters.fetch("FUTURE", DataCategory.KLINE, "rb2610").source,
                         "sina")

    def test_unexpected_exception_is_treated_as_source_failure(self):
        first = _FakeProvider("akshare", error=RuntimeError("upstream exploded"))
        second = _FakeProvider("sina")
        self._wire(first, second)
        self.assertEqual(adapters.fetch("FUTURE", DataCategory.KLINE, "rb2610").source,
                         "sina")

    def test_whole_chain_failure_raises(self):
        self._wire(_FakeProvider("akshare", error=ProviderUnavailable("a")),
                   _FakeProvider("sina", error=ProviderUnavailable("b")))
        with self.assertRaises(ProviderUnavailable):
            adapters.fetch("FUTURE", DataCategory.KLINE, "rb2610")

    def test_three_consecutive_failures_cool_the_source_down(self):
        first = _FakeProvider("akshare", error=ProviderUnavailable("boom"))
        second = _FakeProvider("sina")
        self._wire(first, second)
        for _ in range(adapters.COOLDOWN_FAIL_STREAK):
            adapters.fetch("FUTURE", DataCategory.KLINE, "rb2610")
        calls_after_cooldown = first.calls
        # The cooled source must be skipped entirely on the next request.
        adapters.fetch("FUTURE", DataCategory.KLINE, "rb2610")
        self.assertEqual(first.calls, calls_after_cooldown)
        state = adapters.cooldown_state()
        self.assertIn("FUTURE/akshare", state["cooldown_until"])

    def test_success_resets_the_fail_streak(self):
        first = _FakeProvider("akshare", error=ProviderUnavailable("boom"))
        second = _FakeProvider("sina")
        self._wire(first, second)
        adapters.fetch("FUTURE", DataCategory.KLINE, "rb2610")
        self.assertEqual(adapters.cooldown_state()["fail_streak"]["FUTURE/akshare"], 1)
        first._error = None
        adapters.fetch("FUTURE", DataCategory.KLINE, "rb2610")
        self.assertEqual(adapters.cooldown_state()["fail_streak"]["FUTURE/akshare"], 0)

    def test_reset_cooldown_clears_state(self):
        self._wire(_FakeProvider("akshare", error=ProviderUnavailable("boom")),
                   _FakeProvider("sina"))
        for _ in range(adapters.COOLDOWN_FAIL_STREAK):
            adapters.fetch("FUTURE", DataCategory.KLINE, "rb2610")
        adapters.reset_cooldown()
        self.assertEqual(adapters.cooldown_state()["fail_streak"], {})
        self.assertEqual(adapters.cooldown_state()["cooldown_until"], {})


class FrequencyHonestyTest(unittest.TestCase):
    """Frequency integrity: never re-label a daily frame as an intraday one."""

    def setUp(self):
        self.provider = AkshareProvider(asset_type="FUTURE")

    def test_daily_weekly_monthly_are_accepted(self):
        for freq in ("daily", "weekly", "monthly"):
            with self.subTest(freq=freq):
                self.assertEqual(providers_mod._guard_freq(self.provider, freq), freq)

    def test_intraday_frequencies_are_refused(self):
        for freq in ("60m", "30m", "15m", "5m", "1m"):
            with self.subTest(freq=freq):
                with self.assertRaises(ProviderUnavailable):
                    providers_mod._guard_freq(self.provider, freq)

    def test_declared_kline_freqs_match_what_is_served(self):
        self.assertEqual(set(AkshareProvider.kline_freqs),
                         {"daily", "weekly", "monthly"})


class TushareSourceTest(unittest.TestCase):
    """P1 — the commercial source is intraday-capable and degrades without a token."""

    def test_declares_intraday_frequencies(self):
        """Tushare heads the chain *because* it can serve minutes (unlike akshare)."""
        for freq in ("1m", "5m", "15m", "30m", "60m", "daily", "weekly", "monthly"):
            with self.subTest(freq=freq):
                self.assertIn(freq, TushareMaProvider.kline_freqs)

    def test_ts_code_carries_the_exchange_suffix(self):
        provider = TushareMaProvider(asset_type="FUTURE")
        self.assertEqual(provider._ts_code("rb2610"), "RB2610.SHF")
        self.assertEqual(provider._ts_code("SA605"), "SA605.CZC")
        self.assertEqual(provider._ts_code("IF2603"), "IF2603.CFX")

    @unittest.skipUnless(CONTRACT_AVAILABLE, "stock_analysis contract not importable")
    def test_unsupported_asset_type_is_refused(self):
        provider = TushareMaProvider(asset_type="FUND")
        with self.assertRaises(ProviderUnavailable):
            provider._do_fetch(DataCategory.KLINE, symbol="rb2610")

    def test_bond_ts_code_carries_the_venue_suffix(self):
        """6-digit exchange bond codes map onto tushare's ``.SH`` / ``.SZ`` suffix."""
        provider = TushareMaProvider(asset_type="BOND")
        self.assertEqual(provider._ts_code("019547"), "019547.SH")
        self.assertEqual(provider._ts_code("112456"), "112456.SZ")

    @unittest.skipUnless(CONTRACT_AVAILABLE, "stock_analysis contract not importable")
    def test_bond_intraday_frequency_is_refused(self):
        """Bonds are daily-only (cb_daily); intraday must never be fabricated."""
        provider = TushareMaProvider(asset_type="BOND")
        with self.assertRaises(ProviderUnavailable):
            provider._do_fetch(DataCategory.KLINE, symbol="019547", freq="60m")


class BondReferenceTest(unittest.TestCase):
    """P2 — ``cb_basic`` records map onto ``ma_bond_ref`` rows (network-free)."""

    def test_ts_code_is_split_into_code_and_mic_exchange(self):
        rows = _bond_ref_rows([
            {"ts_code": "110030.SH", "bond_short_name": "alpha"},
            {"ts_code": "123001.SZ", "bond_short_name": "beta"},
        ])
        self.assertEqual([(r["code"], r["exchange"]) for r in rows],
                         [("110030", "SSE"), ("123001", "SZSE")])
        self.assertEqual(rows[0]["name_norm"], "ALPHA")
        self.assertEqual(rows[0]["source"], "tushare_cb_basic")

    def test_unknown_venue_and_blank_rows_are_skipped(self):
        rows = _bond_ref_rows([
            {"ts_code": "110030.BJ"},      # no bond venue for BJ
            {"ts_code": ""},               # blank code
            {},                            # missing ts_code
        ])
        self.assertEqual(rows, [])

    def test_missing_columns_stay_none_and_issuer_is_never_invented(self):
        rows = _bond_ref_rows([{"ts_code": "110030.SH"}])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIsNone(row["coupon_rate"])
        self.assertIsNone(row["credit_rating"])
        self.assertIsNone(row["convert_price"])
        self.assertIsNone(row["issuer"])

    def test_dates_and_numbers_are_normalised(self):
        rows = _bond_ref_rows([{"ts_code": "110030.SH", "value_date": "20200115",
                                "maturity_date": "20260114", "coupon_rate": 0.5,
                                "cb_type": "CB", "conv_price": 12.34}])
        row = rows[0]
        self.assertEqual(row["issue_date"], "2020-01-15")
        self.assertEqual(row["maturity_date"], "2026-01-14")
        self.assertEqual(row["bond_type"], "CB")
        self.assertAlmostEqual(row["coupon_rate"], 0.5)
        self.assertAlmostEqual(row["convert_price"], 12.34)

    def test_rating_falls_back_to_the_issue_rating_column(self):
        rows = _bond_ref_rows([{"ts_code": "110030.SH", "issue_rating": "AA+"}])
        self.assertEqual(rows[0]["credit_rating"], "AA+")


class NormalizeOhlcvTest(unittest.TestCase):
    """The vendor column map is loaded from data/akshare_cn.json, not hard-coded."""

    def _vendor_columns(self):
        """Invert the shipped column map: canonical name -> vendor column name.

        Taking the keys from ``data/akshare_cn.json`` (instead of writing CJK
        literals in this file) also proves the JSON asset is really wired in — and
        keeps the test file clean for the repo's ``--check-cn`` scan.
        """
        col_map = load_data("akshare_cn.json")["col_map"]
        inverted = {}
        for vendor, canonical in col_map.items():
            inverted.setdefault(canonical, vendor)
        return inverted

    def _frame(self):
        import pandas as pd
        col = self._vendor_columns()
        return pd.DataFrame({
            col["date"]: ["2024-01-02", "2024-01-03", "2024-01-04"],
            col["open"]: [1.0, 2.0, 3.0],
            col["high"]: [2.0, 3.0, 4.0],
            col["low"]: [0.5, 1.5, 2.5],
            col["close"]: [1.5, 2.5, 3.5],
            col["volume"]: [10, 20, 30],
        })

    def test_chinese_columns_are_mapped(self):
        out = normalize_ohlcv(self._frame(), "daily")
        self.assertEqual(list(out.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(out.index.name, "date")
        self.assertEqual(float(out.iloc[0]["close"]), 1.5)

    def test_weekly_resample_is_really_aggregated(self):
        out = normalize_ohlcv(self._frame(), "weekly")
        self.assertEqual(len(out), 1)                      # one trading week
        self.assertEqual(float(out.iloc[0]["close"]), 3.5)  # last close of the week
        self.assertEqual(float(out.iloc[0]["high"]), 4.0)   # max of the week
        self.assertEqual(float(out.iloc[0]["volume"]), 60)  # summed volume

    def test_none_frame_returns_none(self):
        self.assertIsNone(normalize_ohlcv(None, "daily"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
