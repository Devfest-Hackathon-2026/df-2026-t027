"""F0（ログインと立場）のテスト（要件定義書F0・10-3、開発手順書 Step 4-6）。

Google Cloud にも Firebase にも接続しない。IDトークンの確認・立場の決め方・参加コードの
照合・画面に返す値の範囲だけを確かめる。

確かめること：

- トークンなし・形が違う・確かめられないトークンでは 401 になる。
- 別のプロジェクト向けのトークンは通らない。
- 初回の登録が済んでいない人は `/api/me` では 200（登録の画面に進めるため）、
  立場が要る操作では 403 になる。
- 参加コードは組織を順に照合し、2か所に当たったときは通さない。
- 組織の一覧に参加コードのハッシュを含めない。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import auth
from backend.config import get_settings
from backend.main import app
from backend.routers import account, auth_proxy
from backend.services import assignments, bq, facilities, organizations, survivors, users

PROJECT = "gdgoc-test-project"
AUTH_DOMAIN = f"{PROJECT}.firebaseapp.com"
UID = "firebase-uid-0001"


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    """.env の値に左右されないよう、設定をテスト用に固定する。"""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setenv("FIREBASE_API_KEY", "test-api-key")
    monkeypatch.setenv("FIREBASE_AUTH_DOMAIN", AUTH_DOMAIN)
    monkeypatch.setenv("FIREBASE_APP_ID", "1:0:web:test")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    # with を使わない。lifespan を動かすと Gemini のクライアントを作りに行くため。
    return TestClient(app)


@pytest.fixture(autouse=True)
def no_bigquery(monkeypatch):
    """BigQuery を呼んだら失敗させる（呼ぶテストでは個別に差し替える）。"""

    def fail(*args, **kwargs):
        raise AssertionError("テストから BigQuery を呼んではいけません")

    monkeypatch.setattr(bq, "query", fail)
    monkeypatch.setattr(bq, "query_one", fail)
    monkeypatch.setattr(bq, "insert", fail)


def claims(**overrides) -> dict:
    base = {
        "iss": f"{auth.ISSUER_PREFIX}{PROJECT}",
        "aud": PROJECT,
        "sub": UID,
        "firebase": {"sign_in_provider": "google.com"},
    }
    base.update(overrides)
    return base


def accept_token(monkeypatch, **overrides) -> None:
    """IDトークンの確認を通ったことにする（署名の確認そのものは google-auth の責任）。"""
    monkeypatch.setattr(
        auth.id_token, "verify_firebase_token", lambda *args, **kwargs: claims(**overrides)
    )


def set_user(monkeypatch, row: dict | None) -> None:
    monkeypatch.setattr(users, "fetch_user", lambda user_id: row)


BEARER = {"Authorization": "Bearer dummy-token"}

# 支援者の登録で必須の連絡先・活動する市区町村（追加修正④）。架空の値
SUPPORTER_DETAILS = {"contact": "000-0000-0000", "municipality_code": "172049", "address": "架空町1-2"}


# --- トークンの取り出し ---


def test_トークンが無ければ401(client):
    res = client.get("/api/me")
    assert res.status_code == 401


@pytest.mark.parametrize(
    "header",
    ["", "dummy-token", "Basic dummy-token", "Bearer", "Bearer    ", "bearer"],
)
def test_形の違うヘッダは401(client, header):
    res = client.get("/api/me", headers={"Authorization": header})
    assert res.status_code == 401


def test_小文字のbearerも通る(client, monkeypatch):
    # ブラウザや中継が大文字小文字を変えることがあるため、方式の名前は区別しない
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    res = client.get("/api/me", headers={"Authorization": "bearer dummy-token"})
    assert res.status_code == 200


def test_確かめられないトークンは401(client, monkeypatch):
    def reject(*args, **kwargs):
        raise ValueError("Token expired")

    monkeypatch.setattr(auth.id_token, "verify_firebase_token", reject)
    res = client.get("/api/me", headers=BEARER)
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"


def test_時計のずれを許して確かめる(client, monkeypatch):
    # 発行されたばかりのトークンは、`iat`（発行時刻）がこの端末の時計より数秒先になることがある。
    # google-auth は既定でずれを1秒も許さず `Token used too early` で弾くため、
    # **ログインした直後だけ 401 になる**（数秒待つと通る）。実機で実際に起きた。
    # デモ当日の「その場でログインして登録する」で踏むので、ずれを許す値を必ず渡す。
    captured: dict = {}

    def fake_verify(token, request, **kwargs):
        captured.update(kwargs)
        return claims()

    monkeypatch.setattr(auth.id_token, "verify_firebase_token", fake_verify)
    set_user(monkeypatch, None)

    client.get("/api/me", headers=BEARER)

    assert captured.get("clock_skew_in_seconds", 0) >= 30


def test_別のプロジェクト宛てのトークンは401(client, monkeypatch):
    accept_token(monkeypatch, iss=f"{auth.ISSUER_PREFIX}other-project")
    res = client.get("/api/me", headers=BEARER)
    assert res.status_code == 401


def test_利用者のIDが無いトークンは401(client, monkeypatch):
    accept_token(monkeypatch, sub="")
    res = client.get("/api/me", headers=BEARER)
    assert res.status_code == 401


def test_プロジェクトIDが未設定なら500(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    get_settings.cache_clear()
    res = client.get("/api/me", headers=BEARER)
    assert res.status_code == 500


# --- 立場の決め方（要件定義書10-3） ---


def test_未登録でもmeは200で登録前と分かる(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    res = client.get("/api/me", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["registered"] is False
    assert body["role"] is None
    assert body["user_id"] == UID
    assert body["login_method"] == "google"


def test_meは立場と組織をstg_usersから返す(client, monkeypatch):
    accept_token(monkeypatch, **{"firebase": {"sign_in_provider": "phone"}})
    set_user(
        monkeypatch,
        {
            "user_id": UID,
            "org_id": "org_wajima",
            "org_name": "輪島市",
            "role": "professional",
            "profession": "保健師",
            "display_name": "山田",
            "login_method": "google",
            "linked_survivor_id": None,
        },
    )
    body = client.get("/api/me", headers=BEARER).json()
    assert body["role"] == "professional"
    assert body["org_id"] == "org_wajima"
    assert body["org_name"] == "輪島市"
    assert body["profession"] == "保健師"
    # ログインの方法は、保存された値ではなく今のトークンのものを返す
    assert body["login_method"] == "phone"


def test_要求に書いた立場や組織は使われない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(
        monkeypatch,
        {
            "user_id": UID,
            "org_id": "org_wajima",
            "org_name": "輪島市",
            "role": "volunteer",
            "profession": None,
            "display_name": "佐藤",
            "login_method": "google",
            "linked_survivor_id": None,
        },
    )
    res = client.get(
        "/api/me",
        headers={**BEARER, "X-Role": "professional"},
        params={"role": "professional", "org_id": "org_suzu"},
    )
    body = res.json()
    assert body["role"] == "volunteer"
    assert body["org_id"] == "org_wajima"


def test_立場の読み取りに失敗したら503(client, monkeypatch):
    accept_token(monkeypatch)

    def unavailable(user_id):
        raise bq.BigQueryUnavailable("認証切れ")

    monkeypatch.setattr(users, "fetch_user", unavailable)
    res = client.get("/api/me", headers=BEARER)
    assert res.status_code == 503


def test_登録前は立場の要る操作が403():
    unregistered = auth.CurrentUser(uid=UID, login_method="google")
    with pytest.raises(Exception) as kept:
        auth.registered_user(unregistered)
    assert getattr(kept.value, "status_code", None) == 403


def test_使えない立場は403():
    guard = auth.require_roles("professional")
    volunteer = auth.CurrentUser(
        uid=UID, login_method="google", role="volunteer", org_id="org_wajima"
    )
    with pytest.raises(Exception) as kept:
        guard(volunteer)
    assert getattr(kept.value, "status_code", None) == 403


def test_使える立場は通る():
    guard = auth.require_roles("professional", "volunteer")
    volunteer = auth.CurrentUser(
        uid=UID, login_method="google", role="volunteer", org_id="org_wajima"
    )
    assert guard(volunteer) is volunteer


# --- 組織の一覧（参加コードのハッシュを外へ出さない） ---


def test_組織の一覧にトークンが要る(client):
    assert client.get("/api/organizations").status_code == 401


def test_組織の一覧はハッシュの列を読まない(monkeypatch):
    captured: dict[str, str] = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        return [{"org_id": "org_wajima", "org_name": "輪島市", "prefecture": "石川県"}]

    monkeypatch.setattr(bq, "query", fake_query)
    rows = organizations.list_organizations()
    assert rows[0]["org_name"] == "輪島市"
    assert "join_code_hash" not in captured["sql"]
    assert "SELECT *" not in captured["sql"]


# --- 参加コードの照合（ナレッジベース06「範囲を順に試す」） ---


def _org_rows(code_by_column: dict[str, dict[str, str]]) -> list[dict]:
    """組織ごとに、列ごとのコードをハッシュにした行を作る。"""
    from backend.services import codes

    rows = []
    for org_id, columns in code_by_column.items():
        row: dict[str, str | None] = {"org_id": org_id}
        for column, code in columns.items():
            row[column] = codes.hash_code(code, salt=org_id) if code else None
        for column, _ in organizations.JOIN_CODE_COLUMNS:
            row.setdefault(column, None)
        rows.append(row)
    return rows


def _with_orgs(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(bq, "query", lambda sql, params=None: rows)


def test_参加コードから組織と立場が決まる(monkeypatch):
    _with_orgs(
        monkeypatch,
        _org_rows(
            {
                "org_wajima": {"pro_join_code_hash": "AAAA2345", "vol_join_code_hash": "BBBB2345"},
                "org_suzu": {"pro_join_code_hash": "CCCC2345"},
            }
        ),
    )
    assert organizations.resolve_join_code("AAAA2345") == ("org_wajima", "professional")
    assert organizations.resolve_join_code("BBBB2345") == ("org_wajima", "volunteer")
    assert organizations.resolve_join_code("CCCC2345") == ("org_suzu", "professional")


def test_参加コードは全角や区切りが入っても通る(monkeypatch):
    _with_orgs(
        monkeypatch, _org_rows({"org_wajima": {"pro_join_code_hash": "AAAA2345"}})
    )
    assert organizations.resolve_join_code("aaaa-2345") == ("org_wajima", "professional")
    assert organizations.resolve_join_code("ＡＡＡＡ２３４５") == ("org_wajima", "professional")


@pytest.mark.parametrize("given", ["", "   ", "XXXX2345", "AAA", None, "あいうえおか"])
def test_合わない参加コードはNone(monkeypatch, given):
    _with_orgs(
        monkeypatch, _org_rows({"org_wajima": {"pro_join_code_hash": "AAAA2345"}})
    )
    assert organizations.resolve_join_code(given) is None


def test_同じコードが2か所に当たったら通さない(monkeypatch):
    # 組織も立場も決められないため、どちらも採らない（別の組織の記録を読める経路を作らない）
    _with_orgs(
        monkeypatch,
        _org_rows(
            {
                "org_wajima": {"pro_join_code_hash": "SAME2345"},
                "org_suzu": {"pro_join_code_hash": "SAME2345"},
            }
        ),
    )
    assert organizations.resolve_join_code("SAME2345") is None


def test_照合は専門職を先に試す():
    assert [role for _, role in organizations.JOIN_CODE_COLUMNS] == [
        "professional",
        "volunteer",
    ]


# --- 支援者の登録 ---


def test_参加コードが違えば登録できない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    monkeypatch.setattr(organizations, "resolve_join_code", lambda code: None)
    # 組織のコードに合わなければ施設のコードとして照合する（使いやすさの改修の計画.md 9章）
    monkeypatch.setattr(facilities, "resolve_staff_join_code", lambda code: None)
    res = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json={"join_code": "XXXX2345", "display_name": "山田", **SUPPORTER_DETAILS},
    )
    assert res.status_code == 400
    # どの組織のコードかを漏らさない
    assert "org_" not in res.text


def test_専門職は職種が必要(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    monkeypatch.setattr(
        organizations, "resolve_join_code", lambda code: ("org_wajima", "professional")
    )
    res = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json={"join_code": "AAAA2345", "display_name": "山田", **SUPPORTER_DETAILS},
    )
    assert res.status_code == 400
    # 「入れたコードが専門職用だった」ことまで伝える（取り違えが実際に起きた）
    body = res.json()["detail"]
    assert "専門職用" in body
    assert "一般の支援者用" in body


def test_一般の支援者は職種を持たない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    monkeypatch.setattr(
        organizations, "resolve_join_code", lambda code: ("org_wajima", "volunteer")
    )
    monkeypatch.setattr(organizations, "fetch_organization", lambda org_id: {"org_name": "輪島市"})
    written: dict[str, object] = {}
    monkeypatch.setattr(
        users,
        "register_user",
        lambda **kwargs: written.update(kwargs) or kwargs,
    )
    res = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json={
            "join_code": "BBBB2345",
            "display_name": "佐藤",
            "profession": "保健師",
            **SUPPORTER_DETAILS,
        },
    )
    assert res.status_code == 201
    body = res.json()
    assert body["role"] == "volunteer"
    assert body["profession"] is None
    assert written["profession"] is None
    assert written["org_id"] == "org_wajima"


def test_表示名が空なら登録できない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    res = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json={"join_code": "AAAA2345", "display_name": "  "},
    )
    assert res.status_code == 400


def test_登録済みの人は二重に登録できない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(
        monkeypatch,
        {
            "user_id": UID,
            "org_id": "org_wajima",
            "org_name": "輪島市",
            "role": "professional",
            "profession": "保健師",
            "display_name": "山田",
            "login_method": "google",
            "linked_survivor_id": None,
        },
    )
    res = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json={"join_code": "AAAA2345", "display_name": "山田"},
    )
    assert res.status_code == 409


# --- 組織がない支援者の登録（要望1） ---


def test_参加コードを入れないと組織なしの一般の支援者になる(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    written: dict[str, object] = {}
    monkeypatch.setattr(
        users, "register_user", lambda **kwargs: written.update(kwargs) or kwargs
    )
    res = client.post(
        "/api/onboarding/supporter",
        headers=BEARER,
        json={"display_name": "佐藤", **SUPPORTER_DETAILS},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["role"] == "volunteer"
    assert body["org_id"] == ""
    # /api/me の registered は、立場だけで true になる（組織が無くても。要望1）
    assert body["registered"] is True
    assert body["has_org"] is False
    assert written["org_id"] == ""


def test_組織がない人はmeで登録済みと分かるが記録のAPIは使えない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(
        monkeypatch,
        {
            "user_id": UID,
            "org_id": "",
            "org_name": None,
            "role": "volunteer",
            "profession": None,
            "display_name": "佐藤",
            "login_method": "phone",
            "linked_survivor_id": None,
        },
    )
    me = client.get("/api/me", headers=BEARER).json()
    assert me["registered"] is True
    assert me["has_org"] is False

    res = client.get("/api/survivors", headers=BEARER)
    assert res.status_code == 403


def test_組織がない人もプロフィールは変えられる(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(
        monkeypatch,
        {
            "user_id": UID,
            "org_id": "",
            "org_name": None,
            "role": "volunteer",
            "profession": None,
            "display_name": "佐藤",
            "login_method": "phone",
            "linked_survivor_id": None,
        },
    )
    written: dict[str, object] = {}
    monkeypatch.setattr(
        users, "register_user", lambda **kwargs: written.update(kwargs) or kwargs
    )
    res = client.patch(
        "/api/me",
        headers=BEARER,
        json={"display_name": "佐藤 次郎", "profession": None, **SUPPORTER_DETAILS},
    )
    assert res.status_code == 200
    assert written["org_id"] == ""


def test_組織がない人が参加コードで組織に入れる(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(
        monkeypatch,
        {
            "user_id": UID,
            "org_id": "",
            "org_name": None,
            "role": "volunteer",
            "profession": None,
            "display_name": "佐藤",
            "login_method": "phone",
            "linked_survivor_id": None,
        },
    )
    monkeypatch.setattr(
        organizations, "resolve_join_code", lambda code: ("org_wajima", "professional")
    )
    monkeypatch.setattr(organizations, "fetch_organization", lambda org_id: {"org_name": "輪島市"})
    written: dict[str, object] = {}
    monkeypatch.setattr(
        users, "register_user", lambda **kwargs: written.update(kwargs) or kwargs
    )
    res = client.post(
        "/api/me/join", headers=BEARER, json={"join_code": "BBBB2345", "profession": "保健師"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["role"] == "professional"
    assert body["org_id"] == "org_wajima"
    assert written["org_id"] == "org_wajima"


def test_すでに組織に入っていると参加コードは使えない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(
        monkeypatch,
        {
            "user_id": UID,
            "org_id": "org_wajima",
            "org_name": "輪島市",
            "role": "volunteer",
            "profession": None,
            "display_name": "佐藤",
            "login_method": "phone",
            "linked_survivor_id": None,
        },
    )
    res = client.post("/api/me/join", headers=BEARER, json={"join_code": "BBBB2345"})
    assert res.status_code == 400


def test_本人や専門職は組織なし専用のAPIを使えない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(
        monkeypatch,
        {
            "user_id": UID,
            "org_id": "org_wajima",
            "org_name": "輪島市",
            "role": "professional",
            "profession": "保健師",
            "display_name": "山田",
            "login_method": "phone",
            "linked_survivor_id": None,
        },
    )
    assert client.post("/api/me/join", headers=BEARER, json={}).status_code == 403
    assert client.post("/api/organizations", headers=BEARER, json={}).status_code == 403


# --- 組織を作る（要望2） ---


def test_組織がない人が組織を作ると専門職になる(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(
        monkeypatch,
        {
            "user_id": UID,
            "org_id": "",
            "org_name": None,
            "role": "volunteer",
            "profession": None,
            "display_name": "佐藤",
            "login_method": "phone",
            "linked_survivor_id": None,
        },
    )
    monkeypatch.setattr(organizations, "fetch_by_municipality", lambda code: [])
    inserted: dict[str, dict] = {}
    monkeypatch.setattr(
        organizations.bq, "insert", lambda table, row, **kw: inserted.setdefault(table, row)
    )
    written: dict[str, object] = {}
    monkeypatch.setattr(
        users, "register_user", lambda **kwargs: written.update(kwargs) or kwargs
    )
    res = client.post(
        "/api/organizations",
        headers=BEARER,
        json={
            "org_name": "きずな会",
            "prefecture": "石川県",
            "municipality_code": "172049",
            "profession": "保健師",
        },
    )
    assert res.status_code == 201
    body = res.json()
    assert body["me"]["role"] == "professional"
    assert body["organization"]["org_name"] == "きずな会"
    # 参加コードは、この応答でしか返らない
    assert body["pro_join_code"]
    assert body["vol_join_code"]
    assert written["org_id"] == inserted["raw_organizations"]["org_id"]
    assert written["role"] == "professional"


def test_組織を作るのは組織がない人だけ(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(
        monkeypatch,
        {
            "user_id": UID,
            "org_id": "org_wajima",
            "org_name": "輪島市",
            "role": "volunteer",
            "profession": None,
            "display_name": "佐藤",
            "login_method": "phone",
            "linked_survivor_id": None,
        },
    )
    res = client.post(
        "/api/organizations",
        headers=BEARER,
        json={"org_name": "きずな会", "prefecture": "石川県", "municipality_code": "172049"},
    )
    assert res.status_code == 400


# --- 本人の登録 ---


def _survivor_body(**overrides) -> dict:
    body = {
        "municipality_code": "172049",
        "full_name": "田中",
        "age": 78,
        "household_size": 2,
        "stage": "evacuation",
        "location_type": "home",
    }
    body.update(overrides)
    return body


def _one_organization(code: str) -> list[dict]:
    return [{"org_id": "org_wajima", "org_name": "輪島市", "municipality_code": code}]


def _insert_rows(monkeypatch) -> dict[str, list[dict]]:
    """書いた行を表ごとに集める（BigQuery には書かない）。"""
    rows: dict[str, list[dict]] = {}

    def fake_insert(table, row, **kwargs):
        rows.setdefault(table, []).append(row)
        return 1

    monkeypatch.setattr(bq, "insert", fake_insert)
    return rows


def _known_organizations(monkeypatch, *known: str) -> None:
    """実在する組織を決める（それ以外のIDは None＝無い）。"""
    names = {"org_wajima": "輪島市", "org_suzu": "珠洲市", "org_far": "遠くの会"}
    monkeypatch.setattr(
        organizations,
        "fetch_organization",
        lambda org_id: (
            {"org_id": org_id, "org_name": names.get(org_id, org_id)} if org_id in known else None
        ),
    )


def test_組織を選ばずに登録できる(client, monkeypatch):
    # 追加修正⑦。支援の組織は任意。空なら組織なしで登録する（組織なしでも使える。追加修正②）
    accept_token(monkeypatch)
    set_user(monkeypatch, None)

    def must_not_read(*args, **kwargs):
        raise AssertionError("組織を選んでいないときは、組織を読まない")

    monkeypatch.setattr(organizations, "fetch_organization", must_not_read)
    monkeypatch.setattr(organizations, "fetch_by_municipality", must_not_read)
    rows = _insert_rows(monkeypatch)
    res = client.post(
        "/api/onboarding/survivor", headers=BEARER, json=_survivor_body(municipality_code="131016")
    )
    assert res.status_code == 201
    assert rows["raw_survivors"][0]["org_id"] == ""
    assert rows["raw_survivors"][0]["municipality_code"] == "131016"
    assert rows["raw_users"][0]["org_id"] == ""
    assert rows["raw_users"][0]["role"] == users.ROLE_SURVIVOR
    body = res.json()
    assert body["registered"] is True
    assert body["has_org"] is False
    assert body["survivor_id"] == rows["raw_survivors"][0]["survivor_id"]


def test_選んだ組織で登録できる(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    _known_organizations(monkeypatch, "org_wajima")
    rows = _insert_rows(monkeypatch)
    res = client.post(
        "/api/onboarding/survivor", headers=BEARER, json=_survivor_body(org_id="org_wajima")
    )
    assert res.status_code == 201
    assert rows["raw_survivors"][0]["org_id"] == "org_wajima"
    assert rows["raw_users"][0]["org_id"] == "org_wajima"
    assert res.json()["org_name"] == "輪島市"


def test_住んでいる市区町村と関係のない組織も選べる(client, monkeypatch):
    # 全国のどの組織でも選べる（追加修正⑦）。市区町村を受け持つ組織に限らない
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    _known_organizations(monkeypatch, "org_far")
    rows = _insert_rows(monkeypatch)
    res = client.post(
        "/api/onboarding/survivor",
        headers=BEARER,
        json=_survivor_body(municipality_code="131016", org_id="org_far"),
    )
    assert res.status_code == 201
    assert rows["raw_survivors"][0]["org_id"] == "org_far"


def test_存在しない組織のIDを送ると400(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    _known_organizations(monkeypatch, "org_wajima")
    res = client.post(
        "/api/onboarding/survivor", headers=BEARER, json=_survivor_body(org_id="org_fake")
    )
    assert res.status_code == 400
    assert "組織" in res.json()["detail"]


def test_組織が複数あっても409で止めない(client, monkeypatch):
    # 前の「複数あるときに選ぶ欄」（409）は、登録の画面の「支援の組織」の欄に置き換えた
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    monkeypatch.setattr(
        organizations,
        "fetch_by_municipality",
        lambda code: [{"org_id": "org_a"}, {"org_id": "org_b"}],
    )
    _insert_rows(monkeypatch)
    res = client.post("/api/onboarding/survivor", headers=BEARER, json=_survivor_body())
    assert res.status_code == 201


# --- 本人が支援の組織を選び直す（追加修正⑦） ---

ORGLESS_SURVIVOR = {
    "user_id": UID,
    "org_id": "",
    "role": "survivor",
    "display_name": "田中",
    "linked_survivor_id": "self_x",
}


def _orgless_record(**overrides) -> dict:
    row = {
        "survivor_id": "self_x",
        "org_id": "",
        "version": 1,
        "full_name": "田中",
        "age": 78,
        "household_size": 2,
        "stage": "evacuation",
        "location_type": "home",
        "self_user_id": UID,
        "registered_via": "self",
        "municipality_code": "172049",
        "address": "河井町1-1",
    }
    row.update(overrides)
    return row


def _no_assignments(monkeypatch) -> None:
    monkeypatch.setattr(assignments, "list_for_survivor", lambda **kwargs: [])


def test_組織がない本人が組織を選んでつなげる(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, ORGLESS_SURVIVOR)
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: _orgless_record())
    _known_organizations(monkeypatch, "org_wajima")
    _no_assignments(monkeypatch)
    rows = _insert_rows(monkeypatch)
    res = client.post("/api/me/organization", headers=BEARER, json={"org_id": "org_wajima"})
    assert res.status_code == 200
    survivor_row = rows["raw_survivors"][0]
    assert survivor_row["org_id"] == "org_wajima"
    assert survivor_row["version"] == 2
    # 前の版の内容は引き継ぐ（住所が消えない）
    assert survivor_row["address"] == "河井町1-1"
    assert survivor_row["municipality_code"] == "172049"
    assert rows["raw_users"][0]["org_id"] == "org_wajima"
    assert rows["raw_users"][0]["linked_survivor_id"] == "self_x"
    body = res.json()
    assert body["has_org"] is True
    assert body["org_name"] == "輪島市"
    # 組織がなかったので、外す担当も無い
    assert "raw_assignment_events" not in rows


def test_組織を変えると記録の版を足し前の組織の担当を外す(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, {**ORGLESS_SURVIVOR, "org_id": "org_wajima"})
    monkeypatch.setattr(
        survivors,
        "fetch_survivor",
        lambda survivor_id: _orgless_record(org_id="org_wajima", version=3),
    )
    _known_organizations(monkeypatch, "org_wajima", "org_suzu")
    seen: dict[str, str] = {}

    def fake_assignments(**kwargs):
        seen.update(kwargs)
        return [
            {"assignment_id": "asg_1", "supporter_user_id": "vol_1", "status": "active"},
            {"assignment_id": "asg_2", "supporter_user_id": "vol_2", "status": "pending"},
            {"assignment_id": "asg_3", "supporter_user_id": "vol_3", "status": "removed"},
        ]

    monkeypatch.setattr(assignments, "list_for_survivor", fake_assignments)
    rows = _insert_rows(monkeypatch)
    res = client.post("/api/me/organization", headers=BEARER, json={"org_id": "org_suzu"})
    assert res.status_code == 200
    # 前の組織の担当を読む
    assert seen == {"survivor_id": "self_x", "org_id": "org_wajima"}
    # 有効・承認待ちの担当を外す（外れているものは書かない）。消さずに「外した」を足す
    removed = rows["raw_assignment_events"]
    assert [row["assignment_id"] for row in removed] == ["asg_1", "asg_2"]
    assert {row["action"] for row in removed} == {"removed"}
    assert {row["org_id"] for row in removed} == {"org_wajima"}
    # 記録には新しい版を足す（上書きしない）
    assert rows["raw_survivors"][0]["org_id"] == "org_suzu"
    assert rows["raw_survivors"][0]["version"] == 4
    assert rows["raw_users"][0]["org_id"] == "org_suzu"
    assert res.json()["org_name"] == "珠洲市"


def test_組織を外せる(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, {**ORGLESS_SURVIVOR, "org_id": "org_wajima"})
    monkeypatch.setattr(
        survivors, "fetch_survivor", lambda survivor_id: _orgless_record(org_id="org_wajima")
    )
    _no_assignments(monkeypatch)
    rows = _insert_rows(monkeypatch)
    res = client.post("/api/me/organization", headers=BEARER, json={"org_id": ""})
    assert res.status_code == 200
    assert rows["raw_survivors"][0]["org_id"] == ""
    assert rows["raw_users"][0]["org_id"] == ""
    assert res.json()["has_org"] is False


def test_同じ組織には変えられない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, {**ORGLESS_SURVIVOR, "org_id": "org_wajima"})
    _known_organizations(monkeypatch, "org_wajima")
    res = client.post("/api/me/organization", headers=BEARER, json={"org_id": "org_wajima"})
    assert res.status_code == 400


def test_存在しない組織には変えられない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, ORGLESS_SURVIVOR)
    _known_organizations(monkeypatch, "org_wajima")
    res = client.post("/api/me/organization", headers=BEARER, json={"org_id": "org_fake"})
    assert res.status_code == 400


def test_組織を変える途中で落ちてもやり直せる(client, monkeypatch):
    # 記録の版は書けたが利用者の行が書けなかった。もう一度押すと、利用者の行だけ書く
    accept_token(monkeypatch)
    set_user(monkeypatch, {**ORGLESS_SURVIVOR, "org_id": "org_wajima"})
    monkeypatch.setattr(
        survivors, "fetch_survivor", lambda survivor_id: _orgless_record(org_id="org_suzu")
    )
    _known_organizations(monkeypatch, "org_suzu")
    _no_assignments(monkeypatch)
    rows = _insert_rows(monkeypatch)
    res = client.post("/api/me/organization", headers=BEARER, json={"org_id": "org_suzu"})
    assert res.status_code == 200
    assert "raw_survivors" not in rows
    assert rows["raw_users"][0]["org_id"] == "org_suzu"


def test_他人の記録では組織を変えられない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, ORGLESS_SURVIVOR)
    _known_organizations(monkeypatch, "org_wajima")
    monkeypatch.setattr(
        survivors, "fetch_survivor", lambda survivor_id: _orgless_record(self_user_id="someone")
    )
    res = client.post("/api/me/organization", headers=BEARER, json={"org_id": "org_wajima"})
    assert res.status_code == 400


def test_支援者は本人の組織を変えるAPIを使えない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, {**ORGLESS_SURVIVOR, "role": "volunteer", "linked_survivor_id": None})
    res = client.post("/api/me/organization", headers=BEARER, json={"org_id": "org_wajima"})
    assert res.status_code == 403


def test_組織がない本人は自分の記録を読める依存を通る():
    survivor = auth.CurrentUser(uid=UID, login_method="phone", role="survivor", org_id="")
    assert auth.registered_or_self(survivor) is survivor
    volunteer = auth.CurrentUser(uid=UID, login_method="phone", role="volunteer", org_id="")
    with pytest.raises(auth.HTTPException):
        auth.registered_or_self(volunteer)
    nobody = auth.CurrentUser(uid=UID, login_method="phone")
    with pytest.raises(auth.HTTPException):
        auth.registered_or_self(nobody)


def test_市区町村を選んでいないと登録できない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    res = client.post(
        "/api/onboarding/survivor", headers=BEARER, json=_survivor_body(municipality_code="")
    )
    assert res.status_code == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"full_name": "  "},
        {"age": 999},
        {"household_size": 0},
        {"stage": "unknown"},
        {"location_type": "unknown"},
        {"stage": None},
    ],
)
def test_入力が足りなければ登録できない(client, monkeypatch, overrides):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    res = client.post(
        "/api/onboarding/survivor", headers=BEARER, json=_survivor_body(**overrides)
    )
    assert res.status_code in (400, 422)


def test_本人の登録で記録と利用者の両方を書く(client, monkeypatch):
    accept_token(monkeypatch, **{"firebase": {"sign_in_provider": "phone"}})
    set_user(monkeypatch, None)
    _known_organizations(monkeypatch, "org_wajima")
    rows = _insert_rows(monkeypatch)
    res = client.post(
        "/api/onboarding/survivor", headers=BEARER, json=_survivor_body(org_id="org_wajima")
    )
    assert res.status_code == 201

    survivor_row = rows["raw_survivors"][0]
    user_row = rows["raw_users"][0]
    assert survivor_row["version"] == 1
    assert survivor_row["self_user_id"] == UID
    assert survivor_row["registered_via"] == survivors.VIA_SELF
    assert survivor_row["org_id"] == "org_wajima"
    assert user_row["role"] == users.ROLE_SURVIVOR
    assert user_row["login_method"] == "phone"
    # 利用者の行から自分の記録にたどれる
    assert user_row["linked_survivor_id"] == survivor_row["survivor_id"]

    body = res.json()
    assert body["role"] == "survivor"
    assert body["survivor_id"] == survivor_row["survivor_id"]


def test_住所も記録に書かれる(client, monkeypatch):
    accept_token(monkeypatch, **{"firebase": {"sign_in_provider": "phone"}})
    set_user(monkeypatch, None)
    rows = _insert_rows(monkeypatch)
    res = client.post(
        "/api/onboarding/survivor",
        headers=BEARER,
        json=_survivor_body(address="石川県輪島市○○町1-1"),
    )
    assert res.status_code == 201
    assert rows["raw_survivors"][0]["address"] == "石川県輪島市○○町1-1"
    assert rows["raw_survivors"][0]["municipality_code"] == "172049"


def test_市区町村から組織を探すAPI(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    monkeypatch.setattr(organizations, "fetch_by_municipality", _one_organization)
    res = client.get(
        "/api/organizations/by-municipality",
        params={"municipality_code": "172049"},
        headers=BEARER,
    )
    assert res.status_code == 200
    assert res.json()["organizations"][0]["org_id"] == "org_wajima"


def test_市区町村から組織を探すAPIは形が違うと400(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    res = client.get(
        "/api/organizations/by-municipality",
        params={"municipality_code": "abc"},
        headers=BEARER,
    )
    assert res.status_code == 400


def test_確認コードを入れたら市町の選択には進まない(client, monkeypatch):
    # 確認コードがあるときは、組織を読まずに照合の経路へ進む（中身は Step 5 で作った）。
    # 照合そのもののテストは 05_tests/test_f1_api.py にある
    accept_token(monkeypatch)
    set_user(monkeypatch, None)

    def must_not_read():
        raise AssertionError("確認コードの経路では、組織の一覧を読まない")

    monkeypatch.setattr(organizations, "list_organizations", must_not_read)
    monkeypatch.setattr(account.link_codes, "resolve_code", lambda code, user_id: None)

    res = client.post(
        "/api/onboarding/survivor", headers=BEARER, json={"link_code": "ABCD2345"}
    )

    assert res.status_code == 400
    assert "確認コード" in res.json()["detail"]


def test_本人の記録のIDは利用者ごとに決まる():
    first = survivors.self_survivor_id(UID)
    assert first == survivors.self_survivor_id(UID)
    assert first != survivors.self_survivor_id("other-uid")
    assert first.startswith("sv_")


def test_居場所の種類は11種類と段階は3つ():
    # 要件定義書F1の11種類・7-3-1の3段階から増減していないことを見張る
    assert len(survivors.LOCATION_TYPES) == 11
    assert len(survivors.STAGES) == 3


# --- ログイン前の設定（iPhone の Safari 対策。要件定義書10-2の5） ---


def test_ローカルではpopupでFirebaseのドメインを使う(client):
    body = client.get("/api/public-config", headers={"Host": "localhost:8000"}).json()
    assert body["login_mode"] == "popup"
    assert body["firebase"]["authDomain"] == AUTH_DOMAIN
    assert body["firebase"]["projectId"] == PROJECT


def test_本番ではredirectでアプリのドメインを使う(client):
    host = "gdgoc-app-128905595686.asia-northeast1.run.app"
    body = client.get("/api/public-config", headers={"Host": host}).json()
    assert body["login_mode"] == "redirect"
    assert body["firebase"]["authDomain"] == host


def test_ログイン前の設定に秘密情報を出さない(client):
    body = client.get("/api/public-config", headers={"Host": "localhost:8000"}).json()
    assert set(body["firebase"]) == {"apiKey", "authDomain", "projectId", "appId"}
    assert "join_code" not in client.get("/api/public-config").text


def test_画面の選択肢はバックエンドが返す(client):
    choices = client.get("/api/public-config").json()["choices"]
    assert [item["value"] for item in choices["location_types"]] == list(
        survivors.LOCATION_TYPES
    )
    assert [item["value"] for item in choices["stages"]] == list(survivors.STAGES)
    assert [item["value"] for item in choices["professions"]] == list(users.PROFESSIONS)


# --- /__/auth/* の中継 ---


def test_中継先はFirebaseのドメインだけ():
    assert auth_proxy.upstream_base() == f"https://{AUTH_DOMAIN}"


@pytest.mark.parametrize(
    "domain",
    ["example.com", "evil.firebaseapp.com.attacker.jp", "", "localhost", "http://x.web.app"],
)
def test_怪しい中継先では中継しない(monkeypatch, domain):
    # 要求の中のホスト名ではなく .env の値を使う。それでも形を確かめ、踏み台にされないようにする
    monkeypatch.setenv("FIREBASE_AUTH_DOMAIN", domain)
    get_settings.cache_clear()
    with pytest.raises(Exception) as kept:
        auth_proxy.upstream_base()
    assert getattr(kept.value, "status_code", None) == 503


def test_上にたどるパスは中継しない(client):
    res = client.get("/__/auth/../../etc/passwd")
    # ブラウザや Starlette がパスを縮めることがあるため、中継が起きないことだけを確かめる
    assert res.status_code in (400, 404)


def test_公開ドメインの許可の形():
    assert auth_proxy.ALLOWED_AUTH_DOMAIN.match("gdgoc-production-514015.firebaseapp.com")
    assert auth_proxy.ALLOWED_AUTH_DOMAIN.match("gdgoc-app.web.app")
    assert not auth_proxy.ALLOWED_AUTH_DOMAIN.match("gdgoc-app.web.app.evil.jp")


# --- 画面の入り口（未ログインではログイン画面だけ） ---


def test_動作の確認はログイン前でも通る(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_画面は配られる(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "ログイン" in res.text


def test_APIの一覧はF0の5本(client):
    paths = app.openapi()["paths"]
    for path in (
        "/api/public-config",
        "/api/organizations",
        "/api/onboarding/survivor",
        "/api/onboarding/supporter",
        "/api/me",
    ):
        assert path in paths
    assert account.router.prefix == "/api"


def test_組織がない本人は自分の記録を開ける(client, monkeypatch):
    # プロフィールの画面が使う（要望4・8・要確認E-1）。組織で絞る読みは空になる
    accept_token(monkeypatch)
    set_user(monkeypatch, ORGLESS_SURVIVOR)
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: _orgless_record())
    monkeypatch.setattr(bq, "query", lambda *a, **k: [])
    monkeypatch.setattr(bq, "query_one", lambda *a, **k: None)
    res = client.get("/api/survivors/self_x", headers=BEARER)
    assert res.status_code == 200, res.text
    assert res.json()["survivor"]["full_name"] == "田中"


def test_組織がない本人は他人の記録を開けない(client, monkeypatch):
    accept_token(monkeypatch)
    set_user(monkeypatch, ORGLESS_SURVIVOR)
    monkeypatch.setattr(
        survivors, "fetch_survivor", lambda survivor_id: _orgless_record(self_user_id="someone")
    )
    res = client.get("/api/survivors/self_x", headers=BEARER)
    assert res.status_code == 404


def test_組織がない本人は相談を送れない(client, monkeypatch):
    # 誰にも届かないため（相談のAPIは今までどおり組織を求める）
    accept_token(monkeypatch)
    set_user(monkeypatch, ORGLESS_SURVIVOR)
    res = client.get("/api/survivors", headers=BEARER)
    assert res.status_code == 403


def test_本人の登録では年齢と世帯の人数を後で入れてよい(client, monkeypatch):
    """使いやすさの改修の計画.md 4-2の3。1段目はお住まいとお名前だけにした。"""
    accept_token(monkeypatch)
    set_user(monkeypatch, None)
    rows = _insert_rows(monkeypatch)
    res = client.post(
        "/api/onboarding/survivor",
        headers=BEARER,
        json=_survivor_body(age=None, household_size=None, municipality_code="131016"),
    )
    assert res.status_code == 201, res.text
    assert rows["raw_survivors"][0]["age"] is None
    assert rows["raw_survivors"][0]["household_size"] is None
