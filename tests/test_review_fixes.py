"""Regression tests for the 2026-07-04 review-driven fixes.

- C-1: MemoryPlugin must ignore unknown (pre-0.4) config keys, not crash.
- M-9: empty `project` must not collapse project_dir onto state_dir.
- M-12: a held consolidate lock must make consolidate() skip.
- L-6: build_inject_text must stay within budget, keeping newest facts.
- L-8: unknown response types must not be persisted.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coderouter_plugin_memory.config import MemoryConfig, _safe_name
from coderouter_plugin_memory.consolidate import consolidate
from coderouter_plugin_memory.plugin import MemoryPlugin, _extract_response_text
from coderouter_plugin_memory.store import build_inject_text


# --- M-9: empty project fallback --------------------------------------------

def test_safe_name_empty_falls_back_to_default():
    assert _safe_name("") == "default"
    assert _safe_name("   ")  # whitespace → non-empty ("___"), not collapsed
    # A path-traversal-y name is neutralized, never empty and never a separator.
    assert _safe_name("../..") and "/" not in _safe_name("../..")


def test_empty_project_does_not_collapse_state_dir(tmp_path: Path):
    cfg = MemoryConfig(project="", state_dir=str(tmp_path))
    # __post_init__ normalizes to "default".
    assert cfg.project == "default"
    pdir = cfg.project_dir()
    assert pdir == tmp_path / "default"
    assert pdir != tmp_path  # not collapsed onto the root


# --- C-1: unknown keys ignored ----------------------------------------------

def test_plugin_ignores_stale_backend_keys(tmp_path: Path):
    # A pre-0.4 config carrying removed keys must not raise TypeError.
    plugin = MemoryPlugin(
        project="test",
        state_dir=str(tmp_path),
        backend="agentmemory",
        endpoint="http://localhost:3111",
        secret_env="AGENTMEMORY_SECRET",
        circuit_breaker_threshold=5,
    )
    assert plugin._cfg.project == "test"


# --- M-12: consolidate lock -------------------------------------------------

def test_consolidate_skips_when_locked(tmp_path: Path):
    cfg = MemoryConfig(project="test", state_dir=str(tmp_path), min_buffer_entries=1)
    pdir = cfg.project_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / ".consolidate.lock").touch()  # simulate a concurrent run
    result = consolidate(cfg)
    assert result.skipped
    assert "lock" in result.reason.lower()


def test_consolidate_timeout_is_configurable():
    assert MemoryConfig(project="p").consolidate_timeout_s == 60
    assert MemoryConfig(project="p", consolidate_timeout_s=5).consolidate_timeout_s == 5


# --- L-6: budget trimming ---------------------------------------------------

def test_inject_budget_keeps_newest_within_limit():
    facts = [{"fact": f"fact number {i} " + "x" * 20} for i in range(50)]
    # Tiny budget forces trimming.
    text = build_inject_text(facts=facts, manual="", token_budget=30, max_facts=50)
    assert text is not None
    assert len(text) <= 30 * 4
    # The most-recent fact is retained; an old one is dropped.
    assert "fact number 49" in text
    assert "fact number 0 " not in text


def test_inject_none_when_empty():
    assert build_inject_text(facts=[], manual="", token_budget=100, max_facts=10) is None


# --- L-8: unknown response type not persisted -------------------------------

def test_extract_unknown_type_returns_empty():
    class Weird:
        def __repr__(self) -> str:  # pragma: no cover - repr must not leak
            return "secret-token=abc123"

    assert _extract_response_text(Weird()) == ""


def test_extract_plain_str_still_supported():
    assert "hello" in _extract_response_text("hello world")
