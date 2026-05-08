<h1 align="center">coderouter-plugin-memory</h1>

<p align="center">
  <strong>Stop re-explaining your project every session.<br>One wire-level plugin handles it.</strong>
</p>

<p align="center">
  <a href="https://github.com/zephel01/coderouter-plugin-memory/actions/workflows/ci.yml"><img src="https://github.com/zephel01/coderouter-plugin-memory/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://pypi.org/project/coderouter-plugin-memory/"><img src="https://img.shields.io/pypi/v/coderouter-plugin-memory?color=blue" alt="version"></a>
  <a href=""><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="python"></a>
  <a href=""><img src="https://img.shields.io/badge/runtime%20deps-1-brightgreen" alt="deps"></a>
  <a href=""><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license"></a>
</p>

<p align="center">
  <a href="./README.md">日本語</a> · <strong>English</strong> · <a href="https://github.com/zephel01/CodeRouter/blob/main/docs/inside/v2.3-plugin-memory-plan.md">Design doc</a> · <a href="https://github.com/zephel01/CodeRouter">CodeRouter</a>
</p>

> **Status (v0.3.0).** Initial release on PyPI: four backends (`builtin` / `agentmemory` / `null` / planned `mem0`), circuit breaker, 112 unit tests. Pairs with the Plugin SDK shipped in CodeRouter `v2.3.0a1+`. See [CHANGELOG](./CHANGELOG.md) and the [design doc](https://github.com/zephel01/CodeRouter/blob/main/docs/inside/v2.3-plugin-memory-plan.md).

---

## In 30 seconds

```
Your agent (Claude Code / Cursor / your-own-agent)
                │   ← memory-unaware, no SDK changes
                ▼
        ┌─ CodeRouter ───────────────┐
        │  ① pre-request hook         │ ─→ smart-search the memory backend
        │     append_system_prompt    │
        │  ② routing + L1-L6 guards   │
        │  ③ post-response hook       │ ─→ observe (record into backend)
        └─────────────────────────────┘
                │
                ▼
            Local LLM (Ollama / LM Studio / cloud)
```

**What it does for you:**

- The agent inherits memory **without knowing memory exists** — wire-layer injection
- The memory engine is **swappable** (sqlite3 builtin / agentmemory recommended / mem0 / null)
- **Zero code changes** in your agent — just route through CodeRouter
- If memory breaks, routing keeps going (degrade pathway built in)
- Same memory experience for Claude Code and your-own-agent — they share the wire

---

## Why "wire-layer"?

Most agent-memory tools ([agentmemory](https://github.com/rohitg00/agentmemory), [mem0](https://github.com/mem0ai/mem0), [Letta](https://github.com/letta-ai/letta)) live **agent-side**: the agent has to call `memory_save` / `memory_recall` via MCP tools or an SDK. That's fine for Claude Code or Cursor, which already speak MCP — but it leaves out **anyone writing their own agent** or using a tool that doesn't.

This plugin sits in the **wire layer** instead (between agent and LLM backend):

| Aspect            | Agent-side memory tools (agentmemory, etc.) | This plugin (wire-layer)                       |
|-------------------|--------------------------------------------|-----------------------------------------------|
| Agent-side code   | MCP client / SDK calls required            | **None** — just route through CodeRouter      |
| Supported agents  | MCP-capable agents only                    | **Any agent that speaks the Anthropic API**   |
| Memory engine     | Built-in                                   | **Delegated to a backend (agentmemory etc.)** |
| Feature depth     | Self-contained                             | Wire-layer injection only (engine = backend)  |

The two are not competitors — they're complements. The ideal setup is **both**: the agent calls agentmemory directly via MCP, AND CodeRouter injects memory transparently in the wire. That way, memory works regardless of how diligent the agent is about calling its memory tools.

---

## Usage

### 1. Install

```bash
# CodeRouter (v2.3.0+ required)
uv tool install coderouter-cli

# This plugin
pip install coderouter-plugin-memory
```

### 2. Start a memory backend (`agentmemory` recommended)

```bash
# In a separate terminal
npx -y @agentmemory/agentmemory
# → listens on http://localhost:3111
```

### 3. Add to `providers.yaml`

```yaml
plugins:
  enabled:
    - memory                       # ← entry-point name, NOT the package name
  config:
    memory:
      backend: agentmemory         # builtin / agentmemory / mem0 / null
      endpoint: http://localhost:3111
      inject_token_budget: 2000    # cap on tokens injected into the system prompt
      secret_env: AGENTMEMORY_SECRET  # env var holding the auth token
```

### 4. Start CodeRouter

```bash
coderouter serve --port 8088
# Startup log: plugin-loaded plugin=memory group=input_filter / observer
```

That's it. Point your agent at CodeRouter and the previous session's context is restored automatically.

---

## Backends (available in `v0.3.0`)

| Backend       | Status     | When to pick this                                                                            |
|---------------|------------|---------------------------------------------------------------------------------------------|
| `builtin`     | ✅ v0.3.0  | You don't want extra services running. sqlite3 + LIKE search, minimum viable.                |
| `agentmemory` | ✅ v0.3.0  | **Recommended.** R@5 95.2% on LongMemEval-S, 4-tier consolidation, 92% token reduction. `npx`-launched. |
| `null`        | ✅ v0.3.0  | Explicit disable, or auto-fallback when the chosen backend is unhealthy.                     |
| `mem0`        | ⏳ Planned | You're already invested in [mem0](https://github.com/mem0ai/mem0) (demand-driven).            |

All backends implement the same `MemoryBackend` Protocol, so switching is just a string change in `providers.yaml`.

---

## Roadmap

| Phase | What                                                                  | Status |
|-------|-----------------------------------------------------------------------|--------|
| P1    | Plugin SDK in CodeRouter Core (`coderouter.plugins`)                  | ✅ [CodeRouter v2.3.0a1+](https://pypi.org/project/coderouter-cli/) |
| P2    | builtin sqlite3 backend / project_id / Inject / Record + tests        | ✅ v0.3.0 |
| P3    | agentmemory backend + integration tests + smoke script                | ✅ v0.3.0 |
| P4    | circuit breaker (degrade on consecutive failures) + walkthrough + examples | ✅ v0.3.0 |
| P0    | agentmemory live-endpoint smoke (`scripts/smoke_agentmemory.sh`)       | ⏳ run locally |
| P5    | mem0 backend                                                          | ⏳ demand-driven |

Detailed implementation plan: [`v2.3-plugin-memory-plan.md`](https://github.com/zephel01/CodeRouter/blob/main/docs/inside/v2.3-plugin-memory-plan.md)

---

## Walkthrough — your-own-agent gets memory for free

The real value lands when you're writing the agent yourself: **you get memory without writing memory code**.

### 30-line agent (`examples/walkthrough_agent.py` excerpt)

```python
import sys, os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ.get("CODEROUTER_BASE_URL", "http://localhost:8088/v1"),
    api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
)

resp = client.chat.completions.create(
    model="qwen3.6:35b-a3b",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": " ".join(sys.argv[1:])},
    ],
)
print(resp.choices[0].message.content)
```

**What's NOT in the script**:
- No `memory_save` / `memory_recall` calls
- No MCP client setup
- No sqlite / vector store / Redis imports
- No rate-limit, fallback chain, or drift detection logic

CodeRouter's wire layer handles all of it.

### Run it (two sessions)

```bash
# Terminal 1: optional — start agentmemory
npx -y @agentmemory/agentmemory   # skip if you're on the builtin backend

# Terminal 2: CodeRouter
coderouter serve --port 8088

# Terminal 3: run the agent twice
python examples/walkthrough_agent.py "Remember: the project's accent color is indigo."
# → "Got it — I'll remember the project color is indigo."

python examples/walkthrough_agent.py "What's the project's accent color?"
# → "Indigo."   ← previous session's context flows in transparently
```

The agent code has **zero lines** of "fetch memory" logic. The second answer remembers the first session because the plugin prepends a `<previous-session-context>` block to the system prompt at the wire layer.

See [`examples/`](./examples/README.md) for full code + sample provider configs.

**Build less in your agent, get more from the wire.**

---

## Related projects

| Project | Role | Relationship |
|---|---|---|
| [CodeRouter](https://github.com/zephel01/CodeRouter) | The wire-layer router itself | **Required** — hosts the Plugin SDK |
| [agentmemory](https://github.com/rohitg00/agentmemory) | Agent memory MCP server | **Recommended backend.** R@5 95.2% |
| [mem0](https://github.com/mem0ai/mem0) | Memory layer API | Optional backend |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Self-improving agent framework | Higher layer, complementary |
| [Plugin SDK design](https://github.com/zephel01/CodeRouter/blob/main/docs/inside/plugin-architecture-draft.md) | CodeRouter's plugin contract | The spec this plugin implements |

---

## License

MIT — see [LICENSE](./LICENSE).
