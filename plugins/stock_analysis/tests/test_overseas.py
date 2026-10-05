# tests/test_overseas.py — 境外市场（美股/港股）接入契约测试（v2.1.0）
#
# 全部为纯函数断言 + mock HTTP 层，不触网、不依赖 Polygon/FMP 凭据：
#   1. market_symbol  A 股 12 样本逐字不变 + 境外五种入口归一为 UID
#   2. resolve_symbol 后缀 / 1~5 位港股裸码（含 9 开头 4 位）/ A 股交易所回归
#   3. 市场门控 supports_category：US→polygon、HK→fmp、CN 源拒绝境外
#   4. FMPProvider._canonical_sym：HK:00700 → 0700.HK（REST 路径形态）
#   5. URL 契约（mock _get_json）：FMP 0700.HK / Polygon AAPL
#   6. gateway.ROUTE：FMP 挂入 KLINE/QUOTE/NEWS 且位于 polygon 之后
#   7. orchestrator market_check 节点：境外符号出境形态回归
#   8. kline_payload：境外帧 vol 列兼容（volume 缺失回退，volume 存在优先）
import unittest
from unittest import mock

import pandas as pd

from plugins.stock_analysis.providers.base import DataCategory
from plugins.stock_analysis.providers.base_v2 import SecretResolver
from plugins.stock_analysis.providers.commons import market_symbol
from plugins.stock_analysis.secmaster import resolve_symbol
from plugins.stock_analysis.providers.fmp_provider import FMPProvider
from plugins.stock_analysis.providers.polygon_provider import PolygonProvider
from plugins.stock_analysis.providers.tencent import TencentProvider
from plugins.stock_analysis.gateway import ROUTE, gateway


class TestMarketSymbol(unittest.TestCase):
    """符号规范化：A 股行为逐字冻结，境外入口统一为 UID。"""

    A_SHARE = {
        "600519": "sh600519", "000001": "sz000001", "300750": "sz300750",
        "688981": "sh688981", "430047": "bj430047", "920002": "bj920002",
        "900901": "sh900901", "510300": "sh510300", "110059": "sz110059",
        "sh600519": "sh600519", "sz000001": "sz000001", "bj920002": "bj920002",
    }
    OVERSEAS = {
        "AAPL": "US:AAPL", "usAAPL": "US:AAPL", "US:AAPL": "US:AAPL",
        "AAPL.US": "US:AAPL",
        "00700": "HK:00700", "0700": "HK:00700", "9988": "HK:09988",
        "hk00700": "HK:00700", "0700.HK": "HK:00700", "HK:00700": "HK:00700",
    }

    def test_a_share_unchanged(self):
        for raw, want in self.A_SHARE.items():
            self.assertEqual(market_symbol(raw), want, f"A-share regression: {raw}")

    def test_overseas_to_uid(self):
        for raw, want in self.OVERSEAS.items():
            self.assertEqual(market_symbol(raw), want, f"overseas mapping: {raw}")

    def test_idempotent(self):
        for raw in ("AAPL", "0700.HK", "hk00700", "HK:00700"):
            once = market_symbol(raw)
            self.assertEqual(market_symbol(once), once)

    def test_blank_input_stripped(self):
        # v2.1.0 起入口 strip()：锁定新行为，防止回退为旧的 "sz  "
        self.assertEqual(market_symbol("  "), "sz")


class TestSecmasterInfer(unittest.TestCase):
    def test_hk_bare_and_suffix(self):
        for raw in ("0700", "00700", "0005", "9988", "9618", "9888", "3690"):
            info = resolve_symbol(raw)
            self.assertEqual((info.market, info.exchange), ("HK", "HKEX"), raw)
        self.assertEqual(resolve_symbol("0700.HK").market, "HK")
        self.assertEqual(resolve_symbol("0005.HK").exchange, "HKEX")

    def test_us(self):
        self.assertEqual(resolve_symbol("AAPL").market, "US")
        self.assertEqual(resolve_symbol("AAPL.US").market, "US")
        self.assertEqual(resolve_symbol("US:AAPL").code, "AAPL")

    def test_cn_exchange_unchanged(self):
        cases = {"600519": ("CN", "SH"), "000001": ("CN", "SZ"),
                 "300750": ("CN", "SZ"), "430047": ("CN", "BJ"),
                 "920002": ("CN", "BJ"), "900901": ("CN", "SH")}
        for raw, want in cases.items():
            info = resolve_symbol(raw)
            self.assertEqual((info.market, info.exchange), want, raw)


