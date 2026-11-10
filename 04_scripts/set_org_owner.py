"""すでにある組織に、決めた人をオーナーとして付ける（追加修正の計画.md 6章。修正点⑥）。

初期データの組織（輪島市・珠洲市）は、アプリで作った組織ではないため、オーナーがいない。
元の参加コードも保存されていない。このスクリプトで1人をオーナーにし、その人がアプリの
プロフィールの画面で参加コードを発行し直す。2人目からは、オーナーがアプリの画面で付ける。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/set_org_owner.py --user-id <利用者のID>           # 書かない。何をするかを出すだけ
    python 04_scripts/set_org_owner.py --user-id <利用者のID> --apply   # 確認のあと、オーナーにする

- 利用者のIDは Firebase Authentication の uid（`stg_users.user_id`）。その人が入っている組織の
  オーナーになる。組織は指定しない（その人の今の組織を `stg_users` から読む）。
- オーナーにできるのは、組織に入っている**専門職**だけ（アプリの画面と同じ決まり）。
- `--apply` でも、**組織のIDを打ち込む確認**を通らないと書かない（delete_data.py と同じ）。
- 書くのは `raw_users` への新しい版の行だけ（上書きしない。要件定義書10-4）。立場・組織・
  表示名・連絡先などは今の版から引き継ぎ、権限だけを owner にする。
- 何度流しても同じ状態になる。すでにオーナーなら何もしない。
- 画面に出すのは利用者のID・立場・組織のID・権限だけ。表示名・連絡先・住所は出さない。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from backend.config import get_settings  # noqa: E402
from backend.services import bq, users  # noqa: E402

# 行を足した人。初期設定と同じく、人ではなく仕組みが足したことを表す（seed.py と同じ値）。
SYSTEM_USER = "system"
SYSTEM_ROLE = "system"


class NotEligible(ValueError):
    """オーナーにできない（見つからない・専門職でない・組織に入っていない）。"""


def check_target(row: dict[str, object] | None, user_id: str) -> dict[str, object]:
    """オーナーにできる人かを確かめて、その行を返す。"""
    if row is None:
        raise NotEligible(f"利用者が見つかりません: {user_id}")
    if not row.get("org_id"):
        raise NotEligible("この利用者は、まだ組織に入っていません")
    if row.get("role") != users.ROLE_PROFESSIONAL:
        raise NotEligible(
            f"オーナーにできるのは専門職だけです（この利用者の立場：{row.get('role')}）"
        )
    return row


def confirm(expected: str, *, reader=None) -> bool:
    """書く前の確認。組織のIDを**そのまま**打ち込まないと進まない。"""
    read = reader or input
    print()
    print("これから raw_users に、オーナーの権限を付けた新しい版の行を1行足します。")
    print(f"続けるなら、組織のIDを打ち込んでください（{expected}）：", end="", flush=True)
    try:
        answer = read()
    except EOFError:
        answer = ""
    if answer.strip() != expected:
        print("打ち込まれた文字が合わないため、何も書きませんでした。")
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="すでにある組織に、決めた人をオーナーとして付ける（既定は書かない）",
    )
    parser.add_argument("--user-id", required=True, help="オーナーにする人の利用者のID（uid）")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="確認のあと、実際に書く（付けなければ、何をするかを出すだけ）",
    )
    return parser


def main(argv: list[str] | None = None, *, reader=None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if not settings.project_id:
        print("GOOGLE_CLOUD_PROJECT が設定されていません（.env を確認してください）")
        return 1

    user_id = args.user_id.strip()
    try:
        row = users.fetch_user(user_id)
    except bq.BigQueryUnavailable as exc:
        print(f"NG: 利用者を読めませんでした（{type(exc).__name__}）")
        return 1

    try:
        target = check_target(row, user_id)
    except NotEligible as exc:
        print(f"NG: {exc}")
        return 1

    org_id = str(target.get("org_id"))
    print(f"プロジェクト：{settings.project_id}")
    print(f"利用者のID　：{user_id}")
    print(f"立場　　　　：{target.get('role')}")
    print(f"組織のID　　：{org_id}")
    print(f"今の権限　　：{target.get('org_permission') or '（なし）'}")

    if users.is_owner(target):
        print()
        print("すでにこの組織のオーナーです。何も書きませんでした。")
        return 0

    if not args.apply:
        print()
        print("書いていません。オーナーにするときは --apply を付けてください。")
        return 0

    if not confirm(org_id, reader=reader):
        return 1

    try:
        users.set_org_owner(
            target=target, owner=True, actor_id=SYSTEM_USER, actor_role=SYSTEM_ROLE
        )
    except (users.UserValidationError, bq.BigQueryUnavailable) as exc:
        print(f"NG: 書けませんでした（{type(exc).__name__}）")
        return 1

    print()
    print("オーナーにしました。アプリのプロフィールの画面で、参加コードを発行し直してください")
    print("（この組織の元のコードは保存されていないため、発行し直すまで画面には出ません）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
