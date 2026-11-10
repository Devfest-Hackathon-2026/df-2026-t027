"""F2（訪問・見守りの記録）と F3（健康・介護の情報）の API のテスト（開発手順書 Step 6）。

Google Cloud にも Firebase にも Gemini にも接続しない。確かめるのは次の5つ。

- **整える処理は保存しない**（要件定義書10-5の「AI」の列）。`bq.insert` を呼べば落ちる
- **Gemini を呼べないときも、手入力で保存できる**（要件定義書F2の最後）
- **一般の支援者が書いた更新の案は反映しない**（確認待ちで残り、専門職が `/apply` で反映する）
- **健康・介護の情報を更新できるのは専門職と本人だけ**（要件定義書F3）
- **本人には訪問・見守りの記録の本文を返さない**（要件定義書F4・10-3）
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from backend import auth
from backend.config import get_settings
from backend.main import app
from backend.services import (
    assignments,
    bq,
    consultations,
    gemini,
    handovers,
    health,
    link_codes,
    supports,
    survivors,
    users,
    visit_ai,
    visits,
)

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
OTHER_ORG = "org_suzu"
SURVIVOR = "sv_0001"
VISIT = "vs_0001"
PRO_UID = "uid-pro-1"
VOL_UID = "uid-vol-1"
SELF_UID = "uid-self-1"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
BEARER = {"Authorization": "Bearer dummy-token"}

RAW_TEXT = "きのうから熱っぽい。ごはんは朝だけ。体育館が寒いと言っている。"


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

    for module in (bq, survivors.bq, assignments.bq, link_codes.bq, health.bq, visits.bq):
        monkeypatch.setattr(module, "query", fail, raising=False)
        monkeypatch.setattr(module, "query_one", fail, raising=False)
        monkeypatch.setattr(module, "insert", fail, raising=False)


@pytest.fixture(autouse=True)
def no_gemini(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("テストから Gemini を呼んではいけません")

    monkeypatch.setattr(gemini, "generate_structured", fail)


def sign_in(monkeypatch, *, uid: str, role: str | None, org_id: str | None = ORG) -> None:
    """IDトークンの確認と立場の読みを差し替える（ログイン済みの状態にする）。"""
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
            "profession": "保健師" if role == users.ROLE_PROFESSIONAL else None,
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
        "age": 78,
        "household_size": 2,
        "contact": None,
        "stage": "evacuation",
        "location_type": "shelter",
        "location_detail": "市民体育館",
        "self_user_id": None,
        "registered_via": "professional",
        "registered_at": NOW,
        "updated_at": NOW,
        "risk_level": None,
        "risk_evidence": None,
        "risk_next_action": None,
        "risk_source": None,
        "risk_assessed_at": None,
        "last_visited_at": None,
        "days_since_last_visit": None,
        "visits_30d": 0,
        "pending_proposals": 0,
        "open_consultations": 0,
        "max_open_urgency": None,
        "last_consulted_at": None,
        "suggested_count": 0,
        "unapplied_count": 0,
        "nearest_unapplied_deadline": None,
        "days_to_nearest_deadline": None,
        "assignee_user_ids": [],
        "assignee_count": 0,
        "never_visited": True,
    }
    row.update(overrides)
    return row


def set_survivor(monkeypatch, row: dict | None) -> None:
    """記録と、詳細で読むものを差し替える。"""
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: row)
    monkeypatch.setattr(survivors, "location_history", lambda **kwargs: [])
    monkeypatch.setattr(assignments, "list_for_survivor", lambda **kwargs: [])
    monkeypatch.setattr(health, "fetch_latest", lambda **kwargs: None)
    monkeypatch.setattr(visits, "list_visits", lambda **kwargs: [])
    # 詳細は F8 の相談も読む（Step 8 で足した。本人と専門職だけ）
    monkeypatch.setattr(consultations, "list_for_survivor", lambda **kwargs: [])
    # 引き継ぎシート（F3。Step 9 で詳細の読みに足した）
    monkeypatch.setattr(handovers, "list_for_survivor", lambda **kwargs: [])
    # 支援と申請の状況（F6。Step 10 で詳細の読みに足した）
    monkeypatch.setattr(supports, "list_for_survivor", lambda **kwargs: [])


def structured_payload(**overrides) -> dict:
    payload = {
        "items": visits.clean_items({"condition": "熱っぽい", "meal": "朝だけ"}),
        "location_proposal": None,
        "health_proposal": None,
    }
    payload.update(overrides)
    return payload


def set_structure(monkeypatch, payload: dict | None = None, *, fail: bool = False) -> list[dict]:
    """Gemini で整える処理を差し替える（渡された引数を記録して返す）。"""
    calls: list[dict] = []

    def run(**kwargs):
        calls.append(kwargs)
        if fail:
            raise gemini.GeminiUnavailable("テスト")
        return payload or structured_payload()

    monkeypatch.setattr(visit_ai, "structure", run)
    return calls


def catch_saves(monkeypatch) -> dict:
    """保存・反映の呼び出しを記録する（BigQuery は呼ばない）。"""
    saved: dict = {"visits": [], "applies": [], "health": [], "locations": []}

    def save_visit(row):
        saved["visits"].append(row)
        return row

    def record_apply(**kwargs):
        saved["applies"].append(kwargs)
        return kwargs

    def save_health(**kwargs):
        saved["health"].append(kwargs)
        return {**kwargs, "version": 1, "created_at": NOW, "created_role": kwargs["created_role"]}

    def change_location(**kwargs):
        saved["locations"].append(kwargs)
        return {**survivor_row(), "version": 2}

    monkeypatch.setattr(visits, "save_visit", save_visit)
    monkeypatch.setattr(visits, "record_apply", record_apply)
    monkeypatch.setattr(health, "save", save_health)
    monkeypatch.setattr(survivors, "change_location", change_location)
    return saved


def visit_row(**overrides) -> dict:
    row = {
        "visit_id": VISIT,
        "version": 1,
        "visited_at": NOW,
        "raw_text": RAW_TEXT,
        **visits.clean_items({"condition": "熱っぽい"}),
        "location_proposal": None,
        "health_proposal": None,
        "proposal_status": visits.STATUS_PENDING,
        "ai_used": True,
        "recorded_at": NOW,
        "recorded_by": VOL_UID,
        "recorded_role": users.ROLE_VOLUNTEER,
        "applied_at": None,
        "apply_result": None,
        "is_pending": True,
    }
    row.update(overrides)
    return row


LOCATION_PROPOSAL = {
    "change_suggested": True,
    "location_type": "temporary_built",
    "location_type_label": "仮設住宅（建設型）",
    "location_detail": "仮設第1団地",
    "reason": "仮設に移ったと話している",
}

HEALTH_PROPOSAL = {
    "change_suggested": True,
    "fields": health.clean_fields({"medications": ["ワーファリン"]}),
    "reason": "薬が増えたと話している",
}


def save_body(**overrides) -> dict:
    body = {
        "raw_text": RAW_TEXT,
        "items": visits.clean_items({"condition": "熱っぽい"}),
        "ai_used": True,
    }
    body.update(overrides)
    return body


def structure_url(survivor_id: str = SURVIVOR) -> str:
    return f"/api/survivors/{survivor_id}/visits/structure"


def visits_url(survivor_id: str = SURVIVOR) -> str:
    return f"/api/survivors/{survivor_id}/visits"


def apply_url(survivor_id: str = SURVIVOR, visit_id: str = VISIT) -> str:
    return f"/api/survivors/{survivor_id}/visits/{visit_id}/apply"


def health_url(survivor_id: str = SURVIVOR) -> str:
    return f"/api/survivors/{survivor_id}/health"


# --- 立場（要件定義書10-5の「使える人」） ---


def test_ログインしていないと整える処理は401(client):
    assert client.post(structure_url(), json={"raw_text": RAW_TEXT}).status_code == 401


@pytest.mark.parametrize(
    "url_builder, method",
    [(structure_url, "post"), (visits_url, "post")],
)
def test_本人は訪問の記録を書けない(client, monkeypatch, url_builder, method):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))

    res = getattr(client, method)(url_builder(), json=save_body(), headers=BEARER)

    assert res.status_code == 403


def test_一般の支援者は更新の案を反映できない(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))

    res = client.post(apply_url(), json={"items": ["location"]}, headers=BEARER)

    assert res.status_code == 403


def test_一般の支援者は健康の情報を更新できない(client, monkeypatch):
    # 更新は F2 の更新の案を通す（要件定義書F3）
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))

    res = client.put(health_url(), json={"allergies": "そば"}, headers=BEARER)

    assert res.status_code == 403


def test_担当でない一般の支援者は404(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[]))
    set_structure(monkeypatch)

    res = client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER)

    assert res.status_code == 404


def test_別の組織の記録には書けない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL, org_id=OTHER_ORG)
    set_survivor(monkeypatch, survivor_row())
    catch_saves(monkeypatch)

    res = client.post(visits_url(), json=save_body(), headers=BEARER)

    assert res.status_code == 404


# --- 整える処理（保存しない。要件定義書F2・10-5） ---


def test_整える処理は保存しない(client, monkeypatch):
    # bq.insert は no_bigquery で落ちるようにしてある（呼べば 500 になる）
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_structure(monkeypatch)

    res = client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER)

    assert res.status_code == 200
    assert res.json()["ai_used"] is True


def test_整えた案は9つの項目を返す(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_structure(monkeypatch)

    body = client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER).json()

    assert set(body["items"]) == set(visits.VISIT_FIELDS)
    assert body["items"]["condition"] == "熱っぽい"
    # 文章に書かれていない項目は「記載なし」（要件定義書F2・10-6）
    assert body["items"]["sleep"] == visits.NOT_RECORDED


def test_整える処理には文章と今の情報を渡す(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    calls = set_structure(monkeypatch)

    client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER)

    assert calls[0]["raw_text"] == RAW_TEXT
    assert calls[0]["survivor"]["location_type"] == "shelter"
    assert "health_row" in calls[0]


def test_文章が空なら400(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_structure(monkeypatch)

    res = client.post(structure_url(), json={"raw_text": "   "}, headers=BEARER)

    assert res.status_code == 400


def test_Geminiを呼べなくても手入力の欄を出せる(client, monkeypatch):
    """要件定義書F2の最後。503 にすると訪問先で記録を残せなくなる。"""
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_structure(monkeypatch, fail=True)

    res = client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER)

    assert res.status_code == 200
    body = res.json()
    assert body["ai_used"] is False
    assert body["notice"]
    assert set(body["items"]) == set(visits.VISIT_FIELDS)
    assert all(value == visits.NOT_RECORDED for value in body["items"].values())


def test_反映できるのは専門職だけだと画面に伝える(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))
    set_structure(monkeypatch)

    body = client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER).json()

    assert body["can_apply"] is False


# --- 保存（要件定義書F2） ---


def test_専門職は記録を保存できる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    saved = catch_saves(monkeypatch)

    res = client.post(visits_url(), json=save_body(), headers=BEARER)

    assert res.status_code == 201
    assert len(saved["visits"]) == 1
    assert saved["visits"][0]["created_role"] == users.ROLE_PROFESSIONAL
    assert saved["visits"][0]["condition"] == "熱っぽい"


def test_一般の支援者も記録を保存できる(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))
    saved = catch_saves(monkeypatch)

    res = client.post(visits_url(), json=save_body(), headers=BEARER)

    assert res.status_code == 201
    assert saved["visits"][0]["created_role"] == users.ROLE_VOLUNTEER


def test_どの項目も空なら400(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    catch_saves(monkeypatch)

    res = client.post(visits_url(), json=save_body(items={}), headers=BEARER)

    assert res.status_code == 400


def test_手入力の記録も保存できる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    saved = catch_saves(monkeypatch)

    res = client.post(
        visits_url(), json=save_body(raw_text="", ai_used=False), headers=BEARER
    )

    assert res.status_code == 201
    assert saved["visits"][0]["ai_used"] is False
    assert saved["visits"][0]["raw_text"] is None


def test_未来の日時は400(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    catch_saves(monkeypatch)
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).isoformat()

    res = client.post(visits_url(), json=save_body(visited_at=future), headers=BEARER)

    assert res.status_code == 400


# --- 更新の案（要件定義書F2） ---


def test_一般の支援者の案は確認待ちになる(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))
    saved = catch_saves(monkeypatch)

    res = client.post(
        visits_url(),
        json=save_body(location_proposal=LOCATION_PROPOSAL, apply_location=True),
        headers=BEARER,
    )

    assert res.status_code == 201
    body = res.json()
    # 要求に apply_location=true と書かれていても、一般の支援者では反映しない（要件定義書10-3）
    assert body["applied"] == []
    assert body["visit"]["is_pending"] is True
    assert saved["locations"] == []
    assert saved["applies"] == []
    assert saved["visits"][0]["proposal_status"] == visits.STATUS_PENDING


def test_専門職はその場で居場所の案を反映できる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    saved = catch_saves(monkeypatch)

    body = client.post(
        visits_url(),
        json=save_body(location_proposal=LOCATION_PROPOSAL, apply_location=True),
        headers=BEARER,
    ).json()

    assert body["applied"] == ["location"]
    assert saved["locations"][0]["location_type"] == "temporary_built"
    # 段階は今のものを引き継ぐ（勝手に変えない）
    assert saved["locations"][0]["stage"] == "evacuation"
    # 反映したことを raw_update_applies に残す（要件定義書7-3-2）
    assert saved["applies"][0]["result"] == visits.RESULT_APPLIED
    assert saved["applies"][0]["applied_items"] == ["location"]


def test_専門職はその場で健康の案を反映できる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    saved = catch_saves(monkeypatch)

    body = client.post(
        visits_url(),
        json=save_body(health_proposal=HEALTH_PROPOSAL, apply_health=True),
        headers=BEARER,
    ).json()

    assert body["applied"] == ["health"]
    # 訪問の記録から反映したと分かるように残す（要件定義書F3）
    assert saved["health"][0]["source"] == health.SOURCE_VISIT_PROPOSAL
    assert saved["health"][0]["fields"]["medications"] == ["ワーファリン"]


def test_整えた案をそのまま送り返して保存できる(client, monkeypatch):
    """`/structure` が返す形と、`/visits` が受け取る形がずれていないこと。

    画面は受け取った案をそのまま送り返す。形がずれると**案が黙って落ちる**
    （健康の案を平らな形で受け取るようにしていて、実際に落ちた）。
    """
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_structure(
        monkeypatch,
        structured_payload(
            location_proposal=visits.normalize_location_proposal(
                {"change_suggested": True, "location_type": "temporary_built"}
            ),
            health_proposal=visits.normalize_health_proposal(
                {"change_suggested": True, "medications": ["ワーファリン"]}
            ),
        ),
    )
    structured = client.post(
        structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER
    ).json()
    saved = catch_saves(monkeypatch)

    body = client.post(
        visits_url(),
        json={
            "raw_text": RAW_TEXT,
            "items": structured["items"],
            "location_proposal": structured["location_proposal"],
            "health_proposal": structured["health_proposal"],
            "ai_used": True,
            "apply_location": True,
            "apply_health": True,
        },
        headers=BEARER,
    ).json()

    assert body["applied"] == ["location", "health"]
    assert saved["visits"][0]["location_proposal"]["location_type"] == "temporary_built"
    assert saved["visits"][0]["health_proposal"]["fields"]["medications"] == ["ワーファリン"]


def test_反映しないときは案が確認待ちで残る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    saved = catch_saves(monkeypatch)

    body = client.post(
        visits_url(),
        json=save_body(location_proposal=LOCATION_PROPOSAL, apply_location=False),
        headers=BEARER,
    ).json()

    assert body["applied"] == []
    assert body["visit"]["is_pending"] is True
    assert saved["applies"] == []


def test_画面に出せない居場所の案は落とす(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    saved = catch_saves(monkeypatch)

    body = client.post(
        visits_url(),
        json=save_body(
            location_proposal={"change_suggested": True, "location_type": "tent"},
            apply_location=True,
        ),
        headers=BEARER,
    ).json()

    assert body["applied"] == []
    assert saved["visits"][0]["location_proposal"] is None
    assert saved["visits"][0]["proposal_status"] == visits.STATUS_NONE


# --- 確認待ちの案を反映する（POST /visits/{vid}/apply） ---


def set_visit(monkeypatch, row: dict | None) -> None:
    monkeypatch.setattr(visits, "fetch_visit", lambda **kwargs: row)


def test_確認待ちの案を専門職が反映できる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_visit(monkeypatch, visit_row(location_proposal=LOCATION_PROPOSAL))
    saved = catch_saves(monkeypatch)

    res = client.post(apply_url(), json={"items": ["location"]}, headers=BEARER)

    assert res.status_code == 200
    assert res.json()["applied"] == ["location"]
    assert saved["locations"][0]["location_type"] == "temporary_built"
    assert saved["applies"][0]["visit_id"] == VISIT


def test_反映しないと決めたことも記録する(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_visit(monkeypatch, visit_row(location_proposal=LOCATION_PROPOSAL))
    saved = catch_saves(monkeypatch)

    res = client.post(
        apply_url(), json={"items": [], "result": "rejected"}, headers=BEARER
    )

    assert res.status_code == 200
    assert saved["locations"] == []
    assert saved["applies"][0]["result"] == visits.RESULT_REJECTED


def test_確かめ済みの案は反映できない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_visit(
        monkeypatch,
        visit_row(location_proposal=LOCATION_PROPOSAL, is_pending=False, apply_result="applied"),
    )
    catch_saves(monkeypatch)

    res = client.post(apply_url(), json={"items": ["location"]}, headers=BEARER)

    assert res.status_code == 400


def test_無い記録の案は404(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_visit(monkeypatch, None)
    catch_saves(monkeypatch)

    res = client.post(apply_url(), json={"items": ["location"]}, headers=BEARER)

    assert res.status_code == 404


def test_案が無い内容を反映しようとしたら400(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_visit(monkeypatch, visit_row(location_proposal=LOCATION_PROPOSAL))
    catch_saves(monkeypatch)

    res = client.post(apply_url(), json={"items": ["health"]}, headers=BEARER)

    assert res.status_code == 400


def test_決まっていない結果は400(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_visit(monkeypatch, visit_row(location_proposal=LOCATION_PROPOSAL))
    catch_saves(monkeypatch)

    res = client.post(apply_url(), json={"items": [], "result": "maybe"}, headers=BEARER)

    assert res.status_code == 400


# --- F3 健康・介護の情報（PUT /health） ---


def test_専門職は健康の情報を保存できる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    saved = catch_saves(monkeypatch)

    res = client.put(
        health_url(),
        json={"chronic_diseases": "高血圧\n糖尿病", "care_level": "要介護2"},
        headers=BEARER,
    )

    assert res.status_code == 200
    assert saved["health"][0]["source"] == health.SOURCE_PROFESSIONAL
    assert saved["health"][0]["fields"]["chronic_diseases"] == ["高血圧", "糖尿病"]


def test_本人が書いた情報は本人の申告として残る(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    saved = catch_saves(monkeypatch)

    res = client.put(health_url(), json={"allergies": "そば"}, headers=BEARER)

    assert res.status_code == 200
    assert saved["health"][0]["source"] == health.SOURCE_SELF
    assert res.json()["health"]["source_label"] == "ご本人の申告"


def test_決まっていない要介護度は400(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    catch_saves(monkeypatch)

    res = client.put(health_url(), json={"care_level": "要介護6"}, headers=BEARER)

    assert res.status_code == 400


def test_他人の記録の健康の情報は書けない(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id="uid-other"))
    catch_saves(monkeypatch)

    res = client.put(health_url(), json={"allergies": "そば"}, headers=BEARER)

    assert res.status_code == 404


# --- 詳細に返す範囲（要件定義書10-3・F4） ---


def test_本人には訪問の記録を返さない(client, monkeypatch):
    """悪化の兆しの度合いと記録の本文は、本人向けのデータから外す（要件定義書F4）。"""
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))

    def fail(**kwargs):
        raise AssertionError("本人の詳細で訪問の記録を読んではいけません")

    monkeypatch.setattr(visits, "list_visits", fail)

    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()

    assert "visits" not in body["survivor"]
    assert RAW_TEXT not in res_text(body)


def res_text(body: dict) -> str:
    import json

    return json.dumps(body, ensure_ascii=False, default=str)


def test_本人にも健康の情報は返す(client, monkeypatch):
    # 本人も更新できる項目なので、本人の画面には出す（要件定義書F3）
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    monkeypatch.setattr(
        health,
        "fetch_latest",
        lambda **kwargs: {
            "survivor_id": SURVIVOR,
            "org_id": ORG,
            "version": 1,
            "allergies": "そば",
            "source": health.SOURCE_SELF,
            "updated_at": NOW,
            "updated_role": users.ROLE_SURVIVOR,
        },
    )

    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()

    assert body["survivor"]["health"]["fields"]["allergies"] == "そば"


def test_支援者には訪問の記録を返す(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(visits, "list_visits", lambda **kwargs: [visit_row()])

    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()

    rows = body["survivor"]["visits"]
    assert len(rows) == 1
    assert rows[0]["items"]["condition"] == "熱っぽい"
    assert rows[0]["is_pending"] is True
