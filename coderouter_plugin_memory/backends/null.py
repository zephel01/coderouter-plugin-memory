"""Null backend — explicit no-op + degrade pathway target.

Two roles:

1. **Explicit disable.** A user who sets ``backend: null`` in
   providers.yaml is saying "load the plugin but don't actually do
   anything memory-shaped". Useful for keeping the plugin pinned in
   a multi-environment config while disabling memory in (say) CI
   without removing it from ``plugins.enabled``.

2. **Degrade fallback target.** Higher-level plugin code can swap a
   broken backend out for ``NullBackend`` to keep request flow alive
   when the chosen backend's health check fails persistently. The
   request engine never throws, never injects, never observes —
   exactly the v2.2.0 behavior, no surprise.

The implementation is intentionally tiny. Don't add knobs here:
anything you'd want to configure goes on a real backend.
"""
from __future__ import annotations

from typing import Any


class NullBackend:
    """No-op memory backend. Always healthy, always returns nothing."""

    name = "null"

    def __init__(self, **_kwargs: Any) -> None:
        # Accept (and ignore) arbitrary kwargs so a config block
        # designed for another backend doesn't blow up when the user
        # temporarily flips ``backend: null`` for debugging.
        return

    async def health(self) -> bool:
        return True

    async def smart_search(
        self,
        *,
        project_id: str,
        query: str,
        token_budget: int,
    ) -> str:
        return ""

    async def observe(
        self,
        *,
        project_id: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        return None
