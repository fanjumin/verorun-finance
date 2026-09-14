#!/usr/bin/env python3
"""test_scenario_prompt.py — A3 场景化 Prompt 单元测试（stock_skill.py，接线点⑤，方案 §6.2）。

覆盖：
- scenario_task_type：9:30 / 15:00 边界、盘前/盘中/盘后、非交易日回退 postclose；
- _scene_prompt：空 task_type / 空表 / 未命中 / 命中 / malformed task_triggers 容错 /
  agent_matrix 不可用回退 ''（降级零事故）；
- _call_llm 挂接：场景段命中时追加到 system prompt 尾部；缺失时保持原 prompt。

运行（需 stock 依赖环境）：
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.stock_analysis.tests.test_scenario_prompt -v

说明：全部走 mock，不触网不连库；agent_matrix 子模块以 sys.modules 假模块隔离，
避免测试触发真实内核连接（数据库/模型配置）。
"""
import contextlib
import datetime as _dt
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

from plugins.stock_analysis import stock_skill as ss
from plugins.stock_analysis.stock_skill import scenario_task_type, _scene_prompt

# 固定交易日样本：2026-01-05（周一）；周末样本：2026-01-10（周六）
_TRADE_DAY = _dt.datetime(2026, 1, 5)
_WEEKEND_DAY = _dt.datetime(2026, 1, 10)


class ScenarioTaskTypeTest(unittest.TestCase):
    """时段打标边界：9:30 / 15:00 归属、非交易日统一 postclose。"""

    def _at(self, hour, minute=0, second=0, base=_TRADE_DAY):
        return base.replace(hour=hour, minute=minute, second=second)

    def test_preopen_before_0930(self):
        for h, m in [(0, 0), (9, 0), (9, 29)]:
            with mock.patch("plugins.stock_analysis.market_calendar.is_trading_day",
                            return_value=True):
                self.assertEqual(scenario_task_type(self._at(h, m)), "stock.preopen",
                                 "09:30 前应判 preopen: %02d:%02d" % (h, m))

    def test_intraday_from_0930_to_1500(self):
        for dt_ in [self._at(9, 30), self._at(10, 30), self._at(11, 30),
                    self._at(15, 0)]:
            with mock.patch("plugins.stock_analysis.market_calendar.is_trading_day",
                            return_value=True):
                self.assertEqual(scenario_task_type(dt_), "stock.intraday",
                                 "9:30-15:00 闭区间应判 intraday: %s" % dt_)

    def test_postclose_after_1500(self):
        for dt_ in [self._at(15, 0, 1), self._at(15, 5), self._at(16, 0),
                    self._at(23, 59, 59)]:
            with mock.patch("plugins.stock_analysis.market_calendar.is_trading_day",
                            return_value=True):
                self.assertEqual(scenario_task_type(dt_), "stock.postclose",
                                 "15:00 后应判 postclose: %s" % dt_)

    def test_non_trading_day_always_postclose(self):
        for hour in [9, 10, 12, 15, 16]:
            with mock.patch("plugins.stock_analysis.market_calendar.is_trading_day",
                            return_value=False):
                self.assertEqual(scenario_task_type(self._at(hour, 0, base=_WEEKEND_DAY)),
                                 "stock.postclose",
                                 "非交易日应回退 postclose: %02d:00" % hour)


def _fake_agent_matrix_modules(get_db):
    """构造 agent_matrix 包假模块：models 提供可控 get_db，engine/model_resolver 留空。

    get_db: contextmanager，yield 的 conn.execute(...) 返回带 fetchall() 的结果对象。
    """
    parent = types.ModuleType("agent_matrix")
    parent.__path__ = []
    models = types.ModuleType("agent_matrix.models")
    models.get_db = get_db
    return {"agent_matrix": parent, "agent_matrix.models": models}


class _FakeRows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql):
        return _FakeRows(self._rows)


def _db_with(rows):
    @contextlib.contextmanager
    def _get_db():
        yield _FakeConn(rows)
    return _get_db


def _scene_row(content, triggers):
    """agent_prompts 行形状：content + task_triggers(JSON 字符串)。"""
    return {"content": content, "triggers": triggers}


