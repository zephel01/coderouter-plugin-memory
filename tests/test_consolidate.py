"""Unit tests for consolidate.py — Ollama fact extraction."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import pytest

from coderouter_plugin_memory.config import MemoryConfig
from coderouter_plugin_memory.consolidate import (
    ConsolidateError,
    ConsolidateResult,
    _combine_buffer,
    _parse_facts,
    consolidate,
)
from coderouter_plugin_memory.store import append_buffer, append_facts, facts_count

# ──────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────


def _make_cfg(tmp_path: Path) -> MemoryConfig:
    return MemoryConfig(project="test", state_dir=str(tmp_path), min_buffer_entries=1)


def _mock_ollama(facts: list[str]) -> object:
    body = json.dumps({"response": json.dumps(facts)}).encode()

    class FakeResp:
        def read(self) -> bytes:
            return body

        def __enter__(self) -> FakeResp:
            return self

        def __exit__(self, *_: object) -> None:
            pass

    return FakeResp()


# ──────────────────────────────────────────────────────────────
# _parse_facts
# ──────────────────────────────────────────────────────────────


def test_parse_json_array() -> None:
    assert _parse_facts('["fact A", "fact B"]') == ["fact A", "fact B"]


def test_parse_with_preamble() -> None:
    assert _parse_facts('Here:\n["f1", "f2"]') == ["f1", "f2"]


def test_parse_bullet_fallback() -> None:
    facts = _parse_facts("- FastAPI\n* asyncio\n• Python 3.12")
    assert "FastAPI" in facts
    assert "asyncio" in facts


def test_parse_empty() -> None:
    assert _parse_facts("") == []


def test_parse_strips_whitespace() -> None:
    assert _parse_facts('["  trimmed  "]') == ["trimmed"]


# ──────────────────────────────────────────────────────────────
# _combine_buffer
# ──────────────────────────────────────────────────────────────


def test_combine_basic() -> None:
    entries = [{"text": "a"}, {"text": "b"}, {"text": "c"}]
    result = _combine_buffer(entries, max_chars=1000)
    assert "a" in result and "b" in result and "c" in result


def test_combine_latest_priority() -> None:
    entries = [{"text": "old" * 50}, {"text": "new"}]
    result = _combine_buffer(entries, max_chars=20)
    assert "new" in result


def test_combine_skips_missing_text() -> None:
    entries = [{"text": "ok"}, {"no_text": True}, {"text": "also ok"}]
    result = _combine_buffer(entries, max_chars=1000)
    assert "ok" in result and "also ok" in result


# ──────────────────────────────────────────────────────────────
# consolidate
# ──────────────────────────────────────────────────────────────


def test_empty_buffer_skipped(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    result = consolidate(cfg)
    assert result.skipped
    assert "empty" in result.reason


def test_consolidate_success(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    append_buffer(cfg.buffer_path(), "A" * 200, "test", "anthropic")
    with patch("urllib.request.urlopen", return_value=_mock_ollama(["fact 1", "fact 2"])):
        result = consolidate(cfg)
    assert not result.skipped
    assert result.written == 2
    assert not cfg.buffer_path().exists()
    assert facts_count(cfg.facts_path()) == 2


def test_consolidate_dry_run(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    append_buffer(cfg.buffer_path(), "B" * 200, "test", "anthropic")
    with patch("urllib.request.urlopen", return_value=_mock_ollama(["dry fact"])):
        result = consolidate(cfg, dry_run=True)
    assert result.dry_run
    assert "dry fact" in result.extracted
    assert cfg.buffer_path().exists()
    assert not cfg.facts_path().exists()


def test_consolidate_ollama_error(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    append_buffer(cfg.buffer_path(), "C" * 200, "test", "anthropic")
    with patch("urllib.request.urlopen", side_effect=URLError("refused")), pytest.raises(ConsolidateError, match="Ollama"):
        consolidate(cfg)


def test_consolidate_deduplication(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    append_facts(cfg.facts_path(), ["existing fact"], project="test")
    append_buffer(cfg.buffer_path(), "D" * 200, "test", "anthropic")
    with patch("urllib.request.urlopen", return_value=_mock_ollama(["existing fact", "new fact"])):
        result = consolidate(cfg)
    assert result.written == 1
    assert "new fact" in result.new_facts


# ──────────────────────────────────────────────────────────────
# ConsolidateResult.summary
# ──────────────────────────────────────────────────────────────


def test_summary_skipped() -> None:
    r = ConsolidateResult(skipped=True, reason="buffer is empty")
    assert "skipped" in r.summary() and "empty" in r.summary()


def test_summary_dry_run() -> None:
    r = ConsolidateResult(skipped=False, buffer_count=3, extracted=["f1"], new_facts=["f1"], dry_run=True)
    assert "dry-run" in r.summary()


def test_summary_success() -> None:
    r = ConsolidateResult(skipped=False, buffer_count=5, extracted=["f1"], new_facts=["f1"], written=1)
    assert "5" in r.summary() and "1" in r.summary()
