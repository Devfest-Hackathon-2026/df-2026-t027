"""F4（悪化の兆しと訪問の順番）を確かめる（開発手順書 Step 7）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/check_f4.py            # 読みの確認＋Gemini の判断の確認
    python 04_scripts/check_f4.py --skip-ai  # 読みの確認だけ

- **読みだけ**を行う。`raw_assessments` には1行も書かない。
  判断の処理（`risk_ai.assess`）は保存をしないので、安全に試せる。
  書き込みの型は `raw_visits.location_proposal`（JSON の列）と同じ組み立てで、
  Step 6 で実機に書けていることが確かめられている。
- 当たらないIDを渡すので、0件が返るのが正しい。確かめているのは
  「新しい並べ替えの列（`risk_rank` ほか）が `int_survivor_list` に実在すること」
  「SQL の構文」「パラメータの型が合うこと」で、自動テストでは拾えない部分。
- Gemini の確認では、開発手順書 Step 7 の完了の確認のとおり、
  **悪化を思わせる記録で度合いが高くなり、根拠に記録の記述が出るか**を見る。
  落ち着いた記録でも高くなってしまうなら、順番を並べる意味がないので、その2つを比べる。
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from backend.config import get_settings  # noqa: E402
from backend.services import assessments, bq, gemini, risk_ai, survivors, visits  # noqa: E402

# 当たらない値（読みだけで済ませるため）
MISSING_SURVIVOR = "sv_check_f4_does_not_exist"
MISSING_ORG = "org_check_f4_does_not_exist"
SAMPLE_VISIT = "vs_check_f4_sample"

SAMPLE_SURVIVOR = {
    "survivor_id": MISSING_SURVIVOR,
    "org_id": MISSING_ORG,
    "stage": "evacuation",
    "location_type": "shelter",
    "location_detail": "市民体育館",
}

# 確かめ用の記録（実在の人のものではない）。
WORSENING = visits.clean_items(
    {
        "location": "市民体育館",
        "condition": "3日前から熱っぽく、きょうは起き上がれない",
        "meal": "朝だけ少し食べた。水はあまり飲めていない",
        "sleep": "夜中に何度も目が覚める",
        "medication": "血圧の薬が3日分しか残っていない",
        "environment": "体育館が寒く、毛布が足りない",
        "concerns": "だれにも相談できないと話している",
        "change": "前回より元気がない",
    }
)

CALM = visits.clean_items(
    {
        "location": "仮設第1団地",
        "condition": "変わりなく過ごせている",
        "meal": "3食とれている",
        "sleep": "よく眠れている",
        "medication": "薬は足りている",
        "environment": "水道も電気も使える",
        "change": "落ち着いている",
    }
)

# 書かせてはいけない言葉（要件定義書F4「医療の診断ではない」）。
DIAGNOSIS_WORDS = ("脱水症", "脱水状態", "うつ病", "肺炎", "インフルエンザ", "認知症と診断")


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
            "一覧を訪ねる順番で読む（int_survivor_list の risk_rank ほか）",
            lambda: survivors.list_survivors(org_id=MISSING_ORG),
        ),
        (
            "絞り込みつきで訪ねる順番で読む（段階・居場所）",
            lambda: survivors.list_survivors(
                org_id=MISSING_ORG, stage="evacuation", location_type="shelter"
            ),
        ),
        (
            "詳細の1件読み（risk_level・risk_evidence を含む）",
            lambda: survivors.fetch_survivor(MISSING_SURVIVOR),
        ),
    ]
    ok = True
    for label, func in checks:
        ok = run(label, func) and ok
    return ok


def visit_rows(items: dict[str, str], *, visit_id: str) -> list[dict]:
    return [{"visit_id": visit_id, "visited_at": dt.datetime.now(dt.timezone.utc), **items}]


def judge(label: str, items: dict[str, str], *, visit_id: str) -> dict | None:
    """Gemini に判断させて、結果を表示する（保存はしない）。"""
    started = time.monotonic()
    try:
        result = risk_ai.assess(
            survivor=SAMPLE_SURVIVOR,
            health_row=None,
            recent_visits=visit_rows(items, visit_id=visit_id),
        )
    except gemini.GeminiUnavailable as exc:
        print(f"- {label} … NG（{exc}）")
        print("  （.env の GEMINI_API_KEY と GEMINI_MODEL を確かめてください）")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"- {label} … NG（{type(exc).__name__}: {exc}）")
        return None

    print(
        f"- {label} … OK（{time.monotonic() - started:.1f} 秒・"
        f"度合い：{assessments.level_label(result['level'])}）"
    )
    print(f"  次に必要な対応：{result['next_action'] or '―'}")
    for item in result["evidence"]:
        source = item["source_id"] or "（IDなし）"
        print(f"  根拠：「{item['quote']}」／{item['source_kind'] or '種類なし'}：{source}")
    if not result["evidence"]:
        print("  根拠：なし")
    return result


def check_assessment() -> bool:
    """悪化を思わせる記録で度合いが高くなるか（開発手順書 Step 7 の完了の確認1）。"""
    worsening = judge("悪化を思わせる記録の判断", WORSENING, visit_id=SAMPLE_VISIT)
    if worsening is None:
        return False
    ok = True

    if worsening["level"] != "high":
        print(
            f"- 悪化を思わせる記録で度合いが「高い」になる … NG"
            f"（{assessments.level_label(worsening['level']) or worsening['level']} だった）"
        )
        ok = False
    else:
        print("- 悪化を思わせる記録で度合いが「高い」になる … OK")

    if not worsening["evidence"]:
        print("- 根拠に記録の記述が出る … NG（根拠が空）")
        ok = False
    elif not any(item["source_id"] == SAMPLE_VISIT for item in worsening["evidence"]):
        print("- 根拠に記録のIDが付く … NG（渡した記録のIDが根拠に無い）")
        ok = False
    else:
        print("- 根拠に記録の記述とIDが出る … OK")

    found = [
        word
        for word in DIAGNOSIS_WORDS
        if word in (worsening["next_action"] or "")
        or any(word in item["quote"] for item in worsening["evidence"])
    ]
    if found:
        print(f"- 診断の言葉を使っていない … NG（{'・'.join(found)}）")
        ok = False
    else:
        print("- 診断の言葉を使っていない … OK")

    print()
    calm = judge("落ち着いた記録の判断", CALM, visit_id=SAMPLE_VISIT)
    if calm is None:
        return False
    if calm["level"] == "high":
        # どの記録でも「高い」になるなら、一覧を並べ替える意味がなくなる
        print("- 落ち着いた記録では「高い」にならない … NG（高いと判断された）")
        ok = False
    else:
        print("- 落ち着いた記録では「高い」にならない … OK")
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
    print("=== F4 の読み（書き込みはしません） ===")
    ok = check_reads()

    if not skip_ai:
        print()
        print("=== Gemini で悪化の兆しを判断する（保存はしません） ===")
        ok = check_assessment() and ok

    print()
    if ok:
        print("すべて通りました。F4 の読みと、悪化の兆しの判断は動きます。")
        return 0
    print("通らないものがあります。上の NG を直してください。")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bq.BigQueryUnavailable as exc:
        print(f"BigQuery に接続できませんでした：{exc}")
        raise SystemExit(1) from exc
