# Changelog

All notable changes to `coderouter-plugin-memory` are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/),
versioning follows [SemVer](https://semver.org/).

---

## [0.4.1] — 2026-07-04 (review-driven correctness pass)

Follow-up to the 0.4.0 rewrite. No public API change; existing 0.4.x configs
keep working.

### Fixed
- **Empty `project` no longer collapses the storage path.** `MemoryConfig`
  now falls back to `"default"` when `project` is blank/whitespace (and
  `_safe_name("")` returns `"default"`), so `project: ""` can't make
  `project_dir()` resolve to `state_dir` itself and mix projects together.
- **Stale/unknown config keys no longer crash the plugin.** `MemoryPlugin`
  filters kwargs to known `MemoryConfig` fields and warns on the rest, so a
  pre-0.4 `providers.yaml` (with `backend:` / `endpoint:` / …) degrades
  instead of raising `TypeError` through the router.
- **Unknown response types are no longer persisted.** `_extract_response_text`
  returns `""` (and logs) for objects that are neither the expected content
  list nor a plain `str`, avoiding writing an arbitrary `__repr__` (which
  could contain secrets) to `buffer.jsonl`.

### Changed
- **CircuitBreaker is wired in (was dead code).** `consolidate` now guards the
  Ollama call with a per-process breaker and a configurable
  `consolidate_timeout_s` (default 60), and takes a `.consolidate.lock` to
  prevent concurrent runs from corrupting `buffer.jsonl` / `facts.jsonl`.
  `CircuitBreaker` is also exported from the package.
- `build_inject_text` rewritten to trim the token budget in O(n) with a single
  string assembly (was an O(n²) rebuild-per-drop loop with duplicated code).
- Examples rewritten to the v0.4.x schema (the old `backend:` / `endpoint:` /
  `secret_env:` / `circuit_breaker_*` keys are gone); `providers.null.yaml`
  now uses `capture_enabled: false` / `inject_enabled: false`.
- `pyproject.toml` header and `examples/README.md` updated to describe the
  JSONL architecture; `scripts/smoke_agentmemory.sh` stubbed as deprecated.
- Clarified the `read_facts` docstring (returns the newest N in record order,
  not re-sorted).

## [0.4.0] — 2026-06-XX (full rewrite: single JSONL backend)

**Breaking.** The multi-backend design was replaced by one dependency-free
local store. This is the release the 0.3.x changelog entries below no longer
describe, so it is recorded here explicitly.

### Removed
- The `backend` selector and all non-builtin backends: `agentmemory` (HTTP),
  `mem0` (SDK), and `null`. With them went the config keys `backend`,
  `endpoint`, `secret_env`, `search_limit`, and `circuit_breaker_*`, and the
  `httpx` runtime dependency (now stdlib-only).

### Changed
- Storage is now JSONL under `~/.coderouter/memory/<project>/`
  (`buffer.jsonl`, `facts.jsonl`, `manual.md`) instead of sqlite3 / a remote
  server.
- Architecture is three explicit phases: **capture** (Observer →
  `buffer.jsonl`), **consolidate** (CLI/cron → a local Ollama model →
  `facts.jsonl`), **inject** (InputFilter → system-prompt prepend). The old
  `inject.py` / `record.py` modules were merged into `plugin.py`.
- Disabling memory is now `capture_enabled: false` + `inject_enabled: false`
  (there is no `backend: null`).

### Notes
- `_circuit.py` shipped but was left unwired in 0.4.0; it is connected to the
  consolidate path in 0.4.1 above.

---

## [0.3.1] — 2026-05-08 (P0 smoke fix — switch agentmemory observe to /remember)

Patch over `0.3.0`. P0 (live-endpoint smoke verification) ran against a
real agentmemory server and surfaced the real wire shape — `/observe`
isn't the right endpoint for a wire-level plugin.

### Fixed

- **`agentmemory observe returned status 400`** at runtime. agentmemory's
  REST surface ships TWO write endpoints with different audiences:

  | Endpoint | Required fields | Designed for |
  |---|---|---|
  | `/agentmemory/observe`  | `hookType`, `sessionId`, `project`, `cwd`, `timestamp` | Claude Code hook pipeline (full hook context) |
  | `/agentmemory/remember` | `content` (string)                         | Anything else — generic free-text memory |

  v0.3.0's plugin posted to `/observe` with a `tool_name` + `input` +
  `output` shape, which agentmemory rejected with
  `{"error":"hookType, sessionId, project, cwd, and timestamp are required strings"}`.

  Fix: switch to `/agentmemory/remember`, serialize the request /
  response pair into a single text blob tagged with `[project=...]`
  so agentmemory's hybrid search picks it up. The structured
  `project_id` is also passed in the body for forward compatibility
  (agentmemory ignores unknown keys today).

  Side effect: agentmemory now responds with `201 Created` (not 200);
  the success-status check uses the `200-299` range so this works
  without further changes.

### Changed

- `coderouter_plugin_memory/backends/agentmemory.py::observe()` now
  posts to `/agentmemory/remember` with `{"content": "...", "project_id": "..."}`.
- Module docstring documents the `/observe` vs `/remember` distinction
  and the rationale for picking the latter at the wire layer.

### Tests

- `tests/test_backend_agentmemory.py::TestObserve::test_request_shape`
  rewritten to assert the `/remember` URL + content-string body shape.
  Other tests unaffected.

### Migration

`pip install -U coderouter-plugin-memory` (or `--pre` if alphas) — no
config changes required. `providers.yaml` stays the same.

---

## [0.3.0] — 2026-05-08 (initial public release)

First public release. All four implementation phases (P1–P4 in the
[design plan](https://github.com/zephel01/CodeRouter/blob/main/docs/inside/v2.3-plugin-memory-plan.md))
landed together in this release: the plugin discovery host (P1) ships
in CodeRouter `v2.3.0a1`, while the plugin code itself (P2/P3/P4)
ships here. 112 unit tests, all passing.

### Added — core plugin

- **`MemoryInjector`** — `coderouter.input_filter` entry point.
  Wraps memory in a `<previous-session-context>` envelope and
  prepends it to `request.system`. Tolerates `system` being `None`,
  a `str`, or the Anthropic-style list-of-text-blocks shape.
- **`MemoryRecorder`** — `coderouter.observer` entry point.
  Listens for `request_completed` events, serializes Pydantic
  models via `model_dump(mode="json")`, swallows all errors
  (Observer is best-effort by contract).
- **`project_id` resolution** — `CODEROUTER_PROJECT_ID` env override
  → `CODEROUTER_CONFIG` path hash → cwd hash, fallback chain. Stable
  across runs, namespace-isolated per project, no PII leaked into
  logs.

### Added — backend ecosystem

| Backend | Stdlib | Extra services | Search quality |
|---|---|---|---|
| `builtin`     | sqlite3 only         | none                                | LIKE on longest word, recency-ordered (deterministic `id DESC` tiebreak) |
| `agentmemory` | httpx                | needs `npx -y @agentmemory/agentmemory` running | hybrid BM25 + vector + graph (R@5 95.2% per agentmemory's LongMemEval-S report) |
| `null`        | none                 | none                                | n/a — explicit no-op |
| `mem0`        | (planned, P5)        | mem0 SDK + vector DB                | (planned, demand-driven) |

All four implement a single `MemoryBackend` Protocol. Switching is
one string in `providers.yaml`.

### Added — reliability

- **Circuit breaker** (`_circuit.CircuitBreaker`). Three-state
  machine (CLOSED / OPEN / HALF_OPEN). After
  `circuit_breaker_threshold` consecutive failures, the breaker
  opens for `circuit_breaker_cooldown_s` seconds, then issues a
  single probe via HALF_OPEN. Cooldown grows exponentially up to
  `max_cooldown_s` while the backend stays unhealthy. Reset on
  success.
- **Defensive response parsing** for the agentmemory backend —
  tolerates `{"context": "..."}`, `{"results": [...]}`,
  `{"data": ...}` envelopes, raw arrays, and several alternate
  field names. Unknown shapes degrade to "no memory found" rather
  than raising. `debug_responses=True` logs raw bodies at debug
  level for diagnosing field-name drift across agentmemory
  versions.
- **Failure-degrades-cleanly invariant.** Backend errors NEVER
  block routing. Search failures → no inject + warn log; observe
  failures → debug log only. The wire layer keeps moving even if
  memory is wedged.

### Added — examples + walkthrough

- `examples/providers.builtin.yaml`     — minimal sqlite3 config.
- `examples/providers.agentmemory.yaml` — recommended config with
  inline-documented circuit-breaker / auth / search-limit knobs.
- `examples/providers.null.yaml`        — explicit no-op.
- `examples/walkthrough_agent.py`       — 30-line OpenAI-SDK agent
  that imports zero memory libraries yet inherits memory across
  sessions through CodeRouter's wire.
- `examples/README.md`                  — index + run instructions.

### Added — smoke verification

- `scripts/smoke_agentmemory.sh` — manual end-to-end probe against
  a running agentmemory server. Exercises health → observe →
  smart-search round trip, prints actual response shapes for diff
  against the parser, asserts an observed keyword shows up in the
  subsequent search.

### Tests

| Module                         | Tests |
|--------------------------------|------:|
| `test_skeleton.py`             |     4 |
| `test_project_id.py`           |     5 |
| `test_backend_null.py`         |     4 |
| `test_backend_builtin.py`      |    18 |
| `test_inject.py`               |    20 |
| `test_record.py`               |    13 |
| `test_backend_agentmemory.py`  |    34 |
| `test_circuit.py`              |    14 |
| **Total**                      |   **112** |

### Compatibility

- Requires CodeRouter `>= 2.3.0a1` (Plugin SDK host).
- Python `>= 3.12`.
- One runtime dep: `httpx>=0.27.0` (used only by the agentmemory
  backend; builtin / null backends are stdlib-only).
- Optional: `coderouter-plugin-memory[mem0]` reserved for P5.
