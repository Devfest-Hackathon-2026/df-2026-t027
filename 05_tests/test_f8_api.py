"""F8（被災者からの相談）の API のテスト（開発手順書 Step 8）。

Google Cloud にも Firebase にも Gemini にも接続しない。確かめるのは次の6つ。

- **整える処理は保存しない**（本人が確かめてから送る。要件定義書F8・10-5）
- **送れるのは本人だけ**で、**返事と状況を変えられるのは専門職だけ**（10-5の「使える人」）
- **本人の応答に急ぎの度合いと根拠が入らない**（要件定義書10-3）
- **送った後、応答の前に F4 の判断が走り、相談が渡される**（10-5の注）
- 命に関わるときの案内（119番）が、**送る前の応答に必ず入る**（要件定義書F8）
- 組織の相談の一覧が**未対応から・急ぎの順**で、ほかの組織の相談は読めない（10-3）
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import auth
from backend.config import get_settings
from backend.main import app
from backend.services import (
    assessments,
    assignments,
    bq,
    consultation_ai,
    consultations,
    gemini,
    handovers,
    health,
    link_codes,
    risk_ai,
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
CONSULTATION = "cs_0001"
PRO_UID = "uid-pro-1"
VOL_UID = "uid-vol-1"
SELF_UID = "uid-self-1"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
BEARER = {"Authorization": "Bearer dummy-token"}

RAW_TEXT = "きのうから胸が苦しい。血圧の薬が切れてしまって、どこでもらえるか分からない。"


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
    ):
        monkeypatch.setattr(module, "query", fail, raising=False)
        monkeypatch.setattr(module, "query_one", fail, raising=False)
        monkeypatch.setattr(module, "insert", fail, raising=False)


@pytest.fixture(autouse=True)
def no_gemini(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("テストから Gemini を呼んではいけません")

    monkeypatch.setattr(gemini, "generate_structured", fail)


@pytest.fixture(autouse=True)
def no_background_assessment(monkeypatch):
    """保存の後に走る F4 の判断は、既定で呼ばれたことだけを数える（差し替え忘れに気づけないため）。"""
    calls: list[dict] = []
    monkeypatch.setattr(risk_ai, "assess_and_save", lambda **kwargs: calls.append(kwargs))
    return calls


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
        "age": 78,
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
        "last_visited_at": NOW,
        "days_since_last_visit": 3,
        "visits_30d": 1,
        "pending_proposals": 0,
        "open_consultations": 0,
        "max_open_urgency": None,
        "last_consulted_at": None,
        "suggested_count": 0,
        "unapplied_count": 0,
        "nearest_unapplied_deadline": None,
        "days_to_nearest_deadline": None,
        "assignee_user_ids": [VOL_UID],
        "assignee_count": 1,
        "never_visited": False,
    }
    row.update(overrides)
    return row


def consultation_row(**overrides) -> dict:
    row = {
        "consultation_id": CONSULTATION,
        "org_id": ORG,
        "survivor_id": SURVIVOR,
        "raw_text": RAW_TEXT,
        "category": "health",
        "structured": {"items": consultations.clean_items({"condition": "胸が苦しい"})},
        "emergency_flag": True,
        "urgency": "high",
        "urgency_evidence": "「胸が苦しい」と書かれている",
        "ai_used": True,
        "consulted_at": NOW,
        "consulted_by": SELF_UID,
        "consulted_role": users.ROLE_SURVIVOR,
        "status": consultations.STATUS_OPEN,
        "first_replied_at": None,
        "reply_count": 0,
        "first_reply_minutes": None,
        "last_reply_text": None,
        "last_next_action": None,
        "last_replied_at": None,
        "is_unanswered": True,
    }
    row.update(overrides)
    return row


def set_survivor(monkeypatch, row: dict | None, *, consultation_rows=None) -> None:
    """記録と、詳細で読むものを差し替える。"""
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: row)
    monkeypatch.setattr(survivors, "location_history", lambda **kwargs: [])
    monkeypatch.setattr(assignments, "list_for_survivor", lambda **kwargs: [])
    monkeypatch.setattr(health, "fetch_latest", lambda **kwargs: None)
    monkeypatch.setattr(visits, "list_visits", lambda **kwargs: [])
    monkeypatch.setattr(
        consultations,
        "list_for_survivor",
        lambda **kwargs: list(consultation_rows if consultation_rows is not None else []),
    )
    # 引き継ぎシート（F3。Step 9 で詳細の読みに足した）
    monkeypatch.setattr(handovers, "list_for_survivor", lambda **kwargs: [])
    # 支援と申請の状況（F6。Step 10 で詳細の読みに足した）
    monkeypatch.setattr(supports, "list_for_survivor", lambda **kwargs: [])


def catch_writes(monkeypatch) -> list[dict]:
    """相談の書き込みを受け止める（BigQuery には出さない）。"""
    written: list[dict] = []
    monkeypatch.setattr(consultations, "save", lambda row: written.append(row) or row)
    monkeypatch.setattr(consultations, "save_event", lambda row: written.append(row) or row)
    return written


def set_structure(monkeypatch, payload=None, *, fail: bool = False) -> None:
    def run(**kwargs):
        if fail:
            raise gemini.GeminiUnavailable("テスト")
        return payload or {
            "category": "health",
            "category_label": "体調",
            "items": consultations.clean_items({"condition": "胸が苦しい"}),
            "emergency_flag": True,
            "emergency_reason": "「胸が苦しい」と書かれている",
        }

    monkeypatch.setattr(consultation_ai, "structure", run)


def set_triage(monkeypatch, result=None) -> list[dict]:
    """送ったときの1回の問い合わせ（整える＋急ぎの度合い。画面の改修の計画.md 6章）を差し替える。"""
    calls: list[dict] = []

    def run(**kwargs):
        calls.append(kwargs)
        return result or {
            "category": "health",
            "items": consultations.clean_items({"condition": "胸が苦しい"}),
            "emergency_flag": True,
            "emergency_reason": "「胸が苦しい」と書かれている",
            "urgency": "high",
            "urgency_evidence": "「胸が苦しい」と書かれている",
            "ai_used": True,
        }

    monkeypatch.setattr(consultation_ai, "triage", run)
    return calls


def structure_url(survivor_id: str = SURVIVOR) -> str:
    return f"/api/survivors/{survivor_id}/consultations/structure"


def send_url(survivor_id: str = SURVIVOR) -> str:
    return f"/api/survivors/{survivor_id}/consultations"


def reply_url(survivor_id: str = SURVIVOR, consultation_id: str = CONSULTATION) -> str:
    return f"/api/survivors/{survivor_id}/consultations/{consultation_id}/replies"


def status_url(survivor_id: str = SURVIVOR, consultation_id: str = CONSULTATION) -> str:
    return f"/api/survivors/{survivor_id}/consultations/{consultation_id}"


def send_body(**overrides) -> dict:
    # ご本人は書いた文章をそのまま送る（種類は選ばなくてもよい。画面の改修の計画.md 6章）
    body = {"raw_text": RAW_TEXT, "category": None}
    body.update(overrides)
    return body


# --- ログインしていない要求（要件定義書10-3） ---


def test_トークンなしの相談のAPIは401(client) -> None:
    assert client.post(structure_url(), json={"raw_text": RAW_TEXT}).status_code == 401
    assert client.post(send_url(), json=send_body()).status_code == 401
    assert client.get("/api/consultations").status_code == 401
    assert client.post(reply_url(), json={"reply_text": "伺います"}).status_code == 401
    assert client.patch(status_url(), json={"status": "closed"}).status_code == 401


# --- 整える処理（要件定義書F8・10-5） ---


def test_整える処理は保存しない(client, monkeypatch) -> None:
    # 保存は、本人が確かめたあとの別の要求で行う（`consultations.save` を呼んだら落ちる）
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    set_structure(monkeypatch)
    monkeypatch.setattr(
        consultations, "save", lambda row: pytest.fail("整える処理で保存してはいけません")
    )
    monkeypatch.setattr(
        consultations, "save_event", lambda row: pytest.fail("整える処理で保存してはいけません")
    )

    res = client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["items"]["condition"] == "胸が苦しい"
    assert body["category"] == "health"
    assert body["ai_used"] is True


def test_整える処理の応答に119番の案内が入る(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    set_structure(monkeypatch)
    body = client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER).json()
    assert "119" in body["emergency_guide"]
    # AIが「急を要する」と見たときは、本人の画面にすぐ出す文言も返す
    assert body["emergency_flag"] is True
    assert body["emergency_notice"]


def test_急を要しないときは強い案内を出さない(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    set_structure(
        monkeypatch,
        {
            "category": "life",
            "category_label": "生活",
            "items": consultations.clean_items({"concerns": "物干しが足りない"}),
            "emergency_flag": False,
            "emergency_reason": None,
        },
    )
    body = client.post(structure_url(), json={"raw_text": "物干しが足りない"}, headers=BEARER).json()
    assert body["emergency_flag"] is False
    assert body["emergency_notice"] is None
    # 送る前の案内そのものは、急を要しないときでも必ず出す（要件定義書F8）
    assert "119" in body["emergency_guide"]


def test_AIを呼べなくても手で入れて送れる(client, monkeypatch) -> None:
    # 503 にすると画面で「やり直し」に見え、相談そのものを送れなくなる
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    set_structure(monkeypatch, fail=True)
    res = client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["ai_used"] is False
    assert body["notice"]
    assert set(body["items"].values()) == {visits.NOT_RECORDED}
    assert "119" in body["emergency_guide"]


def test_相談の文章が空なら400(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    res = client.post(structure_url(), json={"raw_text": "  "}, headers=BEARER)
    assert res.status_code == 400


def test_整える処理は本人だけが使える(client, monkeypatch) -> None:
    for uid, role in ((PRO_UID, users.ROLE_PROFESSIONAL), (VOL_UID, users.ROLE_VOLUNTEER)):
        sign_in(monkeypatch, uid=uid, role=role)
        set_survivor(monkeypatch, survivor_row())
        res = client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER)
        assert res.status_code == 403, role


def test_ほかの人の記録には相談を送れない(client, monkeypatch) -> None:
    # 本人は自分の記録だけ（`can_view` が通らないものは 404。有無も分からないようにする）
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id="uid-other"))
    res = client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER)
    assert res.status_code == 404


# --- 相談を送る（要件定義書F8・10-5） ---


def test_相談を送ると整えた案と急ぎの度合いが付いて保存される(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    written = catch_writes(monkeypatch)
    set_triage(monkeypatch)

    res = client.post(send_url(), json=send_body(), headers=BEARER)
    assert res.status_code == 201
    assert len(written) == 1
    saved = written[0]
    assert saved["raw_text"] == RAW_TEXT
    assert saved["category"] == "health"
    assert saved["urgency"] == "high"
    assert saved["urgency_evidence"] == "「胸が苦しい」と書かれている"
    assert saved["structured"]["items"]["condition"] == "胸が苦しい"
    assert saved["emergency_flag"] is True
    assert saved["ai_used"] is True
    assert saved["created_role"] == users.ROLE_SURVIVOR


def test_送った相談の応答に急ぎの度合いを入れない(client, monkeypatch) -> None:
    # 本人には支援者向けの判断を見せない（要件定義書10-3・F4と同じ考え方）
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    catch_writes(monkeypatch)
    set_triage(monkeypatch)
    body = client.post(send_url(), json=send_body(), headers=BEARER).json()
    assert "urgency" not in body["consultation"]
    assert "urgency_evidence" not in body["consultation"]
    assert body["consultation"]["status"] == consultations.STATUS_OPEN


def test_急を要するときは送ったあとの応答に案内を入れる(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    catch_writes(monkeypatch)
    set_triage(monkeypatch)
    body = client.post(send_url(), json=send_body(), headers=BEARER).json()
    assert body["emergency_notice"] == consultations.EMERGENCY_NOTICE
    assert body["emergency_guide"] == consultations.EMERGENCY_GUIDE


def test_送るときは本人の文章と選んだ種類だけを渡す(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    catch_writes(monkeypatch)
    calls = set_triage(monkeypatch)
    # 古い画面が9つの項目を送ってきても使わない（文章から整え直す）
    body = send_body(category="housing", items={"condition": "古い画面の項目"}, ai_used=False)
    client.post(send_url(), json=body, headers=BEARER)
    assert len(calls) == 1
    assert calls[0]["raw_text"] == RAW_TEXT
    assert calls[0]["chosen_category"] == "housing"
    assert "items" not in calls[0]


def test_種類を選ばなければNoneで渡す(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    catch_writes(monkeypatch)
    calls = set_triage(monkeypatch)
    client.post(send_url(), json=send_body(category=""), headers=BEARER)
    assert calls[0]["chosen_category"] is None


def test_AIを呼べなくても文章だけで保存する(client, monkeypatch) -> None:
    # triage を差し替えず、Gemini を呼べない状態にする（本物の fallback を通す）
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    written = catch_writes(monkeypatch)

    def unavailable(**kwargs):
        raise gemini.GeminiUnavailable("テスト")

    monkeypatch.setattr(gemini, "generate_structured", unavailable)
    res = client.post(send_url(), json=send_body(), headers=BEARER)
    assert res.status_code == 201
    saved = written[0]
    assert saved["raw_text"] == RAW_TEXT
    assert saved["ai_used"] is False
    assert saved["urgency"] == consultations.FALLBACK_URGENCY
    assert set(saved["structured"]["items"].values()) == {visits.NOT_RECORDED}
    assert res.json()["emergency_notice"] is None


def test_文章が空なら送れず400(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    written = catch_writes(monkeypatch)
    calls = set_triage(monkeypatch)
    res = client.post(send_url(), json=send_body(raw_text="  "), headers=BEARER)
    assert res.status_code == 400
    assert written == [] and calls == []


def test_相談を送れるのは本人だけ(client, monkeypatch) -> None:
    for uid, role in ((PRO_UID, users.ROLE_PROFESSIONAL), (VOL_UID, users.ROLE_VOLUNTEER)):
        sign_in(monkeypatch, uid=uid, role=role)
        set_survivor(monkeypatch, survivor_row())
        res = client.post(send_url(), json=send_body(), headers=BEARER)
        assert res.status_code == 403, role


def test_送った後に応答の前にF4の判断が走る(client, monkeypatch, no_background_assessment) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    written = catch_writes(monkeypatch)
    set_triage(monkeypatch)

    client.post(send_url(), json=send_body(), headers=BEARER)
    assert len(no_background_assessment) == 1
    call = no_background_assessment[0]
    assert call["trigger_id"] == written[0]["consultation_id"]
    # 保存した相談そのものを渡す（要件定義書10-6は記録と相談の両方を入力にする）
    recent = call["recent_consultations"]
    assert recent[0]["consultation_id"] == written[0]["consultation_id"]
    assert "胸が苦しい" in recent[0]["body"]


def test_判断を呼ぶ経路は画面に無い(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    res = client.post(f"/api/survivors/{SURVIVOR}/consultations/assess", json={}, headers=BEARER)
    assert res.status_code in (404, 405)


# --- 組織の相談の一覧（専門職のホーム。要件定義書F8・10-5） ---


def test_組織の相談の一覧は専門職だけ(client, monkeypatch) -> None:
    for uid, role in ((SELF_UID, users.ROLE_SURVIVOR), (VOL_UID, users.ROLE_VOLUNTEER)):
        sign_in(monkeypatch, uid=uid, role=role)
        assert client.get("/api/consultations", headers=BEARER).status_code == 403, role


def test_組織の相談の一覧は急ぎの順のまま返す(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    rows = [
        consultation_row(consultation_id="cs_high", urgency="high", full_name="佐藤 太郎"),
        consultation_row(
            consultation_id="cs_low",
            urgency="low",
            status=consultations.STATUS_CLOSED,
            is_unanswered=False,
            full_name="鈴木 花子",
        ),
    ]
    captured: dict = {}

    def list_for_org(**kwargs):
        captured.update(kwargs)
        return rows

    monkeypatch.setattr(consultations, "list_for_org", list_for_org)

    body = client.get("/api/consultations", headers=BEARER).json()
    assert captured["org_id"] == ORG  # 立場は stg_users から決める（要求の値は使わない）
    assert [item["consultation_id"] for item in body["consultations"]] == ["cs_high", "cs_low"]
    assert body["consultations"][0]["urgency"] == "high"
    assert body["consultations"][0]["full_name"] == "佐藤 太郎"
    assert body["unanswered_count"] == 1


def test_一覧が読めないときは503(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)

    def unavailable(**kwargs):
        raise bq.BigQueryUnavailable("テスト")

    monkeypatch.setattr(consultations, "list_for_org", unavailable)
    assert client.get("/api/consultations", headers=BEARER).status_code == 503


# --- 返事と状況（要件定義書F8・10-5） ---


def test_返事を書けるのは専門職だけ(client, monkeypatch) -> None:
    for uid, role in ((SELF_UID, users.ROLE_SURVIVOR), (VOL_UID, users.ROLE_VOLUNTEER)):
        sign_in(monkeypatch, uid=uid, role=role)
        set_survivor(monkeypatch, survivor_row())
        res = client.post(reply_url(), json={"reply_text": "伺います"}, headers=BEARER)
        assert res.status_code == 403, role


def test_返事を書くと状況も記録される(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(consultations, "fetch_consultation", lambda **kwargs: consultation_row())
    written = catch_writes(monkeypatch)

    res = client.post(
        reply_url(),
        json={
            "reply_text": "きょうの午後に伺います",
            "next_action": "午後に訪問する",
            "status": consultations.STATUS_IN_PROGRESS,
        },
        headers=BEARER,
    )
    assert res.status_code == 201
    assert written[0]["action"] == consultations.ACTION_REPLIED
    assert written[0]["reply_text"] == "きょうの午後に伺います"
    assert written[0]["status"] == consultations.STATUS_IN_PROGRESS
    assert res.json()["consultation"]["last_reply_text"] == "きょうの午後に伺います"


def test_空の返事は400(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(consultations, "fetch_consultation", lambda **kwargs: consultation_row())
    catch_writes(monkeypatch)
    res = client.post(reply_url(), json={"reply_text": "   "}, headers=BEARER)
    assert res.status_code == 400


def test_ない相談への返事は404(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(consultations, "fetch_consultation", lambda **kwargs: None)
    res = client.post(reply_url(), json={"reply_text": "伺います"}, headers=BEARER)
    assert res.status_code == 404


def test_状況を変えられるのは専門職だけ(client, monkeypatch) -> None:
    for uid, role in ((SELF_UID, users.ROLE_SURVIVOR), (VOL_UID, users.ROLE_VOLUNTEER)):
        sign_in(monkeypatch, uid=uid, role=role)
        set_survivor(monkeypatch, survivor_row())
        res = client.patch(status_url(), json={"status": "closed"}, headers=BEARER)
        assert res.status_code == 403, role


def test_状況を変えると追記される(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(consultations, "fetch_consultation", lambda **kwargs: consultation_row())
    written = catch_writes(monkeypatch)

    res = client.patch(status_url(), json={"status": consultations.STATUS_CLOSED}, headers=BEARER)
    assert res.status_code == 200
    assert written[0]["action"] == consultations.ACTION_STATUS_CHANGED
    assert written[0]["status"] == consultations.STATUS_CLOSED
    assert res.json()["consultation"]["status_label"] == "対応済み"


def test_知らない状況は400(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(consultations, "fetch_consultation", lambda **kwargs: consultation_row())
    catch_writes(monkeypatch)
    assert client.patch(status_url(), json={"status": "done"}, headers=BEARER).status_code == 400


# --- 相談のやり取り（要望7。要件定義書には無い追加の機能） ---


def messages_url(survivor_id: str = SURVIVOR, consultation_id: str = CONSULTATION) -> str:
    return f"/api/survivors/{survivor_id}/consultations/{consultation_id}/messages"


def test_本人が書き足せる(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(consultations, "fetch_consultation", lambda **kwargs: consultation_row())
    written = catch_writes(monkeypatch)

    res = client.post(messages_url(), json={"message": "その後、熱も出てきました"}, headers=BEARER)
    assert res.status_code == 201
    assert written[0]["action"] == consultations.ACTION_MESSAGE
    assert written[0]["reply_text"] == "その後、熱も出てきました"
    assert written[0]["status"] is None
    assert res.json()["message"]["from_survivor"] is True


def test_書き足せるのは本人だけ(client, monkeypatch) -> None:
    for uid, role in ((PRO_UID, users.ROLE_PROFESSIONAL), (VOL_UID, users.ROLE_VOLUNTEER)):
        sign_in(monkeypatch, uid=uid, role=role)
        set_survivor(monkeypatch, survivor_row())
        res = client.post(messages_url(), json={"message": "伺います"}, headers=BEARER)
        assert res.status_code == 403, role


def test_空の書き足しは400(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(consultations, "fetch_consultation", lambda **kwargs: consultation_row())
    catch_writes(monkeypatch)
    res = client.post(messages_url(), json={"message": "   "}, headers=BEARER)
    assert res.status_code == 400


def test_ない相談への書き足しは404(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(consultations, "fetch_consultation", lambda **kwargs: None)
    res = client.post(messages_url(), json={"message": "伺います"}, headers=BEARER)
    assert res.status_code == 404


def test_ほかの人の相談には書き足せない(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id="uid-other"))
    res = client.post(messages_url(), json={"message": "伺います"}, headers=BEARER)
    assert res.status_code == 404


def test_やり取りは本人と専門職が読める(client, monkeypatch) -> None:
    for uid, role in ((SELF_UID, users.ROLE_SURVIVOR), (PRO_UID, users.ROLE_PROFESSIONAL)):
        sign_in(monkeypatch, uid=uid, role=role)
        set_survivor(monkeypatch, survivor_row())
        monkeypatch.setattr(
            consultations, "fetch_consultation", lambda **kwargs: consultation_row()
        )
        monkeypatch.setattr(
            consultations,
            "list_events",
            lambda **kwargs: [
                {
                    "event_id": "evt_1",
                    "created_at": NOW,
                    "created_role": "survivor",
                    "action": consultations.ACTION_MESSAGE,
                    "reply_text": "その後、熱も出てきました",
                    "next_action": None,
                }
            ],
        )
        res = client.get(messages_url(), headers=BEARER)
        assert res.status_code == 200, role
        assert res.json()["messages"][0]["text"] == "その後、熱も出てきました"


def test_一般の支援者はやり取りを読めない(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row())
    res = client.get(messages_url(), headers=BEARER)
    assert res.status_code == 403


# --- 詳細の画面（要件定義書F1の1画面・7-1の「見られる人」） ---


def test_詳細に相談が入る_本人(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(), consultation_rows=[consultation_row()])
    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()
    items = body["survivor"]["consultations"]
    assert items[0]["consultation_id"] == CONSULTATION
    assert "urgency" not in items[0]  # 本人には急ぎの度合いを出さない


def test_詳細に相談が入る_専門職(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(), consultation_rows=[consultation_row()])
    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()
    items = body["survivor"]["consultations"]
    assert items[0]["urgency"] == "high"
    assert items[0]["urgency_evidence"]


def test_一般の支援者の詳細には相談を入れない(client, monkeypatch) -> None:
    # 7-1 の「見られる人」は本人と同じ組織の専門職だけ（相談は読まない）
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(
        consultations, "list_for_survivor", lambda **kwargs: pytest.fail("読んではいけません")
    )
    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()
    assert body["survivor"]["consultations"] == []


# --- 画面の選択肢（/api/public-config に相乗りさせる） ---


def test_画面の選択肢に相談の種類と状況が入る(client) -> None:
    body = client.get("/api/public-config").json()
    choices = body["choices"]
    assert [item["value"] for item in choices["consultation_categories"]] == list(
        consultations.CATEGORIES
    )
    assert [item["value"] for item in choices["consultation_statuses"]] == list(
        consultations.STATUSES
    )
    assert [item["value"] for item in choices["consultation_urgency_levels"]] == list(
        consultations.URGENCY_LEVELS
    )


def test_案内の文言はAPIが返す(client) -> None:
    # 文言の正本は services/consultations.py。画面に書き写すとずれる
    body = client.get("/api/public-config").json()
    assert body["choices"]["consultation_emergency_guide"] == consultations.EMERGENCY_GUIDE


def test_画面に案内の文言を書き写していない() -> None:
    # 画面はAPIが返す文言だけを出す（コメントでの言及は可。表示する文は持たない）
    source = (
        Path(__file__).resolve().parents[1] / "02_app" / "frontend" / "f8.js"
    ).read_text(encoding="utf-8")
    assert "119番へ電話" not in source
    assert consultations.EMERGENCY_NOTICE[:12] not in source


# --- 組織につながっていない本人の相談（追加修正②） ---

ORGLESS_SELF_UID = "uid-self-orgless"
ORGLESS_SURVIVOR = "sv_orgless"


def orgless_sign_in(monkeypatch) -> None:
    """組織がない本人としてログインする（linked_survivor_id は組織がない記録）。"""
    sign_in(monkeypatch, uid=ORGLESS_SELF_UID, role=users.ROLE_SURVIVOR, org_id="")
    monkeypatch.setattr(
        users,
        "fetch_user",
        lambda user_id: {
            "user_id": ORGLESS_SELF_UID,
            "org_id": "",
            "role": users.ROLE_SURVIVOR,
            "profession": None,
            "display_name": "テスト本人",
            "login_method": "phone",
            "linked_survivor_id": ORGLESS_SURVIVOR,
            "org_name": None,
        },
    )


def orgless_row(**overrides) -> dict:
    values = {
        "survivor_id": ORGLESS_SURVIVOR,
        "org_id": "",
        "self_user_id": ORGLESS_SELF_UID,
        "registered_via": "self",
        "municipality_code": "172049",
        "assignee_user_ids": [],
    }
    values.update(overrides)
    return survivor_row(**values)


def public_row(**overrides) -> dict:
    """公開の一覧の1行（`list_public` が返す形。氏名・年齢は読まない）。"""
    row = consultation_row(
        consultation_id="cs_public",
        org_id="",
        survivor_id=ORGLESS_SURVIVOR,
        municipality_code="172049",
    )
    row.update(overrides)
    return row


def test_組織がない本人も相談を送れて公開になる(client, monkeypatch, no_background_assessment) -> None:
    orgless_sign_in(monkeypatch)
    set_survivor(monkeypatch, orgless_row())
    written = catch_writes(monkeypatch)
    set_triage(monkeypatch)

    res = client.post(send_url(ORGLESS_SURVIVOR), json=send_body(), headers=BEARER)
    assert res.status_code == 201
    # 組織のIDを空にして残す（これが公開の相談になる）
    assert written[0]["org_id"] == ""
    assert res.json()["consultation"]["is_public"] is True
    # 組織がないと悪化の兆しを使う人がいないので、判断しない（Gemini の回数も使わない）
    assert no_background_assessment == []


def test_組織がない本人には送る前に公開になることを知らせる(client, monkeypatch) -> None:
    orgless_sign_in(monkeypatch)
    set_survivor(monkeypatch, orgless_row())
    set_structure(monkeypatch)
    res = client.post(
        structure_url(ORGLESS_SURVIVOR), json={"raw_text": RAW_TEXT}, headers=BEARER
    )
    assert res.status_code == 200
    body = res.json()
    assert body["public"] is True
    assert body["public_notice"] == consultations.PUBLIC_NOTICE
    assert "組織を問わず支援者が見られます" in consultations.PUBLIC_NOTICE
    assert "知られたくないことは書かないでください" in consultations.PUBLIC_NOTICE


def test_組織に入っている本人には公開の知らせを出さない(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row())
    set_structure(monkeypatch)
    body = client.post(structure_url(), json={"raw_text": RAW_TEXT}, headers=BEARER).json()
    assert body["public"] is False
    assert body["public_notice"] is None


def test_公開の知らせの文言はAPIが返す(client) -> None:
    body = client.get("/api/public-config").json()
    assert body["choices"]["consultation_public_notice"] == consultations.PUBLIC_NOTICE


@pytest.mark.parametrize(
    ("role", "org_id"),
    [
        (users.ROLE_PROFESSIONAL, ORG),
        (users.ROLE_PROFESSIONAL, OTHER_ORG),
        (users.ROLE_VOLUNTEER, ORG),
        # 組織に入っていない支援者も見られる
        (users.ROLE_VOLUNTEER, ""),
    ],
)
def test_公開の相談はどの組織の支援者からも見え氏名が出ない(client, monkeypatch, role, org_id) -> None:
    sign_in(monkeypatch, uid=f"uid-{role}-{org_id or 'none'}", role=role, org_id=org_id)
    # 一覧の読みには氏名が入っていても、応答には出さない（形を作るところで落とす）
    monkeypatch.setattr(
        consultations,
        "list_public",
        lambda **kwargs: [public_row(full_name="佐藤 太郎", age=78)],
    )
    res = client.get("/api/consultations/public", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    item = body["consultations"][0]
    assert item["consultation_id"] == "cs_public"
    assert item["municipality_code"] == "172049"
    assert "full_name" not in item
    assert "age" not in item
    assert "survivor_id" not in item
    assert "佐藤" not in str(body)
    assert body["can_reply"] is (role == users.ROLE_PROFESSIONAL)


def test_本人は公開の一覧を読めない(client, monkeypatch) -> None:
    orgless_sign_in(monkeypatch)
    assert client.get("/api/consultations/public", headers=BEARER).status_code == 403


@pytest.mark.parametrize("org_id", [ORG, OTHER_ORG])
def test_公開の相談にはどの組織の専門職でも返事を書ける(client, monkeypatch, org_id) -> None:
    sign_in(monkeypatch, uid=f"uid-pro-{org_id}", role=users.ROLE_PROFESSIONAL, org_id=org_id)
    monkeypatch.setattr(consultations, "fetch_public", lambda **kwargs: public_row())
    written = catch_writes(monkeypatch)
    res = client.post(
        "/api/consultations/public/cs_public/replies",
        json={"reply_text": "近くの相談窓口をお伝えします"},
        headers=BEARER,
    )
    assert res.status_code == 201
    assert written[0]["action"] == consultations.ACTION_REPLIED
    assert written[0]["survivor_id"] == ORGLESS_SURVIVOR
    # 公開の相談のやり取りは、組織のIDを空にして残す
    assert written[0]["org_id"] == ""
    item = res.json()["consultation"]
    assert "survivor_id" not in item
    assert item["last_reply_text"] == "近くの相談窓口をお伝えします"


@pytest.mark.parametrize("org_id", [ORG, ""])
def test_一般の支援者は公開の相談に返事を書けない(client, monkeypatch, org_id) -> None:
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER, org_id=org_id)
    monkeypatch.setattr(consultations, "fetch_public", lambda **kwargs: public_row())
    res = client.post(
        "/api/consultations/public/cs_public/replies",
        json={"reply_text": "伺います"},
        headers=BEARER,
    )
    assert res.status_code == 403


def test_公開でない相談には公開の経路で返事を書けない(client, monkeypatch) -> None:
    # 本人が組織につないだあとは、公開の一覧から消え、公開の経路では404になる
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL, org_id=OTHER_ORG)
    monkeypatch.setattr(consultations, "fetch_public", lambda **kwargs: None)
    res = client.post(
        "/api/consultations/public/cs_public/replies",
        json={"reply_text": "伺います"},
        headers=BEARER,
    )
    assert res.status_code == 404


def test_公開の相談のやり取りは支援者が読める(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER, org_id="")
    monkeypatch.setattr(consultations, "fetch_public", lambda **kwargs: public_row())
    captured: dict = {}

    def list_events(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(consultations, "list_events", list_events)
    res = client.get("/api/consultations/public/cs_public/messages", headers=BEARER)
    assert res.status_code == 200
    assert captured == {"consultation_id": "cs_public", "survivor_id": ORGLESS_SURVIVOR}


def test_組織につないだあとの前の相談はその組織の専門職だけが見る(client, monkeypatch) -> None:
    # 記録のいまの組織は ORG。組織がないときに送った相談（org_id が空）が詳細に出る
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL, org_id=ORG)
    earlier = public_row(consultation_id="cs_before")
    set_survivor(
        monkeypatch,
        orgless_row(org_id=ORG),
        consultation_rows=[earlier],
    )
    body = client.get(f"/api/survivors/{ORGLESS_SURVIVOR}", headers=BEARER).json()
    item = body["survivor"]["consultations"][0]
    assert item["consultation_id"] == "cs_before"
    # いまは組織の相談で、公開ではない
    assert item["is_public"] is False

    # この組織の専門職は返事を書ける（返事の行の組織は、記録のいまの組織）
    monkeypatch.setattr(consultations, "fetch_consultation", lambda **kwargs: earlier)
    written = catch_writes(monkeypatch)
    res = client.post(
        reply_url(ORGLESS_SURVIVOR, "cs_before"), json={"reply_text": "伺います"}, headers=BEARER
    )
    assert res.status_code == 201
    assert written[0]["org_id"] == ORG


def test_組織につないだあとはほかの組織の専門職から見えない(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL, org_id=OTHER_ORG)
    set_survivor(monkeypatch, orgless_row(org_id=ORG), consultation_rows=[public_row()])
    assert client.get(f"/api/survivors/{ORGLESS_SURVIVOR}", headers=BEARER).status_code == 404
    res = client.post(
        reply_url(ORGLESS_SURVIVOR, "cs_public"), json={"reply_text": "伺います"}, headers=BEARER
    )
    assert res.status_code == 404


def test_組織に入っている本人の相談は今までどおりその組織の専門職だけ(client, monkeypatch) -> None:
    # 同じ組織の専門職は見られる
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL, org_id=ORG)
    set_survivor(monkeypatch, survivor_row(), consultation_rows=[consultation_row()])
    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()
    assert body["survivor"]["consultations"][0]["is_public"] is False
    # ほかの組織の専門職は見られない
    sign_in(monkeypatch, uid="uid-pro-other", role=users.ROLE_PROFESSIONAL, org_id=OTHER_ORG)
    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404
    # 公開の経路でも見られない（組織の相談は公開ではない）
    monkeypatch.setattr(consultations, "fetch_public", lambda **kwargs: None)
    res = client.get(f"/api/consultations/public/{CONSULTATION}/messages", headers=BEARER)
    assert res.status_code == 404


def test_組織がない本人は自分の相談のやり取りを読める(client, monkeypatch) -> None:
    orgless_sign_in(monkeypatch)
    set_survivor(monkeypatch, orgless_row())
    monkeypatch.setattr(consultations, "fetch_consultation", lambda **kwargs: public_row())
    monkeypatch.setattr(consultations, "list_events", lambda **kwargs: [])
    res = client.get(messages_url(ORGLESS_SURVIVOR, "cs_public"), headers=BEARER)
    assert res.status_code == 200
