"""Tests for ``coderouter_plugin_memory.backends.agentmemory``.

We mock ``httpx.AsyncClient`` instead of pulling in pytest-httpx so
the test runs in any environment that has ``httpx`` (which is a
runtime dep anyway). The mock substitutes the async client's request
methods with coroutines that return synthetic ``httpx.Response``
objects, which is enough fidelity to exercise:

- request body shape (we capture and assert on what the backend
  sent over the wire)
- response shape parsing (multiple JSON layouts → expected text)
- failure paths (transport error, non-2xx, non-JSON body)
- bearer-token plumbing
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from coderouter_plugin_memory.backends import (
    AgentMemoryBackend,
    MemoryBackendError,
)
from coderouter_plugin_memory.backends.agentmemory import (
    _extract_context_text,
    _format_hit,
    _summarize_request,
    _summarize_response,
)

# ----------------------------------------------------------------------
# Helpers — fake httpx.Response + fake AsyncClient
# ----------------------------------------------------------------------


def _fake_response(
    *,
    status: int = 200,
    body: Any = None,
    text_body: str | None = None,
) -> httpx.Response:
    """Build a real httpx.Response so .json() / .text / .status_code work."""
    if text_body is not None:
        content = text_body.encode("utf-8")
    elif body is None:
        content = b""
    else:
        content = json.dumps(body).encode("utf-8")
    headers = {"content-type": "application/json"} if body is not None else {}
    return httpx.Response(status_code=status, headers=headers, content=content)


class _FakeAsyncClient:
    """Captures GET/POST calls and returns a queued response per call.

    Built so we can assert on what URL + JSON body the backend sent
    and on the headers it set (auth).
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.gets: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        self.gets.append({"url": url, **kw})
        return self._next()

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        self.posts.append({"url": url, **kw})
        return self._next()

    def _next(self) -> httpx.Response:
        if not self._responses:
            raise AssertionError("ran out of queued responses")
        return self._responses.pop(0)


def _patch_client(client: _FakeAsyncClient):
    """Patch ``httpx.AsyncClient`` constructor to return our fake."""
    return patch(
        "coderouter_plugin_memory.backends.agentmemory.httpx.AsyncClient",
        return_value=client,
    )


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------


class TestConstruction:
    def test_default_endpoint(self) -> None:
        b = AgentMemoryBackend()
        # _endpoint has trailing slash stripped.
        assert b._endpoint == "http://localhost:3111"

    def test_strips_trailing_slash(self) -> None:
        b = AgentMemoryBackend(endpoint="http://example.com/")
        assert b._endpoint == "http://example.com"

    def test_secret_read_at_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTMEMORY_SECRET", "shh")
        b = AgentMemoryBackend()
        assert b._auth_headers() == {"Authorization": "Bearer shh"}

    def test_no_secret_means_no_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTMEMORY_SECRET", raising=False)
        b = AgentMemoryBackend()
        assert b._auth_headers() == {}

    def test_custom_secret_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_TOKEN", "abc")
        b = AgentMemoryBackend(secret_env="MY_TOKEN")
        assert b._auth_headers() == {"Authorization": "Bearer abc"}

    def test_negative_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match="timeout_s"):
            AgentMemoryBackend(timeout_s=-1.0)

    def test_zero_search_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="search_limit"):
            AgentMemoryBackend(search_limit=0)


# ----------------------------------------------------------------------
# health()
# ----------------------------------------------------------------------


class TestHealth:
    def test_2xx_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeAsyncClient([_fake_response(status=200, body={"ok": True})])
        b = AgentMemoryBackend()
        with _patch_client(client):
            assert asyncio.run(b.health()) is True
        assert client.gets[0]["url"] == "http://localhost:3111/agentmemory/health"

    def test_5xx_returns_false(self) -> None:
        client = _FakeAsyncClient([_fake_response(status=503, text_body="busy")])
        b = AgentMemoryBackend()
        with _patch_client(client):
            assert asyncio.run(b.health()) is False

    def test_transport_error_returns_false(self) -> None:
        # Make the .get() coroutine raise.
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        b = AgentMemoryBackend()
        with patch(
            "coderouter_plugin_memory.backends.agentmemory.httpx.AsyncClient",
            return_value=client,
        ):
            assert asyncio.run(b.health()) is False


# ----------------------------------------------------------------------
# smart_search()
# ----------------------------------------------------------------------


