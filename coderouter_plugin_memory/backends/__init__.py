"""Memory backend adapters.

Concrete backends:

- :class:`BuiltinBackend`     — sqlite3, stdlib only, minimal features.
- :class:`AgentMemoryBackend` — HTTP client to a running agentmemory
                                server (recommended for quality).
- :class:`NullBackend`        — no-op (explicit disable / degrade target).
- ``Mem0Backend``             — optional, deps-heavy (lands in 0.4.0 / P5).

All implement the :class:`coderouter_plugin_memory.backends.base.MemoryBackend`
Protocol. The plugin's :class:`MemoryInjector` / :class:`MemoryRecorder`
construct one of these by name based on ``providers.yaml ->
plugins.config.memory.backend``.
"""
from __future__ import annotations

from coderouter_plugin_memory.backends.agentmemory import AgentMemoryBackend
from coderouter_plugin_memory.backends.base import (
    MemoryBackend,
    MemoryBackendError,
)
from coderouter_plugin_memory.backends.builtin import BuiltinBackend
from coderouter_plugin_memory.backends.null import NullBackend

__all__ = [
    "AgentMemoryBackend",
    "BuiltinBackend",
    "MemoryBackend",
    "MemoryBackendError",
    "NullBackend",
]
