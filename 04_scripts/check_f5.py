"""F5（受け入れ先の施設）と F3（引き継ぎシート）を確かめる（開発手順書 Step 9）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/check_f5.py            # 読みの確認＋候補の並び＋Gemini の確認（引き継ぎシート）
    python 04_scripts/check_f5.py --skip-ai  # 読みの確認と候補の並びだけ（Gemini を呼ばない）

- **読みだけ**を行う。`raw_facilities`・`raw_placements`・`raw_handovers` には1行も書かない。
  施設の候補（`facilities.rank_candidates`）と引き継ぎシートの案（`handover_ai.draft`）は
  保存をしないので、安全に試せる。施設の候補は AI を使わずルールで並べる（画面の改修の計画.md 7章）。
- 当たらないIDを渡すので、0件が返るのが正しい。確かめているのは
  「`stg_facilities_latest`（`is_stale` を計算するビュー）の SQL が実際に通ること」
  「`stg_placements`・`stg_handovers` の読みが通ること」「パラメータの型が合うこと」で、
  自動テストでは拾えない部分。
- 開発手順書 Step 9 の完了の確認のとおり、**候補が理由つきで出るか**、**空きのない施設と
  必要な医療に対応できない施設が候補に出ないか**を見る。Gemini の確認では、
  **引き継ぎシートの要約と気をつけることが出るか**、**診断の言葉を使っていないか**を見る。
- 施設の情報は**この確認のために作った架空のもの**で、実在の施設ではない。
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from backend.config import get_settings  # noqa: E402
from backend.services import (  # noqa: E402
    bq,
    consultations,
    facilities,
    gemini,
    handover_ai,
    handovers,
)

# 当たらない値（読みだけで済ませるため）
MISSING_SURVIVOR = "sv_check_f5_does_not_exist"
MISSING_ORG = "org_check_f5_does_not_exist"
MISSING_FACILITY = "fc_check_f5_does_not_exist"

NOW = dt.datetime.now(dt.timezone.utc)

SAMPLE_SURVIVOR = {
    "survivor_id": MISSING_SURVIVOR,
    "org_id": MISSING_ORG,
    "full_name": "確認用 三郎",
    "age": 82,
    "stage": "evacuation",
    "location_type": "shelter",
    "location_detail": "市民体育館",
}

# 架空の健康・介護の情報（透析が必要な方）。
SAMPLE_HEALTH = {
    "version": 2,
    "source": "professional",
    "chronic_diseases": ["高血圧", "腎不全"],
    "medications": ["降圧剤"],
    "allergies": None,
    "primary_clinic": "確認用クリニック",
    "medical_needs": ["透析", "週3回の通院の付き添い"],
    "care_level": "要介護2",
    "assistance_mobility": "車いすを使う",
    "assistance_meal": None,
    "assistance_toilet": "一部介助",
    "dementia_notes": None,
    "family_contact": "長女（遠方）",
}

# 架空の訪問の記録と相談（引き継ぎシートの案に渡す）。
SAMPLE_VISITS = [
    {
        "visit_id": "vs_check_f5",
        "visited_at": NOW,
        "location": "市民体育館",
        "condition": "きのうから熱っぽい",
        "meal": "朝だけ食べている",
        "sleep": "夜に何度もトイレで起きる",
        "medication": "降圧剤は飲めている",
        "environment": "体育館が寒い",
        "concerns": "透析の通院の足がない",
        "change": "前回より食事が減った",
        "next_action": "受診をすすめる",
    }
]

SAMPLE_CONSULTATIONS = [
    {
        "consultation_id": "cs_check_f5",
        "consulted_at": NOW,
        "category": consultations.CATEGORIES["care"],
        "urgency": "medium",
        "body": "透析の送迎をお願いできないか／体育館が寒い",
    }
]


def sample_facility(
    facility_id: str,
    *,
    name: str,
    facility_type: str,
    vacancies: int,
    capabilities: list[str],
    is_stale: bool = False,
    water_outage: bool = False,
) -> dict:
    """架空の施設の1行（`stg_facilities_latest` と同じ形）。実在の施設ではない。"""
    return {
        "facility_id": facility_id,
        "org_id": MISSING_ORG,
        "version": 1,
        "facility_name": name,
        "facility_type": facility_type,
        "address": "確認用の住所",
        "vacancies": vacancies,
        "capabilities": capabilities,
        "water_outage": water_outage,
        "power_outage": False,
        "updated_at": NOW,
        "updated_by": "check_f5",
        "is_stale": is_stale,
    }


# 架空の施設4件。透析に対応できて空きがあるのは A と D（Dは情報が古い）。
SAMPLE_FACILITIES = [
    sample_facility(
        "fc_check_a",
        name="確認用A 福祉避難所",
        facility_type="welfare_shelter",
        vacancies=4,
        capabilities=["dialysis", "wheelchair", "nurse"],
    ),
    sample_facility(
        "fc_check_b",
        name="確認用B 介護施設（空きなし）",
        facility_type="care_facility",
        vacancies=0,
        capabilities=["dialysis", "wheelchair"],
    ),
    sample_facility(
        "fc_check_c",
        name="確認用C 福祉避難所（透析に対応できない）",
        facility_type="welfare_shelter",
        vacancies=5,
        capabilities=["wheelchair", "dementia"],
    ),
    sample_facility(
        "fc_check_d",
        name="確認用D 病院（情報が古い）",
        facility_type="hospital",
        vacancies=1,
        capabilities=["dialysis", "nurse", "bedridden"],
        is_stale=True,
        water_outage=True,
    ),
]

# 書かせてはいけない言葉（要件定義書10-6「診断の言葉を使わない」）。
DIAGNOSIS_WORDS = ("腎不全と診断", "脱水症", "肺炎", "うつ病", "認知症と診断", "心不全")

DESTINATION = "確認用A 福祉避難所"


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
            "施設の一覧を読む（stg_facilities_latest・is_stale を計算する）",
            lambda: facilities.list_facilities(org_id=MISSING_ORG),
        ),
        (
            "施設を1件読む（更新のときに実在を確かめる）",
            lambda: facilities.fetch_facility(
                facility_id=MISSING_FACILITY, org_id=MISSING_ORG
            ),
        ),
        (
            "移る先の決定を読む（stg_placements）",
            lambda: facilities.list_placements(
                survivor_id=MISSING_SURVIVOR, org_id=MISSING_ORG
            ),
        ),
        (
            "引き継ぎシートを読む（stg_handovers）",
            lambda: handovers.list_for_survivor(
                survivor_id=MISSING_SURVIVOR, org_id=MISSING_ORG
            ),
        ),
    ]
    ok = True
    for label, func in checks:
        ok = run(label, func) and ok
    return ok


def check_filter() -> tuple[bool, list[dict], list[str]]:
    """Gemini に渡す前に外す処理（要件定義書F5）。AIを呼ばずに確かめる。

    Returns:
        (すべて通ったか, Gemini に渡す施設, 判断できない必要な医療)
    """
    ok = True
    needs = SAMPLE_HEALTH["medical_needs"]
    matched, unmatched = facilities.required_capabilities(needs)
    print(
        f"- 必要な医療を読み替える … OK（コード：{'・'.join(matched) or 'なし'}／"
        f"判断できない：{'・'.join(unmatched) or 'なし'}）"
    )
    if "dialysis" not in matched:
        print("  NG：「透析」を読み替えられませんでした")
        ok = False
    if not unmatched:
        print("  NG：「週3回の通院の付き添い」が判断できないものとして残りませんでした")
        ok = False

    eligible, excluded = facilities.filter_eligible(SAMPLE_FACILITIES, medical_needs=needs)
    passed = [row["facility_id"] for row in eligible]
    print(f"- 渡す施設 … {'・'.join(passed)}")
    for item in excluded:
        print(f"  外した：{item['facility_name']}（{item['reason']}）")
    if passed != ["fc_check_a", "fc_check_d"]:
        print("  NG：空きのない施設と対応できない施設を外せていません")
        ok = False
    return ok, eligible, unmatched


def check_candidates(eligible: list[dict]) -> bool:
    """ルールで候補を並べる（AI は使わない。保存はしない）。"""
    candidates = facilities.rank_candidates(
        eligible,
        medical_needs=SAMPLE_HEALTH["medical_needs"],
        health_row=SAMPLE_HEALTH,
    )
    print(f"- 施設の候補を並べる … {len(candidates)} 件")
    ok = True
    for item in candidates:
        print(f"  {item['facility_name']}（空き {item['vacancies']}）：{item['reason'] or '―'}")
        if item["is_stale"]:
            print(f"    ※{item['stale_notice']}")

    returned = [item["facility_id"] for item in candidates]
    if not returned:
        print("  NG：候補が1件も出ませんでした（空きのある施設を渡しています）")
        ok = False
    if any(not item["reason"] for item in candidates):
        print("  NG：理由の無い候補があります")
        ok = False
    if len(returned) > facilities.MAX_CANDIDATES:
        print("  NG：候補が3件を超えました")
        ok = False
    else:
        print("- 候補が3件以内 … OK")
    if "fc_check_b" in returned or "fc_check_c" in returned:
        print("  NG：空きのない施設か、対応できない施設が候補に出ました")
        ok = False
    else:
        print("- 空きのない施設と対応できない施設が候補に出ない … OK")
    return ok


def check_handover() -> bool:
    """Gemini に引き継ぎシートの案を作らせる（保存はしない）。"""
    started = time.monotonic()
    result = handover_ai.draft(
        survivor=SAMPLE_SURVIVOR,
        health_row=SAMPLE_HEALTH,
        recent_visits=SAMPLE_VISITS,
        recent_consultations=SAMPLE_CONSULTATIONS,
        destination=DESTINATION,
    )
    elapsed = time.monotonic() - started
    mark = "OK" if result["ai_used"] else "NG（AIで要約を作れませんでした）"
    print(f"- 引き継ぎシートの案を作る … {mark}（{elapsed:.1f} 秒）")
    if result["notice"]:
        print(f"  知らせ：{result['notice']}")
    if not result["ai_used"]:
        return False

    print("  【要約】")
    print(f"  {result['summary']}")
    print("  【気をつけること】")
    print(f"  {result['cautions']}")

    ok = True
    if not result["summary"]:
        print("- 要約が入っている … NG（空でした）")
        ok = False
    else:
        print("- 要約が入っている … OK")
    if not result["cautions"]:
        print("- 気をつけることが入っている … NG（空でした）")
        ok = False
    else:
        print("- 気をつけることが入っている … OK")

    text = f"{result['summary']} {result['cautions']}"
    found = [word for word in DIAGNOSIS_WORDS if word in text]
    if found:
        print(f"- 診断の言葉を使っていない … NG（{'・'.join(found)}）")
        ok = False
    else:
        print("- 診断の言葉を使っていない … OK")

    # 保存できる長さに収まっているか（表に入らない案を画面に出さない）
    if len(result["summary"]) > handovers.SUMMARY_MAX_LENGTH:
        print("- 保存できる長さに収まっている … NG")
        ok = False
    else:
        print("- 保存できる長さに収まっている … OK")
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
    print("=== F5・F3 の読み（書き込みはしません） ===")
    ok = check_reads()

    print()
    print("=== 候補を並べる前に外す処理（AIは呼びません） ===")
    filter_ok, eligible, _ = check_filter()
    ok = filter_ok and ok

    print()
    print("=== 施設の候補をルールで並べる（AIは呼びません・保存はしません） ===")
    ok = check_candidates(eligible) and ok

    if not skip_ai:
        print()
        print("=== Gemini で引き継ぎシートの案を作る（保存はしません） ===")
        ok = check_handover() and ok

    print()
    if ok:
        print("すべて通りました。F5 の施設と候補、F3 の引き継ぎシートの案は動きます。")
        return 0
    print("通らないものがあります。上の NG を直してください。")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bq.BigQueryUnavailable as exc:
        print(f"BigQuery に接続できませんでした：{exc}")
        raise SystemExit(1) from exc