class TestSmartSearch:
    def test_request_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTMEMORY_SECRET", "tok")
        client = _FakeAsyncClient(
            [_fake_response(status=200, body={"context": "X"})]
        )
        b = AgentMemoryBackend(search_limit=7)

        with _patch_client(client):
            asyncio.run(
                b.smart_search(
                    project_id="proj-z", query="auth flow", token_budget=2000
                )
            )

        assert len(client.posts) == 1
        post = client.posts[0]
        assert post["url"] == "http://localhost:3111/agentmemory/smart-search"
        assert post["json"] == {
            "query": "auth flow",
            "project_id": "proj-z",
            "limit": 7,
        }
        assert post["headers"] == {"Authorization": "Bearer tok"}

    def test_zero_budget_skips_network_call(self) -> None:
        client = _FakeAsyncClient([])  # no responses queued = would error if called
        b = AgentMemoryBackend()
        with _patch_client(client):
            out = asyncio.run(
                b.smart_search(
                    project_id="proj-x", query="q", token_budget=0
                )
            )
        assert out == ""
        assert client.posts == []

    def test_preformatted_context_response(self) -> None:
        client = _FakeAsyncClient(
            [_fake_response(body={"context": "Memory text here"})]
        )
        b = AgentMemoryBackend()
        with _patch_client(client):
            out = asyncio.run(
                b.smart_search(project_id="p", query="q", token_budget=2000)
            )
        assert out == "Memory text here"

    def test_results_array_response(self) -> None:
        body = {
            "results": [
                {
                    "timestamp": "2026-05-08T03:14Z",
                    "user": "set up auth",
                    "assistant": "done",
                },
                {
                    "timestamp": "2026-05-07T01:00Z",
                    "text": "raw text-only hit",
                },
            ]
        }
        client = _FakeAsyncClient([_fake_response(body=body)])
        b = AgentMemoryBackend()
        with _patch_client(client):
            out = asyncio.run(
                b.smart_search(project_id="p", query="q", token_budget=2000)
            )
        assert "Past session 2026-05-08T03:14Z" in out
        assert "set up auth" in out
        assert "done" in out
        assert "raw text-only hit" in out

    def test_data_envelope_unwrap(self) -> None:
        body = {"data": {"context": "wrapped"}}
        client = _FakeAsyncClient([_fake_response(body=body)])
        b = AgentMemoryBackend()
        with _patch_client(client):
            out = asyncio.run(
                b.smart_search(project_id="p", query="q", token_budget=2000)
            )
        assert out == "wrapped"

    def test_unrecognized_shape_returns_empty(self) -> None:
        body = {"surprise": True}
        client = _FakeAsyncClient([_fake_response(body=body)])
        b = AgentMemoryBackend()
        with _patch_client(client):
            out = asyncio.run(
                b.smart_search(project_id="p", query="q", token_budget=2000)
            )
        assert out == ""

    def test_truncates_to_char_budget(self) -> None:
        long_text = "x" * 10000
        body = {"context": long_text}
        client = _FakeAsyncClient([_fake_response(body=body)])
        b = AgentMemoryBackend()
        with _patch_client(client):
            out = asyncio.run(
                b.smart_search(
                    project_id="p", query="q", token_budget=100  # 400 chars
                )
            )
        assert len(out) == 400

    def test_non_2xx_raises(self) -> None:
        client = _FakeAsyncClient(
            [_fake_response(status=500, text_body="boom")]
        )
        b = AgentMemoryBackend()
        with _patch_client(client), pytest.raises(MemoryBackendError, match="500"):
            asyncio.run(
                b.smart_search(project_id="p", query="q", token_budget=2000)
            )

    def test_transport_error_raises(self) -> None:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(
            side_effect=httpx.ConnectError("no server")
        )
        b = AgentMemoryBackend()
        with patch(
            "coderouter_plugin_memory.backends.agentmemory.httpx.AsyncClient",
            return_value=client,
        ), pytest.raises(MemoryBackendError, match="transport"):
            asyncio.run(
                b.smart_search(project_id="p", query="q", token_budget=2000)
            )

    def test_non_json_body_raises(self) -> None:
        client = _FakeAsyncClient(
            [_fake_response(status=200, text_body="not json")]
        )
        b = AgentMemoryBackend()
        with _patch_client(client), pytest.raises(MemoryBackendError, match="non-JSON"):
            asyncio.run(
                b.smart_search(project_id="p", query="q", token_budget=2000)
            )


# ----------------------------------------------------------------------
# observe()
# ----------------------------------------------------------------------


