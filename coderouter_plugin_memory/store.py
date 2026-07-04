"""JSONL-based fact storage — no sqlite3, no external deps.

ファイル構成::

    ~/.coderouter/memory/{project}/
        buffer.jsonl   — capture された生応答 (1行=1エントリ)
        facts.jsonl    — consolidate 済みの key facts (1行=1 fact)
        manual.md      — ユーザーが手動で書く固定メモ (CLAUDE.md 感覚)

どちらも通常のテキストエディタで開いて確認・編集できる。
"""
from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# 型エイリアス (軽量、dataclass 不要)
# ──────────────────────────────────────────────────────────────

BufferEntry = dict  # {"ts", "text", "project", "provider"}
FactEntry = dict    # {"ts", "fact", "project", "source"}


# ──────────────────────────────────────────────────────────────
# Buffer (capture フェーズが書く)
# ──────────────────────────────────────────────────────────────

def append_buffer(path: Path, text: str, project: str, provider: str) -> None:
    """buffer.jsonl に応答テキストを 1 行追記する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry: BufferEntry = {
        "ts": _now_iso(),
        "text": text,
        "project": project,
        "provider": provider,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_buffer(path: Path) -> list[BufferEntry]:
    """buffer.jsonl を全行読み込む。ファイルなし → []。"""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                entries.append(json.loads(line))
    return entries


def clear_buffer(path: Path) -> int:
    """buffer.jsonl を削除し、削除前のエントリ数を返す。"""
    if not path.exists():
        return 0
    count = len(read_buffer(path))
    path.unlink()
    return count


def buffer_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


# ──────────────────────────────────────────────────────────────
# Facts (consolidate フェーズが書く、inject フェーズが読む)
# ──────────────────────────────────────────────────────────────

def append_facts(path: Path, facts: list[str], project: str, source: str = "consolidated") -> int:
    """facts.jsonl に fact リストを追記する。重複除去済みのものを渡すこと。
    追記した件数を返す。"""
    if not facts:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = _now_iso()
    written = 0
    with path.open("a", encoding="utf-8") as f:
        for fact in facts:
            fact = fact.strip()
            if fact:
                entry: FactEntry = {
                    "ts": ts,
                    "fact": fact,
                    "project": project,
                    "source": source,
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                written += 1
    return written


def read_facts(path: Path, max_facts: int = 50) -> list[FactEntry]:
    """facts.jsonl の末尾 (＝新しい方) から max_facts 件を、記録順 (古→新) の
    まま返す。並べ替えは行わない (新しい順にソートし直すわけではない)。"""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                entries.append(json.loads(line))
    # 最新 max_facts 件だけ返す (古い→新しい順)
    return entries[-max_facts:] if len(entries) > max_facts else entries


def existing_fact_texts(path: Path) -> set[str]:
    """重複除去用: 既存 facts の text セットを返す。"""
    return {e["fact"].lower() for e in read_facts(path, max_facts=200)}


def facts_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def add_manual_fact(manual_path: Path, text: str) -> None:
    """`manual.md` に 1 行追記する (coderouter memory add)。"""
    manual_path.parent.mkdir(parents=True, exist_ok=True)
    with manual_path.open("a", encoding="utf-8") as f:
        f.write(f"- {text.strip()}\n")


def read_manual(manual_path: Path) -> str:
    """manual.md の内容を返す。なければ空文字。"""
    if not manual_path.exists():
        return ""
    return manual_path.read_text(encoding="utf-8").strip()


# ──────────────────────────────────────────────────────────────
# Inject helpers
# ──────────────────────────────────────────────────────────────

def build_inject_text(
    facts: list[FactEntry],
    manual: str,
    token_budget: int,
    max_facts: int,
) -> str | None:
    """system prompt に注入するテキストを組み立てる。

    token_budget を chars/4 heuristic で換算し、超えた場合は古い fact から
    切り捨てて新しいものを優先的に残す。注入するものが何もない場合は None。
    """
    char_limit = token_budget * 4

    manual_lines: list[str] = []
    if manual:
        manual_lines.append("[Memory — manual notes]")
        manual_lines.extend(f"  {ln}" for ln in manual.splitlines() if ln.strip())

    display_facts = list(facts[-max_facts:]) if facts else []
    fact_lines = [f"  - {entry['fact']}" for entry in display_facts]

    def _assemble(kept_fact_lines: list[str]) -> str:
        out = list(manual_lines)
        if kept_fact_lines:
            if out:
                out.append("")
            out.append("[Memory — past context]")
            out.extend(kept_fact_lines)
        return "\n".join(out)

    full = _assemble(fact_lines)
    if not full:
        return None
    if len(full) <= char_limit:
        return full

    # Over budget. Keep the largest suffix of (newest) facts that fits, using a
    # running length so the final string is built exactly once (O(n), not the
    # previous O(n^2) rebuild-per-drop loop). `fixed` is the cost of the manual
    # block plus the past-context header, which is present iff >=1 fact is kept.
    if manual_lines:
        fixed = len("\n".join(manual_lines)) + len("\n\n[Memory — past context]")
    else:
        fixed = len("[Memory — past context]")

    kept_rev: list[str] = []
    length = fixed
    for line in reversed(fact_lines):
        add = 1 + len(line)  # a leading "\n" plus the line itself
        if length + add > char_limit:
            break
        kept_rev.append(line)
        length += add

    text = _assemble(list(reversed(kept_rev)))
    return text or None


# ──────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
