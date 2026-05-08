"""Tests for ``coderouter_plugin_memory._circuit.CircuitBreaker``.

Time is mocked through the ``clock`` constructor parameter so we
don't sleep — all transitions happen synchronously by advancing a
manual counter.
"""
from __future__ import annotations

import pytest

from coderouter_plugin_memory._circuit import CircuitBreaker


class _FakeClock:
    """Hand-controlled monotonic-style clock."""

    def __init__(self) -> None:
        self.now = 1000.0  # arbitrary non-zero start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _new(threshold: int = 3, cooldown_s: float = 10.0, max_cooldown_s: float | None = None) -> tuple[CircuitBreaker, _FakeClock]:
    clock = _FakeClock()
    breaker = CircuitBreaker(
        threshold=threshold,
        cooldown_s=cooldown_s,
        max_cooldown_s=max_cooldown_s,
        clock=clock,
    )
    return breaker, clock


class TestConstruction:
    def test_invalid_threshold_rejected(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            CircuitBreaker(threshold=0, cooldown_s=10.0)

    def test_invalid_cooldown_rejected(self) -> None:
        with pytest.raises(ValueError, match="cooldown_s"):
            CircuitBreaker(threshold=3, cooldown_s=0)

    def test_default_max_cooldown_is_10x(self) -> None:
        b = CircuitBreaker(threshold=3, cooldown_s=5.0)
        # 10x not directly observable, but verify _max_cooldown via internal name:
        assert b._max_cooldown == pytest.approx(50.0)


class TestStateTransitions:
    def test_starts_closed(self) -> None:
        b, _ = _new()
        assert b.state == "closed"
        assert b.should_skip() is False

    def test_failures_below_threshold_stay_closed(self) -> None:
        b, _ = _new(threshold=3)
        b.record_failure()
        b.record_failure()
        assert b.state == "closed"
        assert b.should_skip() is False
        assert b.failure_count == 2

    def test_threshold_failures_open_breaker(self) -> None:
        b, _ = _new(threshold=3)
        for _ in range(3):
            b.record_failure()
        assert b.state == "open"
        assert b.should_skip() is True

    def test_success_resets_count_and_closes(self) -> None:
        b, _ = _new(threshold=3)
        b.record_failure()
        b.record_failure()
        b.record_success()
        assert b.state == "closed"
        assert b.failure_count == 0

    def test_open_to_half_open_after_cooldown(self) -> None:
        b, clock = _new(threshold=3, cooldown_s=10.0)
        for _ in range(3):
            b.record_failure()
        assert b.state == "open"

        # During cooldown — still open.
        clock.advance(5.0)
        assert b.should_skip() is True
        assert b.state == "open"

        # After cooldown — should_skip returns False AND state moves
        # to HALF_OPEN. The next call probes the backend.
        clock.advance(6.0)
        assert b.should_skip() is False
        assert b.state == "half_open"

    def test_half_open_success_closes_breaker(self) -> None:
        b, clock = _new(threshold=2, cooldown_s=5.0)
        b.record_failure()
        b.record_failure()
        clock.advance(6.0)
        b.should_skip()  # transitions to half_open
        assert b.state == "half_open"

        b.record_success()
        assert b.state == "closed"
        assert b.failure_count == 0

    def test_half_open_failure_reopens_with_doubled_cooldown(self) -> None:
        b, clock = _new(threshold=2, cooldown_s=10.0, max_cooldown_s=100.0)
        b.record_failure()
        b.record_failure()
        # Advance past cooldown.
        clock.advance(11.0)
        b.should_skip()  # half_open

        b.record_failure()
        assert b.state == "open"
        # Cooldown doubled from 10 → 20.
        assert b.open_seconds_remaining == pytest.approx(20.0, rel=1e-3)

    def test_max_cooldown_caps_growth(self) -> None:
        b, clock = _new(threshold=1, cooldown_s=10.0, max_cooldown_s=15.0)
        # Trip the breaker, then keep failing in HALF_OPEN; cooldown
        # should never exceed max.
        b.record_failure()
        for _ in range(10):
            clock.advance(b.open_seconds_remaining + 1)
            b.should_skip()  # → half_open
            b.record_failure()  # → open with grown cooldown
        # Cooldown is capped at 15.0
        assert b.open_seconds_remaining <= 15.0 + 1e-6


class TestDiagnostics:
    def test_failure_count_visible(self) -> None:
        b, _ = _new(threshold=5)
        b.record_failure()
        b.record_failure()
        assert b.failure_count == 2

    def test_open_seconds_remaining_zero_when_closed(self) -> None:
        b, _ = _new()
        assert b.open_seconds_remaining == 0.0

    def test_open_seconds_remaining_decreases_with_clock(self) -> None:
        b, clock = _new(threshold=1, cooldown_s=20.0)
        b.record_failure()
        assert b.open_seconds_remaining == pytest.approx(20.0)
        clock.advance(7.0)
        assert b.open_seconds_remaining == pytest.approx(13.0)
        clock.advance(15.0)
        # Past the deadline — saturated to 0.
        assert b.open_seconds_remaining == 0.0
