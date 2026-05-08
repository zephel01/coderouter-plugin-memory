"""MemoryInjector — InputFilter that injects memory context.

Wires the configured :class:`MemoryBackend` into CodeRouter's
``coderouter.input_filter`` extension point. On every Anthropic
request the engine processes, this filter:

1. Resolves the project_id (cwd / config-path-hash / explicit
   override — see :mod:`coderouter_plugin_memory.project_id`).
2. Pulls the last user message out of ``request.messages`` to use as
   the search query.
3. Calls ``backend.smart_search(...)`` and gets back a plain-text
   block (or empty string if there's nothing to inject).
4. Prepends that block to ``request.system`` inside a
   ``<previous-session-context>`` envelope so the model can tell
   what's memory vs. what's the current conversation.

Failure modes are deliberately quiet: a backend that's unhealthy or
raises during search degrades to "no inject" and lets the request
flow through unchanged. The wire-layer router's reliability story
(P2 in the Vision) doesn't tolerate memory issues taking out routing.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar

from coderouter_plugin_memory._circuit import CircuitBreaker
from coderouter_plugin_memory.backends import (
    AgentMemoryBackend,
    BuiltinBackend,
    MemoryBackend,
    MemoryBackendError,
    NullBackend,
)
from coderouter_plugin_memory.project_id import resolve_project_id

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Backend factory
# ----------------------------------------------------------------------

# Registry of slug → class. ``mem0`` is reserved for a future phase;
# referring to it in providers.yaml today gives a clear "not yet
# available in this version" error rather than silently dropping
# memory.
_BACKEND_FACTORIES: ClassVar[dict[str, Any]] = {
    "builtin": BuiltinBackend,
    "agentmemory": AgentMemoryBackend,
    "null": NullBackend,
    # "mem0":        Mem0Backend,         # 0.4.0 / P5
}


def _build_backend(name: str, **kwargs: Any) -> MemoryBackend:
    """Resolve a backend slug to a constructed instance.

    Unknown names raise ``ValueError`` at plugin construction time so
    a typo in providers.yaml fails fast at startup rather than
    silently dropping memory mid-session.
    """
    cls = _BACKEND_FACTORIES.get(name)
    if cls is None:
        known = ", ".join(sorted(_BACKEND_FACTORIES.keys()))
        raise ValueError(
            f"unknown memory backend: {name!r}. "
            f"Known: {known}. (agentmemory / mem0 land in later releases.)"
        )
    return cls(**kwargs)


# ----------------------------------------------------------------------
# Helpers — system prompt manipulation
# ----------------------------------------------------------------------

# Wrapper added around the injected memory text. Two reasons for the
# explicit envelope:
#   1. Lets the model distinguish memory from "real" system content.
#   2. Lets a future audit / debug pass strip the injection back out
#      by simple string match.
_OPEN_TAG = "<previous-session-context>"
_CLOSE_TAG = "</previous-session-context>"


def _wrap_for_inject(text: str) -> str:
    """Wrap memory text in the envelope (with leading newline for spacing)."""
    return f"\n\n{_OPEN_TAG}\n{text.rstrip()}\n{_CLOSE_TAG}"


def _prepend_to_system(system: Any, addition: str) -> Any:
    """Add ``addition`` after the existing system content.

    Anthropic's ``system`` field accepts either ``str | None`` or a
    list of ``{"type": "text", "text": ...}`` blocks. We preserve
    whichever shape the request came in with so downstream cache_control
    / beta block handling stays untouched.
    """
    if system is None:
        return addition
    if isinstance(system, str):
        return system + addition
    if isinstance(system, list):
        # Append a new text block. We deliberately don't merge into
        # an existing block — keeping memory in its own block makes
        # cache_control easier to reason about (cache the project's
        # static system prompt; don't cache the dynamic memory).
        return [*system, {"type": "text", "text": addition.lstrip()}]
    # Unknown shape — be conservative: leave it alone.
    return system


def _extract_user_query(messages: list[Any]) -> str:
    """Walk messages backwards, return the last user message as plain text.

    Mirrors :func:`coderouter_plugin_memory.backends.builtin._flatten_last_user_message`
    but operates on the Anthropic Pydantic model surface (where the
    inputs are ``AnthropicMessage`` instances) instead of dict form.
    """
    for msg in reversed(messages):
        # Tolerant of dict-shaped messages too (e.g. tests).
        role = getattr(msg, "role", None) or (
            msg.get("role") if isinstance(msg, dict) else None
        )
        if role != "user":
            continue
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        return _flatten_content(content)
    return ""


def _flatten_content(content: Any) -> str:
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
            else:
                # AnthropicTextBlock / similar Pydantic model:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(c for c in chunks if c)
    return str(content)


# ----------------------------------------------------------------------
# Plugin class
# ----------------------------------------------------------------------


class MemoryInjector:
    """InputFilter — pre-request memory injection.

    Args:
        backend: backend slug. ``"builtin"`` (default) or ``"null"``;
            ``"agentmemory"`` / ``"mem0"`` reserved for later releases.
        inject_token_budget: cap on tokens injected into ``request.system``.
            Defaults to 2000 — the same default agentmemory uses, so
            the two backends inject roughly the same amount.
        project_id_override: when set, every call uses this id instead
            of running :func:`resolve_project_id`. Useful for tests
            and for users who want monorepo-shared memory.
        **backend_kwargs: passed straight through to the backend
            constructor (e.g. ``store=`` for builtin, ``endpoint=`` /
            ``secret_env=`` for agentmemory).
    """

    name = "memory"

    def __init__(
        self,
        *,
        backend: str = "builtin",
        inject_token_budget: int = 2000,
        project_id_override: str | None = None,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown_s: float = 60.0,
        **backend_kwargs: Any,
    ) -> None:
        if inject_token_budget < 0:
            raise ValueError(
                f"inject_token_budget must be >= 0, got {inject_token_budget}"
            )
        self._backend = _build_backend(backend, **backend_kwargs)
        self._budget = inject_token_budget
        self._project_override = project_id_override
        # v0.3.0 (P4): circuit breaker that flips a misbehaving backend
        # off after ``threshold`` consecutive failures, then probes
        # again after ``cooldown_s``. NullBackend never fails so its
        # breaker effectively never opens; agentmemory / mem0 / builtin
        # all benefit from skip-during-outage to avoid stacking 5s
        # timeouts on every request.
        self._breaker = CircuitBreaker(
            threshold=circuit_breaker_threshold,
            cooldown_s=circuit_breaker_cooldown_s,
        )

    @property
    def backend(self) -> MemoryBackend:
        """Underlying backend instance — exposed for tests."""
        return self._backend

    async def transform(self, request: Any) -> Any:
        """Run the configured backend's ``smart_search`` and inject the result.

        Behavior contract (each branch is degrade-safe):

        - ``inject_token_budget == 0``  → no-op, return request unchanged.
        - backend raises during search → log warn, return request unchanged.
        - backend returns empty string → no-op, return request unchanged.
        - happy path                   → return ``request.model_copy``-ed
          version with memory prepended into ``system``.

        The plugin never raises out of this method — the engine's hook
        wrapper would log it as ``input-filter-failed`` and degrade
        anyway, but doing the catch here keeps the log context focused.
        """
        if self._budget <= 0:
            return request

        # P4 (v0.3.0): circuit breaker check. When the breaker is OPEN
        # past its cooldown we short-circuit without paying the
        # backend's per-call latency (HTTP timeouts especially). The
        # breaker logs the transition itself; we just take the hint.
        if self._breaker.should_skip():
            logger.debug(
                "memory-search-skipped-circuit-open",
                extra={"backend": self._backend.name},
            )
            return request

        project_id = self._project_override or resolve_project_id()
        query = _extract_user_query(getattr(request, "messages", []) or [])

        try:
            context = await self._backend.smart_search(
                project_id=project_id,
                query=query,
                token_budget=self._budget,
            )
        except MemoryBackendError as exc:
            self._breaker.record_failure()
            logger.warning(
                "memory-search-failed",
                extra={
                    "backend": self._backend.name,
                    "project_id": project_id,
                    "error": str(exc)[:500],
                    "circuit_state": self._breaker.state,
                    "consecutive_failures": self._breaker.failure_count,
                },
            )
            return request
        except Exception as exc:  # pragma: no cover - defensive
            # An unexpected error type — still must not bubble out.
            self._breaker.record_failure()
            logger.warning(
                "memory-search-unexpected-error",
                extra={
                    "backend": self._backend.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                    "circuit_state": self._breaker.state,
                },
            )
            return request

        # Successful call — make sure a half-open probe is registered
        # as such. NullBackend's empty-return is also a success here.
        self._breaker.record_success()

        if not context:
            return request

        wrapped = _wrap_for_inject(context)
        new_system = _prepend_to_system(getattr(request, "system", None), wrapped)
        return request.model_copy(update={"system": new_system})
