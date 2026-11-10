"""BigQuery の準備ができたかをまとめて確かめる（開発手順書 Step 2-4）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/run_sql.py --all
    python 04_scripts/check_bigquery.py

確かめること:

1. データセット3つが東京（asia-northeast1）にある（要件定義書10-4）
2. raw の表が7-3-1の16表と周りの地域の状況の1表（使いやすさの改修の計画.md 8章）そろっていて、発生日時の日付で分割・組織のID（と被災者のID）でクラスタ化されている
3. staging のビューが7-3-2の16個＋周りの地域の状況の1個＋7-3-3の5個そろっていて、どれも実際に読める
4. mart の表が7-3-4の5表そろっていて、as_of と org_id を持っている
5. 一時の表で `bq.insert`・`bq.query` を試し、書いた行を読める
"""

from __future__ import annotations

import datetime as dt
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from google.cloud import bigquery  # noqa: E402

from backend.config import get_settings  # noqa: E402
from backend.services import bq  # noqa: E402

EXPECTED_LOCATION = "asia-northeast1"

# 7-3-1 の16表（と8章・10章の2表）と、期待するクラスタの列（10-4：組織のIDと被災者のID）
EXPECTED_RAW_TABLES: dict[str, list[str]] = {
    "raw_organizations": ["org_id"],
    "raw_users": ["org_id", "user_id"],
    "raw_survivors": ["org_id", "survivor_id"],
    "raw_link_codes": ["org_id", "survivor_id", "code_hash"],
    "raw_assignment_events": ["org_id", "survivor_id", "assignment_id"],
    "raw_visits": ["org_id", "survivor_id", "visit_id"],
    "raw_update_applies": ["org_id", "survivor_id", "visit_id"],
    "raw_health_versions": ["org_id", "survivor_id"],
    "raw_handovers": ["org_id", "survivor_id", "handover_id"],
    "raw_assessments": ["org_id", "survivor_id", "assessment_id"],
    "raw_facilities": ["org_id", "facility_id"],
    "raw_placements": ["org_id", "survivor_id", "placement_id"],
    "raw_programs": ["org_id", "program_id"],
    "raw_support_events": ["org_id", "survivor_id", "support_id"],
    "raw_consultations": ["org_id", "survivor_id", "consultation_id"],
    "raw_consultation_events": ["org_id", "survivor_id", "consultation_id"],
    # 周りの地域の状況（使いやすさの改修の計画.md 8章。7-3-1には無い追加の表）
    "raw_disaster_status": ["org_id", "municipality_code", "status_id"],
    # 家族と記録のつながり（使いやすさの改修の計画.md 10章）
    "raw_family_links": ["org_id", "survivor_id", "user_id"],
}

# 7-3-2 の16ビュー（と8章・10章の2ビュー）と 7-3-3 の5ビュー
EXPECTED_STAGING_VIEWS: list[str] = [
    "stg_organizations",
    "stg_users",
    "stg_survivors",
    "stg_location_history",
    "stg_link_codes",
    "stg_active_assignments",
    "stg_visits",
    "stg_health_latest",
    "stg_handovers",
    "stg_assessments",
    "stg_assessment_latest",
    "stg_facilities_latest",
    "stg_placements",
    "stg_programs",
    "stg_supports_latest",
    "stg_consultations",
    # 周りの地域の状況（使いやすさの改修の計画.md 8章）
    "stg_disaster_status",
    # いまつながっている家族（使いやすさの改修の計画.md 10章）
    "stg_family_links",
    "int_survivor_visits",
    "int_survivor_consultations",
    "int_survivor_supports",
    "int_survivor_assignees",
    "int_survivor_list",
]

# 7-3-4 の5表
EXPECTED_MART_TABLES: list[str] = [
    "mart_org_status",
    "mart_visit_coverage",
    "mart_consultation_response_daily",
    "mart_support_uptake",
    "mart_activity_daily",
]

# 5の確認に使う一時の表（確かめ終わったら消す）
CHECK_TABLE = "check_insert_tmp"


class Report:
    """OK・NG を数えて、最後にまとめを出す。"""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0

    def ok(self, message: str) -> None:
        self.checks += 1
        print(f"  OK   {message}")

    def ng(self, message: str) -> None:
        self.checks += 1
        self.failures.append(message)
        print(f"  NG   {message}")

    def judge(self, condition: bool, message: str, detail: str = "") -> bool:
        if condition:
            self.ok(message)
        else:
            self.ng(f"{message}{f'（{detail}）' if detail else ''}")
        return condition


