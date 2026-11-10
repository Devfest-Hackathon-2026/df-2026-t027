"""組織のオーナーと、参加コードを何回でも見る（追加修正の計画.md 6章。修正点⑥）。

確かめること（6-4）：

- 組織を作った人はオーナーになり、自分の組織のコードを読める
- オーナーでない専門職・一般の支援者・本人・別の組織のオーナーは、読めない（403）
  （立場ごとの403は `test_permissions.py` の表でも確かめる。別の組織は、組織を指定する方法がなく、
  要求に組織のIDを書いても自分の組織しか読まないことを確かめる）
- オーナーが、同じ組織の専門職にオーナーの権限を付けられる。付けられた人もコードを読める
- 別の組織の人・一般の支援者には、オーナーの権限を付けられない
- オーナーでない人は、オーナーの権限を付けられない（403）
- 最後の1人のオーナーは外せない
- 発行し直すと、前のコードで組織に入れない
- 参加コードがログに出ない

ほかに、`04_scripts/set_org_owner.py`（すでにある組織にオーナーを付ける）を確かめる。
Google Cloud にも Firebase にも接続しない。試す値はすべて架空のもの。
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import set_org_owner
from backend import auth, request_log
from backend.config import get_settings
from backend.main import app
from backend.services import bq, codes, organizations, users

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
OTHER_ORG = "org_suzu"
OWNER_UID = "uid-owner-1"
PRO_UID = "uid-pro-2"
VOL_UID = "uid-vol-3"
OTHER_UID = "uid-other-4"
BEARER = {"Authorization": "Bearer dummy-token"}
OWNER = users.ORG_PERMISSION_OWNER

# 保存されている元のコード（架空の値）
PRO_CODE = "PRQ7K2MX"
VOL_CODE = "VLH4N8TB"


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


def user_row(uid: str, **overrides) -> dict:
    row = {
        "user_id": uid,
        "org_id": ORG,
        "org_name": "輪島市",
        "role": users.ROLE_PROFESSIONAL,
        "profession": "保健師",
        "display_name": f"テスト{uid[-1]}",
        "login_method": "phone",
        "linked_survivor_id": None,
        "contact": "000-0000-0000",
        "municipality_code": "172049",
        "address": "テスト町1番地",
        "org_permission": None,
    }
    row.update(overrides)
    return row


class World:
    """利用者の最新の行（stg_users の代わり）と、書いた行を持つ。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = {row["user_id"]: row for row in rows}
        self.written: list[dict] = []

    def fetch_user(self, user_id: str):
        return self.rows.get(user_id)

    def professionals(self, org_id: str) -> list[dict]:
        return [
            row
            for row in self.rows.values()
            if row["org_id"] == org_id and row["role"] == users.ROLE_PROFESSIONAL
        ]

    def insert(self, table, row, **kwargs) -> None:
        assert table == "raw_users"
        self.written.append(dict(row))
        # 新しい版が最新になる（stg_users と同じ）
        merged = {**self.rows.get(row["user_id"], {}), **row}
        self.rows[row["user_id"]] = merged


def sign_in(monkeypatch, world: World, uid: str) -> None:
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
    monkeypatch.setattr(users, "fetch_user", world.fetch_user)
    monkeypatch.setattr(users, "list_org_professionals", world.professionals)
    monkeypatch.setattr(users.bq, "insert", world.insert)


def stored_codes(monkeypatch) -> list[str]:
    """組織の元のコードを読む処理を差し替え、読んだ組織のIDを記録する。"""
    asked: list[str] = []

    def fetch(org_id):
        asked.append(org_id)
        return {
            "org_id": org_id,
            "org_name": "輪島市",
            "pro_join_code": PRO_CODE,
            "vol_join_code": VOL_CODE,
        }

    monkeypatch.setattr(organizations, "fetch_join_codes", fetch)
    return asked


# --- 組織を作った人はオーナーになる ---