class TestMarketGate(unittest.TestCase):
    """市场门控 fail-fast：境外代码不进入 CN 源，HK/US 各归其源。"""

    def setUp(self):
        self.pol = PolygonProvider(secrets=SecretResolver(lambda n: None))
        self.fmp = FMPProvider(secrets=SecretResolver(lambda n: None))
        self.tencent = TencentProvider(secrets=SecretResolver(lambda n: None))

    def test_polygon_us_only(self):
        self.assertTrue(self.pol.supports_category(DataCategory.KLINE, "US:AAPL"))
        self.assertTrue(self.pol.supports_category(DataCategory.QUOTE, "US:AAPL"))
        self.assertFalse(self.pol.supports_category(DataCategory.KLINE, "HK:00700"))

    def test_fmp_hk_and_global(self):
        for cat in (DataCategory.KLINE, DataCategory.QUOTE, DataCategory.NEWS):
            self.assertTrue(self.fmp.supports_category(cat, "HK:00700"), f"fmp HK {cat}")
        self.assertTrue(self.fmp.supports_category(DataCategory.KLINE, "US:AAPL"))

    def test_fmp_rejects_cn_six_digits(self):
        self.assertFalse(self.fmp.supports_category(DataCategory.KLINE, "600519"))

    def test_cn_source_rejects_overseas(self):
        # 腾讯 CN 门控必须拒绝港股个股（其放行仅限带境外前缀的 INDEX）
        self.assertFalse(self.tencent.supports_category(DataCategory.QUOTE, "HK:00700"))


class TestFMPCanonicalSymbol(unittest.TestCase):
    CASES = {
        "HK:00700": "0700.HK", "00700": "0700.HK", "0700.HK": "0700.HK",
        "HK:00005": "0005.HK", "HK:09988": "9988.HK", "HK:00001": "0001.HK",
        "US:AAPL": "AAPL", "AAPL": "AAPL", "600519": "600519", "": "",
    }

    def test_canonical(self):
        for raw, want in self.CASES.items():
            self.assertEqual(FMPProvider._canonical_sym(raw), want, raw)


class TestProviderUrlContract(unittest.TestCase):
    """mock HTTP 层断言真实出网 path/params（不触网）。"""

    def _fmp_provider(self, calls):
        def fake_get_json(self, url, params=None, timeout=20):
            calls.append((url, params or {}))
            if "historical-price-full" in url:
                return {"historical": [
                    {"date": "2026-09-30", "open": 10, "high": 11,
                     "low": 9, "close": 10.5, "volume": 1000}]}
            if "/quote/" in url:
                return [{}]
            if "stock_news" in url:
                return []
            return {}

        p = FMPProvider(secrets=SecretResolver(lambda n: "test-key"))
        self._ctx = mock.patch.object(FMPProvider, "_get_json", fake_get_json)
        self._ctx.start()
        self.addCleanup(self._ctx.stop)
        return p

    def test_fmp_hk_urls(self):
        calls = []
        p = self._fmp_provider(calls)
        p._do_fetch(DataCategory.KLINE, symbol="HK:00700")
        p._do_fetch(DataCategory.QUOTE, symbol="HK:00700")
        p._do_fetch(DataCategory.NEWS, symbol="HK:00005")
        urls = [u for u, _ in calls]
        params = [pr for _, pr in calls]
        self.assertTrue(any(u.endswith("/historical-price-full/0700.HK") for u in urls))
        self.assertTrue(any(u.endswith("/quote/0700.HK") for u in urls))
        self.assertEqual(params[2].get("tickers"), "0005.HK")

    def test_fmp_us_kline_url(self):
        calls = []
        p = self._fmp_provider(calls)
        p._do_fetch(DataCategory.KLINE, symbol="US:AAPL")
        self.assertTrue(calls[0][0].endswith("/historical-price-full/AAPL"))

    def test_polygon_us_kline_url_and_vol_column(self):
        captured = []

        def fake_get_json(self, url, params=None, timeout=20):
            captured.append((url, params or {}))
            return {"results": [{"o": 1, "h": 2, "l": 0.5, "c": 1.5,
                                 "v": 1234567, "t": 1759200000000}]}

        p = PolygonProvider(secrets=SecretResolver(lambda n: "test-key"))
        with mock.patch.object(PolygonProvider, "_get_json", fake_get_json):
            result = p._do_fetch(DataCategory.KLINE, symbol="US:AAPL")
        self.assertIn("v2/aggs/ticker/AAPL/range/1/day", captured[0][0])
        # polygon 帧列名为 vol —— kline_service 必须回退兼容（见 TestKlineVolFallback）
        self.assertEqual(list(result.data.columns),
                         ["open", "high", "low", "close", "vol"])
        self.assertEqual(float(result.data["vol"].iloc[0]), 1234567.0)


