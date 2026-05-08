# Changelog

All notable changes to `coderouter-plugin-memory` are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/),
versioning follows [SemVer](https://semver.org/).

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
