"""services/bq.py のテスト（開発手順書 Step 2-3、要件定義書10-3・10-4）。

Google Cloud には接続しない。SQL と パラメータの組み立てだけを確かめる。
"""

from __future__ import annotations

import datetime as dt
import threading
import time

import pytest

from backend.services import bq

PROJECT = "test-project"


# --- 表と列の名前の検査（文字列をつないで SQL を作らないため。要件定義書10-3） ---


@pytest.mark.parametrize("name", ["raw_visits", "org_id", "a1_B2", "_private", "X"])
def test_validate_identifier_accepts_word_characters(name: str) -> None:
    assert bq.validate_identifier(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "raw_visits; DROP TABLE raw_users",
        "raw-visits",
        "raw visits",
        "raw.visits",
        "raw_visits`",
        "被災者",
        "",
        "*",
        "org_id)",
    ],
)
def test_validate_identifier_rejects_everything_else(name: str) -> None:
    with pytest.raises(bq.InvalidIdentifier):
        bq.validate_identifier(name)


def test_validate_identifier_rejects_non_string() -> None:
    with pytest.raises(bq.InvalidIdentifier):
        bq.validate_identifier(None)  # type: ignore[arg-type]


def test_qualified_table_builds_backquoted_reference() -> None:
    assert (
        bq.qualified_table(PROJECT, "gdgoc_raw", "raw_visits")
        == "`test-project.gdgoc_raw.raw_visits`"
    )


def test_qualified_table_rejects_injection_in_table_name() -> None:
    with pytest.raises(bq.InvalidIdentifier):
        bq.qualified_table(PROJECT, "gdgoc_raw", "raw_visits` WHERE TRUE--")


# --- INSERT の組み立て ---


def test_build_insert_uses_parameters_for_every_value() -> None:
    row = {"event_id": "e1", "org_id": "o1", "age": 80, "ai_used": True}
    sql, params = bq.build_insert(PROJECT, "gdgoc_raw", "raw_survivors", row)

    assert sql.startswith("INSERT INTO `test-project.gdgoc_raw.raw_survivors`")
    assert "(event_id, org_id, age, ai_used)" in sql
    assert "VALUES (@p_event_id, @p_org_id, @p_age, @p_ai_used)" in sql
    assert {p.name for p in params} == {"p_event_id", "p_org_id", "p_age", "p_ai_used"}


def test_build_insert_never_puts_values_into_the_sql_text() -> None:
    row = {"raw_text": "'); DROP TABLE raw_users; --"}
    sql, params = bq.build_insert(PROJECT, "gdgoc_raw", "raw_visits", row)

    assert "DROP TABLE" not in sql
    assert params[0].value == "'); DROP TABLE raw_users; --"


def test_build_insert_rejects_a_column_name_that_is_not_a_word() -> None:
    with pytest.raises(bq.InvalidIdentifier):
        bq.build_insert(PROJECT, "gdgoc_raw", "raw_visits", {"raw_text) VALUES ('x'); --": "x"})


def test_build_insert_rejects_an_empty_row() -> None:
    with pytest.raises(ValueError):
        bq.build_insert(PROJECT, "gdgoc_raw", "raw_visits", {})


def test_build_insert_writes_null_for_none_without_a_parameter() -> None:
    sql, params = bq.build_insert(PROJECT, "gdgoc_raw", "raw_survivors", {"org_id": "o1", "contact": None})

    assert "VALUES (@p_org_id, NULL)" in sql
    assert [p.name for p in params] == ["p_org_id"]


def test_build_insert_wraps_json_columns_in_parse_json() -> None:
    row = {"survivor_id": "s1", "evidence": {"quotes": ["足がむくむ"], "visit_id": "v1"}}
    sql, params = bq.build_insert(PROJECT, "gdgoc_raw", "raw_assessments", row)

    assert "VALUES (@p_survivor_id, PARSE_JSON(@p_evidence))" in sql
    evidence = next(p for p in params if p.name == "p_evidence")
    assert evidence.type_ == "STRING"
    assert "足がむくむ" in evidence.value  # 日本語をエスケープしない


