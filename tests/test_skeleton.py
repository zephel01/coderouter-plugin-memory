"""Smoke tests — package surface remains stable.

The deeper behavior of each module is exercised in its own
``test_<module>.py``. This file confirms the package imports
cleanly, the public symbol is exported, and the entry-point
class attribute is still ``"memory"`` (which the loader requires).
"""
from __future__ import annotations

import coderouter_plugin_memory


def test_package_imports() -> None:
    assert coderouter_plugin_memory.__version__.startswith("0.")


def test_public_symbol_exported() -> None:
    assert hasattr(coderouter_plugin_memory, "MemoryPlugin")


def test_entry_point_class_attribute_is_memory() -> None:
    """The Plugin SDK loader keys plugins by class.name, not class.__name__."""
    assert coderouter_plugin_memory.MemoryPlugin.name == "memory"
