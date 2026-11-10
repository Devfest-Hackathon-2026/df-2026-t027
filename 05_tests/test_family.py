"""家族などによる代わりの登録とつなぎのテスト（使いやすさの改修の計画.md 10章）。

Google Cloud にも Firebase にも接続しない。BigQuery の読み書きは差し替えて、次のことを確かめる。

- 代わりに登録した家族と、つながった家族だけが記録を見られる。つながっていない家族は 404（10-4）。
- 家族として見られても、ほかの立場の API（記録の詳細など）の判定は変わらない。
- 代わりの登録では、本人の同意（同意を得た／同意できない状態）が必須で、記録に残る（10-3）。
- 家族が発行した確認コードで、本人のアカウントが記録につながる（今の確認コードの仕組み）。
- 本人は家族の招待コードを発行でき、家族を外せる。外された家族は 404（10-4）。
- 専門職が人を登録するとき、家族が代わりに登録した記録も「似た人」の知らせに入る。

人物はすべて架空のもの。
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from backend import auth
from backend.config import get_settings
from backend.main import app
from backend.services import (
    bq,
    codes,
    family,
    link_codes,
    organizations,
    survivors,
    users,
)

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
SURVIVOR = "sv_family_0001"
FAMILY_UID = "uid-family-1"
OTHER_FAMILY_UID = "uid-family-2"
SELF_UID = "uid-self-1"
PRO_UID = "uid-pro-1"
NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)
BEARER = {"Authorization": "Bearer dummy-token"}

VOL = users.ROLE_VOLUNTEER
PRO = users.ROLE_PROFESSIONAL
SURVIVOR_ROLE = users.ROLE_SURVIVOR


# --- 下ごしらえ ---


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
    """BigQuery を呼んだら落ちる（呼ぶテストでは個別に差し替える）。"""

    def fail(*args, **kwargs):
        raise AssertionError("テストから BigQuery を呼んではいけません")

    monkeypatch.setattr(bq, "query", fail)
    monkeypatch.setattr(bq, "query_one", fail)
    monkeypatch.setattr(bq, "insert", fail)


@pytest.fixture
def inserted(monkeypatch) -> list[tuple[str, dict]]:
    """raw への追記を受け取る（表の名前と行の組で、書いた順に残す）。"""
    rows: list[tuple[str, dict]] = []

    def fake_insert(table, row, **kwargs):
        rows.append((table, dict(row)))
        return 1

    monkeypatch.setattr(bq, "insert", fake_insert)
    return rows


def rows_of(inserted: list[tuple[str, dict]], table: str) -> list[dict]:
    return [row for name, row in inserted if name == table]


def sign_in(
    monkeypatch,
    *,
    uid: str,
    role: str | None,
    org_id: str | None = "",
    linked_survivor_id: str | None = None,
) -> None:
    """ログイン済みの状態にする（立場と組織は stg_users の読みを差し替えて決める）。"""
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
            "profession": "保健師" if role == PRO else None,
            "display_name": "テスト利用者",
            "login_method": "phone",
            "linked_survivor_id": linked_survivor_id,
            "org_name": None,
        }
    monkeypatch.setattr(users, "fetch_user", lambda user_id: row)
    monkeypatch.setattr(users, "list_by_role", lambda **kwargs: [])
    monkeypatch.setattr(users, "names_by_ids", lambda **kwargs: [])


def proxy_row(**overrides) -> dict:
    """家族が代わりに登録した記録（`int_survivor_list` の1行。権限に関わる列だけ本物に合わせる）。"""
    row = {
        "survivor_id": SURVIVOR,
        "org_id": "",
        "version": 1,
        "full_name": "架空 花子",
        "age": None,
        "household_size": None,
        "contact": "000-0000-0000",
        "stage": "evacuation",
        "location_type": "relatives",
        "location_detail": "架空町の親せきの家",
        "municipality_code": "172049",
        "address": None,
        "self_user_id": None,
        "registered_via": survivors.VIA_PROXY,
        "proxy_consent": "unable",
        "registered_at": NOW,
        "updated_at": NOW,
        "risk_level": "high",
        "risk_next_action": "今日のうちに訪問する",
        "risk_evidence": None,
        "risk_source": "ai",
        "assignee_user_ids": [],
        "assignee_count": 0,
        "family_user_ids": [FAMILY_UID],
    }
    row.update(overrides)
    return row


def set_record(monkeypatch, row: dict | None) -> None:
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: row)
    monkeypatch.setattr(survivors, "location_history", lambda **kwargs: [])


def proxy_body(**overrides) -> dict:
    body = {
        "municipality_code": "172049",
        "full_name": "架空 花子",
        "stage": "evacuation",
        "location_type": "relatives",
        "location_detail": "架空町の親せきの家",
        "proxy_consent": "obtained",
    }
    body.update(overrides)
    return body


# --- 見える範囲（can_view） ---


@pytest.mark.parametrize(
    ("role", "uid", "family_ids", "expected"),
    [
        # 家族としてつながっている一般の支援者だけ
        (VOL, FAMILY_UID, [FAMILY_UID], True),
        (VOL, OTHER_FAMILY_UID, [FAMILY_UID], False),
        (VOL, FAMILY_UID, [], False),
        # 家族の判定は一般の支援者だけ（本人・専門職は家族の画面を使わない）
        (PRO, FAMILY_UID, [FAMILY_UID], False),
        (SURVIVOR_ROLE, FAMILY_UID, [FAMILY_UID], False),
    ],
)
def test_家族として見られるのはつながっている一般の支援者だけ(role, uid, family_ids, expected):
    row = proxy_row(family_user_ids=family_ids)
    # 組織がない家族でも、つながっていれば見られる
    assert survivors.can_view(row, role=role, user_id=uid, org_id="", as_family=True) is expected


def test_家族というだけでは今までの判定は変わらない():
    # 立場のAPI（健康・訪問・支援など）は as_family=False のまま。家族でも担当でなければ通さない
    row = proxy_row(org_id=ORG, family_user_ids=[FAMILY_UID])
    assert survivors.can_view(row, role=VOL, user_id=FAMILY_UID, org_id=ORG) is False
    # 専門職の判定はそのまま（同じ組織なら見られる）
    assert survivors.can_view(row, role=PRO, user_id=PRO_UID, org_id=ORG) is True


# --- 家族が記録を見る ---


def test_代わりに登録した家族は記録を見られる(client, monkeypatch):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row())

    res = client.get(f"/api/family/survivors/{SURVIVOR}", headers=BEARER)

    assert res.status_code == 200, res.text
    survivor = res.json()["survivor"]
    assert survivor["full_name"] == "架空 花子"
    assert survivor["registered_by_family"] is True
    assert survivor["proxy_consent_label"] == survivors.PROXY_CONSENTS["unable"]
    # 健康・悪化の兆し・訪問・相談・支援は家族の画面に返さない
    for key in ("risk", "risk_level", "health", "visits", "consultations", "supports"):
        assert key not in survivor
    assert "今日のうちに訪問する" not in res.text


def test_つながっていない家族は404(client, monkeypatch):
    sign_in(monkeypatch, uid=OTHER_FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row())

    assert client.get(f"/api/family/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


def test_記録が無いときも同じ404(client, monkeypatch):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    set_record(monkeypatch, None)

    assert client.get(f"/api/family/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


def test_つながっていない家族は直せない(client, monkeypatch):
    sign_in(monkeypatch, uid=OTHER_FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row())

    res = client.post(
        f"/api/family/survivors/{SURVIVOR}/location",
        headers=BEARER,
        json={"stage": "temporary", "location_type": "temporary_built"},
    )
    assert res.status_code == 404
    res = client.patch(
        f"/api/family/survivors/{SURVIVOR}/basic", headers=BEARER, json={"full_name": "別の名前"}
    )
    assert res.status_code == 404


def test_家族は今までの記録のAPIでは見られない(client, monkeypatch):
    # 組織がない家族は、記録の詳細のAPIそのものを使えない（今までどおり 403）
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row())
    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 403

    # 組織に入っていても、担当でなければ 404 のまま（家族の権限は今までの判定に混ざらない）
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL, org_id=ORG)
    set_record(monkeypatch, proxy_row(org_id=ORG))
    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


def test_家族は居場所を変えられる(client, monkeypatch, inserted):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row())

    res = client.post(
        f"/api/family/survivors/{SURVIVOR}/location",
        headers=BEARER,
        json={"stage": "temporary", "location_type": "temporary_built", "location_detail": "架空団地"},
    )

    assert res.status_code == 200, res.text
    [row] = rows_of(inserted, "raw_survivors")
    assert row["version"] == 2
    assert row["location_type"] == "temporary_built"
    assert row["created_by"] == FAMILY_UID
    assert row["created_role"] == VOL
    # 代わりの登録の印と本人の同意は、版を足しても消えない
    assert row["registered_via"] == survivors.VIA_PROXY
    assert row["proxy_consent"] == "unable"


def test_家族は基本の情報を直せる(client, monkeypatch, inserted):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row())

    res = client.patch(
        f"/api/family/survivors/{SURVIVOR}/basic",
        headers=BEARER,
        json={"full_name": "架空 花子", "age": 81, "household_size": None},
    )

    assert res.status_code == 200, res.text
    [row] = rows_of(inserted, "raw_survivors")
    assert row["age"] == 81
    assert row["household_size"] is None


def test_家族の一覧はつながっている記録だけを読む(client, monkeypatch):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    seen: dict = {}

    def fake_query(sql, params=None):
        seen["sql"] = sql
        seen["params"] = params
        return [proxy_row()]

    monkeypatch.setattr(bq, "query", fake_query)

    res = client.get("/api/family/survivors", headers=BEARER)

    assert res.status_code == 200, res.text
    assert "@user_id IN UNNEST(family_user_ids)" in seen["sql"]
    # 利用者のIDは要求からではなく、ログインした人のIDを使う
    assert seen["params"] == {"user_id": FAMILY_UID}
    [item] = res.json()["survivors"]
    assert item["survivor_id"] == SURVIVOR
    assert "contact" not in item


# --- 代わりの登録 ---


def test_代わりの登録で記録と家族のつながりを書く(client, monkeypatch, inserted):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)

    res = client.post("/api/family/survivors", headers=BEARER, json=proxy_body())

    assert res.status_code == 201, res.text
    # つながりを先に書く（記録だけが残って、誰も見られない記録にならないように）
    assert [name for name, _ in inserted] == ["raw_family_links", "raw_survivors"]
    [link] = rows_of(inserted, "raw_family_links")
    [record] = rows_of(inserted, "raw_survivors")
    assert link["action"] == family.ACTION_LINK
    assert link["via"] == family.VIA_PROXY
    assert link["user_id"] == FAMILY_UID
    assert link["survivor_id"] == record["survivor_id"]
    assert record["registered_via"] == survivors.VIA_PROXY
    assert record["proxy_consent"] == "obtained"
    assert record["self_user_id"] is None
    assert record["org_id"] == ""
    # 年齢・世帯の人数は本人の登録と同じく後で入れてよい
    assert record["age"] is None
    assert record["household_size"] is None
    assert res.json()["survivor"]["registered_by_family"] is True


@pytest.mark.parametrize("consent", ["", "maybe", None])
def test_代わりの登録は本人の同意が必須(client, monkeypatch, inserted, consent):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    body = proxy_body()
    if consent is None:
        del body["proxy_consent"]
    else:
        body["proxy_consent"] = consent

    res = client.post("/api/family/survivors", headers=BEARER, json=body)

    assert res.status_code == 400
    assert "同意" in res.json()["detail"]
    # 何も書かない
    assert inserted == []


@pytest.mark.parametrize(
    "overrides",
    [{"municipality_code": ""}, {"full_name": ""}, {"stage": ""}, {"location_type": ""}],
)
def test_代わりの登録は本人の登録と同じ欄が必須(client, monkeypatch, inserted, overrides):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    res = client.post("/api/family/survivors", headers=BEARER, json=proxy_body(**overrides))
    assert res.status_code == 400
    assert inserted == []


def test_代わりの登録で選んだ組織を確かめる(client, monkeypatch, inserted):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    monkeypatch.setattr(
        organizations,
        "fetch_organization",
        lambda org_id: {"org_id": org_id, "org_name": "架空市"} if org_id == ORG else None,
    )

    res = client.post("/api/family/survivors", headers=BEARER, json=proxy_body(org_id="org_none"))
    assert res.status_code == 400
    assert inserted == []

    res = client.post("/api/family/survivors", headers=BEARER, json=proxy_body(org_id=ORG))
    assert res.status_code == 201, res.text
    [record] = rows_of(inserted, "raw_survivors")
    assert record["org_id"] == ORG


def test_代わりの登録を使えるのは一般の支援者だけ(client, monkeypatch):
    for role in (PRO, SURVIVOR_ROLE):
        sign_in(monkeypatch, uid=f"uid-{role}", role=role, org_id=ORG)
        assert client.post("/api/family/survivors", headers=BEARER, json=proxy_body()).status_code == 403


def test_代わりの登録ではない記録には同意の列を入れない():
    # 09_migrations.sql を流す前でも、本人・専門職の記録を書けるように
    row = survivors.build_survivor_row(
        survivor_id="sv_x",
        org_id="",
        created_by=SELF_UID,
        created_role=SURVIVOR_ROLE,
        full_name="架空 一郎",
        age=None,
        household_size=None,
        stage="evacuation",
        location_type="home",
        self_user_id=SELF_UID,
        registered_via=survivors.VIA_SELF,
        proxy_consent="obtained",
    )
    assert "proxy_consent" not in row


# --- 本人とつなぐ（家族が発行する確認コード） ---


def test_家族は本人とつなぐ確認コードを発行できる(client, monkeypatch, inserted):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row())

    res = client.post(f"/api/family/survivors/{SURVIVOR}/link-code", headers=BEARER)

    assert res.status_code == 201, res.text
    code = res.json()["link_code"]["code"]
    [row] = rows_of(inserted, "raw_link_codes")
    assert row["action"] == link_codes.ACTION_ISSUED
    assert row["survivor_id"] == SURVIVOR
    assert row["created_role"] == VOL
    # 保存するのはハッシュだけ
    assert all(code not in str(value) for value in row.values())


def test_本人がつながった記録には確認コードを出さない(client, monkeypatch, inserted):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row(self_user_id=SELF_UID))

    res = client.post(f"/api/family/survivors/{SURVIVOR}/link-code", headers=BEARER)

    assert res.status_code == 400
    assert inserted == []


def test_つながっていない家族は確認コードを出せない(client, monkeypatch, inserted):
    sign_in(monkeypatch, uid=OTHER_FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row())

    res = client.post(f"/api/family/survivors/{SURVIVOR}/link-code", headers=BEARER)

    assert res.status_code == 404
    assert inserted == []


def test_家族が発行した確認コードで本人が記録につながる(client, monkeypatch, inserted):
    # 1. 家族がコードを発行する
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row())
    code = client.post(f"/api/family/survivors/{SURVIVOR}/link-code", headers=BEARER).json()[
        "link_code"
    ]["code"]
    [issued] = rows_of(inserted, "raw_link_codes")

    # 2. 本人が、登録の画面の「確認コードでつなぐ」にコードを入れる（今の仕組みのまま）
    sign_in(monkeypatch, uid=SELF_UID, role=None)
    monkeypatch.setattr(
        link_codes,
        "fetch_valid_codes",
        lambda: [{"code_hash": issued["code_hash"], "survivor_id": SURVIVOR, "org_id": ""}],
    )
    monkeypatch.setattr(link_codes, "count_recent_failures", lambda user_id: 0)

    res = client.post("/api/onboarding/survivor", headers=BEARER, json={"link_code": code})

    assert res.status_code == 201, res.text
    assert res.json()["survivor_id"] == SURVIVOR
    record = rows_of(inserted, "raw_survivors")[-1]
    assert record["self_user_id"] == SELF_UID
    assert record["version"] == 2
    # 家族が登録した印と本人の同意は残る（本人は家族が登録した内容を見て直せる）
    assert record["registered_via"] == survivors.VIA_PROXY
    assert record["proxy_consent"] == "unable"
    user_row = rows_of(inserted, "raw_users")[-1]
    assert user_row["linked_survivor_id"] == SURVIVOR
    assert user_row["role"] == SURVIVOR_ROLE
    used = rows_of(inserted, "raw_link_codes")[-1]
    assert used["action"] == link_codes.ACTION_USED


def test_本人の記録の詳細に家族が登録した記録と出る():
    payload = survivors.detail_payload(proxy_row(self_user_id=SELF_UID), role=SURVIVOR_ROLE, user_id=SELF_UID)
    assert payload["registered_by_family"] is True


# --- 本人が家族を招く・外す ---


def own_record(monkeypatch) -> None:
    sign_in(monkeypatch, uid=SELF_UID, role=SURVIVOR_ROLE, linked_survivor_id=SURVIVOR)
    set_record(monkeypatch, proxy_row(self_user_id=SELF_UID))


def test_本人は家族の招待コードを発行できる(client, monkeypatch, inserted):
    own_record(monkeypatch)

    res = client.post("/api/family/invites", headers=BEARER)

    assert res.status_code == 201, res.text
    code = res.json()["invite_code"]["code"]
    [row] = rows_of(inserted, "raw_link_codes")
    assert row["action"] == link_codes.ACTION_FAMILY_ISSUED
    assert row["survivor_id"] == SURVIVOR
    assert all(code not in str(value) for value in row.values())
    # 本人をつなぐ確認コードとはソルトが違う（同じ被災者のIDでもハッシュが重ならない）
    assert not codes.verify_code(code, salt=SURVIVOR, code_hash=row["code_hash"])


def test_他人の記録には招待コードを出せない(client, monkeypatch, inserted):
    sign_in(monkeypatch, uid=SELF_UID, role=SURVIVOR_ROLE, linked_survivor_id=SURVIVOR)
    set_record(monkeypatch, proxy_row(self_user_id="uid-self-9"))

    assert client.post("/api/family/invites", headers=BEARER).status_code == 400
    assert inserted == []


def test_招待コードで家族としてつながる(client, monkeypatch, inserted):
    own_record(monkeypatch)
    code = client.post("/api/family/invites", headers=BEARER).json()["invite_code"]["code"]
    [issued] = rows_of(inserted, "raw_link_codes")

    sign_in(monkeypatch, uid=OTHER_FAMILY_UID, role=VOL)
    monkeypatch.setattr(
        link_codes,
        "fetch_valid_family_codes",
        lambda: [{"code_hash": issued["code_hash"], "survivor_id": SURVIVOR, "org_id": ""}],
    )
    monkeypatch.setattr(link_codes, "count_recent_failures", lambda user_id: 0)

    res = client.post("/api/family/join", headers=BEARER, json={"code": code})

    assert res.status_code == 200, res.text
    [link] = rows_of(inserted, "raw_family_links")
    assert link["user_id"] == OTHER_FAMILY_UID
    assert link["via"] == family.VIA_INVITE
    assert link["action"] == family.ACTION_LINK
    used = rows_of(inserted, "raw_link_codes")[-1]
    assert used["action"] == link_codes.ACTION_FAMILY_USED
    # 招待コードは、本人をつなぐ確認コードとしては使えない（照合の相手に出てこない）
    assert issued["action"] != link_codes.ACTION_ISSUED


def test_招待コードが違えば400で誤りを数える(client, monkeypatch, inserted):
    sign_in(monkeypatch, uid=OTHER_FAMILY_UID, role=VOL)
    monkeypatch.setattr(link_codes, "fetch_valid_family_codes", lambda: [])
    monkeypatch.setattr(link_codes, "count_recent_failures", lambda user_id: 0)

    res = client.post("/api/family/join", headers=BEARER, json={"code": "ABCD2345"})

    assert res.status_code == 400
    [failed] = rows_of(inserted, "raw_link_codes")
    assert failed["action"] == link_codes.ACTION_FAILED
    assert failed["created_by"] == OTHER_FAMILY_UID
    assert failed["created_role"] == VOL
    assert "ABCD2345" not in str(failed)


def test_招待コードを何度も間違えたら429(client, monkeypatch, inserted):
    sign_in(monkeypatch, uid=OTHER_FAMILY_UID, role=VOL)
    monkeypatch.setattr(
        link_codes, "count_recent_failures", lambda user_id: link_codes.MAX_FAILURES_PER_HOUR
    )

    res = client.post("/api/family/join", headers=BEARER, json={"code": "ABCD2345"})

    assert res.status_code == 429
    assert inserted == []


def test_すでにつながっている家族は招待コードを使わない(client, monkeypatch, inserted):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row(self_user_id=SELF_UID))
    monkeypatch.setattr(
        link_codes,
        "resolve_family_code",
        lambda code, user_id, user_role: link_codes.ResolvedCode(
            code_hash="sha256$x", survivor_id=SURVIVOR, org_id=""
        ),
    )

    res = client.post("/api/family/join", headers=BEARER, json={"code": "ABCD2345"})

    assert res.status_code == 400
    assert inserted == []


def test_本人はつながっている家族を見られる(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=SURVIVOR_ROLE, linked_survivor_id=SURVIVOR)
    monkeypatch.setattr(
        family,
        "list_links",
        lambda survivor_id: [
            {"survivor_id": survivor_id, "user_id": FAMILY_UID, "via": "proxy", "linked_at": NOW}
        ],
    )
    monkeypatch.setattr(family, "display_names", lambda user_ids: {FAMILY_UID: "架空 太郎"})

    res = client.get("/api/family/members", headers=BEARER)

    assert res.status_code == 200, res.text
    [member] = res.json()["members"]
    assert member["user_id"] == FAMILY_UID
    assert member["display_name"] == "架空 太郎"
    assert member["via_label"] == family.VIA_LABELS["proxy"]
    # 家族の連絡先・住所は返さない
    assert "contact" not in member
    assert "address" not in member


def test_本人は家族を外せて外された家族は404(client, monkeypatch, inserted):
    own_record(monkeypatch)
    monkeypatch.setattr(
        family,
        "list_links",
        lambda survivor_id: [
            {"survivor_id": survivor_id, "user_id": FAMILY_UID, "via": "proxy", "linked_at": NOW}
        ],
    )

    res = client.post(f"/api/family/members/{FAMILY_UID}/remove", headers=BEARER)

    assert res.status_code == 200, res.text
    [row] = rows_of(inserted, "raw_family_links")
    assert row["action"] == family.ACTION_UNLINK
    assert row["user_id"] == FAMILY_UID
    assert row["survivor_id"] == SURVIVOR
    assert row["created_by"] == SELF_UID

    # 外したあとは stg_family_links から消え、family_user_ids にも入らない → 404
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    set_record(monkeypatch, proxy_row(self_user_id=SELF_UID, family_user_ids=[]))
    assert client.get(f"/api/family/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


def test_つながっていない人は外せない(client, monkeypatch, inserted):
    own_record(monkeypatch)
    monkeypatch.setattr(family, "list_links", lambda survivor_id: [])

    res = client.post(f"/api/family/members/{OTHER_FAMILY_UID}/remove", headers=BEARER)

    assert res.status_code == 404
    assert inserted == []


def test_家族は家族を外すAPIを使えない(client, monkeypatch):
    sign_in(monkeypatch, uid=FAMILY_UID, role=VOL)
    res = client.post(f"/api/family/members/{OTHER_FAMILY_UID}/remove", headers=BEARER)
    assert res.status_code == 403


# --- 訪問者と家族をつなぐ（似た人の知らせ） ---


def test_似た人の知らせに家族が登録した記録も入る(monkeypatch):
    seen: dict = {}

    def fake_query(sql, params=None):
        seen["sql"] = sql
        seen["params"] = params
        return [
            {
                "survivor_id": SURVIVOR,
                "full_name": "架空　花子",
                "age": None,
                "stage": "evacuation",
                "location_type": "relatives",
                "location_detail": None,
                "registered_at": NOW,
                "registered_via": survivors.VIA_PROXY,
            }
        ]

    monkeypatch.setattr(bq, "query", fake_query)

    similar = survivors.find_similar(org_id=ORG, full_name="架空 花子", age=80)

    # 年齢が空の代わりの登録も候補にする（同じ組織だけ）
    assert "age IS NULL AND registered_via = @via_proxy" in seen["sql"]
    assert seen["params"]["via_proxy"] == survivors.VIA_PROXY
    assert seen["params"]["org_id"] == ORG
    [item] = similar
    assert item["registered_by_family"] is True


def test_専門職の詳細に家族がつながっていることだけを出す():
    payload = survivors.detail_payload(proxy_row(org_id=ORG), role=PRO, user_id=PRO_UID)
    assert payload["has_family"] is True
    assert payload["proxy_consent_label"] == survivors.PROXY_CONSENTS["unable"]
    # 家族の利用者のIDそのものは返さない
    assert FAMILY_UID not in str(payload)