def test_組織を作った人はオーナーになり自分の組織のコードを読める(client, monkeypatch):
    world = World([user_row(OWNER_UID, org_id="", org_name=None, role=users.ROLE_VOLUNTEER, profession=None)])
    sign_in(monkeypatch, world, OWNER_UID)
    monkeypatch.setattr(organizations, "fetch_by_municipality", lambda code: [])
    orgs: list[dict] = []
    # users.bq と organizations.bq は同じモジュールなので、表の名前で振り分ける
    monkeypatch.setattr(
        bq,
        "insert",
        lambda table, row, **kw: orgs.append(dict(row))
        if table == "raw_organizations"
        else world.insert(table, row),
    )

    res = client.post(
        "/api/organizations",
        headers=BEARER,
        json={
            "org_name": "テストの会",
            "prefecture": "石川県",
            "municipality_code": "172049",
            "profession": "保健師",
        },
    )

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["me"]["is_owner"] is True
    assert world.written[0]["org_permission"] == OWNER
    # 元のコードも保存する。照合に使うハッシュも今までどおり入れる
    org_row = orgs[0]
    assert org_row["pro_join_code"] == body["pro_join_code"]
    assert org_row["vol_join_code"] == body["vol_join_code"]
    assert codes.verify_code(
        body["pro_join_code"], salt=org_row["org_id"], code_hash=org_row["pro_join_code_hash"]
    )

    # 作った人として読み直すと、自分の組織のコードが読める
    world.rows[OWNER_UID]["org_id"] = org_row["org_id"]
    monkeypatch.setattr(
        organizations,
        "fetch_join_codes",
        lambda org_id: {
            "org_id": org_id,
            "org_name": "テストの会",
            "pro_join_code": org_row["pro_join_code"],
            "vol_join_code": org_row["vol_join_code"],
        },
    )
    me = client.get("/api/me", headers=BEARER).json()
    assert me["is_owner"] is True
    res = client.get("/api/organizations/join-codes", headers=BEARER)
    assert res.status_code == 200
    assert res.json()["pro_join_code"] == body["pro_join_code"]
    assert res.json()["available"] is True


def test_コードは組織を作った版の行に元の値も入る(monkeypatch):
    monkeypatch.setattr(organizations, "fetch_by_municipality", lambda code: [])
    inserted: list[dict] = []
    monkeypatch.setattr(organizations.bq, "insert", lambda table, row, **kw: inserted.append(dict(row)))
    created = organizations.create_organization(
        org_name="テストの会",
        prefecture="石川県",
        municipality_code="172049",
        created_by="uid-x",
        created_role="volunteer",
    )
    assert inserted[0]["pro_join_code"] == created["pro_join_code"]
    assert inserted[0]["vol_join_code"] == created["vol_join_code"]


# --- 読めない人 ---


@pytest.mark.parametrize(
    "row",
    [
        user_row(PRO_UID),  # オーナーでない専門職
        user_row(VOL_UID, role=users.ROLE_VOLUNTEER, profession=None),
        user_row(VOL_UID, role=users.ROLE_SURVIVOR, profession=None, linked_survivor_id="sv_1"),
        # 権限の値が owner でも、専門職でなければオーナーではない
        user_row(VOL_UID, role=users.ROLE_VOLUNTEER, profession=None, org_permission=OWNER),
    ],
)
def test_オーナーでなければコードを読めない(client, monkeypatch, row):
    world = World([row])
    sign_in(monkeypatch, world, row["user_id"])
    monkeypatch.setattr(
        organizations, "fetch_join_codes", lambda org_id: pytest.fail("読んではいけません")
    )
    assert client.get("/api/organizations/join-codes", headers=BEARER).status_code == 403
    assert client.post("/api/organizations/join-codes", headers=BEARER).status_code == 403
    assert client.get("/api/organizations/members", headers=BEARER).status_code == 403
    assert client.get("/api/me", headers=BEARER).json()["is_owner"] is False


def test_別の組織のオーナーは自分の組織のコードしか読めない(client, monkeypatch):
    # 組織のIDは要求から受け取らない。別の組織のIDを書いても、自分の組織だけを読む
    world = World([user_row(OTHER_UID, org_id=OTHER_ORG, org_name="珠洲市", org_permission=OWNER)])
    sign_in(monkeypatch, world, OTHER_UID)
    asked = stored_codes(monkeypatch)
    res = client.get(
        "/api/organizations/join-codes", headers=BEARER, params={"org_id": ORG}
    )
    assert res.status_code == 200
    assert asked == [OTHER_ORG]
    assert res.json()["org_id"] == OTHER_ORG


