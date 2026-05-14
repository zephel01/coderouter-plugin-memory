"""Unit tests for store.py — JSONL-based buffer / facts / manual storage.

v0.4.0 で builtin backend は JSONL ベースに再設計。
旧 sqlite3 BuiltinBackend は廃止。
"""
from __future__ import annotations

from pathlib import Path

from coderouter_plugin_memory.store import (
    add_manual_fact,
    append_buffer,
    append_facts,
    buffer_count,
    build_inject_text,
    clear_buffer,
    existing_fact_texts,
    facts_count,
    read_buffer,
    read_facts,
    read_manual,
)

# ──────────────────────────────────────────────────────────────
# buffer
# ──────────────────────────────────────────────────────────────


def test_buffer_append_and_read(tmp_path: Path) -> None:
    p = tmp_path / "buffer.jsonl"
    append_buffer(p, "hello world", "test", "anthropic")
    entries = read_buffer(p)
    assert len(entries) == 1
    assert entries[0]["text"] == "hello world"
    assert entries[0]["provider"] == "anthropic"
    assert "ts" in entries[0]


def test_buffer_multiple(tmp_path: Path) -> None:
    p = tmp_path / "buffer.jsonl"
    for i in range(5):
        append_buffer(p, f"entry {i}", "test", "openai")
    assert len(read_buffer(p)) == 5


def test_buffer_read_missing(tmp_path: Path) -> None:
    assert read_buffer(tmp_path / "none.jsonl") == []


def test_buffer_clear_returns_count(tmp_path: Path) -> None:
    p = tmp_path / "buffer.jsonl"
    for i in range(3):
        append_buffer(p, f"t{i}", "p", "x")
    assert clear_buffer(p) == 3
    assert not p.exists()


def test_buffer_clear_missing(tmp_path: Path) -> None:
    assert clear_buffer(tmp_path / "none.jsonl") == 0


def test_buffer_count(tmp_path: Path) -> None:
    p = tmp_path / "buffer.jsonl"
    assert buffer_count(p) == 0
    append_buffer(p, "a", "p", "x")
    append_buffer(p, "b", "p", "x")
    assert buffer_count(p) == 2


def test_buffer_tolerates_broken_lines(tmp_path: Path) -> None:
    p = tmp_path / "buffer.jsonl"
    p.write_text(
        '{"text":"ok","ts":"2026","project":"x","provider":"y"}\nNOT_JSON\n'
        '{"text":"ok2","ts":"2026","project":"x","provider":"y"}\n'
    )
    assert len(read_buffer(p)) == 2


# ──────────────────────────────────────────────────────────────
# facts
# ──────────────────────────────────────────────────────────────


def test_facts_append_and_read(tmp_path: Path) -> None:
    p = tmp_path / "facts.jsonl"
    written = append_facts(p, ["fact A", "fact B"], "test")
    assert written == 2
    facts = read_facts(p)
    assert facts[0]["fact"] == "fact A"


def test_facts_max_limit(tmp_path: Path) -> None:
    p = tmp_path / "facts.jsonl"
    append_facts(p, [f"fact {i}" for i in range(20)], "test")
    facts = read_facts(p, max_facts=5)
    assert len(facts) == 5
    assert facts[-1]["fact"] == "fact 19"


def test_facts_existing_lowercase(tmp_path: Path) -> None:
    p = tmp_path / "facts.jsonl"
    append_facts(p, ["FastAPI を使用"], "test")
    assert "fastapi を使用" in existing_fact_texts(p)


def test_facts_strips_whitespace(tmp_path: Path) -> None:
    p = tmp_path / "facts.jsonl"
    written = append_facts(p, ["  trimmed  ", "", "   "], "p")
    assert written == 1
    assert read_facts(p)[0]["fact"] == "trimmed"


def test_facts_count(tmp_path: Path) -> None:
    p = tmp_path / "facts.jsonl"
    assert facts_count(p) == 0
    append_facts(p, ["a", "b", "c"], "p")
    assert facts_count(p) == 3


# ──────────────────────────────────────────────────────────────
# manual
# ──────────────────────────────────────────────────────────────


def test_manual_add_and_read(tmp_path: Path) -> None:
    p = tmp_path / "manual.md"
    add_manual_fact(p, "FastAPI を使用")
    add_manual_fact(p, "Python 3.12+")
    content = read_manual(p)
    assert "FastAPI" in content
    assert "Python 3.12+" in content


def test_manual_read_missing(tmp_path: Path) -> None:
    assert read_manual(tmp_path / "manual.md") == ""


def test_manual_bullet_prefix(tmp_path: Path) -> None:
    p = tmp_path / "manual.md"
    add_manual_fact(p, "test fact")
    assert any(ln.startswith("- ") for ln in p.read_text().splitlines())


# ──────────────────────────────────────────────────────────────
# build_inject_text
# ──────────────────────────────────────────────────────────────


def _make_facts(texts: list[str]) -> list[dict]:
    return [{"fact": t, "ts": "2026-01-01T00:00:00+00:00", "project": "test", "source": "consolidated"} for t in texts]


def test_inject_empty_returns_none() -> None:
    assert build_inject_text([], "", token_budget=2000, max_facts=10) is None


def test_inject_facts_only() -> None:
    result = build_inject_text(_make_facts(["fact A", "fact B"]), "", token_budget=2000, max_facts=10)
    assert result is not None
    assert "[Memory — past context]" in result
    assert "fact A" in result


def test_inject_manual_only() -> None:
    result = build_inject_text([], "- my note", token_budget=2000, max_facts=10)
    assert result is not None
    assert "[Memory — manual notes]" in result


def test_inject_both_sections() -> None:
    result = build_inject_text(_make_facts(["fact X"]), "- note Y", token_budget=2000, max_facts=10)
    assert result is not None
    assert "[Memory — manual notes]" in result
    assert "[Memory — past context]" in result


def test_inject_token_budget_trim() -> None:
    facts = _make_facts([f"fact {i} " * 20 for i in range(50)])
    result = build_inject_text(facts, "", token_budget=1, max_facts=50)
    # 非常に小さい budget → None になるか大幅に削られる
    if result is not None:
        assert len(result) < 200
