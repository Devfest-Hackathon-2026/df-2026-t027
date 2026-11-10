"""03_sql/ の SQL が要件定義書7-3・10-4のとおりかを確かめる（Google Cloud には接続しない）。

- raw は16表（7-3-1）、stg_ は16ビュー（7-3-2）、int_ は5ビュー（7-3-3）、mart は5表（7-3-4）。
  raw と stg_ には、使いやすさの改修の計画.md 8章の周りの地域の状況と、10章の家族のつながりを
  1つずつ足した（18表・18ビュー）
- raw は発生日時の日付で分割し、組織のIDでクラスタ化する。被災者に関わる表は被災者のIDもクラスタに入れる（10-4）
- UPDATE・DELETE は使わない（10-4）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import check_bigquery as check

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = REPO_ROOT / "03_sql"

FILES = {
    "datasets": SQL_DIR / "01_datasets.sql",
    "raw": SQL_DIR / "02_raw_tables.sql",
    "stg": SQL_DIR / "03_stg_views.sql",
    "int": SQL_DIR / "04_int_views.sql",
    "mart": SQL_DIR / "05_mart.sql",
}

# 7-3-1 の16表と、使いやすさの改修の計画.md 8章・10章の2表。被災者に関わる表（survivor_id を持つ表）は True
RAW_TABLES: dict[str, bool] = {
    "raw_organizations": False,
    "raw_users": False,
    "raw_survivors": True,
    "raw_link_codes": True,
    "raw_assignment_events": True,
    "raw_visits": True,
    "raw_update_applies": True,
    "raw_health_versions": True,
    "raw_handovers": True,
    "raw_assessments": True,
    "raw_facilities": False,
    "raw_placements": True,
    "raw_programs": False,
    "raw_support_events": True,
    "raw_consultations": True,
    "raw_consultation_events": True,
    # 周りの地域の状況（使いやすさの改修の計画.md 8章。7-3-1には無い追加の表）
    "raw_disaster_status": False,
    "raw_family_links": True,
}

STG_VIEWS = [
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
    # 周りの地域の状況（使いやすさの改修の計画.md 8章。7-3-2には無い追加のビュー）
    "stg_disaster_status",
    "stg_family_links",
]

INT_VIEWS = [
    "int_survivor_visits",
    "int_survivor_consultations",
    "int_survivor_supports",
    "int_survivor_assignees",
    "int_survivor_list",
]

MART_TABLES = [
    "mart_org_status",
    "mart_visit_coverage",
    "mart_consultation_response_daily",
    "mart_support_uptake",
    "mart_activity_daily",
]


def read(key: str) -> str:
    return FILES[key].read_text(encoding="utf-8")


def all_sql() -> str:
    return "\n".join(read(key) for key in FILES)


def test_every_sql_file_exists() -> None:
    missing = [str(path.relative_to(REPO_ROOT)) for path in FILES.values() if not path.exists()]
    assert missing == []


# --- 数と名前が7-3と同じか ---


def test_raw_has_the_seventeen_tables_of_7_3_1() -> None:
    found = re.findall(r"CREATE TABLE IF NOT EXISTS `\{\{PROJECT\}\}\.gdgoc_raw\.(\w+)`", read("raw"))
    assert sorted(found) == sorted(RAW_TABLES)
    # 7-3-1 の16表と、8章・10章の2表
    assert len(found) == 18


def test_staging_has_the_seventeen_stg_views_of_7_3_2() -> None:
    found = re.findall(r"CREATE OR REPLACE VIEW `\{\{PROJECT\}\}\.gdgoc_staging\.(\w+)`", read("stg"))
    assert sorted(found) == sorted(STG_VIEWS)
    # 7-3-2 の16ビューと、8章・10章の2ビュー
    assert len(found) == 18


def test_staging_has_the_five_int_views_of_7_3_3() -> None:
    found = re.findall(r"CREATE OR REPLACE VIEW `\{\{PROJECT\}\}\.gdgoc_staging\.(\w+)`", read("int"))
    assert sorted(found) == sorted(INT_VIEWS)
    assert len(found) == 5


def test_mart_has_the_five_tables_of_7_3_4() -> None:
    found = re.findall(r"CREATE OR REPLACE TABLE `\{\{PROJECT\}\}\.gdgoc_mart\.(\w+)`", read("mart"))
    assert sorted(found) == sorted(MART_TABLES)
    assert len(found) == 5


def test_check_bigquery_expects_the_same_objects_as_the_sql() -> None:
    """確認スクリプトの期待値と SQL がずれないようにする。"""
    assert sorted(check.EXPECTED_RAW_TABLES) == sorted(RAW_TABLES)
    assert sorted(check.EXPECTED_STAGING_VIEWS) == sorted(STG_VIEWS + INT_VIEWS)
    assert sorted(check.EXPECTED_MART_TABLES) == sorted(MART_TABLES)


# --- raw の作り方（10-4） ---


def split_raw_statements() -> dict[str, str]:
    statements = {}
    for block in read("raw").split(";"):
        match = re.search(r"CREATE TABLE IF NOT EXISTS `\{\{PROJECT\}\}\.gdgoc_raw\.(\w+)`", block)
        if match:
            statements[match.group(1)] = block
    return statements


@pytest.mark.parametrize("table", sorted(RAW_TABLES))
def test_every_raw_table_has_the_common_columns_of_7_3_1(table: str) -> None:
    body = split_raw_statements()[table]
    for column in ("event_id STRING", "org_id STRING", "created_at TIMESTAMP", "created_by STRING", "created_role STRING"):
        assert column in body, f"{table} に {column} がない"


@pytest.mark.parametrize("table", sorted(RAW_TABLES))
def test_every_raw_table_is_partitioned_by_the_date_of_created_at(table: str) -> None:
    assert "PARTITION BY DATE(created_at)" in split_raw_statements()[table]


@pytest.mark.parametrize("table, has_survivor", sorted(RAW_TABLES.items()))
def test_raw_tables_are_clustered_by_org_and_survivor(table: str, has_survivor: bool) -> None:
    body = split_raw_statements()[table]
    cluster = re.search(r"CLUSTER BY ([\w, ]+)", body)
    assert cluster, f"{table} に CLUSTER BY がない"
    keys = [key.strip() for key in cluster.group(1).split(",")]

    assert keys[0] == "org_id"
    if has_survivor:
        assert "survivor_id STRING" in body, f"{table} に survivor_id がない"
        assert keys[1] == "survivor_id"
    else:
        assert "survivor_id" not in keys


@pytest.mark.parametrize(
    "table",
    ["raw_survivors", "raw_visits", "raw_health_versions", "raw_facilities", "raw_programs", "raw_disaster_status"],
)
def test_tables_with_versions_have_a_version_number(table: str) -> None:
    assert "version INT64" in split_raw_statements()[table]


# --- 何度流しても同じ状態になるか（Step 2 の完了の確認） ---


def test_every_statement_is_repeatable() -> None:
    sql = all_sql()
    creates = re.findall(r"^\s*CREATE[^`]*", sql, flags=re.MULTILINE)
    assert creates, "CREATE 文が見つからない"
    for create in creates:
        head = " ".join(create.split())
        assert "IF NOT EXISTS" in head or "OR REPLACE" in head, head


def test_sql_never_updates_or_deletes() -> None:
    sql = all_sql().upper()
    for forbidden in ("UPDATE ", "DELETE ", "MERGE ", "TRUNCATE ", "DROP TABLE", "DROP VIEW"):
        assert forbidden not in sql, f"{forbidden} を使っている（要件定義書10-4）"


def test_sql_uses_only_the_two_placeholders_that_run_sql_replaces() -> None:
    found = set(re.findall(r"\{\{(\w+)\}\}", all_sql()))
    assert found == {"PROJECT", "LOCATION"}


def test_only_the_dataset_file_sets_the_location() -> None:
    """場所の指定はデータセットだけ（表とビューは所属するデータセットの場所に従う）。"""
    assert "{{LOCATION}}" in read("datasets")
    for key in ("raw", "stg", "int", "mart"):
        assert "{{LOCATION}}" not in read(key)
