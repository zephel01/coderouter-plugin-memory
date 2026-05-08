<h1 align="center">coderouter-plugin-memory</h1>

<p align="center">
  <strong>毎セッション同じ説明を繰り返す問題、<br>ルーター層で透過的に直します。</strong>
</p>

<p align="center">
  <a href="https://github.com/zephel01/coderouter-plugin-memory/actions/workflows/ci.yml"><img src="https://github.com/zephel01/coderouter-plugin-memory/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://pypi.org/project/coderouter-plugin-memory/"><img src="https://img.shields.io/pypi/v/coderouter-plugin-memory?include_prereleases&color=blue&label=pypi" alt="pypi"></a>
  <a href=""><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="python"></a>
  <a href=""><img src="https://img.shields.io/badge/runtime%20deps-1-brightgreen" alt="deps"></a>
  <a href=""><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license"></a>
</p>

<p align="center">
  <strong>日本語</strong> · <a href="./README.en.md">English</a> · <a href="https://github.com/zephel01/CodeRouter/blob/main/docs/inside/v2.3-plugin-memory-plan.md">設計ドキュメント</a> · <a href="https://github.com/zephel01/CodeRouter">CodeRouter 本体</a>
</p>

> **状態**: 3 backend (`builtin` / `agentmemory` / `null` / 計画中の `mem0`) と回路ブレーカー、112 unit tests。**実 agentmemory サーバーで round-trip 動作確認済み** (`/agentmemory/remember` → memory_id 発行 → `/agentmemory/smart-search` でヒット)。CodeRouter `v2.3.0a1+` の Plugin SDK と組み合わせて動作。最新版の詳細は上の `pypi` バッジ + [CHANGELOG](./CHANGELOG.md) を参照。設計の経緯は[設計ドキュメント](https://github.com/zephel01/CodeRouter/blob/main/docs/inside/v2.3-plugin-memory-plan.md)に。

---

## 30 秒で

```
あなたのエージェント (Claude Code / Cursor / 自作 agent)
                │  ← memory のことを知らなくていい
                ▼
        ┌─ CodeRouter ──────────────┐
        │  ① pre-request hook        │ ─→ memory backend に検索
        │     append_system_prompt 注入 │
        │  ② 通常の routing + L1-L6 ガード │
        │  ③ post-response hook       │ ─→ memory backend に蓄積
        └────────────────────────────┘
                │
                ▼
            Local LLM (Ollama / LM Studio / ...)
```

**何をしてくれるか:**

- agent が memory ツールを **知らなくても** wire 層で会話文脈が引き継がれる
- backend は **差し替え可能** (sqlite3 内蔵 / agentmemory 推奨 / mem0 / null)
- agent 側のコードを **1 行も変えない** (CodeRouter を経由するだけ)
- memory が壊れても routing は止まらない (degrade pathway 完備)
- Claude Code でも自作 agent でも同じ wire 経由なので **同じ memory 体験**

---

## なぜ「wire 層」?

ほとんどの agent memory ツール ([agentmemory](https://github.com/rohitg00/agentmemory) / [mem0](https://github.com/mem0ai/mem0) / [Letta](https://github.com/letta-ai/letta)) は **agent 側**で動きます。`memory_save` / `memory_recall` を MCP tool や SDK 経由で agent が能動的に呼ぶ設計です。

これは Claude Code / Cursor のような MCP 対応 agent には便利ですが、**自作 agent や MCP 非対応のクライアント**は対象外です。

このプラグインは代わりに **wire 層** (agent ↔ LLM backend の中間) に置きます:

| 観点 | agent 側 memory ツール (agentmemory 等) | このプラグイン (wire 層) |
|---|---|---|
| agent 側のコード | MCP client / SDK 呼び出しが必要 | **不要** (CodeRouter を通すだけ) |
| 対応 agent | MCP 対応 agent のみ | **Anthropic API を話す全 agent** |
| Memory engine | 自前実装 | **agentmemory 等を backend として委譲** |
| 機能の depth | 単独で完結 | wire 透過注入のみ (engine は backend 側) |

両者は競合せず、**組み合わせ**が理想です: agent が MCP で agentmemory を直接叩きつつ、CodeRouter も wire で透過注入することで、memory tool 利用率に依存せず確実に文脈が引き継がれます。

---

## 使い方

### 1. インストール

```bash
# CodeRouter 本体 (v2.3.0+ が必要)
uv tool install coderouter-cli

# このプラグイン
pip install coderouter-plugin-memory
```

### 2. memory backend を起動 (`agentmemory` を推奨)

```bash
# 別ターミナルで
npx -y @agentmemory/agentmemory
# → http://localhost:3111 で待ち受け
```

### 3. `providers.yaml` に追記

```yaml
plugins:
  enabled:
    - memory                       # ← entry point 名 (パッケージ名ではない)
  config:
    memory:
      backend: agentmemory         # builtin / agentmemory / mem0 / null
      endpoint: http://localhost:3111
      inject_token_budget: 2000    # システムプロンプトに注入する上限
      secret_env: AGENTMEMORY_SECRET  # 認証トークンの環境変数名
```

### 4. CodeRouter 起動

```bash
coderouter serve --port 8088
# 起動ログに plugin-loaded plugin=memory group=input_filter / observer が出る
```

これだけ。あなたの agent は CodeRouter を経由する設定にするだけで、自動的に前回セッションの文脈が引き継がれます。

---

## バックエンド一覧

| Backend       | 状態        | こういう人向け                                                                          |
|---------------|-----------|-----------------------------------------------------------------------------------|
| `builtin`     | ✅ shipped | 余計なサービスを増やしたくない。sqlite3 + LIKE 検索の最小機能。お試しに。                         |
| `agentmemory` | ✅ shipped | **推奨**。LongMemEval-S R@5 95.2%、4 層 consolidation、token 92% 削減 (公称)。`npx` で起動。 |
| `null`        | ✅ shipped | 明示的に memory を切る、または backend が落ちたときの自動 fallback 先。                          |
| `mem0`        | ⏳ planned | すでに [mem0](https://github.com/mem0ai/mem0) を使っているユーザー (要望ベースで実装)。               |

すべての backend は同じ `MemoryBackend` プロトコルを実装するので、`providers.yaml` の `backend:` を書き換えるだけで切り替えられます。各リリースで何が landed / shipped したかは [CHANGELOG](./CHANGELOG.md) を参照。

---

## ロードマップ

| Phase | 内容                                                  | 状態     |
|-------|-----------------------------------------------------|--------|
| P1    | CodeRouter 本体に Plugin SDK 追加 (`coderouter.plugins`)   | ✅ shipped ([coderouter-cli](https://pypi.org/project/coderouter-cli/)) |
| P2    | builtin sqlite3 backend / project_id / Inject / Record + tests | ✅ shipped |
| P3    | agentmemory backend + integration tests + smoke script  | ✅ shipped |
| P4    | 回路ブレーカー (連続失敗で degrade) + 自作 agent walkthrough + examples  | ✅ shipped |
| P0    | agentmemory 実 endpoint smoke 検証 (`scripts/smoke_agentmemory.sh`) | ✅ shipped (`/observe` → `/remember` 切替) |
| P5    | mem0 backend                       | ⏳ 要望ベース |

詳細な実装計画: [`v2.3-plugin-memory-plan.md`](https://github.com/zephel01/CodeRouter/blob/main/docs/inside/v2.3-plugin-memory-plan.md)

---

## 自作 agent から見た価値 (walkthrough)

このプラグインの本領は「**自作 agent がコードを書かずに memory を得られる**」点にあります。

### 30 行の自作 agent (`examples/walkthrough_agent.py` 抜粋)

```python
import sys, os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ.get("CODEROUTER_BASE_URL", "http://localhost:8088/v1"),
    api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
)

resp = client.chat.completions.create(
    model="qwen3.6:35b-a3b",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": " ".join(sys.argv[1:])},
    ],
)
print(resp.choices[0].message.content)
```

**書かれていないもの**:
- `memory_save` / `memory_recall` の呼び出し
- MCP client のセットアップ
- sqlite / vector store / Redis の import
- レート制限・fallback chain・drift detection

すべて CodeRouter の wire 層が裏で処理しています。

### 動かし方 (2 セッション)

```bash
# Terminal 1: 必要なら agentmemory を起動
npx -y @agentmemory/agentmemory   # builtin backend を使うなら不要

# Terminal 2: CodeRouter を起動
coderouter serve --port 8088

# Terminal 3: agent を 2 回実行
python examples/walkthrough_agent.py "プロジェクトのテーマカラーは indigo です。覚えておいて。"
# → "了解しました。indigo を覚えておきます。"

python examples/walkthrough_agent.py "プロジェクトのテーマカラーは何でしたっけ?"
# → "indigo です。"   ← 前回の文脈が透過的に注入されている
```

agent コードには「過去のセッション」を取り出すロジックは **1 行もない**。それでも 2 回目の応答が 1 回目を覚えているのは、wire 層の plugin が `<previous-session-context>` を system prompt に prepend しているから。

サンプル設定 / 完全なコードは [`examples/`](./examples/README.md) を参照。

**Build less in your agent, get more from the wire**。

---

## 関連プロジェクト

| プロジェクト | 役割 | このプラグインとの関係 |
|---|---|---|
| [CodeRouter](https://github.com/zephel01/CodeRouter) | wire 層ルーター本体 | **必須**。Plugin SDK の host |
| [agentmemory](https://github.com/rohitg00/agentmemory) | agent memory MCP server | **推奨 backend**。R@5 95.2% |
| [mem0](https://github.com/mem0ai/mem0) | memory layer API | 任意 backend |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | self-improving agent framework | 上位レイヤー、補完関係 |
| [Plugin SDK 設計](https://github.com/zephel01/CodeRouter/blob/main/docs/inside/plugin-architecture-draft.md) | CodeRouter 側の plugin 契約 | このプラグインが従う仕様 |

---

## ライセンス

MIT — [LICENSE](./LICENSE) を参照。