class TestObserve:
    """``observe()`` posts to ``/agentmemory/remember`` (the simple
    text-store endpoint), NOT to ``/observe`` (which requires Claude
    Code hook context: hookType / sessionId / project / cwd / timestamp).
    See P0 smoke results 2026-05-08 in CHANGELOG."""

    def test_request_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTMEMORY_SECRET", raising=False)
        # /remember returns 201 Created (NOT 200) on success.
        client = _FakeAsyncClient(
            [_fake_response(status=201, body={"success": True, "memory": {"id": "mem_x"}})]
        )
        b = AgentMemoryBackend()
        request = {
            "messages": [
                {"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "ack"},
                {"role": "user", "content": "the actual question"},
            ],
            "tools": [{"name": "Read"}],
            "stream": False,
        }
        response = {
            "model": "claude-3-5-sonnet",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "answer"}],
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }
        with _patch_client(client):
            asyncio.run(
                b.observe(project_id="proj-z", request=request, response=response)
            )

        assert len(client.posts) == 1
        post = client.posts[0]
        # /remember endpoint, not /observe.
        assert post["url"] == "http://localhost:3111/agentmemory/remember"
        body = post["json"]
        # /remember requires `content` (string) only; we tag with project_id.
        assert body["project_id"] == "proj-z"
        assert isinstance(body["content"], str)
        assert "[project=proj-z]" in body["content"]
        assert "the actual question" in body["content"]
        assert "answer" in body["content"]
        # No auth env → no header.
        assert post["headers"] == {}

    def test_non_2xx_raises(self) -> None:
        client = _FakeAsyncClient(
            [_fake_response(status=400, text_body="bad")]
        )
        b = AgentMemoryBackend()
        with _patch_client(client), pytest.raises(MemoryBackendError, match="400"):
            asyncio.run(
                b.observe(
                    project_id="p",
                    request={"messages": []},
                    response={"content": []},
                )
            )


# ----------------------------------------------------------------------
# Pure-function helpers
# ----------------------------------------------------------------------


class TestExtractContextText:
    def test_none_returns_empty(self) -> None:
        assert _extract_context_text(None, char_budget=100) == ""

    def test_dict_with_blank_context_falls_through(self) -> None:
        assert _extract_context_text({"context": "   "}, char_budget=100) == ""

    def test_alt_list_field_names(self) -> None:
        # ``items`` and friends are recognized.
        body = {
            "items": [
                {"text": "hello world", "timestamp": "2026-05-01"},
            ]
        }
        out = _extract_context_text(body, char_budget=200)
        assert "hello world" in out
        assert "2026-05-01" in out

    def test_raw_list_payload(self) -> None:
        body = [{"text": "alpha"}, {"text": "beta"}]
        out = _extract_context_text(body, char_budget=200)
        assert "alpha" in out and "beta" in out


class TestFormatHit:
    def test_str_hit(self) -> None:
        assert _format_hit("plain") == "plain\n"

    def test_dict_with_text_only(self) -> None:
        out = _format_hit({"text": "X", "timestamp": "T"})
        assert "Past session T" in out
        assert "assistant: X" in out

    def test_dict_user_assistant(self) -> None:
        out = _format_hit(
            {"timestamp": "T", "user": "Q", "assistant": "A"}
        )
        assert "Past session T" in out
        assert "user: Q" in out
        assert "assistant: A" in out

    def test_unknown_dict_returns_empty(self) -> None:
        assert _format_hit({"unrelated": "stuff"}) == ""

    def test_list_value_flattened(self) -> None:
        out = _format_hit(
            {
                "timestamp": "T",
                "assistant": [
                    {"type": "text", "text": "alpha"},
                    {"type": "text", "text": "beta"},
                ],
            }
        )
        assert "alpha" in out and "beta" in out


class TestSummarize:
    def test_summarize_request(self) -> None:
        out = _summarize_request(
            {
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "ack"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "latest part 1"},
                            {"type": "text", "text": "latest part 2"},
                        ],
                    },
                ],
                "tools": [{"name": "Read"}],
                "stream": True,
            }
        )
        assert out["last_user_message"] == "latest part 1\nlatest part 2"
        assert out["had_tools"] is True
        assert out["stream"] is True

    def test_summarize_request_no_user(self) -> None:
        out = _summarize_request({"messages": [{"role": "assistant", "content": "x"}]})
        assert out["last_user_message"] == ""
        assert out["had_tools"] is False

    def test_summarize_response(self) -> None:
        out = _summarize_response(
            {
                "model": "x",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "alpha"},
                    {"type": "tool_use", "name": "Read", "input": {}},
                    {"type": "text", "text": "beta"},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            }
        )
        assert out["text"] == "alphabeta"
        assert out["input_tokens"] == 1
        assert out["output_tokens"] == 2
