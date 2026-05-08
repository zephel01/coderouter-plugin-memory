"""agentmemory HTTP backend.

Talks to a running [agentmemory](https://github.com/rohitg00/agentmemory)
server over HTTP. agentmemory is the recommended backend for this
plugin: R@5 95.2% on LongMemEval-S, 4-tier consolidation, 51 MCP
tools, hybrid BM25 + vector + graph search. We don't reimplement any
of that — this adapter is a thin HTTP shim.

Public-facing endpoints used (all under ``base_url + /agentmemory/``):

- ``GET  /health``         — liveness probe (always public, no auth).
- ``POST /smart-search``   — hybrid search; consumed by ``smart_search()``.
- ``POST /remember``       — store a free-text memory blob; consumed by
                             ``observe()``.

Why ``/remember`` rather than ``/observe`` (P0 finding, 2026-05-08)
=================================================================

agentmemory ships TWO write endpoints with different audiences:

- ``/observe`` requires ``hookType``, ``sessionId``, ``project``, ``cwd``,
  ``timestamp`` — i.e. a full Claude Code hook context. It's designed
  for the ``claude-code`` hook pipeline that tags every tool invocation.
- ``/remember`` accepts a single ``content`` string and is the natural
  fit for any caller that doesn't have hook-grade context.

CodeRouter sits at the wire layer between agent and LLM backend; we
don't have ``hookType`` semantics or a ``cwd`` that necessarily matches
the agent's idea of "current project". So we serialize the request /
response pair into a tagged text blob and POST it to ``/remember``.
agentmemory's hybrid search picks it up just like a hook-fed memory.

Authentication
==============

When ``AGENTMEMORY_SECRET`` is set on the agentmemory server, every
non-``/health`` endpoint requires ``Authorization: Bearer <secret>``.
This backend reads the secret from the env var named in
``secret_env`` (defaults to ``AGENTMEMORY_SECRET``) at construction
time. Health probes never carry the header so they exercise the
real public path.

Response-shape robustness
=========================

agentmemory's REST surface is documented at the path/method level
but the exact JSON body shape can drift between minor releases. We
tolerate that by:

1. Parsing the response defensively — multiple plausible field
   paths (``context`` / ``results`` / top-level array) are tried in
   order and the first non-empty one wins.
2. Treating any 2xx response with an unrecognized body as "no
   memory found" rather than an error. The plugin's degrade
   pathway then returns the request unchanged.
3. Surfacing the raw payload in a ``debug`` log line when
   ``debug_responses=True`` so an operator can inspect the actual
   shape and either adjust agentmemory's version or open an issue
   here.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from coderouter_plugin_memory.backends.base import (
    MemoryBackend,
    MemoryBackendError,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------

# agentmemory's default REST port (from its README's "API" section).
_DEFAULT_ENDPOINT = "http://localhost:3111"

# Maximum time to wait for a search call. agentmemory advertises
# sub-millisecond HNSW retrieval, so 5s is generous; user can tighten
# this if their backend is overloaded.
_DEFAULT_TIMEOUT_S = 5.0

# Health probes have a tighter timeout — we don't want a slow probe
# to add latency to the request when agentmemory is wedged. The
# backend simply degrades to NullBackend if health repeatedly fails.
_HEALTH_TIMEOUT_S = 2.0

# Per-search row cap. agentmemory honors a ``limit`` field on the
# search request body; without it, the server picks a reasonable
# default. We send a value here so the response stays bounded even
# when the server's default is generous.
_DEFAULT_SEARCH_LIMIT = 20

# Default env var name for the bearer secret. Users can override
# via the ``secret_env`` constructor kwarg.
_DEFAULT_SECRET_ENV = "AGENTMEMORY_SECRET"


# ----------------------------------------------------------------------
# Public class
# ----------------------------------------------------------------------


class AgentMemoryBackend(MemoryBackend):
    """HTTP client adapter for an agentmemory server.

    Args:
        endpoint: base URL where the agentmemory REST API listens.
            Default: ``http://localhost:3111``. Trailing slash optional.
        timeout_s: per-request timeout for search/observe calls.
            Default 5s.
        secret_env: name of the env var holding the bearer token.
            Default ``AGENTMEMORY_SECRET``. The token is read at
            construction time; if the env var is unset or empty,
            requests are sent unauthenticated (which is fine if
            agentmemory was started without ``AGENTMEMORY_SECRET``).
        search_limit: max rows the server should return per search.
            Default 20. Memory is rendered into a single text block
            sized by ``inject_token_budget`` upstream, so this is a
            ceiling, not a precise cap.
        debug_responses: when True, log the raw JSON body of every
            search response at debug level. Useful for diagnosing
            field-name drift between agentmemory versions.
        **_kwargs: ignored — we accept arbitrary kwargs so the same
            ``plugins.config.memory:`` block can be reused across
            backends without surgical edits.
    """

    name = "agentmemory"

    def __init__(
        self,
        *,
        endpoint: str = _DEFAULT_ENDPOINT,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        secret_env: str = _DEFAULT_SECRET_ENV,
        search_limit: int = _DEFAULT_SEARCH_LIMIT,
        debug_responses: bool = False,
        **_kwargs: Any,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError(f"timeout_s must be positive, got {timeout_s}")
        if search_limit <= 0:
            raise ValueError(f"search_limit must be positive, got {search_limit}")

        self._endpoint = endpoint.rstrip("/")
        self._timeout_s = timeout_s
        self._secret_env = secret_env
        self._secret = os.environ.get(secret_env, "").strip() or None
        self._search_limit = search_limit
        self._debug_responses = debug_responses

    # ------------------------------------------------------------------
    # MemoryBackend Protocol surface
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        """GET /agentmemory/health. Returns True on 2xx, False on anything else."""
        try:
            async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_S) as client:
                resp = await client.get(self._url("/agentmemory/health"))
                return 200 <= resp.status_code < 300
        except Exception as exc:
            logger.warning(
                "agentmemory-health-failed",
                extra={
                    "endpoint": self._endpoint,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                },
            )
            return False

    async def smart_search(
        self,
        *,
        project_id: str,
        query: str,
        token_budget: int,
    ) -> str:
        """POST /agentmemory/smart-search. Returns formatted memory text or ''.

        Empty string is the no-memory signal — that's not an error
        (agentmemory might just not have anything for this project
        yet). Network or HTTP failures raise
        :class:`MemoryBackendError`; the plugin's Inject hook catches
        and degrades to "no inject".
        """
        if token_budget <= 0:
            return ""

        body = {
            "query": query,
            "project_id": project_id,
            "limit": self._search_limit,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    self._url("/agentmemory/smart-search"),
                    json=body,
                    headers=self._auth_headers(),
                )
        except httpx.HTTPError as exc:
            raise MemoryBackendError(
                f"agentmemory smart-search transport error: {exc}"
            ) from exc

        if not (200 <= resp.status_code < 300):
            raise MemoryBackendError(
                f"agentmemory smart-search returned status "
                f"{resp.status_code}: {resp.text[:200]}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise MemoryBackendError(
                f"agentmemory smart-search returned non-JSON body: {exc}"
            ) from exc

        if self._debug_responses:
            logger.debug(
                "agentmemory-search-response",
                extra={"endpoint": self._endpoint, "body": payload},
            )

        return _extract_context_text(payload, char_budget=token_budget * 4)

    async def observe(
        self,
        *,
        project_id: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        """POST /agentmemory/remember. Best-effort record of one request.

        We collapse the request / response pair into a single text
        blob tagged with ``project_id`` so agentmemory's hybrid search
        can find it later. ``project_id`` is included in the body
        too in case agentmemory's future versions key on a structured
        field; current versions ignore unknown keys.
        """
        request_summary = _summarize_request(request)
        response_summary = _summarize_response(response)

        # Shape the content as a recognizable session block. Including
        # the project_id in the text lets the BM25 side of agentmemory's
        # hybrid search match on it even when there's no structured
        # project filter on the search call.
        content = (
            f"[project={project_id}]\n"
            f"user: {request_summary.get('last_user_message', '')}\n"
            f"assistant: {response_summary.get('text', '')}"
        )

        body: dict[str, Any] = {
            "content": content,
            # Carry the structured project_id too; agentmemory ignores
            # unknown keys today but may key on it in future versions.
            "project_id": project_id,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    self._url("/agentmemory/remember"),
                    json=body,
                    headers=self._auth_headers(),
                )
        except httpx.HTTPError as exc:
            raise MemoryBackendError(
                f"agentmemory remember transport error: {exc}"
            ) from exc

        if not (200 <= resp.status_code < 300):
            raise MemoryBackendError(
                f"agentmemory remember returned status "
                f"{resp.status_code}: {resp.text[:200]}"
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self._endpoint + path

    def _auth_headers(self) -> dict[str, str]:
        if self._secret is None:
            return {}
        return {"Authorization": f"Bearer {self._secret}"}


# ----------------------------------------------------------------------
# Response parsing — defensive, version-tolerant
# ----------------------------------------------------------------------


def _extract_context_text(payload: Any, *, char_budget: int) -> str:
    """Best-effort extraction of memory text from a search response.

    agentmemory's response body could be any of:

    1. ``{"context": "<plain text block>"}`` — preformatted, ready to inject.
    2. ``{"results": [{"text": "...", "score": float, ...}, ...]}`` — list
       of hits to render.
    3. ``{"data": <one of the above>}`` — common JSON-API envelope.
    4. ``[...]`` — raw array of hits.
    5. Anything else — treat as "no memory" and return "".

    We try them in order, format hits into our standard
    "[Past session ...]\\nuser: ...\\nassistant: ..." block when
    rendering from list shapes, and bail at ``char_budget`` so we
    don't blow the inject budget upstream.
    """
    if payload is None:
        return ""

    # Unwrap common envelope.
    if isinstance(payload, dict) and "data" in payload and len(payload) == 1:
        payload = payload["data"]

    # Shape 1: preformatted text.
    if isinstance(payload, dict):
        ctx = payload.get("context")
        if isinstance(ctx, str) and ctx.strip():
            return _truncate(ctx, char_budget)
        # Shape 2: list of hits under "results".
        results = payload.get("results")
        if isinstance(results, list):
            return _render_hits(results, char_budget=char_budget)
        # Some servers use "items" / "memories" / "hits" — try them too.
        for alt in ("items", "memories", "hits", "matches"):
            value = payload.get(alt)
            if isinstance(value, list):
                return _render_hits(value, char_budget=char_budget)

    # Shape 4: raw list.
    if isinstance(payload, list):
        return _render_hits(payload, char_budget=char_budget)

    return ""


def _render_hits(hits: list[Any], *, char_budget: int) -> str:
    """Render a list of hit dicts as a recency-shaped text block."""
    if not hits:
        return ""

    parts: list[str] = []
    used = 0

    for hit in hits:
        block = _format_hit(hit)
        if not block:
            continue
        if used + len(block) > char_budget:
            if parts:
                break
            block = block[: max(char_budget - used, 0)]
            parts.append(block)
            break
        parts.append(block)
        used += len(block)

    return "\n".join(parts)


def _format_hit(hit: Any) -> str:
    """Format a single hit dict into our session-block shape.

    Falls back to a simple ``str(hit)`` if the dict has no recognized
    text fields — better to inject something than to silently drop
    a result.
    """
    if isinstance(hit, str):
        return hit + "\n"

    if not isinstance(hit, dict):
        return ""

    timestamp = (
        hit.get("timestamp")
        or hit.get("created_at")
        or hit.get("ts")
        or "unknown"
    )

    # Two preferred shapes:
    #   {"user_message": "...", "assistant_text": "..."}  (our builtin shape)
    #   {"text": "...", ...}                              (agentmemory hits)
    user = hit.get("user_message") or hit.get("user") or hit.get("input")
    asst = (
        hit.get("assistant_text")
        or hit.get("assistant")
        or hit.get("output")
        or hit.get("text")
        or hit.get("content")
    )

    if not (user or asst):
        return ""

    block_lines = [f"[Past session {timestamp}]"]
    if user:
        block_lines.append(f"user: {_to_plain(user)}")
    if asst:
        block_lines.append(f"assistant: {_to_plain(asst)}")
    return "\n".join(block_lines) + "\n"


def _to_plain(value: Any) -> str:
    """Coerce a hit field (str / dict / list) to plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                t = item.get("text") or item.get("content")
                if isinstance(t, str):
                    chunks.append(t)
        return "\n".join(c for c in chunks if c)
    if isinstance(value, dict):
        t = value.get("text") or value.get("content")
        if isinstance(t, str):
            return t
    return str(value)


