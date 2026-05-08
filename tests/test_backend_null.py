"""Tests for the null backend — confirms the no-op contract."""
from __future__ import annotations

import asyncio

from coderouter_plugin_memory.backends import NullBackend


def test_null_health_always_true() -> None:
    n = NullBackend()
    assert asyncio.run(n.health()) is True


def test_null_smart_search_returns_empty() -> None:
    n = NullBackend()
    out = asyncio.run(
        n.smart_search(project_id="proj-x", query="anything", token_budget=2000)
    )
    assert out == ""


def test_null_observe_returns_none_silently() -> None:
    n = NullBackend()
    asyncio.run(
        n.observe(
            project_id="proj-x",
            request={"messages": [{"role": "user", "content": "hi"}]},
            response={"content": [{"type": "text", "text": "hi back"}]},
        )
    )
    # No assertion needed: the contract is "doesn't raise".


def test_null_accepts_arbitrary_kwargs() -> None:
    """A user temporarily flipping ``backend: null`` shouldn't have to
    delete the rest of their config block — extra kwargs are absorbed."""
    NullBackend(endpoint="ignored", inject_token_budget=2000, secret_env="X")
