<h1 align="center">coderouter-plugin-memory</h1>

<p align="center">
  <strong>毎セッション同じ説明を繰り返す問題、<br>ルーター層で透過的に直します。</strong>
</p>

<p align="center">
  <a href="https://github.com/zephel01/coderouter-plugin-memory/actions/workflows/ci.yml"><img src="https://github.com/zephel01/coderouter-plugin-memory/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://pypi.org/project/coderouter-plugin-memory/"><img src="https://img.shields.io/pypi/v/coderouter-plugin-memory?include_prereleases&color=blue&label=pypi" alt="pypi"></a>
  <a href=""><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="python"></a>
  <a href=""><img src="https://img.shields.io/badge/runtime%20deps-0-brightgreen" alt="deps"></a>
  <a href=""><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license"></a>
</p>

<p align="center">
  <strong>日本語</strong> · <a href="./README.en.md">English</a> · <a href="https://github.com/zephel01/CodeRouter">CodeRouter 本体</a>
</p>

> **v0.4.0**: builtin backend を JSONL + Ollama (qwen3:1.7b) ベースに完全再設計。外部依存ゼロ (stdlib only)、`coderouter-memory` CLI で動作確認が即できる。CodeRouter `v2.3.0a1+` の Plugin SDK と組み合わせて動作。

---

## 30 秒で

```
あなたのエージェント (Claude Code / Cursor / 自作 agent)
                │  ← memory のことを知らなくていい
                ▼
        ┌─ CodeRouter ──────────────────┐
        │  ① pre-request hook            │ ─→ facts.jsonl を system prompt に注入
        │  ② 通常の routing + L1-L6 ガード  │
        │  ③ post-response hook          │ ─→ buffer.jsonl に応答を追記
        └───────────────────────────────┘
                │
                ▼
            Local LLM (Ollama / LM Studio / ...)

  [作業終了後に 1 コマンド]
  coderouter-memory consolidate
  → Ollama (qwen3:1.7b) が buffer を読み、key facts を抽出 → facts.jsonl
  → 次のセッションから自動注入
```

**3 フェーズの仕組み:**

| フェーズ | タイミング | 何をするか |
|---|---|---|
| **capture** | レスポンスごと (自動) | `buffer.jsonl` に応答テキストを追記 |
| **consolidate** | 作業終了後 (手動 or cron) | Ollama が buffer → key facts を抽出 → `facts.jsonl` |
| **inject** | 次セッションの各リクエスト前 (自動) | `facts.jsonl` を system prompt の先頭に注入 |

---

## 使い方

### 1. インストール

```bash
# CodeRouter 本体 (v2.3.0+ が必要)
uv tool install coderouter-cli

# このプラグイン
pip install coderouter-plugin-memory

# consolidate 用の Ollama モデル (1 回だけ)
ollama pull qwen3:1.7b
```

### 2. `providers.yaml` に追記

```yaml
plugins:
  enabled:
    - memory
  config:
    memory:
      # project は省略可 — cwd のハッシュから自動検出
      project: myapp
      consolidate_model: qwen3:1.7b        # お好みの軽量モデルに変更可
      inject_token_budget: 2000            # system prompt への注入上限 (tokens)
      min_buffer_entries: 3               # consolidate のトリガー件数
```

### 3. CodeRouter 起動

```bash
coderouter serve --port 8088
# 起動ログに [memory] plugin-loaded が出る
```

### 4. 作業後に consolidate

```bash
coderouter-memory consolidate
# buffer 5 件 → Ollama で fact 抽出 → facts.jsonl に書き込み
# 次のセッションから自動注入される
```

---

## CLI リファレンス

```bash
# 現在の状態を確認 (動いているかの第一確認)
coderouter-memory status

# facts 一覧を表示
coderouter-memory list

# 手動で固定 fact を追加 (CLAUDE.md 感覚で使える)
coderouter-memory add "FastAPI を使用、asyncio ベース"

# buffer → Ollama → facts.jsonl (--dry-run で内容を確認してから実行)
coderouter-memory consolidate --dry-run
coderouter-memory consolidate

# buffer の内容を確認 (デバッグ用)
coderouter-memory buffer

# buffer を削除
coderouter-memory clear
```

