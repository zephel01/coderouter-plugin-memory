"""Unit tests for ``coderouter_plugin_memory.inject``.

The injector is exercised against synthetic Pydantic-shaped objects
so we don't pull in CodeRouter as a runtime test dep.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from coderouter_plugin_memory import MemoryInjector
from coderouter_plugin_memory.backends import MemoryBackendError
from coderouter_plugin_memory.inject import (
    _build_backend,
    _extract_user_query,
    _prepend_to_system,
    _wrap_for_inject,
)

# ----------------------------------------------------------------------
# Synthetic AnthropicRequest replacement (minimal Pydantic-like surface)
# ----------------------------------------------------------------------


@dataclass
class FakeMessage:
    role: str
    content: Any


@dataclass
class FakeRequest:
    messages: list[FakeMessage] = field(default_factory=list)
    system: Any = None

    def model_copy(self, *, update: dict[str, Any]) -> FakeRequest:
        copy = FakeRequest(
            messages=list(self.messages),
            system=self.system,
        )
        for k, v in update.items():
            setattr(copy, k, v)
        return copy


# ----------------------------------------------------------------------
# Stub backend
# ----------------------------------------------------------------------


class StubBackend:
    name = "stub"

    def __init__(
        self,
        *,
        search_result: str = "",
        raise_on_search: bool = False,
    ) -> None:
        self.search_result = search_result
        self.raise_on_search = raise_on_search
        self.searches: list[tuple[str, str, int]] = []

    async def health(self) -> bool:
        return True

    async def smart_search(
        self, *, project_id: str, query: str, token_budget: int
    ) -> str:
        self.searches.append((project_id, query, token_budget))
        if self.raise_on_search:
            raise MemoryBackendError("stub-broken")
        return self.search_result

    async def observe(self, **_: Any) -> None:
        return None


def _injector_with(stub: StubBackend, **kwargs: Any) -> MemoryInjector:
    """Build a MemoryInjector with a hand-injected backend, bypassing the factory."""
    inj = MemoryInjector(backend="builtin", **kwargs)
    inj._backend = stub  # type: ignore[attr-defined]
    return inj


# ----------------------------------------------------------------------
# Free helpers
# ----------------------------------------------------------------------


class TestExtractUserQuery:
    def test_returns_last_user_string(self) -> None:
        msgs = [
            FakeMessage("user", "old"),
            FakeMessage("assistant", "ack"),
            FakeMessage("user", "new"),
        ]
        assert _extract_user_query(msgs) == "new"

    def test_handles_list_content(self) -> None:
        msgs = [
            FakeMessage(
                "user",
                [
                    {"type": "text", "text": "alpha"},
                    {"type": "text", "text": "beta"},
                ],
            )
        ]
        assert _extract_user_query(msgs) == "alpha\nbeta"

    def test_returns_empty_when_no_user(self) -> None:
        assert _extract_user_query([FakeMessage("assistant", "hi")]) == ""

    def test_empty_messages(self) -> None:
        assert _extract_user_query([]) == ""


class TestPrependToSystem:
    def test_none_replaced_with_addition(self) -> None:
        assert _prepend_to_system(None, "MEM") == "MEM"

    def test_str_concatenated(self) -> None:
        assert _prepend_to_system("BASE", "MEM") == "BASEMEM"

    def test_list_appended_as_text_block(self) -> None:
        out = _prepend_to_system(
            [{"type": "text", "text": "BASE"}], "  MEM"
        )
        assert out == [
            {"type": "text", "text": "BASE"},
            {"type": "text", "text": "MEM"},  # leading whitespace stripped
        ]


class TestWrapForInject:
    def test_envelope_present(self) -> None:
        wrapped = _wrap_for_inject("HELLO")
        assert "<previous-session-context>" in wrapped
        assert "</previous-session-context>" in wrapped
        assert "HELLO" in wrapped


class TestBuildBackendFactory:
    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown memory backend"):
            _build_backend("not-real")

    def test_known_backends_construct(self) -> None:
        from coderouter_plugin_memory.backends import BuiltinBackend, NullBackend

        assert isinstance(_build_backend("null"), NullBackend)
        # builtin requires a writable store path; pass a tmp via kwargs:
        b = _build_backend("builtin", store="/tmp/coderouter-plugin-memory-test.sqlite3")
        assert isinstance(b, BuiltinBackend)


# ----------------------------------------------------------------------
# Injector behavior
# ----------------------------------------------------------------------


class TestInjectorTransform:
    def test_no_inject_when_backend_returns_empty(self) -> None:
        stub = StubBackend(search_result="")
        inj = _injector_with(stub, project_id_override="proj-x")
        req = FakeRequest(messages=[FakeMessage("user", "anything")], system="S")

        out = asyncio.run(inj.transform(req))
        assert out is req  # exact same object → no copy made
        # And the stub WAS asked.
        assert stub.searches == [("proj-x", "anything", 2000)]

    def test_inject_prepends_envelope(self) -> None:
        stub = StubBackend(search_result="MEMORY-FROM-STUB")
        inj = _injector_with(stub, project_id_override="proj-x")
        req = FakeRequest(messages=[FakeMessage("user", "hi")], system="BASE")

        out = asyncio.run(inj.transform(req))
        assert out is not req  # new object
        assert "MEMORY-FROM-STUB" in out.system
        assert "<previous-session-context>" in out.system
        assert out.system.startswith("BASE")  # original system preserved

    def test_inject_works_with_none_system(self) -> None:
        stub = StubBackend(search_result="X")
        inj = _injector_with(stub, project_id_override="proj-x")
        req = FakeRequest(messages=[FakeMessage("user", "hi")], system=None)

        out = asyncio.run(inj.transform(req))
        assert "X" in out.system
        assert out.system.startswith("\n\n<previous-session-context>")

    def test_search_failure_degrades_to_no_inject(self) -> None:
        stub = StubBackend(raise_on_search=True)
        inj = _injector_with(stub, project_id_override="proj-x")
        req = FakeRequest(messages=[FakeMessage("user", "hi")], system="BASE")

        out = asyncio.run(inj.transform(req))
        assert out is req  # degraded — original returned
        assert out.system == "BASE"

    def test_zero_budget_short_circuits(self) -> None:
        stub = StubBackend(search_result="WOULD-INJECT")
        inj = _injector_with(
            stub, project_id_override="proj-x", inject_token_budget=0
        )
        req = FakeRequest(messages=[FakeMessage("user", "hi")], system=None)

        out = asyncio.run(inj.transform(req))
        assert out is req
        assert stub.searches == []  # never even asked

    def test_negative_budget_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="inject_token_budget"):
            MemoryInjector(backend="null", inject_token_budget=-1)

    def test_invalid_backend_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="unknown memory backend"):
            MemoryInjector(backend="not-real")

    def test_uses_cwd_id_when_no_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """No project_id_override → resolve_project_id is called and used."""
        monkeypatch.delenv("CODEROUTER_PROJECT_ID", raising=False)
        monkeypatch.delenv("CODEROUTER_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)

        stub = StubBackend(search_result="")
        inj = _injector_with(stub)  # no project_id_override
        req = FakeRequest(messages=[FakeMessage("user", "anything")])
        asyncio.run(inj.transform(req))

        assert len(stub.searches) == 1
        project_id, _query, _budget = stub.searches[0]
        assert project_id.startswith("proj-")


class TestInjectorCircuitBreaker:
    """Verifies that repeated backend failures stop hitting the backend."""

    def test_breaker_opens_after_threshold_failures(self) -> None:
        stub = StubBackend(raise_on_search=True)
        inj = _injector_with(
            stub,
            project_id_override="proj-x",
            circuit_breaker_threshold=3,
        )
        req = FakeRequest(messages=[FakeMessage("user", "hi")])
        # Trip the breaker.
        for _ in range(3):
            asyncio.run(inj.transform(req))
        # All three calls reached the backend.
        assert len(stub.searches) == 3
        # Now the breaker is open — further calls skip the backend.
        for _ in range(5):
            asyncio.run(inj.transform(req))
        assert len(stub.searches) == 3  # unchanged

    def test_breaker_recovers_on_success(self) -> None:
        # First the backend will raise; we'll flip it to success
        # mid-test so the half-open probe closes the breaker.
        stub = StubBackend(raise_on_search=True)
        inj = _injector_with(
            stub,
            project_id_override="proj-x",
            circuit_breaker_threshold=2,
            circuit_breaker_cooldown_s=0.001,  # near-zero so the test is fast
        )
        req = FakeRequest(messages=[FakeMessage("user", "hi")])

        # Trip the breaker.
        asyncio.run(inj.transform(req))
        asyncio.run(inj.transform(req))
        # Breaker is OPEN — next call will be skipped.
        asyncio.run(inj.transform(req))
        assert len(stub.searches) == 2

        # Sleep past the cooldown, fix the backend, try again.
        import time
        time.sleep(0.005)
        stub.raise_on_search = False
        stub.search_result = "RECOVERED"
        out = asyncio.run(inj.transform(req))
        # Probe call reached the backend, returned content, breaker closed.
        assert len(stub.searches) == 3
        assert "RECOVERED" in (out.system or "")
