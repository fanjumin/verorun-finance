"""neural_flow 单测（纯函数层，无 DB 依赖）。

运行（仓库根目录；必须直接跑本文件——unittest discover 会在加载期 import
插件包，从而触发 psycopg2 缺失错误）：
  python plugins/neural_flow/tests/test_neural_flow.py

本机可能缺 psycopg2：`import plugins.neural_flow` 会执行包 __init__ →
plugin_manager → manager → psycopg2，整包不可导入。故此处用 importlib 按文件
路径加载被测子模块（合成包 nf_under_test 解析相对导入），并把 plugin_manager
父包短路为轻量桩——本模块组只用到 logger 通道。
被测模块内部对 DB 的惰性导入仍走各自的 except 静默降级路径。
"""
import importlib.util
import json
import logging
import os
import sys
import types
import unittest

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEST_PKG = "nf_under_test"
_LOADED = {}
_SNAPSHOT_KEYS = ()
_SAVED_ORIGINALS = {}


def _stub_plugin_manager():
    """短路 plugin_manager 父包（仅提供 plugin_manager.logger 通道）。"""
    pm = types.ModuleType("plugin_manager")
    pm.__path__ = []
    pml = types.ModuleType("plugin_manager.logger")
    pml.get_plugin_logger = lambda identifier: logging.getLogger("plugin." + identifier)
    sys.modules["plugin_manager"] = pm
    sys.modules["plugin_manager.logger"] = pml


