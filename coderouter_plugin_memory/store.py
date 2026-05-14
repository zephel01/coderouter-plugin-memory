"""JSONL-based fact storage — no sqlite3, no external deps.

ファイル構成::

    ~/.coderouter/memory/{project}/
        buffer.jsonl   — capture された生応答 (1行=1エントリ)
        facts.jsonl    — consolidate 済みの key facts (1行=1 fact)
        manual.md      — ユーザーが手動で書く固定メモ (CLAUDE.md 感覚)

どちらも通常のテキストエディタで開いて確認・編集できる。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # 壊れた行はスキップ
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
    """facts.jsonl を最新 max_facts 件読む (古い順)。"""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
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

    token_budget を chars/4 heuristic で換算し、超えた場合は facts を末尾から切り捨てる。
    注入するものが何もない場合は None を返す。
    """
    lines: list[str] = []

    if manual:
        lines.append("[Memory — manual notes]")
        lines.extend(f"  {l}" for l in manual.splitlines() if l.strip())

    if facts:
        if lines:
            lines.append("")
        lines.append("[Memory — past context]")
        for entry in facts[-max_facts:]:
            lines.append(f"  - {entry['fact']}")

    if not lines:
        return None

    text = "\n".join(lines)

    # token budget (chars/4 heuristic)
    char_limit = token_budget * 4
    if len(text) > char_limit:
        # 予算オーバーなら fact を前から削っていく
        while len(text) > char_limit and facts:
            facts = facts[1:]
            lines_trimmed: list[str] = []
            if manual:
                lines_trimmed.append("[Memory — manual notes]")
                lines_trimmed.extend(f"  {l}" for l in manual.splitlines() if l.strip())
            if facts:
                if lines_trimmed:
                    lines_trimmed.append("")
                lines_trimmed.append("[Memory — past context]")
                for entry in facts[-max_facts:]:
                    lines_trimmed.append(f"  - {entry['fact']}")
            text = "\n".join(lines_trimmed) if lines_trimmed else ""
        if not text:
            return None

    return text


# ──────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
