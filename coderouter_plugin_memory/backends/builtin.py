"""Builtin sqlite3 memory backend — minimal, stdlib-only.

This is the **floor** of the memory experience: keyword LIKE search,
recency-weighted ordering, project namespacing, JSONL-flavored
metadata. No embeddings, no consolidation, no graph hops. Users who
want quality search bring up an :class:`AgentMemoryBackend` (R@5
95.2%, 4-tier consolidation, etc.); ``BuiltinBackend`` exists so
small-stakes setups can run with zero extra processes and zero new
deps beyond ``sqlite3`` (which ships with Python).

Design choices
==============

- **Schema is one table.** Append-only ``memory`` rows with
  ``(project_id, timestamp, user_message, assistant_text, metadata)``.
  An index on ``(project_id, timestamp DESC)`` makes the recency-
  ordered project read O(log N + k) for k results.

- **WAL mode + check_same_thread=False.** sqlite3's WAL journal
  permits concurrent readers + a single writer, which is exactly the
  workload the plugin produces (one observe per request, many
  searches per request from concurrent tabs of the same agent).
  ``check_same_thread=False`` lets us share a connection across the
  asyncio thread pool that backends a CPython server.

- **Graceful degradation.** Disk-write failures (full disk, locked
  file, permissions) are logged and swallowed. The plugin's value
  proposition is "transparent memory"; making the wire layer fail
  hard because the disk is full would defeat that.

- **No fancy ranking.** Search is "rows whose user_message OR
  assistant_text matches LIKE %query%, newest first, take top-k that
  fit the token budget". A real BM25 ranker would be 200 LOC of
  Python and still lose to agentmemory's HNSW. Keep the contract
  honest: this is a recency-biased keyword search.

- **No automatic TTL pruning.** A future call may add it; for now
  the table grows without bound. A production user who wants
  bounded growth either (a) runs ``VACUUM`` + a manual ``DELETE``
  cron, or (b) uses agentmemory which handles that themselves.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coderouter_plugin_memory.backends.base import MemoryBackendError

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Schema + tuning
# ----------------------------------------------------------------------

# One table, append-only. We keep user_message and assistant_text in
# separate columns so future search refinements (e.g. weight matches
# in the user side higher) are a one-line change.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    user_message    TEXT    NOT NULL,
    assistant_text  TEXT    NOT NULL,
    metadata        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_recent
    ON memory (project_id, timestamp DESC);
"""

# WAL gives us concurrent reads while observe() writes in the
# background. The pragma persists in the database file so subsequent
# opens inherit it.
_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
)

# Default ceiling on rows scanned per smart_search call. Generous
# enough that the LIKE-then-sort path is fine for tens of thousands
# of rows, low enough to bound the worst case.
_SEARCH_SCAN_LIMIT = 500

# Char→token estimator constant. Same heuristic the v2.0-F context
# budget guard uses upstream — keep them aligned so the plugin's
# internal accounting matches the engine's external view.
_CHARS_PER_TOKEN = 4


# ----------------------------------------------------------------------
# Public class
# ----------------------------------------------------------------------


