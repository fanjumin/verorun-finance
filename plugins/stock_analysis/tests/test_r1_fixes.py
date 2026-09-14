#!/usr/bin/env python3
"""test_r1_fixes.py — R1 测试报告金融版缺陷修复的回归（不连库、不鉴真 token）。

覆盖本次三项改动的真实执行：
  1. FIX-14  SSE 并发闸由硬编码改为 SA_SSE_MAX_CONNECTIONS 可配（默认仍 2）+ 越界钳制 + 非法值回退；
  2. FIX-06  kb.py 对 project_workspace 的硬依赖改为软依赖（缺失抛 KnowledgeBaseUnavailable，可被路由转 503）；
  3. FIX-13  delete_alert 级联清理事件流水（sa_alert_events 无外键，硬删规则会永久留孤儿）。

运行：
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.stock_analysis.tests.test_r1_fixes -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))


class SseLimitConfigTest(unittest.TestCase):
    """1. FIX-14：SSE 并发闸配置化"""

    def _resolve(self, env_value):
        from plugins.stock_analysis import routes
        with mock.patch.dict(os.environ,
                             {routes._SSE_LIMIT_ENV: env_value} if env_value is not None else {},
                             clear=False):
            if env_value is None:
                os.environ.pop(routes._SSE_LIMIT_ENV, None)
            return routes._resolve_sse_max_connections()

    def test_default_unchanged(self):
        self.assertEqual(self._resolve(None), 2, '默认值必须与原硬编码一致（不改行为）')

    def test_env_override(self):
        self.assertEqual(self._resolve('3'), 3)

    def test_clamped_to_bounds(self):
        from plugins.stock_analysis import routes
        self.assertEqual(self._resolve('0'), 1, '下界必须钳到 1（0 会让 SSE 全拒）')
        self.assertEqual(self._resolve('-5'), 1)
        self.assertEqual(self._resolve('999'), routes._SSE_LIMIT_MAX,
                         '上界钳制：单进程仅 8 条线程，放任放大等于重现原缺陷')

    def test_garbage_falls_back_never_raises(self):
        """非法值绝不能在 import 期抛异常 —— 那会让整个插件 setup 失败（比原缺陷更糟）"""
        from plugins.stock_analysis import routes
        for bad in ('abc', '', '  ', '2.5'):
            with self.subTest(bad=bad):
                self.assertEqual(self._resolve(bad), routes._SSE_LIMIT_DEFAULT)

    def test_semaphore_capacity_matches_resolved_value(self):
        """行为法（不碰私有属性）：信号量容量必须等于解析值，且用后可完整归还"""
        import threading
        from plugins.stock_analysis import routes
        n = routes._SSE_MAX_CONNECTIONS
        self.assertEqual(n, routes._resolve_sse_max_connections(),
                         '模块级常量与环境解析值不一致（蓝图用的上限与信号量初值不同源）')
        probe = threading.BoundedSemaphore(n)
        for _ in range(n):
            self.assertTrue(probe.acquire(blocking=False), f'容量应至少 {n}')
        self.assertFalse(probe.acquire(blocking=False), f'第 {n + 1} 条必须拿不到（超限即拒）')
        for _ in range(n):
            probe.release()


class ChannelValidationTest(unittest.TestCase):
    """4. 复测中新发现：写入渠道校验（未知值被静默改成 in_app，契约要求 400）"""

    def test_coerce_rejects_unknown(self):
        from plugins.stock_analysis.alert_engine import coerce_channel
        for bad in ('wechat', 'emial', 'sms', '钉钉'):
            with self.subTest(bad=bad):
                self.assertIsNone(coerce_channel(bad), '未知渠道被静默改写 = 用户意图丢失')

    def test_coerce_accepts_contract_values(self):
        from plugins.stock_analysis.alert_engine import coerce_channel
        self.assertEqual(coerce_channel(None), 'in_app')
        self.assertEqual(coerce_channel(''), 'in_app')
        self.assertEqual(coerce_channel('   '), 'in_app')
        for code in ('in_app', 'email', 'im'):
            self.assertEqual(coerce_channel(code), code)
        self.assertEqual(coerce_channel('  email  '), 'email')
        # 旧中文行值仍可写（兼容存量客户端），但归一为语义码
        self.assertEqual(coerce_channel('IM'), 'im')
        self.assertEqual(coerce_channel('邮件'), 'email')

    def test_read_side_tolerance_unchanged(self):
        """读侧（存量脏行展示）不得被改成严格：那会让老数据把页面打崩"""
        from plugins.stock_analysis.alert_engine import normalize_channel
        self.assertEqual(normalize_channel('历史遗留值'), 'in_app')
        self.assertEqual(normalize_channel(None), 'in_app')


class KbSoftDependencyTest(unittest.TestCase):
    """2. FIX-06：project_workspace 缺失时降级为可捕获异常，而不是 ImportError 冒成 500"""

    def test_public_helpers_raise_on_trim(self):
        """打包态物理裁剪（N-07）后这条路径才真正可达：对外取用器也必须转成降级信号"""
        from plugins.stock_analysis import kb
        cases = (('plugins.project_workspace.models', kb.pw_get_db),
                 ('plugins.project_workspace.services.doc_processor', kb.pw_doc_processor))
        for mod, fn in cases:
            with self.subTest(mod=mod):
                with mock.patch.dict(sys.modules, {mod: None, 'plugins.project_workspace': None}):
                    with self.assertRaises(kb.KnowledgeBaseUnavailable):
                        fn()

    def test_raises_kb_unavailable_when_pw_missing(self):
        from plugins.stock_analysis import kb
        with mock.patch.dict(sys.modules, {'plugins.project_workspace.models': None}):
            with self.assertRaises(kb.KnowledgeBaseUnavailable) as ctx:
                kb._get_pw_db()
        self.assertIn('project_workspace', str(ctx.exception))

    def test_retriever_also_soft(self):
        from plugins.stock_analysis import kb
        with mock.patch.dict(sys.modules,
                             {'plugins.project_workspace.services.retriever': None}):
            with self.assertRaises(kb.KnowledgeBaseUnavailable):
                kb._pw_retriever()

    def test_search_research_propagates_unavailable_not_empty_list(self):
        """后端不可用 ≠ 无结果：必须把信号透传给路由（否则返回 200 空列表掩盖故障）"""
        from plugins.stock_analysis import kb
        with mock.patch.object(kb, 'get_stock_research_project_id', return_value='p-1'), \
                mock.patch.object(kb, '_pw_retriever',
                                  side_effect=kb.KnowledgeBaseUnavailable('pw missing')):
            with self.assertRaises(kb.KnowledgeBaseUnavailable):
                kb.search_research('q')

    def test_helper_returns_503_envelope(self):
        """真实执行响应构造：后端不可用 → 503 + meta.code=kb_unavailable（不是 500 裸栈）"""
        from flask import Flask
        from plugins.stock_analysis import routes
        app = Flask(__name__)
        with app.test_request_context('/api/kb/docs'):
            resp, status = routes._kb_unavailable('list')
        self.assertEqual(status, 503)
        payload = resp.get_json()
        self.assertFalse(payload['ok'])
        self.assertIsNone(payload['data'])
        self.assertEqual(payload['meta']['code'], 'kb_unavailable')
        self.assertEqual(payload['meta']['op'], 'list')
        self.assertTrue(payload['meta']['retryable'])

    def test_every_kb_route_handles_the_degradation(self):
        """结构锁：每个 /api/kb/* 视图都必须捕获 KnowledgeBaseUnavailable（新增端点漏接即红）"""
        import inspect
        import re
        from plugins.stock_analysis import routes
        src = inspect.getsource(routes)
        kb_routes = re.findall(r'@stock_analysis_bp\.(?:get|post|delete)\("(/api/kb/[^"]+)"\)', src)
        self.assertGreaterEqual(len(kb_routes), 7, 'kb 端点数与预期不符（用例需同步更新）')
        # 逐个视图函数体检查是否含降级捕获
        bodies = re.split(r'\n@stock_analysis_bp\.', src)
        kb_bodies = [b for b in bodies if '/api/kb/' in b.split('\n')[0]]
        self.assertEqual(len(kb_bodies), len(kb_routes), '视图与路由数不一致')
        for b in kb_bodies:
            head = b.split('\n')[0]
            self.assertIn('except KnowledgeBaseUnavailable', b,
                          f'{head} 未接降级信号（会冒成 500 裸栈）')


class DeleteAlertCascadeTest(unittest.TestCase):
    """3. FIX-13：硬删规则必须级联清掉事件流水（无外键约束）"""

    def _run_delete(self, rule_exists, event_rows):
        from plugins.stock_analysis import models_sa
        calls = []

        class _Cur:
            def __init__(self, rowcount):
                self.rowcount = rowcount

            def fetchone(self):
                return {'id': 7} if rule_exists else None

        class _Conn:
            def execute(self, sql, params=()):
                calls.append((' '.join(sql.split()), params))
                if 'RETURNING id' in sql:
                    return _Cur(1 if rule_exists else 0)
                return _Cur(event_rows)

            def commit(self):
                calls.append(('COMMIT', ()))

        cm = mock.MagicMock()
        cm.__enter__.return_value = _Conn()
        cm.__exit__.return_value = False
        with mock.patch.object(models_sa, 'get_db', return_value=cm):
            result = models_sa.delete_alert(7)
        return result, [c[0] for c in calls]

    def test_rule_deleted_also_purges_events(self):
        ok, sqls = self._run_delete(True, 3)
        self.assertTrue(ok)
        purge = [s for s in sqls if 'DELETE FROM sa_alert_events' in s]
        self.assertEqual(len(purge), 1, '删除规则后未级联清理事件 → 仍会产生孤儿行')
        self.assertIn('WHERE alert_id = ?', purge[0])

    def test_missing_rule_does_not_touch_events(self):
        ok, sqls = self._run_delete(False, 0)
        self.assertFalse(ok)
        self.assertFalse([s for s in sqls if 'sa_alert_events' in s],
                         '规则不存在却清理事件 = 误删他人历史')


if __name__ == '__main__':
    unittest.main(verbosity=2)
