"""Project identifier resolution.

The plugin namespaces every memory by ``project_id`` so two unrelated
projects on the same machine never bleed into each other. Users
shouldn't have to set this manually for the common case — we derive a
stable identifier from the working directory or the loaded
``providers.yaml`` path, and only fall back to an explicit override
when the user wants two separate paths to share a memory namespace
(e.g. a monorepo with multiple working directories).

Resolution order (first match wins)
===================================

1. ``CODEROUTER_PROJECT_ID`` env var — explicit override, used as-is.
2. ``CODEROUTER_CONFIG`` env var — the path to providers.yaml that
   CodeRouter loaded. Hashed to a stable slug.
3. Process current working directory. Hashed to a stable slug.

Why the hash, not the raw path? A raw path leaks user-private
information into log messages and audit trails. The 12-hex SHA-256
prefix is short enough to read in a UI and uniquely identifies the
path with negligible collision risk for a single user's set of
projects (we're not building a global namespace).

The function is pure with respect to the environment at call time —
it deliberately re-reads ``os.environ`` and ``Path.cwd()`` on every
invocation so a long-running CodeRouter process that ``chdir``s or
has its env mutated picks up the change immediately. Callers that
want a stable id across a request lifecycle should snapshot the
return value once at the top of the request.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

# Length of the hex slice taken from the SHA-256 digest. 12 hex chars
# = 48 bits of entropy → ~16 million projects before a 50% collision
# probability per the birthday bound. More than enough for a single
# user's tree, and short enough to be human-readable in logs.
_SLUG_LEN = 12

# Prefix prepended to derived ids so they're easy to spot in logs as
# "automatically derived" vs the explicit override (which the user
# typed and is therefore arbitrary).
_AUTO_PREFIX = "proj-"

# Env var names. Imported from CodeRouter's existing convention so a
# user who already exports CODEROUTER_CONFIG to point at a non-default
# providers.yaml gets the right namespace for free.
_ENV_EXPLICIT = "CODEROUTER_PROJECT_ID"
_ENV_CONFIG_PATH = "CODEROUTER_CONFIG"


def resolve_project_id() -> str:
    """Return a stable, namespace-safe project identifier.

    Returns:
        Either the user-supplied ``CODEROUTER_PROJECT_ID`` (used
        verbatim, no transformation), or a string of the form
        ``proj-XXXXXXXXXXXX`` where ``X`` is the first 12 hex
        characters of the SHA-256 of the resolved path.
    """
    explicit = os.environ.get(_ENV_EXPLICIT, "").strip()
    if explicit:
        return explicit

    cfg_path = os.environ.get(_ENV_CONFIG_PATH, "").strip()
    if cfg_path:
        # Don't bother resolving symlinks here — the user's intent is
        # "the path I set in this env var". If a/b/c.yaml and the
        # symlink-resolved real path produce different namespaces,
        # that's surprising. Hashing the literal env value matches
        # the user's mental model.
        return _slug_from(cfg_path)

    return _slug_from(str(Path.cwd().resolve()))


def _slug_from(text: str) -> str:
    """Hash ``text`` and return ``proj-{12-hex}``.

    Stable across processes and Python versions: SHA-256 of the
    UTF-8 bytes, sliced to :data:`_SLUG_LEN` hex characters.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_AUTO_PREFIX}{digest[:_SLUG_LEN]}"
