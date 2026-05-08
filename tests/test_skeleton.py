"""Smoke tests — package surface remains stable.

The deeper behavior of each module is exercised in its own
``test_<module>.py``. This file just confirms the package imports
cleanly, the public symbols are exported, and the entry-point
class attributes are still ``"memory"`` (which the loader requires).
"""
from __future__ import annotations

import coderouter_plugin_memory


def test_package_imports() -> None:
    assert coderouter_plugin_memory.__version__.startswith("0.")


def test_public_symbols_exported() -> None:
    assert hasattr(coderouter_plugin_memory, "MemoryInjector")
    assert hasattr(coderouter_plugin_memory, "MemoryRecorder")


def test_entry_point_class_attribute_is_memory() -> None:
    """The Plugin SDK loader keys plugins by class.name, not class.__name__."""
    assert coderouter_plugin_memory.MemoryInjector.name == "memory"
    assert coderouter_plugin_memory.MemoryRecorder.name == "memory"


def test_protocol_and_error_importable() -> None:
    from coderouter_plugin_memory.backends.base import (
        MemoryBackend,
        MemoryBackendError,
    )

    assert MemoryBackend.__name__ == "MemoryBackend"
    assert issubclass(MemoryBackendError, Exception)
