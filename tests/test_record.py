"""Unit tests for ``coderouter_plugin_memory.record``."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from coderouter_plugin_memory import MemoryRecorder
from coderouter_plugin_memory.backends import MemoryBackendError
from coderouter_plugin_memory.record import _to_serializable

# ----------------------------------------------------------------------
# Synthetic Pydantic-shaped fixtures
# ----------------------------------------------------------------------


@dataclass
class FakePydanticModel:
    """Has model_dump(mode="json") just like Anthropic's translation models."""

    field_a: str = "x"
    field_b: int = 1

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return {"field_a": self.field_a, "field_b": self.field_b}


@dataclass
class FakeRequest:
    messages: list[dict] = field(default_factory=list)


# ----------------------------------------------------------------------
# Stub backend
# ----------------------------------------------------------------------


class StubBackend:
    name = "stub"

    def __init__(self, *, raise_on_observe: bool = False) -> None:
        self.observed: list[tuple[str, dict, dict]] = []
        self.raise_on_observe = raise_on_observe

    async def health(self) -> bool:
        return True

    async def smart_search(self, **_: Any) -> str:
        return ""

    async def observe(
        self,
        *,
        project_id: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        self.observed.append((project_id, request, response))
        if self.raise_on_observe:
            raise MemoryBackendError("stub-broken")


def _recorder_with(stub: StubBackend, **kwargs: Any) -> MemoryRecorder:
    rec = MemoryRecorder(backend="null", **kwargs)  # null avoids touching disk
    rec._backend = stub  # type: ignore[attr-defined]
    return rec


# ----------------------------------------------------------------------
# _to_serializable helper
# ----------------------------------------------------------------------


class TestToSerializable:
    def test_dict_passes_through(self) -> None:
        d = {"a": 1}
        assert _to_serializable(d) is d

    def test_pydantic_dump(self) -> None:
        out = _to_serializable(FakePydanticModel(field_a="y", field_b=42))
        assert out == {"field_a": "y", "field_b": 42}

    def test_none_returns_empty_dict(self) -> None:
        assert _to_serializable(None) == {}

    def test_unknown_type_returns_dict_or_empty(self) -> None:
        # Plain object — best-effort vars(), or empty if vars() fails.
        class _Bag:
            def __init__(self) -> None:
                self.x = 1

        out = _to_serializable(_Bag())
        # Works either way; the contract is "doesn't raise".
        assert isinstance(out, dict)


# ----------------------------------------------------------------------
# on_event behavior
# ----------------------------------------------------------------------


class TestRecorderOnEvent:
    def test_ignores_other_events(self) -> None:
        stub = StubBackend()
        rec = _recorder_with(stub, project_id_override="proj-x")
        asyncio.run(rec.on_event("cache_observed", {}))
        assert stub.observed == []

    def test_request_completed_records(self) -> None:
        stub = StubBackend()
        rec = _recorder_with(stub, project_id_override="proj-x")
        payload = {
            "request": FakePydanticModel(field_a="req"),
            "response": FakePydanticModel(field_a="resp"),
            "provider": "p",
            "latency_ms": 12.0,
            "stream": False,
        }
        asyncio.run(rec.on_event("request_completed", payload))

        assert len(stub.observed) == 1
        project_id, req, resp = stub.observed[0]
        assert project_id == "proj-x"
        assert req == {"field_a": "req", "field_b": 1}
        assert resp == {"field_a": "resp", "field_b": 1}

    def test_missing_request_or_response_skipped(self) -> None:
        stub = StubBackend()
        rec = _recorder_with(stub, project_id_override="proj-x")
        asyncio.run(rec.on_event("request_completed", {"request": None}))
        assert stub.observed == []

    def test_observe_failure_swallowed(self) -> None:
        """Backend MemoryBackendError must NOT propagate out of on_event."""
        stub = StubBackend(raise_on_observe=True)
        rec = _recorder_with(stub, project_id_override="proj-x")
        payload = {
            "request": {"messages": []},
            "response": {"content": []},
        }
        # Should not raise.
        asyncio.run(rec.on_event("request_completed", payload))

    def test_unexpected_exception_swallowed(self) -> None:
        """Any exception type from the backend must NOT propagate."""

        class BadBackend(StubBackend):
            async def observe(self, **_: Any) -> None:  # type: ignore[override]
                raise RuntimeError("not a MemoryBackendError")

        rec = _recorder_with(BadBackend(), project_id_override="proj-x")
        payload = {
            "request": {"messages": []},
            "response": {"content": []},
        }
        asyncio.run(rec.on_event("request_completed", payload))


class TestRecorderConstruction:
    def test_unknown_backend_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown memory backend"):
            MemoryRecorder(backend="not-real")

    def test_null_backend_works_with_arbitrary_kwargs(self) -> None:
        # Useful for users who keep one config block + flip backend
        # to "null" without removing the rest of the keys.
        rec = MemoryRecorder(
            backend="null",
            inject_token_budget=2000,
            endpoint="http://x",
        )
        assert rec.backend.name == "null"


class TestRecorderCircuitBreaker:
    def test_breaker_opens_after_threshold_failures(self) -> None:
        stub = StubBackend(raise_on_observe=True)
        rec = _recorder_with(
            stub,
            project_id_override="proj-x",
            circuit_breaker_threshold=3,
        )
        payload = {
            "request": {"messages": []},
            "response": {"content": []},
        }
        for _ in range(3):
            asyncio.run(rec.on_event("request_completed", payload))
        # All three calls reached the backend.
        assert len(stub.observed) == 3
        # Now breaker is OPEN — further calls skip.
        for _ in range(5):
            asyncio.run(rec.on_event("request_completed", payload))
        assert len(stub.observed) == 3  # unchanged

    def test_breaker_resets_on_success(self) -> None:
        # Mix: 2 failures (threshold 3) → success → another 2 failures
        # should NOT trip because the success reset the counter.
        stub = StubBackend(raise_on_observe=True)
        rec = _recorder_with(
            stub,
            project_id_override="proj-x",
            circuit_breaker_threshold=3,
        )
        payload = {
            "request": {"messages": []},
            "response": {"content": []},
        }
        asyncio.run(rec.on_event("request_completed", payload))
        asyncio.run(rec.on_event("request_completed", payload))

        stub.raise_on_observe = False
        asyncio.run(rec.on_event("request_completed", payload))  # success → counter resets

        stub.raise_on_observe = True
        asyncio.run(rec.on_event("request_completed", payload))
        asyncio.run(rec.on_event("request_completed", payload))

        # 5 calls so far, all reached backend (breaker never opened).
        assert len(stub.observed) == 5
