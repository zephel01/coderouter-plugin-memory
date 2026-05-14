<h1 align="center">coderouter-plugin-memory</h1>

<p align="center">
  <strong>Stop re-explaining your project every session.<br>One wire-level plugin handles it.</strong>
</p>

<p align="center">
  <a href="https://github.com/zephel01/coderouter-plugin-memory/actions/workflows/ci.yml"><img src="https://github.com/zephel01/coderouter-plugin-memory/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://pypi.org/project/coderouter-plugin-memory/"><img src="https://img.shields.io/pypi/v/coderouter-plugin-memory?include_prereleases&color=blue&label=pypi" alt="pypi"></a>
  <a href=""><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="python"></a>
  <a href=""><img src="https://img.shields.io/badge/runtime%20deps-0-brightgreen" alt="deps"></a>
  <a href=""><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license"></a>
</p>

<p align="center">
  <a href="./README.md">日本語</a> · <strong>English</strong> · <a href="https://github.com/zephel01/CodeRouter">CodeRouter</a>
</p>

> **v0.4.0**: Completely redesigned the builtin backend around JSONL + Ollama (qwen3:1.7b). Zero runtime dependencies (stdlib only). The `coderouter-memory` CLI gives instant visibility into what the plugin is doing. Pairs with CodeRouter `v2.3.0a1+`.

---

## In 30 seconds

```
Your agent (Claude Code / Cursor / your-own-agent)
                │   ← memory-unaware, no SDK changes
                ▼
        ┌─ CodeRouter ────────────────────┐
        │  ① pre-request hook              │ ─→ inject facts.jsonl into system prompt
        │  ② routing + L1-L6 guards        │
        │  ③ post-response hook            │ ─→ append response to buffer.jsonl
        └──────────────────────────────────┘
                │
                ▼
            Local LLM (Ollama / LM Studio / ...)

  [After your work session — one command]
  coderouter-memory consolidate
  → Ollama (qwen3:1.7b) reads the buffer, extracts key facts → facts.jsonl
  → Auto-injected from the next session onward
```

**Three phases:**

| Phase | When | What happens |
|---|---|---|
| **capture** | Every response (automatic) | Appends response text to `buffer.jsonl` |
| **consolidate** | After work session (manual or cron) | Ollama distills buffer → key facts → `facts.jsonl` |
| **inject** | Before each request next session (automatic) | Prepends `facts.jsonl` to the system prompt |

---

## Usage

### 1. Install

```bash
# CodeRouter (v2.3.0+ required)
uv tool install coderouter-cli

# This plugin
pip install coderouter-plugin-memory

# Consolidation model — one-time pull
ollama pull qwen3:1.7b
```

### 2. Add to `providers.yaml`

```yaml
plugins:
  enabled:
    - memory
  config:
    memory:
      # project is optional — auto-detected from cwd hash
      project: myapp
      consolidate_model: qwen3:1.7b        # swap for any lightweight model
      inject_token_budget: 2000            # token cap on system prompt injection
      min_buffer_entries: 3               # minimum entries before consolidate runs
```

### 3. Start CodeRouter

```bash
coderouter serve --port 8088
# Startup log: [memory] plugin-loaded
```

### 4. Consolidate after your session

```bash
coderouter-memory consolidate
# 5 buffer entries → Ollama extracts facts → written to facts.jsonl
# Auto-injected from the next session onward
```

---

## CLI reference

```bash
# Check current state (first thing to run when debugging)
coderouter-memory status

# List stored facts
coderouter-memory list

# Pin a fact manually (like a project-level CLAUDE.md)
coderouter-memory add "Using FastAPI with async handlers"

# Preview what consolidate would extract, without writing anything
coderouter-memory consolidate --dry-run

# Run consolidation
coderouter-memory consolidate

# Inspect raw buffer entries (debug)
coderouter-memory buffer

# Drop the buffer
coderouter-memory clear
```

Example `status` output:

```
project   : proj-fd5766aa25d0          ← auto-detected from cwd
state_dir : ~/.coderouter/memory/proj-fd5766aa25d0
buffer    : 7 entries
facts     : 12 entries
manual    : 2 lines
model     : qwen3:1.7b
ollama    : http://localhost:11434

💡 buffer has 7 entries. Ready to consolidate:
   coderouter-memory consolidate
```

---

## Storage layout

Everything is plain text — open and edit in any editor.

```
~/.coderouter/memory/{project}/
    buffer.jsonl   — raw captured responses (one JSON object per line)
    facts.jsonl    — consolidated key facts (one fact per line)
    manual.md      — hand-written notes (injected alongside facts)
```

---

## Why "wire-layer"?

Most agent-memory tools ([agentmemory](https://github.com/rohitg00/agentmemory), [mem0](https://github.com/mem0ai/mem0)) live **agent-side**: the agent calls `memory_save` / `memory_recall` via MCP tools or an SDK. That works fine for Claude Code or Cursor — but it leaves out anyone writing a **custom agent** or using a non-MCP client.

This plugin lives in the **wire layer** instead (between agent and LLM backend):

| Aspect | Agent-side memory tools | This plugin (wire-layer) |
|---|---|---|
| Agent-side code | MCP client / SDK calls required | **None** — just route through CodeRouter |
| Supported agents | MCP-capable agents only | **Any agent that speaks the Anthropic API** |
| Extra processes | Memory server must be running | **Ollama only** (you already have it) |
| Runtime deps | httpx etc. | **stdlib only** |

---

## Walkthrough — your-own-agent gets memory for free

### 30-line agent

```python
import sys, os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ.get("CODEROUTER_BASE_URL", "http://localhost:8088/v1"),
    api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
)

resp = client.chat.completions.create(
    model="qwen3:14b",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": " ".join(sys.argv[1:])},
    ],
)
print(resp.choices[0].message.content)
```

**What's NOT in the script**: `memory_save` / `memory_recall` / MCP client / sqlite / vector store — the wire layer handles all of it.

### Two sessions

```bash
# Session 1
python agent.py "Remember: the project accent color is indigo."
# → "Got it, the accent color is indigo."

# After your session
coderouter-memory consolidate
# → Ollama extracts: "project accent color is indigo"

# Session 2 (next day)
python agent.py "What's the project accent color?"
# → "Indigo."   ← facts.jsonl was injected into the system prompt automatically
```

---

## Roadmap

| Version | What | Status |
|---|---|---|
| v0.1–0.3 | Plugin SDK integration / multi-backend (sqlite3 · agentmemory · null) / circuit breaker | ✅ shipped |
| **v0.4.0** | **Redesigned builtin: JSONL + Ollama consolidation / CLI / stdlib-only** | ✅ **current** |
| v0.5 (planned) | agentmemory backend as an optional extra (demand-driven) | ⏳ |

---

## Related projects

| Project | Role | Relationship |
|---|---|---|
| [CodeRouter](https://github.com/zephel01/CodeRouter) | The wire-layer router itself | **Required** — hosts the Plugin SDK |
| [Ollama](https://github.com/ollama/ollama) | Local LLM runtime | Used in the consolidate phase |
| [agentmemory](https://github.com/rohitg00/agentmemory) | Agent memory MCP server | Future optional backend candidate |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Self-improving agent framework | Higher layer, complementary |

---

## License

MIT — see [LICENSE](./LICENSE).
