"""支援者の連絡先・活動する市区町村・住所（追加修正の計画.md 4章。修正点④）。

確かめること（4-4）：

- 必須の項目（表示名・職種・連絡先・活動する市区町村）が空のときは400になり、画面にも「必須」の印が出る
  （画面の印は `test_frontend_required.py` の表でも確かめる）
- 任意の項目（住所・参加コード）は空のままで登録できる
- 支援者の連絡先・住所が、ログに出ない
- 一般の支援者が、他の支援者の連絡先・住所を読めない（本人の `/api/me` にだけ返す）

ほかに、前の版の値を引き継ぐこと（組織に入る・プロフィールを直す）と、
前から登録している支援者にホームで入力を案内すること（`profile_incomplete`）を確かめる。

Google Cloud にも Firebase にも接続しない。試す値はすべて架空のもの。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import auth, request_log
from backend.config import get_settings
from backend.main import app
from backend.services import bq, organizations, users

PROJECT = "gdgoc-test-project"
UID = "uid-supporter-1"
BEARER = {"Authorization": "Bearer dummy-token"}
INDEX_HTML = (
    Path(__file__).resolve().parents[1] / "02_app" / "frontend" / "index.html"
).read_text(encoding="utf-8")

# ログに出ていないかを探す目印（架空の値。ほかの文字と混ざらない形にする）
CONTACT = "000-1111-2222"
ADDRESS = "テスト町9丁目99番地"
MUNICIPALITY = "172049"


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


@pytest.fixture(autouse=True)
def no_bigquery(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("テストから BigQuery を呼んではいけません")

    monkeypatch.setattr(bq, "query", fail)
    monkeypatch.setattr(bq, "query_one", fail)
    monkeypatch.setattr(bq, "insert", fail)


@pytest.fixture
def client():
    return TestClient(app)


def sign_in(monkeypatch, row: dict | None) -> None:
    monkeypatch.setattr(
        auth.id_token,
        "verify_firebase_token",
        lambda *args, **kwargs: {
            "iss": f"{auth.ISSUER_PREFIX}{PROJECT}",
            "aud": PROJECT,
            "sub": UID,
            "firebase": {"sign_in_provider": "phone"},
        },
    )
    monkeypatch.setattr(users, "fetch_user", lambda user_id: row)


def supporter_row(**overrides) -> dict:
    row = {
        "user_id": UID,
        "org_id": "org_wajima",
        "org_name": "輪島市",
        "role": users.ROLE_VOLUNTEER,
        "profession": None,
        "display_name": "テスト支援者",
        "login_method": "phone",
        "linked_survivor_id": None,
        "contact": CONTACT,
        "municipality_code": MUNICIPALITY,
        "address": ADDRESS,
    }
    row.update(overrides)
    return row


def record_writes(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(
        users.bq, "insert", lambda table, row, **kwargs: written.append(dict(row))
    )
    return written


def onboarding_body(**overrides) -> dict:
    body = {
        "display_name": "テスト支援者",
        "profession": users.NO_PROFESSION,
        "contact": CONTACT,
        "municipality_code": MUNICIPALITY,
        "address": ADDRESS,
    }
    body.update(overrides)
    return body


# --- 必須の項目 ---


@pytest.mark.parametrize(
    "overrides",
    [
        {"display_name": ""},
        {"contact": ""},
        {"contact": "   "},
        {"contact": None},
        {"municipality_code": ""},
        {"municipality_code": "石川県"},
        {"municipality_code": "17204"},
        {"contact": "0" * (users.CONTACT_MAX_LENGTH + 1)},
        {"address": "あ" * (users.ADDRESS_MAX_LENGTH + 1)},
    ],
)
def test_支援者の登録で必須の項目が空なら400(client, monkeypatch, overrides):
    sign_in(monkeypatch, None)
    # 書き込みまで進むと no_bigquery が落とす（入力の確認より先に書かない）
    res = client.post("/api/onboarding/supporter", headers=BEARER, json=onboarding_body(**overrides))
    assert res.status_code == 400
    assert res.json()["detail"]


def test_必須の項目の文面は日本語で何を入れるかを伝える(client, monkeypatch):
    sign_in(monkeypatch, None)
    res = client.post("/api/onboarding/supporter", headers=BEARER, json=onboarding_body(contact=""))
    assert "連絡先" in res.json()["detail"]
    res = client.post(
        "/api/onboarding/supporter", headers=BEARER, json=onboarding_body(municipality_code="")
    )
    assert "市区町村" in res.json()["detail"]


def test_参加コードより先に必須の項目を確かめる(client, monkeypatch):
    # 連絡先が空なら、参加コードの照合（BigQuery の読み）まで進まない
    sign_in(monkeypatch, None)
    monkeypatch.setattr(
        organizations, "resolve_join_code", lambda code: pytest.fail("照合してはいけません")
    )
    res = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json=onboarding_body(join_code="AAAA2345", contact=""),
    )
    assert res.status_code == 400


@pytest.mark.parametrize(
    "overrides",
    [{"display_name": ""}, {"contact": ""}, {"municipality_code": ""}],
)
def test_プロフィールで必須の項目が空なら400(client, monkeypatch, overrides):
    sign_in(monkeypatch, supporter_row())
    body = {
        "display_name": "テスト支援者",
        "contact": CONTACT,
        "municipality_code": MUNICIPALITY,
        **overrides,
    }
    res = client.patch("/api/me", headers=BEARER, json=body)
    assert res.status_code == 400


def test_画面の必須の欄にrequiredが付いている():
    for field_id in (
        "supporter-name",
        "supporter-profession",
        "supporter-contact",
        "supporter-prefecture",
        "supporter-municipality",
        "profile-display-name",
        "profile-supporter-contact",
        "profile-supporter-prefecture",
        "profile-supporter-municipality",
    ):
        tag = re.search(rf'id="{field_id}"[^>]*>', INDEX_HTML, re.DOTALL)
        assert tag, field_id
        assert re.search(r"\srequired[\s/>]", tag.group(0)), field_id


# --- 任意の項目 ---


def test_参加コードは空のままで登録できる(client, monkeypatch):
    sign_in(monkeypatch, None)
    written = record_writes(monkeypatch)
    res = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json=onboarding_body(join_code=""),
    )
    assert res.status_code == 201, res.text
    row = written[0]
    assert row["contact"] == CONTACT
    assert row["municipality_code"] == MUNICIPALITY
    assert row["address"]
    body = res.json()
    assert body["contact"] == CONTACT
    assert body["municipality_code"] == MUNICIPALITY
    assert body["profile_incomplete"] is False


def test_住所が空なら登録できない(client, monkeypatch):
    """支援者の住所は必須（使いやすさの改修の計画.md 3章）。"""
    sign_in(monkeypatch, None)
    record_writes(monkeypatch)
    res = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json=onboarding_body(address="  ", join_code=""),
    )
    assert res.status_code == 400
    assert "住所" in res.json()["detail"]


def test_住所が空の支援者には入力を案内する():
    assert users.is_profile_incomplete(supporter_row(address=None)) is True


def test_参加コードで登録しても新しい項目を保存する(client, monkeypatch):
    sign_in(monkeypatch, None)
    monkeypatch.setattr(
        organizations, "resolve_join_code", lambda code: ("org_wajima", users.ROLE_VOLUNTEER)
    )
    monkeypatch.setattr(organizations, "fetch_organization", lambda org_id: {"org_name": "輪島市"})
    written = record_writes(monkeypatch)
    res = client.post(
        "/api/onboarding/supporter", headers=BEARER, json=onboarding_body(join_code="BBBB2345")
    )
    assert res.status_code == 201, res.text
    assert written[0]["contact"] == CONTACT
    assert written[0]["address"] == ADDRESS
    assert written[0]["municipality_code"] == MUNICIPALITY


# --- 前の版の値を引き継ぐ ---


def test_プロフィールで変えると新しい版に保存する(client, monkeypatch):
    sign_in(monkeypatch, supporter_row(contact=None, municipality_code=None, address=None))
    written = record_writes(monkeypatch)
    res = client.patch(
        "/api/me",
        headers=BEARER,
        json={
            "display_name": "テスト支援者",
            "contact": CONTACT,
            "municipality_code": MUNICIPALITY,
            "address": "架空町3-4",
        },
    )
    assert res.status_code == 200, res.text
    assert written[0]["contact"] == CONTACT
    assert written[0]["municipality_code"] == MUNICIPALITY
    assert written[0]["address"] == "架空町3-4"
    # 立場・組織は変わらない
    assert written[0]["role"] == users.ROLE_VOLUNTEER
    assert written[0]["org_id"] == "org_wajima"
    assert res.json()["profile_incomplete"] is False


def test_組織に入っても連絡先などを引き継ぐ(client, monkeypatch):
    sign_in(monkeypatch, supporter_row(org_id="", org_name=None))
    monkeypatch.setattr(
        organizations, "resolve_join_code", lambda code: ("org_wajima", users.ROLE_VOLUNTEER)
    )
    monkeypatch.setattr(organizations, "fetch_organization", lambda org_id: {"org_name": "輪島市"})
    written = record_writes(monkeypatch)
    res = client.post(
        "/api/me/join",
        headers=BEARER,
        json={"join_code": "BBBB2345", "profession": users.NO_PROFESSION},
    )
    assert res.status_code == 200, res.text
    assert written[0]["contact"] == CONTACT
    assert written[0]["municipality_code"] == MUNICIPALITY
    assert written[0]["address"] == ADDRESS


# --- 前から登録している支援者への案内 ---


def test_新しい項目が空の支援者はホームで案内する(client, monkeypatch):
    sign_in(monkeypatch, supporter_row(contact=None, municipality_code=None, address=None))
    body = client.get("/api/me", headers=BEARER).json()
    assert body["profile_incomplete"] is True
    # 案内するだけで、記録のAPIは今までどおり使える（止めない）
    assert body["registered"] is True


def test_入力済みの支援者と本人には案内しない():
    assert users.is_profile_incomplete(supporter_row()) is False
    assert users.is_profile_incomplete({"role": users.ROLE_SURVIVOR}) is False
    assert users.is_profile_incomplete(None) is False
    assert users.is_profile_incomplete(supporter_row(municipality_code="")) is True


# --- ログに出さない ---


def test_支援者の連絡先と住所がログに出ない(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    entries: list[str] = []
    monkeypatch.setattr(request_log, "emit", lambda entry: entries.append(str(entry)))

    sign_in(monkeypatch, None)
    record_writes(monkeypatch)
    ok = client.post("/api/onboarding/supporter", headers=BEARER, json=onboarding_body())
    assert ok.status_code == 201
    # 断られたとき（長すぎる住所）も、送られた値を出さない
    ng = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json=onboarding_body(address=ADDRESS * 20),
    )
    assert ng.status_code == 400

    sign_in(monkeypatch, supporter_row())
    patched = client.patch(
        "/api/me",
        headers=BEARER,
        json={
            "display_name": "テスト支援者",
            "contact": CONTACT,
            "municipality_code": MUNICIPALITY,
            "address": ADDRESS,
        },
    )
    assert patched.status_code == 200

    logged = caplog.text + "\n".join(entries)
    assert entries, "要求のログが出ていません（確かめる対象が空です）"
    assert CONTACT not in logged
    assert ADDRESS not in logged


def test_利用者の値をreprに出さない():
    # 誤って利用者をログに出しても、連絡先と住所は混ざらない
    user = auth.CurrentUser(uid=UID, login_method="phone", contact=CONTACT, address=ADDRESS)
    assert CONTACT not in repr(user)
    assert ADDRESS not in repr(user)


# --- 他の支援者には見せない ---


def test_他の支援者を読む処理は連絡先と住所を読まない(monkeypatch):
    captured: list[str] = []

    def fake_query(sql, params=None):
        captured.append(sql)
        return []

    def fake_query_one(sql, params=None):
        captured.append(sql)
        return None

    monkeypatch.setattr(users.bq, "query", fake_query)
    monkeypatch.setattr(users.bq, "query_one", fake_query_one)

    users.list_by_role(org_id="org_wajima", role=[users.ROLE_PROFESSIONAL, users.ROLE_VOLUNTEER])
    users.names_by_ids(org_id="org_wajima", user_ids=["uid-other"])
    users.fetch_supporter("uid-other")

    assert len(captured) == 3
    for sql in captured:
        assert "contact" not in sql
        assert "address" not in sql


def test_meは自分の行だけを読む(client, monkeypatch):
    # /api/me は要求してきた本人の user_id だけで読む（ほかの人のIDを受け取らない）
    asked: list[str] = []

    def fetch(user_id):
        asked.append(user_id)
        return supporter_row()

    sign_in(monkeypatch, None)
    monkeypatch.setattr(users, "fetch_user", fetch)
    body = client.get("/api/me", headers=BEARER, params={"user_id": "uid-other"}).json()
    assert asked == [UID]
    assert body["contact"] == CONTACT
    assert body["address"] == ADDRESS
