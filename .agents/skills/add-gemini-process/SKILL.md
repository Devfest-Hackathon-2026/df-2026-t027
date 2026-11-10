---
name: add-gemini-process
description: このリポジトリで Gemini の処理（要件定義書10-6の表の1行）を足す・直すときに使う。指示文のファイル、Pydantic のスキーマ、generate_structured の呼び方、テストの書き方をそろえる。
---

# Gemini の処理を足す手順

1. 要件定義書10-6の表に、渡すもの・返させるもの・守らせることを書く（先に仕様を決める）
2. 02_app/backend/prompts/<処理>.md に指示文を書く。「書かれていないことは『記載なし』にする」「診断をしない」など、既存の指示文の書き方にそろえる
3. 返させる形を Pydantic のモデルで書く
4. services/gemini.py の generate_structured(schema=..., contents=..., system_instruction=load_prompt("<処理>"), operation="<処理>") で呼ぶ
   - operation はログに出る名前。contents（本文）はログに出さない
   - 項目に振り分けるだけの処理は thinking_budget=0 を渡す
   - temperature は既定の 0 のままにする
5. GeminiUnavailable を受けたら、画面に手入力の欄を出して続けられるようにする
6. 一覧から選ばせる処理は、返った ID を渡した一覧と照らし、無いものを捨てる
7. 05_tests にテストを足す。Gemini にはつながず、test_gemini.py と同じようにクライアントを差し替える。データは架空の人物だけ
8. python -m pytest がすべて通ることを確かめる
