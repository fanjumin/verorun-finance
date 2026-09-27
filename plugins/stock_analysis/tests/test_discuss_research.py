#!/usr/bin/env python3
"""test_discuss_research.py — 阶段 B 多空对辩研判单测（discuss_research.py，方案 §2）。

覆盖：
- PROMPTS 四阶段键齐全且占位符正确（planner/reviewer/revise/decider）；
- run_discussed_research 四轮 LLM 调用顺序 + emit 逐轮回调顺序 + rounds 原文完整；
- signal 结构化解析命中（Decider 输出严格 JSON）与未命中兜底（hold/0.5）两条路径；
- emit 回调抛异常不影响对辩主链路；中途模型失败向上传播（供调用方归类任务失败）。

运行（需 stock 依赖环境）：
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.stock_analysis.tests.test_discuss_research -v

说明：LLM_FACTORY 注入假模型，不触网不连库；证据文本用占位字符串。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

import plugins.stock_analysis.discuss_research as dr

_EVIDENCE = "fundamental#1 revenue up; fundamental#2 margin stable."
_PHASES = ("planner", "reviewer", "revise", "decider")


def _call_content(fake_calls):
    return fake_calls[-1][0]["content"]


class PromptShapeTest(unittest.TestCase):
    def test_four_phase_keys_present(self):
        for phase in _PHASES:
            self.assertIn(phase, dr.PROMPTS)
            self.assertIsInstance(dr.PROMPTS[phase], str)
            self.assertTrue(dr.PROMPTS[phase].strip())

    def test_required_placeholders(self):
        required = {
            "planner": ["{symbol}", "{evidence}"],
            "reviewer": ["{plan_v1}", "{evidence}"],
            "revise": ["{plan_v1}", "{review}"],
            "decider": ["{symbol}", "{plan_v2}", "{review}"],
        }
        for phase, keys in required.items():
            for key in keys:
                self.assertIn(key, dr.PROMPTS[phase], "%s missing %s" % (phase, key))

    def test_decider_requests_strict_json_keys(self):
        text = dr.PROMPTS["decider"]
        for key in ("signal", "confidence", "reasons", "summary", "evidence_refs", "dissent"):
            self.assertIn('"%s"' % key, text)


class RunDiscussedResearchTest(unittest.TestCase):
    def setUp(self):
        self._saved = dr.LLM_FACTORY
        dr.LLM_FACTORY = None

    def tearDown(self):
        dr.LLM_FACTORY = self._saved

    def test_four_rounds_order_and_emit_sequence(self):
        calls, emits = [], []

        def fake(messages):
            calls.append(messages)
            return "OUT_%d" % len(calls)

        def emit(phase, content):
            emits.append((phase, content))

        # 注入缝必须先接线：原用例只定义了 fake 却未赋给 dr.LLM_FACTORY，
        # 于是 _chat 落到真实 UnifiedLLM → 无凭据时全候选失败并触发告警链路，
        # 最终在 health_check.routes 的 health_bp=None（无 Flask 上下文）处报错。
        dr.LLM_FACTORY = fake
        result = dr.run_discussed_research("600519", _EVIDENCE, emit=emit)

        # 恰好四轮 LLM，均为 user 单条消息
        self.assertEqual(len(calls), 4)
        for msg in calls:
            self.assertEqual([m["role"] for m in msg], ["user"])
        # emit 顺序 = 四阶段
        self.assertEqual([p for p, _ in emits], list(_PHASES))
        # rounds 原文与每轮输出一一对应（planner=第1轮…decider=第4轮）
        self.assertEqual(result["rounds"],
                         {"planner": "OUT_1", "reviewer": "OUT_2",
                          "revise": "OUT_3", "decider": "OUT_4"})
        # Decider 输出无结构化 JSON → 保守兜底 hold/0.5
        self.assertEqual(result["report"], "OUT_4")
        self.assertEqual(result["signal"]["signal"], "hold")
        self.assertEqual(result["signal"]["confidence"], 0.5)
        self.assertIn("conservative", result["signal"]["reasons"][0])

    def test_structured_signal_hit_on_decider(self):
        outputs = iter(["PLAN", "REVIEW", "REVISE",
                        '{"signal":"buy","confidence":0.8,"reasons":["r1","r2"],'
                        '"summary":"strong","evidence_refs":["fundamental#1"],'
                        '"dissent":"valuation high"}'])

        def fake(messages):
            return next(outputs)

        dr.LLM_FACTORY = fake
        result = dr.run_discussed_research("600519", _EVIDENCE)
        sig = result["signal"]
        self.assertEqual(sig["signal"], "buy")
        self.assertEqual(sig["confidence"], 0.8)
        self.assertIn("fundamental#1", sig.get("evidence_refs", []))
        # dissent 不进入结构化 signal（parse_structured_output 定义），保留在 Decider 原文
        self.assertIn("valuation high", result["report"])

    def test_emit_error_does_not_break_chain(self):
        def fake(messages):
            return "OUT"

        def broken_emit(phase, content):
            raise RuntimeError("emit down")

        dr.LLM_FACTORY = fake
        result = dr.run_discussed_research("600519", _EVIDENCE, emit=broken_emit)
        self.assertEqual(len(result["rounds"]), 4)

    def test_mid_phase_llm_failure_propagates(self):
        state = {"n": 0}

        def fake(messages):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("provider timeout")
            return "OUT_%d" % state["n"]

        dr.LLM_FACTORY = fake
        with self.assertRaises(RuntimeError):
            dr.run_discussed_research("600519", _EVIDENCE)

    def test_empty_evidence_still_runs(self):
        dr.LLM_FACTORY = lambda messages: "OUT"
        result = dr.run_discussed_research("600519", "")
        self.assertEqual(len(result["rounds"]), 4)


if __name__ == "__main__":
    unittest.main()
