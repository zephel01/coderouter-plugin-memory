"""coderouter-plugin-memory — wire-level memory plugin for CodeRouter.

Public API:
    MemoryInjector  — InputFilter that injects memory into AnthropicRequest
    MemoryRecorder  — Observer that records request/response pairs

Entry points (registered in pyproject.toml):
    coderouter.input_filter -> memory = MemoryInjector
    coderouter.observer     -> memory = MemoryRecorder

Both are activated by adding ``memory`` to ``plugins.enabled`` in
providers.yaml.

Status: skeleton only. Functional code lands in 0.1.0 (P2).
"""
from __future__ import annotations

from coderouter_plugin_memory.inject import MemoryInjector
from coderouter_plugin_memory.record import MemoryRecorder

__all__ = ["MemoryInjector", "MemoryRecorder"]

__version__ = "0.1.0.dev0"
