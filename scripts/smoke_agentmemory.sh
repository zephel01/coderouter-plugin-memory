#!/usr/bin/env bash
# DEPRECATED (v0.4.0): the agentmemory backend was removed.
#
# This smoke test exercised the pre-0.4 agentmemory REST backend
# (/agentmemory/remember, /agentmemory/smart-search). That backend no longer
# exists — v0.4.x memory is a local JSONL store with no network calls, so there
# is nothing here to smoke-test. The script is kept as a stub (not deleted) so
# it can be restored when a networked backend returns in v0.5.
#
# To sanity-check v0.4.x memory instead, use the CLI:
#   coderouter-memory add "some durable fact"
#   coderouter-memory consolidate     # requires a local Ollama
#   coderouter-memory show
set -euo pipefail
echo "smoke_agentmemory.sh is deprecated in v0.4.0 (agentmemory backend removed)." >&2
echo "See the header comment for the v0.4.x CLI-based check." >&2
exit 0
