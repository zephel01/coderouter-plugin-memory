"""Unit tests for MemoryPlugin Observer (capture phase).

record.py は v0.4.0 で plugin.py に統合された。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coderouter_plugin_memory.plugin import MemoryPlugin, _extract_response_text
from coderouter_plugin_memory.store import buffer_count, read_buffer

# ──────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────


def _make_plugin(tmp_path: Path, **kwargs: object) -> MemoryPlugin:
    return MemoryPlugin(project="test", state_dir=str(tmp_path), **kwargs)


def _mock_response(texts: list[str]) -> MagicMock:
    resp = MagicMock()
    resp.content = [{"type": "text", "text": t} for t in texts]
    return resp


# ──────────────────────────────────────────────────────────────
# _extract_response_text
# ──────────────────────────────────────────────────────────────


def test_extract_none() -> None:
    assert _extract_response_text(None) == ""


def test_extract_text_blocks() -> None:
    resp = _mock_response(["Hello", " world"])
    result = _extract_response_text(resp)
    assert "Hello" in result and "world" in result


def test_extract_ignores_tool_use() -> None:
    resp = MagicMock()
    resp.content = [{"type": "tool_use", "name": "bash"}, {"type": "text", "text": "done"}]
    assert _extract_response_text(resp) == "done"


def test_extract_fallback_str() -> None:
    result = _extract_response_text("plain string")
    assert "plain string" in result


# ──────────────────────────────────────────────────────────────
# MemoryPlugin.on_event
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ignores_non_completed_events(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path)
    await plugin.on_event("request_started", {})
    assert buffer_count(plugin._cfg.buffer_path()) == 0


@pytest.mark.asyncio
async def test_capture_disabled(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, capture_enabled=False)
    resp = _mock_response(["long enough response " * 5])
    await plugin.on_event("request_completed", {"response": resp, "provider": "anthropic"})
    assert buffer_count(plugin._cfg.buffer_path()) == 0


@pytest.mark.asyncio
async def test_captures_long_response(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, capture_enabled=True, min_capture_chars=10)
    resp = _mock_response(["This is a sufficiently long response for capture."])
    await plugin.on_event("request_completed", {"response": resp, "provider": "anthropic"})
    assert buffer_count(plugin._cfg.buffer_path()) == 1


@pytest.mark.asyncio
async def test_skips_short_response(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, capture_enabled=True, min_capture_chars=200)
    resp = _mock_response(["short"])
    await plugin.on_event("request_completed", {"response": resp, "provider": "anthropic"})
    assert buffer_count(plugin._cfg.buffer_path()) == 0


@pytest.mark.asyncio
async def test_provider_stored(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, capture_enabled=True, min_capture_chars=5)
    resp = _mock_response(["hello world response"])
    await plugin.on_event("request_completed", {"response": resp, "provider": "openai"})
    entries = read_buffer(plugin._cfg.buffer_path())
    assert entries[0]["provider"] == "openai"