def check_datasets(client: bigquery.Client, settings, report: Report) -> None:
    print("\n=== 1. データセット（要件定義書10-4） ===")
    expected = [settings.dataset_raw, settings.dataset_staging, settings.dataset_mart]
    found = {dataset.dataset_id for dataset in client.list_datasets()}
    for name in expected:
        if name not in found:
            report.ng(f"{name} がない（run_sql.py --all を流してください）")
            continue
        location = client.get_dataset(f"{settings.project_id}.{name}").location
        report.judge(
            location.lower() == EXPECTED_LOCATION,
            f"{name} が {EXPECTED_LOCATION} にある",
            f"今は {location}",
        )


def check_raw_tables(client: bigquery.Client, settings, report: Report) -> None:
    print("\n=== 2. raw の表（要件定義書7-3-1・10-4） ===")
    found = {
        table.table_id
        for table in client.list_tables(f"{settings.project_id}.{settings.dataset_raw}")
        if table.table_type == "TABLE" and table.table_id.startswith("raw_")
    }
    report.judge(
        found == set(EXPECTED_RAW_TABLES),
        f"raw の表が{len(EXPECTED_RAW_TABLES)}表そろっている（今は {len(found)} 表）",
        f"足りない: {sorted(set(EXPECTED_RAW_TABLES) - found)} / 余り: {sorted(found - set(EXPECTED_RAW_TABLES))}",
    )

    for name, cluster in EXPECTED_RAW_TABLES.items():
        if name not in found:
            continue
        table = client.get_table(f"{settings.project_id}.{settings.dataset_raw}.{name}")
        columns = {field.name for field in table.schema}

        missing = {"event_id", "org_id", "created_at", "created_by", "created_role"} - columns
        partitioned = (
            table.time_partitioning is not None
            and table.time_partitioning.field == "created_at"
        )
        if missing:
            report.ng(f"{name}：共通の列が足りない {sorted(missing)}")
        elif not partitioned:
            report.ng(f"{name}：created_at の日付で分割されていない")
        elif list(table.clustering_fields or []) != cluster:
            report.ng(f"{name}：クラスタが {table.clustering_fields}（期待は {cluster}）")
        else:
            report.ok(f"{name}：共通の列・分割・クラスタ（{', '.join(cluster)}）")


def check_staging_views(client: bigquery.Client, settings, report: Report) -> None:
    print("\n=== 3. staging のビュー（要件定義書7-3-2・7-3-3） ===")
    found = {
        table.table_id: table.table_type
        for table in client.list_tables(f"{settings.project_id}.{settings.dataset_staging}")
    }
    report.judge(
        set(found) == set(EXPECTED_STAGING_VIEWS),
        f"staging のビューが21個そろっている（今は {len(found)} 個）",
        f"足りない: {sorted(set(EXPECTED_STAGING_VIEWS) - set(found))} / 余り: {sorted(set(found) - set(EXPECTED_STAGING_VIEWS))}",
    )

    for name in EXPECTED_STAGING_VIEWS:
        if name not in found:
            continue
        if found[name] != "VIEW":
            report.ng(f"{name}：ビューではなく {found[name]} になっている")
            continue
        try:
            # 実際に読めるか（ビューの SQL の誤りをここで見つける）。0行なので費用はかからない。
            bq.query(
                f"SELECT * FROM `{settings.project_id}.{settings.dataset_staging}.{name}` LIMIT 0"
            )
        except bq.BigQueryUnavailable as exc:
            report.ng(f"{name}：読めない（{str(exc)[:160]}）")
        else:
            report.ok(f"{name}：ビューとして読める")


def check_mart_tables(client: bigquery.Client, settings, report: Report) -> None:
    print("\n=== 4. mart の表（要件定義書7-3-4） ===")
    found = {
        table.table_id
        for table in client.list_tables(f"{settings.project_id}.{settings.dataset_mart}")
    }
    report.judge(
        found == set(EXPECTED_MART_TABLES),
        f"mart の表が5表そろっている（今は {len(found)} 表）",
        f"足りない: {sorted(set(EXPECTED_MART_TABLES) - found)} / 余り: {sorted(found - set(EXPECTED_MART_TABLES))}",
    )

    for name in EXPECTED_MART_TABLES:
        if name not in found:
            continue
        table = client.get_table(f"{settings.project_id}.{settings.dataset_mart}.{name}")
        columns = {field.name for field in table.schema}
        missing = {"as_of", "org_id"} - columns
        report.judge(not missing, f"{name}：as_of と org_id を持っている", f"足りない {sorted(missing)}")


