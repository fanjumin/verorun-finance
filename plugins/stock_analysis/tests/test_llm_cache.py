#!/usr/bin/env python3
"""test_llm_cache.py — LLM 响应缓存单元测试（llm_cache.py，跨用户复用 + 时段 TTL）。

覆盖：
- _quantize：档位归整、None/非数值归 0.0；
- evidence_fingerprint：数值变化不换指纹、结构变化换指纹（EvidenceBundle.hash 不可用）；
- build_key：稳定性 + model/scene/symbol/scope 任一变则键变；
- reuse_ttl_seconds：盘前/盘中/盘后/非交易日四档，含 9:30 与 15:00 边界；
- get/put：DB 异常时 fail-open（get→None、put 不抛）；空 payload 不写。

运行（需 stock 依赖环境）：
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.stock_analysis.tests.test_llm_cache -v

说明：全部走 mock，不触网不连库。
"""
import datetime as _dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

from plugins.stock_analysis import llm_cache as lc

# 固定交易日样本：2026-01-05（周一）；周末样本：2026-01-10（周六）
_TRADE_DAY = _dt.datetime(2026, 1, 5)
_WEEKEND_DAY = _dt.datetime(2026, 1, 10)


class QuantizeTest(unittest.TestCase):
    """连续值量化：档位对齐 + 异常输入不参与区分。"""

    def test_step_alignment(self):
        self.assertEqual(lc._quantize(3.2, 0.5), 3.0)
        self.assertEqual(lc._quantize(3.4, 0.5), 3.5)
        self.assertEqual(lc._quantize(52, 5), 50)
        self.assertEqual(lc._quantize(53, 5), 55)

    def test_invalid_falls_back_to_zero(self):
        for bad in (None, "", "abc", [], {}):
            self.assertEqual(lc._quantize(bad, 0.5), 0.0)

    def test_small_change_same_bucket(self):
        """小幅波动同档 → 缓存不失效（核心设计意图）。

        1.1/0.5=2.2→2、1.2/0.5=2.4→2，同为 1.0 档；跨到 1.3（2.6→3）才换档。
        """
        self.assertEqual(lc._quantize(1.1, 0.5), lc._quantize(1.2, 0.5))

    def test_large_change_crosses_bucket(self):
        """大幅波动换档 → 缓存自动失效。"""
        self.assertNotEqual(lc._quantize(1.1, 0.5), lc._quantize(2.4, 0.5))


class FingerprintTest(unittest.TestCase):
    """证据指纹：剥数字后取 hash，数值微调不失效、结构变化才失效。"""

    def test_numeric_change_keeps_fingerprint(self):
        a = "毛利率 91.8%｜营收 1742.0亿｜PE 21.3"
        b = "毛利率 92.1%｜营收 1750.4亿｜PE 21.9"
        self.assertEqual(lc.evidence_fingerprint(a), lc.evidence_fingerprint(b))

    def test_structural_change_changes_fingerprint(self):
        a = "毛利率 91.8%｜营收 1742.0亿"
        b = "毛利率 91.8%｜营收 1742.0亿｜商誉 12.0亿"
        self.assertNotEqual(lc.evidence_fingerprint(a), lc.evidence_fingerprint(b))

    def test_empty_safe(self):
        self.assertEqual(lc.evidence_fingerprint(""), lc.evidence_fingerprint(None))

    def test_unusable_bundle_hash_note(self):
        """EvidenceBundle.hash 含 as_of 时间戳 → 同内容不同时刻结果不同（故不可用）。"""
        self.assertEqual(len(lc.evidence_fingerprint("x")), 16)


class BuildKeyTest(unittest.TestCase):
    """缓存键：稳定 + 五元组任一变化即变。"""

    _BASE = ("full", "600519", "qwen-max", "stock.intraday", "fp123")

    def test_stable(self):
        self.assertEqual(lc.build_key(*self._BASE), lc.build_key(*self._BASE))

    def test_model_matters(self):
        a = lc.build_key(*self._BASE)
        b = lc.build_key(*self._BASE[:2], "deepseek-v4", *self._BASE[3:])
        self.assertNotEqual(a, b)

    def test_scene_matters(self):
        """盘前/盘中/盘后挂接的场景 prompt 不同，必须换键。"""
        a = lc.build_key(*self._BASE)
        b = lc.build_key(*self._BASE[:3], "stock.postclose", *self._BASE[4:])
        self.assertNotEqual(a, b)

    def test_symbol_and_scope_matter(self):
        base = lc.build_key(*self._BASE)
        self.assertNotEqual(base, lc.build_key("research", *self._BASE[1:]))
        self.assertNotEqual(base, lc.build_key(*self._BASE[:1], "000001", *self._BASE[2:]))


class ReuseTtlTest(unittest.TestCase):
    """时段 TTL：盘中收紧、盘后跨天、非交易日放宽。"""

    def _ttl(self, day, hour, minute=0, trading=True):
        now = day.replace(hour=hour, minute=minute)
        with mock.patch("plugins.stock_analysis.market_calendar.is_trading_day",
                        return_value=trading):
            return lc.reuse_ttl_seconds(now)

    def test_intraday_is_15min(self):
        for h, m in [(9, 30), (10, 30), (14, 59)]:
            self.assertEqual(self._ttl(_TRADE_DAY, h, m), 15 * 60)

    def test_1500_boundary_matches_scenario(self):
        """15:00 整仍属盘中，与 scenario_task_type 的边界口径保持一致

        （stock_skill.py:80 `if t <= dtime(15, 0): return "stock.intraday"`）——
        两者必须同边界，否则 TTL 与场景 prompt 会错位。"""
        self.assertEqual(self._ttl(_TRADE_DAY, 15, 0), 15 * 60)
        self.assertGreater(self._ttl(_TRADE_DAY, 15, 1), 15 * 60)

    def test_preopen_is_30min(self):
        self.assertEqual(self._ttl(_TRADE_DAY, 9, 0), 30 * 60)

    def test_non_trading_day_is_12h(self):
        self.assertEqual(self._ttl(_WEEKEND_DAY, 10, 0, trading=False), 12 * 3600)

    def test_postclose_crosses_midnight(self):
        """盘后有效期必须跨到次日开盘前（原日级判定次日 9:30 就失效）。"""
        ttl = self._ttl(_TRADE_DAY, 20, 0)
        # 20:00 → 次日 9:25 约 13.4 小时
        self.assertGreater(ttl, 12 * 3600)
        self.assertLessEqual(ttl, 14 * 3600)


class FailOpenTest(unittest.TestCase):
    """缓存层绝不阻断主链路。"""

    def test_get_returns_none_on_db_error(self):
        with mock.patch.object(lc, "build_key", return_value="k"), \
             mock.patch("plugins.stock_analysis.models_sa.ensure_tables",
                        side_effect=RuntimeError("db down")):
            self.assertIsNone(lc.get("full", "600519", "m", "stock.intraday", "fp"))

    def test_put_swallows_db_error(self):
        with mock.patch("plugins.stock_analysis.models_sa.ensure_tables",
                        side_effect=RuntimeError("db down")):
            lc.put("full", "600519", "m", "stock.intraday", "fp",
                   {"report": "x"})     # 不抛异常即通过

    def test_put_skips_empty_payload(self):
        """空响应/降级文案不得落缓存，否则污染后续结果。"""
        with mock.patch("plugins.stock_analysis.models_sa.ensure_tables") as m:
            lc.put("full", "600519", "m", "stock.intraday", "fp", {})
            lc.put("full", "600519", "m", "stock.intraday", "fp", None)
            m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