def test_元のコードが無い組織は発行し直すよう伝える(client, monkeypatch):
    world = World([user_row(OWNER_UID, org_permission=OWNER)])
    sign_in(monkeypatch, world, OWNER_UID)
    monkeypatch.setattr(
        organizations,
        "fetch_join_codes",
        lambda org_id: {"org_id": org_id, "org_name": "輪島市", "pro_join_code": None, "vol_join_code": None},
    )
    body = client.get("/api/organizations/join-codes", headers=BEARER).json()
    assert body["available"] is False
    assert body["pro_join_code"] is None


def test_組織の一覧には元のコードを返さない():
    for column in ("pro_join_code", "vol_join_code", "pro_join_code_hash", "vol_join_code_hash"):
        assert column not in organizations._PUBLIC_COLUMNS


def test_元のコードを読むSQLはハッシュを読まずパラメータで絞る(monkeypatch):
    captured: dict = {}

    def fake_query_one(sql, params=None):
        captured.update(sql=sql, params=params)
        return None

    monkeypatch.setattr(organizations.bq, "query_one", fake_query_one)
    organizations.fetch_join_codes(ORG)
    assert "hash" not in captured["sql"]
    assert "@org_id" in captured["sql"]
    assert captured["params"] == {"org_id": ORG}


# --- オーナーの権限を付ける・外す ---


def owners_world() -> World:
    return World(
        [
            user_row(OWNER_UID, org_permission=OWNER),
            user_row(PRO_UID),
            user_row(VOL_UID, role=users.ROLE_VOLUNTEER, profession=None),
            user_row(OTHER_UID, org_id=OTHER_ORG, org_name="珠洲市"),
        ]
    )


def test_オーナーは同じ組織の専門職をオーナーにできる(client, monkeypatch):
    world = owners_world()
    sign_in(monkeypatch, world, OWNER_UID)
    res = client.post(
        "/api/organizations/owners", headers=BEARER, json={"user_id": PRO_UID, "owner": True}
    )
    assert res.status_code == 200, res.text
    # 新しい版の行を1行足す（上書きしない）。立場・組織・連絡先などは引き継ぐ
    assert len(world.written) == 1
    row = world.written[0]
    assert row["user_id"] == PRO_UID
    assert row["org_permission"] == OWNER
    assert row["role"] == users.ROLE_PROFESSIONAL
    assert row["org_id"] == ORG
    assert row["contact"] == "000-0000-0000"
    assert row["created_by"] == OWNER_UID
    members = {item["user_id"]: item for item in res.json()["members"]}
    assert members[PRO_UID]["is_owner"] is True
    # 一覧に連絡先・住所を入れない
    assert "contact" not in str(res.json())
    assert "テスト町" not in str(res.json())

    # 付けられた人も、コードを読める
    sign_in(monkeypatch, world, PRO_UID)
    stored_codes(monkeypatch)
    res = client.get("/api/organizations/join-codes", headers=BEARER)
    assert res.status_code == 200
    assert res.json()["pro_join_code"] == PRO_CODE


def test_別の組織の人にはオーナーの権限を付けられない(client, monkeypatch):
    world = owners_world()
    sign_in(monkeypatch, world, OWNER_UID)
    res = client.post(
        "/api/organizations/owners", headers=BEARER, json={"user_id": OTHER_UID, "owner": True}
    )
    assert res.status_code == 404
    assert world.written == []


def test_一般の支援者にはオーナーの権限を付けられない(client, monkeypatch):
    world = owners_world()
    sign_in(monkeypatch, world, OWNER_UID)
    res = client.post(
        "/api/organizations/owners", headers=BEARER, json={"user_id": VOL_UID, "owner": True}
    )
    assert res.status_code == 400
    assert world.written == []


def test_いない人にはオーナーの権限を付けられない(client, monkeypatch):
    world = owners_world()
    sign_in(monkeypatch, world, OWNER_UID)
    res = client.post(
        "/api/organizations/owners", headers=BEARER, json={"user_id": "uid-nobody", "owner": True}
    )
    assert res.status_code == 404
    assert world.written == []


def test_オーナーでない人はオーナーの権限を付けられない(client, monkeypatch):
    world = owners_world()
    sign_in(monkeypatch, world, PRO_UID)
    res = client.post(
        "/api/organizations/owners", headers=BEARER, json={"user_id": PRO_UID, "owner": True}
    )
    assert res.status_code == 403
    assert world.written == []


