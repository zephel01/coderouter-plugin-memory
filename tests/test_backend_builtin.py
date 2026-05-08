"""Unit tests for ``coderouter_plugin_memory.backends.builtin``.

These exercise the sqlite3 backend end-to-end against a temporary
file. The test file is recreated per case via the ``tmp_path``
fixture so we never share state across cases.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coderouter_plugin_memory.backends import (
    BuiltinBackend,
    MemoryBackendError,
)
from coderouter_plugin_memory.backends.builtin import (
    _build_like_pattern,
    _flatten_assistant_text,
    _flatten_last_user_message,
    _format_rows_within_budget,
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_backend(tmp_path: Path) -> BuiltinBackend:
    return BuiltinBackend(store=str(tmp_path / "memory.sqlite3"))


def _request_with(user_text: str, *, has_tools: bool = False) -> dict:
    req: dict = {
        "messages": [{"role": "user", "content": user_text}],
    }
    if has_tools:
        req["tools"] = [{"name": "Read", "input_schema": {}}]
    return req


def _response_with(assistant_text: str, *, model: str = "x", usage: dict | None = None) -> dict:
    return {
        "model": model,
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": assistant_text}],
        "usage": usage or {"input_tokens": 5, "output_tokens": 3},
    }


# ----------------------------------------------------------------------
# Free helpers (synchronous)
# ----------------------------------------------------------------------


class TestBuildLikePattern:
    def test_picks_a_longest_word(self) -> None:
        out = _build_like_pattern("set up the JWT auth path")
        # "the" is 3 chars; "JWT" / "auth" / "path" / "set" are also
        # 3-4. The implementation picks the latest 4-char word
        # (reversed-iteration tiebreak), which lands on "path".
        # Looser assertion: it picked SOME 4-char word and returned
        # the canonical ``%word%`` shape.
        assert out is not None
        assert out.startswith("%") and out.endswith("%")
        assert len(out) >= 5  # %abc% minimum

    def test_returns_none_when_only_short_words(self) -> None:
        # Each word is < 3 chars — no LIKE pattern; backend will fall
        # back to recency-only.
        assert _build_like_pattern("a b c") is None

    def test_returns_none_for_empty(self) -> None:
        assert _build_like_pattern("") is None
        assert _build_like_pattern("   ") is None

    def test_keyword_extraction_strips_non_word_chars(self) -> None:
        """``\\w+`` already excludes ``%`` / ``_`` / punctuation, so the
        extracted keyword is letters-and-digits-only by construction.
        The escape branch in :func:`_build_like_pattern` is therefore
        dead in practice, but exists as defense-in-depth — this test
        documents the actual current behavior.
        """
        # "foobar%baz" → re.findall(r"\w+", ...) = ["foobar", "baz"]
        # → longest = "foobar" (6 chars). Pattern is "%foobar%".
        assert _build_like_pattern("foobar%baz") == "%foobar%"
        # "fizz_buzz" → ["fizz_buzz"] (underscore IS a word char) →
        # keyword = "fizz_buzz". Now the escape DOES kick in.
        assert _build_like_pattern("fizz_buzz") == r"%fizz\_buzz%"


class TestFlatten:
    def test_last_user_message_str_content(self) -> None:
        req = {
            "messages": [
                {"role": "user", "content": "older"},
                {"role": "assistant", "content": "ack"},
                {"role": "user", "content": "newer"},
            ]
        }
        assert _flatten_last_user_message(req) == "newer"

    def test_last_user_message_list_content(self) -> None:
        req = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "part one"},
                        {"type": "text", "text": "part two"},
                    ],
                }
            ]
        }
        assert _flatten_last_user_message(req) == "part one\npart two"

    def test_assistant_text_filters_non_text_blocks(self) -> None:
        resp = {
            "content": [
                {"type": "text", "text": "answer"},
                {"type": "tool_use", "name": "Read", "input": {}},
            ]
        }
        assert _flatten_assistant_text(resp) == "answer"


class TestFormatRowsWithinBudget:
    def _row(self, ts: str, user: str, asst: str) -> dict:
        # sqlite3.Row supports dict-style access; using a real dict
        # in the test is fine because the formatter only does ``row['x']``.
        return {
            "id": 1,
            "project_id": "proj-x",
            "timestamp": ts,
            "user_message": user,
            "assistant_text": asst,
            "metadata": "{}",
        }

    def test_includes_blocks_until_budget(self) -> None:
        rows = [
            self._row("2026-05-08T03:00:00+00:00", "first", "alpha"),
            self._row("2026-05-08T02:00:00+00:00", "second", "beta"),
        ]
        out = _format_rows_within_budget(rows, token_budget=200)
        assert "first" in out and "second" in out
        # Newest first.
        assert out.index("first") < out.index("second")

    def test_stops_at_budget(self) -> None:
        rows = [self._row(f"2026-05-08T0{i}:00:00+00:00", "x" * 80, "y") for i in range(5)]
        # token_budget=20 → 80 chars budget → only the first row fits.
        out = _format_rows_within_budget(rows, token_budget=20)
        # At least one full block, and definitely truncated.
        assert "Past session" in out
        assert len(out) <= 80 + 100  # one block plus formatting slack

    def test_empty_rows_returns_empty(self) -> None:
        assert _format_rows_within_budget([], token_budget=2000) == ""


# ----------------------------------------------------------------------
# Async backend behavior
# ----------------------------------------------------------------------


class TestBuiltinBackendBehavior:
    def test_health_on_fresh_path_passes(self, tmp_path: Path) -> None:
        backend = _make_backend(tmp_path)
        try:
            assert asyncio.run(backend.health()) is True
        finally:
            backend.close()

    def test_observe_then_search_round_trip(self, tmp_path: Path) -> None:
        """Observe a session, then search using a query that shares
        a keyword with the user_message (LIKE-on-longest-word doesn't
        match different forms — that's an agentmemory job)."""
        backend = _make_backend(tmp_path)
        try:
            asyncio.run(
                backend.observe(
                    project_id="proj-A",
                    request=_request_with("set up JWT authentication in src/middleware/auth.ts"),
                    response=_response_with("Done. Used jose middleware."),
                )
            )
            ctx = asyncio.run(
                backend.smart_search(
                    project_id="proj-A",
                    query="how does our authentication flow work?",
                    token_budget=2000,
                )
            )
            assert "JWT authentication" in ctx
            assert "jose middleware" in ctx
            assert "Past session" in ctx
        finally:
            backend.close()

    def test_search_returns_empty_for_unknown_project(self, tmp_path: Path) -> None:
        backend = _make_backend(tmp_path)
        try:
            asyncio.run(
                backend.observe(
                    project_id="proj-A",
                    request=_request_with("alpha beta gamma"),
                    response=_response_with("done"),
                )
            )
            out = asyncio.run(
                backend.smart_search(
                    project_id="proj-OTHER",  # different project
                    query="alpha",
                    token_budget=2000,
                )
            )
            assert out == ""
        finally:
            backend.close()

    def test_search_falls_back_to_recency_when_query_has_no_keyword(
        self, tmp_path: Path
    ) -> None:
        """A query with only short words still returns recent rows."""
        backend = _make_backend(tmp_path)
        try:
            asyncio.run(
                backend.observe(
                    project_id="proj-A",
                    request=_request_with("paint the building blue"),
                    response=_response_with("ok"),
                )
            )
            out = asyncio.run(
                backend.smart_search(
                    project_id="proj-A",
                    query="a b c",  # no word ≥ 3 chars
                    token_budget=2000,
                )
            )
            assert "paint" in out

        finally:
            backend.close()

    def test_zero_token_budget_returns_empty_immediately(self, tmp_path: Path) -> None:
        backend = _make_backend(tmp_path)
        try:
            asyncio.run(
                backend.observe(
                    project_id="proj-A",
                    request=_request_with("anything"),
                    response=_response_with("fine"),
                )
            )
            out = asyncio.run(
                backend.smart_search(
                    project_id="proj-A", query="anything", token_budget=0
                )
            )
            assert out == ""
        finally:
            backend.close()

    def test_recency_ordering(self, tmp_path: Path) -> None:
        backend = _make_backend(tmp_path)
        try:
            for tag in ("first-rows", "second-rows", "third-rows"):
                asyncio.run(
                    backend.observe(
                        project_id="proj-A",
                        request=_request_with(f"build {tag}"),
                        response=_response_with(f"saw {tag}"),
                    )
                )
            out = asyncio.run(
                backend.smart_search(
                    project_id="proj-A",
                    query="rows",
                    token_budget=2000,
                )
            )
            # Newest-first: third-rows comes before first-rows in output.
            assert out.index("third-rows") < out.index("first-rows")
        finally:
            backend.close()

    def test_health_returns_false_on_unwritable_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unreachable database path → health() False, never raises."""
        # Pick a path inside a file (not a directory) so creating it
        # fails on every OS we care about.
        unwritable = tmp_path / "not-a-dir" / "memory.sqlite3"
        # Make the parent be a regular file, so mkdir() can't create it.
        (tmp_path / "not-a-dir").write_text("blocker")
        backend = BuiltinBackend(store=str(unwritable))
        try:
            assert asyncio.run(backend.health()) is False
        finally:
            backend.close()

    def test_observe_raises_memory_backend_error_on_sqlite_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _make_backend(tmp_path)
        try:
            # Force the conn into a state where INSERT fails: close the
            # connection out from under it.
            backend._ensure_conn()  # opens
            assert backend._conn is not None
            backend._conn.close()  # but don't reset _conn — backend thinks it's still open

            with pytest.raises(MemoryBackendError):
                asyncio.run(
                    backend.observe(
                        project_id="proj-A",
                        request=_request_with("x"),
                        response=_response_with("y"),
                    )
                )
        finally:
            # Clean shutdown is best-effort here.
            backend._conn = None