class ScenePromptTest(unittest.TestCase):
    """_scene_prompt：层三语义（prompt_type='scene' + is_active + task_triggers 命中）。"""

    def test_empty_task_type_returns_empty(self):
        self.assertEqual(_scene_prompt(""), "")
        self.assertEqual(_scene_prompt(None), "")

    def test_empty_table_returns_empty(self):
        with mock.patch.dict(sys.modules,
                             _fake_agent_matrix_modules(_db_with([]))):
            self.assertEqual(_scene_prompt("stock.intraday"), "")

    def test_matching_row_returns_content(self):
        rows = [_scene_row("SCENE_INTRA", '["stock.intraday"]')]
        with mock.patch.dict(sys.modules,
                             _fake_agent_matrix_modules(_db_with(rows))):
            self.assertEqual(_scene_prompt("stock.intraday"), "SCENE_INTRA")

    def test_no_matching_triggers_returns_empty(self):
        rows = [_scene_row("SCENE_PRE", '["stock.preopen"]')]
        with mock.patch.dict(sys.modules,
                             _fake_agent_matrix_modules(_db_with(rows))):
            self.assertEqual(_scene_prompt("stock.postclose"), "")

    def test_malformed_triggers_skipped_then_later_match(self):
        rows = [
            _scene_row("BROKEN", "{not-json"),
            _scene_row("SCENE_POST", '["stock.postclose", "stock.preopen"]'),
        ]
        with mock.patch.dict(sys.modules,
                             _fake_agent_matrix_modules(_db_with(rows))):
            self.assertEqual(_scene_prompt("stock.postclose"), "SCENE_POST")

    def test_first_matching_row_wins(self):
        rows = [
            _scene_row("V2", '["stock.intraday"]'),
            _scene_row("V1", '["stock.intraday"]'),
        ]
        with mock.patch.dict(sys.modules,
                             _fake_agent_matrix_modules(_db_with(rows))):
            # 结果按 priority DESC, version DESC 返回，取首条命中
            self.assertEqual(_scene_prompt("stock.intraday"), "V2")

    def test_agent_matrix_unavailable_returns_empty(self):
        # models 模块无 get_db 属性 → 调用方静默回退 ''
        modules = _fake_agent_matrix_modules(None)
        del modules["agent_matrix.models"].get_db
        with mock.patch.dict(sys.modules, modules):
            self.assertEqual(_scene_prompt("stock.intraday"), "")


class _FakeUnifiedLLM:
    """捕获 messages 的 UnifiedLLM 假实现（验证 system prompt 组装）。"""

    last = None

    def __init__(self, agent_config):
        self.agent_config = agent_config

    def chat(self, messages, **kwargs):
        _FakeUnifiedLLM.last = messages
        return "ANALYSIS_OK"


def _llm_module_fakes():
    parent = types.ModuleType("agent_matrix")
    parent.__path__ = []
    engine = types.ModuleType("agent_matrix.engine")
    engine.UnifiedLLM = _FakeUnifiedLLM
    models = types.ModuleType("agent_matrix.models")
    models.get_agent_by_slug = lambda slug: {
        "system_prompt": "BASE_PROMPT",
        "model_name": "m-stub",
    }
    resolver = types.ModuleType("agent_matrix.model_resolver")
    resolver.resolve_model_args = lambda policy: {}
    return {
        "agent_matrix": parent,
        "agent_matrix.engine": engine,
        "agent_matrix.models": models,
        "agent_matrix.model_resolver": resolver,
    }


class CallLlmSceneHookTest(unittest.TestCase):
    """_call_llm 挂接点：场景命中追加 system prompt；场景缺失原样回退。"""

    def setUp(self):
        self.skill = object.__new__(ss.StockAnalysisSkill)  # 绕过 __init__（无网络侧效）
        self._mods = mock.patch.dict(sys.modules, _llm_module_fakes())
        self._mods.start()
        self.addCleanup(self._mods.stop)
        _FakeUnifiedLLM.last = None

    def test_scene_present_appended_after_base(self):
        with mock.patch.object(ss, "scenario_task_type",
                               return_value="stock.intraday"), \
             mock.patch.object(ss, "_scene_prompt",
                               return_value="SCENE_INTRADAY"):
            result = self.skill._call_llm("USER_QUESTION")

        self.assertEqual(result, "ANALYSIS_OK")
        sys_msg = _FakeUnifiedLLM.last[0]
        self.assertEqual(sys_msg["role"], "system")
        self.assertIn("BASE_PROMPT", sys_msg["content"])
        self.assertIn("SCENE_INTRADAY", sys_msg["content"])
        # 用户问题不受污染
        self.assertEqual(_FakeUnifiedLLM.last[-1]["role"], "user")

    def test_scene_missing_falls_back_to_base(self):
        with mock.patch.object(ss, "scenario_task_type",
                               return_value="stock.preopen"), \
             mock.patch.object(ss, "_scene_prompt", return_value=""):
            result = self.skill._call_llm("USER_QUESTION")

        self.assertEqual(result, "ANALYSIS_OK")
        sys_msg = _FakeUnifiedLLM.last[0]
        self.assertEqual(sys_msg["content"], "BASE_PROMPT")
        self.assertNotIn("---", sys_msg["content"])

    def test_scene_failure_falls_back_quietly(self):
        # _scene_prompt 本身异常时按设计静默回退（不外抛，主链路零事故）
        with mock.patch.object(ss, "scenario_task_type",
                               side_effect=RuntimeError("cal broken")), \
             mock.patch.object(ss, "_scene_prompt",
                               side_effect=RuntimeError("scene broken")):
            result = self.skill._call_llm("USER_QUESTION")

        self.assertEqual(result, "ANALYSIS_OK")
        self.assertEqual(_FakeUnifiedLLM.last[0]["content"], "BASE_PROMPT")


if __name__ == "__main__":
    unittest.main()