def test_最後の1人のオーナーは外せない(client, monkeypatch):
    world = owners_world()
    sign_in(monkeypatch, world, OWNER_UID)
    res = client.post(
        "/api/organizations/owners", headers=BEARER, json={"user_id": OWNER_UID, "owner": False}
    )
    assert res.status_code == 400
    assert "最後の1人" in res.json()["detail"]
    assert world.written == []


def test_オーナーが2人いれば1人を外せる(client, monkeypatch):
    world = owners_world()
    world.rows[PRO_UID]["org_permission"] = OWNER
    sign_in(monkeypatch, world, OWNER_UID)

    # 自分を外す。もう1人が残るので外せる
    res = client.post(
        "/api/organizations/owners", headers=BEARER, json={"user_id": OWNER_UID, "owner": False}
    )
    assert res.status_code == 200, res.text
    assert world.written[0]["org_permission"] is None
    assert res.json()["me_is_owner"] is False

    # 外れた人は、もうオーナーのAPIを使えない（権限は毎回読み直す）
    assert client.get("/api/organizations/join-codes", headers=BEARER).status_code == 403

    # 残った1人は、最後の1人なので外せない
    sign_in(monkeypatch, world, PRO_UID)
    res = client.post(
        "/api/organizations/owners", headers=BEARER, json={"user_id": PRO_UID, "owner": False}
    )
    assert res.status_code == 400


def test_すでにその状態なら行を足さない(client, monkeypatch):
    world = owners_world()
    sign_in(monkeypatch, world, OWNER_UID)
    res = client.post(
        "/api/organizations/owners", headers=BEARER, json={"user_id": OWNER_UID, "owner": True}
    )
    assert res.status_code == 200
    assert world.written == []


