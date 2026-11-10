"""初期データが入ったかを確かめる（開発手順書 Step 3 の完了の確認）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/check_seed.py

確かめること（初期データは全国共通の支援制度だけ。2026-11-10 に組織と参加コードはやめた）:

1. `stg_programs` に、全国共通の制度が1制度につき1行だけ出る（2回流しても重複しない）
2. その組織のIDが空（全国共通）である
3. 制度の列（名前・条件・内容・申請先・期限・書類・出典）がどれも空でなく、出典が［4］［17］のURLである

組織ごとの制度（2026-11-10 より前に入れた輪島市・珠洲市の分など）は、参考として件数だけ出す。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))
sys.path.insert(0, str(REPO_ROOT / "04_scripts"))

from backend.config import get_settings  # noqa: E402
from backend.services import bq  # noqa: E402

import seed  # noqa: E402


class Report:
    """確かめた件数と、NG だったことを集める（check_bigquery.py と同じ形）。"""

    def __init__(self) -> None:
        self.checks = 0
        self.failures: list[str] = []

    def ok(self, label: str) -> None:
        self.checks += 1
        print(f"- OK : {label}")

    def ng(self, label: str) -> None:
        self.checks += 1
        self.failures.append(label)
        print(f"- NG : {label}")

    def expect(self, condition: bool, label: str) -> bool:
        if condition:
            self.ok(label)
        else:
            self.ng(label)
        return condition


def check_programs(settings, report: Report) -> None:
    print("\n=== 全国共通の支援制度（stg_programs）===")
    columns = ", ".join(("program_id", "org_id", "version") + seed.PROGRAM_CONTENT_COLUMNS)
    sql = (
        f"SELECT {columns}\n"
        f"FROM `{settings.project_id}.{settings.dataset_staging}.stg_programs`"
    )
    rows = bq.query(sql)
    by_id: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_id.setdefault(str(row["program_id"]), []).append(row)

    expected_ids = []
    for program in seed.SEED_PROGRAMS:
        program_id = seed.program_id_for(seed.NATIONWIDE_ORG_ID, program.key)
        expected_ids.append(program_id)
        found = by_id.get(program_id, [])
        if not report.expect(len(found) == 1, f"{program_id} が1行だけ出る（実際は {len(found)} 行）"):
            continue
        row = found[0]
        report.expect((row.get("org_id") or "") == "", f"{program_id} の組織のIDが空（全国共通）である")
        empty = [
            column for column in seed.PROGRAM_CONTENT_COLUMNS if not str(row.get(column) or "").strip()
        ]
        report.expect(not empty, f"{program_id} の列が空でない（空：{empty or 'なし'}）")
        # 出典は制度ごとに決めてある（［4］［17］、または［18］。使いやすさの改修の計画.md 6-2）
        report.expect(
            row.get("source_url") == program.source_url,
            f"{program_id} の出典が seed.py に書いた要件定義書14章のURLである",
        )

    nationwide_extra = sorted(
        program_id
        for program_id, found in by_id.items()
        if program_id not in expected_ids and all(not (row.get("org_id") or "") for row in found)
    )
    report.expect(
        not nationwide_extra, f"知らない全国共通の制度が出ない（出たもの：{nationwide_extra or 'なし'}）"
    )
    org_scoped = sorted(set(by_id) - set(expected_ids) - set(nationwide_extra))
    if org_scoped:
        print(f"- 参考: 組織ごとの制度 {len(org_scoped)} 件（初期データの確認からは外す）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初期データ（全国共通の支援制度）が入ったかを確かめる")
    parser.parse_args(argv)

    settings = get_settings()
    print("=== 設定 ===")
    print(f"プロジェクト : {settings.project_id or '(未設定)'}")
    print(f"場所         : {settings.bq_location}")

    if not settings.project_id:
        print("\nNG: .env の GOOGLE_CLOUD_PROJECT を設定してください。")
        return 1

    report = Report()
    try:
        check_programs(settings, report)
    except bq.BigQueryUnavailable as exc:
        print(f"\nNG: BigQuery を読めません: {exc}")
        print("- 03_sql を流し終えているか（python 04_scripts/run_sql.py --all）")
        print("- 初期データを入れたか（python 04_scripts/seed.py）")
        return 1
    except Exception as exc:  # 権限不足など
        print(f"\nNG: 確認の途中で止まりました: {type(exc).__name__}: {exc}")
        return 1

    print(f"\n=== まとめ（{report.checks} 件） ===")
    if report.failures:
        print(f"NG が {len(report.failures)} 件あります：")
        for failure in report.failures:
            print(f"- {failure}")
        return 1
    print("すべてOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
