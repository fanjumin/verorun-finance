# plugins/stock_analysis/tests/test_portfolio_var.py
"""组合 VaR 回归（回归 P0：未装 scipy 时 /api/portfolio/analyze 曾 500）。

实现说明（复测 N4）：本文件用 unittest 而非 pytest —— 与同目录其余 9 个测试文件
保持一致，使 `python -m unittest discover` 可一键运行且不需要额外依赖。
"""
import builtins
import os
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd

# 与同级测试文件一致：把仓库根加入 sys.path，使 `plugins.stock_analysis.*` 可导入
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

from plugins.stock_analysis.portfolio import _norm_ppf, portfolio_var


class NormPpfTest(unittest.TestCase):
    def test_matches_reference(self):
        """无 scipy 环境下的正态分位精度锚点。"""
        ref = {0.001: -3.090232, 0.01: -2.326348, 0.05: -1.644854,
               0.5: 0.0, 0.95: 1.644854, 0.99: 2.326348}
        for p, expect in ref.items():
            with self.subTest(p=p):
                self.assertAlmostEqual(_norm_ppf(p), expect, delta=1e-5)


class PortfolioVarTest(unittest.TestCase):
    def test_no_scipy_import_required(self):
        """硬门：模块导入与全方法调用不得依赖 scipy。"""
        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name.startswith("scipy"):
                raise ImportError("scipy blocked by test")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", _blocked):
            rng = np.random.default_rng(2026)
            ret = pd.DataFrame(rng.normal(0, 0.01, (300, 3)), columns=list("ABC"))
            for method in ("historical", "parametric", "cornish_fisher"):
                out = portfolio_var(ret, np.array([0.4, 0.3, 0.3]), method=method)
                self.assertTrue(np.isfinite(out["var"]) and np.isfinite(out["cvar"]),
                                "%s 在无 scipy 环境下应可用" % method)

    def test_cvar_is_not_trivially_equal_to_var(self):
        """回归：cornish_fisher 分支旧实现 cvar = var（偷懒分支）。"""
        rng = np.random.default_rng(7)
        # 造明显左偏 + 厚尾样本，确保 CVaR 应与 VaR 有实质差异
        x = np.concatenate([rng.normal(0, 0.008, 400), -np.abs(rng.normal(0, 0.05, 40))])
        ret = pd.DataFrame({"A": x})
        out = portfolio_var(ret, np.array([1.0]), method="cornish_fisher")
        self.assertLess(out["cvar"], out["var"], "CVaR 必须比 VaR 更极端（更小）")

    def test_degenerate_sample_is_flagged(self):
        """样本不足/零波动时如实标注 degenerate，不返回 NaN 也不伪造尾部。"""
        ret = pd.DataFrame({"A": [0.01, 0.01, 0.01]})
        out = portfolio_var(ret, np.array([1.0]), method="cornish_fisher")
        self.assertTrue(out["degenerate"])
        self.assertTrue(np.isfinite(out["var"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
