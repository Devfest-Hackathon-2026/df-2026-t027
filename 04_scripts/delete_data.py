"""デモで入れたデータを消す（開発手順書 Step 12-3、要件定義書11-3）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/delete_data.py                  # 消さない。いま何行あるかを数えて出すだけ
    python 04_scripts/delete_data.py --rows-only      # raw の表の中身だけ消す（表とビューは残す）
    python 04_scripts/delete_data.py --datasets       # データセット3つをまとめて消す（作り直しが必要）

どちらの消し方でも、**プロジェクトIDを打ち込む確認**を通らないと消さない。

消したあとの作り直し：

    python 04_scripts/run_sql.py --all     # --datasets で消したときだけ必要（表とビューを作り直す）
    python 04_scripts/seed.py              # 初期データ（全国共通の支援制度）を入れ直す

注意：

- 消すのは BigQuery のデータだけ。**Firebase のアカウントは残る**ため、同じ電話番号は
  「登録がまだの人」に戻る（デモ当日の手順9で使える）。
- `--datasets` は `gdgoc_mart` も消す。mart に書き込むスケジュールクエリは、
  `run_sql.py --all` で作り直すまで失敗する。
- 消す時期は鈴木さんが決める（健康の情報は要配慮個人情報。要件定義書11-3）。
- 数える処理も消す処理も、件数しか画面に出さない。記録の本文や健康の情報は出さない
  （要件定義書10-2の6）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from backend.config import get_settings  # noqa: E402
from backend.services import bq  # noqa: E402

# raw の表の一覧は 03_sql/02_raw_tables.sql を正本として読む
# （表が増えたときに、このスクリプトを直し忘れても消し漏れが出ないようにする）。
RAW_TABLES_SQL = REPO_ROOT / "03_sql" / "02_raw_tables.sql"

CREATE_TABLE_PATTERN = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`[^`]*?\.(\w+)\.(\w+)`",
    re.IGNORECASE,
)


class NoTablesFound(RuntimeError):
    """02_raw_tables.sql から表の名前を1つも読み取れなかった。"""


def raw_tables(sql_path: Path = RAW_TABLES_SQL) -> list[str]:
    """`03_sql/02_raw_tables.sql` が作る表の名前を、書かれている順に返す。"""
    text = sql_path.read_text(encoding="utf-8")
    names = [match.group(2) for match in CREATE_TABLE_PATTERN.finditer(text)]
    # 同じ表が2回出てきても1つにする（順番は保つ）
    unique: list[str] = []
    for name in names:
        if name not in unique:
            unique.append(name)
    if not unique:
        raise NoTablesFound(f"表の名前を読み取れませんでした: {sql_path}")
    return unique


def count_rows(tables: list[str], *, project: str, dataset: str) -> list[tuple[str, int | None]]:
    """表ごとの行数を返す。読めなかった表は None にする（まだ作っていない場合）。"""
    counts: list[tuple[str, int | None]] = []
    for table in tables:
        qualified = bq.qualified_table(project, dataset, table)
        try:
            row = bq.query_one(f"SELECT COUNT(*) AS n FROM {qualified}")  # noqa: S608
        except Exception as exc:  # 表が無い・権限が無い・認証切れ
            print(f"  {table}: 読めませんでした（{type(exc).__name__}）")
            counts.append((table, None))
            continue
        counts.append((table, int(row["n"]) if row else 0))
    return counts


def confirm(expected: str, *, action: str, reader=None) -> bool:
    """消す前の確認。プロジェクトIDを**そのまま**打ち込まないと進まない。

    `reader` は呼ぶときに決める（既定の引数で `input` を縛るとテストから差し替えられない）。
    """
    read = reader or input
    print()
    print(f"これから {action} を行います。元には戻せません。")
    print(f"続けるなら、プロジェクトIDを打ち込んでください（{expected}）：", end="", flush=True)
    try:
        answer = read()
    except EOFError:
        answer = ""
    if answer.strip() != expected:
        print("打ち込まれた文字が合わないため、何も消しませんでした。")
        return False
    return True


def delete_rows(tables: list[str], *, project: str, dataset: str) -> None:
    """raw の表の中身だけ消す（表とビューは残すので作り直しは要らない）。"""
    for table in tables:
        qualified = bq.qualified_table(project, dataset, table)
        # DELETE は `WHERE` を省けない。TRUE で全行を指す。
        bq.execute(f"DELETE FROM {qualified} WHERE TRUE", operation="delete_data")  # noqa: S608
        print(f"  {table}: 消しました")


def delete_datasets(datasets: list[str], *, project: str) -> None:
    """データセットごと消す（中身もまとめて消える）。"""
    client = bq.get_client()
    for dataset in datasets:
        bq.validate_identifier(dataset)
        client.delete_dataset(
            f"{project}.{dataset}", delete_contents=True, not_found_ok=True
        )
        print(f"  {dataset}: 消しました")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="デモで入れたデータを消す（既定は数えるだけ）",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--rows-only",
        action="store_true",
        help="raw の表の中身だけ消す（表とビューは残す。run_sql.py の流し直しは不要）",
    )
    group.add_argument(
        "--datasets",
        action="store_true",
        help="gdgoc_raw・gdgoc_staging・gdgoc_mart をまとめて消す（run_sql.py --all の流し直しが必要）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    project = settings.project_id
    if not project:
        print("GOOGLE_CLOUD_PROJECT が設定されていません（.env を確認してください）")
        return 1

    tables = raw_tables()
    datasets = [settings.dataset_raw, settings.dataset_staging, settings.dataset_mart]

    print(f"プロジェクト：{project}")
    print(f"データセット：{', '.join(datasets)}")
    print(f"raw の表：{len(tables)}個")
    print()
    print("いま入っている行数：")
    counts = count_rows(tables, project=project, dataset=settings.dataset_raw)
    total = 0
    for table, n in counts:
        if n is None:
            continue
        total += n
        print(f"  {table}: {n}行")
    print(f"  合計：{total}行")

    if not args.rows_only and not args.datasets:
        print()
        print("消していません。消すときは --rows-only か --datasets を付けてください。")
        return 0

    if args.rows_only:
        if not confirm(project, action=f"{settings.dataset_raw} の全ての表の中身を消すこと"):
            return 1
        print()
        delete_rows(tables, project=project, dataset=settings.dataset_raw)
        print()
        print("消しました。初期データを入れ直すには：python 04_scripts/seed.py")
        return 0

    if not confirm(project, action=f"データセット {', '.join(datasets)} をまとめて消すこと"):
        return 1
    print()
    delete_datasets(datasets, project=project)
    print()
    print("消しました。作り直すには、この順に実行してください：")
    print("  python 04_scripts/run_sql.py --all")
    print("  python 04_scripts/seed.py")
    print("（mart に書き込むスケジュールクエリは、作り直すまで失敗します）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
