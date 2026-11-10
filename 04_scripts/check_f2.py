"""F2（訪問・見守りの記録）と F3（健康・介護の情報）を確かめる（開発手順書 Step 6）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/check_f2.py            # 読みの確認＋Gemini で整える確認
    python 04_scripts/check_f2.py --skip-ai  # 読みの確認だけ

- **読みだけ**を行う。raw の表には1行も書かない（デモのデータを汚さない）。
  Gemini で整える処理も、保存をしない処理なので安全に試せる（要件定義書10-5の「AI」の列）。
- 当たらないIDを渡すので、0件が返るのが正しい。確かめているのは
  「SQL の構文」「列があること」「パラメータの型が合うこと」で、自動テストでは拾えない部分。
- Gemini の確認では、**文章に書いていない項目が「記載なし」になるか**を見る
  （開発手順書 Step 6 の完了の確認の2つめ）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from backend.config import get_settings  # noqa: E402
from backend.services import bq, gemini, health, visit_ai, visits  # noqa: E402

# 当たらない値（読みだけで済ませるため）
MISSING_SURVIVOR = "sv_check_f2_does_not_exist"
MISSING_ORG = "org_check_f2_does_not_exist"
MISSING_VISIT = "vs_check_f2_does_not_exist"

# Gemini に渡す確かめ用の文章（実在の人のものではない）。
# 「睡眠」と「服薬」には触れていないので、そこが「記載なし」になるのが正しい。
SAMPLE_TEXT = (
    "きのうから熱っぽいと言っている。ごはんは朝だけ食べた。"
    "体育館が寒いと言っていて、毛布が足りないそうだ。来週には仮設住宅へ移ると話していた。"
)

# 文章に書かれていない項目（ここが「記載なし」でなければ、推測で埋めている）
UNWRITTEN_FIELDS = ("sleep", "medication")

SAMPLE_SURVIVOR = {
    "survivor_id": MISSING_SURVIVOR,
    "org_id": MISSING_ORG,
    "stage": "evacuation",
    "location_type": "shelter",
    "location_detail": "市民体育館",
}


def run(label: str, func) -> bool:
    started = time.monotonic()
    try:
        result = func()
    except Exception as exc:  # noqa: BLE001  確かめるのが目的なので、種類を問わず拾う
        print(f"- {label} … NG（{type(exc).__name__}: {exc}）")
        return False
    count = len(result) if isinstance(result, list) else (0 if result is None else 1)
    print(f"- {label} … OK（{time.monotonic() - started:.1f} 秒・{count} 件）")
    return True


def check_reads() -> bool:
    checks = [
        (
            "健康・介護の最新の版（stg_health_latest）",
            lambda: health.fetch_latest(survivor_id=MISSING_SURVIVOR, org_id=MISSING_ORG),
        ),
        (
            "訪問・見守りの記録の一覧（stg_visits）",
            lambda: visits.list_visits(survivor_id=MISSING_SURVIVOR, org_id=MISSING_ORG),
        ),
        (
            "訪問・見守りの記録の1件読み（stg_visits）",
            lambda: visits.fetch_visit(
                visit_id=MISSING_VISIT, survivor_id=MISSING_SURVIVOR, org_id=MISSING_ORG
            ),
        ),
    ]
    ok = True
    for label, func in checks:
        ok = run(label, func) and ok
    return ok


def check_structure() -> bool:
    """Gemini で9つの項目に整えられるかを確かめる（保存はしない）。"""
    started = time.monotonic()
    try:
        result = visit_ai.structure(
            raw_text=SAMPLE_TEXT, survivor=SAMPLE_SURVIVOR, health_row=None
        )
    except gemini.GeminiUnavailable as exc:
        print(f"- Gemini で項目に整える … NG（{exc}）")
        print("  （.env の GEMINI_API_KEY と GEMINI_MODEL を確かめてください）")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"- Gemini で項目に整える … NG（{type(exc).__name__}: {exc}）")
        return False

    elapsed = time.monotonic() - started
    items = result["items"]
    ok = True

    if set(items) != set(visits.VISIT_FIELDS):
        print(f"- 9つの項目がそろっている … NG（{sorted(items)}）")
        ok = False
    else:
        print(f"- Gemini で項目に整える … OK（{elapsed:.1f} 秒・9項目）")

    unwritten = [name for name in UNWRITTEN_FIELDS if items.get(name) != visits.NOT_RECORDED]
    if unwritten:
        labels = "・".join(visits.VISIT_FIELDS[name] for name in unwritten)
        print(f"- 書いていない項目が「記載なし」になる … NG（{labels} が埋められている）")
        ok = False
    else:
        print("- 書いていない項目が「記載なし」になる … OK")

    print()
    print("整えた項目：")
    for name, label in visits.VISIT_FIELDS.items():
        print(f"  {label}：{items[name]}")

    location = result["location_proposal"]
    print()
    if location:
        print(
            "居場所の更新の案："
            f"{location['location_type_label']}"
            f"（{location['location_detail'] or '場所の名前は記載なし'}）"
            f"／理由：{location['reason'] or '―'}"
        )
    else:
        print("居場所の更新の案：なし")

    health_proposal = result["health_proposal"]
    if health_proposal:
        changed = [
            f"{health.FIELD_LABELS[name]}={value}"
            for name, value in health_proposal["fields"].items()
            if value
        ]
        print(f"健康・介護の更新の案：{'／'.join(changed)}")
    else:
        print("健康・介護の更新の案：なし")
    return ok


def main() -> int:
    skip_ai = "--skip-ai" in sys.argv
    settings = get_settings()
    if not settings.project_id:
        print("GOOGLE_CLOUD_PROJECT が設定されていません（.env を確認してください）")
        return 1

    print("=== 設定 ===")
    print(f"プロジェクト : {settings.project_id}")
    print(f"場所         : {settings.bq_location}")
    print(f"Gemini       : {settings.gemini_model}（AI Studio のAPIキー）")
    print()
    print("=== F2・F3 の読み（書き込みはしません） ===")
    ok = check_reads()

    if not skip_ai:
        print()
        print("=== Gemini で項目に整える（保存はしません） ===")
        ok = check_structure() and ok

    print()
    if ok:
        print("すべて通りました。F2・F3 の読みと、AIで項目に整える処理は動きます。")
        return 0
    print("通らないものがあります。上の NG を直してください。")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bq.BigQueryUnavailable as exc:
        print(f"BigQuery に接続できませんでした：{exc}")
        raise SystemExit(1) from exc