def test_プロフィールを直してもオーナーの権限は残る(monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr(users.bq, "insert", lambda table, row, **kw: written.append(dict(row)))
    users.update_profile(
        current=user_row(OWNER_UID, org_permission=OWNER),
        display_name="テスト1",
        profession="保健師",
        contact="000-0000-0000",
        municipality_code="172049",
        address="テスト町1番地",
    )
    assert written[0]["org_permission"] == OWNER


def test_組織の専門職の一覧は連絡先と住所を読まない(monkeypatch):
    captured: dict = {}

    def fake_query(sql, params=None):
        captured.update(sql=sql, params=params)
        return []

    monkeypatch.setattr(users.bq, "query", fake_query)
    users.list_org_professionals(ORG)
    assert "contact" not in captured["sql"]
    assert "address" not in captured["sql"]
    assert captured["params"] == {"org_id": ORG, "role": users.ROLE_PROFESSIONAL}


# --- 発行し直す ---


def test_発行し直すと前のコードで組織に入れない(client, monkeypatch):
    world = World([user_row(OWNER_UID, org_permission=OWNER)])
    sign_in(monkeypatch, world, OWNER_UID)
    old_row = {
        "org_id": ORG,
        "pro_join_code_hash": codes.hash_code(PRO_CODE, salt=ORG),
        "vol_join_code_hash": codes.hash_code(VOL_CODE, salt=ORG),
    }
    # 発行し直す前は、前のコードで入れる
    monkeypatch.setattr(organizations, "_fetch_join_code_hashes", lambda: [old_row])
    assert organizations.resolve_join_code(PRO_CODE) == (ORG, users.ROLE_PROFESSIONAL)

    monkeypatch.setattr(
        organizations,
        "fetch_organization",
        lambda org_id: {
            "org_id": org_id,
            "org_name": "輪島市",
            "prefecture": "石川県",
            "municipality_code": "172049",
        },
    )
    inserted: list[dict] = []
    monkeypatch.setattr(
        organizations.bq, "insert", lambda table, row, **kw: inserted.append((table, dict(row)))
    )
    res = client.post("/api/organizations/join-codes", headers=BEARER)
    assert res.status_code == 200, res.text
    body = res.json()

    # 新しい版の行を1行足す。名前などは引き継ぎ、コードだけ新しくする
    table, new_row = inserted[0]
    assert table == "raw_organizations"
    assert new_row["org_id"] == ORG
    assert new_row["org_name"] == "輪島市"
    assert new_row["municipality_code"] == "172049"
    assert new_row["created_by"] == OWNER_UID
    assert new_row["pro_join_code"] == body["pro_join_code"]
    assert body["pro_join_code"] not in (PRO_CODE, VOL_CODE)

    # stg_organizations は最新の版だけを返す → 前のコードは合わず、新しいコードは合う
    monkeypatch.setattr(organizations, "_fetch_join_code_hashes", lambda: [new_row])
    assert organizations.resolve_join_code(PRO_CODE) is None
    assert organizations.resolve_join_code(VOL_CODE) is None
    assert organizations.resolve_join_code(body["pro_join_code"]) == (ORG, users.ROLE_PROFESSIONAL)
    assert organizations.resolve_join_code(body["vol_join_code"]) == (ORG, users.ROLE_VOLUNTEER)


# --- ログに出さない ---


def test_参加コードがログに出ない(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    entries: list[str] = []
    monkeypatch.setattr(request_log, "emit", lambda entry: entries.append(str(entry)))

    world = World([user_row(OWNER_UID, org_permission=OWNER)])
    sign_in(monkeypatch, world, OWNER_UID)
    stored_codes(monkeypatch)
    assert client.get("/api/organizations/join-codes", headers=BEARER).status_code == 200

    monkeypatch.setattr(
        organizations,
        "fetch_organization",
        lambda org_id: {"org_id": org_id, "org_name": "輪島市", "prefecture": "石川県", "municipality_code": "172049"},
    )
    monkeypatch.setattr(organizations.bq, "insert", lambda *a, **k: None)
    reissued = client.post("/api/organizations/join-codes", headers=BEARER).json()

    logged = caplog.text + "\n".join(entries)
    assert entries, "要求のログが出ていません（確かめる対象が空です）"
    for code in (PRO_CODE, VOL_CODE, reissued["pro_join_code"], reissued["vol_join_code"]):
        assert code not in logged


# --- 04_scripts/set_org_owner.py ---


@pytest.fixture
def script_world(monkeypatch):
    world = World(
        [
            user_row(PRO_UID),
            user_row(VOL_UID, role=users.ROLE_VOLUNTEER, profession=None),
            user_row(OWNER_UID, org_permission=OWNER),
        ]
    )
    monkeypatch.setattr(users, "fetch_user", world.fetch_user)
    monkeypatch.setattr(users.bq, "insert", world.insert)
    return world


def test_スクリプトは既定では書かない(script_world, capsys):
    assert set_org_owner.main(["--user-id", PRO_UID]) == 0
    assert script_world.written == []
    out = capsys.readouterr().out
    assert "--apply" in out
    # 表示名・連絡先・住所は画面に出さない
    assert "テスト2" not in out
    assert "000-0000-0000" not in out
    assert "テスト町" not in out


def test_スクリプトは確認が合わなければ書かない(script_world):
    assert set_org_owner.main(["--user-id", PRO_UID, "--apply"], reader=lambda: "org_other") == 1
    assert script_world.written == []


def test_スクリプトは確認が合えばオーナーにする(script_world):
    assert set_org_owner.main(["--user-id", PRO_UID, "--apply"], reader=lambda: ORG) == 0
    assert len(script_world.written) == 1
    row = script_world.written[0]
    assert row["user_id"] == PRO_UID
    assert row["org_permission"] == OWNER
    assert row["org_id"] == ORG
    assert row["created_by"] == "system"
    assert row["created_role"] == "system"


def test_スクリプトは専門職でない人をオーナーにしない(script_world):
    assert (
        set_org_owner.main(["--user-id", VOL_UID, "--apply"], reader=lambda: pytest.fail("聞いてはいけません"))
        == 1
    )
    assert script_world.written == []


def test_スクリプトはいない人を断る(script_world):
    assert set_org_owner.main(["--user-id", "uid-nobody", "--apply"]) == 1
    assert script_world.written == []


def test_スクリプトはすでにオーナーなら何もしない(script_world):
    assert (
        set_org_owner.main(["--user-id", OWNER_UID, "--apply"], reader=lambda: pytest.fail("聞いてはいけません"))
        == 0
    )
    assert script_world.written == []
