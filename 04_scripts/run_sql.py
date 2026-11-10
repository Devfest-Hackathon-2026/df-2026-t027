"""03_sql/ の SQL を順に実行する（開発手順書 Step 2-2）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/run_sql.py --all              # 01〜05 を順に流す
    python 04_scripts/run_sql.py --all --dry-run    # 流さずに、置き換えた SQL を確かめる
    python 04_scripts/run_sql.py 03_sql/05_mart.sql # 指定したファイルだけ流す（デモ前の mart の作り直し）

- `{{PROJECT}}` と `{{LOCATION}}` を `.env` の値に置き換えてから実行する。
- 何度流しても同じ状態になる（CREATE … IF NOT EXISTS / CREATE OR REPLACE …）。
- 場所は `.env` の `BQ_LOCATION`（既定は asia-northeast1）。ずれると「Not found: Dataset」になる。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from backend.config import get_settings  # noqa: E402
from backend.services import bq  # noqa: E402

SQL_DIR = REPO_ROOT / "03_sql"

# ファイル名は「番号_名前.sql」。番号の順に流す（前のファイルに頼る順になっている）。
SQL_FILE_PATTERN = re.compile(r"^\d{2}_.+\.sql$")

# 置き換える値も SQL の文字列に入るため、形を検査する（要件定義書10-3）。
PROJECT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
LOCATION_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
PLACEHOLDER_PATTERN = re.compile(r"\{\{(\w+)\}\}")


class UnknownPlaceholder(ValueError):
    """SQL に、置き換え方の分からない `{{…}}` が残っている。"""


def sql_files() -> list[Path]:
    """03_sql/ の SQL を番号の順に返す。"""
    return sorted(
        path for path in SQL_DIR.glob("*.sql") if SQL_FILE_PATTERN.match(path.name)
    )


def render(sql: str, *, project: str, location: str) -> str:
    """`{{PROJECT}}`・`{{LOCATION}}` を置き換える。ほかの `{{…}}` が残っていたら拒む。"""
    if not project:
        raise ValueError("GOOGLE_CLOUD_PROJECT が設定されていません（.env を確認してください）")
    if not PROJECT_PATTERN.match(project):
        raise ValueError(f"プロジェクトIDの形ではありません: {project!r}")
    if not location or not LOCATION_PATTERN.match(location):
        raise ValueError(f"場所の形ではありません: {location!r}")

    rendered = sql.replace("{{PROJECT}}", project).replace("{{LOCATION}}", location)

    left = sorted(set(PLACEHOLDER_PATTERN.findall(rendered)))
    if left:
        raise UnknownPlaceholder(f"置き換え方の分からない項目が残っています: {', '.join(left)}")
    return rendered


def resolve_targets(args: argparse.Namespace) -> list[Path]:
    if args.all:
        return sql_files()
    targets = []
    for name in args.files:
        path = Path(name)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.exists():
            raise FileNotFoundError(f"SQL のファイルが見つかりません: {name}")
        targets.append(path)
    return targets


def run_file(path: Path, *, project: str, location: str, dry_run: bool) -> None:
    sql = render(path.read_text(encoding="utf-8"), project=project, location=location)
    label = path.name

    if dry_run:
        print(f"--- {label}（流さずに表示）---")
        print(sql)
        return

    statements = len([block for block in sql.split(";") if block.strip()])
    print(f"- {label}（{statements} 文）… ", end="", flush=True)
    started = time.monotonic()
    client = bq.get_client()
    job = client.query(sql, location=location)
    job.result()
    billed = job.total_bytes_billed or 0
    print(f"OK（{time.monotonic() - started:.1f} 秒・課金対象 {billed / 1024 / 1024:.1f} MiB）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="03_sql/ の SQL を順に実行する")
    parser.add_argument("files", nargs="*", help="流す SQL のファイル（省略時は --all が必要）")
    parser.add_argument("--all", action="store_true", help="01〜05 を順に流す")
    parser.add_argument("--dry-run", action="store_true", help="流さずに、置き換えた SQL を表示する")
    args = parser.parse_args(argv)

    if not args.all and not args.files:
        parser.error("--all か、流すファイルを指定してください")

    settings = get_settings()
    try:
        targets = resolve_targets(args)
    except FileNotFoundError as exc:
        print(f"NG: {exc}")
        return 1

    print("=== 設定 ===")
    print(f"プロジェクト : {settings.project_id or '(未設定)'}")
    print(f"場所         : {settings.bq_location}")
    print(f"流すファイル : {', '.join(path.name for path in targets)}")
    print()

    for path in targets:
        try:
            run_file(
                path,
                project=settings.project_id,
                location=settings.bq_location,
                dry_run=args.dry_run,
            )
        except (ValueError, bq.BigQueryUnavailable) as exc:
            print(f"\nNG: {path.name} で止まりました\n{exc}")
            return 1
        except Exception as exc:  # BigQuery が返したエラー（構文・権限など）
            print(f"\nNG: {path.name} で止まりました\n{type(exc).__name__}: {exc}")
            return 1

    if not args.dry_run:
        print("\nすべて流し終わりました。もう一度流しても同じ状態になります。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