class BuiltinBackend:
    """Minimum-viable memory backend backed by a single sqlite3 file.

    Args:
        store: filesystem path to the sqlite3 file. Created (along
            with parent directories) on first use. Defaults to
            ``~/.coderouter/memory.sqlite3`` so the plugin works
            with zero config in the common case.
    """

    name = "builtin"

    def __init__(
        self,
        *,
        store: str | None = None,
        **_kwargs: Any,  # absorb backend-shared kwargs the plugin passes
    ) -> None:
        self._path = Path(store).expanduser() if store else _default_store()
        # Lazy connection: opened on first use so __init__ never
        # touches disk. That keeps the plugin loadable in CI / dry-run
        # contexts where the home directory may be unwritable.
        self._conn: sqlite3.Connection | None = None
        # sqlite3 connections are technically not async, so we serialize
        # writes through a stdlib lock. The contention window per call
        # is microseconds — too small to need an asyncio.Lock.
        self._write_lock = threading.Lock()
        # Once-flag for schema bootstrap. Avoids repeated CREATE IF
        # NOT EXISTS on the hot path.
        self._initialized = False

    # ------------------------------------------------------------------
    # MemoryBackend Protocol surface
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        """Open the database (or confirm it's open) and read one row.

        Treated as healthy if the connection works and the schema is
        in place. We don't actually require any rows to exist — a
        fresh install is healthy too.
        """
        try:
            conn = self._ensure_conn()
            with contextlib.closing(conn.cursor()) as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception as exc:
            # health() never raises. The plugin caller checks this to
            # decide whether to degrade to NullBackend.
            logger.warning(
                "memory-backend-unhealthy",
                extra={
                    "backend": self.name,
                    "store": str(self._path),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
            )
            return False

    async def smart_search(
        self,
        *,
        project_id: str,
        query: str,
        token_budget: int,
    ) -> str:
        """Return a recency-ordered, keyword-filtered context block.

        Format::

            [Past session 2026-05-08T03:14Z]
            user: "..."
            assistant: "..."

            [Past session 2026-05-08T03:11Z]
            ...

        Empty string when no rows match — that's not an error. The
        result is truncated so the total length stays within
        roughly ``token_budget * _CHARS_PER_TOKEN`` characters.

        Raises:
            MemoryBackendError: if the database can't be queried at
            all (programming bug, corrupted file, etc.).
        """
        # token_budget == 0 is equivalent to "don't inject anything".
        # Short-circuit without touching the DB.
        if token_budget <= 0:
            return ""

        # Sanitize query for LIKE: collapse whitespace, take a single
        # contiguous token if the user message is long. We're not
        # doing tokenization; this is a heuristic to keep the LIKE
        # parameter useful.
        like_param = _build_like_pattern(query)

        try:
            rows = await _to_thread(
                self._search_rows, project_id, like_param
            )
        except sqlite3.Error as exc:
            raise MemoryBackendError(
                f"sqlite3 error during smart_search: {exc}"
            ) from exc

        if not rows:
            return ""

        return _format_rows_within_budget(rows, token_budget)

    async def observe(
        self,
        *,
        project_id: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        """Append one row summarizing this request/response pair.

        We don't store the full request/response — that's both a
        privacy issue and a disk-space issue. Instead we keep:

        - The last user message (as the search target).
        - The assistant's flattened text response.
        - A small metadata JSON blob (provider, token counts, etc.)
          for future use by tooling that wants to filter / audit.

        Failures are converted to :class:`MemoryBackendError`. The
        plugin's Observer hook catches and silently logs that
        (Observer is best-effort, never blocks engine response).
        """
        user_msg = _flatten_last_user_message(request)
        assistant_text = _flatten_assistant_text(response)
        metadata = _extract_observe_metadata(request, response)

        timestamp = datetime.now(UTC).isoformat(timespec="seconds")

        try:
            await _to_thread(
                self._insert_row,
                project_id,
                timestamp,
                user_msg,
                assistant_text,
                json.dumps(metadata, ensure_ascii=False),
            )
        except sqlite3.Error as exc:
            raise MemoryBackendError(
                f"sqlite3 error during observe: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal — DB lifecycle + row I/O
    # ------------------------------------------------------------------

    def _ensure_conn(self) -> sqlite3.Connection:
        """Open the connection on first use, then reuse it."""
        if self._conn is not None:
            return self._conn

        # Create parent directory if needed. Done here, not in
        # __init__, so a misconfigured ``store`` path can be flagged
        # at first health() call instead of import time.
        self._path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,  # autocommit so we don't have to manage txns
        )
        for pragma in _PRAGMAS:
            conn.execute(pragma)

        if not self._initialized:
            conn.executescript(_SCHEMA_SQL)
            self._initialized = True

        self._conn = conn
        return conn

    def _search_rows(
        self,
        project_id: str,
        like_param: str | None,
    ) -> list[sqlite3.Row]:
        conn = self._ensure_conn()
        conn.row_factory = sqlite3.Row

        with contextlib.closing(conn.cursor()) as cur:
            # ``id DESC`` is appended as a deterministic tiebreaker
            # for rows that landed within the same ``timespec=seconds``
            # bucket. Without it, two observe() calls within one
            # second can swap order across runs — which both confuses
            # tests and causes user-visible flicker on rapid sessions.
            if like_param is None:
                # Empty / unmatched query: return the most recent
                # rows for the project as a fallback. Better than
                # returning nothing — at least the user sees recent
                # context.
                cur.execute(
                    "SELECT * FROM memory "
                    "WHERE project_id = ? "
                    "ORDER BY timestamp DESC, id DESC "
                    "LIMIT ?",
                    (project_id, _SEARCH_SCAN_LIMIT),
                )
            else:
                cur.execute(
                    "SELECT * FROM memory "
                    "WHERE project_id = ? "
                    "  AND (user_message LIKE ? OR assistant_text LIKE ?) "
                    "ORDER BY timestamp DESC, id DESC "
                    "LIMIT ?",
                    (project_id, like_param, like_param, _SEARCH_SCAN_LIMIT),
                )
            return cur.fetchall()

    def _insert_row(
        self,
        project_id: str,
        timestamp: str,
        user_message: str,
        assistant_text: str,
        metadata_json: str,
    ) -> None:
        conn = self._ensure_conn()
        with self._write_lock, contextlib.closing(conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO memory "
                "(project_id, timestamp, user_message, assistant_text, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, timestamp, user_message, assistant_text, metadata_json),
            )

    def close(self) -> None:
        """Close the underlying connection. Safe to call multiple times.

        Tests use this between cases to drop the file lock; production
        callers don't have to — Python finalization closes the conn
        on interpreter shutdown.
        """
        if self._conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None
            self._initialized = False


# ----------------------------------------------------------------------
# Helpers (free functions — easy to unit-test in isolation)
# ----------------------------------------------------------------------


def _default_store() -> Path:
    """``~/.coderouter/memory.sqlite3`` — keeps memory near other CodeRouter state."""
    return Path.home() / ".coderouter" / "memory.sqlite3"


def _build_like_pattern(query: str) -> str | None:
    """Build a LIKE parameter from a free-text query.

    Strategy: take the longest run of word characters in ``query``
    and wrap it in ``%...%``. That avoids matching nothing when the
    user message is "Now do that for the auth path too" (a literal
    LIKE on the whole string only matches messages that say exactly
    that). The longest word is usually the noun the user cares
    about — "auth" in this example.

    Returns None when the query has no word characters at all (the
    caller treats that as a "fall back to recent rows" signal).
    """
    import re

    words = re.findall(r"\w+", query)
    if not words:
        return None
    # Pick the longest word; ties broken by latest occurrence so a
    # follow-up like "what's the rate-limiting spec?" prefers
    # "rate-limiting" over an earlier word of the same length.
    keyword = max(reversed(words), key=len)
    if len(keyword) < 3:
        # Single-letter / two-letter "words" like "I" / "do" / "is"
        # don't carry signal — fall back to recency-only.
        return None
    # Escape sqlite LIKE wildcards in the user input.
    safe = keyword.replace("%", r"\%").replace("_", r"\_")
    return f"%{safe}%"


def _format_rows_within_budget(
    rows: list[sqlite3.Row],
    token_budget: int,
) -> str:
    """Render rows newest-first; stop adding once we hit the budget."""
    char_budget = token_budget * _CHARS_PER_TOKEN
    parts: list[str] = []
    used = 0

    for row in rows:
        block = (
            f"[Past session {row['timestamp']}]\n"
            f"user: {row['user_message']}\n"
            f"assistant: {row['assistant_text']}\n"
        )
        if used + len(block) > char_budget:
            # If we already have at least one block, stop. If not,
            # truncate this single block so we still inject something.
            if parts:
                break
            block = block[: max(char_budget - used, 0)]
            parts.append(block)
            break
        parts.append(block)
        used += len(block)

    return "\n".join(parts)


def _flatten_last_user_message(request: dict[str, Any]) -> str:
    """Walk request.messages backwards and return the last user message text."""
    messages = request.get("messages") or []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        return _flatten_content(msg.get("content"))
    return ""


def _flatten_assistant_text(response: dict[str, Any]) -> str:
    """Pick text-shaped content blocks out of an Anthropic response dict."""
    content = response.get("content") or []
    chunks: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            chunks.append(str(block.get("text") or ""))
    return "\n".join(c for c in chunks if c)


def _flatten_content(content: Any) -> str:
    """Normalize Anthropic-style content (str | list of blocks) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                chunks.append(block)
        return "\n".join(c for c in chunks if c)
    return str(content)


def _extract_observe_metadata(
    request: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    """Pull a small, safe-to-store summary out of request/response.

    We deliberately store ONLY token counts + provider + stop reason.
    Bodies (system prompt, full message list, tool inputs) are NOT
    persisted: they're privacy-sensitive and the LIKE search doesn't
    need them.
    """
    usage = response.get("usage") or {}
    return {
        "model": response.get("model"),
        "stop_reason": response.get("stop_reason"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "had_tools": bool(request.get("tools")),
        "stream": bool(request.get("stream")),
    }


async def _to_thread(fn: Any, *args: Any) -> Any:
    """Run a blocking sqlite3 call on the default thread pool.

    Imported lazily so the module is importable without an event
    loop (e.g. for static analysis). asyncio.to_thread requires
    Python 3.9+; this codebase targets 3.12+ so we're fine.
    """
    import asyncio

    return await asyncio.to_thread(fn, *args)