def test_build_insert_sends_a_list_of_strings_as_an_array() -> None:
    _, params = bq.build_insert(
        PROJECT, "gdgoc_raw", "raw_health_versions", {"medications": ["降圧剤", "インスリン"]}
    )

    assert params[0].array_type == "STRING"
    assert params[0].values == ["降圧剤", "インスリン"]


def test_build_insert_sends_a_list_of_objects_as_json() -> None:
    sql, params = bq.build_insert(
        PROJECT, "gdgoc_raw", "raw_placements", {"candidates": [{"facility_id": "f1"}]}
    )

    assert "PARSE_JSON(@p_candidates)" in sql
    assert params[0].type_ == "STRING"


def test_build_insert_maps_python_types_to_bigquery_types() -> None:
    row = {
        "org_id": "o1",
        "version": 2,
        "ai_used": False,
        "created_at": dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc),
        "deadline": dt.date(2026, 11, 30),
        "rate": 0.5,
    }
    _, params = bq.build_insert(PROJECT, "gdgoc_raw", "raw_support_events", row)
    types = {p.name: p.type_ for p in params}

    assert types == {
        "p_org_id": "STRING",
        "p_version": "INT64",
        "p_ai_used": "BOOL",
        "p_created_at": "TIMESTAMP",
        "p_deadline": "DATE",
        "p_rate": "FLOAT64",
    }


def test_build_insert_rejects_a_value_it_cannot_map() -> None:
    with pytest.raises(TypeError):
        bq.build_insert(PROJECT, "gdgoc_raw", "raw_visits", {"raw_text": object()})


# --- 読み取りのパラメータ ---


def test_build_parameters_handles_scalars_and_arrays() -> None:
    params = bq.build_parameters({"org_id": "o1", "limit": 50, "ids": ["a", "b"]})
    by_name = {p.name: p for p in params}

    assert by_name["org_id"].type_ == "STRING"
    assert by_name["limit"].type_ == "INT64"
    assert by_name["ids"].array_type == "STRING"


def test_build_parameters_rejects_a_name_that_is_not_a_word() -> None:
    with pytest.raises(bq.InvalidIdentifier):
        bq.build_parameters({"org_id; --": "o1"})


# --- UPDATE・DELETE・ストリーミング挿入を使わない（要件定義書10-4） ---


def test_bq_module_has_no_update_delete_or_streaming_insert() -> None:
    assert bq.__file__ is not None
    with open(bq.__file__, encoding="utf-8") as handle:
        source = handle.read().upper()

    assert "INSERT_ROWS" not in source  # ストリーミング挿入は無料枠の外
    assert "UPDATE `" not in source
    assert "DELETE FROM" not in source
    assert "MERGE INTO" not in source


# --- クライアントを作るところ（F1 で同時に読むようになったため） ---


def test_クライアントは同時に呼んでも1つしか作らない(monkeypatch):
    """`get_client` を複数のスレッドから同時に呼んでも、クライアントは1つだけ作る。

    F1（Step 5）で、関係のない読みを `asyncio.to_thread` で同時に走らせるようにした。
    鍵をかけていないと、両方のスレッドが「まだ無い」と見て**2つ作る**（認証も2回走る）。
    """
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gdgoc-test-project")
    from backend.config import get_settings

    get_settings.cache_clear()

    created = []

    class FakeClient:
        def __init__(self, project=None, location=None):
            # 作るのに時間がかかる状況を作る（認証に時間がかかるため実際そうなる）
            time.sleep(0.05)
            created.append(self)

    monkeypatch.setattr(bq.bigquery, "Client", FakeClient)
    monkeypatch.setattr(bq, "_client", None)

    start = threading.Barrier(4, timeout=5)
    got = []

    def worker():
        start.wait()
        got.append(bq.get_client())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    get_settings.cache_clear()

    assert len(created) == 1
    assert len(got) == 4
    assert all(item is got[0] for item in got)
