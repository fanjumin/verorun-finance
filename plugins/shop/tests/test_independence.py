#!/usr/bin/env python3
"""test_independence.py — Shop 插件独立性测试（与 Subscription 零交叉回归护栏）

覆盖场景:
  S1  AST import 图   : shop 全部 .py 不 import subscription（绝对导入）
  S2  声明一致性      : 双方 plugin.json dependencies 为空且不引用对方
  S3  Schema 隔离     : shop 表集合 ∩ subscription 表集合 = ∅
  S4  仅启用 Shop     : 子进程仅 import plugins.shop，sys.modules 不含 subscription
  S5  仅启用 Subscription: 子进程仅 import plugins.subscription，sys.modules 不含 shop
  S6  交替加载稳定性  : 同进程交替 import 双方 ×3，无异常、无残留

运行:
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.shop.tests.test_independence -v
"""

import ast
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
PLUGIN_ID = 'shop'
PLUGIN_DIR = os.path.join(ROOT, 'plugins', PLUGIN_ID)
PEER_ID = 'subscription'
PEER_DIR = os.path.join(ROOT, 'plugins', PEER_ID)
PEER_PKG = f'plugins.{PEER_ID}'

_IGNORE_DIRS = {'tests', 'node_modules', '__pycache__', '.git', 'tmp'}


def _py_files(root):
    """递归收集目录下所有 .py 文件"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for fn in filenames:
            if fn.endswith('.py'):
                out.append(os.path.join(dirpath, fn))
    return out


def _import_targets(src):
    """AST 提取所有绝对 import 目标（相对导入属插件内部，跳过）"""
    tree = ast.parse(src)
    targets = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                targets.add(a.name)
                targets.add(a.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mod = node.module or ''
            targets.add(mod)
            targets.add(mod.split('.')[0])
    return targets


def _load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _extract_table_names(src):
    """从 DDL 源码提取 CREATE TABLE 表名"""
    return set(re.findall(r'CREATE TABLE (?:IF NOT EXISTS )?([A-Za-z_][A-Za-z0-9_\.]*)', src, re.I))


def _run_code(code):
    """在子进程运行一段 Python 代码（cwd=项目根，PYTHONPATH 指向项目根）"""
    env = dict(os.environ)
    env['PYTHONPATH'] = ROOT + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=120)


class TestImportGraph(unittest.TestCase):
    """S1 — shop 插件不得以任何绝对导入方式引用 subscription"""

    def test_shop_never_imports_subscription(self):
        offenders = []
        for path in _py_files(PLUGIN_DIR):
            with open(path, 'r', encoding='utf-8') as f:
                src = f.read()
            targets = _import_targets(src)
            for t in targets:
                if t == PEER_ID or t.startswith(f'plugins.{PEER_ID}'):
                    offenders.append((os.path.relpath(path, ROOT), t))
        self.assertEqual(
            offenders, [],
            f'Shop 插件存在对 Subscription 的 import 引用: {offenders}')


class TestPluginJson(unittest.TestCase):
    """S2 — 双方插件声明互不依赖"""

    def setUp(self):
        self.shop_cfg = _load_json(os.path.join(PLUGIN_DIR, 'plugin.json'))
        self.peer_cfg = _load_json(os.path.join(PEER_DIR, 'plugin.json'))

    def test_shop_dependencies_empty(self):
        self.assertEqual(self.shop_cfg.get('dependencies', {}), {},
                         'Shop plugin.json dependencies 必须为空')

    def test_no_cross_reference(self):
        self.assertNotIn(PEER_ID, self.shop_cfg.get('dependencies', {}),
                         'Shop dependencies 不得引用 subscription')
        self.assertNotIn(PLUGIN_ID, self.peer_cfg.get('dependencies', {}),
                         'Subscription dependencies 不得引用 shop')


class TestSchemaIsolation(unittest.TestCase):
    """S3 — shop 与 subscription 无共享数据库表"""

    def test_no_shared_tables(self):
        with open(os.path.join(PLUGIN_DIR, 'models', 'database.py'), 'r', encoding='utf-8') as f:
            shop_ddl = f.read()
        with open(os.path.join(PEER_DIR, 'models.py'), 'r', encoding='utf-8') as f:
            peer_ddl = f.read()
        shop_tables = _extract_table_names(shop_ddl)
        peer_tables = _extract_table_names(peer_ddl)
        shared = shop_tables & peer_tables
        self.assertEqual(
            shared, set(),
            f'Shop 与 Subscription 存在共享表: {shared}')


class TestRuntimeIsolation(unittest.TestCase):
    """S4/S5 — 进程级 import 隔离（仅启用一方时，另一方模块不进入进程）"""

    def _run(self, code):
        return _run_code(code)

    def test_shop_only_loads_no_subscription(self):
        code = (
            "import sys\n"
            "import plugins.shop\n"
            "bad = [m for m in sys.modules\n"
            "       if m == 'plugins.subscription' or m.startswith('plugins.subscription.')]\n"
            "if bad:\n"
            "    sys.stderr.write('loaded: %r\\n' % bad); sys.exit(1)\n"
        )
        r = self._run(code)
        self.assertEqual(r.returncode, 0, f'仅启用 Shop 时仍加载了 subscription 模块\nstdout={r.stdout}\nstderr={r.stderr}')

    def test_subscription_only_loads_no_shop(self):
        code = (
            "import sys\n"
            "import plugins.subscription\n"
            "bad = [m for m in sys.modules\n"
            "       if m == 'plugins.shop' or m.startswith('plugins.shop.')]\n"
            "if bad:\n"
            "    sys.stderr.write('loaded: %r\\n' % bad); sys.exit(1)\n"
        )
        r = self._run(code)
        self.assertEqual(r.returncode, 0, f'仅启用 Subscription 时仍加载了 shop 模块\nstdout={r.stdout}\nstderr={r.stderr}')


class TestToggleStability(unittest.TestCase):
    """S6 — 同进程交替加载双方插件 ×3，验证无冲突、无异常"""

    def test_alternating_imports_stable(self):
        code = (
            "for i in range(3):\n"
            "    import importlib\n"
            "    for mod in ('plugins.shop', 'plugins.subscription'):\n"
            "        m = importlib.import_module(mod)\n"
            "        assert m is not None\n"
            "        importlib.reload(m)\n"
        )
        r = _run_code(code)
        self.assertEqual(
            r.returncode, 0,
            f'交替加载双方插件异常\nstdout={r.stdout}\nstderr={r.stderr}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
