"""MemoryBackend Protocol — common interface for all 4 backends.

Status: contract finalized as part of design review (Plan §4.1).
Concrete implementations land in 0.1.0+.

Failure semantics:
    health()        — never raises; returns False on any error.
    smart_search()  — raises MemoryBackendError on transport / backend
                      errors. Plugin catches and degrades to no inject.
                      No-context-found is NOT an error (return "").
    observe()       — raises MemoryBackendError; Observer hook catches
                      and logs at debug level (best-effort).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackend(Protocol):
    """Common interface for all memory backends.

    Implementers MUST provide a ``name`` class attribute matching the
    backend slug used in providers.yaml (e.g. ``"agentmemory"``,
    ``"builtin"``).
    """

    name: str

    async def health(self) -> bool:
        """Lightweight liveness probe. Never raises.

        Returns True if the backend is ready to accept search/observe
        calls. False on any failure (network, auth, schema, etc.).
        """
        ...

    async def smart_search(
        self,
        *,
        project_id: str,
        query: str,
        token_budget: int,
    ) -> str:
        """Look up memory context relevant to ``query`` for ``project_id``.

        Returns a plain text block fitting within roughly
        ``token_budget`` tokens. The plugin will prepend this to the
        request's ``system`` prompt verbatim.

        Returns ``""`` (empty string) when the backend has no memory
        for this project — that's NOT an error condition.

        Raises:
            MemoryBackendError: on transport, auth, or backend errors.
        """
        ...

    async def observe(
        self,
        *,
        project_id: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        """Record a request/response pair into the backend.

        Both ``request`` and ``response`` are JSON-serializable dicts
        (already converted from Pydantic models by the caller).

        Raises:
            MemoryBackendError: on transport, auth, or backend errors.
                Callers (the Observer hook) MUST catch this — Observer
                is best-effort and never blocks engine response.
        """
        ...


class MemoryBackendError(Exception):
    """Backend operation failed.

    Plugin code catches this and either degrades (smart_search → no
    inject) or silently logs (observe → ignore).
    """
