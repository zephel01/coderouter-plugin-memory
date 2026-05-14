"""CLI entry point: `coderouter-memory <subcommand>`.

サブコマンド:
    status      — buffer / facts の状況を表示
    list        — 保存済み facts を一覧表示
    add <text>  — manual.md に fact を追記
    consolidate — buffer → Ollama → facts.jsonl を手動実行
    clear       — buffer.jsonl を削除 (テスト用)
    buffer      — buffer の内容を確認 (デバッグ用)

使い方::

    coderouter-memory status
    coderouter-memory list
    coderouter-memory add "FastAPI を使用、asyncio ベース"
    coderouter-memory consolidate --dry-run
    coderouter-memory consolidate --model qwen3:1.7b
"""
from __future__ import annotations

import argparse
import sys

from coderouter_plugin_memory.config import MemoryConfig
from coderouter_plugin_memory.store import (
    add_manual_fact,
    buffer_count,
    clear_buffer,
    facts_count,
    read_buffer,
    read_facts,
    read_manual,
)


def _make_cfg(args: argparse.Namespace) -> MemoryConfig:
    """CLI args から MemoryConfig を構築する。"""
    kwargs: dict = {}
    if hasattr(args, "project") and args.project:
        kwargs["project"] = args.project
    if hasattr(args, "model") and args.model:
        kwargs["consolidate_model"] = args.model
    if hasattr(args, "ollama_url") and args.ollama_url:
        kwargs["ollama_base_url"] = args.ollama_url
    if hasattr(args, "state_dir") and args.state_dir:
        kwargs["state_dir"] = args.state_dir
    return MemoryConfig(**kwargs)


# ──────────────────────────────────────────────────────────────
# subcommands
# ──────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    cfg = _make_cfg(args)
    buf = buffer_count(cfg.buffer_path())
    fcts = facts_count(cfg.facts_path())
    manual_text = read_manual(cfg.manual_path())
    manual_lines = len([ln for ln in manual_text.splitlines() if ln.strip()]) if manual_text else 0

    print(f"project   : {cfg.project}")
    print(f"state_dir : {cfg.project_dir()}")
    print(f"buffer    : {buf} entries")
    print(f"facts     : {fcts} entries")
    print(f"manual    : {manual_lines} lines")
    print(f"model     : {cfg.consolidate_model}")
    print(f"ollama    : {cfg.ollama_base_url}")

    if buf >= cfg.min_buffer_entries:
        print(f"\n💡 buffer が {buf} 件あります。consolidate を実行できます:")
        print("   coderouter-memory consolidate")
    elif buf > 0:
        print(f"\n[i] consolidate には最低 {cfg.min_buffer_entries} 件必要 (現在 {buf} 件)")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _make_cfg(args)
    facts = read_facts(cfg.facts_path(), max_facts=cfg.max_inject_facts * 5)

    if not facts:
        print(f"facts が見つかりません (project: {cfg.project})")
        print(f"  path: {cfg.facts_path()}")
        return 0

    print(f"[{cfg.project}] facts ({len(facts)} 件)\n")
    for i, entry in enumerate(facts, 1):
        ts = entry.get("ts", "")[:16].replace("T", " ")
        fact = entry.get("fact", "")
        src = entry.get("source", "")
        print(f"  {i:3}. [{ts}] {fact}")
        if args.verbose:
            print(f"       source: {src}")

    manual = read_manual(cfg.manual_path())
    if manual:
        print("\n[Manual notes]\n")
        for line in manual.splitlines():
            if line.strip():
                print(f"  {line}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    cfg = _make_cfg(args)
    text = " ".join(args.text)
    if not text.strip():
        print("エラー: テキストを指定してください", file=sys.stderr)
        return 1
    add_manual_fact(cfg.manual_path(), text)
    print(f"✓ manual fact を追記しました: {text!r}")
    print(f"  path: {cfg.manual_path()}")
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    cfg = _make_cfg(args)

    from coderouter_plugin_memory.consolidate import ConsolidateError, consolidate

    dry_run: bool = args.dry_run
    if dry_run:
        print("[dry-run] ファイルは変更しません\n")

    try:
        result = consolidate(cfg, dry_run=dry_run)
    except ConsolidateError as exc:
        print(f"❌ consolidate 失敗:\n  {exc}", file=sys.stderr)
        return 1

    print(result.summary())

    if result.skipped:
        return 0

    if result.extracted:
        print(f"\n抽出された facts ({len(result.extracted)} 件):")
        for f in result.extracted:
            mark = "+" if f in (result.new_facts or []) else " "
            print(f"  [{mark}] {f}")
        print(f"\n  新規: {len(result.new_facts)} 件 / 重複除去: {len(result.extracted) - len(result.new_facts)} 件")

    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    cfg = _make_cfg(args)
    if not args.yes:
        ans = input(f"buffer.jsonl を削除しますか? ({cfg.buffer_path()}) [y/N] ")
        if ans.lower() not in ("y", "yes"):
            print("キャンセルしました")
            return 0
    count = clear_buffer(cfg.buffer_path())
    print(f"✓ buffer を削除しました ({count} エントリ)")
    return 0


def cmd_buffer(args: argparse.Namespace) -> int:
    cfg = _make_cfg(args)
    entries = read_buffer(cfg.buffer_path())
    if not entries:
        print(f"buffer が空です (project: {cfg.project})")
        return 0

    print(f"[{cfg.project}] buffer ({len(entries)} 件)\n")
    for i, entry in enumerate(entries, 1):
        ts = entry.get("ts", "")[:16].replace("T", " ")
        provider = entry.get("provider", "")
        text = entry.get("text", "")
        preview = text[:80].replace("\n", " ") + ("..." if len(text) > 80 else "")
        print(f"  {i:3}. [{ts}] ({provider}) {preview}")
    return 0


# ──────────────────────────────────────────────────────────────
# Parser
# ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coderouter-memory",
        description="CodeRouter memory plugin CLI",
    )
    parser.add_argument(
        "--project", "-p",
        default=None,
        metavar="NAME",
        help="プロジェクト名 (省略時は cwd から自動検出)",
    )
    parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="memory ストレージのルートディレクトリ (default: ~/.coderouter/memory)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="buffer / facts の状況を表示")

    p_list = sub.add_parser("list", help="保存済み facts を一覧表示")
    p_list.add_argument("-v", "--verbose", action="store_true", help="source も表示")

    p_add = sub.add_parser("add", help="manual fact を追記")
    p_add.add_argument("text", nargs="+", help="追記するテキスト")

    p_con = sub.add_parser("consolidate", help="buffer → Ollama → facts.jsonl")
    p_con.add_argument("--model", metavar="MODEL", help="Ollama モデル名 (default: qwen3:1.7b)")
    p_con.add_argument("--ollama-url", metavar="URL", help="Ollama エンドポイント")
    p_con.add_argument("--dry-run", action="store_true", help="ファイルを変更せず結果だけ表示")

    p_clr = sub.add_parser("clear", help="buffer.jsonl を削除")
    p_clr.add_argument("-y", "--yes", action="store_true", help="確認をスキップ")

    sub.add_parser("buffer", help="buffer の内容を確認 (デバッグ用)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "status": cmd_status,
        "list": cmd_list,
        "add": cmd_add,
        "consolidate": cmd_consolidate,
        "clear": cmd_clear,
        "buffer": cmd_buffer,
    }
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(fn(args))


if __name__ == "__main__":
    main()
