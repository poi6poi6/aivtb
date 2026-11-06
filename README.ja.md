# ai-vtuber

[简体中文](README.md) · [English](README.en.md) · [日本語](README.ja.md)

Windows で動作するバーチャル配信者向けのコンソールです。まずはブラウザーで配信コメントの受信から音声再生までをオフラインで試せます。テキスト生成モデルや音声合成サービス、お手持ちの Live2D モデル、配信ソフトは後から接続できます。視聴者とのやり取りを記憶する機能、バックグラウンドタスク、楽曲リクエスト、投票、ゲーム連携、OBS 用の配信画面も利用できます。

## オフラインでコンソールを試す

プロジェクトのルートディレクトリで PowerShell を開き、Python 3.10 以降で仮想環境を作成してデモを起動します。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m brain.local_test
```

ブラウザーにローカルコンソールが開きます。テキストを入力するか、配信コメントを模擬送信すると、あらかじめ用意された返答と確認音で一連の流れを試せます。セットアップ後はオフラインでも利用できます。`启动.cmd` のメニューからも起動できます。終了するには PowerShell で Ctrl+C を押してください。[ローカルテストの手順](docs/local-test.md)

この README からリンクしている詳しい手順書は中国語で書かれています。

## テキストで会話する

1. ルートディレクトリで `Copy-Item .env.example .env` を実行し、利用するサービスの API キーを `.env` に設定します。標準設定では `DEEPSEEK_API_KEY` を読み込みます。
2. `config/settings.yaml` の `llm` セクションで、接続先、モデル ID、API キーを読み込む環境変数名を確認します。標準設定の `persona_llm.backend: inherit` では、キャラクターの会話にも同じ接続設定を使用します。
3. プロジェクトのルートディレクトリで次のコマンドを実行し、ブラウザーで[ローカルコンソール](http://127.0.0.1:8080/)を開きます。

```powershell
.\.venv\Scripts\python.exe -m brain.main
```

同梱の設定では `tts.backend: mock` を使用します。テキストでの会話にはテキスト生成モデルの API が必要です。音声合成エンジンやキャラクター素材は後から追加できます。ランチャーから本番起動、環境チェック、ログの確認もできます。[ランチャーの使い方](docs/launcher.md)

## 自分のキャラクターを設定する

- `config/persona.yaml` に名前、話し方、やり取りのルールを記入し、保存後にメインサービスを再起動します。公開サンプルの名前は「助手」です。[キャラクター設定](docs/formal-persona.md)
- キャラクターを画面に表示するには、利用権のある Cubism モデル一式をローカルの `skin/avatar/` に配置します。モデル定義ファイルの名前を `avatar.model3.json` にし、そこに記載された相対パスに合わせてモデル本体、テクスチャー、モーション、表情などのファイルを置いてください。起動後に初めて `skin/` を作成した場合はサービスを再起動し、ブラウザーを再読み込みします。利用できる動作はモデルに含まれるパラメーターと素材によって異なります。
- リポジトリに同梱される PixiJS、pixi-live2d-display、Cubism Core は、モデル表示用の汎用ランタイムです。音声モデル、参照音声、キャラクター素材、ローカルの保存データは各自の環境で用意してください。

## オプション機能

| 機能 | 準備するもの・設定方法 |
| --- | --- |
| 音声合成 | IndexTTS2、GPT-SoVITS、Fish Speech から選びます。専用の環境に開発元が提供する推論サービスとモデルの重みをインストールしてください。`tts.backend` を切り替え、接続先、自分で用意した参照音声、エンジンが要求する場合はその正確な書き起こしを設定します。IndexTTS2 用のローカルランチャーは、プロジェクト内の `IndexTTS2/.venv` と `IndexTTS2/checkpoints` を使用します。[Fish Speech の接続手順](docs/fish-speech.md) |
| 状態を永続化する認知エンジン | Python モジュール `cognitive_os` を含むプロジェクトを別途入手します。このプロジェクトと同じ親ディレクトリに `构想/` という名前で配置した場合は、本プロジェクトのルートから `.\.venv\Scripts\python.exe -m pip install "../构想"` を実行し、必要に応じて `cognition.enabled` を有効にします。 |
| bilibili の配信コメント | `requirements-bilibili.txt` の依存パッケージをインストールし、自分の配信ルームの `room_id` を入力して `sources.bilibili_enabled` を有効にします。[配信への接続手順](docs/bilibili-live.md) |
| OBS への表示 | `http://127.0.0.1:8080/?display=1` をブラウザーソースとして追加します。`启动OBS.cmd` は、ポータブル版 OBS を `data/obs-studio` に配置した環境向けです。 |
| バックグラウンドタスク | 独立したタスクサービスは、ローカルの実行環境とログイン済みのアカウントを使用します。[バックグラウンドタスク](docs/agent-tasks.md) |
| 楽曲リクエスト・カバー・オリジナル曲 | ローカルの音楽ファイルは `data/songs/` に配置します。カバー曲を作る場合は RVC を別途インストールし、用意した声質変換モデルを設定します。オリジナル曲は手動で取り込めます。[楽曲リクエスト](docs/bilibili-media.md) · [カバー](docs/rvc.md) · [オリジナル曲](docs/suno-manual.md) |
| ゲーム連携 | 画面認識と入力操作には `requirements-live.txt` の依存パッケージをインストールします。OCR には Tesseract と対象言語のデータが別途必要です。Minecraft には専用の Node.js プロジェクトとサーバーを使用します。[Minecraft の設定](docs/minecraft.md) |

## ディレクトリ構成

- `brain/` — Python のメインサービスとブラウザー用フロントエンド。
- `body/`、`minecraft/` — ゲーム連携。
- `config/` — 汎用の設定例。`tools/` — ランチャー、拡張機能、診断ツール。
- `tests/` — Python と JavaScript のテスト。一部の結合テストを実行するには、対象の外部サービスをローカルで用意する必要があります。
- `vendor/blivedm/` — bilibili の配信コメント用クライアント、入手元情報、ライセンス、このプロジェクト向けの互換性パッチ。
- `data/` — サービスがローカルに作成し、ログ、会話データ、生成した音声を保存するディレクトリ。

[サービス構成図](docs/architecture-20261017.mmd) · [配信準備の流れ](docs/broadcast-workflow-20260927.md) · [QQ との連携](docs/qq-private-chat.md) · [配信コーナーの管理](docs/show-sessions.md)
