"""Unit tests for ``coderouter_plugin_memory.project_id``."""
from __future__ import annotations

from pathlib import Path

import pytest

from coderouter_plugin_memory.project_id import resolve_project_id


def test_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEROUTER_PROJECT_ID", "my-monorepo")
    monkeypatch.setenv("CODEROUTER_CONFIG", "/tmp/some.yaml")
    assert resolve_project_id() == "my-monorepo"


def test_blank_explicit_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only override is treated as unset."""
    monkeypatch.setenv("CODEROUTER_PROJECT_ID", "   ")
    monkeypatch.setenv("CODEROUTER_CONFIG", "/tmp/sentinel.yaml")
    out = resolve_project_id()
    assert out.startswith("proj-")
    assert len(out) == len("proj-") + 12


def test_config_path_used_when_no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEROUTER_PROJECT_ID", raising=False)
    monkeypatch.setenv("CODEROUTER_CONFIG", "/path/A/providers.yaml")
    a = resolve_project_id()
    monkeypatch.setenv("CODEROUTER_CONFIG", "/path/B/providers.yaml")
    b = resolve_project_id()
    # Different paths → different ids.
    assert a != b
    # Both stable across runs.
    monkeypatch.setenv("CODEROUTER_CONFIG", "/path/A/providers.yaml")
    a_again = resolve_project_id()
    assert a == a_again


def test_falls_back_to_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CODEROUTER_PROJECT_ID", raising=False)
    monkeypatch.delenv("CODEROUTER_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    out = resolve_project_id()
    assert out.startswith("proj-")
    assert len(out) == len("proj-") + 12


def test_slug_format() -> None:
    """Slug is exactly 'proj-' + 12 lowercase hex chars."""
    import re

    out = resolve_project_id()
    if not out.startswith("proj-"):
        # Override was set in this env — skip; the format spec only
        # applies to the auto-derived path.
        return
    pattern = re.compile(r"^proj-[0-9a-f]{12}$")
    assert pattern.match(out), f"unexpected slug shape: {out!r}"
