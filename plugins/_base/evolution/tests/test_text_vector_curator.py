#!/usr/bin/env python3
"""基座单测：text / vector / curator（P1a DoD）。"""

import hashlib

import pytest

from plugins._base.evolution import text as evo_text
from plugins._base.evolution import vector as evo_vector
from plugins._base.evolution import curator as evo_curator


# ── keywords ────────────────────────────────────────────────────────
def test_keywords_cjk_and_latin():
    assert set(evo_text.keywords('Hello 世界 order-9')) == {'世界', 'hello', 'order'}


def test_keywords_excludes_digits():
    # 与 memory_engine 口径一致：latin 词为 [a-z]{2,}，不含数字。
    assert '12345' not in evo_text.keywords('abc12345 xyz')


def test_keywords_dedup_and_truncate():
    text = ' '.join(f'w{i:02d}' for i in range(20)) + ' w00 w00'
    out = evo_text.keywords(text)
    assert len(out) <= 12
    assert len(out) == len(set(out))


def test_keywords_empty():
    assert evo_text.keywords('') == []


# ── record_hash ─────────────────────────────────────────────────────
def test_record_hash_matches_sha256():
    assert evo_text.record_hash('u1', 'hello') == hashlib.sha256(
        b'u1|hello').hexdigest()


def test_record_hash_differs_by_owner_and_content():
    assert evo_text.record_hash('u1', 'x') != evo_text.record_hash('u2', 'x')
    assert evo_text.record_hash('u1', 'x') != evo_text.record_hash('u1', 'y')


# ── vector_literal ──────────────────────────────────────────────────
def test_vector_literal_formats_as_float():
    assert evo_vector.vector_literal([0, 0.5, 1]) == '[0.0,0.5,1.0]'


# ── embedding_literal ───────────────────────────────────────────────
class _FakeEmbed:
    _ready = True
    _vec = [0.1, 0.2]

    def __init__(self, *_a, **_k):
        pass

    def is_ready(self):
        return _FakeEmbed._ready

    def embed(self, _text):
        if isinstance(_FakeEmbed._vec, Exception):
            raise _FakeEmbed._vec
        return _FakeEmbed._vec


@pytest.fixture()
def fake_embedding(monkeypatch):
    import plugins._base.embeddings as base_emb
    _FakeEmbed._ready, _FakeEmbed._vec = True, [0.1, 0.2]
    monkeypatch.setattr(base_emb, 'EmbeddingService', _FakeEmbed)
    return _FakeEmbed


def test_embedding_literal_ok(fake_embedding):
    assert evo_vector.embedding_literal('hi') == '[0.1,0.2]'


def test_embedding_literal_not_ready_returns_none(fake_embedding):
    fake_embedding._ready = False
    assert evo_vector.embedding_literal('hi') is None


def test_embedding_literal_empty_vector_returns_none(fake_embedding):
    fake_embedding._vec = []
    assert evo_vector.embedding_literal('hi') is None


def test_embedding_literal_swallows_exception(fake_embedding):
    fake_embedding._vec = RuntimeError('boom')
    assert evo_vector.embedding_literal('hi') is None


# ── load_curator_config ─────────────────────────────────────────────
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


def _patch_models(monkeypatch, row, roles=None):
    import agent_matrix.models as m
    monkeypatch.setattr(m, 'get_db', lambda: _FakeConn(row), raising=False)
    if roles is not None:
        monkeypatch.setattr(
            m, 'resolve_agent_roles', lambda _pid, _meta: roles, raising=False)


def _write_prompt(tmp_path, body='curator prompt'):
    p = tmp_path / 'p.md'
    p.write_text(body, encoding='utf-8')
    return str(p)


def test_load_curator_config_success(monkeypatch, tmp_path):
    _patch_models(monkeypatch, {'provider_model_id': 7, 'slug': 'athena'},
                  roles=['athena'])
    cfg = evo_curator.load_curator_config(
        'memory_engine', 'athena', _write_prompt(tmp_path), 'memory_curator')
    assert cfg['name'] == 'memory_curator'
    assert cfg['system_prompt'] == 'curator prompt'
    assert cfg['provider_model_id'] == 7


def test_load_curator_config_prompt_missing(monkeypatch, tmp_path):
    _patch_models(monkeypatch, {'slug': 'athena'}, roles=['athena'])
    assert evo_curator.load_curator_config(
        'memory_engine', 'athena', str(tmp_path / 'nope.md'), 'x') == {}


def test_load_curator_config_no_row(monkeypatch, tmp_path):
    _patch_models(monkeypatch, None, roles=['athena'])
    assert evo_curator.load_curator_config(
        'memory_engine', 'athena', _write_prompt(tmp_path), 'x') == {}


def test_load_curator_config_old_kernel_fallback(monkeypatch, tmp_path):
    # 旧内核无 resolve_agent_roles → 回退 [agent_role]，仍能取到行。
    import agent_matrix.models as m
    monkeypatch.delattr(m, 'resolve_agent_roles', raising=False)
    _patch_models(monkeypatch, {'slug': 'athena'})
    cfg = evo_curator.load_curator_config(
        'cogevolution_substrate', 'athena', _write_prompt(tmp_path), 'evolution_curator')
    assert cfg['name'] == 'evolution_curator'
