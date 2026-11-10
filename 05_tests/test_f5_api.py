"""F5（受け入れ先の施設と移る先の決定）の API のテスト（開発手順書 Step 9-1・9-2）。

Google Cloud にも Firebase にも Gemini にも接続しない。確かめるのは次の7つ。

- **使えるのは専門職だけ**（要件定義書F5・10-5。本人と一般の支援者は 403）
- 施設の更新は**上書きせず版を足す**（名前と対応できる医療が消えない）
- **空きがない施設と、必要な医療に対応できない施設は Gemini に渡さない**（要件定義書F5）
- **渡した一覧にない施設は候補にならない**（要件定義書10-6）
- 最終更新から24時間以上たった施設は、**そのことが応答に入る**（要件定義書F5）
- 移る先の決定は、**その組織に実在する施設しか保存しない**（要求の値を信じない。10-3）
- Gemini を呼べなくても、**専門職が自分で選んで決められる**（移る先が決まらなくならない）
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from backend import auth
from backend.config import get_settings
from backend.main import app
from backend.services import (
    assessments,
    assignments,
    bq,
    consultations,
    facilities,
    gemini,
    handovers,
    health,
    link_codes,
    supports,
    survivors,
    users,
    visits,
)

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
OTHER_ORG = "org_suzu"
SURVIVOR = "sv_0001"
FACILITY = "fc_0001"
PRO_UID = "uid-pro-1"
VOL_UID = "uid-vol-1"
SELF_UID = "uid-self-1"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
BEARER = {"Authorization": "Bearer dummy-token"}


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

    for module in (
        bq,
        survivors.bq,
        assignments.bq,
        link_codes.bq,
        health.bq,
        visits.bq,
        assessments.bq,
        consultations.bq,
        supports.bq,
        facilities.bq,
        handovers.bq,
    ):
        monkeypatch.setattr(module, "query", fail, raising=False)
        monkeypatch.setattr(module, "query_one", fail, raising=False)
        monkeypatch.setattr(module, "insert", fail, raising=False)


@pytest.fixture(autouse=True)
def no_gemini(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("テストから Gemini を呼んではいけません")

    monkeypatch.setattr(gemini, "generate_structured", fail)


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
            "linked_survivor_id": SURVIVOR if role == users.ROLE_SURVIVOR else None,
            "org_name": "輪島市",
        }
    monkeypatch.setattr(users, "fetch_user", lambda user_id: row)
    monkeypatch.setattr(users, "list_by_role", lambda **kwargs: [])
    monkeypatch.setattr(users, "names_by_ids", lambda **kwargs: [])


def survivor_row(**overrides) -> dict:
    row = {
        "survivor_id": SURVIVOR,
        "org_id": ORG,
        "version": 1,
        "full_name": "佐藤 太郎",
        "age": 82,
        "household_size": 2,
        "contact": None,
        "stage": "evacuation",
        "location_type": "shelter",
        "location_detail": "市民体育館",
        "self_user_id": SELF_UID,
        "registered_via": "professional",
        "registered_at": NOW,
        "updated_at": NOW,
        "risk_level": "medium",
        "risk_evidence": {"items": []},
        "risk_next_action": None,
        "risk_source": assessments.SOURCE_AI,
        "risk_assessed_at": NOW,
        "assignee_user_ids": [VOL_UID],
        "assignee_count": 1,
    }
    row.update(overrides)
    return row


def facility_row(facility_id: str = FACILITY, **overrides) -> dict:
    row = {
        "facility_id": facility_id,
        "org_id": ORG,
        "version": 1,
        "facility_name": "輪島中央 福祉避難所",
        "facility_type": "welfare_shelter",
        "address": "輪島市中央町1-1",
        "vacancies": 3,
        "capabilities": ["dialysis", "wheelchair"],
        "water_outage": False,
        "power_outage": False,
        "updated_at": NOW,
        "updated_by": PRO_UID,
        "is_stale": False,
    }
    row.update(overrides)
    return row


def health_row(**overrides) -> dict:
    row = {
        "version": 2,
        "source": "professional",
        "chronic_diseases": ["高血圧"],
        "medications": ["降圧剤"],
        "allergies": None,
        "primary_clinic": "輪島中央クリニック",
        "medical_needs": ["透析"],
        "care_level": "要介護2",
        "assistance_mobility": "車いすを使う",
        "assistance_meal": None,
        "assistance_toilet": "一部介助",
        "dementia_notes": None,
        "family_contact": "長女（金沢市）",
        "updated_at": NOW,
        "updated_role": "professional",
    }
    row.update(overrides)
    return row


def set_survivor(monkeypatch, row: dict | None) -> None:
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: row)
    monkeypatch.setattr(health, "fetch_latest", lambda **kwargs: health_row())


def catch_writes(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(facilities, "save", lambda row: written.append(row) or row)
    monkeypatch.setattr(facilities, "save_placement", lambda row: written.append(row) or row)
    return written


def new_body(**overrides) -> dict:
    body = {
        "facility_name": "輪島中央 福祉避難所",
        "facility_type": "welfare_shelter",
        "address": "輪島市中央町1-1",
        "vacancies": 3,
        "capabilities": ["dialysis", "wheelchair"],
        "water_outage": False,
        "power_outage": False,
    }
    body.update(overrides)
    return body


def candidates_url(survivor_id: str = SURVIVOR) -> str:
    return f"/api/survivors/{survivor_id}/facility-candidates"


def placements_url(survivor_id: str = SURVIVOR) -> str:
    return f"/api/survivors/{survivor_id}/placements"


# --- ログインしていない要求（要件定義書10-3） ---


def test_トークンなしの施設のAPIは401(client) -> None:
    assert client.get("/api/facilities").status_code == 401
    assert client.post("/api/facilities", json=new_body()).status_code == 401
    assert client.patch(f"/api/facilities/{FACILITY}", json={"vacancies": 0}).status_code == 401
    assert client.post(candidates_url(), json={}).status_code == 401
    assert client.post(placements_url(), json={}).status_code == 401


# --- 使える立場（要件定義書F5・10-5） ---


@pytest.mark.parametrize("role", [users.ROLE_SURVIVOR, users.ROLE_VOLUNTEER])
def test_専門職以外は施設を扱えない(client, monkeypatch, role) -> None:
    uid = SELF_UID if role == users.ROLE_SURVIVOR else VOL_UID
    sign_in(monkeypatch, uid=uid, role=role)
    assert client.get("/api/facilities", headers=BEARER).status_code == 403
    assert client.post("/api/facilities", json=new_body(), headers=BEARER).status_code == 403
    assert (
        client.patch(
            f"/api/facilities/{FACILITY}", json={"vacancies": 0}, headers=BEARER
        ).status_code
        == 403
    )
    assert client.post(candidates_url(), json={}, headers=BEARER).status_code == 403
    assert (
        client.post(
            placements_url(),
            json={"chosen_facility_id": FACILITY, "chosen_reason": "理由"},
            headers=BEARER,
        ).status_code
        == 403
    )


# --- 施設の一覧と登録 ---


def test_施設の一覧が組織の施設を返す(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    captured = {}

    def listed(**kwargs):
        captured.update(kwargs)
        return [facility_row(), facility_row("fc_0002", is_stale=True)]

    monkeypatch.setattr(facilities, "list_facilities", listed)
    res = client.get("/api/facilities", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    # 組織は stg_users から決める（要求に書かれた値は使わない）
    assert captured["org_id"] == ORG
    assert [item["facility_id"] for item in body["facilities"]] == [FACILITY, "fc_0002"]
    assert body["facilities"][0]["facility_type_label"] == "福祉避難所"
    # 古い施設は、そのことが分かる形で返る（要件定義書F5）
    assert body["facilities"][1]["is_stale"] is True
    assert body["facilities"][1]["stale_notice"]
    assert body["stale_count"] == 1


def test_施設を登録すると版1で追記される(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    written = catch_writes(monkeypatch)
    res = client.post("/api/facilities", json=new_body(), headers=BEARER)
    assert res.status_code == 201
    assert len(written) == 1
    assert written[0]["version"] == 1
    assert written[0]["org_id"] == ORG
    assert written[0]["created_by"] == PRO_UID
    assert written[0]["capabilities"] == ["dialysis", "wheelchair"]
    assert res.json()["facility"]["facility_id"] == written[0]["facility_id"]


def test_選択肢にない対応できる医療は400(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    catch_writes(monkeypatch)
    res = client.post(
        "/api/facilities", json=new_body(capabilities=["透析できます"]), headers=BEARER
    )
    assert res.status_code == 400


def test_空きの数が未記入なら400(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    catch_writes(monkeypatch)
    res = client.post("/api/facilities", json=new_body(vacancies=None), headers=BEARER)
    assert res.status_code == 400


# --- 施設の更新（版を足す） ---


def test_空きを更新すると版が増えて名前と対応できる医療が残る(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(facilities, "fetch_facility", lambda **kwargs: facility_row(version=2))
    written = catch_writes(monkeypatch)
    res = client.patch(
        f"/api/facilities/{FACILITY}",
        json={"vacancies": 0, "water_outage": True},
        headers=BEARER,
    )
    assert res.status_code == 200
    assert written[0]["version"] == 3
    assert written[0]["facility_name"] == "輪島中央 福祉避難所"
    assert written[0]["capabilities"] == ["dialysis", "wheelchair"]
    assert written[0]["vacancies"] == 0
    assert written[0]["water_outage"] is True
    assert written[0]["power_outage"] is False


def test_ほかの組織の施設は更新できず404(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    # 組織で絞って読むので、ほかの組織の施設は見つからない
    monkeypatch.setattr(facilities, "fetch_facility", lambda **kwargs: None)
    catch_writes(monkeypatch)
    res = client.patch(f"/api/facilities/{FACILITY}", json={"vacancies": 1}, headers=BEARER)
    assert res.status_code == 404


# --- 施設の候補（要件定義書F5・10-6） ---


def set_facilities(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(facilities, "list_facilities", lambda **kwargs: list(rows))


def candidate_ids(body: dict) -> list[str]:
    return [item["facility_id"] for item in body["candidates"]]


def test_空きがない施設と対応できない施設は候補に出さない(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_facilities(
        monkeypatch,
        [
            facility_row(),  # 空きあり・透析に対応できる → 候補
            facility_row("fc_0002", vacancies=0),  # 空きがない → 外す
            facility_row("fc_0003", capabilities=["wheelchair"]),  # 透析できない → 外す
        ],
    )
    res = client.post(candidates_url(), json={}, headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert candidate_ids(body) == [FACILITY]
    reasons = {item["facility_id"]: item["reason"] for item in body["excluded"]}
    assert reasons["fc_0002"] == facilities.EXCLUDED_NO_VACANCY
    assert reasons["fc_0003"] == facilities.EXCLUDED_NO_CAPABILITY


def test_候補はAIを使わずルールの理由を付ける(client, monkeypatch) -> None:
    # Gemini を呼ぶと no_gemini が失敗にする（画面の改修の計画.md 7章）
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_facilities(monkeypatch, [facility_row()])
    body = client.post(candidates_url(), json={}, headers=BEARER).json()
    assert body["ai_used"] is False
    assert body["notice"] is None
    reason = body["candidates"][0]["reason"]
    # 透析（必要な医療）と車いす（移動の介助の欄から読み取る）に対応
    assert reason == "空き3。対応できるもの：透析・車いすで生活できる。断水・停電なし。情報は24時間以内の更新。"


def test_候補にない施設も選べるように渡した施設を返す(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_facilities(monkeypatch, [facility_row(), facility_row("fc_0004")])
    body = client.post(candidates_url(), json={}, headers=BEARER).json()
    # 要件定義書F5「候補にない施設も選べる」。画面が選べる施設を返す
    assert [item["facility_id"] for item in body["selectable"]] == [FACILITY, "fc_0004"]


def test_判断できない必要な医療はそのことを返す(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: survivor_row())
    monkeypatch.setattr(
        health,
        "fetch_latest",
        lambda **kwargs: health_row(medical_needs=["週3回の通院の付き添い"]),
    )
    set_facilities(monkeypatch, [facility_row(capabilities=[])])
    body = client.post(candidates_url(), json={}, headers=BEARER).json()
    assert body["unmatched_needs"] == ["週3回の通院の付き添い"]
    assert body["unmatched_notice"] == facilities.UNMATCHED_NOTICE
    # 分からないものでは外さない（施設は候補に残り、理由で確かめるよう伝える）
    assert candidate_ids(body) == [FACILITY]
    assert "施設に確かめる必要がある" in body["candidates"][0]["reason"]


def test_渡せる施設が無ければ案内を出す(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_facilities(monkeypatch, [facility_row(vacancies=0)])
    body = client.post(candidates_url(), json={}, headers=BEARER).json()
    assert body["candidates"] == []
    assert body["notice"] == facilities.NO_ELIGIBLE_NOTICE


def test_ほかの組織の記録の候補は404(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(org_id=OTHER_ORG))
    res = client.post(candidates_url(), json={}, headers=BEARER)
    assert res.status_code == 404


# --- 移る先の決定（要件定義書F5） ---


def test_候補から選ぶと候補から選んだと残る(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_facilities(monkeypatch, [facility_row()])
    written = catch_writes(monkeypatch)
    res = client.post(
        placements_url(),
        json={
            "chosen_facility_id": FACILITY,
            "chosen_reason": "空きがあり、透析に対応できる",
            "candidates": [{"facility_id": FACILITY, "reason": "透析に対応できる"}],
        },
        headers=BEARER,
    )
    assert res.status_code == 201
    assert written[0]["chosen_from_candidates"] is True
    assert written[0]["survivor_id"] == SURVIVOR
    assert written[0]["org_id"] == ORG
    body = res.json()
    assert body["placement"]["chosen_facility_name"] == "輪島中央 福祉避難所"
    # 施設を決めたら、居場所の変更と引き継ぎシートへ進める（要件定義書F5）
    assert body["next_steps"]["location_detail"] == "輪島中央 福祉避難所"


def test_その組織に無い施設は決められず400(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_facilities(monkeypatch, [facility_row()])
    catch_writes(monkeypatch)
    res = client.post(
        placements_url(),
        json={"chosen_facility_id": "fc_9999", "chosen_reason": "理由はある"},
        headers=BEARER,
    )
    assert res.status_code == 400


def test_候補に無いIDを送っても保存されない(client, monkeypatch) -> None:
    # 要求に書かれた候補をそのまま保存しない（要件定義書10-3）
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_facilities(monkeypatch, [facility_row()])
    written = catch_writes(monkeypatch)
    client.post(
        placements_url(),
        json={
            "chosen_facility_id": FACILITY,
            "chosen_reason": "理由はある",
            "candidates": [
                {"facility_id": FACILITY, "reason": "空きがある"},
                {"facility_id": "fc_9999", "reason": "存在しない施設"},
            ],
        },
        headers=BEARER,
    )
    saved = written[0]["candidates"]["items"]
    assert [item["facility_id"] for item in saved] == [FACILITY]


def test_理由を書かないと400(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_facilities(monkeypatch, [facility_row()])
    catch_writes(monkeypatch)
    res = client.post(
        placements_url(),
        json={"chosen_facility_id": FACILITY, "chosen_reason": "  "},
        headers=BEARER,
    )
    assert res.status_code == 400


# --- 選択肢と文言の正本（画面に書き写さない。Step 8 と同じ形） ---


def test_施設の選択肢はAPIが返す(client) -> None:
    # 値の正本は services/facilities.py。画面に書き写すとずれる
    choices = client.get("/api/public-config").json()["choices"]
    assert [item["value"] for item in choices["facility_types"]] == list(
        facilities.FACILITY_TYPES
    )
    assert [item["value"] for item in choices["facility_capabilities"]] == list(
        facilities.CAPABILITIES
    )
    assert choices["facility_stale_notice"] == facilities.STALE_NOTICE


def test_画面に施設の知らせの文言を書き写していない() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "02_app" / "frontend" / "f5.js"
    ).read_text(encoding="utf-8")
    # 古い施設の知らせは、APIが返すものだけを出す
    assert "電話で確かめて" not in source
