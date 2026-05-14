"""coderouter-plugin-memory — Zero-friction builtin memory for CodeRouter.

3-phase architecture:
  capture     (Observer)    → buffer.jsonl
  consolidate (CLI / cron)  → Ollama qwen3:1.7b → facts.jsonl
  inject      (InputFilter) → system prompt prepend

Storage: ~/.coderouter/memory/{project}/
External deps: none (stdlib only)

Entry points (registered in pyproject.toml):
    coderouter.input_filter -> memory = MemoryPlugin
    coderouter.observer     -> memory = MemoryPlugin

Both are activated by adding ``memory`` to ``plugins.enabled`` in providers.yaml.
"""
from __future__ import annotations

__version__ = "0.4.0"
__all__ = ["MemoryPlugin"]

from coderouter_plugin_memory.plugin import MemoryPlugin
