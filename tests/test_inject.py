"""Unit tests for MemoryPlugin InputFilter (inject phase).

inject.py は v0.4.0 で plugin.py に統合された。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coderouter_plugin_memory.plugin import MemoryPlugin, _prepend_memory
from coderouter_plugin_memory.store import add_manual_fact, append_facts

# ──────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────


def _make_plugin(tmp_path: Path, **kwargs: object) -> MemoryPlugin:
    return MemoryPlugin(project="test", state_dir=str(tmp_path), **kwargs)


def _mock_request(system: object = None) -> MagicMock:
    req = MagicMock()
    req.system = system

    def model_copy(update: dict) -> MagicMock:
        m = MagicMock()
        m.system = update.get("system", system)
        return m

    req.model_copy = model_copy
    return req


# ──────────────────────────────────────────────────────────────
# _prepend_memory
# ──────────────────────────────────────────────────────────────


def test_prepend_none_system() -> None:
    assert _prepend_memory(None, "MEM") == "MEM"


def test_prepend_str_system() -> None:
    result = _prepend_memory("base", "MEM")
    assert result == "MEM\n\nbase"


def test_prepend_list_system() -> None:
    existing = [{"type": "text", "text": "orig"}]
    result = _prepend_memory(existing, "MEM")
    assert isinstance(result, list)
    assert result[0] == {"type": "text", "text": "MEM"}
    assert result[1]["text"] == "orig"


def test_prepend_empty_str() -> None:
    result = _prepend_memory("", "MEM")
    assert isinstance(result, str)
    assert result.startswith("MEM")


# ──────────────────────────────────────────────────────────────
# MemoryPlugin.transform
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_disabled_passthrough(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, inject_enabled=False)
    req = _mock_request(system="original")
    result = await plugin.transform(req)
    assert result.system == "original"


@pytest.mark.asyncio
async def test_no_facts_passthrough(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, inject_enabled=True)
    req = _mock_request(system="original")
    result = await plugin.transform(req)
    assert result.system == "original"


@pytest.mark.asyncio
async def test_injects_facts_into_str_system(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, inject_enabled=True)
    append_facts(plugin._cfg.facts_path(), ["FastAPI を使用"], project="test")
    req = _mock_request(system="base prompt")
    result = await plugin.transform(req)
    assert "[Memory" in result.system
    assert "FastAPI" in result.system
    assert "base prompt" in result.system


@pytest.mark.asyncio
async def test_injects_manual_into_none_system(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, inject_enabled=True)
    add_manual_fact(plugin._cfg.manual_path(), "always use type hints")
    req = _mock_request(system=None)
    result = await plugin.transform(req)
    assert "type hints" in result.system


@pytest.mark.asyncio
async def test_injects_into_list_system(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, inject_enabled=True)
    append_facts(plugin._cfg.facts_path(), ["asyncio ベース"], project="test")
    req = _mock_request(system=[{"type": "text", "text": "existing"}])
    result = await plugin.transform(req)
    assert isinstance(result.system, list)
    assert result.system[0]["text"].startswith("[Memory")
