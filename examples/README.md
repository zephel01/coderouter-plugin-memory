# Examples

Short, self-contained samples for common configurations.

## providers.yaml templates

| File | When to use |
|---|---|
| [`providers.builtin.yaml`](./providers.builtin.yaml) | Zero-extra-process. sqlite3 file at `~/.coderouter/memory.sqlite3`, keyword LIKE search. Good for trying the plugin out. |
| [`providers.agentmemory.yaml`](./providers.agentmemory.yaml) | **Recommended.** Hybrid BM25 + vector + graph search via a running [agentmemory](https://github.com/rohitg00/agentmemory) server. R@5 95.2% on LongMemEval-S. |
| [`providers.null.yaml`](./providers.null.yaml) | Plugin loaded but does nothing. Useful for CI / staging where you want the same yaml across environments but no memory persistence. |

Drop one of these into `~/.coderouter/providers.yaml` (or pass `--config <path>` to `coderouter serve`).

## Walkthrough

[`walkthrough_agent.py`](./walkthrough_agent.py) — a 30-line OpenAI-SDK agent that demonstrates "Build less in your agent, get more from the wire". Run it once with a memorable instruction, run it again with a question that requires that memory, and watch the second response reference the first session.

```bash
# Terminal 1: CodeRouter + (optional) agentmemory
npx -y @agentmemory/agentmemory   # or skip for builtin backend
coderouter serve --port 8088

# Terminal 2: the agent
python examples/walkthrough_agent.py "remember the project color is indigo"
python examples/walkthrough_agent.py "what's the project color?"
```

The agent code never imports a memory library. The plugin handles it all at the wire layer.
