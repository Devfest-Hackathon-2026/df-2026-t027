"""F7（組織全体の状況）と mart のスケジュール登録のテスト（開発手順書 Step 11）。

Google Cloud にも Firebase にも Gemini にも接続しない。確かめるのは次の7つ。

- **使えるのは専門職だけ**（本人と一般の支援者は 403。要件定義書10-5の表「専」）
- トークンが無ければ 401
- 読むのは **mart の `mart_org_status` 1表だけ**で、**`org_id` で必ず絞る**
  （組織をまたいで人数を読める経路を作らない。要件定義書10-3）
- 人数は段階・居場所の種類・悪化の兆しの度合いごとに足し合わされ、**合計が元の行と合う**
- **0人の段階・度合いも出る**（見落としに気づけるように）／判断前は「まだ判断していない」
- **いつ時点の集計か（as_of）を返す**（mart は1時間ごとにしか変わらない）
- スケジュールの登録は **1時間ごと**で、宛先の表を指定せず、**料金の見積もりが無料枠に収まる**
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from backend import auth
from backend.config import get_settings
from backend.main import app
from backend.services import bq, dashboard, survivors, users

import schedule_mart

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
PRO_UID = "uid-pro-1"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
LATER = dt.datetime(2026, 11, 8, 13, 0, tzinfo=dt.timezone.utc)
BEARER = {"Authorization": "Bearer dummy-token"}


def mart_row(**overrides) -> dict:
    row = {
        "as_of": NOW,
        "stage": "temporary",
        "location_type": "temporary_built",
        "risk_level": "high",
        "survivor_count": 0,
        "with_open_consultation": 0,
        "with_unapplied_support": 0,
        "with_pending_proposal": 0,
    }
    row.update(overrides)
    return row


ROWS = [
    mart_row(
        stage="evacuation",
        location_type="shelter",
        risk_level="low",
        survivor_count=3,
        with_open_consultation=1,
        with_unapplied_support=2,
    ),
    mart_row(
        stage="temporary",
        location_type="temporary_built",
        risk_level="high",
        survivor_count=2,
        with_open_consultation=2,
        with_unapplied_support=1,
        with_pending_proposal=1,
    ),
    mart_row(
        as_of=LATER,
        stage="temporary",
        location_type="shelter",
        risk_level=None,
        survivor_count=1,
    ),
]


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setenv("FIREBASE_API_KEY", "test-api-key")
    monkeypatch.setenv("FIREBASE_AUTH_DOMAIN", f"{PROJECT}.firebaseapp.com")
    monkeypatch.setenv("FIREBASE_APP_ID", "1:0:web:test")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def no_bigquery(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("テストから BigQuery を呼んではいけません")

    for name in ("query", "query_one", "insert"):
        monkeypatch.setattr(bq, name, fail, raising=False)


def sign_in(monkeypatch, *, uid: str, role: str | None, org_id: str | None = ORG) -> None:
    monkeypatch.setattr(
        auth.id_token,
        "verify_firebase_token",
        lambda *args, **kwargs: {
            "iss": f"{auth.ISSUER_PREFIX}{PROJECT}",
            "aud": PROJECT,
            "sub": uid,
            "firebase": {"sign_in_provider": "phone"},
        },
    )
    row = None
    if role is not None:
        row = {
            "user_id": uid,
            "org_id": org_id,
            "role": role,
            "profession": "相談員" if role == users.ROLE_PROFESSIONAL else None,
            "display_name": "テスト利用者",
            "login_method": "phone",
            "linked_survivor_id": None,
            "org_name": "輪島市",
        }
    monkeypatch.setattr(users, "fetch_user", lambda user_id: row)


def use_rows(monkeypatch, rows: list[dict]) -> list[tuple[str, dict]]:
    """`bq.query` を差し替え、呼ばれた SQL とパラメータを覚える。"""
    calls: list[tuple[str, dict]] = []

    def fake_query(sql, params=None):
        calls.append((sql, params or {}))
        return list(rows)

    monkeypatch.setattr(bq, "query", fake_query)
    return calls


# --- 立場の制限（要件定義書10-5） ---


def test_token_required(client):
    assert client.get("/api/dashboard").status_code == 401


@pytest.mark.parametrize("role", ["survivor", "volunteer"])
def test_only_professional_can_read(client, monkeypatch, role):
    sign_in(monkeypatch, uid="uid-x", role=role)
    monkeypatch.setattr(bq, "query", lambda *a, **k: pytest.fail("読みを走らせてはいけない"))
    assert client.get("/api/dashboard", headers=BEARER).status_code == 403


def test_unregistered_user_is_denied(client, monkeypatch):
    sign_in(monkeypatch, uid="uid-new", role=None)
    assert client.get("/api/dashboard", headers=BEARER).status_code == 403


# --- 読み方（mart 1表・org_id で絞る） ---


def test_reads_only_mart_org_status_for_own_org(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    calls = use_rows(monkeypatch, ROWS)

    response = client.get("/api/dashboard", headers=BEARER)

    assert response.status_code == 200
    assert len(calls) == 1, "読みは1回だけにする"
    sql, params = calls[0]
    assert f"{PROJECT}.gdgoc_mart.mart_org_status" in sql
    assert "WHERE org_id = @org_id" in sql
    assert params == {"org_id": ORG}
    assert "SELECT *" not in sql
    # 画面のために stg_ や int_ を数え直さない（人数の正本は mart だけ）
    assert "gdgoc_staging" not in sql


def test_unavailable_when_mart_cannot_be_read(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)

    def unavailable(*args, **kwargs):
        raise bq.BigQueryUnavailable("表がありません")

    monkeypatch.setattr(bq, "query", unavailable)
    assert client.get("/api/dashboard", headers=BEARER).status_code == 503


# --- 集計の中身 ---


def test_totals_match_rows():
    payload = dashboard.build_payload(ROWS)
    assert payload["total"] == {
        "survivor_count": 6,
        "with_open_consultation": 3,
        "with_unapplied_support": 3,
        "with_pending_proposal": 1,
    }


def test_as_of_is_the_newest():
    assert dashboard.build_payload(ROWS)["as_of"] == LATER
    assert dashboard.build_payload([])["as_of"] is None


def test_stage_keeps_every_stage_in_order():
    by_stage = dashboard.build_payload(ROWS)["by_stage"]
    assert [item["value"] for item in by_stage] == list(survivors.STAGES)
    counts = {item["value"]: item["survivor_count"] for item in by_stage}
    assert counts == {"evacuation": 3, "temporary": 3, "rebuilding": 0}
    assert by_stage[0]["label"] == survivors.STAGES["evacuation"]


def test_location_type_shows_only_recorded_ones_in_order():
    by_location = dashboard.build_payload(ROWS)["by_location_type"]
    assert [item["value"] for item in by_location] == ["shelter", "temporary_built"]
    assert [item["survivor_count"] for item in by_location] == [4, 2]
    assert by_location[0]["label"] == "避難所"


def test_risk_level_keeps_zero_and_names_the_unjudged():
    by_risk = dashboard.build_payload(ROWS)["by_risk_level"]
    assert [item["value"] for item in by_risk] == ["high", "medium", "low", "unknown"]
    counts = {item["value"]: item["survivor_count"] for item in by_risk}
    assert counts == {"high": 2, "medium": 0, "low": 3, "unknown": 1}
    unknown = next(item for item in by_risk if item["value"] == "unknown")
    assert unknown["label"] == "まだ判断していない"


def test_unknown_values_are_kept_not_dropped():
    rows = [mart_row(stage="unexpected", survivor_count=5)]
    payload = dashboard.build_payload(rows)
    assert payload["total"]["survivor_count"] == 5
    labels = {item["value"]: item["label"] for item in payload["by_stage"]}
    assert labels["unexpected"] == "unexpected"


def test_note_tells_the_numbers_are_hourly():
    note = dashboard.build_payload([])["note"]
    assert "1時間" in note


# --- スケジュールクエリの登録（Step 11-2） ---


def test_schedule_is_hourly_without_destination_table():
    body = schedule_mart.build_config("SELECT 1")
    assert body["schedule"] == "every 1 hours"
    assert body["dataSourceId"] == "scheduled_query"
    assert body["params"] == {"query": "SELECT 1"}
    # 複数文の SQL は書き込む先を SQL 自身が持つ
    assert "destination_dataset_id" not in body
    assert "destination_table_name_template" not in body


def test_render_query_replaces_placeholders():
    query = schedule_mart.render_query(project=PROJECT, location="asia-northeast1")
    assert "{{PROJECT}}" not in query
    assert f"{PROJECT}.gdgoc_mart.mart_org_status" in query


def test_cost_estimate_stays_in_the_free_tier():
    estimate = schedule_mart.estimate_cost(10.0)
    assert estimate["runs_per_month"] == 720
    assert estimate["monthly_tib"] < 1.0
    assert estimate["usd_per_month"] == 0.0


def test_cost_estimate_warns_when_over_the_free_tier():
    estimate = schedule_mart.estimate_cost(2048.0)
    assert estimate["over_free_tib"] > 0
    assert estimate["usd_per_month"] > 0


def test_service_account_defaults_to_gdgoc_app():
    assert schedule_mart.service_account(PROJECT, None) == (
        f"gdgoc-app@{PROJECT}.iam.gserviceaccount.com"
    )
    assert schedule_mart.service_account(PROJECT, "other@x.iam.gserviceaccount.com") == (
        "other@x.iam.gserviceaccount.com"
    )
