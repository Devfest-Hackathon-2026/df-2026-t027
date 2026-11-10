"""F4（悪化の兆しと訪問の順番）の API のテスト（開発手順書 Step 7）。

Google Cloud にも Firebase にも Gemini にも接続しない。確かめるのは次の5つ。

- **度合いを出すAPIは画面に無い**（記録を保存した後、応答を返す前にサーバーの中で判断する。10-5の注）
- **判断が失敗しても、記録の保存は成功のまま**（書き直させない）
- **度合いを変えられるのは専門職だけ**で、理由が必ず残る（要件定義書F4）
- **ホームの「対応が必要な人」は一覧と同じ読みから作る**（読みを増やさない）
- **本人の応答には、度合いも訪問・見守りの記録の本文も入らない**（要件定義書F4の最後・10-3）
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

# 差し替える前の本物（判断が失敗したときの動きを確かめるテストで使う）
REAL_ASSESS_AND_SAVE = risk_ai.assess_and_save

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
OTHER_ORG = "org_suzu"
SURVIVOR = "sv_0001"
OTHER_SURVIVOR = "sv_0002"
VISIT = "vs_0001"
PRO_UID = "uid-pro-1"
VOL_UID = "uid-vol-1"
SELF_UID = "uid-self-1"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
BEARER = {"Authorization": "Bearer dummy-token"}

RAW_TEXT = "きのうから熱っぽい。ごはんは朝だけ。体育館が寒いと言っている。"
EVIDENCE = {"quote": "ごはんは朝だけ", "source_kind": "visit", "source_id": VISIT}


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
    """保存の後に走る判断は、明示して差し替えたテストだけで動かす。

    差し替えを忘れると BigQuery と Gemini を呼ぼうとするが、`assess_and_save` は
    例外を外に出さないので**気づけない**。既定では呼ばれたことだけを数える。
    """
    calls: list[dict] = []
    monkeypatch.setattr(risk_ai, "assess_and_save", lambda **kwargs: calls.append(kwargs))
    return calls


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
    """`int_survivor_list` の1行（悪化の兆しが「高い」状態）。"""
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
        "risk_level": "high",
        "risk_evidence": {"items": [EVIDENCE]},
        "risk_next_action": "今日のうちに訪問する",
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
        "assignee_user_ids": [],
        "assignee_count": 0,
        "never_visited": False,
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


def catch_assessment_saves(monkeypatch) -> list[dict]:
    saved: list[dict] = []
    monkeypatch.setattr(assessments, "save", lambda row: saved.append(row) or row)
    return saved


def assessment_url(survivor_id: str = SURVIVOR) -> str:
    return f"/api/survivors/{survivor_id}/assessment"


def override_body(**overrides) -> dict:
    body = {"level": "low", "reason": "本人に会って確かめた。食事はとれていた"}
    body.update(overrides)
    return body


# --- 度合いを出すAPIは画面に無い（要件定義書10-5の注） ---


def test_度合いを出すAPIは画面から呼べない(client, monkeypatch):
    # 判断は記録・相談の保存の直後にサーバーの中で走らせる。経路を作らない
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())

    res = client.post(f"/api/survivors/{SURVIVOR}/assess", json={}, headers=BEARER)

    assert res.status_code in (404, 405)


# --- 立場（要件定義書10-5の「使える人」） ---


def test_ログインしていないと度合いを変えられない(client):
    assert client.put(assessment_url(), json=override_body()).status_code == 401


def test_一般の支援者は度合いを変えられない(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))

    res = client.put(assessment_url(), json=override_body(), headers=BEARER)

    assert res.status_code == 403


def test_本人は度合いを変えられない(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))

    res = client.put(assessment_url(), json=override_body(), headers=BEARER)

    assert res.status_code == 403


def test_別の組織の記録の度合いは変えられない(client, monkeypatch):
    # 見る権限がないときは、存在しないときと同じ 404（要件定義書10-3）
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL, org_id=OTHER_ORG)
    set_survivor(monkeypatch, survivor_row())
    catch_assessment_saves(monkeypatch)

    res = client.put(assessment_url(), json=override_body(), headers=BEARER)

    assert res.status_code == 404


# --- 度合いを変える（要件定義書F4） ---


def test_理由を書かないと度合いを変えられない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    saved = catch_assessment_saves(monkeypatch)

    res = client.put(assessment_url(), json=override_body(reason="  "), headers=BEARER)

    assert res.status_code == 400
    assert saved == []


def test_決まっていない度合いは断る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    saved = catch_assessment_saves(monkeypatch)

    res = client.put(assessment_url(), json=override_body(level="とても低い"), headers=BEARER)

    assert res.status_code == 400
    assert saved == []


def test_専門職が度合いを変えると理由と前の根拠が残る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    saved = catch_assessment_saves(monkeypatch)

    res = client.put(assessment_url(), json=override_body(), headers=BEARER)

    assert res.status_code == 200
    assert len(saved) == 1
    assert saved[0]["level"] == "low"
    assert saved[0]["source"] == assessments.SOURCE_PROFESSIONAL
    assert saved[0]["override_reason"] == "本人に会って確かめた。食事はとれていた"
    # 根拠と次に必要な対応は前の判断から引き継ぐ（何を見て直したのか残す）
    assert saved[0]["evidence"] == {"items": [EVIDENCE]}
    assert saved[0]["next_action"] == "今日のうちに訪問する"

    risk = res.json()["risk"]
    assert risk["level"] == "low"
    assert risk["level_label"] == "低い"
    assert risk["source_label"] == "専門職が変えた"
    assert risk["evidence"] == [EVIDENCE]
    # 医療の診断ではないことを画面に出すための文言（要件定義書F4）
    assert "診断ではありません" in risk["disclaimer"]


def test_読み書きできないときは503(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())

    def fail(row):
        raise bq.BigQueryUnavailable("テスト")

    monkeypatch.setattr(assessments, "save", fail)

    res = client.put(assessment_url(), json=override_body(), headers=BEARER)

    assert res.status_code == 503


# --- 記録を保存した後、応答を返す前に判断する（要件定義書10-5の注） ---


def set_visit_save(monkeypatch) -> list[dict]:
    rows: list[dict] = []
    monkeypatch.setattr(visits, "save_visit", lambda row: rows.append(row) or row)
    return rows


def save_visit_body() -> dict:
    return {
        "raw_text": RAW_TEXT,
        "items": visits.clean_items({"condition": "熱っぽい"}),
        "ai_used": True,
    }


def test_記録を保存すると応答の前に悪化の兆しを判断する(
    client, monkeypatch, no_background_assessment
):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))
    rows = set_visit_save(monkeypatch)

    res = client.post(f"/api/survivors/{SURVIVOR}/visits", json=save_visit_body(), headers=BEARER)

    assert res.status_code == 201
    assert len(no_background_assessment) == 1
    call = no_background_assessment[0]
    # きっかけは保存した記録のID（raw_assessments.trigger_id）
    assert call["trigger_id"] == rows[0]["visit_id"]
    assert call["survivor"]["survivor_id"] == SURVIVOR
    assert call["created_by"] == VOL_UID
    assert call["created_role"] == users.ROLE_VOLUNTEER


def test_判断が失敗しても記録の保存は成功のまま(client, monkeypatch):
    # 判断は応答の前に走るので、失敗を外に出すと記録の保存まで失敗に見える。
    # 本物の `assess_and_save` で、判断の途中の読みが失敗したときも 201 になることを確かめる
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    set_visit_save(monkeypatch)
    monkeypatch.setattr(risk_ai, "assess_and_save", REAL_ASSESS_AND_SAVE)

    def broken_read(**kwargs):
        raise bq.BigQueryUnavailable("テスト")

    monkeypatch.setattr(risk_ai.visits, "list_visits", broken_read)

    res = client.post(f"/api/survivors/{SURVIVOR}/visits", json=save_visit_body(), headers=BEARER)

    assert res.status_code == 201


def test_整えるだけの処理では判断を走らせない(client, monkeypatch, no_background_assessment):
    # 保存していない文章で判断すると、採用しなかった案で一覧の順番が変わってしまう
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(
        visit_ai,
        "structure",
        lambda **kwargs: {
            "items": visits.clean_items({"condition": "熱っぽい"}),
            "location_proposal": None,
            "health_proposal": None,
        },
    )

    res = client.post(
        f"/api/survivors/{SURVIVOR}/visits/structure", json={"raw_text": RAW_TEXT}, headers=BEARER
    )

    assert res.status_code == 200
    assert no_background_assessment == []


# --- 一覧の順番と「対応が必要な人」 ---


def set_list(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(survivors, "list_survivors", lambda **kwargs: rows)


def test_一覧の1行に度合いと根拠が入る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_list(monkeypatch, [survivor_row()])

    body = client.get("/api/survivors", headers=BEARER).json()

    item = body["survivors"][0]
    assert item["risk_level"] == "high"
    assert item["risk_level_label"] == "高い"
    assert item["risk_evidence"] == [EVIDENCE]
    assert item["risk_next_action"] == "今日のうちに訪問する"
    # 最後の訪問からの日数も一覧に出す（要件定義書F4）
    assert item["days_since_last_visit"] == 3


def test_ホームの対応が必要な人は度合いが高い人だけ(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_list(
        monkeypatch,
        [
            survivor_row(),
            survivor_row(
                survivor_id=OTHER_SURVIVOR,
                full_name="鈴木 花子",
                risk_level="medium",
            ),
        ],
    )

    body = client.get("/api/survivors", headers=BEARER).json()

    attention = body["attention"]
    assert attention["count"] == 1
    assert [item["full_name"] for item in attention["survivors"]] == ["佐藤 太郎"]
    assert "診断ではありません" in attention["disclaimer"]


def test_一般の支援者のホームにも対応が必要な人が出る(client, monkeypatch):
    # 一覧は担当の人だけに絞られているので、そのまま使える
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_list(monkeypatch, [survivor_row(assignee_user_ids=[VOL_UID])])

    body = client.get("/api/survivors", headers=BEARER).json()

    assert body["attention"]["count"] == 1


def test_判断がまだ無い人は対応が必要な人に入らない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_list(monkeypatch, [survivor_row(risk_level=None, risk_evidence=None)])

    body = client.get("/api/survivors", headers=BEARER).json()

    assert body["attention"] == {
        "count": 0,
        "survivors": [],
        "disclaimer": assessments.DISCLAIMER,
    }


# --- 本人には返さない（要件定義書F4の最後・10-3） ---


def test_本人の詳細には度合いも記録の本文も入らない(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    row = survivor_row(self_user_id=SELF_UID)
    set_survivor(monkeypatch, row)
    # 本人のときは記録を読みに行かないので、読んだら落ちるようにしておく
    monkeypatch.setattr(
        visits,
        "list_visits",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("本人の画面で記録を読んではいけません")
        ),
    )

    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)

    assert res.status_code == 200
    survivor = res.json()["survivor"]
    assert "risk" not in survivor
    assert "visits" not in survivor
    assert "watch" not in survivor
    # 応答のどこにも度合い・根拠・記録の本文が出ていない
    text = res.text
    assert "high" not in text
    assert EVIDENCE["quote"] not in text
    assert "今日のうちに訪問する" not in text


def test_支援者の詳細には度合いと根拠が入る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())

    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)

    risk = res.json()["survivor"]["risk"]
    assert risk["level"] == "high"
    assert risk["evidence"] == [EVIDENCE]
    assert risk["source_label"] == "AIの判断"
    assert "診断ではありません" in risk["disclaimer"]


def test_本人の一覧は使えない(client, monkeypatch):
    # 一覧そのものが専門職と一般の支援者だけ（要件定義書10-5）
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)

    assert client.get("/api/survivors", headers=BEARER).status_code == 403


def test_画面の選択肢に度合いが入る(client, monkeypatch):
    # 値を画面に書くと二重管理になる（正本は services/assessments.py の LEVELS）
    body = client.get("/api/public-config").json()

    levels = body["choices"]["risk_levels"]
    assert [item["value"] for item in levels] == ["high", "medium", "low"]
    assert [item["label"] for item in levels] == ["高い", "中くらい", "低い"]
