"""F1（被災者の記録と担当）の API のテスト（要件定義書F1・10-3・10-5、開発手順書 Step 5）。

Google Cloud にも Firebase にも接続しない。立場と担当で分かれる範囲・確認コード・担当の
同意の取り方だけを確かめる。

要件定義書10-3 が自動テストで求める4つのうち、ここで書けるもの：

- 本人がほかの人の記録を読めない
- 一般の支援者が担当でない人・担当を外れた人の記録を読めない
- 別の組織の記録を読めない
- 本人に悪化の兆しの度合いが返らない
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest
from fastapi.testclient import TestClient

from backend import auth
from backend.config import get_settings
from backend.main import app
from backend.services import (
    assignments,
    bq,
    consultations,
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

    for module in (bq, survivors.bq, assignments.bq, link_codes.bq, health.bq, visits.bq):
        monkeypatch.setattr(module, "query", fail, raising=False)
        monkeypatch.setattr(module, "query_one", fail, raising=False)
        monkeypatch.setattr(module, "insert", fail, raising=False)


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
    # 既定では支援者の一覧を空にしておく（必要なテストだけ上書きする）
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
        "contact": "090-0000-0000",
        "stage": "evacuation",
        "location_type": "shelter",
        "location_detail": "市民体育館",
        "self_user_id": None,
        "registered_via": "professional",
        "registered_at": NOW,
        "updated_at": NOW,
        "risk_level": "high",
        "risk_next_action": "今日のうちに訪問する",
        "risk_evidence": None,
        "risk_source": "ai",
        "risk_assessed_at": NOW,
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
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: row)
    monkeypatch.setattr(survivors, "location_history", lambda **kwargs: [])
    monkeypatch.setattr(assignments, "list_for_survivor", lambda **kwargs: [])
    monkeypatch.setattr(users, "list_by_role", lambda **kwargs: [])
    # 詳細は F3 の健康・介護の情報と F2 の記録も読む（Step 6 で足した）。
    # F8 の相談も読む（Step 8 で足した。本人と専門職だけ）
    monkeypatch.setattr(health, "fetch_latest", lambda **kwargs: None)
    monkeypatch.setattr(visits, "list_visits", lambda **kwargs: [])
    monkeypatch.setattr(consultations, "list_for_survivor", lambda **kwargs: [])
    # 引き継ぎシート（F3。Step 9 で詳細の読みに足した）
    monkeypatch.setattr(handovers, "list_for_survivor", lambda **kwargs: [])
    # 支援と申請の状況（F6。Step 10 で詳細の読みに足した）
    monkeypatch.setattr(supports, "list_for_survivor", lambda **kwargs: [])


# --- 一覧（GET /api/survivors） ---


def test_一覧はログインしていないと401(client):
    assert client.get("/api/survivors").status_code == 401


def test_本人は一覧を使えない(client, monkeypatch):
    # 要件定義書10-5：F1の一覧を使えるのは専門職と一般の支援者だけ
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    assert client.get("/api/survivors", headers=BEARER).status_code == 403


def test_登録がまだの人は一覧を使えない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=None)
    assert client.get("/api/survivors", headers=BEARER).status_code == 403


def test_専門職は組織の一覧を読める(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(survivors, "list_survivors", lambda **kwargs: [survivor_row()])

    res = client.get("/api/survivors", headers=BEARER)

    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 1
    assert body["survivors"][0]["full_name"] == "佐藤 太郎"


def test_一覧に連絡先を返さない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(survivors, "list_survivors", lambda **kwargs: [survivor_row()])

    body = client.get("/api/survivors", headers=BEARER).json()

    assert "090-0000-0000" not in str(body)


def test_一般の支援者の一覧は自分が担当の人だけに絞られる(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    captured: dict = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(survivors, "list_survivors", fake_list)

    client.get("/api/survivors", headers=BEARER)

    assert captured["assignee_user_id"] == VOL_UID


def test_一般の支援者は担当の絞り込みを他人に変えられない(client, monkeypatch):
    # 要求に書かれた値で範囲を広げられないこと（要件定義書10-3）
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    captured: dict = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(survivors, "list_survivors", fake_list)

    client.get("/api/survivors?assignee=uid-vol-9", headers=BEARER)

    assert captured["assignee_user_id"] == VOL_UID


def test_一覧は組織を要求から受け取らない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    captured: dict = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(survivors, "list_survivors", fake_list)

    client.get(f"/api/survivors?org_id={OTHER_ORG}", headers=BEARER)

    assert captured["org_id"] == ORG


def test_一覧の絞り込みの値が正しくなければ400(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)

    res = client.get("/api/survivors?stage=unknown", headers=BEARER)

    assert res.status_code == 400


def test_一覧は氏名と年齢が近い組を専門職に知らせる(client, monkeypatch):
    # 要件定義書F1：本人が登録したときに近い記録があれば、専門職に知らせる
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(
        survivors,
        "list_survivors",
        lambda **kwargs: [
            survivor_row(survivor_id="sv_a", full_name="佐藤太郎", age=78),
            survivor_row(survivor_id="sv_b", full_name="佐藤 太郎", age=79, registered_via="self"),
            survivor_row(survivor_id="sv_c", full_name="鈴木花子", age=70),
        ],
    )

    body = client.get("/api/survivors", headers=BEARER).json()

    groups = body["possible_duplicates"]
    assert len(groups) == 1
    assert sorted(groups[0]["survivor_ids"]) == ["sv_a", "sv_b"]


def test_一覧に支援者の一覧が入る(client, monkeypatch):
    # 「担当で絞り込む」の選択肢に名前を出すため（要件定義書F1の絞り込み）
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(survivors, "list_survivors", lambda **kwargs: [])
    monkeypatch.setattr(
        users, "list_by_role", lambda **kwargs: [{"user_id": VOL_UID, "display_name": "山本さん"}]
    )

    body = client.get("/api/survivors", headers=BEARER).json()

    assert body["supporters"] == [{"user_id": VOL_UID, "display_name": "山本さん"}]


def test_一般の支援者には支援者の一覧を返さない(client, monkeypatch):
    # 一般の支援者は自分の担当だけを見るので、絞り込みも要らない
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    monkeypatch.setattr(survivors, "list_survivors", lambda **kwargs: [])

    def must_not_read(**kwargs):
        raise AssertionError("一般の支援者には、ほかの支援者の一覧を読まない")

    monkeypatch.setattr(users, "list_by_role", must_not_read)

    body = client.get("/api/survivors", headers=BEARER).json()

    assert body["supporters"] == []


def test_一般の支援者には重複の知らせを出さない(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    monkeypatch.setattr(
        survivors,
        "list_survivors",
        lambda **kwargs: [
            survivor_row(survivor_id="sv_a", full_name="佐藤太郎", age=78),
            survivor_row(survivor_id="sv_b", full_name="佐藤太郎", age=78),
        ],
    )

    body = client.get("/api/survivors", headers=BEARER).json()

    assert body["possible_duplicates"] == []


# --- 登録（POST /api/survivors） ---

NEW_SURVIVOR = {
    "full_name": "田中 花子",
    "age": 81,
    "household_size": 1,
    "stage": "evacuation",
    "location_type": "shelter",
    "location_detail": "中学校体育館",
}


def test_専門職以外は登録できない(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    res = client.post("/api/survivors", headers=BEARER, json=NEW_SURVIVOR)
    assert res.status_code == 403


def stub_issue(monkeypatch) -> None:
    monkeypatch.setattr(
        link_codes,
        "issue_code",
        lambda **kwargs: link_codes.IssuedCode(
            code="ABCD2345",
            code_hash="sha256$x",
            survivor_id=kwargs["survivor_id"],
            expires_at=NOW,
        ),
    )


def test_専門職の登録で確認コードが返る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(survivors, "find_similar", lambda **kwargs: [])
    monkeypatch.setattr(survivors, "insert_survivor", lambda row: row)
    stub_issue(monkeypatch)

    res = client.post("/api/survivors", headers=BEARER, json=NEW_SURVIVOR)

    assert res.status_code == 201
    assert res.json()["link_code"]["code"] == "ABCD2345"


def test_登録した記録は専門職が作ったことになる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    captured: dict = {}

    def fake_insert(row):
        captured.update(row)
        return row

    monkeypatch.setattr(survivors, "find_similar", lambda **kwargs: [])
    monkeypatch.setattr(survivors, "insert_survivor", fake_insert)
    stub_issue(monkeypatch)

    client.post("/api/survivors", headers=BEARER, json=NEW_SURVIVOR)

    assert captured["registered_via"] == survivors.VIA_PROFESSIONAL
    assert captured["org_id"] == ORG
    assert captured["created_by"] == PRO_UID
    assert captured["self_user_id"] is None
    assert captured["version"] == 1


def test_入力が足りなければ確認コードを発行する前に400(client, monkeypatch):
    # 入力の検査は、確認コードを発行する前に行う。順番が逆だと、検査で落ちるたびに
    # 使われないコードの行が raw_link_codes に残る（issue_code は差し替えない。
    # 呼ばれたら BigQuery を触って AssertionError になるので、呼ばれないことが分かる）
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(survivors, "find_similar", lambda **kwargs: [])

    res = client.post("/api/survivors", headers=BEARER, json={**NEW_SURVIVOR, "full_name": ""})

    assert res.status_code == 400


def test_近い記録があれば409で候補を返す(client, monkeypatch):
    # 候補には登録日時（datetime）が入る。HTTPException の本文は FastAPI の変換を通らず
    # **素の json.dumps** になるため、日時をそのまま入れると 500 になる（実機で発生）
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(
        survivors,
        "find_similar",
        lambda **kwargs: [
            {
                "survivor_id": SURVIVOR,
                "full_name": "田中花子",
                "age": 81,
                "registered_at": NOW,
            }
        ],
    )

    res = client.post("/api/survivors", headers=BEARER, json=NEW_SURVIVOR)

    assert res.status_code == 409
    similar = res.json()["detail"]["similar"][0]
    assert similar["survivor_id"] == SURVIVOR
    assert similar["registered_at"].startswith("2026-11-08")


def test_近い記録があっても確かめれば登録できる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(
        survivors, "find_similar", lambda **kwargs: [{"survivor_id": SURVIVOR}]
    )
    monkeypatch.setattr(survivors, "insert_survivor", lambda row: row)
    stub_issue(monkeypatch)

    res = client.post(
        "/api/survivors", headers=BEARER, json={**NEW_SURVIVOR, "acknowledged_similar": True}
    )

    assert res.status_code == 201


# --- 詳細（GET /api/survivors/{id}） ---


def test_専門職は同じ組織の詳細を読める(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())

    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)

    assert res.status_code == 200
    assert res.json()["survivor"]["survivor_id"] == SURVIVOR


def test_専門職は別の組織の詳細を読めない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(org_id=OTHER_ORG))

    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


def test_本人は自分の詳細を読める(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))

    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 200


def test_本人は他人の詳細を読めない(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id="uid-self-9"))

    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


def test_本人には悪化の兆しの度合いを返さない(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))

    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()

    assert "risk" not in body["survivor"]
    assert "high" not in str(body)


def test_一般の支援者は担当の詳細を読める(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))

    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 200


def test_一般の支援者は担当でない人の詳細を読めない(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[]))

    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


def test_担当を外れた支援者は詳細を読めない(client, monkeypatch):
    # 外すと stg_active_assignments から消え、assignee_user_ids に入らなくなる
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=["uid-vol-9"]))

    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


def test_無い記録は404(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, None)

    assert client.get("/api/survivors/sv_none", headers=BEARER).status_code == 404


def test_詳細に担当と居場所の履歴が入る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(
        assignments,
        "list_for_survivor",
        lambda **kwargs: [
            {
                "assignment_id": "as_1",
                "supporter_user_id": VOL_UID,
                "status": assignments.STATUS_PENDING,
                "assigned_at": NOW,
                "consented_at": None,
                "consent_type": None,
                "removed_at": None,
            }
        ],
    )
    monkeypatch.setattr(
        survivors,
        "location_history",
        lambda **kwargs: [{"version": 1, "stage": "evacuation", "location_type": "shelter"}],
    )
    monkeypatch.setattr(
        users,
        "list_by_role",
        lambda **kwargs: [
            {"user_id": VOL_UID, "display_name": "山本さん"},
            {"user_id": "uid-vol-2", "display_name": "田村さん"},
        ],
    )

    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()

    assert body["assignments"][0]["assignment_id"] == "as_1"
    assert body["assignments"][0]["supporter_name"] == "山本さん"
    assert len(body["location_history"]) == 1
    # すでに担当になっている人（承認待ちも含む）は、候補に出さない
    assert [item["user_id"] for item in body["supporter_candidates"]] == ["uid-vol-2"]


def test_詳細の読みは同時に走る(client, monkeypatch):
    """担当・居場所の履歴・健康の情報・記録・相談・引き継ぎシート・支援・支援者の名前を並行に読む
    （要確認19の性能の対策）。

    BigQuery の読みは1回1秒前後かかる。順に読むと詳細の1画面で積み上がるため、
    記録を読んだあとの8つは同時に走らせる（Step 10 で支援と申請の状況が1つ増えた）。

    8人そろうまで進めない関所（Barrier）を置き、**順番に走っていたら最初の1つで
    時間切れになる**形にしてある（時間を測る形にすると、遅い環境で不安定になるため）。
    """
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: survivor_row())

    barrier = threading.Barrier(8, timeout=5)

    def wait_then(result):
        def run(*args, **kwargs):
            barrier.wait()
            return result

        return run

    monkeypatch.setattr(assignments, "list_for_survivor", wait_then([]))
    monkeypatch.setattr(survivors, "location_history", wait_then([]))
    monkeypatch.setattr(users, "list_by_role", wait_then([]))
    monkeypatch.setattr(health, "fetch_latest", wait_then(None))
    monkeypatch.setattr(visits, "list_visits", wait_then([]))
    monkeypatch.setattr(consultations, "list_for_survivor", wait_then([]))
    monkeypatch.setattr(handovers, "list_for_survivor", wait_then([]))
    monkeypatch.setattr(supports, "list_for_survivor", wait_then([]))

    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)

    assert res.status_code == 200


def test_一覧の2つの読みは同時に走る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    barrier = threading.Barrier(2, timeout=5)

    def wait_then(result):
        def run(*args, **kwargs):
            barrier.wait()
            return result

        return run

    monkeypatch.setattr(survivors, "list_survivors", wait_then([]))
    monkeypatch.setattr(users, "list_by_role", wait_then([]))

    res = client.get("/api/survivors", headers=BEARER)

    assert res.status_code == 200


def test_一般の支援者の画面に利用者のIDを出さない(client, monkeypatch):
    # ブラウザでしか出なかった不具合：名前が引けず、画面が supporter_user_id に落ちていた
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))
    monkeypatch.setattr(
        assignments,
        "list_for_survivor",
        lambda **kwargs: [
            {
                "assignment_id": "as_1",
                "supporter_user_id": VOL_UID,
                "status": "active",
                "removed_at": None,
            }
        ],
    )
    monkeypatch.setattr(
        users,
        "names_by_ids",
        lambda **kwargs: [{"user_id": VOL_UID, "display_name": "山本さん"}],
    )

    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()

    assert body["assignments"][0]["supporter_name"] == "山本さん"


def test_一般の支援者には組織の利用者の一覧を読まない(client, monkeypatch):
    # 自分の担当だけを見る立場なので、必要な人の名前だけを引く（一覧の絞り込みと同じ方針）
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))

    def must_not_read(**kwargs):
        raise AssertionError("一般の支援者には、組織の利用者の一覧を読まない")

    monkeypatch.setattr(users, "list_by_role", must_not_read)

    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 200


def test_専門職が書いた記録に名前が出る(client, monkeypatch):
    # 要件定義書594行：訪問の記録は「記録した人と立場」を見せる。
    # 名前の辞書を一般の支援者だけで作っていたため、専門職の記録の名前が空になっていた
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(
        users,
        "list_by_role",
        lambda **kwargs: [
            {"user_id": PRO_UID, "display_name": "保健師A", "role": users.ROLE_PROFESSIONAL},
            {"user_id": VOL_UID, "display_name": "山本さん", "role": users.ROLE_VOLUNTEER},
        ],
    )
    monkeypatch.setattr(
        visits,
        "list_visits",
        lambda **kwargs: [
            {
                "visit_id": "vs_1",
                "version": 1,
                "recorded_by": PRO_UID,
                "recorded_role": users.ROLE_PROFESSIONAL,
                "proposal_status": "none",
            }
        ],
    )

    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()

    assert body["survivor"]["visits"][0]["recorded_by_name"] == "保健師A"
    # 専門職は担当の候補に出さない（担当になれるのは一般の支援者だけ）
    assert [item["user_id"] for item in body["supporter_candidates"]] == [VOL_UID]


def test_一般の支援者には担当の候補を返さない(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))

    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()

    assert body["supporter_candidates"] == []


# --- 居場所の変更（POST /api/survivors/{id}/location） ---

LOCATION_BODY = {
    "stage": "temporary",
    "location_type": "temporary_built",
    "location_detail": "仮設第1団地",
}


def test_専門職は居場所を変えられる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())
    monkeypatch.setattr(
        survivors, "change_location", lambda **kwargs: {**survivor_row(), "version": 2}
    )

    res = client.post(f"/api/survivors/{SURVIVOR}/location", headers=BEARER, json=LOCATION_BODY)

    assert res.status_code == 200
    assert res.json()["survivor"]["version"] == 2


def test_本人は自分の居場所を変えられる(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    monkeypatch.setattr(
        survivors, "change_location", lambda **kwargs: {**survivor_row(), "version": 2}
    )

    res = client.post(f"/api/survivors/{SURVIVOR}/location", headers=BEARER, json=LOCATION_BODY)

    assert res.status_code == 200


def test_一般の支援者は居場所を変えられない(client, monkeypatch):
    # 要件定義書F1：居場所は、専門職と本人が変えられる
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))

    res = client.post(f"/api/survivors/{SURVIVOR}/location", headers=BEARER, json=LOCATION_BODY)

    assert res.status_code == 403


def test_見られない記録の居場所は変えられない(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id="uid-self-9"))

    res = client.post(f"/api/survivors/{SURVIVOR}/location", headers=BEARER, json=LOCATION_BODY)

    assert res.status_code == 404


def test_居場所の値が正しくなければ400(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row())

    def fake_change(**kwargs):
        raise survivors.SurvivorValidationError("今の居場所の種類を選んでください")

    monkeypatch.setattr(survivors, "change_location", fake_change)

    res = client.post(
        f"/api/survivors/{SURVIVOR}/location",
        headers=BEARER,
        json={**LOCATION_BODY, "location_type": "バス"},
    )

    assert res.status_code == 400


# --- 担当の割り当て（POST /api/survivors/{id}/assignments） ---


def set_supporter(monkeypatch, *, role=users.ROLE_VOLUNTEER, org_id=ORG) -> None:
    """担当にする相手を読めるようにする（立場と組織の確認のため）。"""
    monkeypatch.setattr(
        users,
        "fetch_supporter",
        lambda user_id: {
            "user_id": user_id,
            "role": role,
            "org_id": org_id,
            "display_name": "山本さん",
        },
    )


def test_専門職は一般の支援者を担当にできる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    set_supporter(monkeypatch)
    monkeypatch.setattr(
        assignments,
        "create",
        lambda **kwargs: assignments.CreatedAssignment(
            assignment_id="as_1", supporter_user_id=VOL_UID, status=assignments.STATUS_PENDING
        ),
    )

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments",
        headers=BEARER,
        json={"supporter_user_id": VOL_UID},
    )

    assert res.status_code == 201
    assert res.json()["assignment"]["status"] == assignments.STATUS_PENDING


def test_本人のアカウントがあるときは口頭の同意を使わない(client, monkeypatch):
    # 本人がアカウントを持っているときは、本人が認めるまで担当にしない（F1）
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    set_supporter(monkeypatch)
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return assignments.CreatedAssignment(
            assignment_id="as_1", supporter_user_id=VOL_UID, status=assignments.STATUS_PENDING
        )

    monkeypatch.setattr(assignments, "create", fake_create)

    client.post(
        f"/api/survivors/{SURVIVOR}/assignments",
        headers=BEARER,
        json={"supporter_user_id": VOL_UID, "verbal_consent": True},
    )

    assert captured["verbal_consent"] is False


def test_本人のアカウントが無いなら口頭の同意が必須(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(self_user_id=None))
    set_supporter(monkeypatch)

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments",
        headers=BEARER,
        json={"supporter_user_id": VOL_UID, "verbal_consent": False},
    )

    assert res.status_code == 400


def test_口頭の同意を記録すればその場で有効になる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(self_user_id=None))
    set_supporter(monkeypatch)
    monkeypatch.setattr(
        assignments,
        "create",
        lambda **kwargs: assignments.CreatedAssignment(
            assignment_id="as_1", supporter_user_id=VOL_UID, status=assignments.STATUS_ACTIVE
        ),
    )

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments",
        headers=BEARER,
        json={"supporter_user_id": VOL_UID, "verbal_consent": True},
    )

    assert res.status_code == 201
    assert res.json()["assignment"]["status"] == assignments.STATUS_ACTIVE


def test_専門職を担当にはできない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    set_supporter(monkeypatch, role=users.ROLE_PROFESSIONAL)

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments",
        headers=BEARER,
        json={"supporter_user_id": "uid-pro-2"},
    )

    assert res.status_code == 400


def test_別の組織の支援者を担当にできない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    set_supporter(monkeypatch, org_id=OTHER_ORG)

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments",
        headers=BEARER,
        json={"supporter_user_id": VOL_UID},
    )

    assert res.status_code == 400


def test_居ない支援者を担当にできない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    monkeypatch.setattr(users, "fetch_supporter", lambda user_id: None)

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments",
        headers=BEARER,
        json={"supporter_user_id": "uid-none"},
    )

    assert res.status_code == 400


def test_本人は担当を割り当てられない(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments",
        headers=BEARER,
        json={"supporter_user_id": VOL_UID},
    )

    assert res.status_code == 403


# --- 承認と外す ---


def pending_assignment() -> dict:
    return {
        "assignment_id": "as_1",
        "supporter_user_id": VOL_UID,
        "status": assignments.STATUS_PENDING,
        "assigned_at": NOW,
        "consented_at": None,
        "consent_type": None,
        "removed_at": None,
    }


def test_本人は担当を認められる(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    monkeypatch.setattr(assignments, "list_for_survivor", lambda **kwargs: [pending_assignment()])
    called: dict = {}
    monkeypatch.setattr(assignments, "approve", lambda **kwargs: called.update(kwargs))

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments/as_1/approve", headers=BEARER, json={}
    )

    assert res.status_code == 200
    assert called["acted_by"] == SELF_UID
    assert called["supporter_user_id"] == VOL_UID


def test_専門職は担当を認められない(client, monkeypatch):
    # 要件定義書10-5：認めるのは本人だけ
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments/as_1/approve", headers=BEARER, json={}
    )

    assert res.status_code == 403


def test_無い割り当てを認めようとすると404(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    monkeypatch.setattr(assignments, "list_for_survivor", lambda **kwargs: [])

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments/as_9/approve", headers=BEARER, json={}
    )

    assert res.status_code == 404


def test_本人は担当を外せる(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    monkeypatch.setattr(assignments, "list_for_survivor", lambda **kwargs: [pending_assignment()])
    called: dict = {}
    monkeypatch.setattr(assignments, "remove", lambda **kwargs: called.update(kwargs))

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments/as_1/remove", headers=BEARER, json={}
    )

    assert res.status_code == 200
    assert called["acted_by"] == SELF_UID


def test_専門職も担当を外せる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))
    monkeypatch.setattr(assignments, "list_for_survivor", lambda **kwargs: [pending_assignment()])
    monkeypatch.setattr(assignments, "remove", lambda **kwargs: None)

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments/as_1/remove", headers=BEARER, json={}
    )

    assert res.status_code == 200


def test_一般の支援者は担当を外せない(client, monkeypatch):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))

    res = client.post(
        f"/api/survivors/{SURVIVOR}/assignments/as_1/remove", headers=BEARER, json={}
    )

    assert res.status_code == 403


# --- 確認コードでつなぐ（POST /api/onboarding/survivor） ---


def test_確認コードで記録につながる(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=None)
    monkeypatch.setattr(
        link_codes,
        "resolve_code",
        lambda code, user_id: link_codes.ResolvedCode(
            code_hash="sha256$x", survivor_id=SURVIVOR, org_id=ORG
        ),
    )
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: survivor_row())
    monkeypatch.setattr(survivors, "link_self_user", lambda **kwargs: survivor_row(version=2))
    monkeypatch.setattr(users, "register_user", lambda **kwargs: dict(kwargs))
    monkeypatch.setattr(link_codes, "mark_used", lambda resolved, used_by: None)

    res = client.post("/api/onboarding/survivor", headers=BEARER, json={"link_code": "ABCD2345"})

    assert res.status_code == 201
    body = res.json()
    assert body["role"] == users.ROLE_SURVIVOR
    assert body["survivor_id"] == SURVIVOR
    assert body["org_id"] == ORG


def test_確認コードが違えば400(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=None)
    monkeypatch.setattr(link_codes, "resolve_code", lambda code, user_id: None)

    res = client.post("/api/onboarding/survivor", headers=BEARER, json={"link_code": "ABCD2345"})

    assert res.status_code == 400


def test_何度も間違えたら429(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=None)

    def locked(code, user_id):
        raise link_codes.TooManyAttempts("待ってからお試しください")

    monkeypatch.setattr(link_codes, "resolve_code", locked)

    res = client.post("/api/onboarding/survivor", headers=BEARER, json={"link_code": "ABCD2345"})

    assert res.status_code == 429


def test_コードが指す記録が見つからなければ400(client, monkeypatch):
    # 記録を作る前にコードを発行するため、記録だけが作られなかった場合がありうる
    sign_in(monkeypatch, uid=SELF_UID, role=None)
    monkeypatch.setattr(
        link_codes,
        "resolve_code",
        lambda code, user_id: link_codes.ResolvedCode(
            code_hash="sha256$x", survivor_id=SURVIVOR, org_id=ORG
        ),
    )
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: None)

    res = client.post("/api/onboarding/survivor", headers=BEARER, json={"link_code": "ABCD2345"})

    assert res.status_code == 400


def test_すでに本人がついている記録にはつなげない(client, monkeypatch):
    # コードを使う前に、記録の self_user_id を確かめる（同じコードで2人がつながらないように）
    sign_in(monkeypatch, uid=SELF_UID, role=None)
    monkeypatch.setattr(
        link_codes,
        "resolve_code",
        lambda code, user_id: link_codes.ResolvedCode(
            code_hash="sha256$x", survivor_id=SURVIVOR, org_id=ORG
        ),
    )
    monkeypatch.setattr(
        survivors, "fetch_survivor", lambda survivor_id: survivor_row(self_user_id="uid-self-9")
    )

    res = client.post("/api/onboarding/survivor", headers=BEARER, json={"link_code": "ABCD2345"})

    assert res.status_code == 400


def test_つないだあとに使用済みにする(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=None)
    order: list[str] = []
    monkeypatch.setattr(
        link_codes,
        "resolve_code",
        lambda code, user_id: link_codes.ResolvedCode(
            code_hash="sha256$x", survivor_id=SURVIVOR, org_id=ORG
        ),
    )
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: survivor_row())

    def fake_link(**kwargs):
        order.append("link")
        return survivor_row(version=2)

    def fake_register(**kwargs):
        order.append("user")
        return dict(kwargs)

    def fake_mark(resolved, used_by):
        order.append("used")

    monkeypatch.setattr(survivors, "link_self_user", fake_link)
    monkeypatch.setattr(users, "register_user", fake_register)
    monkeypatch.setattr(link_codes, "mark_used", fake_mark)

    client.post("/api/onboarding/survivor", headers=BEARER, json={"link_code": "ABCD2345"})

    # 使用済みにするのは最後。途中で落ちたらやり直せるようにする
    assert order == ["link", "user", "used"]
