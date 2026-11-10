"""F8（被災者からの相談）を確かめる（開発手順書 Step 8）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/check_f8.py            # 読みの確認＋Gemini の確認
    python 04_scripts/check_f8.py --skip-ai  # 読みの確認だけ

- **読みだけ**を行う。`raw_consultations`・`raw_consultation_events` には1行も書かない。
  送ったときの1回の問い合わせ（`consultation_ai.triage`。整える処理と急ぎの度合い。
  画面の改修の計画.md 6章）は保存をしないので、安全に試せる。書き込みの型は、Step 6 で `CREATE TABLE LIKE` の
  一時の表で確かめた組み立て（JSON の列に辞書を入れる形）と同じ。
- 当たらないIDを渡すので、0件が返るのが正しい。確かめているのは
  「`stg_consultations` と `stg_survivors` をつないだ SQL が実際に通ること」
  「急ぎの度合いの並べ替えの式が通ること」「パラメータの型が合うこと」で、
  自動テストでは拾えない部分。
- Gemini の確認では、開発手順書 Step 8 の完了の確認のとおり
  **急を要する相談で `emergency` が立つか**、**書いていない項目が「記載なし」になるか**、
  **急ぎの度合いが相談の重さで変わるか**（命に関わる相談は high、物の相談は high でない）、
  **診断の言葉を使っていないか**を見る。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from backend.config import get_settings  # noqa: E402
from backend.services import bq, consultation_ai, consultations, visits  # noqa: E402

# 当たらない値（読みだけで済ませるため）
MISSING_SURVIVOR = "sv_check_f8_does_not_exist"
MISSING_ORG = "org_check_f8_does_not_exist"
MISSING_CONSULTATION = "cs_check_f8_does_not_exist"

SAMPLE_SURVIVOR = {
    "survivor_id": MISSING_SURVIVOR,
    "org_id": MISSING_ORG,
    "stage": "evacuation",
    "location_type": "shelter",
    "location_detail": "市民体育館",
}

# 確かめ用の相談（実在の人のものではない）。
URGENT_TEXT = (
    "きのうの夜から胸が苦しくて、息が浅い感じがします。"
    "血圧の薬がきょうで切れてしまい、どこでもらえるのか分かりません。"
)
CALM_TEXT = "体育館の物干しが足りません。洗濯物を干す場所をどこかに作ってもらえませんか。"

# 文章で触れていない項目（「記載なし」になるべきもの）。
UNTOUCHED_IN_URGENT = ("sleep",)
UNTOUCHED_IN_CALM = ("condition", "meal", "sleep", "medication")

# 書かせてはいけない言葉（要件定義書F8・10-6「診断の言葉を使わない」）。
DIAGNOSIS_WORDS = ("心筋梗塞", "狭心症", "肺炎", "脱水症", "うつ病", "不整脈と診断")


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
            "その人の相談を読む（stg_consultations）",
            lambda: consultations.list_for_survivor(
                survivor_id=MISSING_SURVIVOR, org_id=MISSING_ORG
            ),
        ),
        (
            "組織の相談を未対応から急ぎの順に読む（stg_survivors とつなぐ）",
            lambda: consultations.list_for_org(org_id=MISSING_ORG),
        ),
        (
            "相談を1件読む（返事のときに実在を確かめる）",
            lambda: consultations.fetch_consultation(
                consultation_id=MISSING_CONSULTATION,
                survivor_id=MISSING_SURVIVOR,
                org_id=MISSING_ORG,
            ),
        ),
    ]
    ok = True
    for label, func in checks:
        ok = run(label, func) and ok
    return ok


def triage(label: str, raw_text: str) -> dict | None:
    """Gemini に1回で整えさせ、急ぎの度合いを付けさせる（保存はしない。例外を外に出さない処理）。"""
    started = time.monotonic()
    result = consultation_ai.triage(
        raw_text=raw_text,
        chosen_category=None,
        survivor=SAMPLE_SURVIVOR,
        health_row=None,
        recent_visits=[],
    )
    if not result["ai_used"]:
        print(f"- {label} … NG（AIで整えられず、既定の値に落ちました）")
        print("  （.env の GEMINI_API_KEY と GEMINI_MODEL を確かめてください）")
        return None
    label_text = consultations.URGENCY_LEVELS.get(result["urgency"], result["urgency"])
    print(
        f"- {label} … OK（{time.monotonic() - started:.1f} 秒・"
        f"種類：{consultations.CATEGORIES[result['category']]}・"
        f"急を要する：{'あり' if result['emergency_flag'] else 'なし'}・急ぎ：{label_text}）"
    )
    for name, value in result["items"].items():
        if value != visits.NOT_RECORDED:
            print(f"  {visits.VISIT_FIELDS[name]}：{value}")
    if result["emergency_reason"]:
        print(f"  急を要すると見た理由：{result['emergency_reason']}")
    print(f"  急ぎの根拠：{result['urgency_evidence'] or '―'}")
    return result


def check_untouched(structured: dict, names: tuple[str, ...], *, label: str) -> bool:
    """文章で触れていない項目が「記載なし」になっているか（推測で埋めていないか）。"""
    filled = [
        visits.VISIT_FIELDS[name]
        for name in names
        if structured["items"].get(name) != visits.NOT_RECORDED
    ]
    if filled:
        print(f"- {label} … NG（{'・'.join(filled)} が埋まっていました）")
        return False
    print(f"- {label} … OK")
    return True


def check_ai() -> bool:
    ok = True

    urgent = triage("急を要する相談を整えて度合いを付ける", URGENT_TEXT)
    if urgent is None:
        return False
    if not urgent["emergency_flag"]:
        # 迷ったら「要する」にする決まり（要件定義書10-6）。立たないのは見落とし
        print("- 急を要する相談で emergency が立つ … NG（立たなかった）")
        ok = False
    else:
        print("- 急を要する相談で emergency が立つ … OK")
    ok = check_untouched(urgent, UNTOUCHED_IN_URGENT, label="触れていない項目が「記載なし」") and ok

    found = [word for word in DIAGNOSIS_WORDS if any(word in value for value in urgent["items"].values())]
    if found:
        print(f"- 診断の言葉を使っていない … NG（{'・'.join(found)}）")
        ok = False
    else:
        print("- 診断の言葉を使っていない … OK")

    if urgent["urgency"] != "high":
        print("- 命に関わる相談が「急ぎ」になる … NG")
        ok = False
    else:
        print("- 命に関わる相談が「急ぎ」になる … OK")

    print()
    calm = triage("急がない相談を整えて度合いを付ける", CALM_TEXT)
    if calm is None:
        return False
    if calm["emergency_flag"]:
        print("- 急がない相談で emergency が立たない … NG（立ってしまった）")
        ok = False
    else:
        print("- 急がない相談で emergency が立たない … OK")
    ok = check_untouched(calm, UNTOUCHED_IN_CALM, label="触れていない項目が「記載なし」") and ok

    if calm["urgency"] == "high":
        # どの相談でも「急ぎ」になるなら、ホームの並べ替えが意味を失う
        print("- 物の相談が「急ぎ」にならない … NG（急ぎと判断された）")
        ok = False
    else:
        print("- 物の相談が「急ぎ」にならない … OK")
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
    print("=== F8 の読み（書き込みはしません） ===")
    ok = check_reads()

    if not skip_ai:
        print()
        print("=== Gemini で相談を整え、急ぎの度合いを付ける（1回の問い合わせ・保存はしません） ===")
        ok = check_ai() and ok

    print()
    if ok:
        print("すべて通りました。F8 の読みと、相談の整理・急ぎの度合いの判断は動きます。")
        return 0
    print("通らないものがあります。上の NG を直してください。")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bq.BigQueryUnavailable as exc:
        print(f"BigQuery に接続できませんでした：{exc}")
        raise SystemExit(1) from exc
