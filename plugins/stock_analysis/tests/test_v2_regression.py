#!/usr/bin/env python3
"""test_v2_regression.py — P0 v2 模块回归测试套件。

覆盖：
1. SecMaster UID 解析（CN/HK/US 市场归一化）
2. FetchResult provenance 生成与新鲜度分级
3. EvidenceBundle 防幻觉校验
4. FinancialQuality TTM + DuPont + Altman Z-Score
5. ValuationModels DCF + reverse DCF + comps
6. Provider 契约（BaseProviderV2 template method）
7. Gateway ROUTE 完整性

运行（需 stock 依赖环境，.stock_deps 在 PYTHONPATH）：
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.stock_analysis.tests.test_v2_regression -v

本地无依赖时可用桩模式验证语法：
    python plugins/stock_analysis/tests/test_v2_regression.py
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, _root)

# ── 桩模块：避免 __init__ 拉入 psycopg2 / flask 整条依赖链 ──
import types as _types

def _stub(name, attrs=None):
    m = sys.modules.setdefault(name, _types.ModuleType(name))
    if attrs:
        for k, v in attrs.items():
            setattr(m, k, v)
    return m

_stub('plugin_manager')
_stub('plugin_manager.base', {'BasePlugin': type('BasePlugin', (), {})})

_flask_attrs = {n: (lambda *a, **kw: None) for n in [
    'Blueprint', 'Response', 'current_app', 'jsonify', 'request',
    'abort', 'g', 'render_template', 'redirect', 'url_for', 'flash',
    'session', 'send_file',
]}
_stub('flask', _flask_attrs)

# 阻止 stock_analysis/__init__.py 执行 routes 导入
_pkg = _stub('plugins.stock_analysis')
_pkg.__path__ = [os.path.join(_root, 'plugins', 'stock_analysis')]


class TestSecMaster(unittest.TestCase):
    """SecMaster UID 解析测试。"""

    def test_resolve_cn(self):
        from plugins.stock_analysis.secmaster import resolve_symbol
        sid = resolve_symbol("000001")
        self.assertIsNotNone(sid)
        self.assertEqual(sid.market, "CN")
        self.assertEqual(sid.code, "000001")
        self.assertEqual(sid.uid, "CN:000001")

    def test_resolve_hk(self):
        from plugins.stock_analysis.secmaster import resolve_symbol
        sid = resolve_symbol("00700")
        self.assertIsNotNone(sid)
        self.assertEqual(sid.market, "HK")
        self.assertEqual(sid.code, "00700")

    def test_resolve_us(self):
        from plugins.stock_analysis.secmaster import resolve_symbol
        sid = resolve_symbol("AAPL")
        self.assertIsNotNone(sid)
        self.assertEqual(sid.market, "US")
        self.assertEqual(sid.code, "AAPL")

    def test_resolve_with_prefix(self):
        from plugins.stock_analysis.secmaster import resolve_symbol
        sid = resolve_symbol("CN:600519")
        self.assertIsNotNone(sid)
        self.assertEqual(sid.market, "CN")
        self.assertEqual(sid.code, "600519")


class TestFetchResult(unittest.TestCase):
    """FetchResult provenance 与新鲜度测试。"""

    def test_provenance_auto_gen(self):
        from plugins.stock_analysis.providers.base_v2 import FetchResult, DataCategory
        r = FetchResult(category=DataCategory.KLINE, data=[], source="test", as_of="2026-01-01")
        self.assertEqual(len(r.provenance_id), 12)
        self.assertTrue(r.provenance_id.isalnum())

    def test_empty_detection(self):
        from plugins.stock_analysis.providers.base_v2 import FetchResult, DataCategory
        r1 = FetchResult(category=DataCategory.KLINE, data=[], source="test", as_of="")
        self.assertTrue(r1.empty)
        r2 = FetchResult(category=DataCategory.KLINE, data=[1, 2], source="test", as_of="")
        self.assertFalse(r2.empty)

    def test_freshness_realtime(self):
        from plugins.stock_analysis.providers.base_v2 import FetchResult, DataCategory
        now = pd.Timestamp.now().isoformat()
        r = FetchResult(category=DataCategory.QUOTE, data={}, source="test",
                        as_of=now, delay_seconds=0)
        self.assertEqual(r.freshness, "realtime")


class TestEvidenceBundle(unittest.TestCase):
    """EvidenceBundle 防幻觉校验测试。"""

    def test_bundle_creation(self):
        from plugins.stock_analysis.evidence_bundle import EvidenceItem, EvidenceBundle
        items = [
            EvidenceItem(key="quote.close", label="收盘价", value=150.0,
                         unit="USD", source="fmp", provenance_id="abc123"),
        ]
        bundle = EvidenceBundle(uid="US:AAPL", items=items)
        self.assertEqual(len(bundle.items), 1)
        self.assertEqual(bundle.uid, "US:AAPL")

    def test_verify_against_evidence(self):
        from plugins.stock_analysis.evidence_bundle import (
            EvidenceItem, EvidenceBundle, verify_against_evidence,
        )
        items = [
            EvidenceItem(key="quote.close", label="收盘价", value=150.0,
                         unit="USD", source="fmp", provenance_id="abc123"),
        ]
        bundle = EvidenceBundle(uid="US:AAPL", items=items)
        report = "收盘价为 150.0 美元，成交量为 1000000。"
        result = verify_against_evidence(report, bundle)
        self.assertIn("clean", result)
        self.assertIn("suspect_count", result)
        self.assertIn("suspects", result)
        self.assertTrue(result["evidence_values"] >= 1)

    # ── 复测 N2/N3 回归锚点（原用例只校验返回键存在、从不校验语义，
    #    故"日期误标"与"两步换算误报"两个缺陷长期无人发现）──

    def test_date_tokens_are_excluded(self):
        """N2：日期/期数 token 不得计入疑似幻觉（否则 clean 对真实研报恒为 False）。"""
        from plugins.stock_analysis.evidence_bundle import (
            EvidenceItem, EvidenceBundle, verify_against_evidence,
        )
        bundle = EvidenceBundle(uid="CN:600519", items=[
            EvidenceItem(key="quote.close", label="收盘价", value=150.0, source="fmp"),
        ])
        report = "报告日期：2026 年 9 月 18 日，数据截至 2025Q3，收盘价 150.0 元。"
        result = verify_against_evidence(report, bundle)
        self.assertEqual(result["suspects"], [])
        self.assertTrue(result["clean"])
        self.assertGreaterEqual(result["excluded_date_tokens"], 3)

    def test_two_step_derivation_is_allowed(self):
        """N3：两值相减后再换算亿（两级推导）不得误报。"""
        from plugins.stock_analysis.evidence_bundle import (
            EvidenceItem, EvidenceBundle, verify_against_evidence,
        )
        bundle = EvidenceBundle(uid="CN:600519", items=[
            EvidenceItem(key="income.revenue_ttm", label="营业收入(TTM)",
                         value=1.395e10, unit="CNY", source="tushare"),
            EvidenceItem(key="income.revenue_prev", label="营业收入(上年同期)",
                         value=5.45e9, unit="CNY", source="tushare"),
        ])
        # (1.395e10 − 5.45e9) / 1e8 = 85.0 亿
        result = verify_against_evidence("同比增加 85.00 亿元。", bundle)
        self.assertEqual(result["suspects"], [])
        self.assertTrue(result["clean"])

    def test_genuine_hallucination_is_still_caught(self):
        """放宽日期剥除与两级派生后，检出能力不得退化（真幻觉仍须被标出）。"""
        from plugins.stock_analysis.evidence_bundle import (
            EvidenceItem, EvidenceBundle, verify_against_evidence,
        )
        bundle = EvidenceBundle(uid="CN:600519", items=[
            EvidenceItem(key="quote.close", label="收盘价", value=150.0, source="fmp"),
        ])
        report = "报告日期：2026 年 9 月 18 日，预计净利润 88888.88 亿元。"
        result = verify_against_evidence(report, bundle)
        self.assertFalse(result["clean"])
        self.assertEqual(result["suspects"], ["88888.88"])


class TestFinancialQuality(unittest.TestCase):
    """FinancialQuality TTM + DuPont + 质量指标测试。"""

    def test_ttm_calculation(self):
        from plugins.stock_analysis.financial_quality import ttm
        df = pd.DataFrame({
            "revenue": [100, 110, 105, 115],
        }, index=["2025Q1", "2025Q2", "2025Q3", "2025Q4"])
        result = ttm(df, "revenue")
        self.assertAlmostEqual(result.iloc[-1], 430.0)

    def test_dupont_five_layer(self):
        from plugins.stock_analysis.financial_quality import dupont
        row = {
            "net_profit": 100, "revenue": 1000, "total_assets": 2000,
            "equity": 800, "ebt": 130, "tax": 30, "ebit": 150,
        }
        result = dupont(row)
        self.assertIn("roe", result)
        self.assertIn("tax_burden", result)
        self.assertIn("interest_burden", result)
        self.assertIn("operating_margin", result)
        self.assertIn("asset_turnover", result)
        # 实现自 2026-09 起把权益乘数键名由 equity_multiplier 改为 leverage（复测 N1-2）
        self.assertIn("leverage", result)
        # 五层乘积须自洽等于 ROE（杜邦恒等式）
        self.assertAlmostEqual(result["check_product"], result["roe"], places=6)

    def test_altman_z_score(self):
        from plugins.stock_analysis.financial_quality import altman_z
        row = {
            "working_capital": 200, "total_assets": 1000,
            "retained_earnings": 300, "ebit": 150,
            "equity": 500, "total_liab": 500,
            "revenue": 2000,
        }
        z = altman_z(row)
        self.assertIsNotNone(z)
        self.assertIsInstance(z, float)
        self.assertTrue(z > 0)


class TestValuationModels(unittest.TestCase):
    """ValuationModels DCF + reverse DCF 测试。"""

    def test_dcf_basic(self):
        from plugins.stock_analysis.valuation_models import dcf_two_stage, DCFInputs
        inp = DCFInputs(
            fcf0=100, growths=[0.15] * 5, wacc=0.10,
            terminal_growth=0.03, shares=100,
        )
        result = dcf_two_stage(inp)
        self.assertIn("value_per_share", result)
        self.assertTrue(result["value_per_share"] > 0)

    def test_reverse_dcf(self):
        from plugins.stock_analysis.valuation_models import reverse_dcf, DCFInputs
        inp = DCFInputs(
            fcf0=100, growths=[0.15] * 5, wacc=0.10,
            terminal_growth=0.03, shares=100,
        )
        result = reverse_dcf(price=50, inp=inp)
        self.assertIn("implied_g", result)
        if not np.isnan(result.get("implied_g", float("nan"))):
            self.assertTrue(-0.10 <= result["implied_g"] <= 0.40)

    def test_comps_table(self):
        from plugins.stock_analysis.valuation_models import comps
        peers = pd.DataFrame({
            "pe": [15, 18, 16, 17, 14],
            "pb": [2.0, 2.5, 2.2, 2.1, 1.9],
            "ev_ebitda": [10, 12, 11, 10.5, 11.5],
        })
        target = {"eps": 3.0, "bps": 20.0, "ebitda": 50.0}
        result = comps(target, peers)
        self.assertIn("pe", result)
        self.assertIn("pb", result)
        self.assertIn("peer_median", result["pe"])
        self.assertIn("implied_value_centre", result["pe"])


class TestProviderContract(unittest.TestCase):
    """BaseProviderV2 契约测试。"""

    def test_fmp_supports_check(self):
        from plugins.stock_analysis.providers.fmp_provider import FMPProvider
        from plugins.stock_analysis.providers.base_v2 import SecretResolver, DataCategory
        p = FMPProvider(secrets=SecretResolver(lambda n: None))
        self.assertTrue(p.supports_category(DataCategory.FUNDAMENTAL, "AAPL"))
        self.assertFalse(p.supports_category(DataCategory.FUNDAMENTAL, "600519"))

    def test_polygon_supports_check(self):
        from plugins.stock_analysis.providers.polygon_provider import PolygonProvider
        from plugins.stock_analysis.providers.base_v2 import SecretResolver, DataCategory
        p = PolygonProvider(secrets=SecretResolver(lambda n: None))
        self.assertTrue(p.supports_category(DataCategory.KLINE, "AAPL"))
        self.assertFalse(p.supports_category(DataCategory.KLINE, "600519"))

    def test_v1_classmethod_supports(self):
        """gateway 通过 `category in cls.supports()` 过滤路由链，
        子类实例方法不得遮蔽此类方法（DEF: supports shadow regression）。"""
        from plugins.stock_analysis.providers.fmp_provider import FMPProvider
        from plugins.stock_analysis.providers.polygon_provider import PolygonProvider
        from plugins.stock_analysis.providers.user_supplied import UserSuppliedProvider
        from plugins.stock_analysis.providers.base import DataCategory
        for cls in (FMPProvider, PolygonProvider, UserSuppliedProvider):
            cats = cls.supports()
            self.assertIsInstance(cats, set, f"{cls.__name__}.supports() must return set")
            self.assertIn(DataCategory.KLINE, cats,
                          f"{cls.__name__} must support KLINE")

    def test_user_supplied_ingest(self):
        import tempfile
        from plugins.stock_analysis.providers.user_supplied import UserSuppliedProvider
        from plugins.stock_analysis.providers.base_v2 import DataCategory
        p = UserSuppliedProvider()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write("date,open,high,low,close,volume\n")
            f.write("2026-01-01,100,105,99,103,1000\n")
            f.write("2026-01-02,103,107,102,106,1200\n")
            path = f.name
        try:
            key = p.ingest(DataCategory.KLINE, "TEST", path)
            self.assertIsInstance(key, str)
            imported = p.list_imported()
            self.assertEqual(len(imported), 1)
        finally:
            os.unlink(path)


class TestGatewayRoute(unittest.TestCase):
    """Gateway ROUTE 完整性测试。"""

    def test_route_has_all_categories(self):
        from plugins.stock_analysis.gateway import ROUTE
        from plugins.stock_analysis.providers.base import DataCategory
        required = {DataCategory.KLINE, DataCategory.QUOTE, DataCategory.FUNDAMENTAL}
        for cat in required:
            self.assertIn(cat, ROUTE, f"{cat.value} missing from ROUTE")

    def test_route_has_v2_providers(self):
        from plugins.stock_analysis.gateway import ROUTE
        from plugins.stock_analysis.providers.base import DataCategory
        from plugins.stock_analysis.providers.fmp_provider import FMPProvider
        from plugins.stock_analysis.providers.polygon_provider import PolygonProvider
        from plugins.stock_analysis.providers.user_supplied import UserSuppliedProvider
        fundamental_chain = ROUTE.get(DataCategory.FUNDAMENTAL, [])
        self.assertIn(FMPProvider, fundamental_chain)
        self.assertIn(UserSuppliedProvider, fundamental_chain)
        kline_chain = ROUTE.get(DataCategory.KLINE, [])
        self.assertIn(PolygonProvider, kline_chain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