`status` の出力例:

```
project   : proj-fd5766aa25d0          ← cwd から自動検出
state_dir : ~/.coderouter/memory/proj-fd5766aa25d0
buffer    : 7 entries
facts     : 12 entries
manual    : 2 lines
model     : qwen3:1.7b
ollama    : http://localhost:11434

💡 buffer が 7 件あります。consolidate を実行できます:
   coderouter-memory consolidate
```

---

## ストレージ

すべてプレーンテキスト。テキストエディタで直接確認・編集できます。

```
~/.coderouter/memory/{project}/
    buffer.jsonl   — capture された生応答 (1行=1エントリ)
    facts.jsonl    — consolidate 済みの key facts (1行=1 fact)
    manual.md      — 手動で書く固定メモ
```

---

## なぜ「wire 層」?

ほとんどの agent memory ツール ([agentmemory](https://github.com/rohitg00/agentmemory) / [mem0](https://github.com/mem0ai/mem0)) は **agent 側**で動きます。`memory_save` / `memory_recall` を MCP tool や SDK 経由で agent が能動的に呼ぶ設計です。

このプラグインは代わりに **wire 層** (agent ↔ LLM backend の中間) に置きます:

| 観点 | agent 側 memory ツール | このプラグイン (wire 層) |
|---|---|---|
| agent 側のコード | MCP client / SDK 呼び出しが必要 | **不要** (CodeRouter を通すだけ) |
| 対応 agent | MCP 対応 agent のみ | **Anthropic API を話す全 agent** |
| 追加プロセス | memory サーバーの起動が必要 | **Ollama だけ** (すでに使っているはず) |
| 外部依存 | httpx 等 | **stdlib only** |

---

## 自作 agent から見た価値 (walkthrough)

### 30 行の自作 agent

```python
import sys, os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ.get("CODEROUTER_BASE_URL", "http://localhost:8088/v1"),
    api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
)

resp = client.chat.completions.create(
    model="qwen3:14b",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": " ".join(sys.argv[1:])},
    ],
)
print(resp.choices[0].message.content)
```

**書かれていないもの**: `memory_save` / `memory_recall` / MCP client / sqlite / vector store — すべて CodeRouter の wire 層が裏で処理しています。

### 動かし方 (2 セッション)

```bash
# Session 1
python agent.py "プロジェクトのテーマカラーは indigo です。覚えておいて。"
# → "了解しました。indigo を覚えておきます。"

# 作業終了後
coderouter-memory consolidate
# → Ollama が "テーマカラーは indigo" を fact として抽出

# Session 2 (翌日でも)
python agent.py "プロジェクトのテーマカラーは何でしたっけ?"
# → "indigo です。"  ← facts.jsonl が system prompt に自動注入された
```

---

## ロードマップ

| バージョン | 内容 | 状態 |
|---|---|---|
| v0.1–0.3 | Plugin SDK 統合 / multi-backend (sqlite3・agentmemory・null) / circuit breaker | ✅ shipped |
| **v0.4.0** | **builtin JSONL + Ollama consolidation に完全再設計 / CLI / stdlib-only** | ✅ **現在** |
| v0.5 (予定) | agentmemory backend を optional で復活 (community 要望ベース) | ⏳ |

---

## 関連プロジェクト

| プロジェクト | 役割 | このプラグインとの関係 |
|---|---|---|
| [CodeRouter](https://github.com/zephel01/CodeRouter) | wire 層ルーター本体 | **必須**。Plugin SDK の host |
| [Ollama](https://github.com/ollama/ollama) | ローカル LLM ランタイム | consolidate フェーズで使用 |
| [agentmemory](https://github.com/rohitg00/agentmemory) | agent memory MCP server | 将来の optional backend 候補 |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | self-improving agent framework | 上位レイヤー、補完関係 |

---

## ライセンス

MIT — [LICENSE](./LICENSE) を参照。
