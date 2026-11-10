"""組織と市区町村のテスト（F0・要望8。要件定義書7-3-2の stg_organizations）。

Google Cloud には接続しない。確かめるのは次の3つ。

- 市区町村の団体コードの形（6桁の数字）だけを確かめ、他の形は断る
- `fetch_by_municipality` が、その市区町村を受け持つ組織を返す（0件もありうる）
- 画面に返す一覧には、参加コードのハッシュを含めない（従来からの決まり）
"""

from __future__ import annotations

import pytest

from backend.config import get_settings
from backend.services import codes, municipalities, organizations


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    """.env が無い環境（git worktree など）でも、読みの SQL を組み立てられるようにする。"""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gdgoc-test-project")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_団体コードは6桁の数字() -> None:
    assert organizations.validate_municipality_code("172049") == "172049"


@pytest.mark.parametrize("value", ["", "17204", "1720499", "abcdef", None, "17204a"])
def test_団体コードの形が違えば断る(value) -> None:
    with pytest.raises(organizations.OrganizationValidationError):
        organizations.validate_municipality_code(value)


def test_団体コードの前後の空白は取る() -> None:
    assert organizations.validate_municipality_code("  172049  ") == "172049"


def test_受け持つ組織を市区町村で探す(monkeypatch) -> None:
    captured: dict = {}

    def fake_query(sql, params=None):
        captured.update(sql=sql, params=params)
        return [{"org_id": "org_wajima", "org_name": "輪島市", "municipality_code": "172049"}]

    monkeypatch.setattr(organizations.bq, "query", fake_query)
    rows = organizations.fetch_by_municipality("172049")
    assert rows[0]["org_id"] == "org_wajima"
    assert captured["params"] == {
        "municipality_code": "172049",
        "municipality_name": "輪島市",
        "prefecture_name": "石川県",
    }
    assert "hash" not in captured["sql"].lower()


def test_市区町村が入っていない組織は都道府県と名前で探す(monkeypatch) -> None:
    # seed で市区町村を入れ直さなくても、「石川県・輪島市」の組織が見つかる
    captured: dict = {}

    def fake_query(sql, params=None):
        captured.update(sql=sql, params=params)
        return []

    monkeypatch.setattr(organizations.bq, "query", fake_query)
    organizations.fetch_by_municipality("172057")
    sql = captured["sql"]
    assert "IFNULL(municipality_code, '') = ''" in sql
    assert "TRIM(org_name) = @municipality_name" in sql
    assert "@prefecture_name" in sql
    assert captured["params"]["municipality_name"] == "珠洲市"
    assert captured["params"]["prefecture_name"] == "石川県"


def test_一覧に無い団体コードは名前では探さない(monkeypatch) -> None:
    captured: dict = {}

    def fake_query(sql, params=None):
        captured.update(sql=sql, params=params)
        return []

    monkeypatch.setattr(organizations.bq, "query", fake_query)
    organizations.fetch_by_municipality("999999")
    assert captured["params"] == {"municipality_code": "999999"}
    assert "org_name" not in captured["sql"].split("WHERE", 1)[1]


def test_市区町村の一覧から名前を引く() -> None:
    assert municipalities.lookup("172049") == {
        "code": "172049",
        "name": "輪島市",
        "prefecture": "石川県",
    }
    assert municipalities.lookup(" 131016 ")["name"] == "千代田区"
    assert municipalities.lookup("999999") is None
    assert municipalities.lookup("") is None
    assert municipalities.lookup(None) is None


def test_団体コードが空なら読まない(monkeypatch) -> None:
    monkeypatch.setattr(
        organizations.bq, "query", lambda *a, **k: pytest.fail("読んではいけません")
    )
    assert organizations.fetch_by_municipality("") == []


def test_公開の列に参加コードのハッシュを含めない() -> None:
    assert "pro_join_code_hash" not in organizations._PUBLIC_COLUMNS
    assert "vol_join_code_hash" not in organizations._PUBLIC_COLUMNS
    assert "municipality_code" in organizations._PUBLIC_COLUMNS


# --- 組織を作る（要望2） ---


def test_組織の名前が空だと断る() -> None:
    with pytest.raises(organizations.OrganizationValidationError):
        organizations.validate_org_name("  ")


def test_組織の名前が長すぎると断る() -> None:
    with pytest.raises(organizations.OrganizationValidationError):
        organizations.validate_org_name("あ" * (organizations.ORG_NAME_MAX_LENGTH + 1))


def test_同じ市区町村に同じ名前があれば重複とみなす(monkeypatch) -> None:
    monkeypatch.setattr(
        organizations,
        "fetch_by_municipality",
        lambda code: [{"org_id": "org_a", "org_name": "きずな会", "municipality_code": code}],
    )
    assert organizations.find_duplicate(municipality_code="172049", org_name="きずな会") is not None
    assert organizations.find_duplicate(municipality_code="172049", org_name="別の会") is None


def test_組織を作ると参加コードが発行される(monkeypatch) -> None:
    monkeypatch.setattr(organizations, "fetch_by_municipality", lambda code: [])
    inserted = []
    monkeypatch.setattr(
        organizations.bq, "insert", lambda table, row, **kw: inserted.append((table, dict(row)))
    )

    created = organizations.create_organization(
        org_name="きずな会",
        prefecture="石川県",
        municipality_code="172049",
        created_by="uid-vol-1",
        created_role="volunteer",
    )

    table, row = inserted[0]
    assert table == "raw_organizations"
    assert row["org_name"] == "きずな会"
    assert row["municipality_code"] == "172049"
    # 参加コードは、この戻り値でしか返らない。保存するのはハッシュだけ
    assert created["pro_join_code"] != created["vol_join_code"]
    assert "pro_join_code_hash" not in created
    assert row["pro_join_code_hash"] != row["vol_join_code_hash"]
    # 作ったコードで、その場で照合できる（照合ロジックとハッシュの付け方がずれていないこと）
    assert codes.verify_code(
        created["pro_join_code"], salt=created["org_id"], code_hash=row["pro_join_code_hash"]
    )


def test_同じ市区町村の同じ名前では作れない(monkeypatch) -> None:
    monkeypatch.setattr(
        organizations,
        "fetch_by_municipality",
        lambda code: [{"org_id": "org_a", "org_name": "きずな会", "municipality_code": code}],
    )
    monkeypatch.setattr(
        organizations.bq, "insert", lambda *a, **k: pytest.fail("作ってはいけません")
    )
    with pytest.raises(organizations.OrganizationValidationError):
        organizations.create_organization(
            org_name="きずな会",
            prefecture="石川県",
            municipality_code="172049",
            created_by="uid-vol-1",
            created_role="volunteer",
        )


def test_団体コードの形が違う組織は作れない(monkeypatch) -> None:
    monkeypatch.setattr(
        organizations.bq, "insert", lambda *a, **k: pytest.fail("作ってはいけません")
    )
    with pytest.raises(organizations.OrganizationValidationError):
        organizations.create_organization(
            org_name="きずな会",
            prefecture="石川県",
            municipality_code="abc",
            created_by="uid-vol-1",
            created_role="volunteer",
        )
