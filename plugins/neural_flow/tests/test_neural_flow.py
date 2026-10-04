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
import ast
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import tempfile
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

    def test_parse_topics_dedup(self):
        """重复 topic 保序去重（NF-14）：flow,flow 只占一个占位，不影响白名单语义。"""
        self.assertEqual(self.sse.parse_topics("flow,flow"), (["flow"], None))
        self.assertEqual(self.sse.parse_topics(" flow , FLOW ,flow "),
                         (["flow"], None))
        self.assertEqual(self.sse.parse_topics("flow, flow, flow"),
                         (["flow"], None))

    def test_parse_last_id_validation(self):
        self.assertEqual(self.sse.parse_last_id(""), (0, None))
        self.assertEqual(self.sse.parse_last_id(None), (0, None))
        self.assertEqual(self.sse.parse_last_id("123"), (123, None))
        self.assertIsNone(self.sse.parse_last_id("abc")[0])
        self.assertIsNone(self.sse.parse_last_id("-1")[0])
        self.assertIsNone(self.sse.parse_last_id("1.5")[0])

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

    def test_platform_subdomains_resolve_to_platform(self):
        """llm/agent/system 子域解析到 platform 档案，且对外视图可表达。"""
        pr = _load("profile_registry")
        pr.refresh_registry()
        for sub in ("llm", "agent", "system"):
            prof = pr.get_profile(sub)
            self.assertIsNotNone(prof, "subdomain %s missing" % sub)
            self.assertEqual(prof["domain"], "platform")
        views = {p["domain"]: p for p in pr.list_profiles()}
        for sub in ("llm", "agent", "system"):
            self.assertIn(sub, views)
            self.assertEqual(views[sub]["alias_of"], "platform")

    def test_reserved_domain_guard(self):
        """内置域保留；platform 子域别名不在保留范围。"""
        pr = _load("profile_registry")
        self.assertTrue(pr._is_reserved_domain("platform"))
        self.assertTrue(pr._is_reserved_domain("stock"))
        for free in ("llm", "agent", "system", "medical"):
            self.assertFalse(pr._is_reserved_domain(free), free)

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "pyyaml required")
    def test_external_cannot_override_builtin(self):
        """外部 domain: stock 被拒载并留痕，内置 stock 档案不被替换。"""
        pr = _load("profile_registry")
        pr.refresh_registry()
        tmp = tempfile.mkdtemp(prefix="nfplug_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        plug_dir = os.path.join(tmp, "evilplug")
        os.makedirs(plug_dir)
        with open(os.path.join(plug_dir, "domain.yaml"), "w", encoding="utf-8") as fh:
            fh.write("domain: stock\npipeline:\n  - stage: data\n")
        pr._scan_external(base_dir=tmp)
        self.assertIs(pr.get_registry()["stock"], pr.BUILTIN_PROFILES["stock"])
        reserved = [e for e in pr.get_errors()
                    if e.get("plugin") == "evilplug"
                    and "reserved" in e.get("reason", "")]
        self.assertTrue(reserved, "expected a reserved-domain rejection entry")

    def test_validate_optional_sections_structure(self):
        """decision/routes/statusbar/nouns 省略合法，畸形结构被拒（NF-11）。"""
        pr = _load("profile_registry")
        base = {"domain": "medical", "pipeline": [{"stage": "data"}]}
        ok, _ = pr.validate_profile(dict(base))
        self.assertTrue(ok)
        bad_cases = [
            {"decision": []},
            {"decision": {"title": "x"}},
            {"routes": []},
            {"routes": {"cheap": []}},
            {"routes": {"cheap": {"card": []}}},
            {"statusbar": {}},
            {"statusbar": [{}]},
            {"statusbar": [{"source": "  "}]},
            {"nouns": []},
            {"nouns": {"trace": []}},
        ]
        for extra in bad_cases:
            merged = dict(base)
            merged.update(extra)
            ok, reason = pr.validate_profile(merged)
            self.assertFalse(ok, "expected rejection for %r (%s)" % (extra, reason))


class TestCollectors(unittest.TestCase):
    def _fresh_state(self, col):
        col._CURSOR_READY["agent_token_logs"] = False
        col._CURSORS["agent_token_logs"] = 0

    def test_init_cursors_reuses_locked_conn_success(self):
        """复用持锁连接初始化成功：置就绪、写游标、不关闭借用连接、建 SAVEPOINT。"""
        col = _load("collectors")
        self._fresh_state(col)

        class FakeCursor:
            def fetchone(self):
                return {"m": 555}

        class FakeConn:
            def __init__(self):
                self.statements = []
                self.closed = False

            def execute(self, sql, params=None):
                self.statements.append(sql)
                return FakeCursor()

            def close(self):
                self.closed = True

        conn = FakeConn()
        col.init_cursors(conn)
        self.assertTrue(col._CURSOR_READY["agent_token_logs"])
        self.assertEqual(col._CURSORS["agent_token_logs"], 555)
        self.assertFalse(conn.closed, "borrowed conn must not be closed")
        self.assertTrue(any("SAVEPOINT" in s for s in conn.statements))

    def test_init_cursors_reuses_locked_conn_failure(self):
        """MAX(id) 查询失败：ROLLBACK TO SAVEPOINT、保持未就绪、不外抛、不关闭连接。"""
        col = _load("collectors")
        self._fresh_state(col)

        class FakeConn:
            def __init__(self):
                self.statements = []
                self.closed = False

            def execute(self, sql, params=None):
                self.statements.append(sql)
                if "agent_token_logs" in sql:
                    raise RuntimeError("db is down")
                return None

            def close(self):
                self.closed = True

        conn = FakeConn()
        col.init_cursors(conn)  # 失败不外抛
        self.assertFalse(col._CURSOR_READY["agent_token_logs"])
        self.assertFalse(conn.closed)
        self.assertTrue(any("ROLLBACK TO SAVEPOINT" in s for s in conn.statements))

    def test_run_once_no_per_round_create_schema(self):
        """run_once 不再每轮 CREATE SCHEMA；锁/读/解锁链路与连接归还正常（NF-12）。"""
        col = _load("collectors")
        _load("sdk")  # 确保 run_once 内 from .sdk import emit_span 可解析
        self._fresh_state(col)
        col._CURSOR_READY["agent_token_logs"] = True
        col._CURSORS["agent_token_logs"] = 0

        class ResultCursor:
            def __init__(self, one=None, all_=None):
                self._one = one
                self._all = all_

            def fetchone(self):
                return self._one

            def fetchall(self):
                return self._all if self._all is not None else []

        class FakeConn:
            def __init__(self):
                self.statements = []
                self.closed = False

            def execute(self, sql, params=None):
                self.statements.append(sql)
                if "pg_try_advisory_lock" in sql:
                    return ResultCursor(one={"ok": True})
                if "agent_token_logs" in sql:
                    return ResultCursor(all_=[])
                return ResultCursor()

            def commit(self):
                pass

            def close(self):
                self.closed = True

        conn = FakeConn()
        db_mod = types.ModuleType("plugins._base.db")
        db_mod.get_pooled_connection = lambda: conn
        plug_mod = types.ModuleType("plugins")
        plug_mod.__path__ = []
        base_mod = types.ModuleType("plugins._base")
        base_mod.__path__ = []
        missing = object()
        saved = {k: sys.modules.get(k, missing) for k in
                 ("plugins", "plugins._base", "plugins._base.db")}
        sys.modules["plugins"] = plug_mod
        sys.modules["plugins._base"] = base_mod
        sys.modules["plugins._base.db"] = db_mod
        try:
            result = col.run_once()
        finally:
            for key, value in saved.items():
                if value is missing:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value
        self.assertEqual(result, {"emitted": 0})
        self.assertFalse(any("CREATE SCHEMA" in s for s in conn.statements),
                         "run_once must not issue CREATE SCHEMA per round")
        # DEF-24：主库表已显式 public. 限定，run_once 不再依赖 search_path
        self.assertFalse(any("SET search_path" in s for s in conn.statements),
                         "run_once must not issue SET search_path per round")
        self.assertTrue(any("public.agent_token_logs" in s for s in conn.statements))
        self.assertTrue(any("pg_try_advisory_lock" in s for s in conn.statements))
        self.assertTrue(any("pg_advisory_unlock" in s for s in conn.statements))
        self.assertTrue(conn.closed, "connection must be returned to pool")

    def test_clamp_collector_interval(self):
        """间隔归一化（NF-13）：非法→5、下界 1、上界 3600 钳制并告警、边界与正常值原样。"""
        col = _load("collectors")
        # 非数值/None → 默认 5，不告警
        for raw in (None, "", "abc", []):
            self.assertEqual(col.clamp_collector_interval(raw), 5, repr(raw))
        # 下界静默钳制
        self.assertEqual(col.clamp_collector_interval("0"), 1)
        self.assertEqual(col.clamp_collector_interval(-5), 1)
        # 边界值与正常区间原样
        self.assertEqual(col.clamp_collector_interval(1), 1)
        self.assertEqual(col.clamp_collector_interval(5), 5)
        self.assertEqual(col.clamp_collector_interval("3600"), 3600)
        # 超上界：钳到 3600 且经 log 回调留痕
        warnings = []
        self.assertEqual(
            col.clamp_collector_interval(999999, log=lambda m, lv="warning": warnings.append((m, lv))),
            col.MAX_INTERVAL_SECONDS)
        self.assertEqual(len(warnings), 1)
        self.assertIn("999999", warnings[0][0])
        self.assertEqual(warnings[0][1], "warning")
        # 未注入 log 回调时超上界也不抛
        self.assertEqual(col.clamp_collector_interval(999999), col.MAX_INTERVAL_SECONDS)


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

    def test_emit_span_bad_cost_does_not_raise(self):
        """坏 cost_usd 不抛穿打断主流程，归零后照常发射；正常值保留 6 位小数。"""
        sdk = _load("sdk")
        for bad in ("abc", None, [1], float("nan"), float("inf")):
            payload = sdk.emit_span("t", domain="platform", cost_usd=bad)
            self.assertEqual(payload["cost_usd"], 0.0, repr(bad))
            json.dumps(payload)  # 归零后仍可序列化
        payload = sdk.emit_span("t", cost_usd=0.1234567)
        self.assertEqual(payload["cost_usd"], 0.123457)

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

    def test_span_timer_error_path_reports_usage(self):
        """异常路径的 error end 帧同样透传 tokens/cost_usd/confidence（NF-09）。"""
        sdk = _load("sdk")
        emitted = []
        orig = sdk.emit_span
        sdk.emit_span = lambda *a, **kw: emitted.append((a, kw)) or {}
        try:
            try:
                with sdk.span_timer("t", domain="test", stage="triage") as sp:
                    sp.tokens = {"prompt": 30, "completion": 8}
                    sp.cost_usd = 0.05
                    sp.confidence = 0.4
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            end = emitted[1][1]
            self.assertEqual(end["status"], "error")
            self.assertEqual(end["tokens"], {"prompt": 30, "completion": 8})
            self.assertEqual(end["cost_usd"], 0.05)
            self.assertEqual(end["confidence"], 0.4)
        finally:
            sdk.emit_span = orig

    def test_span_timer_error_message_truncated(self):
        """超长异常摘要被截断到 200 字符，保留异常类型前缀（NF-10）。"""
        sdk = _load("sdk")
        emitted = []
        orig = sdk.emit_span
        sdk.emit_span = lambda *a, **kw: emitted.append((a, kw)) or {}
        try:
            try:
                with sdk.span_timer("t", domain="test", stage="triage"):
                    raise RuntimeError("x" * 500)
            except RuntimeError:
                pass
            msg = emitted[1][1]["message"]
            self.assertLessEqual(len(msg), 200)
            self.assertTrue(msg.startswith("RuntimeError:"))
        finally:
            sdk.emit_span = orig


def _norm_sql(stmt: str) -> str:
    """SQL 规范化（仅用于迁移 vs 实跑的覆盖率比对）：去行注释、去 schema 限定、
    折叠空白、转小写。使迁移文件的 `neural_flow.tbl` 多行写法与 `_SCHEMA`
    内不带前缀的字符串可比。"""
    lines = [ln.split("--", 1)[0] for ln in stmt.splitlines()]
    text = " ".join(lines).replace("neural_flow.", "")
    return re.sub(r"\s+", " ", text).strip().lower()


class TestSchemaDdl(unittest.TestCase):
    """NF-01 回归门：ensure_tables() 实跑 DDL 必须覆盖迁移文件的全部语句。

    曾经的事故：`_SCHEMA = \"\"\"…\"\"\",` 顶层逗号把值解析成 1 元组，后 5 条
    DDL 沦为被丢弃的独立表达式语句，唯一索引永不创建且无告警。
    """

    def test_ensure_tables_covers_migration(self):
        migrations = os.path.join(_PLUGIN_DIR, "migrations", "v1.0.0_init.sql")

        class FakeConn:
            def __init__(self):
                self.statements = []

            def execute(self, sql, params=None):
                self.statements.append(sql)
                return self

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        conn = FakeConn()
        db_mod = types.ModuleType("plugins._base.db")
        db_mod.get_pooled_connection = lambda: conn
        plug_mod = types.ModuleType("plugins")
        plug_mod.__path__ = []
        base_mod = types.ModuleType("plugins._base")
        base_mod.__path__ = []
        missing = object()
        saved = {k: sys.modules.get(k, missing)
                 for k in ("plugins", "plugins._base", "plugins._base.db")}
        sys.modules["plugins"] = plug_mod
        sys.modules["plugins._base"] = base_mod
        sys.modules["plugins._base.db"] = db_mod
        _LOADED.pop("models_nf", None)  # 确保在桩连接下重新绑定
        try:
            models = _load("models_nf")
            # 结构性守护：_SCHEMA 必须是 6 条 DDL 的元组（NF-01 事故时长度为 1）
            self.assertIsInstance(models._SCHEMA, tuple)
            self.assertEqual(len(models._SCHEMA), 6)
            models.ensure_tables()
        finally:
            for key, value in saved.items():
                if value is missing:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value

        executed = {_norm_sql(s) for s in conn.statements}
        # get_nf_db 上下文 2 条（CREATE SCHEMA + SET search_path）+ _SCHEMA 6 条
        self.assertEqual(len(conn.statements), 8)
        # 六个关键标志必须真实出现在实跑语句里
        joined = "\n".join(sorted(executed))
        for marker in (
                "create table if not exists nf_flow_spans",
                "create index if not exists idx_nfs_domain_created",
                "create index if not exists idx_nfs_trace",
                "create unique index if not exists uq_nfs_source",
                "add column if not exists source",
                "add column if not exists source_id"):
            self.assertIn(marker, joined, "missing DDL marker: %s" % marker)
        # 迁移文件逐条语句都必须被实跑集合覆盖（审计档不得是装饰性文件）
        with open(migrations, encoding="utf-8") as fh:
            migration_sql = fh.read()
        migration_stmts = [s for s in (_norm_sql(x) for x in migration_sql.split(";"))
                           if s]
        self.assertEqual(len(migration_stmts), 7)  # schema + table + 2 idx + 2 alter + uq
        for stmt in migration_stmts:
            self.assertIn(stmt, executed,
                          "migration DDL not executed by ensure_tables(): %s" % stmt)


class TestPluginContract(unittest.TestCase):
    """NF-02 / NF-03：生命周期失败语义与版本兼容声明（插件包在本机不可整包
    导入，NF-02 以 AST 结构守护，不实例化插件）。"""

    INIT_PY = os.path.join(_PLUGIN_DIR, "__init__.py")
    PLUGIN_JSON = os.path.join(_PLUGIN_DIR, "plugin.json")

    def test_setup_raises_plugin_install_error(self):
        """setup() 建表失败必须 raise PluginInstallError（PF-02 闸门），不得 return False。"""
        with open(self.INIT_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        setup = next((n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "setup"), None)
        self.assertIsNotNone(setup, "NeuralFlowPlugin.setup() must exist")

        def _raises_plugin_install_error(node):
            return (isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name)
                    and node.exc.func.id == "PluginInstallError")

        self.assertTrue(any(_raises_plugin_install_error(n) for n in ast.walk(setup)),
                        "setup() must raise PluginInstallError on ensure_tables failure")
        returns_false = [n for n in ast.walk(setup)
                         if (isinstance(n, ast.Return)
                             and isinstance(n.value, ast.Constant)
                             and n.value.value is False)]
        self.assertEqual(returns_false, [], "setup() must not express failure via return False")

    def test_compatible_editions_cross_edition(self):
        """DEF-27：outbox 不可用时由 probe_outbox() 优雅降级（503 + 有界退出），
        实时通道不再构成版本收窄的理由；恢复全版本通用（空数组 = 全版本兼容），
        同时解开"现网已装、卸载后不可回装"的死结（原 NF-03 的 finance-only 声明）。"""
        with open(self.PLUGIN_JSON, encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(meta["compatible_editions"], [])


if __name__ == "__main__":
    unittest.main()
