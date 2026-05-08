"""Tiny in-process circuit breaker for backend calls.

Why this exists
===============

Without a breaker, every request that arrives while the chosen
backend is down still pays the cost of trying — for an HTTP backend
that's 5 seconds of timeout per request. On a multi-tab setup with
agentmemory wedged, that adds 5 seconds to every reply until the
backend recovers. Not great.

The breaker tracks consecutive failures. After ``threshold`` of them,
it flips OPEN and short-circuits all backend calls for ``cooldown_s``
seconds. When the cooldown expires it flips to HALF_OPEN; the *next*
backend call is allowed through, and either:

- succeeds → breaker closes, failure counter resets.
- fails    → breaker opens again with the cooldown extended (capped).

This is a deliberately small, single-instance state machine. It does
not coordinate across processes — each CodeRouter instance has its
own breaker, which matches the Plugin SDK's per-process scope.

Usage shape
===========

::

    breaker = CircuitBreaker(threshold=5, cooldown_s=60.0)

    if breaker.should_skip():
        # treat the backend as unavailable; degrade.
        return ""

    try:
        result = await backend.smart_search(...)
    except MemoryBackendError:
        breaker.record_failure()
        raise
    else:
        breaker.record_success()
        return result

The plugin's :class:`MemoryInjector` / :class:`MemoryRecorder` wrap
this pattern so individual backends don't need to know about it.
"""
from __future__ import annotations

import time
from enum import Enum


class _State(Enum):
    """Three-state machine — closed (healthy) / open (skip) / half-open (probing)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-backend failure counter with cooldown.

    Args:
        threshold: number of consecutive failures that flip the
            breaker open. Must be >= 1.
        cooldown_s: seconds the breaker stays open before flipping
            to HALF_OPEN. Must be > 0.
        max_cooldown_s: ceiling on backoff growth. Each successive
            HALF_OPEN failure doubles the cooldown up to this cap.
            Defaults to 10x the initial cooldown.
        clock: callable returning monotonic seconds. Injectable for
            tests so we don't have to sleep.
    """

    def __init__(
        self,
        *,
        threshold: int = 5,
        cooldown_s: float = 60.0,
        max_cooldown_s: float | None = None,
        clock: object | None = None,
    ) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        if cooldown_s <= 0:
            raise ValueError(f"cooldown_s must be positive, got {cooldown_s}")

        self._threshold = threshold
        self._initial_cooldown = cooldown_s
        self._max_cooldown = max_cooldown_s or (cooldown_s * 10)
        # Falls back to time.monotonic when not injected. Stored as a
        # callable rather than a method so tests can patch it cleanly.
        self._clock = clock if callable(clock) else time.monotonic

        self._state: _State = _State.CLOSED
        self._failure_count: int = 0
        self._open_until: float = 0.0  # monotonic deadline (only meaningful when OPEN)
        self._current_cooldown: float = cooldown_s

    # ------------------------------------------------------------------
    # Hot-path: caller asks "should I even bother making the request?"
    # ------------------------------------------------------------------

    def should_skip(self) -> bool:
        """Return True iff the breaker is OPEN past the cooldown.

        After cooldown elapses, transitions OPEN → HALF_OPEN as a side
        effect: the *next* call gets to try. Side-effecting reads are
        normally a smell, but here it's the natural place to do the
        transition (we're already checking the deadline).
        """
        if self._state is _State.CLOSED:
            return False

        if self._state is _State.OPEN:
            now = self._clock()
            if now >= self._open_until:
                self._state = _State.HALF_OPEN
                return False
            return True

        # HALF_OPEN: let exactly one call through. The breaker will
        # be moved back to CLOSED or OPEN by record_success / record_failure
        # once the call returns.
        return False

    # ------------------------------------------------------------------
    # Caller reports the outcome
    # ------------------------------------------------------------------

    def record_success(self) -> None:
        """Reset the breaker — backend is healthy."""
        self._state = _State.CLOSED
        self._failure_count = 0
        self._current_cooldown = self._initial_cooldown
        self._open_until = 0.0

    def record_failure(self) -> None:
        """Tally one failure; flip OPEN if threshold reached.

        From HALF_OPEN: any failure flips immediately back to OPEN
        with the cooldown doubled (up to ``max_cooldown_s``). That
        backoff dampens "broken backend gets retried every minute"
        churn during long outages.
        """
        if self._state is _State.HALF_OPEN:
            # Probe call failed → reopen with longer cooldown.
            self._current_cooldown = min(
                self._current_cooldown * 2.0, self._max_cooldown
            )
            self._open_until = self._clock() + self._current_cooldown
            self._state = _State.OPEN
            return

        self._failure_count += 1
        if self._failure_count >= self._threshold:
            self._open_until = self._clock() + self._current_cooldown
            self._state = _State.OPEN

    # ------------------------------------------------------------------
    # Read-only diagnostics — used by tests + future log lines.
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state.value

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def open_seconds_remaining(self) -> float:
        """Seconds until OPEN flips to HALF_OPEN, or 0.0 if already past."""
        if self._state is not _State.OPEN:
            return 0.0
        return max(0.0, self._open_until - self._clock())
