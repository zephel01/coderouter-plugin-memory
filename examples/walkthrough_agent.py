"""Walkthrough: a self-built agent gets memory for free.

This is the concrete version of the README's "Build less in your
agent, get more from the wire" claim. The agent below is ~30 lines.
It uses the OpenAI Python SDK (the most common toolkit for hand-
rolled agents), does no memory bookkeeping of its own, and yet:

    Run 1:  ask "remember the project's color is indigo"
    Run 2:  ask "what's the project's color?" — gets "indigo"

The trick is the ``base_url`` argument: by pointing OpenAI's SDK at
a CodeRouter instance with the ``memory`` plugin enabled, every
request is intercepted at the wire layer:

    1. Pre-request: the plugin searches its memory backend for context
       relevant to the latest user message and prepends it to the
       request's ``system`` prompt.
    2. Post-response: the plugin records the request/response pair
       so the next session can find it.

Setup
=====

    # 1. Install CodeRouter (host of the Plugin SDK)
    uv tool install coderouter-cli

    # 2. Install this plugin
    pip install coderouter-plugin-memory

    # 3. Pick a config — agentmemory recommended for quality
    #    (or copy examples/providers.builtin.yaml for zero-extra-process)
    cp examples/providers.agentmemory.yaml ~/.coderouter/providers.yaml

    # 4. (agentmemory only) start the memory backend in another terminal
    npx -y @agentmemory/agentmemory

    # 5. Start CodeRouter
    coderouter serve --port 8088

    # 6. (this terminal) install the OpenAI SDK and run the script
    pip install openai
    python examples/walkthrough_agent.py "remember the project's color is indigo"

Then run it again with a different question:

    python examples/walkthrough_agent.py "what's the project's color?"

You should see the second answer reference indigo even though the
script itself never stored, retrieved, or knew about any memory.

Note
====

The OpenAI client is used here because most hand-rolled agents start
from there. If you're already using the Anthropic Python SDK, the
same trick works — point ``ANTHROPIC_BASE_URL`` at CodeRouter.
"""
from __future__ import annotations

import os
import sys


def main() -> None:
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit(
            "openai SDK not installed. Run: pip install openai\n"
            "(Used here just to make the walkthrough realistic — the\n"
            "plugin works with any client that speaks the Anthropic\n"
            "or OpenAI-compatible API.)"
        )

    if len(sys.argv) < 2:
        sys.exit(
            "usage: python walkthrough_agent.py 'your question or instruction'\n"
            "Example:\n"
            "  python walkthrough_agent.py 'remember the project color is indigo'\n"
            "  python walkthrough_agent.py 'what's the project color?'"
        )

    user_message = " ".join(sys.argv[1:])

    # The whole trick. base_url points at CodeRouter; api_key is a
    # placeholder because the local backend doesn't actually need
    # one. CodeRouter handles auth to whichever upstream provider
    # the active profile selects.
    client = OpenAI(
        base_url=os.environ.get(
            "CODEROUTER_BASE_URL", "http://localhost:8088/v1"
        ),
        api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
    )

    response = client.chat.completions.create(
        # ``model`` is mostly ignored by CodeRouter routing — the
        # active profile in providers.yaml picks the actual provider.
        model="qwen3.6:35b-a3b",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant. If you receive context "
                    "from previous sessions, use it; otherwise just answer "
                    "the question directly."
                ),
            },
            {"role": "user", "content": user_message},
        ],
    )

    text = response.choices[0].message.content
    print(text)

    # Notice what's missing from this script:
    #
    # - No call to memory_save / memory_recall / memory_smart_search.
    # - No MCP client setup.
    # - No sqlite / vector store / Redis import.
    # - No rate-limit handling, no fallback chain, no drift detection.
    #
    # CodeRouter's wire layer handles all of that. The agent code
    # above is the entire "agent" — 30-ish lines, mostly imports
    # and CLI plumbing.


if __name__ == "__main__":
    main()
