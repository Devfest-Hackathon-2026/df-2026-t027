# AGENTS.md：被災者支援つなぎ記録（仮称）

AIのコーディングエージェント（Claude Code・Antigravity・Gemini CLI）向けの決まり。
人向けの説明は README.md と 01_docs/ にある。決まりはこのファイルにだけ書く（GEMINI.md は作らない）。

## 構成

- 01_docs：要件定義書・開発手順書・開発ログ・計画。仕様の正本は 01_docs/要件定義書.md
- 02_app/backend：Python 3.12・FastAPI。Gemini は services/gemini.py、指示文は prompts/<処理>.md
- 02_app/frontend：ビルドしない HTML・CSS・JavaScript（スマートフォンを基準にする）
- 03_sql：BigQuery（raw → staging → mart）。番号の順に流す。列を足すときは migrations の SQL を足す
- 04_scripts：確認と初期データのスクリプト。05_tests：pytest
- 技術の構成は要件定義書10章のとおりにする。変えたいときは先に聞く

## 動かし方（Windows PowerShell）

- 仮想環境：.venv\Scripts\Activate.ps1
- 起動：uvicorn backend.main:app --app-dir 02_app --reload
- テスト：python -m pytest（外部のサービスにはつながない。Gemini と BigQuery は差し替えて試す）

## 必ず守ること

- 秘密情報（.env、APIキー、鍵のファイル）を読まない・書かない・コミットしない。値は環境変数からだけ読む
- ログに記録の本文や健康・介護の情報、氏名、電話番号を出さない（要配慮個人情報）。出してよいのは処理の名前・ID・かかった時間・エラーの種類
- 試すデータは架空の人物だけにする。実在の人の氏名や健康の状態を、コード・テスト・プロンプト・会話に入れない
- 立場と組織は、要求ごとに BigQuery から読み直す。要求に書かれた立場や組織を信じない。見る権限のない記録は 404 を返す（要件定義書10-3）
- Gemini はバックエンドからだけ呼び、APIキーを画面に渡さない
- Gemini は AI Studio の無料枠で呼ぶ。1分・1日の回数に上限があるため、試しに何度も呼ばない。429 のときは画面で手入力に切り替わる
- Gemini の呼び出しは services/gemini.py の generate_structured を使う（構造化出力・30秒で打ち切り・2回までやり直し）
- generate_content から Interactions API へ移さない。temperature・thinking_budget を外さない。モデル名をコードに書かない（.env の GEMINI_MODEL）。変えたいときは先に聞く
- AI の出力は案として扱う。採用するかは人が決める。渡した一覧にない施設・制度を出させない（返った ID を一覧と照らす）
- BigQuery は INSERT だけを使い、UPDATE と DELETE を使わない。クエリは必ずパラメータ付きで書く
- 決まっていないことは仮に決めて進め、01_docs/開発ログ.md の「要確認」に書く

## 手順のあるもの

- Gemini の処理を足す・直すとき：.agents/skills/add-gemini-process/SKILL.md を読む

## 文書の書き方

- 日本語の「である調」。短い文で書き、専門用語は言い換える