def check_insert_and_query(settings, report: Report) -> None:
    print("\n=== 5. insert と query（開発手順書 Step 2-3） ===")
    table_ref = f"`{settings.project_id}.{settings.dataset_raw}.{CHECK_TABLE}`"
    event_id = f"check-{uuid.uuid4()}"

    try:
        bq.execute(
            f"""
            CREATE OR REPLACE TABLE {table_ref} (
              event_id STRING NOT NULL,
              org_id STRING NOT NULL,
              created_at TIMESTAMP NOT NULL,
              created_by STRING NOT NULL,
              created_role STRING NOT NULL,
              survivor_id STRING,
              version INT64,
              note STRING,
              items ARRAY<STRING>,
              detail JSON,
              ai_used BOOL,
              deadline DATE
            )
            PARTITION BY DATE(created_at)
            CLUSTER BY org_id, survivor_id
            OPTIONS (
              description = "確認用の一時の表（check_bigquery.py が作って消す）",
              expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)
            )
            """,
            operation="check:create_tmp",
        )
        report.ok("一時の表を作れた")
    except bq.BigQueryUnavailable as exc:
        report.ng(f"一時の表を作れない（{str(exc)[:200]}）")
        return

    row = {
        "event_id": event_id,
        "org_id": "check-org",
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": "system",
        "created_role": "system",
        "survivor_id": "check-survivor",
        "version": 1,
        "note": "確認用の行。'); DROP TABLE raw_users; -- も値として扱えること",
        "items": ["location", "health"],
        "detail": {"quote": "足がむくんでいる", "visit_id": "v1"},
        "ai_used": True,
        "deadline": dt.date(2026, 11, 30),
        "contact": None,
    }

    try:
        bq.insert(CHECK_TABLE, {key: value for key, value in row.items() if key != "contact"})
        report.ok("insert で1行追記できた（パラメータ付きの INSERT）")
    except bq.BigQueryUnavailable as exc:
        report.ng(f"insert できない（{str(exc)[:200]}）")
        return

    try:
        rows = bq.query(
            f"SELECT event_id, note, items, detail, ai_used, deadline, version FROM {table_ref} WHERE event_id = @event_id",
            {"event_id": event_id},
        )
    except bq.BigQueryUnavailable as exc:
        report.ng(f"query できない（{str(exc)[:200]}）")
        return

    if report.judge(len(rows) == 1, "query で書いた行を読める", f"{len(rows)} 行返った") and rows:
        got = rows[0]
        report.judge(got["note"] == row["note"], "文字列がそのまま入っている（SQL に混ざらない）")
        report.judge(list(got["items"]) == row["items"], "配列（ARRAY<STRING>）が入っている")
        report.judge("足がむくんでいる" in str(got["detail"]), "JSON が入っている")
        report.judge(got["ai_used"] is True, "真偽値が入っている")
        report.judge(str(got["deadline"]) == "2026-11-30", "日付が入っている")
        report.judge(got["version"] == 1, "整数が入っている")

    try:
        bq.execute(f"DROP TABLE IF EXISTS {table_ref}", operation="check:drop_tmp")
        report.ok("一時の表を消せた")
    except bq.BigQueryUnavailable as exc:
        report.ng(f"一時の表を消せない（{str(exc)[:200]}）。1時間後に自動で消えます")


def main() -> int:
    settings = get_settings()
    print("=== 設定 ===")
    print(f"プロジェクト : {settings.project_id or '(未設定)'}")
    print(f"場所         : {settings.bq_location}")

    if not settings.project_id:
        print("\nNG: .env の GOOGLE_CLOUD_PROJECT を設定してください。")
        return 1

    try:
        client = bq.get_client()
    except bq.BigQueryUnavailable as exc:
        print(f"\nNG: BigQuery につながりません: {exc}")
        print("- 認証: gcloud auth application-default login を済ませたか")
        return 1

    report = Report()
    try:
        check_datasets(client, settings, report)
        check_raw_tables(client, settings, report)
        check_staging_views(client, settings, report)
        check_mart_tables(client, settings, report)
        check_insert_and_query(settings, report)
    except Exception as exc:  # 権限不足・APIが有効でないなど
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
