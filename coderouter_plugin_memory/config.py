"""Memory plugin configuration (passed from providers.yaml plugins.config.memory)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MemoryConfig:
    """All settings for the builtin memory backend.

    providers.yaml example::

        plugins:
          enabled: [memory]
          config:
            memory:
              project: myapp          # 省略時は cwd / env から自動検出
              consolidate_model: qwen3:1.7b
              ollama_base_url: http://localhost:11434
              inject_token_budget: 2000
              min_buffer_entries: 3
    """

    # プロジェクト識別名。省略時は project_id.py の resolve_project_id() で自動検出。
    # providers.yaml で明示指定した場合はそちらを優先。
    project: str = field(default_factory=lambda: _resolve_project())

    # consolidate 時に使う Ollama モデル (軽量なもの推奨)
    consolidate_model: str = "qwen3:1.7b"

    # Ollama エンドポイント
    ollama_base_url: str = "http://localhost:11434"

    # system prompt に inject するトークン上限 (chars/4 heuristic)
    inject_token_budget: int = 2000

    # consolidate のトリガーとなる最小 buffer エントリ数
    min_buffer_entries: int = 3

    # inject する最大 fact 数
    max_inject_facts: int = 10

    # memory ストレージのルートディレクトリ
    state_dir: str = field(
        default_factory=lambda: str(
            Path(os.environ.get("CODEROUTER_STATE_DIR", Path.home() / ".coderouter")) / "memory"
        )
    )

    # capture を有効にするか (Observer フェーズ)
    capture_enabled: bool = True

    # inject を有効にするか (InputFilter フェーズ)
    inject_enabled: bool = True

    # capture 時に無視する最小文字数 (短すぎる応答はスキップ)
    min_capture_chars: int = 80

    # consolidate 時の Ollama 呼び出しタイムアウト秒数。Ollama 未起動の環境で
    # cron 実行する場合、これが長いと 1 回あたり最大この秒数ブロックする。
    consolidate_timeout_s: int = 60

    def __post_init__(self) -> None:
        # 空文字 / 空白のみの project 名は project_dir() を state_dir 直下に
        # 潰してしまい、複数プロジェクトの記憶が混ざる。安全側で "default" に
        # フォールバックする。
        if not (isinstance(self.project, str) and self.project.strip()):
            self.project = "default"

    def project_dir(self) -> Path:
        """このプロジェクトの memory ディレクトリを返す。"""
        return Path(self.state_dir) / _safe_name(self.project)

    def buffer_path(self) -> Path:
        return self.project_dir() / "buffer.jsonl"

    def facts_path(self) -> Path:
        return self.project_dir() / "facts.jsonl"

    def manual_path(self) -> Path:
        return self.project_dir() / "manual.md"


def _resolve_project() -> str:
    """project_id.py の resolve_project_id() を呼ぶ。import 失敗時は 'default'。"""
    try:
        from coderouter_plugin_memory.project_id import resolve_project_id
        return resolve_project_id()
    except Exception:
        return "default"


def _safe_name(name: str) -> str:
    """ディレクトリ名として安全な文字列に変換。

    正規化後に空文字となる入力 (例: "", "  ", "../..") でも、project_dir() が
    state_dir 直下に潰れないよう "default" を返す。
    """
    import re
    cleaned = re.sub(r"[^\w\-]", "_", name)[:64]
    # 全て "_" に潰れた場合 (例: "../..") はそのまま安全だが、空文字だけは
    # ディレクトリ区切りとして無害化する必要がある。
    return cleaned or "default"
