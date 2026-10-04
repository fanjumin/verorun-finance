#!/usr/bin/env python3
"""等价性回归：两插件隐私/引擎门在委托基座后，行为与改造前逐项一致。"""

import pytest

import agent_matrix.models as am_models


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
    monkeypatch.setattr(am_models, 'get_db', _get_db, raising=False)


# ── memory_engine：user_opted_in（fail-open + 空 owner fail-closed）──
from plugins.memory_engine.prompt_injector import user_opted_in as me_opted_in


@pytest.mark.parametrize('default', [True, False])
def test_me_opted_in_empty_user_is_false(monkeypatch, default):
    # 空 owner 必须 fail-closed，且不应触达 DB。
    _patch_get_db(monkeypatch, RuntimeError('should not be called'))
    assert me_opted_in('', {'memory_opt_in_default': default}) is False


@pytest.mark.parametrize('default', [True, False])
def test_me_opted_in_no_row_uses_default(monkeypatch, default):
    _patch_get_db(monkeypatch, None)
    assert me_opted_in('u1', {'memory_opt_in_default': default}) is default


@pytest.mark.parametrize('default', [True, False])
def test_me_opted_in_read_error_fails_open(monkeypatch, default):
    _patch_get_db(monkeypatch, RuntimeError('db down'))
    assert me_opted_in('u1', {'memory_opt_in_default': default}) is default


def test_me_opted_in_explicit_false(monkeypatch):
    _patch_get_db(monkeypatch, {'meta': {'memory_opt_in': False}})
    assert me_opted_in('u1', {'memory_opt_in_default': True}) is False


def test_me_opted_in_explicit_true(monkeypatch):
    _patch_get_db(monkeypatch, {'meta': {'memory_opt_in': True}})
    assert me_opted_in('u1', {'memory_opt_in_default': False}) is True


# ── CES：user_chose_ces / user_opted_in / pii_blocked ────────────────
from plugins.cogevolution_substrate.services import extractor as ces_ex

_CES_FORCE_DEFAULT = {
    'coexistence_mode': 'force_default',
    'default_engine': 'cogevolution_substrate',
}


def test_ces_chose_no_row_force_default_admits(monkeypatch):
    # 无档案 → {}（非 None）→ 继续走 force_default 分支（与改造前一致）。
    _patch_get_db(monkeypatch, None)
    assert ces_ex.user_chose_ces('u1', _CES_FORCE_DEFAULT) is True


def test_ces_chose_no_row_remind_denies(monkeypatch):
    _patch_get_db(monkeypatch, None)
    assert ces_ex.user_chose_ces(
        'u1', {'coexistence_mode': 'remind', 'default_engine': 'cogevolution_substrate'}) is False


def test_ces_chose_read_error_denies(monkeypatch):
    _patch_get_db(monkeypatch, RuntimeError('db down'))
    assert ces_ex.user_chose_ces('u1', _CES_FORCE_DEFAULT) is False


def test_ces_chose_explicit_selection(monkeypatch):
    _patch_get_db(monkeypatch, {'meta': {'cognitive_engine': 'cogevolution_substrate'}})
    assert ces_ex.user_chose_ces('u1', {}) is True


def test_ces_opted_in_no_row_uses_default(monkeypatch):
    _patch_get_db(monkeypatch, None)
    assert ces_ex.user_opted_in('u1', {'memory_opt_in_default': True}) is True
    assert ces_ex.user_opted_in('u1', {'memory_opt_in_default': False}) is False


def test_ces_opted_in_read_error_fails_closed(monkeypatch):
    _patch_get_db(monkeypatch, RuntimeError('db down'))
    assert ces_ex.user_opted_in('u1', {'memory_opt_in_default': True}) is False


def test_ces_pii_blocked():
    assert ces_ex.MemoryExtractor.pii_blocked('api_key=secret') is True
    assert ces_ex.MemoryExtractor.pii_blocked('v0.62.1 发布') is False


# ── P1a 等价性：memory_engine 三处 keywords 委托基座后与基座同输出 ─────
from plugins._base.evolution import text as evo_text
from plugins.memory_engine.services.extractor import MemoryExtractor as _MeExtractor
from plugins.memory_engine.services.reflexion import _keywords as _me_reflexion_keywords
from plugins.memory_engine.services.sedimentation import (
    SedimentationService as _MeSedimentation,
)

_KEYS_SAMPLE = 'Hello 世界 order-9 记忆系统 api123'


def test_me_keywords_delegation_equivalent():
    expect = set(evo_text.keywords(_KEYS_SAMPLE))
    assert set(_MeExtractor._keywords(_KEYS_SAMPLE)) == expect
    assert set(_me_reflexion_keywords(_KEYS_SAMPLE)) == expect
    assert set(_MeSedimentation._naive_keywords(_KEYS_SAMPLE)) == expect


# ── P1a 回归：CES 保留自身 keywords / content_hash（未被基座收敛）─────
def test_ces_keywords_kept_divergent():
    sample = 'Hello 世界 order-9 12345'
    ces_words = list(ces_ex.MemoryExtractor._keywords(sample))
    assert '12345' in ces_words           # CES 混排正则含纯数字 token
    assert '12345' not in evo_text.keywords(sample)  # 基座 me 版不含


def test_ces_content_hash_kept():
    import hashlib
    from plugins.cogevolution_substrate.services.curation_store import content_hash
    normalized = ' '.join('A  B'.strip().lower().split())
    assert content_hash('user', 'u1', 'd1', 'A  B') == hashlib.sha256(
        '|'.join(('user', 'u1', 'd1', normalized)).encode('utf-8')).hexdigest()
