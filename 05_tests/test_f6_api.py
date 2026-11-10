"""F6（受けられる支援と申請の状況）の API のテスト（開発手順書 Step 10）。

Google Cloud にも Firebase にも Gemini にも接続しない。確かめるのは次の7つ。

- **使えるのは本人と専門職だけ**（一般の支援者は 403。要件定義書446行・601行）
- トークンが無ければ 401（3本すべて）
- **登録されていない制度は記録しない**（要求に書かれたIDを信じない。要件定義書10-3）
- 示した支援は**未申請から始まり**、状況の更新は**追記**になる（10-4）
- ほかの人・ほかの組織の支援の記録は **404**（存在を分からせない。10-3）
- 世帯の人数を変えたときは、**記録の版が1つ増える**（要件定義書F6）
- 「目安であり、受けられるかは自治体が決める」が**応答と選択肢のAPIに入る**
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
    consultations,
    facilities,
    handovers,
    health,
    link_codes,
    support_ai,
    supports,
    survivors,
    users,
    visits,
)

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
OTHER_ORG = "org_suzu"
SURVIVOR = "sv_0001"
PROGRAM = "org_wajima__seikatsu_saiken_kiso"
PRO_UID = "uid-pro-1"
VOL_UID = "uid-vol-1"
SELF_UID = "uid-self-1"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
BEARER = {"Authorization": "Bearer dummy-token"}
SUPPORT_ID = supports.support_id_for(org_id=ORG, survivor_id=SURVIVOR, program_id=PROGRAM)


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
        facilities.bq,
        handovers.bq,
        supports.bq,
    ):
        monkeypatch.setattr(module, "query", fail, raising=False)
        monkeypatch.setattr(module, "query_one", fail, raising=False)
        monkeypatch.setattr(module, "insert", fail, raising=False)


@pytest.fixture(autouse=True)
def no_gemini(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("テストから Gemini を呼んではいけません")

    monkeypatch.setattr(support_ai.gemini, "generate_structured", fail)


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
        "version": 3,
        "full_name": "佐藤 太郎",
        "age": 72,
        "household_size": 2,
        "contact": None,
        "stage": "temporary",
        "location_type": "temporary_built",
        "location_detail": "仮設第1団地",
        "self_user_id": SELF_UID,
        "registered_via": "professional",
        "registered_at": NOW,
        "updated_at": NOW,
        "risk_level": "low",
        "risk_evidence": {"items": []},
        "risk_next_action": None,
        "risk_source": assessments.SOURCE_AI,
        "risk_assessed_at": NOW,
        "assignee_user_ids": [VOL_UID],
        "assignee_count": 1,
        "suggested_count": 1,
        "unapplied_count": 1,
        "nearest_unapplied_deadline": dt.date(2026, 11, 20),
        "days_to_nearest_deadline": 12,
    }
    row.update(overrides)
    return row


def program_row(program_id: str = PROGRAM, **overrides) -> dict:
    row = {
        "program_id": program_id,
        "org_id": ORG,
        "version": 1,
        "program_name": "被災者生活再建支援金（基礎支援金）",
        "conditions": "住宅が全壊、または解体した世帯",
        "benefit": "100万円（複数世帯）",
        "apply_to": "輪島市 福祉課",
        "deadline_rule": "災害発生日から13月以内",
        "documents": "罹災証明書",
        "source_url": "https://example.lg.jp/saiken",
    }
    row.update(overrides)
    return row


def support_row(**overrides) -> dict:
    row = {
        "support_id": SUPPORT_ID,
        "survivor_id": SURVIVOR,
        "org_id": ORG,
        "program_id": PROGRAM,
        "suggested_reason": "住宅が全壊のため対象になりそうです",
        "suggested_at": NOW,
        "status": supports.STATUS_NOT_APPLIED,
        "deadline": dt.date(2026, 11, 20),
        "days_to_deadline": 12,
        "housing_damage": "total",
        "rebuild_intent": "rebuild",
        "updated_at": NOW,
        "updated_by": PRO_UID,
    }
    row.update(program_row())
    row.update(overrides)
    return row


def set_reads(
    monkeypatch, *, survivor=None, programs=None, existing=None, support=None, consulted=None
) -> None:
    monkeypatch.setattr(
        consultations,
        "list_for_survivor",
        lambda **kwargs: list(consulted or []),
    )
    monkeypatch.setattr(
        survivors,
        "fetch_survivor",
        lambda survivor_id: survivor_row() if survivor is None else survivor,
    )
    monkeypatch.setattr(
        supports,
        "list_programs",
        lambda **kwargs: list(programs if programs is not None else [program_row()]),
    )
    monkeypatch.setattr(
        supports,
        "list_for_survivor",
        lambda **kwargs: list(existing if existing is not None else []),
    )
    monkeypatch.setattr(supports, "fetch_support", lambda **kwargs: support)


def catch_writes(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(supports, "save", lambda row: written.append(row) or row)
    monkeypatch.setattr(survivors.bq, "insert", lambda table, row, **kw: written.append(row) or 1)
    return written


def fake_ai(monkeypatch, *, candidates=None, ai_used=True, notice=None) -> None:
    monkeypatch.setattr(
        support_ai,
        "suggest",
        lambda **kwargs: {
            "candidates": list(candidates or []),
            "ai_used": ai_used,
            "dropped_count": 0,
            "notice": notice,
        },
    )


def candidate(program_id: str = PROGRAM, **overrides) -> dict:
    item = {
        **supports.program_payload(program_row(program_id)),
        "reason": "住宅が全壊のため",
        "deadline": "2026-11-20",
        "status": supports.STATUS_NOT_APPLIED,
        "status_label": "未申請",
        "next_action": supports.NEXT_ACTIONS[supports.STATUS_NOT_APPLIED],
        "disclaimer": supports.DISCLAIMER,
    }
    item.update(overrides)
    return item


CANDIDATES_BODY = {"housing_damage": "total", "rebuild_intent": "rebuild"}
CREATE_BODY = {
    "housing_damage": "total",
    "rebuild_intent": "rebuild",
    "items": [{"program_id": PROGRAM, "reason": "住宅が全壊のため", "deadline": "2026-11-20"}],
}


# --- 立場と認証 ---


def test_トークンが無ければ401(client):
    assert client.post(f"/api/survivors/{SURVIVOR}/support-candidates", json={}).status_code == 401
    assert client.post(f"/api/survivors/{SURVIVOR}/supports", json={}).status_code == 401
    res = client.patch(f"/api/survivors/{SURVIVOR}/supports/{SUPPORT_ID}", json={})
    assert res.status_code == 401


def test_一般の支援者は支援制度を扱えない(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates", json=CANDIDATES_BODY, headers=BEARER
    )
    assert res.status_code == 403
    res = client.post(f"/api/survivors/{SURVIVOR}/supports", json=CREATE_BODY, headers=BEARER)
    assert res.status_code == 403
    res = client.patch(
        f"/api/survivors/{SURVIVOR}/supports/{SUPPORT_ID}",
        json={"status": "applying"},
        headers=BEARER,
    )
    assert res.status_code == 403


# --- F6-1. 受けられそうな支援 ---


def test_専門職は候補と制度の一覧と断り書きを受け取る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch)
    fake_ai(monkeypatch, candidates=[candidate()])
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates", json=CANDIDATES_BODY, headers=BEARER
    )
    assert res.status_code == 200
    body = res.json()
    assert body["candidates"][0]["program_id"] == PROGRAM
    assert body["candidates"][0]["already_recorded"] is False
    assert body["programs"][0]["program_id"] == PROGRAM
    assert body["disclaimer"] == supports.DISCLAIMER
    assert body["ai_used"] is True


def test_本人も候補を受け取れる(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_reads(monkeypatch)
    fake_ai(monkeypatch, candidates=[candidate()])
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates", json=CANDIDATES_BODY, headers=BEARER
    )
    assert res.status_code == 200
    assert res.json()["candidates"][0]["next_action"]


def test_すでに記録した制度には印が付く(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch, existing=[support_row()])
    fake_ai(monkeypatch, candidates=[candidate()])
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates", json=CANDIDATES_BODY, headers=BEARER
    )
    body = res.json()
    assert body["candidates"][0]["already_recorded"] is True
    assert body["supports"][0]["support_id"] == SUPPORT_ID


def test_選択肢の外の被害の程度は400(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates",
        json={"housing_damage": "ぜんかい", "rebuild_intent": "rebuild"},
        headers=BEARER,
    )
    assert res.status_code == 400


def test_AIを呼べなくても制度の一覧は返る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch)
    fake_ai(monkeypatch, ai_used=False, notice=support_ai.AI_UNAVAILABLE_NOTICE)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates", json=CANDIDATES_BODY, headers=BEARER
    )
    body = res.json()
    assert body["candidates"] == []
    assert body["ai_used"] is False
    assert body["notice"] == support_ai.AI_UNAVAILABLE_NOTICE
    assert len(body["programs"]) == 1


def test_世帯の人数を変えても記録は変えない(client, monkeypatch):
    """この画面からは記録の版を足さない（使いやすさの改修の計画.md 5章・6-4）。"""
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        support_ai,
        "suggest",
        lambda **kwargs: seen.update(kwargs)
        or {"candidates": [], "ai_used": True, "dropped_count": 0, "notice": None},
    )
    written = catch_writes(monkeypatch)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates",
        json={**CANDIDATES_BODY, "household_size": 4},
        headers=BEARER,
    )
    assert res.status_code == 200
    assert written == []
    # 記録の値（2人）で調べる。要求の4は使わない
    assert seen["household_size"] == 2
    assert res.json()["household_size"] == 2
    assert res.json()["household_from_record"] is True


def test_記録に世帯の人数が無いときだけ要求の値をその場で使う(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch, survivor=survivor_row(household_size=None))
    fake_ai(monkeypatch, candidates=[])
    written = catch_writes(monkeypatch)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates",
        json={**CANDIDATES_BODY, "household_size": 3},
        headers=BEARER,
    )
    assert res.status_code == 200
    assert written == []
    assert res.json()["household_size"] == 3
    assert res.json()["household_from_record"] is False


def test_記録に無いときの世帯の人数も範囲を確かめる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch, survivor=survivor_row(household_size=None))
    fake_ai(monkeypatch, candidates=[])
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates",
        json={**CANDIDATES_BODY, "household_size": 99},
        headers=BEARER,
    )
    assert res.status_code == 400


def test_調べるときにその人の直近の相談を渡す(client, monkeypatch):
    """使いやすさの改修の計画.md 6-3。相談の内容も使って調べる。"""
    now = dt.datetime.now(dt.timezone.utc)
    consulted = [
        {
            "consultation_id": "c_new",
            "consulted_at": now - dt.timedelta(days=1),
            "category": "money",
            "raw_text": "仕事がなくなり、家賃が払えない",
            "structured": None,
            "urgency": "medium",
        },
        {
            "consultation_id": "c_old",
            "consulted_at": now - dt.timedelta(days=200),
            "category": "money",
            "raw_text": "古い相談",
            "structured": None,
            "urgency": "low",
        },
    ]
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR, org_id=ORG)
    seen_ids = {}

    def fake_list(**kwargs):
        seen_ids["survivor_id"] = kwargs.get("survivor_id")
        return consulted

    set_reads(monkeypatch, survivor=survivor_row(self_user_id=SELF_UID))
    monkeypatch.setattr(consultations, "list_for_survivor", fake_list)
    seen = {}
    monkeypatch.setattr(
        support_ai,
        "suggest",
        lambda **kwargs: seen.update(kwargs)
        or {"candidates": [], "ai_used": True, "dropped_count": 0, "notice": None},
    )
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates", json=CANDIDATES_BODY, headers=BEARER
    )
    assert res.status_code == 200
    assert seen_ids["survivor_id"] == SURVIVOR
    # 90日より前の相談は渡さない
    assert [item["consultation_id"] for item in seen["consultation_items"]] == ["c_new"]
    assert res.json()["consultation_count"] == 1


def test_ほかの組織の記録は404(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch, survivor=survivor_row(org_id=OTHER_ORG))
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates", json=CANDIDATES_BODY, headers=BEARER
    )
    assert res.status_code == 404


def test_BigQueryを呼べないときは503(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch)

    def unavailable(**kwargs):
        raise bq.BigQueryUnavailable("読めない")

    monkeypatch.setattr(supports, "list_programs", unavailable)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates", json=CANDIDATES_BODY, headers=BEARER
    )
    assert res.status_code == 503


# --- F6-2. 示した支援の記録 ---


def test_示した支援は未申請で記録される(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch)
    written = catch_writes(monkeypatch)
    res = client.post(f"/api/survivors/{SURVIVOR}/supports", json=CREATE_BODY, headers=BEARER)
    assert res.status_code == 201
    body = res.json()
    assert body["count"] == 1
    assert body["supports"][0]["status"] == supports.STATUS_NOT_APPLIED
    assert body["supports"][0]["program_name"] == "被災者生活再建支援金（基礎支援金）"
    assert body["supports"][0]["apply_to"] == "輪島市 福祉課"
    assert body["disclaimer"] == supports.DISCLAIMER
    assert len(written) == 1
    assert written[0]["action"] == supports.ACTION_SUGGESTED
    assert written[0]["support_id"] == SUPPORT_ID
    assert written[0]["deadline"] == dt.date(2026, 11, 20)


def test_本人も支援を記録できる(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_reads(monkeypatch)
    written = catch_writes(monkeypatch)
    res = client.post(f"/api/survivors/{SURVIVOR}/supports", json=CREATE_BODY, headers=BEARER)
    assert res.status_code == 201
    assert written[0]["created_role"] == users.ROLE_SURVIVOR
    # 本人の応答には、誰が変えたかを入れない
    assert "updated_by" not in res.json()["supports"][0]


def test_登録されていない制度は記録しない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch)
    written = catch_writes(monkeypatch)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/supports",
        json={**CREATE_BODY, "items": [{"program_id": "架空の見舞金", "reason": "もらえそう"}]},
        headers=BEARER,
    )
    assert res.status_code == 400
    assert written == []


def test_登録された制度だけを選んで記録する(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch)
    written = catch_writes(monkeypatch)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/supports",
        json={
            **CREATE_BODY,
            "items": [
                {"program_id": PROGRAM, "reason": "全壊のため"},
                {"program_id": "架空の見舞金", "reason": "もらえそう"},
                {"program_id": PROGRAM, "reason": "二重の指定"},
            ],
        },
        headers=BEARER,
    )
    assert res.status_code == 201
    assert res.json()["count"] == 1
    assert len(written) == 1


def test_支援が選ばれていなければ400(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/supports",
        json={**CREATE_BODY, "items": []},
        headers=BEARER,
    )
    assert res.status_code == 400


# --- F6-3. 申請の状況の更新 ---


def test_申請の状況は追記で更新される(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch, support=support_row())
    written = catch_writes(monkeypatch)
    res = client.patch(
        f"/api/survivors/{SURVIVOR}/supports/{SUPPORT_ID}",
        json={"status": "applying"},
        headers=BEARER,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["support"]["status"] == "applying"
    assert body["support"]["status_label"] == "申請中"
    # 申請中になったら期限の知らせは出さない（未申請だけ知らせる）
    assert body["support"]["deadline_notice"] is None
    assert len(written) == 1
    assert written[0]["action"] == supports.ACTION_STATUS_CHANGED
    assert written[0]["support_id"] == SUPPORT_ID
    # 期限を渡さなければ、いまの期限を引き継ぐ
    assert written[0]["deadline"] == dt.date(2026, 11, 20)


def test_本人も申請の状況を更新できる(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_reads(monkeypatch, support=support_row())
    written = catch_writes(monkeypatch)
    res = client.patch(
        f"/api/survivors/{SURVIVOR}/supports/{SUPPORT_ID}",
        json={"status": "received"},
        headers=BEARER,
    )
    assert res.status_code == 200
    assert written[0]["created_role"] == users.ROLE_SURVIVOR
    assert res.json()["support"]["next_action"] == supports.NEXT_ACTIONS["received"]


def test_ほかの人の支援の記録は404(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch, support=None)
    written = catch_writes(monkeypatch)
    res = client.patch(
        f"/api/survivors/{SURVIVOR}/supports/sp_other",
        json={"status": "applying"},
        headers=BEARER,
    )
    assert res.status_code == 404
    assert written == []


def test_選択肢の外の状況は400(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch, support=support_row())
    written = catch_writes(monkeypatch)
    res = client.patch(
        f"/api/survivors/{SURVIVOR}/supports/{SUPPORT_ID}",
        json={"status": "うけとった"},
        headers=BEARER,
    )
    assert res.status_code == 400
    assert written == []


# --- 詳細・一覧・選択肢のAPI ---


def set_detail_reads(monkeypatch, *, support_rows) -> None:
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: survivor_row())
    monkeypatch.setattr(survivors, "location_history", lambda **kwargs: [])
    monkeypatch.setattr(assignments, "list_for_survivor", lambda **kwargs: [])
    monkeypatch.setattr(health, "fetch_latest", lambda **kwargs: None)
    monkeypatch.setattr(visits, "list_visits", lambda **kwargs: [])
    monkeypatch.setattr(consultations, "list_for_survivor", lambda **kwargs: [])
    monkeypatch.setattr(handovers, "list_for_survivor", lambda **kwargs: [])
    monkeypatch.setattr(supports, "list_for_survivor", lambda **kwargs: list(support_rows))


def test_詳細に支援と申請の状況が入る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_detail_reads(monkeypatch, support_rows=[support_row()])
    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)
    assert res.status_code == 200
    items = res.json()["survivor"]["supports"]
    assert items[0]["program_name"] == "被災者生活再建支援金（基礎支援金）"
    assert items[0]["deadline_notice"] == "申請の期限まであと12日です"
    assert items[0]["disclaimer"] == supports.DISCLAIMER


def test_本人の詳細にも支援が入る(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_detail_reads(monkeypatch, support_rows=[support_row()])
    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)
    assert res.status_code == 200
    items = res.json()["survivor"]["supports"]
    assert items[0]["next_action"] == supports.NEXT_ACTIONS[supports.STATUS_NOT_APPLIED]
    assert "updated_by" not in items[0]


def test_一般の支援者の詳細では支援を読まない(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: survivor_row())
    monkeypatch.setattr(survivors, "location_history", lambda **kwargs: [])
    monkeypatch.setattr(
        assignments,
        "list_for_survivor",
        lambda **kwargs: [
            {"supporter_user_id": VOL_UID, "status": assignments.STATUS_ACTIVE}
        ],
    )
    monkeypatch.setattr(health, "fetch_latest", lambda **kwargs: None)
    monkeypatch.setattr(visits, "list_visits", lambda **kwargs: [])
    monkeypatch.setattr(handovers, "list_for_survivor", lambda **kwargs: [])

    def fail(**kwargs):
        raise AssertionError("一般の支援者には支援を読まない")

    monkeypatch.setattr(supports, "list_for_survivor", fail)
    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)
    assert res.status_code == 200
    assert res.json()["survivor"]["supports"] == []


def test_一覧に期限の近い未申請の知らせが入る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(survivors, "list_survivors", lambda **kwargs: [survivor_row()])
    res = client.get("/api/survivors", headers=BEARER)
    assert res.status_code == 200
    alerts = res.json()["support_alerts"]
    assert alerts["count"] == 1
    assert alerts["survivors"][0]["survivor_id"] == SURVIVOR
    assert alerts["survivors"][0]["notice"] == "申請の期限まであと12日です"
    assert alerts["disclaimer"] == supports.DISCLAIMER
    # 一覧の行にも未申請の数が入る（画面が自分で数えない）
    assert res.json()["survivors"][0]["unapplied_count"] == 1


def test_一般の支援者の一覧には期限の知らせを渡さない(client, monkeypatch):
    # 支援制度を扱わない立場（要件定義書446行）。形は変えず、中身だけ空にする
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    monkeypatch.setattr(survivors, "list_survivors", lambda **kwargs: [survivor_row()])
    res = client.get("/api/survivors", headers=BEARER)
    assert res.status_code == 200
    alerts = res.json()["support_alerts"]
    assert alerts["count"] == 0
    assert alerts["survivors"] == []


def test_画面に支援の文言を書き写していない() -> None:
    # 断り書き・「次にすること」・期限の知らせは、APIが返すものだけを出す
    # （正本は services/supports.py。画面に書き写すとずれる）
    source = (
        Path(__file__).resolve().parents[1] / "02_app" / "frontend" / "f6.js"
    ).read_text(encoding="utf-8")
    assert supports.DISCLAIMER not in source
    assert supports.DEADLINE_PASSED_NOTICE not in source
    for text in supports.NEXT_ACTIONS.values():
        assert text not in source
    # 選択肢も画面に書かず、/api/public-config が返すものから作る
    # （「分からない」「未申請」のように画面の案内文と同じ言葉があるので、
    #  無いことを数えるのではなく、APIの選択肢を使っていることを確かめる）
    assert "choices().housing_damages" in source
    assert "choices().rebuild_intents" in source
    assert "choices().support_statuses" in source
    assert "choices().support_disclaimer" in source


def test_選択肢のAPIにF6の値と断り書きが入る(client):
    body = client.get("/api/public-config").json()["choices"]
    assert {item["value"] for item in body["housing_damages"]} == set(supports.HOUSING_DAMAGES)
    assert {item["value"] for item in body["rebuild_intents"]} == set(supports.REBUILD_INTENTS)
    assert [item["value"] for item in body["support_statuses"]] == list(supports.STATUSES)
    assert body["support_disclaimer"] == supports.DISCLAIMER
    assert body["support_deadline_soon_days"] == supports.DEADLINE_SOON_DAYS


# --- 組織につながっていない本人（追加修正②） ---


def test_組織がない本人に全国共通の制度が出る(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR, org_id="")
    nationwide = program_row("nationwide__kiso", org_id="")
    set_reads(monkeypatch, survivor=survivor_row(org_id=""))
    captured: dict = {}

    def list_programs(**kwargs):
        captured.update(kwargs)
        return [nationwide]

    monkeypatch.setattr(supports, "list_programs", list_programs)
    fake_ai(monkeypatch)

    res = client.post(
        f"/api/survivors/{SURVIVOR}/support-candidates", json=CANDIDATES_BODY, headers=BEARER
    )

    assert res.status_code == 200, res.text
    # 組織がないので、組織のIDは空で渡す（全国共通の制度だけを読む）
    assert captured["org_id"] == ""
    programs = res.json()["programs"]
    assert [item["program_id"] for item in programs] == ["nationwide__kiso"]
    assert programs[0]["nationwide"] is True


def test_組織がない本人も全国共通の制度を記録できる(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR, org_id="")
    nationwide = program_row("nationwide__kiso", org_id="")
    set_reads(monkeypatch, survivor=survivor_row(org_id=""), programs=[nationwide])
    written = catch_writes(monkeypatch)
    body = {
        **CREATE_BODY,
        "items": [{"program_id": "nationwide__kiso", "reason": "全壊のため"}],
    }

    res = client.post(f"/api/survivors/{SURVIVOR}/supports", json=body, headers=BEARER)

    assert res.status_code == 201, res.text
    assert written[0]["org_id"] == ""
    assert written[0]["program_id"] == "nationwide__kiso"
    # 全国共通の制度は、組織につないだあとも同じ記録のIDになる
    assert written[0]["support_id"] == supports.support_id_for(
        org_id="", survivor_id=SURVIVOR, program_id="nationwide__kiso"
    )


# --- 支援の一覧（使いやすさの改修の計画.md 6-2） ---


def test_制度の一覧と記録した支援の一覧はトークンが無ければ401(client):
    assert client.get("/api/programs").status_code == 401
    assert client.get(f"/api/survivors/{SURVIVOR}/supports").status_code == 401


def test_一般の支援者は制度の一覧を見られない(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_reads(monkeypatch)
    assert client.get("/api/programs", headers=BEARER).status_code == 403
    assert client.get(f"/api/survivors/{SURVIVOR}/supports", headers=BEARER).status_code == 403


def test_本人は自分の組織の制度と全国共通の制度を受け取る(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR, org_id=ORG)
    seen = {}

    def fake_list(**kwargs):
        seen.update(kwargs)
        return [program_row(), program_row("nationwide__kiso", org_id="", program_name="全国の制度")]

    monkeypatch.setattr(supports, "list_programs", fake_list)
    res = client.get("/api/programs", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    # 組織は stg_users から読み直した値（要求には書かれていない）
    assert seen["org_id"] == ORG
    assert [item["program_id"] for item in body["programs"]] == [PROGRAM, "nationwide__kiso"]
    assert [item["nationwide"] for item in body["programs"]] == [False, True]
    assert body["count"] == 2
    assert body["disclaimer"] == supports.DISCLAIMER
    # 一覧には申し込み先・期限の決まり・書類・出典まで入る
    first = body["programs"][0]
    for key in ("conditions", "benefit", "apply_to", "deadline_rule", "documents", "source_url"):
        assert first[key]


def test_専門職も自分の組織の制度と全国共通の制度を受け取る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL, org_id=ORG)
    seen = {}
    monkeypatch.setattr(
        supports, "list_programs", lambda **kwargs: seen.update(kwargs) or [program_row()]
    )
    res = client.get("/api/programs", headers=BEARER)
    assert res.status_code == 200
    assert seen["org_id"] == ORG


def test_組織がない本人は全国共通の制度だけを読む(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR, org_id=None)
    seen = {}
    monkeypatch.setattr(supports, "list_programs", lambda **kwargs: seen.update(kwargs) or [])
    res = client.get("/api/programs", headers=BEARER)
    assert res.status_code == 200
    assert seen["org_id"] == ""
    assert res.json()["programs"] == []


def test_制度の読みは全国共通と自分の組織だけに絞る(monkeypatch):
    """`list_programs` の SQL が、ほかの組織の制度を読まないこと（パラメータで渡す）。"""
    captured = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [program_row(), program_row("nationwide__kiso", org_id="", program_name="全国の制度")]

    monkeypatch.setattr(supports.bq, "query", fake_query)
    rows = supports.list_programs(org_id=ORG)
    assert "WHERE IFNULL(org_id, '') = '' OR org_id = @org_id" in captured["sql"]
    assert captured["params"] == {"org_id": ORG}
    assert ORG not in captured["sql"]
    assert {row["org_id"] for row in rows} == {ORG, ""}


def test_制度の一覧ではGeminiを呼ばない(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_reads(monkeypatch)

    def fail(**kwargs):
        raise AssertionError("一覧で AI を呼んではいけません")

    monkeypatch.setattr(support_ai, "suggest", fail)
    assert client.get("/api/programs", headers=BEARER).status_code == 200
    assert client.get(f"/api/survivors/{SURVIVOR}/supports", headers=BEARER).status_code == 200


def test_制度の一覧はBigQueryを呼べないとき503(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)

    def unavailable(**kwargs):
        raise bq.BigQueryUnavailable("読めない")

    monkeypatch.setattr(supports, "list_programs", unavailable)
    assert client.get("/api/programs", headers=BEARER).status_code == 503


def test_本人は記録した支援の一覧を受け取る(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_reads(monkeypatch, existing=[support_row(days_to_deadline=2)])
    res = client.get(f"/api/survivors/{SURVIVOR}/supports", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    item = body["supports"][0]
    assert item["support_id"] == SUPPORT_ID
    assert item["status_label"] == "未申請"
    assert item["next_action"] == supports.NEXT_ACTIONS[supports.STATUS_NOT_APPLIED]
    assert item["days_to_deadline"] == 2
    assert item["deadline_notice"] == "申請の期限まであと2日です"
    assert body["alert_days"] == supports.SURVIVOR_ALERT_DAYS == 3
    assert body["disclaimer"] == supports.DISCLAIMER
    # 誰が変えたかは本人には返さない
    assert "updated_by" not in item


def test_ほかの人の支援の一覧は404(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_reads(monkeypatch, survivor=survivor_row(self_user_id="uid-someone-else"))

    def fail(**kwargs):
        raise AssertionError("見られない記録の支援を読んではいけません")

    monkeypatch.setattr(supports, "list_for_survivor", fail)
    res = client.get(f"/api/survivors/{SURVIVOR}/supports", headers=BEARER)
    assert res.status_code == 404


def test_ほかの組織の記録の支援の一覧は404(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_reads(monkeypatch, survivor=survivor_row(org_id=OTHER_ORG))
    res = client.get(f"/api/survivors/{SURVIVOR}/supports", headers=BEARER)
    assert res.status_code == 404


LIST_BODY = {"from_list": True, "items": [{"program_id": PROGRAM}]}


def test_一覧から記録するときは被害の程度と意向を空にできる(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_reads(monkeypatch)
    written = catch_writes(monkeypatch)
    res = client.post(f"/api/survivors/{SURVIVOR}/supports", json=LIST_BODY, headers=BEARER)
    assert res.status_code == 201, res.text
    assert len(written) == 1
    row = written[0]
    assert row["status"] == supports.STATUS_NOT_APPLIED
    assert row["housing_damage"] is None
    assert row["rebuild_intent"] is None
    assert row["reason"] is None
    assert res.json()["supports"][0]["status_label"] == "未申請"


def test_一覧からでなければ被害の程度は今までどおり必須(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_reads(monkeypatch)
    written = catch_writes(monkeypatch)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/supports",
        json={"items": [{"program_id": PROGRAM}]},
        headers=BEARER,
    )
    assert res.status_code == 400
    assert written == []


def test_一覧からでも選択肢の外の被害の程度は400(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_reads(monkeypatch)
    written = catch_writes(monkeypatch)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/supports",
        json={**LIST_BODY, "housing_damage": "ぜんかい"},
        headers=BEARER,
    )
    assert res.status_code == 400
    assert written == []


def test_一覧からでも登録されていない制度は記録しない(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_reads(monkeypatch)
    written = catch_writes(monkeypatch)
    res = client.post(
        f"/api/survivors/{SURVIVOR}/supports",
        json={"from_list": True, "items": [{"program_id": "org_suzu__unknown"}]},
        headers=BEARER,
    )
    assert res.status_code == 400
    assert written == []


def test_一覧からはすでに記録した制度を記録し直さない(client, monkeypatch):
    """記録し直すと、申請中などの状況が未申請に戻ってしまうため。"""
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_reads(monkeypatch, existing=[support_row(status=supports.STATUS_APPLYING)])
    written = catch_writes(monkeypatch)
    res = client.post(f"/api/survivors/{SURVIVOR}/supports", json=LIST_BODY, headers=BEARER)
    assert res.status_code == 400
    assert "もう記録してあります" in res.json()["detail"]
    assert written == []


def test_一般の支援者は一覧からも記録できない(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    res = client.post(f"/api/survivors/{SURVIVOR}/supports", json=LIST_BODY, headers=BEARER)
    assert res.status_code == 403
