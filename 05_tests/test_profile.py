"""プロフィールの設定のテスト（要望4。追加改修の計画.md 5章）。

- `services/users.py` の `update_profile`：版を1つ足し、立場・組織・ログインの方法は
  引き継いだままにする（要件定義書10-3。変えると見られる記録の範囲が変わるため）。
- `PATCH /api/me`：支援者（専門職・一般の支援者）だけが使える（本人は使えない。
  本人の基本の情報は `test_survivors.py` の `change_basic` が確かめる）。

Google Cloud にも Firebase にも接続しない。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import auth
from backend.config import get_settings
from backend.main import app
from backend.services import bq, users

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
PRO_UID = "uid-pro-1"
BEARER = {"Authorization": "Bearer dummy-token"}

# 支援者の必須の項目（追加修正④）。架空の値
DETAILS = {"contact": "000-0000-0000", "municipality_code": "172049", "address": "架空町1-2"}


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


def pro_row(**overrides) -> dict:
    row = {
        "user_id": PRO_UID,
        "org_id": ORG,
        "role": users.ROLE_PROFESSIONAL,
        "profession": "保健師",
        "display_name": "鈴木 一郎",
        "login_method": "phone",
        "linked_survivor_id": None,
    }
    row.update(overrides)
    return row


# --- services/users.py の update_profile ---


def test_表示名を変えると版が1つ増える(monkeypatch):
    inserted = []
    monkeypatch.setattr(users.bq, "insert", lambda table, row, **kw: inserted.append((table, dict(row))))

    users.update_profile(
        current=pro_row(), display_name="鈴木 二郎", profession="保健師", **DETAILS
    )

    table, row = inserted[0]
    assert table == "raw_users"
    assert row["display_name"] == "鈴木 二郎"
    # 立場・組織・ログインの方法は引き継いだまま（要件定義書10-3）
    assert row["role"] == users.ROLE_PROFESSIONAL
    assert row["org_id"] == ORG
    assert row["login_method"] == "phone"


def test_表示名が空だと断る(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("入力の確認より先に書き込んではいけません")

    monkeypatch.setattr(users.bq, "insert", fail)
    with pytest.raises(users.UserValidationError):
        users.update_profile(current=pro_row(), display_name="", profession="保健師", **DETAILS)


def test_専門職は職種が選択肢に無いと断る(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("入力の確認より先に書き込んではいけません")

    monkeypatch.setattr(users.bq, "insert", fail)
    with pytest.raises(users.UserValidationError):
        users.update_profile(
            current=pro_row(), display_name="鈴木 二郎", profession="バス運転士", **DETAILS
        )


def test_一般の支援者は職種を空にする(monkeypatch):
    inserted = []
    monkeypatch.setattr(users.bq, "insert", lambda table, row, **kw: inserted.append((table, dict(row))))

    row = pro_row(role=users.ROLE_VOLUNTEER, profession=None)
    users.update_profile(current=row, display_name="佐藤 支援子", profession="保健師", **DETAILS)

    _, saved = inserted[0]
    # 一般の支援者には職種が無いので、渡しても validate_profession が None に戻す
    assert saved["profession"] is None


# --- PATCH /api/me ---


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def no_bigquery(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("テストから BigQuery を呼んではいけません")

    monkeypatch.setattr(bq, "query", fail, raising=False)
    monkeypatch.setattr(bq, "query_one", fail, raising=False)
    monkeypatch.setattr(bq, "insert", fail, raising=False)
    monkeypatch.setattr(users.bq, "query", fail, raising=False)
    monkeypatch.setattr(users.bq, "query_one", fail, raising=False)


def sign_in_pro(monkeypatch) -> None:
    monkeypatch.setattr(
        auth.id_token,
        "verify_firebase_token",
        lambda *args, **kwargs: {
            "iss": f"{auth.ISSUER_PREFIX}{PROJECT}",
            "aud": PROJECT,
            "sub": PRO_UID,
            "firebase": {"sign_in_provider": "phone"},
        },
    )
    monkeypatch.setattr(users, "fetch_user", lambda user_id: pro_row(org_name="輪島市"))


def test_表示名を保存できる(client, monkeypatch):
    sign_in_pro(monkeypatch)
    inserted = []
    monkeypatch.setattr(users.bq, "insert", lambda table, row, **kw: inserted.append((table, dict(row))))

    res = client.patch(
        "/api/me",
        json={"display_name": "鈴木 二郎", "profession": "保健師", **DETAILS},
        headers=BEARER,
    )

    assert res.status_code == 200
    body = res.json()
    assert body["display_name"] == "鈴木 二郎"
    # 立場・組織は変わらない
    assert body["role"] == users.ROLE_PROFESSIONAL
    assert body["org_id"] == ORG
    assert inserted[0][0] == "raw_users"


def test_表示名が空だと400(client, monkeypatch):
    sign_in_pro(monkeypatch)
    res = client.patch("/api/me", json={"display_name": "", "profession": "保健師", **DETAILS}, headers=BEARER)
    assert res.status_code == 400
