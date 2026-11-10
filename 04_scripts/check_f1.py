"""F1（被災者の記録と担当）の読みのSQLが BigQuery で通るかを確かめる（開発手順書 Step 5）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/check_f1.py

- **読みだけ**を行う。raw の表には1行も書かない（デモのデータを汚さない）。
- 当たらないIDを渡すので、0件が返るのが正しい。確かめているのは
  「SQL の構文」「列があること」「パラメータの型が合うこと」で、自動テストでは拾えない部分。
- `int_survivor_list` に `version` を足したあと、ビューを作り直したかの確認にもなる。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from backend.config import get_settings  # noqa: E402
from backend.services import assignments, bq, link_codes, survivors, users  # noqa: E402

# 当たらない値（読みだけで済ませるため）
MISSING_SURVIVOR = "sv_check_f1_does_not_exist"
MISSING_ORG = "org_check_f1_does_not_exist"
MISSING_USER = "uid_check_f1_does_not_exist"


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


def main() -> int:
    settings = get_settings()
    if not settings.project_id:
        print("GOOGLE_CLOUD_PROJECT が設定されていません（.env を確認してください）")
        return 1

    print("=== 設定 ===")
    print(f"プロジェクト : {settings.project_id}")
    print(f"場所         : {settings.bq_location}")
    print()
    print("=== F1 の読み（書き込みはしません） ===")

    checks = [
        ("被災者の詳細（int_survivor_list）", lambda: survivors.fetch_survivor(MISSING_SURVIVOR)),
        ("被災者の一覧（絞り込みなし）", lambda: survivors.list_survivors(org_id=MISSING_ORG)),
        (
            "被災者の一覧（段階・居場所・担当で絞る）",
            lambda: survivors.list_survivors(
                org_id=MISSING_ORG,
                stage="evacuation",
                location_type="shelter",
                assignee_user_id=MISSING_USER,
            ),
        ),
        (
            "居場所の履歴（stg_location_history）",
            lambda: survivors.location_history(
                survivor_id=MISSING_SURVIVOR, org_id=MISSING_ORG
            ),
        ),
        (
            "氏名と年齢が近い記録（stg_survivors）",
            lambda: survivors.find_similar(org_id=MISSING_ORG, full_name="確認用", age=80),
        ),
        (
            "担当の出来事（raw_assignment_events）",
            lambda: assignments.list_for_survivor(
                survivor_id=MISSING_SURVIVOR, org_id=MISSING_ORG
            ),
        ),
        ("有効な確認コード（stg_link_codes）", link_codes.fetch_valid_codes),
        (
            "確認コードの誤りの回数（raw_link_codes）",
            lambda: link_codes.count_recent_failures(MISSING_USER),
        ),
        ("支援者の1件読み（stg_users）", lambda: users.fetch_supporter(MISSING_USER)),
        (
            "組織の支援者の一覧（stg_users）",
            lambda: users.list_by_role(org_id=MISSING_ORG, role=users.ROLE_VOLUNTEER),
        ),
    ]

    ok = True
    for label, func in checks:
        ok = run(label, func) and ok

    print()
    if ok:
        print("すべて通りました。F1 の読みは BigQuery で動きます。")
        return 0
    print("通らないものがあります。上の NG を直してください。")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bq.BigQueryUnavailable as exc:
        print(f"BigQuery に接続できませんでした：{exc}")
        raise SystemExit(1) from exc
