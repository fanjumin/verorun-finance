#!/usr/bin/env python3
"""V-01 — asset identification regression tests (audit P0-1 / P0-2).

These are the negative cases the design draft got wrong; each one is a named
regression so the two P0 defects cannot come back:

  P0-1  the futures parser used ``raw.upper()`` before testing case (so the
        lowercase branch was dead code), mistook any ``T``-prefixed code for CFFEX
        treasury (swallowing CZCE ``TA``), and had a SHFE whitelist of 4 products;
  P0-2  the 6-digit parser had a dead bond branch (``head == '0'`` can never hold
        for a 6-digit code) and was missing the BSE / SZSE-B segments.

No network, no DB: pure symbol logic.
"""
import unittest

from plugins.multi_asset import asset_symbol as sym
from plugins.multi_asset.asset_symbol import (AssetType, Exchange,
                                              contract_expiry, contract_month,
                                              normalize, parse_cn_code,
                                              parse_future_symbol, parse_symbol)


class FuturesParsingTest(unittest.TestCase):
    """P0-1 — case and digit-width discrimination."""

    def test_czce_uppercase_three_digit(self):
        self.assertEqual(parse_future_symbol("SA605"),
                         (AssetType.FUTURE, Exchange.CZCE.value, "SA605"))

    def test_ta_is_czce_not_cffex_treasury(self):
        """Regression: ``startswith('T')`` used to swallow CZCE TA into CFFEX."""
        at, ex, code = parse_future_symbol("TA605")
        self.assertEqual((at, ex, code), (AssetType.FUTURE, Exchange.CZCE.value, "TA605"))

    def test_lowercase_three_digit_is_rejected(self):
        """CZCE 3-digit months are uppercase-only; lowercase must not be guessed."""
        with self.assertRaises(ValueError):
            parse_future_symbol("sa605")

    def test_shfe_lowercase_four_digit(self):
        """Regression: the lowercase branch was unreachable before the fix."""
        self.assertEqual(parse_future_symbol("rb2610"),
                         (AssetType.FUTURE, Exchange.SHFE.value, "rb2610"))

    def test_uppercase_vendor_symbol_falls_back_to_commodity_map(self):
        """Vendor feeds are uppercase; SHFE must not be mistaken for CFFEX."""
        self.assertEqual(parse_future_symbol("RB2610"),
                         (AssetType.FUTURE, Exchange.SHFE.value, "rb2610"))
        self.assertEqual(parse_future_symbol("RB0"),
                         (AssetType.FUTURE, Exchange.SHFE.value, "rb0"))

    def test_dce_and_gfex(self):
        self.assertEqual(parse_future_symbol("m2609"),
                         (AssetType.FUTURE, Exchange.DCE.value, "m2609"))
        self.assertEqual(parse_future_symbol("si2609"),
                         (AssetType.FUTURE, Exchange.GFEX.value, "si2609"))
        self.assertEqual(parse_future_symbol("lc2609"),
                         (AssetType.FUTURE, Exchange.GFEX.value, "lc2609"))

    def test_ine(self):
        self.assertEqual(parse_future_symbol("sc2612"),
                         (AssetType.FUTURE, Exchange.INE.value, "sc2612"))

    def test_cffex_index_and_treasury(self):
        for code in ("IF2603", "IH2603", "IC2603", "IM2603",
                     "T2603", "TF2603", "TS2603", "TL2603"):
            with self.subTest(code=code):
                self.assertEqual(parse_future_symbol(code),
                                 (AssetType.FUTURE, Exchange.CFFEX.value, code))

    def test_cffex_options_are_options(self):
        self.assertEqual(parse_future_symbol("IO2603"),
                         (AssetType.OPTION, Exchange.CFFEX.value, "IO2603"))

    def test_continuous_contracts(self):
        self.assertEqual(parse_future_symbol("SA0"),
                         (AssetType.FUTURE, Exchange.CZCE.value, "SA0"))
        self.assertEqual(parse_future_symbol("IF00"),
                         (AssetType.FUTURE, Exchange.CFFEX.value, "IF00"))

    def test_three_sets_are_disjoint_so_fallback_cannot_misfile(self):
        """The uppercase fallback must never steal a CFFEX/CZCE code."""
        overlap = (sym.CFFEX_PRODUCTS | sym.CFFEX_OPTION_PRODUCTS | sym.CZCE_PRODUCTS)
        overlap = {p.lower() for p in overlap} & set(sym.FUTURES_LOWER)
        self.assertEqual(overlap, set())
        # CZCE does not use 4-digit months -> a 4-digit AP is an error, not a guess.
        with self.assertRaises(ValueError):
            parse_future_symbol("AP2605")

    def test_illegal_month_rejected(self):
        with self.assertRaises(ValueError):
            parse_future_symbol("rb2613")

    def test_variety_map_is_not_a_four_name_whitelist(self):
        """P0-1: the SHFE whitelist used to hold only 4 products."""
        shfe = {p for p, ex in sym.FUTURES_LOWER.items() if ex == "SHFE"}
        self.assertGreaterEqual(len(shfe), 10)
        self.assertGreaterEqual(len(sym.CZCE_PRODUCTS), 20)


