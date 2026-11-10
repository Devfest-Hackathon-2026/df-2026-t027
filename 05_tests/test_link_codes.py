"""確認コードの発行・照合・誤りの制限のテスト（要件定義書F1・10-3、開発手順書 Step 5-2）。

Google Cloud には接続しない。BigQuery の読み書きは差し替えて、次のことだけを確かめる。

- 発行したコードは**応答の中だけ**にあり、保存する行には平文が入らない。
- 照合は「有効なコードを順に試す」形になっている（本人は被災者IDを知らないため）。
  正しいコードでも、**別の被災者のソルトでは当たらない**。
- 1回使ったら無効（`is_valid` が false の行は照合の対象にしない）。
- 何度も間違えたら一定時間入力できない。回数は**入力した利用者ごと**に数える
  （間違ったコードはソルトが分からず、コードごとには数えられないため）。
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend.config import get_settings
from backend.services import codes, link_codes

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
SURVIVOR = "sv_0001"
PRO_UID = "uid-professional-1"
SELF_UID = "uid-survivor-1"


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def inserted(monkeypatch):
    """raw への追記を受け取る（表の名前と行の組で残す）。"""
    rows: list[tuple[str, dict]] = []

    def fake_insert(table, row, **kwargs):
        rows.append((table, dict(row)))
        return 1

    monkeypatch.setattr(link_codes.bq, "insert", fake_insert)
    return rows


def set_valid_codes(monkeypatch, rows: list[dict]) -> None:
    """stg_link_codes の「有効なコード」の読みを差し替える。"""
    monkeypatch.setattr(link_codes, "fetch_valid_codes", lambda: rows)


def set_failures(monkeypatch, count: int) -> None:
    monkeypatch.setattr(link_codes, "count_recent_failures", lambda user_id: count)


# --- 発行 ---


def test_発行したコードは平文で返る(inserted):
    issued = link_codes.issue_code(
        survivor_id=SURVIVOR, org_id=ORG, created_by=PRO_UID, created_role="professional"
    )
    assert issued.code
    assert len(issued.code) == codes.CODE_LENGTH


def test_保存する行にコードの平文が入らない(inserted):
    issued = link_codes.issue_code(
        survivor_id=SURVIVOR, org_id=ORG, created_by=PRO_UID, created_role="professional"
    )
    table, row = inserted[0]
    assert table == "raw_link_codes"
    assert all(issued.code not in str(value) for value in row.values())


def test_発行した行は被災者IDのソルトで照合できる(inserted):
    issued = link_codes.issue_code(
        survivor_id=SURVIVOR, org_id=ORG, created_by=PRO_UID, created_role="professional"
    )
    _, row = inserted[0]
    assert codes.verify_code(issued.code, salt=SURVIVOR, code_hash=row["code_hash"])


def test_発行した行は別の被災者のソルトでは照合できない(inserted):
    issued = link_codes.issue_code(
        survivor_id=SURVIVOR, org_id=ORG, created_by=PRO_UID, created_role="professional"
    )
    _, row = inserted[0]
    assert not codes.verify_code(issued.code, salt="sv_9999", code_hash=row["code_hash"])


def test_発行の行はissuedで期限が7日後(inserted):
    link_codes.issue_code(
        survivor_id=SURVIVOR, org_id=ORG, created_by=PRO_UID, created_role="professional"
    )
    _, row = inserted[0]
    assert row["action"] == link_codes.ACTION_ISSUED
    assert row["survivor_id"] == SURVIVOR
    assert row["org_id"] == ORG
    delta = row["expires_at"] - row["created_at"]
    assert delta == dt.timedelta(days=link_codes.ISSUE_TTL_DAYS)


# --- 照合 ---


def test_合うコードで被災者の記録が返る(monkeypatch, inserted):
    code = "ABCD2345"
    set_valid_codes(
        monkeypatch,
        [
            {
                "code_hash": codes.hash_code(code, salt=SURVIVOR),
                "survivor_id": SURVIVOR,
                "org_id": ORG,
            }
        ],
    )
    set_failures(monkeypatch, 0)

    resolved = link_codes.resolve_code(code, user_id=SELF_UID)

    assert resolved is not None
    assert resolved.survivor_id == SURVIVOR
    assert resolved.org_id == ORG


def test_全角や小文字で入れても照合できる(monkeypatch, inserted):
    code = "ABCD2345"
    set_valid_codes(
        monkeypatch,
        [{"code_hash": codes.hash_code(code, salt=SURVIVOR), "survivor_id": SURVIVOR, "org_id": ORG}],
    )
    set_failures(monkeypatch, 0)

    resolved = link_codes.resolve_code("abcd-2345", user_id=SELF_UID)

    assert resolved is not None


def test_1文字違いのコードは合わない(monkeypatch, inserted):
    set_valid_codes(
        monkeypatch,
        [
            {
                "code_hash": codes.hash_code("ABCD2345", salt=SURVIVOR),
                "survivor_id": SURVIVOR,
                "org_id": ORG,
            }
        ],
    )
    set_failures(monkeypatch, 0)

    assert link_codes.resolve_code("ABCD2346", user_id=SELF_UID) is None


def test_合わなかったら誤りを1行記録する(monkeypatch, inserted):
    set_valid_codes(monkeypatch, [])
    set_failures(monkeypatch, 0)

    link_codes.resolve_code("ABCD2345", user_id=SELF_UID)

    assert [table for table, _ in inserted] == ["raw_link_codes"]
    _, row = inserted[0]
    assert row["action"] == link_codes.ACTION_FAILED
    assert row["created_by"] == SELF_UID


def test_誤りの行にコードの平文が入らない(monkeypatch, inserted):
    set_valid_codes(monkeypatch, [])
    set_failures(monkeypatch, 0)

    link_codes.resolve_code("ABCD2345", user_id=SELF_UID)

    _, row = inserted[0]
    assert all("ABCD2345" not in str(value) for value in row.values())


def test_誤りの行は被災者IDと組織IDをunknownにする(monkeypatch, inserted):
    # raw_link_codes の org_id・survivor_id は NOT NULL。間違ったコードはどの記録の
    # ものか分からないため、印として unknown を入れる（表を作り直さずに済ませる）。
    set_valid_codes(monkeypatch, [])
    set_failures(monkeypatch, 0)

    link_codes.resolve_code("ABCD2345", user_id=SELF_UID)

    _, row = inserted[0]
    assert row["org_id"] == link_codes.UNKNOWN
    assert row["survivor_id"] == link_codes.UNKNOWN


def test_形が正しくないコードは誤りに数えない(monkeypatch, inserted):
    # 短すぎる・使えない文字が入っているものは、どのコードにも当たりえないため、
    # 入力の誤りとして数えない（打ち間違いで入力できなくなるのを防ぐ）。
    set_valid_codes(monkeypatch, [])
    set_failures(monkeypatch, 0)

    assert link_codes.resolve_code("あ", user_id=SELF_UID) is None
    assert inserted == []


def test_空のコードは照合しない(monkeypatch, inserted):
    set_valid_codes(monkeypatch, [])
    set_failures(monkeypatch, 0)

    assert link_codes.resolve_code("", user_id=SELF_UID) is None
    assert inserted == []


def test_誤りが上限に達していたら照合そのものをしない(monkeypatch, inserted):
    def must_not_read():
        raise AssertionError("上限に達したら、コードの照合に進んではいけません")

    monkeypatch.setattr(link_codes, "fetch_valid_codes", must_not_read)
    set_failures(monkeypatch, link_codes.MAX_FAILURES_PER_HOUR)

    with pytest.raises(link_codes.TooManyAttempts):
        link_codes.resolve_code("ABCD2345", user_id=SELF_UID)


def test_上限の1回前なら照合できる(monkeypatch, inserted):
    code = "ABCD2345"
    set_valid_codes(
        monkeypatch,
        [{"code_hash": codes.hash_code(code, salt=SURVIVOR), "survivor_id": SURVIVOR, "org_id": ORG}],
    )
    set_failures(monkeypatch, link_codes.MAX_FAILURES_PER_HOUR - 1)

    assert link_codes.resolve_code(code, user_id=SELF_UID) is not None


def test_使われたコードは照合の対象にならない(monkeypatch, inserted):
    # 1回使ったら無効（要件定義書10-3）。無効の判定は stg_link_codes の is_valid で行い、
    # fetch_valid_codes が有効な行だけを返す。
    set_valid_codes(monkeypatch, [])
    set_failures(monkeypatch, 0)

    assert link_codes.resolve_code("ABCD2345", user_id=SELF_UID) is None


def test_壊れたハッシュが混ざっていても例外にしない(monkeypatch, inserted):
    code = "ABCD2345"
    set_valid_codes(
        monkeypatch,
        [
            {"code_hash": None, "survivor_id": "sv_bad", "org_id": ORG},
            {"code_hash": "平文が入っている", "survivor_id": "sv_bad2", "org_id": ORG},
            {"code_hash": codes.hash_code(code, salt=SURVIVOR), "survivor_id": SURVIVOR, "org_id": ORG},
        ],
    )
    set_failures(monkeypatch, 0)

    resolved = link_codes.resolve_code(code, user_id=SELF_UID)

    assert resolved is not None
    assert resolved.survivor_id == SURVIVOR


def test_被災者IDが無い行は照合の対象にしない(monkeypatch, inserted):
    # survivor_id が空の行はソルトが決まらない。例外にせず、照合の対象から外す。
    code = "ABCD2345"
    set_valid_codes(
        monkeypatch,
        [{"code_hash": codes.hash_code(code, salt=SURVIVOR), "survivor_id": None, "org_id": ORG}],
    )
    set_failures(monkeypatch, 0)

    assert link_codes.resolve_code(code, user_id=SELF_UID) is None


# --- 使用の記録 ---


def test_使ったことをusedで記録する(monkeypatch, inserted):
    resolved = link_codes.ResolvedCode(code_hash="sha256$abc", survivor_id=SURVIVOR, org_id=ORG)

    link_codes.mark_used(resolved, used_by=SELF_UID)

    table, row = inserted[0]
    assert table == "raw_link_codes"
    assert row["action"] == link_codes.ACTION_USED
    assert row["code_hash"] == "sha256$abc"
    assert row["survivor_id"] == SURVIVOR
    assert row["created_by"] == SELF_UID


# --- 読みのSQL（文字列をつないで作らないこと） ---


def test_有効なコードの読みはパラメータを使わずis_validで絞る(monkeypatch):
    captured: dict = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(link_codes.bq, "query", fake_query)

    link_codes.fetch_valid_codes()

    assert "stg_link_codes" in captured["sql"]
    assert "is_valid" in captured["sql"]


def test_誤りの数はパラメータ付きで利用者ごとに数える(monkeypatch):
    captured: dict = {}

    def fake_query_one(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return {"failures": 3}

    monkeypatch.setattr(link_codes.bq, "query_one", fake_query_one)

    assert link_codes.count_recent_failures(SELF_UID) == 3
    assert captured["params"] == {"user_id": SELF_UID}
    assert SELF_UID not in captured["sql"]
    assert "raw_link_codes" in captured["sql"]