class TestGatewayRoute(unittest.TestCase):
    def test_fmp_in_chains(self):
        self.assertIn(FMPProvider, ROUTE[DataCategory.KLINE])
        self.assertIn(FMPProvider, ROUTE[DataCategory.QUOTE])
        self.assertIn(FMPProvider, ROUTE[DataCategory.NEWS])

    def test_polygon_before_fmp(self):
        # 美股由 polygon 主取，fmp 仅备援/接管港股
        chain = ROUTE[DataCategory.KLINE]
        self.assertLess(chain.index(PolygonProvider), chain.index(FMPProvider))


class TestMarketCheckNode(unittest.TestCase):
    """orchestrator market_check 节点（nodes.py:699）出境符号回归。"""

    def _run(self, symbol):
        from orchestrator import nodes
        fake_quote = {"name": "X", "price": 1.0, "change_pct": 0.1, "volume": 1}
        with mock.patch.object(gateway, "get_quote", return_value=fake_quote) as mq:
            out = nodes.handle_market_check(
                {"config": {"symbol": symbol, "metric": "price",
                            "operator": ">", "threshold": 0}}, {})
        self.assertTrue(out.get("success"), out)
        return mq.call_args

    def test_us_symbol(self):
        args, kwargs = self._run("AAPL")
        self.assertEqual(args[0], "US:AAPL")
        self.assertIs(kwargs["category"], DataCategory.QUOTE)

    def test_hk_symbol(self):
        args, kwargs = self._run("0700")
        self.assertEqual(args[0], "HK:00700")
        self.assertIs(kwargs["category"], DataCategory.QUOTE)

    def test_foreign_index_path_intact(self):
        # 境外指数仍走 INDEX/index_symbol，不被个股 UID 分支误伤
        from orchestrator import nodes
        fake_quote = {"name": "HSI", "price": 1.0, "change_pct": 0.1, "volume": 1}
        with mock.patch.object(gateway, "get_quote", return_value=fake_quote) as mq:
            out = nodes.handle_market_check(
                {"config": {"symbol": "hkhsi"}}, {})
        self.assertTrue(out.get("success"), out)
        args, kwargs = mq.call_args
        self.assertEqual(args[0], "hkHSI")
        self.assertIs(kwargs["category"], DataCategory.INDEX)


class TestKlineVolFallback(unittest.TestCase):
    """kline_payload：境外帧只有 vol 列时成交量不得为 null。"""

    @staticmethod
    def _frame(columns):
        idx = pd.date_range("2025-01-01", periods=200, freq="B")
        data = {c: [float(i + 1) for i in range(200)] for c in columns}
        return pd.DataFrame(data, index=idx)

    def test_vol_column_fallback(self):
        from plugins.stock_analysis import kline_service
        frame = self._frame(["open", "high", "low", "close", "vol"])
        with mock.patch.object(gateway, "get_kline", return_value=frame):
            payload = kline_service.kline_payload("HK:00700", period="daily",
                                                  adjust="raw", limit=30)
        bars = payload["data"]["bars"]
        self.assertTrue(bars)
        self.assertIsNotNone(bars[-1]["vol"])
        self.assertEqual(bars[-1]["vol"], 200)

    def test_volume_takes_precedence(self):
        from plugins.stock_analysis import kline_service
        frame = self._frame(["open", "high", "low", "close"])
        frame["volume"] = [111.0] * 200
        frame["vol"] = [222.0] * 200
        with mock.patch.object(gateway, "get_kline", return_value=frame):
            payload = kline_service.kline_payload("600519", period="daily",
                                                  adjust="raw", limit=30)
        self.assertEqual(payload["data"]["bars"][-1]["vol"], 111)


if __name__ == "__main__":
    unittest.main()
