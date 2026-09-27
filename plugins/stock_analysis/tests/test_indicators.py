#!/usr/bin/env python3
"""test_indicators.py — 指标层真实计算用例（不 mock pandas / indicators）。

回归锚点（DEF-04 / DEF-01）：
- kdj 曾在 pandas>=3 下用 `pd.Series(pd.NA, dtype="float64")` 构造抛 TypeError，
  导致 /api/kline daily 502；本地 67 例因全 mock 未拦住。本文件直接调用真实
  compute_indicators，若该写法回归将在此处红灯，无需等待 HTTP 层暴露。

运行（需 stock 依赖环境，.stock_deps 在 PYTHONPATH）：
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.stock_analysis.tests.test_indicators -v
"""
import json
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

from plugins.stock_analysis.indicators import INDICATOR_VERSION, compute_indicators, kdj


def _frame(n=120, seed=1):
    """确定性合成日线（含复权列），无任何网络/数据源依赖。"""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    open_ = np.concatenate(([close[0]], close[:-1])) * (1 + rng.normal(0, 0.002, n))
    high = close * 1.02
    low = close * 0.98
    vol = rng.integers(1_000_000, 5_000_000, n)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": vol,
        "open_hfq": open_ * 1.5, "high_hfq": high * 1.5,
        "low_hfq": low * 1.5, "close_hfq": close * 1.5,
    })


class ComputeIndicatorsTest(unittest.TestCase):
    """真实计算：构造 frame → compute_indicators，全程不 mock 指标层。"""

    def test_compute_returns_full_suite(self):
        # DEF-01 回归：pandas>=3 下 kdj 若用 pd.NA 构造 float64 序列会抛 TypeError
        out = compute_indicators(_frame(), basis="raw")
        self.assertEqual(set(out), {"ma5", "ma10", "ma20", "ma60",
                                    "macd", "kdj", "rsi14", "boll"})
        for key in ("ma5", "ma10", "ma20", "ma60", "rsi14"):
            self.assertEqual(len(out[key]), 120, key)

    def test_indicator_version_bumped_after_kdj_fix(self):
        # 客户端缓存失效锚点：修复占位写法后必须升 iv，确保旧缓存被重建
        self.assertEqual(INDICATOR_VERSION, "iv-4")

    def test_hfq_basis_scales_ma(self):
        frame = _frame()
        raw = compute_indicators(frame, basis="raw")
        hfq = compute_indicators(frame, basis="hfq")
        # 等比放大 1.5 后 MA 按同比例放大（后复权口径）
        self.assertAlmostEqual(hfq["ma20"][-1] / raw["ma20"][-1], 1.5, places=3)

    def test_kdj_length_range_and_j_formula(self):
        out = compute_indicators(_frame(), basis="raw")
        k, d, j = out["kdj"]["k"], out["kdj"]["d"], out["kdj"]["j"]
        self.assertEqual(len(k), 120)
        self.assertEqual(len(d), 120)
        self.assertEqual(len(j), 120)
        tail_k = [x for x in k[-20:] if x is not None]
        tail_d = [x for x in d[-20:] if x is not None]
        self.assertTrue(tail_k and tail_d)
        self.assertTrue(all(0 <= x <= 100 for x in tail_k))
        self.assertTrue(all(0 <= x <= 100 for x in tail_d))
        # j 由未取整的 k/d 算出，而三者在出参上各自四舍五入到 4 位小数，
        # 故 j = 3k − 2d 只在该舍入误差内成立（上界 = 5 × 5e-5 = 2.5e-4）。
        # 原 places=4 恰好卡在边界（实测差 1.0e-4）而误报失败（复测 N1-3）。
        self.assertAlmostEqual(j[-1], 3 * k[-1] - 2 * d[-1], delta=1e-3)

    def test_rsi14_bounds(self):
        out = compute_indicators(_frame(), basis="raw")
        tail = [x for x in out["rsi14"] if x is not None]
        self.assertTrue(tail)
        self.assertTrue(all(0 <= x <= 100 for x in tail))

    def test_output_json_safe(self):
        # 出参可 JSON 序列化（无 pd.NA / NaN 泄漏为非法 JSON）
        out = compute_indicators(_frame(60), basis="raw")
        text = json.dumps(out)
        self.assertIn('"kdj"', text)
        self.assertNotIn("NaN", text)
        self.assertNotIn("NA", text)

    def test_warmup_leading_none_then_filled(self):
        out = compute_indicators(_frame(120), basis="raw")
        # MA60 前 59 根为 None（暖机），末段已收敛非空
        self.assertIsNone(out["ma60"][0])
        self.assertIsNotNone(out["ma60"][-1])


class KdjDirectTest(unittest.TestCase):
    """直接调用 kdj 的最小回归（对应线上崩溃行 indicators.py kdj）。"""

    def test_kdj_does_not_raise(self):
        close = pd.Series(np.linspace(10, 20, 120))
        out = kdj(high=close * 1.02, low=close * 0.98, close=close)
        self.assertEqual(len(out["k"]), 120)
        self.assertIsNotNone(out["k"][-1])

    def test_kdj_warmup_then_converged(self):
        # 单边上涨且"当日收盘即窗口最高"（high=close）→ RSV 饱和于 100，K 应收敛到高位。
        # 原用例用 high=close*1.02 的 ±2% 高位带宽，RSV 实际饱和于 (0.02C+Δ)/(0.04C+Δ)≈72.6，
        # 与"K>90"的期望不符（复测 N1-4）——此处修**数据**而非放宽断言，保持断言语义。
        close = pd.Series(np.linspace(10, 20, 120))
        out = kdj(high=close, low=close * 0.98, close=close)
        self.assertGreater(out["k"][-1], 90)
        self.assertGreater(out["d"][-1], 80)


if __name__ == "__main__":
    unittest.main()
