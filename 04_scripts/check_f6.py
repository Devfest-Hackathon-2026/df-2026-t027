"""F6（受けられる支援と申請の状況）を確かめる（開発手順書 Step 10）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/check_f6.py            # 読みの確認＋Gemini の確認
    python 04_scripts/check_f6.py --skip-ai  # 読みの確認だけ
    python 04_scripts/check_f6.py --org org_wajima  # 制度の一覧を実際の組織で読む

- **読みだけ**を行う。`raw_support_events` には1行も書かない（候補を出す処理
  `support_ai.suggest` は保存をしないので、安全に試せる）。
- 当たらないIDを渡す読みは 0 件が返るのが正しい。確かめているのは
  「`stg_supports_latest` と `stg_programs` をつないだ SQL が実際に通ること」
  「`NULLS LAST` や `DATE_DIFF` を含む並べ替えが通ること」「パラメータの型が合うこと」で、
  自動テストでは拾えない部分。
- Gemini の確認では、開発手順書 Step 10 の完了の確認のとおり
  **全壊・建設で基礎支援金と加算支援金が理由つきで出るか**、
  **渡していない制度を作り出さないか**（混ぜ込んだ架空のIDが候補に出ないか）、
  **被害なしのときに無理に候補を出さないか**を見る。
- 制度の一覧は、`--org` を付けると実際の組織（`stg_programs`）から読む。付けないときは
  このファイルの見本の制度を使う（BigQuery に制度が未登録でも試せるようにする）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from backend.config import get_settings  # noqa: E402
from backend.services import bq, gemini, support_ai, supports  # noqa: E402

# 当たらない値（読みだけで済ませるため）
MISSING_SURVIVOR = "sv_check_f6_does_not_exist"
MISSING_ORG = "org_check_f6_does_not_exist"
MISSING_SUPPORT = "sp_check_f6_does_not_exist"

# 渡していない制度（これが候補に出たら、要件定義書10-6 の照合が効いていない）
FAKE_PROGRAM = "org_check_f6__fake_program"

SAMPLE_SURVIVOR = {
    "survivor_id": MISSING_SURVIVOR,
    "org_id": MISSING_ORG,
    "household_size": 3,
    "stage": "rebuilding",
}

# 見本の制度（出典のある公的な制度の内容に合わせた確認用。保存はしない）
SAMPLE_PROGRAMS = [
    {
        "program_id": "org_check_f6__seikatsu_saiken_kiso",
        "org_id": MISSING_ORG,
        "program_name": "被災者生活再建支援金（基礎支援金）",
        "conditions": "住宅が全壊した世帯、住宅をやむを得ず解体した世帯、長期避難の世帯、大規模半壊の世帯",
        "benefit": "全壊・解体・長期避難は100万円、大規模半壊は50万円（複数世帯）",
        "apply_to": "市町村の担当窓口",
        "deadline_rule": "災害発生日から13月以内",
        "documents": "罹災証明書、住民票、預金通帳の写し",
        "source_url": "https://example.lg.jp/kiso",
    },
    {
        "program_id": "org_check_f6__seikatsu_saiken_kasan",
        "org_id": MISSING_ORG,
        "program_name": "被災者生活再建支援金（加算支援金）",
        "conditions": "基礎支援金の対象の世帯が、住宅を建設・購入、補修、賃借したとき",
        "benefit": "建設・購入は200万円、補修は100万円、賃借は50万円（複数世帯）",
        "apply_to": "市町村の担当窓口",
        "deadline_rule": "災害発生日から37月以内",
        "documents": "罹災証明書、契約書の写し",
        "source_url": "https://example.lg.jp/kasan",
    },
    {
        "program_id": "org_check_f6__oukyu_shuri",
        "org_id": MISSING_ORG,
        "program_name": "住宅の応急修理",
        "conditions": "半壊、準半壊の住宅で、応急的な修理をすれば住めるとき",
        "benefit": "1世帯あたり約70万円を上限に市町村が修理を行う",
        "apply_to": "市町村の建築担当課",
        "deadline_rule": "災害発生日から1月以内に申し込む",
        "documents": "罹災証明書、修理見積書",
        "source_url": "https://example.lg.jp/shuri",
    },
]


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


def check_reads(org_id: str | None) -> bool:
    checks = [
        (
            "組織の支援制度を読む（stg_programs）",
            lambda: supports.list_programs(org_id=org_id or MISSING_ORG),
        ),
        (
            "その人の支援と申請の状況を読む（stg_supports_latest と stg_programs をつなぐ）",
            lambda: supports.list_for_survivor(
                survivor_id=MISSING_SURVIVOR, org_id=org_id or MISSING_ORG
            ),
        ),
        (
            "支援の記録を1件読む（状況の更新のときに実在を確かめる）",
            lambda: supports.fetch_support(
                support_id=MISSING_SUPPORT,
                survivor_id=MISSING_SURVIVOR,
                org_id=org_id or MISSING_ORG,
            ),
        ),
    ]
    ok = True
    for label, func in checks:
        ok = run(label, func) and ok
    return ok


def suggest(label: str, *, housing_damage: str, rebuild_intent: str, programs: list) -> dict | None:
    """Gemini に候補を出させて、結果を表示する（保存はしない）。"""
    started = time.monotonic()
    try:
        result = support_ai.suggest(
            survivor=SAMPLE_SURVIVOR,
            housing_damage=housing_damage,
            rebuild_intent=rebuild_intent,
            household_size=SAMPLE_SURVIVOR["household_size"],
            programs=programs,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"- {label} … NG（{type(exc).__name__}: {exc}）")
        return None

    if not result["ai_used"]:
        print(f"- {label} … NG（AIを呼べませんでした：{result['notice']}）")
        print("  （.env の GEMINI_API_KEY と GEMINI_MODEL を確かめてください）")
        return None

    print(
        f"- {label} … OK（{time.monotonic() - started:.1f} 秒・"
        f"候補 {len(result['candidates'])} 件・落としたID {result['dropped_count']} 件）"
    )
    for item in result["candidates"]:
        deadline = item["deadline"] or "（期限は決まりから日付にできず）"
        print(f"  {item['program_name']}：{item['reason']}")
        print(f"    申請先：{item['apply_to']}／期限：{deadline}")
    return result


def check_ai() -> bool:
    ok = True

    # 1. 全壊・建設 → 基礎支援金と加算支援金が出る（開発手順書 Step 10 の完了の確認）
    full = suggest(
        "全壊・建設の候補",
        housing_damage="total",
        rebuild_intent="rebuild",
        programs=SAMPLE_PROGRAMS,
    )
    if full is None:
        return False
    ids = {item["program_id"] for item in full["candidates"]}
    for needed, name in (
        ("org_check_f6__seikatsu_saiken_kiso", "基礎支援金"),
        ("org_check_f6__seikatsu_saiken_kasan", "加算支援金"),
    ):
        if needed in ids:
            print(f"- 全壊・建設で{name}が出る … OK")
        else:
            print(f"- 全壊・建設で{name}が出る … NG（候補に入らなかった）")
            ok = False
    if any(not item["reason"] for item in full["candidates"]):
        print("- 候補に理由が付く … NG（理由が空の候補がある）")
        ok = False
    else:
        print("- 候補に理由が付く … OK")

    # 2. 渡していない制度を作り出さない（要件定義書10-6）
    print()
    partial = suggest(
        "半壊・補修の候補（渡していないIDを作り出さないか）",
        housing_damage="half",
        rebuild_intent="repair",
        programs=SAMPLE_PROGRAMS,
    )
    if partial is None:
        return False
    given = {row["program_id"] for row in SAMPLE_PROGRAMS}
    outside = {item["program_id"] for item in partial["candidates"]} - given
    if outside:
        print(f"- 渡した一覧の制度だけを返す … NG（{', '.join(sorted(outside))}）")
        ok = False
    else:
        print("- 渡した一覧の制度だけを返す … OK")

    # 3. 被害なしのときに無理に候補を並べない
    print()
    none_damage = suggest(
        "被害なし・決めていないの候補",
        housing_damage="none",
        rebuild_intent="undecided",
        programs=SAMPLE_PROGRAMS,
    )
    if none_damage is None:
        return False
    if len(none_damage["candidates"]) >= len(SAMPLE_PROGRAMS):
        print("- 被害なしで無理に候補を並べない … NG（渡した制度を全部返した）")
        ok = False
    else:
        print("- 被害なしで無理に候補を並べない … OK")

    # 4. 制度が無いときは呼ばない（Gemini を使わずに案内を返す）
    print()
    empty = support_ai.suggest(
        survivor=SAMPLE_SURVIVOR,
        housing_damage="total",
        rebuild_intent="rebuild",
        household_size=3,
        programs=[],
    )
    if empty["candidates"] or empty["ai_used"]:
        print("- 制度が無いときはAIを呼ばない … NG")
        ok = False
    else:
        print(f"- 制度が無いときはAIを呼ばない … OK（案内：{empty['notice']}）")
    return ok


def main() -> int:
    skip_ai = "--skip-ai" in sys.argv
    org_id = None
    if "--org" in sys.argv:
        index = sys.argv.index("--org")
        if index + 1 < len(sys.argv):
            org_id = sys.argv[index + 1]

    settings = get_settings()
    if not settings.project_id:
        print("GOOGLE_CLOUD_PROJECT が設定されていません（.env を確認してください）")
        return 1

    print("=== 設定 ===")
    print(f"プロジェクト : {settings.project_id}")
    print(f"場所         : {settings.bq_location}")
    print(f"Gemini       : {settings.gemini_model}（AI Studio のAPIキー）")
    print(f"制度の読み先 : {org_id or MISSING_ORG}（--org で変えられます）")
    print()
    print("=== F6 の読み（書き込みはしません） ===")
    ok = check_reads(org_id)

    if not skip_ai:
        print()
        print("=== Gemini で受けられそうな支援を出す（保存はしません） ===")
        ok = check_ai() and ok

    print()
    if ok:
        print("すべて通りました。F6 の読みと、受けられそうな支援の出し方は動きます。")
        return 0
    print("通らないものがあります。上の NG を直してください。")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bq.BigQueryUnavailable as exc:
        print(f"BigQuery に接続できませんでした：{exc}")
        raise SystemExit(1) from exc