def _load(name):
    """按文件路径加载插件子模块（不执行插件包 __init__）。"""
    if name in _LOADED:
        return _LOADED[name]
    if _TEST_PKG not in sys.modules:
        pkg = types.ModuleType(_TEST_PKG)
        pkg.__path__ = [_PLUGIN_DIR]
        sys.modules[_TEST_PKG] = pkg
    spec = importlib.util.spec_from_file_location(
        "%s.%s" % (_TEST_PKG, name), os.path.join(_PLUGIN_DIR, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _LOADED[name] = module
    return module


def setUpModule():
    global _SNAPSHOT_KEYS
    _SNAPSHOT_KEYS = tuple(sys.modules)
    for key in ("plugin_manager", "plugin_manager.logger"):
        if key in sys.modules:
            _SAVED_ORIGINALS[key] = sys.modules.pop(key)
    _stub_plugin_manager()


def tearDownModule():
    """还原 sys.modules 快照，避免污染同进程其他测试模块。"""
    for key in list(sys.modules):
        if key not in _SNAPSHOT_KEYS:
            del sys.modules[key]
    sys.modules.update(_SAVED_ORIGINALS)
    _SAVED_ORIGINALS.clear()


class TestSseStream(unittest.TestCase):
    def setUp(self):
        self.sse = _load("sse_stream")

    def test_parse_topics_default_and_flow(self):
        self.assertEqual(self.sse.parse_topics(""), (["flow"], None))
        self.assertEqual(self.sse.parse_topics("flow"), (["flow"], None))
        self.assertEqual(self.sse.parse_topics("FLOW"), (["flow"], None))

    def test_parse_topics_rejects_unknown(self):
        topics, err = self.sse.parse_topics("flow,jobs")
        self.assertIsNone(topics)
        self.assertIn("unknown topic", err)

    def test_event_name_flow_span(self):
        self.assertEqual(self.sse.event_name("flow", {}), "flow.span")

    def test_format_frame_with_id(self):
        frame = self.sse.format_frame(42, "flow.span", {"a": 1})
        self.assertTrue(frame.startswith("id: 42\n"))
        self.assertIn("event: flow.span\n", frame)
        self.assertTrue(frame.endswith("\n\n"))

    def test_stream_events_yields_then_stop(self):
        """游标推进；只取 1 帧后手动停（生成器无限循环）。"""
        rows = [{"id": 1, "topic": "flow", "payload": {"stage": "llm"}}]

        def poll(after_id=None, topics=None, limit=200):
            return [r for r in rows if r["id"] > (after_id or 0)]

        gen = self.sse.stream_events(
            ["flow"], 0, "tok", lambda t: {"is_admin": True},
            poll_fn=poll, poll_interval=0, heartbeat_interval=999,
            reauth_interval=999)
        seen = []
        for frame in gen:
            seen.append(frame)
            if len(seen) >= 1:
                gen.close()
                break
        self.assertEqual(len(seen), 1)
        self.assertIn('"stage": "llm"', seen[0])

    def test_stream_events_auth_fail_first(self):
        gen = self.sse.stream_events(["flow"], 0, "bad", lambda t: None)
        frames = list(gen)  # 鉴权失败：单帧后自然结束
        self.assertEqual(len(frames), 1)
        self.assertIn("AUTH_EXPIRED", frames[0])


class TestProfileRegistry(unittest.TestCase):
    def test_builtin_profiles_valid(self):
        pr = _load("profile_registry")
        for prof in pr.BUILTIN_PROFILES.values():
            ok, reason = pr.validate_profile(prof)
            self.assertTrue(ok, msg="%s: %s" % (prof.get("domain"), reason))

    def test_validate_rejects_bad_domain(self):
        pr = _load("profile_registry")
        ok, reason = pr.validate_profile({"domain": "Bad-Domain"})
        self.assertFalse(ok)
        ok, _ = pr.validate_profile({"domain": "ok", "pipeline": []})
        self.assertFalse(ok)

    def test_validate_stage_dict_color_alias(self):
        pr = _load("profile_registry")
        prof = {"domain": "test", "pipeline": [{"stage": "triage"}],
                "stage_dict": {"triage": {"color": "#ff0000"}}}
        ok, reason = pr.validate_profile(prof)
        self.assertFalse(ok)
        self.assertIn("color", reason)

    def test_validate_extended_arch_requires_field_and_axis(self):
        pr = _load("profile_registry")
        prof = {"domain": "test", "pipeline": [{"stage": "data"}],
                "arches": [{"id": "risk_score"}]}
        ok, reason = pr.validate_profile(prof)
        self.assertFalse(ok)
        prof["arches"][0].update({"field": "meta.risk_score",
                                  "axis": {"min": 0, "max": 100}})
        ok, reason = pr.validate_profile(prof)
        self.assertTrue(ok, msg=reason)

    def test_scan_keeps_builtin_without_yaml(self):
        """外部扫描在无 domain.yaml 时内置档案不受影响。"""
        pr = _load("profile_registry")
        n = pr.refresh_registry()
        self.assertGreaterEqual(n, 2)  # platform + stock
        self.assertIn("stock", pr.get_registry())
        self.assertIn("platform", pr.get_registry())


class TestSdkPayload(unittest.TestCase):
    def test_emit_span_payload_shape(self):
        """双写失败静默 + payload 契约字段齐全（DB 缺席走 ImportError 路径）。"""
        sdk = _load("sdk")
        payload = sdk.emit_span(
            "job_1", domain="stock", stage="llm", event="end",
            entity={"type": "symbol", "id": "600519"},
            model="deepseek-v4-flash", tokens={"prompt": 10, "completion": 5},
            latency_ms=420, message="ok")
        for key in ("span_id", "trace_id", "domain", "entity", "ts", "stage",
                    "event", "decision_type", "model", "tokens", "cost_usd",
                    "latency_ms", "cache_hit", "confidence", "status",
                    "message", "meta"):
            self.assertIn(key, payload)
        self.assertTrue(payload["span_id"].startswith("sp_"))
        self.assertEqual(payload["domain"], "stock")
        json.dumps(payload)  # SSE 帧 / JSONB 双通道可序列化

    def test_span_timer_pairs_start_end(self):
        sdk = _load("sdk")
        emitted = []
        orig = sdk.emit_span
        sdk.emit_span = lambda *a, **kw: emitted.append((a, kw)) or {}
        try:
            with sdk.span_timer("t1", domain="test", stage="triage") as sp:
                sp.confidence = 0.9
            self.assertEqual(len(emitted), 2)
            self.assertEqual(emitted[0][0][3], "start")    # event 参数位
            self.assertEqual(emitted[1][0][3], "end")
            self.assertEqual(emitted[1][1].get("confidence"), 0.9)
            self.assertIsNotNone(emitted[1][1].get("latency_ms"))
            emitted.clear()
            try:  # 异常路径：end 帧 status=error
                with sdk.span_timer("t2", domain="test", stage="triage"):
                    raise ValueError("boom")
            except ValueError:
                pass
            self.assertEqual(len(emitted), 2)
            self.assertEqual(emitted[1][1].get("status"), "error")
        finally:
            sdk.emit_span = orig


if __name__ == "__main__":
    unittest.main()
