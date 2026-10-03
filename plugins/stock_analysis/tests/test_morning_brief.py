# -*- coding: utf-8 -*-
"""晨报生成单测（O6）：只测 build_morning_brief——纯函数，DB 以 monkeypatch 注入。

不测 dispatch_morning_brief：它依赖 advisory lock 与真实连接池，留作冒烟覆盖。
"""
import contextlib

import plugins.stock_analysis.models_sa as sa


class _FakeConn:
    """模拟 get_db() 上下文产出的连接，只实现本用例用到的 execute/fetchall。"""

    def __init__(self, rows):
        self._rows = rows
        self.sql = ''
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return self

    def fetchall(self):
        return self._rows


def _patch(monkeypatch, rows, run=None):
    """注入假 DB。morning_brief 内部是函数级 `from .models_sa import ...`，
    每次调用都重新取模块属性，故 patch 模块属性即可生效。"""
    @contextlib.contextmanager
    def _fake_get_db(*_a, **_kw):
        yield _FakeConn(rows)

    monkeypatch.setattr(sa, 'get_db', _fake_get_db)
    monkeypatch.setattr(sa, 'get_run_stats', lambda _rid: run)


def test_no_data_returns_none(monkeypatch):
    """无信号 → None（dispatch 据此不发射事件，避免空邮件）。"""
    _patch(monkeypatch, [])
    from plugins.stock_analysis.morning_brief import build_morning_brief
    assert build_morning_brief('2026-09-29') is None


def test_groups_ordering_and_name_fallback(monkeypatch):
    rows = [
        {'symbol': '600519', 'name': '贵州茅台', 'signal': 'BUY', 'confidence': 0.91,
         'kind': 'technical'},
        {'symbol': '000001', 'name': '平安银行', 'signal': 'buy', 'confidence': 0.55,
         'kind': 'technical'},
        {'symbol': '300750', 'name': None, 'signal': 'SELL', 'confidence': None,
         'kind': 'technical'},
    ]
    _patch(monkeypatch, rows, run={'run_id': '20260929', 'total': 3, 'ok': 3, 'failed': 0})
    from plugins.stock_analysis.morning_brief import build_morning_brief

    brief = build_morning_brief('2026-09-29')
    assert brief is not None
    assert brief['trade_date'] == '2026-09-29'
    assert brief['total'] == 3
    # 大小写归一到同一组
    assert len(brief['groups']['buy']) == 2
    # 组内按置信度降序
    assert brief['groups']['buy'][0]['symbol'] == '600519'
    # name 缺失时回落为代码，不出现空名称
    assert brief['groups']['sell'][0]['name'] == '300750'
    # 批量统计透传
    assert brief['run']['ok'] == 3
    # 置信度统一转 float（Decimal 不可 JSON 序列化）
    assert isinstance(brief['groups']['buy'][0]['confidence'], float)


def test_sql_uses_qmark_placeholder(monkeypatch):
    """项目契约：SQL 一律用 ? 占位符，不得用 %s。"""
    _patch(monkeypatch, [{'symbol': '600519', 'name': '贵州茅台', 'signal': 'BUY',
                          'confidence': 0.9, 'kind': 'technical'}])
    from plugins.stock_analysis.morning_brief import build_morning_brief

    captured = {}

    @contextlib.contextmanager
    def _spy_get_db(*_a, **_kw):
        conn = _FakeConn([{'symbol': '600519', 'name': '贵州茅台', 'signal': 'BUY',
                           'confidence': 0.9, 'kind': 'technical'}])
        yield conn
        captured['sql'] = conn.sql
        captured['params'] = conn.params

    monkeypatch.setattr(sa, 'get_db', _spy_get_db)
    build_morning_brief('2026-09-29')
    assert '%s' not in captured['sql']
    assert '?' in captured['sql']
    assert captured['params'] == ('2026-09-29',)
