"""MemoryRecorder — Observer that records request/response pairs.

Companion to :class:`MemoryInjector`. The injector runs at the input
edge of the engine and *reads* memory; the recorder runs at the
output edge and *writes* memory.

Design contract
===============

- Listens for ``request_completed`` events emitted by
  ``FallbackEngine._fanout_observers``. Other event types are
  ignored — this plugin doesn't care about ``cache_observed``,
  ``backend-health-changed``, etc.

- Serializes the request and response into JSON-friendly dicts
  before handing them to the backend. The backend Protocol
  signature requires ``dict[str, Any]`` so it doesn't need to know
  about Pydantic.

- **Best-effort.** A failure here MUST NOT block the engine
  response. The engine wraps observer calls in
  :func:`asyncio.create_task` so we're already off the hot path,
  but we still catch :class:`MemoryBackendError` here so log
  context attributes match the injector's failure log.

Backend instance ownership
==========================

The recorder constructs *its own* backend instance, separate from
the injector's. That mirrors how the entry points are registered
(one entry point each — they're loaded as independent plugins) and
sidesteps the question of how to share state across plugin
boundaries. For sqlite3 / HTTP backends the cost of two connections
is negligible, and avoiding shared state means a misconfigured
recorder can't kill the injector's read path.
"""
from __future__ import annotations

import logging
from typing import Any

from coderouter_plugin_memory._circuit import CircuitBreaker
from coderouter_plugin_memory.backends import (
    MemoryBackendError,
)
from coderouter_plugin_memory.inject import _build_backend
from coderouter_plugin_memory.project_id import resolve_project_id

logger = logging.getLogger(__name__)


# Event type emitted by ``FallbackEngine._fanout_observers``. Plugins
# MUST tolerate unknown types (forward-compat); we only care about
# the one event the recorder consumes.
_EVENT_REQUEST_COMPLETED = "request_completed"


class MemoryRecorder:
    """Observer — post-response memory recording.

    Args mirror :class:`MemoryInjector` so a user can copy/paste the
    same config block under ``plugins.config.memory:`` and have both
    halves agree:

    - ``backend`` — slug.
    - ``project_id_override`` — same role as in the injector.
    - ``**backend_kwargs`` — forwarded to the backend constructor.
    """

    name = "memory"

    def __init__(
        self,
        *,
        backend: str = "builtin",
        project_id_override: str | None = None,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown_s: float = 60.0,
        **backend_kwargs: Any,
    ) -> None:
        self._backend = _build_backend(backend, **backend_kwargs)
        self._project_override = project_id_override
        # P4 (v0.3.0): same breaker shape as the injector.  Recorder
        # is best-effort by contract — observe failures already get
        # silently logged — but the breaker still saves us 5s HTTP
        # timeouts per fanout when the backend is wedged.
        self._breaker = CircuitBreaker(
            threshold=circuit_breaker_threshold,
            cooldown_s=circuit_breaker_cooldown_s,
        )

    @property
    def backend(self) -> Any:
        return self._backend

    async def on_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Handle ``request_completed``; ignore everything else."""
        if event_type != _EVENT_REQUEST_COMPLETED:
            return

        request = payload.get("request")
        response = payload.get("response")
        if request is None or response is None:
            # Malformed payload — defensive guard, never seen in
            # practice but cheaper than a stack trace in the log.
            logger.debug(
                "memory-observe-skipped",
                extra={"reason": "missing-request-or-response"},
            )
            return

        if self._breaker.should_skip():
            logger.debug(
                "memory-observe-skipped-circuit-open",
                extra={"backend": self._backend.name},
            )
            return

        project_id = self._project_override or resolve_project_id()

        try:
            await self._backend.observe(
                project_id=project_id,
                request=_to_serializable(request),
                response=_to_serializable(response),
            )
        except MemoryBackendError as exc:
            self._breaker.record_failure()
            # Best-effort: log at debug, not warn. The engine already
            # logged ``observer-failed`` at warn for the asyncio task
            # outcome; double-warning would be noisy.
            logger.debug(
                "memory-observe-failed",
                extra={
                    "backend": self._backend.name,
                    "project_id": project_id,
                    "error": str(exc)[:500],
                    "circuit_state": self._breaker.state,
                    "consecutive_failures": self._breaker.failure_count,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._breaker.record_failure()
            logger.debug(
                "memory-observe-unexpected-error",
                extra={
                    "backend": self._backend.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                    "circuit_state": self._breaker.state,
                },
            )
        else:
            self._breaker.record_success()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _to_serializable(obj: Any) -> dict[str, Any]:
    """Convert a Pydantic model (or dict) to a JSON-friendly dict.

    The Anthropic translation models expose ``model_dump(mode="json")``
    which returns plain dicts/lists/scalars. For dict-shaped inputs
    (test fixtures, tool-emitted payloads) we just return them
    unchanged.
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(mode="json")  # type: ignore[no-any-return]
    # Last resort: best-effort attribute dump. Unknown types
    # serialize to ``{}`` rather than raising — this is the
    # observer code path, never block.
    try:
        return dict(vars(obj))
    except TypeError:
        return {}
