"""MemoryPlugin — InputFilter (inject) + Observer (capture) の両方を実装する。

1 クラスで 2 つの hook を実装しているのは意図的:
- providers.yaml で `enabled: [memory]` の 1 行で capture + inject 両方が有効になる
- 設定オブジェクト (MemoryConfig) を共有するため

Plugin SDK の契約:
- InputFilter.transform: リクエスト前に system prompt に facts を注入する
- Observer.on_event: レスポンス後に buffer.jsonl に応答テキストを append する
"""
from __future__ import annotations

import logging
from typing import Any

from coderouter_plugin_memory.config import MemoryConfig
from coderouter_plugin_memory.store import (
    append_buffer,
    build_inject_text,
    read_facts,
    read_manual,
)

logger = logging.getLogger(__name__)


class MemoryPlugin:
    """InputFilter + Observer を兼ねるメモリプラグイン。

    providers.yaml での設定例::

        plugins:
          enabled: [memory]
          config:
            memory:
              project: myapp          # 省略時は cwd から自動検出
              consolidate_model: qwen3:1.7b
              inject_token_budget: 2000
    """

    name = "memory"

    def __init__(self, **kwargs: Any) -> None:
        self._cfg = MemoryConfig(**{k: v for k, v in kwargs.items() if v is not None})
        self._cfg.project_dir().mkdir(parents=True, exist_ok=True)
        logger.info(
            "memory-plugin-loaded",
            extra={
                "project": self._cfg.project,
                "state_dir": str(self._cfg.project_dir()),
                "inject": self._cfg.inject_enabled,
                "capture": self._cfg.capture_enabled,
                "consolidate_model": self._cfg.consolidate_model,
            },
        )

    # ──────────────────────────────────────────────
    # InputFilter protocol
    # ──────────────────────────────────────────────

    async def transform(self, request: Any) -> Any:
        """system prompt に facts と manual notes を注入する。

        inject_enabled=False or 注入するものがない場合はリクエストをそのまま返す。
        """
        if not self._cfg.inject_enabled:
            return request

        try:
            inject_text = self._build_inject_text()
        except Exception as exc:
            logger.warning("memory-inject-failed", extra={"error": str(exc)[:200]})
            return request

        if not inject_text:
            return request

        updated_system = _prepend_memory(request.system, inject_text)
        result = request.model_copy(update={"system": updated_system})

        injected_tokens = len(inject_text) // 4
        logger.info(
            "memory-injected",
            extra={
                "project": self._cfg.project,
                "chars": len(inject_text),
                "approx_tokens": injected_tokens,
            },
        )
        return result

    def _build_inject_text(self) -> str | None:
        facts = read_facts(self._cfg.facts_path(), max_facts=self._cfg.max_inject_facts)
        manual = read_manual(self._cfg.manual_path())
        return build_inject_text(
            facts=facts,
            manual=manual,
            token_budget=self._cfg.inject_token_budget,
            max_facts=self._cfg.max_inject_facts,
        )

    # ──────────────────────────────────────────────
    # Observer protocol
    # ──────────────────────────────────────────────

    async def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """request_completed イベントを受けて buffer.jsonl に追記する。"""
        if event_type != "request_completed":
            return
        if not self._cfg.capture_enabled:
            return

        try:
            text = _extract_response_text(payload.get("response"))
        except Exception as exc:
            logger.warning("memory-capture-extract-failed", extra={"error": str(exc)[:200]})
            return

        if not text or len(text) < self._cfg.min_capture_chars:
            return

        provider = payload.get("provider", "unknown")
        try:
            append_buffer(
                self._cfg.buffer_path(),
                text=text,
                project=self._cfg.project,
                provider=str(provider),
            )
            logger.debug(
                "memory-captured",
                extra={"project": self._cfg.project, "chars": len(text)},
            )
        except Exception as exc:
            logger.warning("memory-capture-write-failed", extra={"error": str(exc)[:200]})


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _extract_response_text(response: Any) -> str:
    """AnthropicResponse または _StreamUsageAccumulator から応答テキストを取り出す。

    content は list[dict] で、各要素は {"type": "text", "text": "..."} の形。
    """
    if response is None:
        return ""

    content = getattr(response, "content", None)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts).strip()

    return str(response)[:2000]


def _prepend_memory(
    system: str | list | None,
    inject_text: str,
) -> str | list:
    """既存の system フィールドの先頭に memory テキストを付加する。

    - system が str → 先頭に挿入 (改行区切り)
    - system が list[dict] (cache_control ブロック) → 先頭に text ブロックを挿入
    - system が None → inject_text をそのまま str として返す
    """
    if system is None:
        return inject_text

    if isinstance(system, str):
        return inject_text + "\n\n" + system

    if isinstance(system, list):
        memory_block = {"type": "text", "text": inject_text}
        return [memory_block] + list(system)

    return inject_text