class CnCodeParsingTest(unittest.TestCase):
    """P0-2 — 6-digit CN segment table."""

    def test_sse_equity_and_star(self):
        self.assertEqual(parse_cn_code("600519"), (AssetType.EQUITY, "SSE"))
        self.assertEqual(parse_cn_code("688981"), (AssetType.EQUITY, "SSE"))

    def test_szse_equity_and_chinext(self):
        self.assertEqual(parse_cn_code("000001"), (AssetType.EQUITY, "SZSE"))
        self.assertEqual(parse_cn_code("300750"), (AssetType.EQUITY, "SZSE"))

    def test_bse_segments(self):
        """Regression: BSE (43/83/87/88/92) had no branch at all."""
        for code in ("430047", "830799", "871981", "889999", "920819"):
            with self.subTest(code=code):
                self.assertEqual(parse_cn_code(code), (AssetType.EQUITY, "BSE"))

    def test_b_shares_are_equity_not_bond(self):
        """Regression: SZSE B shares (200xxx) were classified as BOND."""
        self.assertEqual(parse_cn_code("900901"), (AssetType.EQUITY, "SSE"))
        self.assertEqual(parse_cn_code("200011"), (AssetType.EQUITY, "SZSE"))

    def test_funds(self):
        self.assertEqual(parse_cn_code("510300"), (AssetType.FUND, "SSE"))
        self.assertEqual(parse_cn_code("159915"), (AssetType.FUND, "SZSE"))
        self.assertEqual(parse_cn_code("160105"), (AssetType.FUND, "SZSE"))
        self.assertEqual(parse_cn_code("180101"), (AssetType.FUND, "SZSE"))

    def test_bonds_are_reachable(self):
        """Regression: the ``head == '0'`` bond branch was dead code."""
        for code in ("019547", "018003", "010107", "110059", "111000", "113050"):
            with self.subTest(code=code):
                self.assertEqual(parse_cn_code(code), (AssetType.BOND, "SSE"))
        for code in ("112345", "123456", "127890", "128145"):
            with self.subTest(code=code):
                self.assertEqual(parse_cn_code(code), (AssetType.BOND, "SZSE"))

    def test_exchange_hint_is_authoritative(self):
        self.assertEqual(parse_cn_code("110059", exchange="SSE"),
                         (AssetType.BOND, "SSE"))
        with self.assertRaises(ValueError):
            parse_cn_code("110059", exchange="SZSE")

    def test_invalid_codes_rejected(self):
        for bad in ("", "12345", "1234567", "abc123", "60051x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_cn_code(bad)


class UniversalParseTest(unittest.TestCase):

    def test_prefixed_codes(self):
        self.assertEqual(normalize("sh600519")["asset_type"], "EQUITY")
        self.assertEqual(normalize("sh600519")["exchange"], "SSE")
        self.assertEqual(normalize("sz159915")["exchange"], "SZSE")
        self.assertEqual(normalize("bj830799")["exchange"], "BSE")

    def test_key_is_stable_and_namespaced(self):
        self.assertEqual(normalize("SA605")["key"], "FUTURE:CZCE:SA605")
        self.assertEqual(normalize("510300")["key"], "FUND:SSE:510300")

    def test_mic_codes(self):
        self.assertEqual(normalize("rb2610")["mic"], "XSGE")
        self.assertEqual(normalize("SA605")["mic"], "XZCE")
        # GFEX official MIC is XGFE (ISO 20022 annex), not the draft's XGEF.
        self.assertEqual(normalize("si2609")["mic"], "XGFE")

    def test_explicit_asset_type_wins(self):
        inst = parse_symbol("510300", asset_type="FUND")
        self.assertEqual(inst.asset_type, AssetType.FUND)

    def test_unknown_symbol_raises_instead_of_guessing(self):
        for bad in ("", "  ", "ZZZ", "hello", "12"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_symbol(bad)


class ContractMathTest(unittest.TestCase):

    def test_contract_month_four_digit(self):
        self.assertEqual(contract_month(AssetType.FUTURE, "CFFEX", "IF2603"), (2026, 3))

    def test_contract_month_czce_single_year_digit(self):
        self.assertEqual(contract_month(AssetType.FUTURE, "CZCE", "SA605"), (2026, 5))

    def test_continuous_contract_has_no_month(self):
        self.assertIsNone(contract_month(AssetType.FUTURE, "SHFE", "rb0"))
        self.assertIsNone(contract_month(AssetType.FUTURE, "CFFEX", "IF00"))

    def test_cffex_expiry_is_a_friday(self):
        expiry = contract_expiry(AssetType.FUTURE, "CFFEX", "IF2603")
        self.assertEqual(expiry.weekday(), 4)
        self.assertEqual(expiry.strftime("%Y-%m-%d"), "2026-03-20")   # 3rd Friday

    def test_treasury_expiry_is_second_friday(self):
        expiry = contract_expiry(AssetType.FUTURE, "CFFEX", "T2603")
        self.assertEqual(expiry.strftime("%Y-%m-%d"), "2026-03-13")

    def test_cfi_letters_follow_iso10962(self):
        self.assertEqual(sym.ASSET_TYPE_CFI[AssetType.EQUITY], "E")
        self.assertEqual(sym.ASSET_TYPE_CFI[AssetType.BOND], "D")
        self.assertEqual(sym.ASSET_TYPE_CFI[AssetType.FUND], "C")
        self.assertEqual(sym.ASSET_TYPE_CFI[AssetType.FUTURE], "F")
        self.assertEqual(sym.ASSET_TYPE_CFI[AssetType.OPTION], "O")


class VarietySearchTest(unittest.TestCase):

    def test_search_by_code(self):
        hits = sym.search_variety("rb")
        self.assertTrue(any(h["product"] == "rb" for h in hits))
        self.assertEqual(hits[0]["exchange"], "SHFE")

    def test_search_by_localized_name(self):
        """The variety search must match the *localized* name from the data asset."""
        zh_name = sym.variety_name("SA", "zh-CN")
        self.assertNotEqual(zh_name, "SA")          # a real name was loaded
        hits = sym.search_variety(zh_name)
        self.assertEqual(hits[0]["product"], "SA")
        self.assertEqual(hits[0]["exchange"], "CZCE")

    def test_empty_query_returns_nothing(self):
        self.assertEqual(sym.search_variety(""), [])

    def test_variety_name_falls_back_to_code(self):
        self.assertEqual(sym.variety_name("ZZZ", "en"), "ZZZ")
        self.assertEqual(sym.variety_name("rb", "en"), "Rebar")


if __name__ == "__main__":
    unittest.main(verbosity=2)
