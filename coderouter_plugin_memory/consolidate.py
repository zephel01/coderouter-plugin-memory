"""Consolidate フェーズ: Ollama の小型モデルで buffer → key facts を抽出する。

外部依存ゼロ (urllib.request のみ使用)。
Ollama が起動していない場合は ConsolidateError を raise する。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import urllib.error
import urllib.request

from coderouter_plugin_memory._circuit import CircuitBreaker
from coderouter_plugin_memory.config import MemoryConfig
from coderouter_plugin_memory.store import (
    BufferEntry,
    append_facts,
    clear_buffer,
    existing_fact_texts,
    read_buffer,
)

# Per-process breaker for the Ollama backend. When consolidate() is invoked
# repeatedly inside one long-lived process (e.g. an in-process scheduler)
# against a downed Ollama, this short-circuits after a few failures instead of
# paying the full timeout every time. A one-shot CLI run starts CLOSED, so the
# first attempt always goes through.
_OLLAMA_BREAKER = CircuitBreaker(threshold=3, cooldown_s=30.0)

EXTRACT_PROMPT = """\
以下は AI アシスタントの応答ログです。
将来のセッションで役立つ重要な facts (事実・決定・制約・好み) を抽出してください。

抽出ルール:
- 各 fact は 30 語以内の短文にする
- プロジェクト固有の技術的決定や制約を優先する
- 汎用的すぎる内容 ("Python を使う" 等) は除外する
- 重複や類似 fact は統合する
- 最大 8 件まで

必ず以下の JSON 配列のみを返してください (説明文不要):
["fact1", "fact2", ...]

応答ログ:
{text}
"""


class ConsolidateError(Exception):
    """consolidate が失敗した場合に raise する。"""


def consolidate(cfg: MemoryConfig, *, dry_run: bool = False) -> ConsolidateResult:
    """buffer.jsonl を読み、Ollama で fact 抽出して facts.jsonl に書く。

    dry_run=True のとき、抽出結果を表示するだけでファイルを変更しない。

    同一プロジェクトに対する consolidate は、buffer のクリアと facts 追記が
    競合しないよう、ロックファイルで排他する (二重起動は skip)。
    """
    project_dir = cfg.project_dir()
    project_dir.mkdir(parents=True, exist_ok=True)
    lock_path = project_dir / ".consolidate.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return ConsolidateResult(
            skipped=True,
            reason="another consolidate run is in progress (lock held)",
        )

    try:
        return _consolidate_locked(cfg, dry_run=dry_run)
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def _consolidate_locked(cfg: MemoryConfig, *, dry_run: bool) -> ConsolidateResult:
    buffer_path = cfg.buffer_path()
    facts_path = cfg.facts_path()

    entries = read_buffer(buffer_path)
    if not entries:
        return ConsolidateResult(skipped=True, reason="buffer is empty")

    if len(entries) < cfg.min_buffer_entries and not dry_run:
        return ConsolidateResult(
            skipped=True,
            reason=f"buffer has {len(entries)} entries (min: {cfg.min_buffer_entries})",
        )

    # 既存 facts で重複チェック用セット
    existing = existing_fact_texts(facts_path)

    # buffer のテキストを結合 (長すぎる場合は最新のものを優先)
    combined = _combine_buffer(entries, max_chars=6000)

    # Ollama で fact 抽出
    raw_facts = _call_ollama(cfg, combined)

    # 重複除去
    new_facts = [
        f for f in raw_facts
        if f.strip() and f.strip().lower() not in existing
    ]

    if dry_run:
        return ConsolidateResult(
            skipped=False,
            buffer_count=len(entries),
            extracted=raw_facts,
            new_facts=new_facts,
            dry_run=True,
        )

    # facts.jsonl に追記
    written = append_facts(facts_path, new_facts, project=cfg.project)

    # buffer をクリア
    cleared = clear_buffer(buffer_path)

    return ConsolidateResult(
        skipped=False,
        buffer_count=cleared,
        extracted=raw_facts,
        new_facts=new_facts,
        written=written,
    )


def _combine_buffer(entries: list[BufferEntry], max_chars: int) -> str:
    """buffer エントリのテキストを結合する (最新優先で max_chars に収める)。"""
    texts = [e.get("text", "") for e in reversed(entries) if e.get("text")]
    combined_parts: list[str] = []
    total = 0
    for t in texts:
        if total + len(t) > max_chars:
            break
        combined_parts.append(t)
        total += len(t)
    # 時系列順 (古い→新しい) に戻す
    return "\n\n---\n\n".join(reversed(combined_parts))


def _call_ollama(
    cfg: MemoryConfig, text: str, *, breaker: CircuitBreaker | None = None
) -> list[str]:
    """Ollama /api/generate を呼び出して facts リストを返す。

    連続失敗時はサーキットブレーカーが OPEN になり、cooldown 中は接続を試みず
    即座に ConsolidateError を返す (down した Ollama への無駄な待ち時間を回避)。
    """
    breaker = breaker or _OLLAMA_BREAKER
    if breaker.should_skip():
        raise ConsolidateError(
            "Ollama サーキットブレーカーが OPEN です (直近の連続失敗により "
            f"約 {breaker.open_seconds_remaining:.0f}s スキップ中)。"
            " Ollama が起動しているか確認してください: ollama serve"
        )

    prompt = EXTRACT_PROMPT.format(text=text)
    payload = json.dumps({
        "model": cfg.consolidate_model,
        "prompt": prompt,
        "stream": False,
    }).encode()

    url = cfg.ollama_base_url.rstrip("/") + "/api/generate"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=cfg.consolidate_timeout_s) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        breaker.record_failure()
        raise ConsolidateError(
            f"Ollama に接続できませんでした ({url}): {exc}\n"
            f"Ollama が起動しているか確認してください: ollama serve"
        ) from exc
    except Exception as exc:
        breaker.record_failure()
        raise ConsolidateError(f"Ollama 呼び出しエラー: {exc}") from exc

    breaker.record_success()
    response_text = body.get("response", "")
    return _parse_facts(response_text)


def _parse_facts(text: str) -> list[str]:
    """LLM の応答テキストから JSON 配列を抽出する。

    モデルが余分な説明文を付けることがあるので、最初の [...] を探す。
    """
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if match:
        try:
            facts = json.loads(match.group())
            if isinstance(facts, list):
                return [str(f).strip() for f in facts if str(f).strip()]
        except json.JSONDecodeError:
            pass

    # フォールバック: "- xxx" 形式の箇条書きを解析
    facts = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ", "• ")):
            fact = line[2:].strip()
            if fact:
                facts.append(fact)
    return facts


class ConsolidateResult:
    def __init__(
        self,
        skipped: bool = False,
        reason: str = "",
        buffer_count: int = 0,
        extracted: list[str] | None = None,
        new_facts: list[str] | None = None,
        written: int = 0,
        dry_run: bool = False,
    ) -> None:
        self.skipped = skipped
        self.reason = reason
        self.buffer_count = buffer_count
        self.extracted = extracted or []
        self.new_facts = new_facts or []
        self.written = written
        self.dry_run = dry_run

    def summary(self) -> str:
        if self.skipped:
            return f"skipped: {self.reason}"
        if self.dry_run:
            return (
                f"[dry-run] buffer: {self.buffer_count} entries, "
                f"extracted: {len(self.extracted)}, new: {len(self.new_facts)}"
            )
        return (
            f"buffer cleared ({self.buffer_count} entries), "
            f"facts written: {self.written}"
        )
