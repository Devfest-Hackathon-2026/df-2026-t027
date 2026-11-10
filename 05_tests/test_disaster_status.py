"""周りの地域の状況の API のテスト（使いやすさの改修の計画.md 8章）。

Google Cloud にも Firebase にも Gemini にも接続しない。確かめるのは次のこと。

- 書けるのは専門職だけ（本人と一般の支援者は 403）。組織や立場は要求から受け取らない
- 出典のURLは必須で、http か https だけ。市区町村は一覧にあるものだけ
- 読むのは全員（組織がない本人・支援者も）。自分の市区町村と、同じ県のものだけを返す
- 更新から30日を超えたものに「古い情報かもしれません」を付ける
- 直すときも、終わったときも、版を足す（INSERT だけ）。ほかの組織の1件は 404
- 読みはパラメータ付きで、UPDATE と DELETE を使わない
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from backend import auth
from backend.config import get_settings
from backend.main import app
from backend.services import bq, disaster_status, gemini, users

PROJECT = "gdgoc-test-project"
ORG = "org_test_a"
OTHER_ORG = "org_test_b"
PRO_UID = "uid-pro-1"
VOL_UID = "uid-vol-1"
SELF_UID = "uid-self-1"
SURVIVOR = "sv_0001"
# 一覧にある団体コード（石川県の2市と、富山県の1市）
HOME = "172049"
SAME_PREF = "172057"
OTHER_PREF = "162019"
NOW = dt.datetime.now(dt.timezone.utc)
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
def no_external(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("テストから BigQuery を呼んではいけません")

    for module in (bq, disaster_status.bq):
        monkeypatch.setattr(module, "query", fail, raising=False)
        monkeypatch.setattr(module, "query_one", fail, raising=False)
        monkeypatch.setattr(module, "insert", fail, raising=False)

    def no_gemini(*args, **kwargs):
        raise AssertionError("周りの地域の状況では Gemini を呼んではいけません")

    monkeypatch.setattr(gemini, "generate_structured", no_gemini)


def sign_in(
    monkeypatch,
    *,
    uid: str,
    role: str | None,
    org_id: str | None = ORG,
    municipality_code: str | None = None,
    org_permission: str | None = None,
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
            "linked_survivor_id": SURVIVOR if role == users.ROLE_SURVIVOR else None,
            "org_name": "テスト組織",
            "municipality_code": municipality_code,
            "org_permission": org_permission,
        }
    monkeypatch.setattr(users, "fetch_user", lambda user_id: row)


def status_row(status_id: str = "ds_1", **overrides) -> dict:
    row = {
        "status_id": status_id,
        "org_id": ORG,
        "version": 1,
        "municipality_code": HOME,
        "prefecture_code": HOME[:2],
        "disaster_name": "架空の豪雨災害",
        "item_kind": "shelter",
        "state": "2か所で開設中",
        "ended": False,
        "deadline": None,
        "source_url": "https://example.jp/shelter",
        "note": None,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def new_body(**overrides) -> dict:
    body = {
        "municipality_code": HOME,
        "disaster_name": "架空の豪雨災害",
        "item_kind": "relief_act",
        "state": "適用中",
        "deadline": None,
        "source_url": "https://example.jp/relief",
        "note": None,
    }
    body.update(overrides)
    return body


def catch_writes(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(disaster_status, "save", lambda row: written.append(row) or row)
    return written


# --- 書ける人 ---


@pytest.mark.parametrize("role", [users.ROLE_SURVIVOR, users.ROLE_VOLUNTEER])
def test_本人と一般の支援者は入れられない(monkeypatch, client, role):
    sign_in(monkeypatch, uid="uid-x", role=role)
    written = catch_writes(monkeypatch)
    res = client.post("/api/disaster-status", json=new_body(), headers=BEARER)
    assert res.status_code == 403
    assert written == []


def test_本人は直すこともできない(monkeypatch, client):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    written = catch_writes(monkeypatch)
    res = client.patch("/api/disaster-status/ds_1", json={"ended": True}, headers=BEARER)
    assert res.status_code == 403
    assert written == []


def test_組織に入っていない専門職は入れられない(monkeypatch, client):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL, org_id=None)
    written = catch_writes(monkeypatch)
    res = client.post("/api/disaster-status", json=new_body(), headers=BEARER)
    assert res.status_code == 403
    assert written == []


def test_専門職が入れると版1で利用者の組織になる(monkeypatch, client):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    written = catch_writes(monkeypatch)
    # 要求に組織や立場を書いても使わない
    deadline = NOW.date() + dt.timedelta(days=30)
    body = new_body(org_id=OTHER_ORG, created_role="system", deadline=deadline.isoformat())
    res = client.post("/api/disaster-status", json=body, headers=BEARER)
    assert res.status_code == 201, res.text
    [row] = written
    assert row["org_id"] == ORG
    assert row["created_by"] == PRO_UID
    assert row["created_role"] == users.ROLE_PROFESSIONAL
    assert row["version"] == 1
    assert row["prefecture_code"] == "17"
    assert row["ended"] is False
    assert row["deadline"] == deadline
    item = res.json()["item"]
    assert item["editable"] is True
    assert item["item_kind_label"] == "災害救助法の適用"
    assert item["stale_notice"] is None
    # 入れた人のIDと組織のIDは返さない
    assert "org_id" not in item and "created_by" not in item


def test_組織のオーナーも入れられる(monkeypatch, client):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL, org_permission="owner")
    written = catch_writes(monkeypatch)
    res = client.post("/api/disaster-status", json=new_body(), headers=BEARER)
    assert res.status_code == 201
    assert len(written) == 1


# --- 入力の確かめ ---


@pytest.mark.parametrize(
    "url",
    ["", "example.jp/page", "ftp://example.jp/a", "javascript:alert(1)", "https://", "https://exa mple.jp"],
)
def test_出典のURLはhttpかhttpsだけ(monkeypatch, client, url):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    written = catch_writes(monkeypatch)
    res = client.post("/api/disaster-status", json=new_body(source_url=url), headers=BEARER)
    assert res.status_code == 400
    assert "出典のURL" in res.json()["detail"]
    assert written == []


@pytest.mark.parametrize("code", ["", "999999", "17204", "abcdef"])
def test_一覧に無い市区町村は断る(monkeypatch, client, code):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    written = catch_writes(monkeypatch)
    res = client.post("/api/disaster-status", json=new_body(municipality_code=code), headers=BEARER)
    assert res.status_code == 400
    assert "市区町村" in res.json()["detail"]
    assert written == []


@pytest.mark.parametrize(
    "overrides, word",
    [
        ({"item_kind": "unknown"}, "項目"),
        ({"disaster_name": "  "}, "災害の名前"),
        ({"state": ""}, "状態"),
        ({"deadline": "2026-13-01"}, "期限"),
        ({"deadline": "1999-01-01"}, "期限"),
    ],
)
def test_そのほかの入力の誤りは400(monkeypatch, client, overrides, word):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    written = catch_writes(monkeypatch)
    res = client.post("/api/disaster-status", json=new_body(**overrides), headers=BEARER)
    assert res.status_code == 400
    assert word in res.json()["detail"]
    assert written == []


def test_一覧に無い市区町村は読むときも断る(monkeypatch, client):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER)
    res = client.get("/api/disaster-status", params={"municipality_code": "999999"}, headers=BEARER)
    assert res.status_code == 400


# --- 読む ---


def fake_reads(monkeypatch, rows: list[dict], *, survivor_code: str | None = HOME) -> list[dict]:
    """読みを差し替える。渡されたパラメータを記録して返す。"""
    calls: list[dict] = []

    def query(sql, params=None):
        calls.append({"sql": sql, "params": dict(params or {})})
        assert "@prefecture_code" in sql and "NOT ended" in sql
        pref = params["prefecture_code"]
        return [row for row in rows if row["prefecture_code"] == pref and not row["ended"]]

    def query_one(sql, params=None):
        calls.append({"sql": sql, "params": dict(params or {})})
        if "stg_survivors" in sql:
            assert "self_user_id = @user_id" in sql
            return {"municipality_code": survivor_code}
        if "stg_organizations" in sql:
            return {"municipality_code": None}
        raise AssertionError(sql)

    monkeypatch.setattr(disaster_status.bq, "query", query)
    monkeypatch.setattr(disaster_status.bq, "query_one", query_one)
    return calls


def test_本人は自分の市区町村と同じ県だけを見る(monkeypatch, client):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR)
    calls = fake_reads(
        monkeypatch,
        [
            status_row("ds_home", municipality_code=HOME, prefecture_code="17"),
            status_row("ds_near", municipality_code=SAME_PREF, prefecture_code="17"),
            status_row("ds_far", municipality_code=OTHER_PREF, prefecture_code="16"),
            status_row("ds_ended", municipality_code=HOME, prefecture_code="17", ended=True),
        ],
    )
    res = client.get("/api/disaster-status", headers=BEARER)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["municipality_code"] == HOME
    assert body["prefecture"] == "石川県"
    assert [item["status_id"] for item in body["own"]] == ["ds_home"]
    assert [item["status_id"] for item in body["nearby"]] == ["ds_near"]
    assert body["count"] == 2
    assert body["can_edit"] is False
    # 本人の記録は自分のものだけを読む。県はパラメータで渡す
    survivor_call = next(call for call in calls if "stg_survivors" in call["sql"])
    assert survivor_call["params"] == {"survivor_id": SURVIVOR, "user_id": SELF_UID}
    list_call = next(call for call in calls if "stg_disaster_status" in call["sql"])
    assert list_call["params"] == {"prefecture_code": "17", "municipality_code": HOME}
    assert HOME not in list_call["sql"]


def test_組織がない本人も読める(monkeypatch, client):
    sign_in(monkeypatch, uid=SELF_UID, role=users.ROLE_SURVIVOR, org_id=None)
    fake_reads(monkeypatch, [status_row()])
    res = client.get("/api/disaster-status", headers=BEARER)
    assert res.status_code == 200
    assert len(res.json()["own"]) == 1


def test_支援者は活動する市区町村で見る(monkeypatch, client):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER, org_id=None, municipality_code=OTHER_PREF)
    fake_reads(
        monkeypatch,
        [
            status_row("ds_home", municipality_code=HOME, prefecture_code="17"),
            status_row("ds_far", municipality_code=OTHER_PREF, prefecture_code="16"),
        ],
    )
    res = client.get("/api/disaster-status", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["municipality_code"] == OTHER_PREF
    assert [item["status_id"] for item in body["own"]] == ["ds_far"]
    assert body["nearby"] == []


def test_市区町村が分からないときは案内を返す(monkeypatch, client):
    sign_in(monkeypatch, uid=VOL_UID, role=users.ROLE_VOLUNTEER, org_id=None, municipality_code=None)
    fake_reads(monkeypatch, [status_row()])
    res = client.get("/api/disaster-status", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["municipality_code"] is None
    assert body["own"] == [] and body["nearby"] == []
    assert body["notice"] == disaster_status.NO_AREA_NOTICE


def test_立場を選んでいない人は読めない(monkeypatch, client):
    sign_in(monkeypatch, uid="uid-new", role=None)
    res = client.get("/api/disaster-status", headers=BEARER)
    assert res.status_code == 403


def test_30日を超えたものは古い情報かもしれないと出す(monkeypatch, client):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL, municipality_code=HOME)
    fake_reads(
        monkeypatch,
        [
            status_row("ds_new", updated_at=NOW - dt.timedelta(days=29)),
            status_row("ds_old", updated_at=NOW - dt.timedelta(days=31), org_id=OTHER_ORG),
        ],
    )
    res = client.get("/api/disaster-status", headers=BEARER)
    assert res.status_code == 200
    items = {item["status_id"]: item for item in res.json()["own"]}
    assert items["ds_new"]["is_stale"] is False
    assert items["ds_new"]["stale_notice"] is None
    assert items["ds_old"]["is_stale"] is True
    assert items["ds_old"]["stale_notice"] == "古い情報かもしれません"
    # 直せるのは自分の組織の1件だけ
    assert items["ds_new"]["editable"] is True
    assert items["ds_old"]["editable"] is False
    assert res.json()["can_edit"] is True


def test_古いかの判断は30日が境目():
    assert disaster_status.STALE_DAYS == 30
    assert disaster_status.STALE_NOTICE == "古い情報かもしれません"
    assert disaster_status.is_stale(NOW - dt.timedelta(days=30, seconds=1), now=NOW)
    assert not disaster_status.is_stale(NOW - dt.timedelta(days=30), now=NOW)
    # 更新の日時が読めないものは古い扱いにする
    assert disaster_status.is_stale(None, now=NOW)


# --- 直す ---


def test_直すと版が増え前の項目を引き継ぐ(monkeypatch, client):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    written = catch_writes(monkeypatch)
    seen: list[dict] = []

    def fetch(**kwargs):
        seen.append(kwargs)
        return status_row(version=2, deadline=dt.date(2026, 12, 1))

    monkeypatch.setattr(disaster_status, "fetch_status", fetch)
    res = client.patch("/api/disaster-status/ds_1", json={"state": "1か所に減った"}, headers=BEARER)
    assert res.status_code == 200, res.text
    # 組織で絞って読む（要求の組織は使わない）
    assert seen == [{"status_id": "ds_1", "org_id": ORG}]
    [row] = written
    assert row["status_id"] == "ds_1"
    assert row["version"] == 3
    assert row["state"] == "1か所に減った"
    assert row["source_url"] == "https://example.jp/shelter"
    assert row["deadline"] == dt.date(2026, 12, 1)
    assert row["event_id"]


def test_終わったときは消さずに印の版を足す(monkeypatch, client):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    written = catch_writes(monkeypatch)
    monkeypatch.setattr(disaster_status, "fetch_status", lambda **kwargs: status_row())
    res = client.patch("/api/disaster-status/ds_1", json={"ended": True}, headers=BEARER)
    assert res.status_code == 200
    [row] = written
    assert row["ended"] is True
    assert row["version"] == 2


def test_期限は空で送ると消える(monkeypatch, client):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    written = catch_writes(monkeypatch)
    monkeypatch.setattr(
        disaster_status, "fetch_status", lambda **kwargs: status_row(deadline=dt.date(2026, 12, 1))
    )
    res = client.patch("/api/disaster-status/ds_1", json={"deadline": ""}, headers=BEARER)
    assert res.status_code == 200
    assert written[0]["deadline"] is None


def test_ほかの組織の1件は404(monkeypatch, client):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    written = catch_writes(monkeypatch)
    # 組織で絞って読むので、ほかの組織の1件は見つからない
    monkeypatch.setattr(disaster_status, "fetch_status", lambda **kwargs: None)
    res = client.patch("/api/disaster-status/ds_other", json={"ended": True}, headers=BEARER)
    assert res.status_code == 404
    assert written == []


def test_直すときも出典のURLを確かめる(monkeypatch, client):
    sign_in(monkeypatch, uid=PRO_UID, role=users.ROLE_PROFESSIONAL)
    written = catch_writes(monkeypatch)
    monkeypatch.setattr(disaster_status, "fetch_status", lambda **kwargs: status_row())
    res = client.patch("/api/disaster-status/ds_1", json={"source_url": "file:///etc"}, headers=BEARER)
    assert res.status_code == 400
    assert written == []


# --- 書き方（INSERT だけ・パラメータ付き） ---


def test_保存はINSERTだけ(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(disaster_status.bq, "insert", lambda table, row: calls.append((table, row)) or 1)
    row = disaster_status.build_row(
        org_id=ORG, created_by=PRO_UID, created_role=users.ROLE_PROFESSIONAL, fields=new_body()
    )
    disaster_status.save(row)
    assert [table for table, _ in calls] == ["raw_disaster_status"]


def test_読みはパラメータ付きでUPDATEとDELETEを使わない(monkeypatch):
    sqls: list[tuple[str, dict]] = []

    def query(sql, params=None):
        sqls.append((sql, params))
        return []

    monkeypatch.setattr(disaster_status.bq, "query", query)
    monkeypatch.setattr(disaster_status.bq, "query_one", lambda sql, params=None: query(sql, params) and None)
    disaster_status.list_for_prefecture(municipality_code=HOME)
    disaster_status.fetch_status(status_id="ds_1", org_id=ORG)
    disaster_status.fetch_survivor_municipality(survivor_id=SURVIVOR, user_id=SELF_UID)
    disaster_status.fetch_org_municipality(ORG)
    assert len(sqls) == 4
    for sql, params in sqls:
        upper = sql.upper()
        assert "UPDATE " not in upper and "DELETE " not in upper
        assert params, sql
        for value in params.values():
            assert str(value) not in sql, "値を SQL に直接書いています"


# --- 画面の作り（動きは自動テストで確かめにくいので、作りを固定する） ---

from pathlib import Path  # noqa: E402

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
DISASTER_JS = (FRONTEND / "disaster.js").read_text(encoding="utf-8")
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")


def test_ホームのカードは待たずに読む():
    # await を付けると、BigQuery の読みのあいだホームが出ない
    assert "  disaster.loadHomeStatus().catch(" in APP_JS
    assert "await disaster.loadHomeStatus()" not in APP_JS
    assert "showSkeleton(list" in DISASTER_JS


def test_専門職のホームに入れる入口がある():
    assert '{ label: "周りの地域の状況を入れる", icon: "map-pin", run: () => disaster.openEditor() }' in APP_JS
    assert 'id="screen-disaster" class="screen' in INDEX_HTML
    assert 'id="home-disaster-card"' in INDEX_HTML


def test_古い印の文言は画面に書き写さない():
    # 文言の正本は services/disaster_status.py の STALE_NOTICE
    assert disaster_status.STALE_NOTICE not in DISASTER_JS
    assert "item.stale_notice" in DISASTER_JS


def test_画面から組織や立場を送らない():
    for word in ("org_id:", "created_role", "created_by"):
        assert word not in DISASTER_JS, word
