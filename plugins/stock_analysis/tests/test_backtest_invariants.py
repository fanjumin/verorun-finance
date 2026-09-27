# plugins/stock_analysis/tests/test_backtest_invariants.py
"""回测/因子不变量测试（补 D6 盲区：这些模块此前零测试）。

这些断言的作用不是"覆盖率"，而是把 S0-1/S0-2/S1-3 三类事故变成 CI 硬门。

实现说明（复测 N4）：本文件用 unittest 而非 pytest —— 与同目录其余 9 个测试文件
保持一致，使 `python -m unittest discover` 可一键运行且不需要额外依赖。
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

# 与同级测试文件一致：把仓库根加入 sys.path，使 `plugins.stock_analysis.*` 可导入
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

from plugins.stock_analysis.backtest_runner import BacktestRunner, price_limit_band
from plugins.stock_analysis.factor_lab import BacktestConfig


class _FakeGW:
    """合成网关：第 i 个标的只返回 n 根 bar，用于验证次新股剔除。"""

    def __init__(self, bars: dict):
        self._bars = bars

    def get_kline(self, symbol: str, datalen: int = 120) -> pd.DataFrame:
        n = self._bars.get(symbol, datalen)
        rng = np.random.default_rng(abs(hash(symbol)) % 2 ** 32)
        px = 10.0 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
        idx = pd.bdate_range("2024-01-02", periods=n)
        return pd.DataFrame({"open": px, "high": px * 1.01, "low": px * 0.99,
                             "close": px, "volume": np.full(n, 1e6)}, index=idx)


class PriceLimitBandTest(unittest.TestCase):
    """涨跌停档位必须分板块（修复：原实现统一 ±9.5%）。"""

    def test_bands_by_board_and_st(self):
        cases = [
            ("600519.SH", False, 0.10),   # 主板
            ("600519.SH", True, 0.05),    # 主板 ST 减半
            ("300750.SZ", False, 0.20),   # 创业板
            ("300750.SZ", True, 0.20),    # 创业板 ST 仍 20%
            ("688981.SH", False, 0.20),   # 科创板
            ("832000.BJ", False, 0.30),   # 北交所
        ]
        for symbol, is_st, expected in cases:
            with self.subTest(symbol=symbol, is_st=is_st):
                self.assertAlmostEqual(price_limit_band(symbol, is_st), expected, places=6)


class IronRulesTest(unittest.TestCase):
    UNIVERSE = ["CN:%06d" % i for i in range(30)]

    def test_iron_rules_expose_enforcement_evidence(self):
        """铁律必须回传 enforced 事实；未接入名单的项不得声称已执行。"""
        res = BacktestRunner(gateway=_FakeGW({})).run_quintile(
            self.UNIVERSE, "mom_20", datalen=120)
        iron = res["iron_rules"]
        self.assertTrue(iron["t_plus_1"]["enforced"])
        self.assertTrue(iron["exclude_suspended_limit"]["enforced"])
        # ST 名单不可得时必须为 False（而非静默 True）
        self.assertFalse(iron["exclude_st"]["enforced"])
        self.assertIn("名单不可得", iron["exclude_st"]["evidence"])

    def test_no_silent_constant_factor(self):
        """回归 S1-3：turnover/ln_mcap 不得在缺数据时静默返回恒定值或空结果。"""
        runner = BacktestRunner(gateway=_FakeGW({}))
        for name in ("turnover_20", "ln_mcap"):
            res = runner.run_quintile(self.UNIVERSE, name, datalen=120)
            self.assertTrue(res.get("error"),
                            "%s 缺数据时必须显式报错，实际返回 %s" % (name, list(res)))
            self.assertEqual(res.get("error_code"), "DATA_UNAVAILABLE")

    def test_min_list_days_actually_filters_new_listings(self):
        """回归 S0-2：min_list_days 必须真实生效（旧实现无任何执行点）。"""
        bars = {"CN:%06d" % i: (30 if i % 2 == 0 else 120) for i in range(30)}
        runner = BacktestRunner(gateway=_FakeGW(bars))
        mask, iron = runner.build_tradable_mask(
            runner.fetch_universe(list(bars), datalen=120), BacktestConfig())
        self.assertTrue(iron["exclude_new_listing"]["enforced"])
        for sym, n in bars.items():
            if n < 60:
                self.assertFalse(mask[sym].any(), "%s 上市 %d 日 < 60，不应可交易" % (sym, n))


if __name__ == "__main__":
    unittest.main(verbosity=2)
