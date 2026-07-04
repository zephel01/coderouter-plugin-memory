# Examples

実践的な providers.yaml サンプルと、自作 agent walkthrough 用スクリプト。

## providers.yaml の選び方

> **v0.4.0 メモ**: 外部 backend (agentmemory / mem0 / sqlite3 / null) と
> `backend:` / `endpoint:` / `secret_env:` / `circuit_breaker_*` キーは廃止され、
> memory は単一の JSONL ストア (capture → consolidate → inject) に統一されました。
> 下記サンプルはすべて v0.4.0 スキーマに更新済みです。

| ファイル | こんなとき |
|---|---|
| [`providers.builtin.yaml`](./providers.builtin.yaml) | **推奨・最小構成**。外部サービス無しで JSONL memory を試す |
| [`providers.agentmemory.yaml`](./providers.agentmemory.yaml) | agentmemory backend の placeholder (v0.5 で復活予定。現状は builtin と同じ JSONL) |
| [`providers.coding.yaml`](./providers.coding.yaml) | コーディング agent + 3 段 fallback chain (速攻 → 高品質 MoE → 万能) |
| [`providers.lightweight.yaml`](./providers.lightweight.yaml) | RAM 8 GB 程度の機械、試運転、CI |
| [`providers.longcontext.yaml`](./providers.longcontext.yaml) | 入力が長い場合だけ自動で 32K context モデルに切替 (auto_router) |
| [`providers.multibackend.yaml`](./providers.multibackend.yaml) | Ollama + LM Studio の冗長化 + self-healing |
| [`providers.null.yaml`](./providers.null.yaml) | plugin は load するが memory は明示的に切る (capture/inject 無効, CI / staging) |

## 使い方

```bash
# 1. お好みの設定を project root にコピー
cp examples/providers.coding.yaml ~/works/project/myproject/.coderouter/providers.yaml

# 2. consolidate 用の軽量モデルを pull (未取得なら)
ollama pull qwen3:1.7b

# 3. CodeRouter を project root から起動
cd ~/works/project/myproject
export CODEROUTER_CONFIG=$(pwd)/.coderouter/providers.yaml
coderouter serve --port 8088

# 4. 別 terminal で agent
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

## モデル名のチェック

各 yaml の `model:` 欄は **`ollama list` の NAME 列の値そのまま** に揃えてください:

```bash
ollama list
```

合わなければ `providers.yaml` を編集するか、`ollama pull <name>` でモデルを取得。

## 自作 agent walkthrough

[`walkthrough_agent.py`](./walkthrough_agent.py) — 30 行の OpenAI-SDK agent が memory を透過的に得る実例。memory ライブラリを 1 つも import せず、CodeRouter 経由で前回セッションの文脈が引き継がれます。

```bash
# 1 回目: memory に何かを覚えさせる
python examples/walkthrough_agent.py "プロジェクトのテーマカラーは indigo です。"

# 2 回目: 別セッションで尋ねる
python examples/walkthrough_agent.py "プロジェクトのテーマカラーは何でしたっけ?"
# → "indigo です。"  ← agent コードに変更ゼロで前回の文脈が通る
```

詳細は script 冒頭の docstring を参照。

## トラブルシュート

| 症状 | 確認 |
|---|---|
| `Extra inputs are not permitted: plugins` | CodeRouter が古い (v2.3.0a1 未満)。`uv tool install coderouter-cli --pre --reinstall` |
| `model 'X' not found` (404) | `ollama list` で実在するモデル名と yaml の `model:` を一致 |
| `plugin-not-found plugin=memory` warn | `pip list | grep coderouter-plugin-memory` で同 Python 環境にインストール済みか |
| memory が injected されない | `coderouter-memory consolidate` で facts.jsonl が生成されているか / CodeRouter ログに `memory-plugin-loaded` があるか |
