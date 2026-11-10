"""Gemini に短い文と簡単なスキーマを送り、返事と応答の時間を表示する（開発手順書 Step 1-4）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/check_gemini.py

開発手順書3-3のモデル名の確認もこれで行う。
モデルは、AI Studio の料金ページで無料枠の対象になっているものを選ぶ（無料枠に収める修正の手順3-2）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from pydantic import BaseModel, Field  # noqa: E402

from backend.config import get_settings  # noqa: E402
from backend.services import gemini  # noqa: E402


class VisitDraft(BaseModel):
    """本番（F2）の縮小版。書かれていない項目は「記載なし」にさせる。"""

    condition: str = Field(description="体調。文章にない場合は「記載なし」")
    meal: str = Field(description="食事。文章にない場合は「記載なし」")
    concerns: str = Field(description="困りごと。文章にない場合は「記載なし」")


SYSTEM_INSTRUCTION = (
    "あなたは被災者支援の記録を整理する補助です。"
    "入力された文章に書かれていることだけを項目に整理します。"
    "文章に書かれていない項目は、推測せず必ず「記載なし」と書きます。"
)

SAMPLE_TEXT = "田中さんを訪問。朝から足がむくんでいるとのこと。食事は1日1回しかとれていない。"


def main() -> int:
    settings = get_settings()
    print("=== 設定 ===")
    print(f"プロジェクト : {settings.project_id or '(未設定)'}")
    print(f"APIキー      : {'設定済み' if settings.gemini_api_key else '(未設定)'}")
    print(f"モデル       : {settings.gemini_model or '(未設定)'}")

    missing = settings.missing_keys()
    if "GEMINI_API_KEY" in missing or "GEMINI_MODEL" in missing:
        print("\nNG: .env の GEMINI_API_KEY と GEMINI_MODEL を設定してください。")
        return 1

    print("\n=== クライアントの作成 ===")
    started = time.monotonic()
    try:
        gemini.init_client()
    except Exception as exc:
        print(f"NG: クライアントを作れませんでした: {exc}")
        return 1
    print(f"作成にかかった時間 : {time.monotonic() - started:.1f} 秒")

    print("\n=== 問い合わせ ===")
    print(f"入力 : {SAMPLE_TEXT}")
    started = time.monotonic()
    try:
        result = gemini.generate_structured(
            schema=VisitDraft,
            contents=SAMPLE_TEXT,
            system_instruction=SYSTEM_INSTRUCTION,
            operation="check_gemini",
        )
    except gemini.GeminiUnavailable as exc:
        print(f"\nNG: {exc}")
        print("- APIキー: .env の GEMINI_API_KEY が AI Studio で作ったキーと合っているか")
        print("- モデル名: AI Studio のモデル一覧の表記と合っているか（無料枠の対象か）")
        print("- 429 が続く: 無料枠の回数の上限。少し待つか、上限の大きいモデルを試す")
        return 1
    elapsed = time.monotonic() - started

    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    print(f"\n応答の時間 : {elapsed:.1f} 秒")

    ok = result.concerns.strip() != "" and "記載なし" in result.concerns
    print("\n=== 判定 ===")
    print("スキーマどおりのJSON : OK")
    print(f"書かれていない項目を「記載なし」にした : {'OK' if ok else 'NG（指示文の調整が要る）'}")
    print(f"10秒以内（要件定義書11-1の目安） : {'OK' if elapsed <= 10 else f'NG（{elapsed:.1f}秒）'}")
    print("\n控えて鈴木さんに伝える値: GEMINI_MODEL（要確認2）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
