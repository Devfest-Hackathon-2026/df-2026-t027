"""権限の自動テストをまとめる（開発手順書 Step 12-1、要件定義書10-3）。

機能ごとのテスト（`test_f1_api.py` など）は、その機能の中で権限を確かめている。
このファイルは**API を1つの表にして横から確かめる**。ねらいは2つ。

1. **API が増えたときに、権限を決め忘れたまま通らないようにする。**
   `PERMISSIONS` に載っていない API があると `test_すべてのAPIが権限の表に載っている` が落ちる。
2. 要件定義書10-3 が自動テストで求めている5つを、1か所で読めるようにする。

| 10-3 が求めるもの | ここでのテスト |
|---|---|
| 本人がほかの人の記録を読めない | `test_本人はほかの人の記録を読めない`・`can_view` の表 |
| 一般の支援者が担当でない人・担当を外れた人の記録を読めない | `test_一般の支援者は担当でない人の記録を読めない`・`can_view` の表 |
| 別の組織の記録を読めない | `test_別の組織の記録は読めない`・`can_view` の表 |
| 本人に悪化の兆しの度合いが返らない | `test_本人に悪化の兆しの度合いを返さない` |
| 使えない立場のAPIは403になる | `test_使えない立場のAPIは403`（表の全API × 使えない立場） |

Google Cloud にも Firebase にも接続しない。断るところまでで BigQuery を呼んでいないことは、
`no_bigquery`（呼んだら落ちる差し替え）が確かめている。
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
OTHER_VOL_UID = "uid-vol-2"
SELF_UID = "uid-self-1"
OTHER_SELF_UID = "uid-self-2"
NOW = dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.timezone.utc)
BEARER = {"Authorization": "Bearer dummy-token"}

SURVIVOR_ROLE = users.ROLE_SURVIVOR
PRO = users.ROLE_PROFESSIONAL
VOL = users.ROLE_VOLUNTEER
# 施設の職員（使いやすさの改修の計画.md 9章）。自分の施設だけを直し、記録のAPIはどれも使えない
FACILITY_STAFF = users.ROLE_FACILITY_STAFF
ALL_ROLES = (SURVIVOR_ROLE, PRO, VOL, FACILITY_STAFF)

# 立場で限っていない API の印
PUBLIC = "public"  # ログインしなくても使える
SIGNED_IN = "signed_in"  # ログインだけ必要（初回の登録がまだでも使える）
REGISTERED = "registered"  # 登録済みなら立場を問わない（範囲は can_view で絞る）
# 組織のオーナーだけ（専門職で、組織の中の権限が owner。追加修正⑥）。立場の表とは別に確かめる
OWNER = "owner"

# 道の `{…}` に入れる仮の値（どれも実在しない。断られるところまでしか進まない）
PATH_VALUES = {
    "survivor_id": SURVIVOR,
    "assignment_id": "asg_0001",
    "visit_id": "vis_0001",
    "consultation_id": "cns_0001",
    "support_id": "sup_0001",
    "facility_id": "fac_0001",
    "status_id": "ds_0001",
    # 本人が外す家族の利用者のID（使いやすさの改修の計画.md 10章）
    "user_id": "uid-family-1",
}

# --- 権限の表（正本は要件定義書10-5 の API の一覧と、各 router の require_roles） ---

PERMISSIONS: dict[tuple[str, str], object] = {
    # F0：ログインと立場
    ("GET", "/api/health"): PUBLIC,
    ("GET", "/api/public-config"): PUBLIC,
    ("GET", "/api/organizations"): SIGNED_IN,
    # 住んでいる市区町村から組織を探す（要望8）
    ("GET", "/api/organizations/by-municipality"): SIGNED_IN,
    ("POST", "/api/onboarding/survivor"): SIGNED_IN,
    ("POST", "/api/onboarding/supporter"): SIGNED_IN,
    ("GET", "/api/me"): SIGNED_IN,
    # プロフィールの画面（要望4）。立場・組織はここでは変えない。施設の職員も直せる（9章）
    ("PATCH", "/api/me"): (PRO, VOL, FACILITY_STAFF),
    # 組織がない支援者が使うAPI（要望1・2）。組織の有無はハンドラの中で確かめる
    ("POST", "/api/me/join"): (VOL,),
    ("POST", "/api/organizations"): (VOL,),
    # 本人が支援の組織を選び直す（つなぐ・変える・外す。追加修正⑦）
    ("POST", "/api/me/organization"): (SURVIVOR_ROLE,),
    # 組織のオーナーだけ（追加修正⑥）。参加コードを見る・発行し直す、オーナーを付ける・外す
    ("GET", "/api/organizations/join-codes"): OWNER,
    ("POST", "/api/organizations/join-codes"): OWNER,
    ("GET", "/api/organizations/members"): OWNER,
    ("POST", "/api/organizations/owners"): OWNER,
    # F1：被災者の記録と担当
    ("GET", "/api/survivors"): (PRO, VOL),
    ("POST", "/api/survivors"): (PRO,),
    ("GET", "/api/survivors/{survivor_id}"): REGISTERED,
    ("POST", "/api/survivors/{survivor_id}/location"): (PRO, SURVIVOR_ROLE),
    ("PATCH", "/api/survivors/{survivor_id}/basic"): (PRO, SURVIVOR_ROLE),
    ("POST", "/api/survivors/{survivor_id}/assignments"): (PRO,),
    ("POST", "/api/survivors/{survivor_id}/assignments/{assignment_id}/approve"): (SURVIVOR_ROLE,),
    ("POST", "/api/survivors/{survivor_id}/assignments/{assignment_id}/remove"): (
        SURVIVOR_ROLE,
        PRO,
    ),
    # F2：訪問・見守りの記録
    ("POST", "/api/survivors/{survivor_id}/visits/structure"): (PRO, VOL),
    ("POST", "/api/survivors/{survivor_id}/visits"): (PRO, VOL),
    ("POST", "/api/survivors/{survivor_id}/visits/{visit_id}/apply"): (PRO,),
    # F3：健康・介護の情報と引き継ぎシート
    ("PUT", "/api/survivors/{survivor_id}/health"): (PRO, SURVIVOR_ROLE),
    ("POST", "/api/survivors/{survivor_id}/handovers/draft"): (PRO, VOL),
    ("POST", "/api/survivors/{survivor_id}/handovers"): (PRO, VOL),
    # 引き継ぎシートの一覧（追加修正⑪）。見られる範囲は記録と同じ（load_visible が決める）
    ("GET", "/api/survivors/{survivor_id}/handovers"): REGISTERED,
    # F4：悪化の兆しの度合い
    ("PUT", "/api/survivors/{survivor_id}/assessment"): (PRO,),
    # F5：受け入れ先の施設と移る先の決定
    ("GET", "/api/facilities"): (PRO,),
    ("POST", "/api/facilities"): (PRO,),
    ("PATCH", "/api/facilities/{facility_id}"): (PRO,),
    ("POST", "/api/survivors/{survivor_id}/facility-candidates"): (PRO,),
    ("POST", "/api/survivors/{survivor_id}/placements"): (PRO,),
    # 施設の参加コード（使いやすさの改修の計画.md 9章）。施設を登録した組織の専門職だけ
    ("GET", "/api/facilities/{facility_id}/staff-join-code"): (PRO,),
    ("POST", "/api/facilities/{facility_id}/staff-join-code"): (PRO,),
    # 施設の職員が自分の施設を読む・直す（9章）。施設のIDは stg_users から読む
    ("GET", "/api/my-facility"): (FACILITY_STAFF,),
    ("PATCH", "/api/my-facility"): (FACILITY_STAFF,),
    # F6：受けられる支援と申請の状況
    # 支援の一覧（使いやすさの改修の計画.md 6-2）。一般の支援者は F6 を扱わない
    ("GET", "/api/programs"): (SURVIVOR_ROLE, PRO),
    ("GET", "/api/survivors/{survivor_id}/supports"): (SURVIVOR_ROLE, PRO),
    ("POST", "/api/survivors/{survivor_id}/support-candidates"): (SURVIVOR_ROLE, PRO),
    ("POST", "/api/survivors/{survivor_id}/supports"): (SURVIVOR_ROLE, PRO),
    ("PATCH", "/api/survivors/{survivor_id}/supports/{support_id}"): (SURVIVOR_ROLE, PRO),
    # F7：組織全体の状況
    ("GET", "/api/dashboard"): (PRO,),
    # F8：被災者からの相談
    ("POST", "/api/survivors/{survivor_id}/consultations/structure"): (SURVIVOR_ROLE,),
    ("POST", "/api/survivors/{survivor_id}/consultations"): (SURVIVOR_ROLE,),
    ("GET", "/api/consultations"): (PRO,),
    ("POST", "/api/survivors/{survivor_id}/consultations/{consultation_id}/replies"): (PRO,),
    ("PATCH", "/api/survivors/{survivor_id}/consultations/{consultation_id}"): (PRO,),
    # 相談のやり取り（要望7。要件定義書には無い追加の機能）
    ("GET", "/api/survivors/{survivor_id}/consultations/{consultation_id}/messages"): (
        SURVIVOR_ROLE,
        PRO,
    ),
    ("POST", "/api/survivors/{survivor_id}/consultations/{consultation_id}/messages"): (
        SURVIVOR_ROLE,
    ),
    # 公開の相談（追加修正②）。見るのは支援者全員（組織がなくても）、返事は専門職だけ
    ("GET", "/api/consultations/public"): (PRO, VOL),
    ("GET", "/api/consultations/public/{consultation_id}/messages"): (PRO, VOL),
    ("POST", "/api/consultations/public/{consultation_id}/replies"): (PRO,),
    # 周りの地域の状況（使いやすさの改修の計画.md 8章）。読むのは全員（組織がなくても）、
    # 入れる・直すのは専門職だけ（組織のオーナーも専門職）
    ("GET", "/api/disaster-status"): ALL_ROLES,
    ("POST", "/api/disaster-status"): (PRO,),
    ("PATCH", "/api/disaster-status/{status_id}"): (PRO,),
    # 家族などによる代わりの登録とつなぎ（使いやすさの改修の計画.md 10章）。
    # 家族として使うのは一般の支援者（組織がなくてもよい）。見てよいかは家族のつながりで決める
    ("POST", "/api/family/survivors"): (VOL,),
    ("GET", "/api/family/survivors"): (VOL,),
    ("GET", "/api/family/survivors/{survivor_id}"): (VOL,),
    ("PATCH", "/api/family/survivors/{survivor_id}/basic"): (VOL,),
    ("POST", "/api/family/survivors/{survivor_id}/location"): (VOL,),
    ("POST", "/api/family/survivors/{survivor_id}/link-code"): (VOL,),
    ("POST", "/api/family/join"): (VOL,),
    # 家族を招く・外すのは本人だけ
    ("POST", "/api/family/invites"): (SURVIVOR_ROLE,),
    ("GET", "/api/family/members"): (SURVIVOR_ROLE,),
    ("POST", "/api/family/members/{user_id}/remove"): (SURVIVOR_ROLE,),
}


def actual_endpoints() -> set[tuple[str, str]]:
    """アプリが持っている API の一覧（OpenAPI から読む。登録漏れも分かる）。"""
    found: set[tuple[str, str]] = set()
    for path, methods in app.openapi()["paths"].items():
        if not path.startswith("/api"):
            continue
        for method in methods:
            if method.upper() in ("HEAD", "OPTIONS"):
                continue
            found.add((method.upper(), path))
    return found


def role_limited() -> list[tuple[str, str, tuple[str, ...]]]:
    """立場で限っている API だけを返す。"""
    return [
        (method, path, roles)
        for (method, path), roles in PERMISSIONS.items()
        if isinstance(roles, tuple)
    ]


def needs_registration() -> list[tuple[str, str]]:
    """初回の登録が済んでいないと使えない API。"""
    return [
        (method, path)
        for (method, path), roles in PERMISSIONS.items()
        if isinstance(roles, tuple) or roles in (REGISTERED, OWNER)
    ]


def owner_only() -> list[tuple[str, str]]:
    """組織のオーナーだけが使える API（追加修正⑥）。"""
    return [(method, path) for (method, path), roles in PERMISSIONS.items() if roles == OWNER]


def url_for(path: str) -> str:
    """道の `{…}` を仮の値に置き換える。"""
    filled = path
    for name, value in PATH_VALUES.items():
        filled = filled.replace("{" + name + "}", value)
    assert "{" not in filled, f"仮の値を決めていない道があります: {path}"
    return filled


def call(client: TestClient, method: str, path: str, *, headers: dict[str, str] | None = None):
    """断られ方だけを見るので、本文は空の JSON を送る（立場の確認は本文より先に走る）。"""
    kwargs: dict[str, object] = {"headers": headers or {}}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}
    return client.request(method, url_for(path), **kwargs)


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
    """BigQuery を呼んだら落ちるようにする（断るところまでで読んでいないことの裏づけ）。"""

    def fail(*args, **kwargs):
        raise AssertionError("テストから BigQuery を呼んではいけません")

    modules = (
        bq,
        survivors.bq,
        assignments.bq,
        link_codes.bq,
        health.bq,
        visits.bq,
        handovers.bq,
        facilities.bq,
        supports.bq,
        consultations.bq,
    )
    for module in modules:
        monkeypatch.setattr(module, "query", fail, raising=False)
        monkeypatch.setattr(module, "query_one", fail, raising=False)
        monkeypatch.setattr(module, "insert", fail, raising=False)


def sign_in(
    monkeypatch,
    *,
    uid: str,
    role: str | None,
    org_id: str | None = ORG,
    org_permission: str | None = None,
) -> None:
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
            "profession": "保健師" if role == PRO else None,
            "display_name": "テスト利用者",
            "login_method": "phone",
            "linked_survivor_id": SURVIVOR if role == SURVIVOR_ROLE else None,
            "org_name": "輪島市",
            "org_permission": org_permission,
            "facility_id": PATH_VALUES["facility_id"] if role == FACILITY_STAFF else None,
        }
    monkeypatch.setattr(users, "fetch_user", lambda user_id: row)
    monkeypatch.setattr(users, "list_by_role", lambda **kwargs: [])
    monkeypatch.setattr(users, "names_by_ids", lambda **kwargs: [])


def survivor_row(**overrides) -> dict:
    """詳細の読みが返す1行（権限の確認に関わる列だけ本物に合わせる）。"""
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
        "self_user_id": SELF_UID,
        "registered_via": "professional",
        "registered_at": NOW,
        "updated_at": NOW,
        "risk_level": "high",
        "risk_next_action": "今日のうちに訪問する",
        # 根拠は JSON の列（`assessments.evidence_items` が読み戻す）。
        # 立場で分かれるのは度合いそのものなので、ここでは空にしておく
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
        "assignee_user_ids": [VOL_UID],
        "assignee_count": 1,
        "never_visited": False,
    }
    row.update(overrides)
    return row


def set_survivor(monkeypatch, row: dict | None) -> None:
    """詳細の読みを差し替える（見てよいかの確認より後ろは、すべて空で返す）。"""
    monkeypatch.setattr(survivors, "fetch_survivor", lambda survivor_id: row)
    monkeypatch.setattr(survivors, "location_history", lambda **kwargs: [])
    monkeypatch.setattr(assignments, "list_for_survivor", lambda **kwargs: [])
    monkeypatch.setattr(health, "fetch_latest", lambda **kwargs: None)
    monkeypatch.setattr(visits, "list_visits", lambda **kwargs: [])
    monkeypatch.setattr(consultations, "list_for_survivor", lambda **kwargs: [])
    monkeypatch.setattr(handovers, "list_for_survivor", lambda **kwargs: [])
    monkeypatch.setattr(supports, "list_for_survivor", lambda **kwargs: [])


# --- 1. 表が API をすべて覆っていること ---


def test_すべてのAPIが権限の表に載っている():
    """API を足したら、この表にも足す（＝権限を決める）までテストが通らない。"""
    missing = actual_endpoints() - set(PERMISSIONS)
    assert not missing, f"権限を決めていない API があります: {sorted(missing)}"


def test_表に載っているAPIは実際にある():
    """消した API が表に残っていると、通っているつもりのテストが空回りする。"""
    extra = set(PERMISSIONS) - actual_endpoints()
    assert not extra, f"無い API が表に残っています: {sorted(extra)}"


# --- 2. ログインしていなければ使えない ---


@pytest.mark.parametrize(
    ("method", "path"),
    [(m, p) for (m, p), roles in PERMISSIONS.items() if roles != PUBLIC],
)
def test_ログインしていないと401(client, method, path):
    assert call(client, method, path).status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [(m, p) for (m, p), roles in PERMISSIONS.items() if roles == PUBLIC],
)
def test_ログインしなくても使えるAPIは401にならない(client, method, path):
    assert call(client, method, path).status_code != 401


# --- 3. 初回の登録が済んでいなければ使えない ---


@pytest.mark.parametrize(("method", "path"), needs_registration())
def test_登録がまだの人は403(client, monkeypatch, method, path):
    sign_in(monkeypatch, uid=PRO_UID, role=None)
    assert call(client, method, path, headers=BEARER).status_code == 403


# --- 4. 使えない立場は403（表の全API × 使えない立場） ---


@pytest.mark.parametrize(
    ("method", "path", "role"),
    [
        (method, path, role)
        for method, path, roles in role_limited()
        for role in ALL_ROLES
        if role not in roles
    ],
)
def test_使えない立場のAPIは403(client, monkeypatch, method, path, role):
    sign_in(monkeypatch, uid=f"uid-{role}", role=role)
    res = call(client, method, path, headers=BEARER)
    assert res.status_code == 403, f"{method} {path} を {role} が使えてしまいます（{res.status_code}）"


@pytest.mark.parametrize(
    ("method", "path", "role"),
    [
        (method, path, role)
        for method, path, roles in role_limited()
        for role in ALL_ROLES
        if role in roles
    ],
)
def test_使える立場は403にならない(client, monkeypatch, method, path, role):
    """使える立場まで塞いでいないことを確かめる（403 が付きすぎると機能が死ぬ）。

    先へ進むと BigQuery を呼ぶので、403 以外であることだけを見る。
    """
    sign_in(monkeypatch, uid=f"uid-{role}", role=role)
    set_survivor(monkeypatch, survivor_row())
    try:
        res = call(client, method, path, headers=BEARER)
    except AssertionError as exc:  # no_bigquery が落とした＝立場の確認は通っている
        assert "BigQuery" in str(exc)
        return
    assert res.status_code != 403, f"{method} {path} を {role} が使えません"


@pytest.mark.parametrize(
    ("method", "path"),
    [(m, p) for (m, p), roles in PERMISSIONS.items() if roles in (REGISTERED, OWNER)],
)
def test_施設の職員は記録のAPIを使えない(client, monkeypatch, method, path):
    """立場で限っていない記録のAPI（範囲は can_view で絞る）も、施設の職員は通さない（9章）。

    施設の職員は組織を持つので、`is_registered` だけでは入口を通ってしまう。
    `auth.registered_user`・`registered_or_self` が根元で断ることを確かめる。
    """
    sign_in(monkeypatch, uid="uid-facility-staff", role=FACILITY_STAFF)
    set_survivor(monkeypatch, survivor_row())
    res = call(client, method, path, headers=BEARER)
    assert res.status_code in (403, 404), f"{method} {path} を施設の職員が使えてしまいます"


# --- 4-2. 組織のオーナーだけが使える API（追加修正⑥） ---


@pytest.mark.parametrize(
    ("method", "path", "role", "org_permission"),
    [
        (method, path, role, org_permission)
        for method, path in owner_only()
        for role, org_permission in (
            # オーナーでない専門職・一般の支援者・本人
            (PRO, None),
            (VOL, None),
            (SURVIVOR_ROLE, None),
            # 権限の値が owner でも、専門職でなければ通さない（一般の支援者・本人）
            (VOL, users.ORG_PERMISSION_OWNER),
            (SURVIVOR_ROLE, users.ORG_PERMISSION_OWNER),
        )
    ],
)
def test_オーナーでなければ403(client, monkeypatch, method, path, role, org_permission):
    sign_in(monkeypatch, uid=f"uid-{role}", role=role, org_permission=org_permission)
    res = call(client, method, path, headers=BEARER)
    assert res.status_code == 403, f"{method} {path} を {role} が使えてしまいます（{res.status_code}）"


@pytest.mark.parametrize(("method", "path"), owner_only())
def test_組織に入っていないオーナーの印では通さない(client, monkeypatch, method, path):
    sign_in(monkeypatch, uid=PRO_UID, role=PRO, org_id="", org_permission=users.ORG_PERMISSION_OWNER)
    assert call(client, method, path, headers=BEARER).status_code == 403


@pytest.mark.parametrize(("method", "path"), owner_only())
def test_オーナーは403にならない(client, monkeypatch, method, path):
    sign_in(monkeypatch, uid=PRO_UID, role=PRO, org_permission=users.ORG_PERMISSION_OWNER)
    try:
        res = call(client, method, path, headers=BEARER)
    except AssertionError as exc:  # no_bigquery が落とした＝権限の確認は通っている
        assert "BigQuery" in str(exc)
        return
    assert res.status_code != 403, f"{method} {path} をオーナーが使えません"


# --- 5. 記録の見える範囲（要件定義書10-3の4つ） ---


def test_本人はほかの人の記録を読めない(client, monkeypatch):
    sign_in(monkeypatch, uid=OTHER_SELF_UID, role=SURVIVOR_ROLE)
    set_survivor(monkeypatch, survivor_row(self_user_id=SELF_UID))

    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)

    # 権限が無いときも「無い」と同じ 404（記録の有無を分からないようにする）
    assert res.status_code == 404


def test_一般の支援者は担当でない人の記録を読めない(client, monkeypatch):
    sign_in(monkeypatch, uid=OTHER_VOL_UID, role=VOL)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[VOL_UID]))

    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


def test_一般の支援者は担当を外れたら読めなくなる(client, monkeypatch):
    # 担当を外れると stg の assignee_user_ids から消える（今有効な担当だけが入る）
    sign_in(monkeypatch, uid=VOL_UID, role=VOL)
    set_survivor(monkeypatch, survivor_row(assignee_user_ids=[]))

    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


def test_別の組織の記録は読めない(client, monkeypatch):
    sign_in(monkeypatch, uid=PRO_UID, role=PRO, org_id=OTHER_ORG)
    set_survivor(monkeypatch, survivor_row(org_id=ORG))

    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404


@pytest.mark.parametrize(
    ("role", "uid", "org_id", "assignees", "expected"),
    [
        # 本人：自分の記録だけ
        (SURVIVOR_ROLE, SELF_UID, ORG, [], True),
        (SURVIVOR_ROLE, OTHER_SELF_UID, ORG, [], False),
        # 専門職：同じ組織なら見られる。別の組織は見られない
        (PRO, PRO_UID, ORG, [], True),
        (PRO, PRO_UID, OTHER_ORG, [], False),
        # 一般の支援者：同じ組織で、今有効な担当に入っているときだけ
        (VOL, VOL_UID, ORG, [VOL_UID], True),
        (VOL, VOL_UID, ORG, [], False),
        (VOL, VOL_UID, OTHER_ORG, [VOL_UID], False),
    ],
)
def test_can_viewの表(role, uid, org_id, assignees, expected):
    """見える範囲の規則そのもの（API を通さずに確かめる）。"""
    row = survivor_row(assignee_user_ids=assignees)
    assert (
        survivors.can_view(row, role=role, user_id=uid, org_id=org_id) is expected
    )


def test_can_viewは記録が無ければ通さない():
    assert survivors.can_view(None, role=PRO, user_id=PRO_UID, org_id=ORG) is False


def test_can_viewは立場や組織が空なら通さない():
    row = survivor_row()
    assert survivors.can_view(row, role=None, user_id=PRO_UID, org_id=ORG) is False
    assert survivors.can_view(row, role=PRO, user_id=PRO_UID, org_id=None) is False


# --- 6. 本人に悪化の兆しの度合いが返らない ---


# 度合いが混ざっていないかを見る手がかり。
# 詳細では `risk`（`services/assessments.py の risk_payload`）、一覧では `risk_level` に入る。
RISK_KEYS = ("risk", "risk_level", "risk_evidence", "risk_next_action")
# 度合いの中身そのもの（鍵の名前が変わっても気づけるように、値でも見る）。
# `next_action` と `disclaimer` は度合いにしか出てこない文章
RISK_TEXTS = ("今日のうちに訪問する", assessments.DISCLAIMER)


def test_本人に悪化の兆しの度合いを返さない(client, monkeypatch):
    sign_in(monkeypatch, uid=SELF_UID, role=SURVIVOR_ROLE)
    set_survivor(monkeypatch, survivor_row())

    res = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER)

    assert res.status_code == 200
    body = res.json()
    for key in RISK_KEYS:
        assert key not in body["survivor"], f"本人の画面に {key} が返っています"
    # 鍵の名前だけでなく、根拠と次の一手の文章そのものも出ていないことを見る
    for text in RISK_TEXTS:
        assert text not in str(body), f"本人の画面に度合いの中身が返っています: {text}"


def test_専門職には悪化の兆しの度合いを返す(client, monkeypatch):
    """本人に返さない作りが、専門職にも返さない形になっていないかを確かめる。

    これが落ちると `test_本人に悪化の兆しの度合いを返さない` が空回りになる。
    """
    sign_in(monkeypatch, uid=PRO_UID, role=PRO)
    set_survivor(monkeypatch, survivor_row())

    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()

    assert "risk" in body["survivor"]
    assert body["survivor"]["risk"]["level"] == "high"
    for text in RISK_TEXTS:
        assert text in str(body), f"専門職の画面に度合いの中身が出ていません: {text}"


def test_detail_payloadは本人に度合いを入れない():
    """画面に返す形そのものを確かめる（API を通さずに）。"""
    payload = survivors.detail_payload(survivor_row(), role=SURVIVOR_ROLE, user_id=SELF_UID)

    for key in RISK_KEYS:
        assert key not in payload
    for text in RISK_TEXTS:
        assert text not in str(payload)


def test_detail_payloadは支援者には度合いを入れる():
    for role in (PRO, VOL):
        payload = survivors.detail_payload(survivor_row(), role=role, user_id=PRO_UID)
        assert "risk" in payload, f"{role} の画面に度合いが入っていません"


# --- 7. 組織につながっていない本人（追加修正②） ---

ORGLESS_SURVIVOR = "sv_orgless_1"
OTHER_ORGLESS_SURVIVOR = "sv_orgless_2"


def orgless_sign_in(monkeypatch, *, uid: str = SELF_UID, survivor_id: str = ORGLESS_SURVIVOR):
    sign_in(monkeypatch, uid=uid, role=SURVIVOR_ROLE, org_id="")
    monkeypatch.setattr(
        users,
        "fetch_user",
        lambda user_id: {
            "user_id": uid,
            "org_id": "",
            "role": SURVIVOR_ROLE,
            "profession": None,
            "display_name": "テスト本人",
            "login_method": "phone",
            "linked_survivor_id": survivor_id,
            "org_name": None,
        },
    )


def orgless_row(**overrides) -> dict:
    values = {
        "survivor_id": ORGLESS_SURVIVOR,
        "org_id": "",
        "self_user_id": SELF_UID,
        "registered_via": "self",
        "assignee_user_ids": [],
    }
    values.update(overrides)
    return survivor_row(**values)


def test_組織がない本人が自分の記録の健康の情報を書ける(client, monkeypatch):
    orgless_sign_in(monkeypatch)
    set_survivor(monkeypatch, orgless_row())
    written: list[dict] = []
    monkeypatch.setattr(health.bq, "insert", lambda table, row, **kw: written.append(row) or 1)

    res = client.put(
        f"/api/survivors/{ORGLESS_SURVIVOR}/health",
        json={"chronic_diseases": ["高血圧"], "medications": ["降圧薬"]},
        headers=BEARER,
    )

    assert res.status_code == 200, res.text
    assert written[0]["survivor_id"] == ORGLESS_SURVIVOR
    # 組織のIDは空のまま残す（本人の記録の続き。見られるのは本人だけ）
    assert written[0]["org_id"] == ""
    assert written[0]["source"] == "self"


def test_組織がない本人どうしで相手の記録は見えない(client, monkeypatch):
    # 組織のIDが空どうしでも、他人の記録は見えない（本人は自分のIDで照らす）
    orgless_sign_in(monkeypatch, uid=OTHER_SELF_UID, survivor_id=OTHER_ORGLESS_SURVIVOR)
    set_survivor(monkeypatch, orgless_row(self_user_id=SELF_UID))

    assert client.get(f"/api/survivors/{ORGLESS_SURVIVOR}", headers=BEARER).status_code == 404
    res = client.put(
        f"/api/survivors/{ORGLESS_SURVIVOR}/health",
        json={"chronic_diseases": ["高血圧"]},
        headers=BEARER,
    )
    assert res.status_code == 404


def test_組織が空の記録は組織がない支援者にも見えない():
    row = orgless_row()
    for role in (PRO, VOL):
        assert survivors.can_view(row, role=role, user_id=PRO_UID, org_id="") is False
        assert survivors.can_view(row, role=role, user_id=PRO_UID, org_id=None) is False


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/survivors"),
        ("POST", "/api/survivors"),
        ("POST", "/api/survivors/{survivor_id}/visits"),
        ("POST", "/api/survivors/{survivor_id}/visits/structure"),
        ("POST", "/api/survivors/{survivor_id}/handovers"),
        ("PUT", "/api/survivors/{survivor_id}/assessment"),
        ("GET", "/api/consultations"),
        ("GET", "/api/consultations/public"),
        ("POST", "/api/consultations/public/{consultation_id}/replies"),
        ("GET", "/api/facilities"),
        ("GET", "/api/dashboard"),
    ],
)
def test_組織がない本人が支援者向けのAPIを呼ぶと403(client, monkeypatch, method, path):
    orgless_sign_in(monkeypatch)
    set_survivor(monkeypatch, orgless_row())
    assert call(client, method, path, headers=BEARER).status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/api/survivors/{survivor_id}/health"),
        ("POST", "/api/survivors/{survivor_id}/location"),
        ("POST", "/api/survivors/{survivor_id}/consultations/structure"),
        ("POST", "/api/survivors/{survivor_id}/consultations"),
        ("POST", "/api/survivors/{survivor_id}/support-candidates"),
        ("POST", "/api/survivors/{survivor_id}/supports"),
        ("GET", "/api/programs"),
        ("GET", "/api/survivors/{survivor_id}/supports"),
    ],
)
def test_組織がない本人は本人向けのAPIで403にならない(client, monkeypatch, method, path):
    orgless_sign_in(monkeypatch)
    # 記録は sv_0001（PATH_VALUES）。本人のIDに合わせる
    set_survivor(monkeypatch, orgless_row(survivor_id=SURVIVOR))
    try:
        res = call(client, method, path, headers=BEARER)
    except AssertionError as exc:  # no_bigquery が落とした＝立場の確認は通っている
        assert "BigQuery" in str(exc)
        return
    assert res.status_code != 403, f"{method} {path} を組織がない本人が使えません"


# --- 8. 本人が組織を選ぶ・変える（追加修正⑦） ---


def test_選んだ組織の専門職から見えてほかの組織の専門職からは見えない():
    row = survivor_row(org_id=ORG)
    assert survivors.can_view(row, role=PRO, user_id=PRO_UID, org_id=ORG) is True
    assert survivors.can_view(row, role=PRO, user_id=PRO_UID, org_id=OTHER_ORG) is False


def test_組織を変えると前の組織の専門職と担当から見えなくなる(client, monkeypatch):
    # 記録のいまの版は OTHER_ORG（本人が ORG から変えた）。前の組織の担当は外れて、
    # int_survivor_list の assignee_user_ids にも入らない（組織でつないで数えるため）
    changed = survivor_row(org_id=OTHER_ORG, version=2, assignee_user_ids=[])

    sign_in(monkeypatch, uid=PRO_UID, role=PRO, org_id=ORG)
    set_survivor(monkeypatch, changed)
    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404

    sign_in(monkeypatch, uid=VOL_UID, role=VOL, org_id=ORG)
    set_survivor(monkeypatch, changed)
    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 404

    # 担当の行がもし残っていても、組織が違えば見えない（二重の守り）
    still_listed = survivor_row(org_id=OTHER_ORG, version=2, assignee_user_ids=[VOL_UID])
    assert survivors.can_view(still_listed, role=VOL, user_id=VOL_UID, org_id=ORG) is False

    sign_in(monkeypatch, uid="uid-pro-new", role=PRO, org_id=OTHER_ORG)
    set_survivor(monkeypatch, changed)
    assert client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).status_code == 200


def test_組織を変えたあとも健康の情報は新しい組織の専門職に見える(client, monkeypatch):
    # 健康の情報は被災者のIDで読む（前の組織のときに書いた版も記録の続き。追加修正⑦）
    sign_in(monkeypatch, uid="uid-pro-new", role=PRO, org_id=OTHER_ORG)
    set_survivor(monkeypatch, survivor_row(org_id=OTHER_ORG, version=2))
    captured: dict = {}

    def fetch_latest(**kwargs):
        captured.update(kwargs)
        return {"version": 3, "source": "self", "chronic_diseases": ["高血圧"]}

    monkeypatch.setattr(health, "fetch_latest", fetch_latest)
    body = client.get(f"/api/survivors/{SURVIVOR}", headers=BEARER).json()
    assert body["survivor"]["health"]["fields"]["chronic_diseases"] == ["高血圧"]
    assert captured["survivor_id"] == SURVIVOR


def test_健康の情報は被災者のIDだけで読む(monkeypatch):
    captured: dict = {}

    def fake_query_one(sql, params=None):
        captured.update(sql=sql, params=params)
        return None

    monkeypatch.setattr(health.bq, "query_one", fake_query_one)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    get_settings.cache_clear()
    health.fetch_latest(survivor_id=SURVIVOR, org_id=ORG)
    assert captured["params"] == {"survivor_id": SURVIVOR}
    assert "org_id" not in captured["sql"]