def _truncate(text: str, char_budget: int) -> str:
    if len(text) <= char_budget:
        return text
    return text[:char_budget]


# ----------------------------------------------------------------------
# Request / response summarization for /observe
# ----------------------------------------------------------------------


def _summarize_request(request: dict[str, Any]) -> dict[str, Any]:
    """Pull the user-visible parts of an Anthropic-shaped request dict.

    We deliberately don't forward the full message list — agentmemory
    has its own privacy / compression pipeline that prefers a
    pre-summarized payload. Sending only the last user message keeps
    the wire small and matches the builtin backend's behavior.
    """
    user_msg = ""
    messages = request.get("messages") or []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        user_msg = _flatten_content(msg.get("content"))
        break

    return {
        "last_user_message": user_msg,
        "had_tools": bool(request.get("tools")),
        "stream": bool(request.get("stream")),
    }


def _summarize_response(response: dict[str, Any]) -> dict[str, Any]:
    """Pull text + minimal metadata out of an Anthropic-shaped response dict."""
    text = ""
    for block in response.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text += str(block.get("text") or "")

    usage = response.get("usage") or {}
    return {
        "text": text,
        "model": response.get("model"),
        "stop_reason": response.get("stop_reason"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                chunks.append(block)
        return "\n".join(c for c in chunks if c)
    return str(content)
