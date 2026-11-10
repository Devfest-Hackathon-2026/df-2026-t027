"""F3（引き継ぎシート）の API のテスト（開発手順書 Step 9-3）。

Google Cloud にも Firebase にも Gemini にも接続しない。確かめるのは次の6つ。

- **案を作る処理は保存しない**（作った人が確かめてから保存する。要件定義書F3・10-5）
- **作れるのは専門職と担当の一般の支援者だけ**（本人は 403。要件定義書F3）
- 担当でない一般の支援者は 404（見る権限のない記録は存在しないときと同じ。10-3）
- 保存するときに**載せた健康・介護の情報の版を残す**（要件定義書7-3-1）
- Gemini を呼べなくても、**手で書いて保存できる**（作業が止まらない）
- 詳細の応答に、保存した引き継ぎシートが入る（印刷の画面から開けるようにする）
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
    handover_ai,
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
SURVIVOR = "sv_0001"
HANDOVER = "ho_0001"
PRO_UID = "uid-pro-1"
VOL_UID = "uid-vol-1"
OTHER_VOL_UID = "uid-vol-2"
SELF_UID = "uid-self-1"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
BEARER = {"Authorization": "Bearer dummy-token"}

DESTINATION = "輪島中央 福祉避難所"
SUMMARY = "11月に入って食事の量が減り、血圧の薬が切れた日があった。"
CAUTIONS = "夜間にトイレで何度も起きるため、入口に近い部屋が望ましい。"


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
        "last_visited_at": NOW,
        "days_since_last_visit": 3,
        "visits_30d": 1,
        "pending_proposals": 0,
        "open_consultations": 0,
        "max_open_urgency": None,
        "assignee_user_ids": [VOL_UID],
        "assignee_count": 1,
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


def handover_stg_row(**overrides) -> dict:
    row = {
        "handover_id": HANDOVER,
        "org_id": ORG,
        "survivor_id": SURVIVOR,
        "destination": DESTINATION,
        "health_version": 2,
        "summary": SUMMARY,
        "cautions": CAUTIONS,
        "ai_used": True,
        "created_at": NOW,
        "created_by": PRO_UID,
        "created_role": "professional",
    }
    row.update(overrides)
    return row


def set_survivor(monkeypatch, row: dict | None, *, handover_rows=None) -> None:
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: row)
    monkeypatch.setattr(survivors, "location_history", lambda **kwargs: [])
    monkeypatch.setattr(assignments, "list_for_survivor", lambda **kwargs: [])
    monkeypatch.setattr(health, "fetch_latest", lambda **kwargs: health_row())
    monkeypatch.setattr(visits, "list_visits", lambda **kwargs: [])
    monkeypatch.setattr(consultations, "list_for_survivor", lambda **kwargs: [])
    monkeypatch.setattr(
        handovers,
        "list_for_survivor",
        lambda **kwargs: list(handover_rows if handover_rows is not None else []),
    )
    # 支援と申請の状況（F6。Step 10 で詳細の読みに足した）
    monkeypatch.setattr(
        supports,
        "list_for_survivor",
        lambda **kwargs: [],
    )


def catch_writes(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(handovers, "save", lambda row: written.append(row) or row)
    return written


def set_draft(monkeypatch, result=None) -> list[dict]:
    calls: list[dict] = []

    def run(**kwargs):
        calls.append(kwargs)
        return result or {
            "summary": SUMMARY,
            "cautions": CAUTIONS,
            "ai_used": True,
            "notice": None,
        }

    monkeypatch.setattr(handover_ai, "draft", run)
    return calls


def draft_url(survivor_id: str = SURVIVOR) -> str:
    return f"/api/survivors/{survivor_id}/handovers/draft"


def save_url(survivor_id: str = SURVIVOR) -> str:
    return f"/api/survivors/{survivor_id}/handovers"


def save_body(**overrides) -> dict:
    body = {
        "destination": DESTINATION,
        "summary": SUMMARY,
        "cautions": CAUTIONS,
        "health_version": 2,
        "ai_used": True,
    }
    body.update(overrides)
    return body


# --- ログインしていない要求 ---


def test_トークンなしの引き継ぎシートのAPIは401(client) -> None:
    assert client.post(draft_url(), json={"destination": DESTINATION}).status_code == 401
    assert client.post(save_url(), json=save_body()).status_code == 401


# --- 使える立場（要件定義書F3） ---


def test_本人は引き継ぎシートを作れない(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    assert (
        client.post(draft_url(), json={"destination": DESTINATION}, headers=BEARER).status_code
        == 403
    )
    assert client.post(save_url(), json=save_body(), headers=BEARER).status_code == 403


def test_担当でない一般の支援者は404(client, monkeypatch) -> None:
    # 見る権限のない記録は、存在しないときと同じ 404（要件定義書10-3）
    sign_in(monkeypatch, uid=OTHER_VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))
    set_draft(monkeypatch)
    res = client.post(draft_url(), json={"destination": DESTINATION}, headers=BEARER)
    assert res.status_code == 404


def test_担当の一般の支援者は作れる(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))
    set_draft(monkeypatch)
    res = client.post(draft_url(), json={"destination": DESTINATION}, headers=BEARER)
    assert res.status_code == 200


# --- 案を作る（保存しない） ---


def test_案を作る処理は保存しない(client, monkeypatch) -> None:
    # `handovers.save` を呼んだら BigQuery の差し替えで落ちる
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_draft(monkeypatch)
    res = client.post(draft_url(), json={"destination": DESTINATION}, headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["draft"]["summary"] == SUMMARY
    assert body["draft"]["cautions"] == CAUTIONS
    # 載せる健康・介護の情報の版を、保存のときに送れるように返す
    assert body["health_version"] == 2
    assert body["health"]["fields"]["medical_needs"] == ["透析"]
    # AIの案であることを必ず画面に出す（要件定義書F3）
    assert body["ai_disclaimer"] == handovers.AI_DISCLAIMER


def test_案を作るときに健康の情報と直近の記録を渡す(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(
        visits,
        "list_visits",
        lambda **kwargs: [{"visit_id": "vs_0001", "visited_at": NOW, "condition": "熱っぽい"}],
    )
    calls = set_draft(monkeypatch)
    client.post(draft_url(), json={"destination": DESTINATION}, headers=BEARER)
    assert calls[0]["destination"] == DESTINATION
    assert calls[0]["health_row"]["medical_needs"] == ["透析"]
    assert calls[0]["recent_visits"][0]["visit_id"] == "vs_0001"


def test_移る先を書かないと案を作れない(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_draft(monkeypatch)
    res = client.post(draft_url(), json={"destination": "  "}, headers=BEARER)
    assert res.status_code == 400


def test_Geminiを呼べなくても案の画面に進める(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_draft(
        monkeypatch,
        result={
            "summary": "",
            "cautions": "",
            "ai_used": False,
            "notice": handover_ai.AI_UNAVAILABLE_NOTICE,
        },
    )
    res = client.post(draft_url(), json={"destination": DESTINATION}, headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["draft"]["ai_used"] is False
    assert body["draft"]["notice"]


# --- 保存する ---


def test_保存すると版と移る先が残る(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    written = catch_writes(monkeypatch)
    res = client.post(save_url(), json=save_body(), headers=BEARER)
    assert res.status_code == 201
    assert len(written) == 1
    assert written[0]["destination"] == DESTINATION
    assert written[0]["health_version"] == 2
    assert written[0]["survivor_id"] == SURVIVOR
    assert written[0]["created_by"] == PRO_UID
    assert written[0]["created_role"] == users.ROLE_PROFESSIONAL
    body = res.json()
    assert body["handover"]["destination"] == DESTINATION
    # 印刷の画面にそのまま出せるように、健康・介護の情報も返す
    assert body["health"]["fields"]["chronic_diseases"] == ["高血圧"]


def test_手で書いたシートも保存できる(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    written = catch_writes(monkeypatch)
    res = client.post(save_url(), json=save_body(ai_used=False), headers=BEARER)
    assert res.status_code == 201
    assert written[0]["ai_used"] is False


def test_要約のないシートは400(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    catch_writes(monkeypatch)
    res = client.post(save_url(), json=save_body(summary=""), headers=BEARER)
    assert res.status_code == 400


# --- 詳細の応答 ---


def test_詳細の応答に引き継ぎシートが入る(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(), handover_rows=[handover_stg_row()])
    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)
    assert res.status_code == 200
    items = res.json()["survivor"]["handovers"]
    assert [item["handover_id"] for item in items] == [HANDOVER]
    assert items[0]["destination"] == DESTINATION


def test_本人の詳細にも引き継ぎシートが入る(client, monkeypatch) -> None:
    # 要件定義書7-1「引き継ぎシート：本人、同じ組織の専門職、担当の一般の支援者」
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(), handover_rows=[handover_stg_row()])
    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)
    assert res.status_code == 200
    items = res.json()["survivor"]["handovers"]
    assert [item["handover_id"] for item in items] == [HANDOVER]


def test_保存したあとに健康の情報が変わっていたら知らせる(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(
        monkeypatch, survivor_row(), handover_rows=[handover_stg_row(health_version=1)]
    )
    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)
    item = res.json()["survivor"]["handovers"][0]
    assert item["health_version_changed"] is True
    assert item["health_version_notice"]


# --- 文言の正本（画面に書き写さない。Step 8 と同じ形） ---


def test_AIの断り書きはAPIが返す(client) -> None:
    # 文言の正本は services/handovers.py。画面に書き写すとずれる
    choices = client.get("/api/public-config").json()["choices"]
    assert choices["handover_ai_disclaimer"] == handovers.AI_DISCLAIMER


def test_画面に断り書きを書き写していない() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "02_app" / "frontend" / "handovers.js"
    ).read_text(encoding="utf-8")
    assert "AIが作った案です" not in source


# --- 引き継ぎシートを関わる人が見られる（追加修正⑪） ---

OLD_ORG = "org_old"
NEW_ORG = "org_new"


def list_url(survivor_id: str = SURVIVOR) -> str:
    return f"/api/survivors/{survivor_id}/handovers"


def test_シートは被災者のIDで読み組織では絞らない(monkeypatch) -> None:
    captured: dict = {}

    def fake_query(sql, params=None):
        captured.update(sql=sql, params=params)
        return []

    monkeypatch.setattr(handovers.bq, "query", fake_query)
    handovers.list_for_survivor(survivor_id=SURVIVOR, org_id=ORG)
    assert captured["params"] == {"survivor_id": SURVIVOR}
    assert "org_id = @org_id" not in captured["sql"]
    # 組織がなくても読む（組織がない本人の自分のシート）
    handovers.list_for_survivor(survivor_id=SURVIVOR, org_id="")
    assert captured["params"] == {"survivor_id": SURVIVOR}


def test_組織がない本人が自分の引き継ぎシートを見られる(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR, org_id="")
    sheet = handover_stg_row(org_id=OLD_ORG)
    set_survivor(monkeypatch, survivor_row(org_id=""), handover_rows=[sheet])

    res = client.get(list_url(), headers=BEARER)
    assert res.status_code == 200, res.text
    body = res.json()
    assert [item["handover_id"] for item in body["handovers"]] == [HANDOVER]
    # 印刷の画面にそのまま出せるように、お名前といまの健康の情報も返す
    assert body["survivor"]["full_name"] == "佐藤 太郎"
    assert body["health"]["version"] == 2

    # 詳細の応答にも入る
    detail = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()
    assert [item["handover_id"] for item in detail["survivor"]["handovers"]] == [HANDOVER]


@pytest.mark.parametrize(
    ("uid", "role"),
    [(SELF_UID, users.ROLE_SURVIVOR), ("uid-pro-new", users.ROLE_PROFESSIONAL)],
)
def test_組織を変えたあとも前の組織で作ったシートを本人と新しい組織の専門職が見られる(
    client, monkeypatch, uid, role
) -> None:
    # 記録のいまの組織は NEW_ORG。シートは OLD_ORG のときに作った
    sign_in(monkeypatch, uid=uid, role=role, org_id=NEW_ORG)
    set_survivor(
        monkeypatch,
        survivor_row(org_id=NEW_ORG, version=2, assignee_user_ids=[]),
        handover_rows=[handover_stg_row(org_id=OLD_ORG)],
    )
    res = client.get(list_url(), headers=BEARER)
    assert res.status_code == 200
    assert [item["handover_id"] for item in res.json()["handovers"]] == [HANDOVER]


@pytest.mark.parametrize(
    ("uid", "role"),
    [(PRO_UID, users.ROLE_PROFESSIONAL), (VOL_UID, users.ROLE_VOLUNTEER)],
)
def test_前の組織の専門職と担当を外れた支援者からは見えない(client, monkeypatch, uid, role) -> None:
    sign_in(monkeypatch, uid=uid, role=role, org_id=OLD_ORG)
    set_survivor(
        monkeypatch,
        survivor_row(org_id=NEW_ORG, version=2, assignee_user_ids=[]),
        handover_rows=[handover_stg_row(org_id=OLD_ORG)],
    )
    assert client.get(list_url(), headers=BEARER).status_code == 404
    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


def test_担当を外れた一般の支援者からは同じ組織でも見えない(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(
        monkeypatch,
        survivor_row(assignee_user_ids=[]),
        handover_rows=[handover_stg_row()],
    )
    assert client.get(list_url(), headers=BEARER).status_code == 404


def test_担当の一般の支援者はシートの一覧を見られる(client, monkeypatch) -> None:
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(), handover_rows=[handover_stg_row()])
    res = client.get(list_url(), headers=BEARER)
    assert res.status_code == 200
    assert len(res.json()["handovers"]) == 1


def test_印刷の画面に印刷するボタンが残っている() -> None:
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[1] / "02_app" / "frontend"
    html = (frontend / "index.html").read_text(encoding="utf-8")
    assert "印刷する（PDFにもできます）" in html
    assert 'id="handover-print"' in html
    assert "window.print()" in (frontend / "handovers.js").read_text(encoding="utf-8")


def test_本人のホームに引き継ぎシートの入口がある() -> None:
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[1] / "02_app" / "frontend"
    app_js = (frontend / "app.js").read_text(encoding="utf-8")
    actions = app_js.split("const SURVIVOR_ACTIONS = [", 1)[1].split("\n];", 1)[0]
    assert "引き継ぎシート" in actions
    assert "openHandoverList" in actions
    handovers_js = (frontend / "handovers.js").read_text(encoding="utf-8")
    # 記録の詳細を開かずに、シートの一覧のAPIを読む
    assert "/handovers`" in handovers_js
    assert 'id="screen-handover-list"' in (frontend / "index.html").read_text(encoding="utf-8")
