"""施設の職員が空きを入力する（01_docs/使いやすさの改修の計画.md 9章）のテスト。

Google Cloud にも Firebase にも Gemini にも接続しない。確かめるのは次のこと。

- 施設の参加コードは**ハッシュで照合**し、元のコードはその組織の専門職だけに返す
  （画面に返す施設の一覧にはハッシュも元のコードも入れない）
- 施設の参加コードで入ると、**サーバーが決めた**施設と組織で施設の職員になる
  （要求に書かれた立場や施設のIDは使わない。要件定義書10-3）
- 施設の職員は、**自分の施設だけ**を直せる。ほかの施設は 404
- 施設の職員は、被災者の一覧・記録のAPIで 403 か 404 になる
- 施設の更新は**上書きせず版を足し**、参加コードを前の版から引き継ぐ
- 施設の一覧に、最後に更新したのが施設の職員か専門職かを出す
- 08_migrations.sql が列を足し、ビューの中身が 03_stg_views.sql と同じであること
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
    codes,
    consultations,
    facilities,
    gemini,
    handovers,
    health,
    link_codes,
    organizations,
    supports,
    survivors,
    users,
    visits,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT = "gdgoc-test-project"
ORG = "org_test_a"
OTHER_ORG = "org_test_b"
FACILITY = "fc_test_0001"
OTHER_FACILITY = "fc_test_0002"
PRO_UID = "uid-pro-1"
STAFF_UID = "uid-staff-1"
NEW_UID = "uid-new-1"
NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)
BEARER = {"Authorization": "Bearer dummy-token"}

# 架空の支援者の項目（実在の人ではない）
DETAILS = {
    "display_name": "テスト職員",
    "profession": users.NO_PROFESSION,
    "contact": "000-0000-0000",
    "municipality_code": "999999",
    "address": "テスト町1-1",
}


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
        organizations.bq,
        users.bq,
    ):
        monkeypatch.setattr(module, "query", fail, raising=False)
        monkeypatch.setattr(module, "query_one", fail, raising=False)
        monkeypatch.setattr(module, "insert", fail, raising=False)


@pytest.fixture(autouse=True)
def no_gemini(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("テストから Gemini を呼んではいけません")

    monkeypatch.setattr(gemini, "generate_structured", fail)


def sign_in(
    monkeypatch,
    *,
    uid: str,
    role: str | None,
    org_id: str | None = ORG,
    facility_id: str | None = None,
) -> None:
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
            "org_name": "テスト組織",
            "contact": "000-0000-0000",
            "municipality_code": "999999",
            "address": "テスト町1-1",
            "facility_id": facility_id,
        }
    monkeypatch.setattr(users, "fetch_user", lambda user_id: row)


def sign_in_staff(monkeypatch, *, facility_id: str | None = FACILITY) -> None:
    sign_in(
        monkeypatch, uid=STAFF_UID, role=users.ROLE_FACILITY_STAFF, facility_id=facility_id
    )


def facility_row(facility_id: str = FACILITY, **overrides) -> dict:
    """`stg_facilities_latest` の1行と同じ形（架空の施設）。"""
    row = {
        "facility_id": facility_id,
        "org_id": ORG,
        "version": 3,
        "facility_name": "テスト福祉避難所",
        "facility_type": "welfare_shelter",
        "address": "テスト町2-2",
        "vacancies": 2,
        "capabilities": ["wheelchair"],
        "water_outage": False,
        "power_outage": True,
        "updated_at": NOW,
        "updated_by": PRO_UID,
        "updated_role": users.ROLE_PROFESSIONAL,
        "is_stale": False,
    }
    row.update(overrides)
    return row


def with_code(row: dict, code: str = "ABCD2345") -> dict:
    """参加コードの列まで読んだ行（`fetch_facility(..., with_join_code=True)`）。"""
    return {
        **row,
        "staff_join_code_hash": codes.hash_code(code, salt=row["facility_id"]),
        "staff_join_code": code,
    }


def catch_writes(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(facilities, "save", lambda row: written.append(row) or row)
    return written


# --- 立場の値 ---


def test_施設の職員の立場がある():
    assert users.ROLE_FACILITY_STAFF == "facility_staff"
    assert users.ROLE_FACILITY_STAFF in users.ROLES
    # 一般の支援者・専門職の参加コードで決まる立場とは別にする
    assert users.ROLE_FACILITY_STAFF not in users.SUPPORTER_ROLES


def test_施設の職員は施設が無ければ登録できない(monkeypatch):
    monkeypatch.setattr(bq, "insert", lambda *args, **kwargs: 1)
    with pytest.raises(users.UserValidationError):
        users.register_user(
            user_id=NEW_UID,
            org_id=ORG,
            role=users.ROLE_FACILITY_STAFF,
            display_name="テスト職員",
            login_method="phone",
        )


def test_施設の職員の行は施設のIDを持ちほかの立場は持たない(monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr(users.bq, "insert", lambda table, row, **kwargs: written.append(row))
    users.register_user(
        user_id=NEW_UID,
        org_id=ORG,
        role=users.ROLE_FACILITY_STAFF,
        display_name="テスト職員",
        login_method="phone",
        facility_id=FACILITY,
    )
    users.register_user(
        user_id=PRO_UID,
        org_id=ORG,
        role=users.ROLE_PROFESSIONAL,
        display_name="テスト専門職",
        login_method="phone",
        profession="相談員",
        facility_id=FACILITY,
    )
    assert written[0]["facility_id"] == FACILITY
    assert written[0]["role"] == users.ROLE_FACILITY_STAFF
    # 施設の職員でなければ、施設のIDを渡されても持たせない
    assert written[1]["facility_id"] is None


def test_プロフィールを直しても施設は変わらない(monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr(users.bq, "insert", lambda table, row, **kwargs: written.append(row))
    users.update_profile(
        current={
            "user_id": STAFF_UID,
            "org_id": ORG,
            "role": users.ROLE_FACILITY_STAFF,
            "login_method": "phone",
            "facility_id": FACILITY,
        },
        display_name="テスト職員2",
        profession=None,
        contact="000-0000-0001",
        municipality_code="999999",
        address="テスト町3-3",
    )
    assert written[0]["facility_id"] == FACILITY
    assert written[0]["role"] == users.ROLE_FACILITY_STAFF
    assert written[0]["org_id"] == ORG


# --- 施設の参加コード（作る・照合する） ---


def test_参加コードの版はハッシュで照合できて空きは引き継ぐ():
    row, code = facilities.build_join_code_row(
        current=facility_row(), created_by=PRO_UID, created_role=users.ROLE_PROFESSIONAL
    )
    assert row["version"] == 4
    assert row["change_kind"] == facilities.CHANGE_JOIN_CODE
    assert row["staff_join_code"] == code
    # ソルトは施設のID（組織のコードと同じ考え方）
    assert codes.verify_code(code, salt=FACILITY, code_hash=row["staff_join_code_hash"])
    assert not codes.verify_code(code, salt=OTHER_FACILITY, code_hash=row["staff_join_code_hash"])
    # 空き・断水・停電・対応できる介護と医療はそのまま
    assert row["vacancies"] == 2
    assert row["power_outage"] is True
    assert row["capabilities"] == ["wheelchair"]


def test_空きを変えても参加コードは引き継ぐ():
    current = with_code(facility_row())
    row = facilities.build_update_row(
        current=current, created_by=STAFF_UID, created_role=users.ROLE_FACILITY_STAFF, vacancies=5
    )
    assert row["staff_join_code_hash"] == current["staff_join_code_hash"]
    assert row["staff_join_code"] == current["staff_join_code"]
    assert row["change_kind"] == facilities.CHANGE_INFO
    assert row["vacancies"] == 5


def test_対応できる介護と医療は渡したときだけ置き換える():
    current = with_code(facility_row())
    kept = facilities.build_update_row(
        current=current, created_by=STAFF_UID, created_role=users.ROLE_FACILITY_STAFF
    )
    replaced = facilities.build_update_row(
        current=current,
        created_by=STAFF_UID,
        created_role=users.ROLE_FACILITY_STAFF,
        capabilities=["dialysis", "dementia"],
    )
    cleared = facilities.build_update_row(
        current=current,
        created_by=STAFF_UID,
        created_role=users.ROLE_FACILITY_STAFF,
        capabilities=[],
    )
    assert kept["capabilities"] == ["wheelchair"]
    assert replaced["capabilities"] == ["dialysis", "dementia"]
    assert cleared["capabilities"] == []


def _fake_code_rows(monkeypatch, rows: list[dict]) -> dict:
    seen: dict = {}

    def fake_query(sql, params=None):
        seen["sql"] = sql
        return rows

    monkeypatch.setattr(facilities.bq, "query", fake_query)
    return seen


def test_施設の参加コードから施設と組織を決める(monkeypatch):
    _fake_code_rows(
        monkeypatch,
        [
            {"facility_id": FACILITY, "org_id": ORG, "staff_join_code_hash": codes.hash_code("ABCD2345", salt=FACILITY)},
            {"facility_id": OTHER_FACILITY, "org_id": OTHER_ORG, "staff_join_code_hash": codes.hash_code("WXYZ6789", salt=OTHER_FACILITY)},
        ],
    )
    assert facilities.resolve_staff_join_code("abcd-2345") == (FACILITY, ORG)
    assert facilities.resolve_staff_join_code("WXYZ6789") == (OTHER_FACILITY, OTHER_ORG)
    assert facilities.resolve_staff_join_code("QQQQ2345") is None


def test_2か所に当たる施設の参加コードは採らない(monkeypatch):
    # 同じハッシュの行を2つ用意する（ありえないはずの状態でも、どちらも採らない）
    code_hash = codes.hash_code("SAME2345", salt=FACILITY)
    _fake_code_rows(
        monkeypatch,
        [
            {"facility_id": FACILITY, "org_id": ORG, "staff_join_code_hash": code_hash},
            {"facility_id": FACILITY, "org_id": OTHER_ORG, "staff_join_code_hash": code_hash},
        ],
    )
    assert facilities.resolve_staff_join_code("SAME2345") is None


def test_画面に返す施設の形に参加コードを入れない():
    item = facilities.payload(with_code(facility_row()))
    assert "staff_join_code" not in item
    assert "staff_join_code_hash" not in item
    # 読む列の一覧にも入れない（一覧の読みでは参加コードを取らない）
    assert "staff_join_code_hash" not in facilities._COLUMNS
    assert "staff_join_code" not in facilities._COLUMNS


def test_参加コードの列は頼んだときだけ読む(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        facilities.bq, "query_one", lambda sql, params=None: seen.append(sql) or None
    )
    facilities.fetch_facility(facility_id=FACILITY, org_id=ORG)
    facilities.fetch_facility(facility_id=FACILITY, org_id=ORG, with_join_code=True)
    assert "staff_join_code" not in seen[0]
    assert "staff_join_code_hash" in seen[1]
    # どちらも組織で絞る
    assert "org_id = @org_id" in seen[0] and "org_id = @org_id" in seen[1]


def test_最後に更新した人の立場を返す():
    assert facilities.payload(facility_row(updated_role="facility_staff"))["updated_role_label"] == "施設の職員"
    assert facilities.payload(facility_row(updated_role="professional"))["updated_role_label"] == "専門職"
    assert facilities.payload(facility_row(updated_role=None))["updated_role_label"] == ""


# --- 専門職が施設の参加コードを見る・発行し直す ---


def test_専門職は施設の参加コードを見られる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    captured: dict = {}

    def fetched(**kwargs):
        captured.update(kwargs)
        return with_code(facility_row(), "ABCD2345")

    monkeypatch.setattr(facilities, "fetch_facility", fetched)
    res = client.get(f"/api/facilities/{FACILITY}/staff-join-code", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["staff_join_code"] == "ABCD2345"
    assert body["available"] is True
    # ハッシュは返さない
    assert "staff_join_code_hash" not in body
    assert "sha256$" not in res.text
    # 組織は stg_users から決める
    assert captured["org_id"] == ORG
    assert captured["with_join_code"] is True


def test_まだ発行していない施設はavailableがfalse(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(facilities, "fetch_facility", lambda **kwargs: facility_row())
    body = client.get(f"/api/facilities/{FACILITY}/staff-join-code", headers=BEARER).json()
    assert body["available"] is False
    assert body["staff_join_code"] is None


def test_ほかの組織の施設の参加コードは404(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL, org_id=OTHER_ORG)
    monkeypatch.setattr(facilities, "fetch_facility", lambda **kwargs: None)
    assert client.get(f"/api/facilities/{FACILITY}/staff-join-code", headers=BEARER).status_code == 404
    assert client.post(f"/api/facilities/{FACILITY}/staff-join-code", headers=BEARER).status_code == 404


def test_発行し直すと版を足し新しいコードで照合できる(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    old = with_code(facility_row(), "ABCD2345")
    monkeypatch.setattr(facilities, "fetch_facility", lambda **kwargs: old)
    written = catch_writes(monkeypatch)
    res = client.post(f"/api/facilities/{FACILITY}/staff-join-code", headers=BEARER)
    assert res.status_code == 200
    code = res.json()["staff_join_code"]
    assert code and code != "ABCD2345"
    assert len(written) == 1
    row = written[0]
    assert row["version"] == old["version"] + 1
    assert row["change_kind"] == facilities.CHANGE_JOIN_CODE
    assert row["created_role"] == users.ROLE_PROFESSIONAL
    assert codes.verify_code(code, salt=FACILITY, code_hash=row["staff_join_code_hash"])
    # 前のコードでは、もう合わない
    assert not codes.verify_code("ABCD2345", salt=FACILITY, code_hash=row["staff_join_code_hash"])


@pytest.mark.parametrize("role", [users.ROLE_VOLUNTEER, users.ROLE_SURVIVOR, users.ROLE_FACILITY_STAFF])
def test_専門職でなければ施設の参加コードを扱えない(client, monkeypatch, role):
    sign_in(monkeypatch, uid="uid-other", role=role, facility_id=FACILITY)
    assert client.get(f"/api/facilities/{FACILITY}/staff-join-code", headers=BEARER).status_code == 403
    assert client.post(f"/api/facilities/{FACILITY}/staff-join-code", headers=BEARER).status_code == 403


# --- 施設の参加コードで入る ---


def test_施設の参加コードで登録すると施設の職員になる(client, monkeypatch):
    sign_in(monkeypatch, uid=NEW_UID, role=None)
    monkeypatch.setattr(organizations, "resolve_join_code", lambda code: None)
    monkeypatch.setattr(facilities, "resolve_staff_join_code", lambda code: (FACILITY, ORG))
    monkeypatch.setattr(organizations, "fetch_organization", lambda org_id: {"org_name": "テスト組織"})
    written: dict = {}
    monkeypatch.setattr(users, "register_user", lambda **kwargs: written.update(kwargs) or kwargs)
    res = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json={
            "join_code": "ABCD2345",
            **DETAILS,
            # 画面が立場や施設を送っても使わない（要件定義書10-3）
            "role": users.ROLE_PROFESSIONAL,
            "facility_id": OTHER_FACILITY,
            "org_id": OTHER_ORG,
        },
    )
    assert res.status_code == 201
    assert written["role"] == users.ROLE_FACILITY_STAFF
    assert written["facility_id"] == FACILITY
    assert written["org_id"] == ORG
    # 施設の職員は職種を持たない
    assert written["profession"] is None
    # 支援者の登録と同じ項目を必須で入れる
    assert written["contact"] and written["municipality_code"] and written["address"]
    body = res.json()
    assert body["role"] == users.ROLE_FACILITY_STAFF
    assert body["facility_id"] == FACILITY
    assert body["registered"] is True


def test_組織の参加コードに合えば施設のコードは照合しない(client, monkeypatch):
    sign_in(monkeypatch, uid=NEW_UID, role=None)
    monkeypatch.setattr(organizations, "resolve_join_code", lambda code: (ORG, users.ROLE_VOLUNTEER))
    monkeypatch.setattr(
        facilities, "resolve_staff_join_code", lambda code: pytest.fail("照合してはいけません")
    )
    monkeypatch.setattr(organizations, "fetch_organization", lambda org_id: {"org_name": "テスト組織"})
    written: dict = {}
    monkeypatch.setattr(users, "register_user", lambda **kwargs: written.update(kwargs) or kwargs)
    res = client.post("/api/onboarding/supporter", headers=BEARER, json={"join_code": "BBBB2345", **DETAILS})
    assert res.status_code == 201
    assert written["role"] == users.ROLE_VOLUNTEER
    assert written["facility_id"] is None


def test_施設の参加コードでも住所が空なら登録できない(client, monkeypatch):
    sign_in(monkeypatch, uid=NEW_UID, role=None)
    monkeypatch.setattr(
        facilities, "resolve_staff_join_code", lambda code: pytest.fail("照合してはいけません")
    )
    res = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json={"join_code": "ABCD2345", **{**DETAILS, "address": ""}},
    )
    assert res.status_code == 400


def test_組織がない支援者が施設の参加コードで入れる(client, monkeypatch):
    sign_in(monkeypatch, uid=NEW_UID, role=users.ROLE_VOLUNTEER, org_id="")
    monkeypatch.setattr(organizations, "resolve_join_code", lambda code: None)
    monkeypatch.setattr(facilities, "resolve_staff_join_code", lambda code: (FACILITY, ORG))
    monkeypatch.setattr(organizations, "fetch_organization", lambda org_id: {"org_name": "テスト組織"})
    written: dict = {}
    monkeypatch.setattr(users, "register_user", lambda **kwargs: written.update(kwargs) or kwargs)
    res = client.post(
        "/api/me/join",
        headers=BEARER,
        json={"join_code": "ABCD2345", "profession": users.NO_PROFESSION},
    )
    assert res.status_code == 200
    assert written["role"] == users.ROLE_FACILITY_STAFF
    assert written["facility_id"] == FACILITY
    assert written["org_id"] == ORG
    # 前の版の連絡先・住所を引き継ぐ
    assert written["address"] == "テスト町1-1"
    assert res.json()["facility_id"] == FACILITY


# --- 施設の職員が自分の施設を読む・直す ---


def test_自分の施設はstg_usersの施設のIDで読む(client, monkeypatch):
    sign_in_staff(monkeypatch)
    captured: dict = {}

    def fetched(**kwargs):
        captured.update(kwargs)
        return facility_row()

    monkeypatch.setattr(facilities, "fetch_facility", fetched)
    res = client.get("/api/my-facility", headers=BEARER)
    assert res.status_code == 200
    assert res.json()["facility"]["facility_id"] == FACILITY
    assert captured["facility_id"] == FACILITY
    assert captured["org_id"] == ORG
    # 施設の職員には参加コードを返さない
    assert captured["with_join_code"] is False
    assert "staff_join_code" not in res.text


def test_施設の職員が空きを直すと版が増えて参加コードが残る(client, monkeypatch):
    sign_in_staff(monkeypatch)
    current = with_code(facility_row(version=3))
    monkeypatch.setattr(facilities, "fetch_facility", lambda **kwargs: current)
    written = catch_writes(monkeypatch)
    res = client.patch(
        "/api/my-facility",
        headers=BEARER,
        json={
            "vacancies": 0,
            "water_outage": True,
            "capabilities": ["dialysis", "wheelchair"],
            # 要求に書かれた施設のIDは使わない
            "facility_id": OTHER_FACILITY,
        },
    )
    assert res.status_code == 200
    assert len(written) == 1
    row = written[0]
    assert row["facility_id"] == FACILITY
    assert row["org_id"] == ORG
    assert row["version"] == 4
    assert row["vacancies"] == 0
    assert row["water_outage"] is True
    # 渡さなかった停電は前の版のまま
    assert row["power_outage"] is True
    assert row["capabilities"] == ["dialysis", "wheelchair"]
    assert row["created_by"] == STAFF_UID
    assert row["created_role"] == users.ROLE_FACILITY_STAFF
    assert row["staff_join_code_hash"] == current["staff_join_code_hash"]
    # 名前・種類・場所は変えない
    assert row["facility_name"] == current["facility_name"]
    body = res.json()["facility"]
    assert body["updated_role_label"] == "施設の職員"
    assert "staff_join_code" not in res.text


def test_選択肢にない対応できる医療は400(client, monkeypatch):
    sign_in_staff(monkeypatch)
    monkeypatch.setattr(facilities, "fetch_facility", lambda **kwargs: with_code(facility_row()))
    catch_writes(monkeypatch)
    res = client.patch("/api/my-facility", headers=BEARER, json={"capabilities": ["何でもできます"]})
    assert res.status_code == 400


def test_空きに負の数は入れられない(client, monkeypatch):
    sign_in_staff(monkeypatch)
    monkeypatch.setattr(facilities, "fetch_facility", lambda **kwargs: with_code(facility_row()))
    catch_writes(monkeypatch)
    assert client.patch("/api/my-facility", headers=BEARER, json={"vacancies": -1}).status_code == 400


def test_施設が別の組織にあれば404(client, monkeypatch):
    # stg_users の施設が、組織で絞ると見つからない（ほかの施設を直す経路を作らない）
    sign_in_staff(monkeypatch, facility_id=OTHER_FACILITY)
    monkeypatch.setattr(facilities, "fetch_facility", lambda **kwargs: None)
    catch_writes(monkeypatch)
    assert client.get("/api/my-facility", headers=BEARER).status_code == 404
    assert client.patch("/api/my-facility", headers=BEARER, json={"vacancies": 1}).status_code == 404


def test_施設の職員はほかの施設を直せない(client, monkeypatch):
    sign_in_staff(monkeypatch)
    catch_writes(monkeypatch)
    # 専門職の施設のAPIは使えない（施設のIDを道に書いても通さない）
    res = client.patch(f"/api/facilities/{OTHER_FACILITY}", headers=BEARER, json={"vacancies": 1})
    assert res.status_code == 403
    assert client.get("/api/facilities", headers=BEARER).status_code == 403
    assert client.post("/api/facilities", headers=BEARER, json={}).status_code == 403


def test_施設のない施設の職員は通さない(client, monkeypatch):
    sign_in_staff(monkeypatch, facility_id=None)
    assert client.get("/api/my-facility", headers=BEARER).status_code == 403


@pytest.mark.parametrize("role", [users.ROLE_PROFESSIONAL, users.ROLE_VOLUNTEER, users.ROLE_SURVIVOR])
def test_施設の職員でなければ自分の施設のAPIは403(client, monkeypatch, role):
    sign_in(monkeypatch, uid="uid-other", role=role, facility_id=FACILITY)
    assert client.get("/api/my-facility", headers=BEARER).status_code == 403
    assert client.patch("/api/my-facility", headers=BEARER, json={"vacancies": 1}).status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/survivors"),
        ("POST", "/api/survivors"),
        ("GET", "/api/survivors/sv_test_0001"),
        ("GET", "/api/survivors/sv_test_0001/handovers"),
        ("PUT", "/api/survivors/sv_test_0001/health"),
        ("POST", "/api/survivors/sv_test_0001/visits"),
        ("POST", "/api/survivors/sv_test_0001/facility-candidates"),
        ("GET", "/api/consultations"),
        ("GET", "/api/consultations/public"),
        ("GET", "/api/dashboard"),
    ],
)
def test_施設の職員は被災者の記録のAPIを使えない(client, monkeypatch, method, path):
    sign_in_staff(monkeypatch)
    kwargs: dict = {"headers": BEARER}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}
    res = client.request(method, path, **kwargs)
    assert res.status_code in (403, 404), f"{method} {path} を施設の職員が使えてしまいます"


def test_施設の職員はプロフィールを直せる(client, monkeypatch):
    sign_in_staff(monkeypatch)
    written: list[dict] = []
    monkeypatch.setattr(users.bq, "insert", lambda table, row, **kwargs: written.append(row))
    res = client.patch(
        "/api/me",
        headers=BEARER,
        json={
            "display_name": "テスト職員2",
            "contact": "000-0000-0002",
            "municipality_code": "999999",
            "address": "テスト町4-4",
        },
    )
    assert res.status_code == 200
    assert written[0]["facility_id"] == FACILITY
    assert written[0]["role"] == users.ROLE_FACILITY_STAFF
    assert res.json()["facility_id"] == FACILITY


def test_meに施設のIDを返す(client, monkeypatch):
    sign_in_staff(monkeypatch)
    body = client.get("/api/me", headers=BEARER).json()
    assert body["role"] == users.ROLE_FACILITY_STAFF
    assert body["facility_id"] == FACILITY
    assert body["registered"] is True


# --- 施設の一覧に、最後に更新した人の立場を出す ---


def test_施設の一覧に更新した人の立場が出る(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    monkeypatch.setattr(
        facilities,
        "list_facilities",
        lambda **kwargs: [
            facility_row(updated_role=users.ROLE_FACILITY_STAFF),
            facility_row(OTHER_FACILITY, updated_role=users.ROLE_PROFESSIONAL),
        ],
    )
    body = client.get("/api/facilities", headers=BEARER).json()
    labels = [item["updated_role_label"] for item in body["facilities"]]
    assert labels == ["施設の職員", "専門職"]


def test_専門職の更新でも参加コードを引き継ぐ(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    captured: dict = {}
    current = with_code(facility_row())

    def fetched(**kwargs):
        captured.update(kwargs)
        return current

    monkeypatch.setattr(facilities, "fetch_facility", fetched)
    written = catch_writes(monkeypatch)
    res = client.patch(f"/api/facilities/{FACILITY}", headers=BEARER, json={"vacancies": 1})
    assert res.status_code == 200
    assert captured["with_join_code"] is True
    assert written[0]["staff_join_code_hash"] == current["staff_join_code_hash"]
    assert res.json()["facility"]["updated_role_label"] == "専門職"


# --- SQL（08_migrations.sql） ---

RAW_SQL = (REPO_ROOT / "03_sql" / "02_raw_tables.sql").read_text(encoding="utf-8")
STG_SQL = (REPO_ROOT / "03_sql" / "03_stg_views.sql").read_text(encoding="utf-8")
SQL_08 = (REPO_ROOT / "03_sql" / "08_migrations.sql").read_text(encoding="utf-8")


def _block(sql: str, marker: str) -> str:
    start = sql.index(marker)
    return sql[start : sql.index(";", start) + 1]


def test_08は列を足すだけでデータを変えない():
    for line in SQL_08.splitlines():
        if "ADD COLUMN" in line:
            assert "IF NOT EXISTS" in line, line
    upper = SQL_08.upper()
    for forbidden in ("UPDATE ", "DELETE ", "MERGE ", "TRUNCATE ", "DROP TABLE", "DROP COLUMN", "DROP VIEW"):
        assert forbidden not in upper, f"{forbidden} を使っている"
    assert "{{PROJECT}}" in SQL_08
    assert "{{LOCATION}}" not in SQL_08


def test_08は施設の職員の列を足す():
    assert "ADD COLUMN IF NOT EXISTS facility_id STRING" in SQL_08
    for column in ("staff_join_code_hash", "staff_join_code", "change_kind"):
        assert f"ADD COLUMN IF NOT EXISTS {column} STRING" in SQL_08
    # 新しく作る環境でも同じ列を持つ
    users_block = _block(RAW_SQL, "gdgoc_raw.raw_users`")
    facilities_block = _block(RAW_SQL, "gdgoc_raw.raw_facilities`")
    assert "  facility_id STRING" in users_block
    for column in ("staff_join_code_hash", "staff_join_code", "change_kind"):
        assert f"  {column} STRING" in facilities_block


@pytest.mark.parametrize("view", ["stg_users", "stg_facilities_latest"])
def test_08のビューは03と同じ中身(view):
    marker = f"CREATE OR REPLACE VIEW `{{{{PROJECT}}}}.gdgoc_staging.{view}`"
    assert _block(SQL_08, marker) == _block(STG_SQL, marker)


def test_stg_usersが施設のIDを返す():
    assert "  facility_id," in _block(STG_SQL, "gdgoc_staging.stg_users`")


def test_最終更新は参加コードの発行だけの版を数えない():
    block = _block(STG_SQL, "gdgoc_staging.stg_facilities_latest`")
    assert 'IFNULL(change_kind, "info") != "join_code"' in block
    assert "AS updated_role" in block
    assert "INTERVAL 24 HOUR" in block
    # 照合と引き継ぎのために、参加コードの列も出す
    assert "l.staff_join_code_hash" in block


# --- 画面（JavaScript を走らせる仕組みは無いので、作りを文字列で確かめる） ---

FRONTEND = REPO_ROOT / "02_app" / "frontend"


def _source(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def test_施設の職員のホームとタブがある():
    app_js = _source("app.js")
    assert 'import * as facilityStaff from "./facility_staff.js"' in app_js
    assert 'facility_staff: "施設の職員"' in app_js
    # HOME_GUIDE・HOME_ACTIONS・TAB_DEFS の3つに立場の鍵がある
    assert app_js.count("facility_staff: {") == 1
    assert app_js.count("facility_staff: [") == 2
    assert "facilityStaff.openMyFacility()" in app_js
    assert "facilityStaff.bind()" in app_js


def test_施設の職員の画面は施設のIDを送らない():
    text = _source("facility_staff.js")
    assert '"/api/my-facility"' in text
    # 施設は stg_users から決まる。画面は施設のIDも立場も送らない
    assert "facility_id:" not in text
    assert "role:" not in text
    # 空きは「−」「＋」で変える
    assert 'el("my-facility-minus")' in text and 'el("my-facility-plus")' in text


def test_施設の職員のホームで被災者の一覧を読まない():
    f4 = _source("f4.js")
    assert "SUPPORTER_ROLES.includes(me.role)" in f4


def test_施設の画面で参加コードを見て発行し直せる():
    f5 = _source("f5.js")
    assert "/staff-join-code" in f5
    assert "updated_role_label" in f5
    html = _source("index.html")
    assert "<!-- 施設の職員（使いやすさの改修の計画.md 9章） -->" in html
    assert 'id="screen-my-facility" class="screen' in html
