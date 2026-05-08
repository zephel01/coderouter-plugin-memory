#!/usr/bin/env bash
# Smoke test against a real agentmemory server.
#
# What this validates
# ===================
# 1. agentmemory's REST surface answers on the documented endpoints.
# 2. The exact JSON shapes match what AgentMemoryBackend assumes
#    (request body field names, response payload structure).
# 3. Round-trip: observe a request, then smart_search the same
#    project — the search returns the observed text.
#
# Why a script, not a Python integration test
# ===========================================
# agentmemory ships as a Node.js server with a native ``iii-engine``
# binary; it doesn't fit cleanly into a CI environment that just
# wants ``pip install``. So this script assumes you've already
# started the server in a separate terminal and just exercises the
# wire from the outside.
#
# Usage
# =====
#   # Terminal 1: start agentmemory
#   npx -y @agentmemory/agentmemory
#
#   # Terminal 2: run this smoke
#   ./scripts/smoke_agentmemory.sh
#
#   # Optional env:
#   AGENTMEMORY_ENDPOINT=http://localhost:3111   # default
#   AGENTMEMORY_SECRET=...                       # if the server was started with auth
#
# Output is human-readable; exit code is 0 on full pass, non-zero on
# any failure. Field names / response shapes get printed so you can
# diff them against ``backends/agentmemory.py`` if agentmemory has
# released a new version that changed the contract.

set -euo pipefail

ENDPOINT="${AGENTMEMORY_ENDPOINT:-http://localhost:3111}"
PROJECT_ID="coderouter-plugin-memory-smoke-$(date +%s)"

# ----- colors (only when stdout is a TTY) -----
if [[ -t 1 ]]; then
    GREEN=$(printf '\033[0;32m')
    RED=$(printf '\033[0;31m')
    YELLOW=$(printf '\033[0;33m')
    BLUE=$(printf '\033[0;34m')
    BOLD=$(printf '\033[1m')
    RESET=$(printf '\033[0m')
else
    GREEN= RED= YELLOW= BLUE= BOLD= RESET=
fi

ok()    { printf '%s[PASS]%s %s\n' "${GREEN}" "${RESET}" "$*"; }
fail()  { printf '%s[FAIL]%s %s\n' "${RED}"   "${RESET}" "$*"; exit 1; }
info()  { printf '%s[....]%s %s\n' "${BLUE}"  "${RESET}" "$*"; }
note()  { printf '%s[note]%s %s\n' "${YELLOW}" "${RESET}" "$*"; }

curl_args=(--silent --show-error --max-time 5)
if [[ -n "${AGENTMEMORY_SECRET:-}" ]]; then
    curl_args+=(-H "Authorization: Bearer ${AGENTMEMORY_SECRET}")
fi

# ----- 0. preflight -----
command -v curl >/dev/null 2>&1 || fail "curl not found in PATH"
command -v jq   >/dev/null 2>&1 || note "jq not found — output will be raw JSON"

printf '%s%s coderouter-plugin-memory smoke (agentmemory)%s\n' \
    "${BOLD}" "===" "${RESET}"
printf '       endpoint:   %s\n' "${ENDPOINT}"
printf '       project_id: %s\n' "${PROJECT_ID}"
printf '       auth:       %s\n' \
    "$( [[ -n "${AGENTMEMORY_SECRET:-}" ]] && echo "bearer (length ${#AGENTMEMORY_SECRET})" || echo "none" )"
echo ""

# ----- 1. health -----
info "GET ${ENDPOINT}/agentmemory/health"
status=$(curl "${curl_args[@]}" -o /tmp/cmem-health.txt -w '%{http_code}' \
    "${ENDPOINT}/agentmemory/health" || echo "000")
if [[ "${status}" == "000" ]]; then
    fail "health: server unreachable (is agentmemory running on ${ENDPOINT}?)"
elif [[ "${status:0:1}" != "2" ]]; then
    fail "health: HTTP ${status}: $(head -c 200 /tmp/cmem-health.txt)"
fi
ok "health: HTTP ${status}"

# ----- 2. observe -----
observe_body=$(cat <<EOF
{
  "project_id": "${PROJECT_ID}",
  "tool_name": "coderouter_request",
  "input": {
    "last_user_message": "set up JWT authentication in src/middleware/auth.ts",
    "had_tools": false,
    "stream": false
  },
  "output": {
    "text": "Done. Added jose middleware that validates HS256 tokens against AUTH_SECRET.",
    "model": "claude-3-5-sonnet",
    "stop_reason": "end_turn",
    "input_tokens": 23,
    "output_tokens": 17
  }
}
EOF
)

info "POST ${ENDPOINT}/agentmemory/observe"
status=$(curl "${curl_args[@]}" -o /tmp/cmem-observe.txt -w '%{http_code}' \
    -H 'content-type: application/json' \
    -X POST --data-raw "${observe_body}" \
    "${ENDPOINT}/agentmemory/observe" || echo "000")
if [[ "${status:0:1}" != "2" ]]; then
    note "observe response body:"
    head -c 1000 /tmp/cmem-observe.txt; echo ""
    fail "observe: HTTP ${status}"
fi
ok "observe: HTTP ${status}"
note "observe body shape:"
if command -v jq >/dev/null 2>&1; then
    jq . /tmp/cmem-observe.txt || cat /tmp/cmem-observe.txt
else
    head -c 500 /tmp/cmem-observe.txt; echo ""
fi

# Give agentmemory a beat to index the observation. Most backends
# index synchronously; some run a short async pipeline. 1s is
# generous for a single-row case but won't make a CI run feel slow.
sleep 1

# ----- 3. smart-search (same project) -----
search_body=$(cat <<EOF
{
  "query": "how does our authentication flow work?",
  "project_id": "${PROJECT_ID}",
  "limit": 5
}
EOF
)

info "POST ${ENDPOINT}/agentmemory/smart-search"
status=$(curl "${curl_args[@]}" -o /tmp/cmem-search.txt -w '%{http_code}' \
    -H 'content-type: application/json' \
    -X POST --data-raw "${search_body}" \
    "${ENDPOINT}/agentmemory/smart-search" || echo "000")
if [[ "${status:0:1}" != "2" ]]; then
    note "smart-search response body:"
    head -c 1000 /tmp/cmem-search.txt; echo ""
    fail "smart-search: HTTP ${status}"
fi
ok "smart-search: HTTP ${status}"
note "smart-search body shape (the field names below are what the plugin parses):"
if command -v jq >/dev/null 2>&1; then
    jq . /tmp/cmem-search.txt || cat /tmp/cmem-search.txt
else
    head -c 1500 /tmp/cmem-search.txt; echo ""
fi
echo ""

# ----- 4. round-trip assertion -----
# We expect the observed text ("jose middleware") to appear in the
# search response somehow. We don't pin the exact JSON path because
# that's the point of the smoke — print what we get and grep for
# the keyword.
if grep -q -i "jose" /tmp/cmem-search.txt; then
    ok "round-trip: observed text (\"jose\") found in search response"
else
    fail "round-trip: observed text NOT in search response — agentmemory may have changed shape, or the LIKE/embedding didn't match"
fi

# ----- 5. (optional) cleanup hint -----
echo ""
note "leaving observed memory in place under project_id=${PROJECT_ID}"
note "to clean up: curl -X POST ${ENDPOINT}/agentmemory/forget -d '{\"project_id\":\"${PROJECT_ID}\"}' -H 'content-type: application/json'"

printf '\n%s%s smoke complete — all checks passed%s\n' "${BOLD}${GREEN}" "===" "${RESET}"
