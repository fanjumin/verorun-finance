#!/usr/bin/env python3
"""基座单测：PII 判定与 user_profile_meta 四态（P0 DoD）。"""

import json

import pytest

from plugins._base.evolution import pii as evo_pii
from plugins._base.evolution import sql as evo_sql


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def execute(self, *_a, **_k):
        return _FakeCursor(self._row)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _patch_get_db(monkeypatch, row_or_exc):
    if isinstance(row_or_exc, Exception):
        def _get_db():
            raise row_or_exc
    else:
        def _get_db():
            return _FakeConn(row_or_exc)
    import agent_matrix.models as m
    monkeypatch.setattr(m, 'get_db', _get_db, raising=False)


# ── contains_pii ────────────────────────────────────────────────────
@pytest.mark.parametrize('text', [
    'password: abc123',
    'api_key=deadbeef',
    '联系手机13800138000尾号',
    '身份证11010519491231002X结束',
])
def test_contains_pii_hits(text):
    assert evo_pii.contains_pii(text) is True


@pytest.mark.parametrize('text', [
    'v0.62.1 发布',
    '端口 8080',
    '订单号 20240101123456789012',
    '普通中文文本，无敏感信息',
    '',
    None,
])
def test_contains_pii_no_false_positive(text):
    assert evo_pii.contains_pii(text) is False


# ── user_profile_meta 四态 ───────────────────────────────────────────
def test_meta_with_dict_row(monkeypatch):
    _patch_get_db(monkeypatch, {'meta': {'memory_opt_in': True}})
    assert evo_sql.user_profile_meta('u1') == {'memory_opt_in': True}


def test_meta_with_json_string(monkeypatch):
    _patch_get_db(monkeypatch, {'meta': json.dumps({'memory_opt_in': False})})
    assert evo_sql.user_profile_meta('u1') == {'memory_opt_in': False}


def test_meta_no_row_returns_empty(monkeypatch):
    _patch_get_db(monkeypatch, None)
    assert evo_sql.user_profile_meta('u1') == {}


def test_meta_bad_json_returns_none(monkeypatch):
    _patch_get_db(monkeypatch, {'meta': 'not-json'})
    assert evo_sql.user_profile_meta('u1') is None


def test_meta_read_error_returns_none(monkeypatch):
    _patch_get_db(monkeypatch, RuntimeError('db down'))
    assert evo_sql.user_profile_meta('u1') is None
