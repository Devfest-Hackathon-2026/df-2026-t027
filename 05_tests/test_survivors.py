"""被災者の記録と担当のテスト（要件定義書F1・10-3、開発手順書 Step 5）。

Google Cloud には接続しない。BigQuery の読み書きを差し替えて、次のことだけを確かめる。

- 版を足す形になっている（上書きしない。居場所・段階を変えると版が増える）。
- 立場と担当で見える範囲が分かれる（本人＝自分だけ／専門職＝同じ組織／一般＝担当だけ）。
- 本人に返すデータに、悪化の兆しの度合いと訪問の本文が入らない（10-3）。
- 担当は、本人が認めるまで有効にならない。外すと有効でなくなる。
- 氏名と年齢が近い記録を見つけられる。
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend.config import get_settings
from backend.services import assignments, survivors, users

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
OTHER_ORG = "org_suzu"
SURVIVOR = "sv_0001"
PRO_UID = "uid-pro-1"
VOL_UID = "uid-vol-1"
SELF_UID = "uid-self-1"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def inserted(monkeypatch):
    rows: list[tuple[str, dict]] = []

    def fake_insert(table, row, **kwargs):
        rows.append((table, dict(row)))
        return 1

    monkeypatch.setattr(survivors.bq, "insert", fake_insert)
    monkeypatch.setattr(assignments.bq, "insert", fake_insert)
    return rows


def survivor_row(**overrides) -> dict:
    """int_survivor_list の1行（F2以降が空の状態）。"""
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
        "self_user_id": None,
        "registered_via": "professional",
        "registered_at": NOW,
        "updated_at": NOW,
        "risk_level": "high",
        "risk_next_action": "今日のうちに訪問する",
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
        "assignee_user_ids": [],
        "assignee_count": 0,
        "never_visited": True,
    }
    row.update(overrides)
    return row


# --- 記録のID ---


def test_専門職が作る記録のIDは毎回違う():
    assert survivors.new_survivor_id() != survivors.new_survivor_id()


def test_本人が作る記録のIDは利用者ごとに決まる():
    # Step 4 からの約束（やり直しても同じ記録になる）。壊していないことを確かめる。
    assert survivors.self_survivor_id(SELF_UID) == survivors.self_survivor_id(SELF_UID)


# --- 版を足す ---


def test_居場所を変えると版が1つ増える(inserted):
    current = survivor_row()

    survivors.change_location(
        current=current,
        stage="temporary",
        location_type="temporary_built",
        location_detail="仮設第1団地",
        created_by=PRO_UID,
        created_role=users.ROLE_PROFESSIONAL,
    )

    table, row = inserted[0]
    assert table == "raw_survivors"
    assert row["version"] == 2
    assert row["stage"] == "temporary"
    assert row["location_type"] == "temporary_built"
    assert row["location_detail"] == "仮設第1団地"


def test_居場所を変えても基本の情報は引き継ぐ(inserted):
    # 上書きしないので、変えない項目は前の版の値をそのまま持つ（履歴として読めるようにする）
    survivors.change_location(
        current=survivor_row(),
        stage="evacuation",
        location_type="welfare_shelter",
        location_detail=None,
        created_by=PRO_UID,
        created_role=users.ROLE_PROFESSIONAL,
    )

    _, row = inserted[0]
    assert row["full_name"] == "佐藤 太郎"
    assert row["age"] == 78
    assert row["household_size"] == 2
    assert row["contact"] == "090-0000-0000"
    assert row["survivor_id"] == SURVIVOR
    assert row["org_id"] == ORG


def test_居場所の種類が正しくなければ断る(inserted):
    with pytest.raises(survivors.SurvivorValidationError):
        survivors.change_location(
            current=survivor_row(),
            stage="evacuation",
            location_type="バス",
            location_detail=None,
            created_by=PRO_UID,
            created_role=users.ROLE_PROFESSIONAL,
        )
    assert inserted == []


def test_基本の情報を変えると版が1つ増える(inserted):
    # プロフィールの画面（要望4）。居場所・段階はここでは変えない
    survivors.change_basic(
        current=survivor_row(),
        full_name="佐藤 花子",
        age=80,
        household_size=3,
        contact="090-1111-1111",
        created_by=SELF_UID,
        created_role=users.ROLE_SURVIVOR,
    )

    table, row = inserted[0]
    assert table == "raw_survivors"
    assert row["version"] == 2
    assert row["full_name"] == "佐藤 花子"
    assert row["age"] == 80
    assert row["household_size"] == 3
    assert row["contact"] == "090-1111-1111"
    # 居場所・段階は前の版のまま引き継ぐ
    assert row["stage"] == "evacuation"
    assert row["location_type"] == "shelter"


def test_基本の情報の氏名が空だと断る(inserted):
    with pytest.raises(survivors.SurvivorValidationError):
        survivors.change_basic(
            current=survivor_row(),
            full_name="",
            age=80,
            household_size=3,
            contact=None,
            created_by=SELF_UID,
            created_role=users.ROLE_SURVIVOR,
        )
    assert inserted == []


def test_住所を変えても市区町村は引き継ぐ(inserted):
    # change_basic は住所だけを変える。住んでいる市区町村はここでは変えない（要望8）。
    survivors.change_basic(
        current=survivor_row(municipality_code="172049", address="○○町1-1"),
        full_name="佐藤 太郎",
        age=78,
        household_size=2,
        contact=None,
        address="△△町2-2",
        created_by=SELF_UID,
        created_role=users.ROLE_SURVIVOR,
    )
    _, row = inserted[0]
    assert row["address"] == "△△町2-2"
    assert row["municipality_code"] == "172049"


# --- 住んでいる市区町村と住所（要望8） ---


def test_市区町村の団体コードはbuild_survivor_rowでは任意(inserted):
    # build_survivor_row 自体は任意にしてある（専門職の登録では組織が決まっているため不要）。
    # 本人の登録で必須にするのは呼ぶ側（routers/account.py の _resolve_organization）
    row = survivors.build_survivor_row(
        survivor_id="sv_new",
        org_id=ORG,
        created_by=PRO_UID,
        created_role=users.ROLE_PROFESSIONAL,
        full_name="田中 花子",
        age=70,
        household_size=1,
        stage="evacuation",
        location_type="home",
    )
    assert row["municipality_code"] is None


def test_市区町村の団体コードの形が違えば断る(inserted):
    with pytest.raises(survivors.SurvivorValidationError):
        survivors.build_survivor_row(
            survivor_id="sv_new",
            org_id=ORG,
            created_by=SELF_UID,
            created_role=users.ROLE_SURVIVOR,
            full_name="田中 花子",
            age=70,
            household_size=1,
            stage="evacuation",
            location_type="home",
            municipality_code="abc",
        )


def test_住所は任意で長さの上限がある(inserted):
    row = survivors.build_survivor_row(
        survivor_id="sv_new",
        org_id=ORG,
        created_by=SELF_UID,
        created_role=users.ROLE_SURVIVOR,
        full_name="田中 花子",
        age=70,
        household_size=1,
        stage="evacuation",
        location_type="home",
        municipality_code="172049",
        address=None,
    )
    assert row["address"] is None

    with pytest.raises(survivors.SurvivorValidationError):
        survivors.build_survivor_row(
            survivor_id="sv_new",
            org_id=ORG,
            created_by=SELF_UID,
            created_role=users.ROLE_SURVIVOR,
            full_name="田中 花子",
            age=70,
            household_size=1,
            stage="evacuation",
            location_type="home",
            municipality_code="172049",
            address="あ" * (survivors.ADDRESS_MAX_LENGTH + 1),
        )


def test_本人をつなぐと版が増えて本人のIDが入る(inserted):
    survivors.link_self_user(current=survivor_row(), user_id=SELF_UID)

    _, row = inserted[0]
    assert row["version"] == 2
    assert row["self_user_id"] == SELF_UID
    assert row["created_by"] == SELF_UID
    assert row["created_role"] == users.ROLE_SURVIVOR
    # 居場所と段階は変えない（stg_location_history に余分な行を作らないため）
    assert row["stage"] == "evacuation"
    assert row["location_type"] == "shelter"


# --- 見える範囲（要件定義書10-3） ---


def test_本人は自分の記録だけ見える():
    row = survivor_row(self_user_id=SELF_UID)
    assert survivors.can_view(row, role=users.ROLE_SURVIVOR, user_id=SELF_UID, org_id=ORG)


def test_本人は他人の記録を見られない():
    row = survivor_row(self_user_id="uid-self-9")
    assert not survivors.can_view(row, role=users.ROLE_SURVIVOR, user_id=SELF_UID, org_id=ORG)


def test_専門職は同じ組織の記録を見られる():
    assert survivors.can_view(
        survivor_row(), role=users.ROLE_PROFESSIONAL, user_id=PRO_UID, org_id=ORG
    )


def test_専門職は別の組織の記録を見られない():
    assert not survivors.can_view(
        survivor_row(org_id=OTHER_ORG), role=users.ROLE_PROFESSIONAL, user_id=PRO_UID, org_id=ORG
    )


def test_一般の支援者は担当の記録だけ見える():
    row = survivor_row(assignee_user_ids=[VOL_UID])
    assert survivors.can_view(row, role=users.ROLE_VOLUNTEER, user_id=VOL_UID, org_id=ORG)


def test_一般の支援者は担当でない記録を見られない():
    assert not survivors.can_view(
        survivor_row(assignee_user_ids=[]), role=users.ROLE_VOLUNTEER, user_id=VOL_UID, org_id=ORG
    )


def test_一般の支援者は担当でも別の組織なら見られない():
    row = survivor_row(org_id=OTHER_ORG, assignee_user_ids=[VOL_UID])
    assert not survivors.can_view(row, role=users.ROLE_VOLUNTEER, user_id=VOL_UID, org_id=ORG)


def test_立場が分からなければ見られない():
    assert not survivors.can_view(survivor_row(), role=None, user_id=PRO_UID, org_id=ORG)


# --- 返すデータの範囲 ---


def test_支援者向けには悪化の兆しが入る():
    payload = survivors.detail_payload(
        survivor_row(), role=users.ROLE_PROFESSIONAL, user_id=PRO_UID
    )
    assert payload["risk"]["level"] == "high"


def test_本人向けには悪化の兆しが入らない():
    # 要件定義書10-3：本人向けのデータからは、悪化の兆しの度合いを取り除く
    payload = survivors.detail_payload(
        survivor_row(self_user_id=SELF_UID), role=users.ROLE_SURVIVOR, user_id=SELF_UID
    )
    assert "risk" not in payload
    assert "high" not in str(payload)


def test_本人向けには訪問の本文の枠を作らない():
    payload = survivors.detail_payload(
        survivor_row(self_user_id=SELF_UID), role=users.ROLE_SURVIVOR, user_id=SELF_UID
    )
    assert "visits" not in payload


def test_段階と居場所は画面に出す名前も返す():
    payload = survivors.detail_payload(
        survivor_row(), role=users.ROLE_PROFESSIONAL, user_id=PRO_UID
    )
    assert payload["stage_label"] == "③避難生活"
    assert payload["location_type_label"] == "避難所"


def test_一覧の1行にも悪化の兆しの度合いが入る():
    row = survivors.list_item(survivor_row(), role=users.ROLE_PROFESSIONAL)
    assert row["risk_level"] == "high"


def test_一覧の1行には連絡先を入れない():
    # 一覧は人数分を一度に返す。連絡先は詳細だけで足りる（外に出す量を減らす）
    row = survivors.list_item(survivor_row(), role=users.ROLE_PROFESSIONAL)
    assert "contact" not in row


# --- 氏名と年齢が近い記録 ---


def test_空白の違いは同じ氏名として扱う():
    assert survivors.normalize_name("佐藤 太郎") == survivors.normalize_name("佐藤太郎")


def test_全角と半角の違いは同じ氏名として扱う():
    assert survivors.normalize_name("ｻﾄｳ ﾀﾛｳ") == survivors.normalize_name("サトウタロウ")


def test_氏名が同じで年齢が近ければ候補に出す(monkeypatch):
    monkeypatch.setattr(
        survivors.bq,
        "query",
        lambda sql, params=None: [
            {"survivor_id": SURVIVOR, "full_name": "佐藤太郎", "age": 79, "stage": "evacuation"}
        ],
    )

    found = survivors.find_similar(org_id=ORG, full_name="佐藤 太郎", age=78)

    assert [row["survivor_id"] for row in found] == [SURVIVOR]


def test_氏名が違えば候補に出さない(monkeypatch):
    monkeypatch.setattr(
        survivors.bq,
        "query",
        lambda sql, params=None: [
            {"survivor_id": SURVIVOR, "full_name": "鈴木花子", "age": 78, "stage": "evacuation"}
        ],
    )

    assert survivors.find_similar(org_id=ORG, full_name="佐藤太郎", age=78) == []


def test_年齢が離れていれば候補に出さない(monkeypatch):
    captured: dict = {}

    def fake_query(sql, params=None):
        captured["params"] = params
        return []

    monkeypatch.setattr(survivors.bq, "query", fake_query)

    survivors.find_similar(org_id=ORG, full_name="佐藤太郎", age=78)

    # 年齢の幅は SQL で絞る（読む行を減らす）
    assert captured["params"]["age_min"] == 78 - survivors.SIMILAR_AGE_DIFF
    assert captured["params"]["age_max"] == 78 + survivors.SIMILAR_AGE_DIFF
    assert captured["params"]["org_id"] == ORG


def test_年齢が無いときは候補を探さない(monkeypatch):
    def must_not_read(*args, **kwargs):
        raise AssertionError("年齢が無ければ読みに行かない")

    monkeypatch.setattr(survivors.bq, "query", must_not_read)

    assert survivors.find_similar(org_id=ORG, full_name="佐藤太郎", age=None) == []


# --- 一覧の読み ---


def test_一覧は組織で絞る(monkeypatch):
    captured: dict = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(survivors.bq, "query", fake_query)

    survivors.list_survivors(org_id=ORG)

    assert captured["params"]["org_id"] == ORG
    assert "int_survivor_list" in captured["sql"]
    assert ORG not in captured["sql"]  # 値は必ずパラメータで渡す（10-3）


def test_一覧は段階と居場所で絞れる(monkeypatch):
    captured: dict = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(survivors.bq, "query", fake_query)

    survivors.list_survivors(org_id=ORG, stage="temporary", location_type="temporary_built")

    assert captured["params"]["stage"] == "temporary"
    assert captured["params"]["location_type"] == "temporary_built"


def test_一覧は担当で絞れる(monkeypatch):
    captured: dict = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(survivors.bq, "query", fake_query)

    survivors.list_survivors(org_id=ORG, assignee_user_id=VOL_UID)

    assert captured["params"]["assignee_user_id"] == VOL_UID
    assert "assignee_user_ids" in captured["sql"]


def test_一覧の並び順は悪化の兆しの高い人から(monkeypatch):
    captured: dict = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        return []

    monkeypatch.setattr(survivors.bq, "query", fake_query)

    survivors.list_survivors(org_id=ORG)

    # 要件定義書7-3-3 の `int_survivor_list` の並び（F4 の訪ねる順番）
    assert "ORDER BY risk_rank DESC" in captured["sql"]
    assert "open_urgency_rank DESC" in captured["sql"]
    assert "days_since_last_visit_sort DESC" in captured["sql"]
    # 同じ度合いの人の順が読むたびに変わらないように、最後に登録の新しい順を足す
    assert captured["sql"].rstrip().endswith("registered_at DESC")


# --- 担当（F1） ---


def test_割り当てただけでは有効にならない(inserted):
    created = assignments.create(
        survivor_id=SURVIVOR,
        org_id=ORG,
        supporter_user_id=VOL_UID,
        created_by=PRO_UID,
        created_role=users.ROLE_PROFESSIONAL,
        verbal_consent=False,
    )

    assert created.status == assignments.STATUS_PENDING
    assert [row["action"] for _, row in inserted] == [assignments.ACTION_ASSIGNED]


def test_口頭の同意を記録すればすぐ有効になる(inserted):
    # 本人がアカウントを持っていないとき（要件定義書F1）
    created = assignments.create(
        survivor_id=SURVIVOR,
        org_id=ORG,
        supporter_user_id=VOL_UID,
        created_by=PRO_UID,
        created_role=users.ROLE_PROFESSIONAL,
        verbal_consent=True,
    )

    assert created.status == assignments.STATUS_ACTIVE
    assert [row["action"] for _, row in inserted] == [
        assignments.ACTION_ASSIGNED,
        assignments.ACTION_VERBAL_CONSENT,
    ]


def test_担当になるのは一般の支援者だけ(inserted):
    with pytest.raises(assignments.AssignmentError):
        assignments.create(
            survivor_id=SURVIVOR,
            org_id=ORG,
            supporter_user_id="",
            created_by=PRO_UID,
            created_role=users.ROLE_PROFESSIONAL,
            verbal_consent=False,
        )
    assert inserted == []


def test_本人が認めるとapprovedを記録する(inserted):
    assignments.approve(
        assignment_id="as_1",
        survivor_id=SURVIVOR,
        org_id=ORG,
        supporter_user_id=VOL_UID,
        acted_by=SELF_UID,
        acted_role=users.ROLE_SURVIVOR,
    )

    _, row = inserted[0]
    assert row["action"] == assignments.ACTION_APPROVED
    assert row["assignment_id"] == "as_1"
    assert row["supporter_user_id"] == VOL_UID


def test_外すとremovedを記録する(inserted):
    assignments.remove(
        assignment_id="as_1",
        survivor_id=SURVIVOR,
        org_id=ORG,
        supporter_user_id=VOL_UID,
        acted_by=PRO_UID,
        acted_role=users.ROLE_PROFESSIONAL,
    )

    _, row = inserted[0]
    assert row["action"] == assignments.ACTION_REMOVED


def _events(*rows) -> list[dict]:
    return list(rows)


def event(action: str, *, minutes: int, assignment_id: str = "as_1", supporter=VOL_UID) -> dict:
    return {
        "assignment_id": assignment_id,
        "supporter_user_id": supporter,
        "action": action,
        "created_at": NOW + dt.timedelta(minutes=minutes),
        "created_by": PRO_UID,
        "created_role": users.ROLE_PROFESSIONAL,
    }


def test_承認前の担当は承認待ちとして返る(monkeypatch):
    monkeypatch.setattr(
        assignments.bq,
        "query",
        lambda sql, params=None: _events(event(assignments.ACTION_ASSIGNED, minutes=0)),
    )

    rows = assignments.list_for_survivor(survivor_id=SURVIVOR, org_id=ORG)

    assert [row["status"] for row in rows] == [assignments.STATUS_PENDING]


def test_承認後の担当は有効として返る(monkeypatch):
    monkeypatch.setattr(
        assignments.bq,
        "query",
        lambda sql, params=None: _events(
            event(assignments.ACTION_ASSIGNED, minutes=0),
            event(assignments.ACTION_APPROVED, minutes=5),
        ),
    )

    rows = assignments.list_for_survivor(survivor_id=SURVIVOR, org_id=ORG)

    assert rows[0]["status"] == assignments.STATUS_ACTIVE
    assert rows[0]["consent_type"] == assignments.ACTION_APPROVED


def test_外した担当は外れたとして返る(monkeypatch):
    monkeypatch.setattr(
        assignments.bq,
        "query",
        lambda sql, params=None: _events(
            event(assignments.ACTION_ASSIGNED, minutes=0),
            event(assignments.ACTION_APPROVED, minutes=5),
            event(assignments.ACTION_REMOVED, minutes=10),
        ),
    )

    rows = assignments.list_for_survivor(survivor_id=SURVIVOR, org_id=ORG)

    assert rows[0]["status"] == assignments.STATUS_REMOVED


def test_外したあとに割り当て直せば有効になる(monkeypatch):
    # stg_active_assignments と同じ判定（removed より後に承認があれば有効）
    monkeypatch.setattr(
        assignments.bq,
        "query",
        lambda sql, params=None: _events(
            event(assignments.ACTION_ASSIGNED, minutes=0),
            event(assignments.ACTION_APPROVED, minutes=5),
            event(assignments.ACTION_REMOVED, minutes=10),
            event(assignments.ACTION_APPROVED, minutes=15),
        ),
    )

    rows = assignments.list_for_survivor(survivor_id=SURVIVOR, org_id=ORG)

    assert rows[0]["status"] == assignments.STATUS_ACTIVE


def test_担当の読みは被災者と組織で絞る(monkeypatch):
    captured: dict = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(assignments.bq, "query", fake_query)

    assignments.list_for_survivor(survivor_id=SURVIVOR, org_id=ORG)

    assert captured["params"] == {"survivor_id": SURVIVOR, "org_id": ORG}
    assert SURVIVOR not in captured["sql"]


# --- 組織がない本人（要望8・11章の要確認E-1） ---


def test_組織がない本人も自分の記録は見られる() -> None:
    row = survivor_row(org_id="", self_user_id=SELF_UID)
    assert survivors.can_view(row, role=users.ROLE_SURVIVOR, user_id=SELF_UID, org_id="")
    assert not survivors.can_view(row, role=users.ROLE_SURVIVOR, user_id="other", org_id="")


def test_組織がない支援者は組織のない記録も見られない() -> None:
    row = survivor_row(org_id="", self_user_id=SELF_UID)
    assert not survivors.can_view(row, role=users.ROLE_PROFESSIONAL, user_id="p", org_id="")
    assert not survivors.can_view(row, role=users.ROLE_VOLUNTEER, user_id="v", org_id="")


def test_本人の登録だけは組織なしで記録を作れる() -> None:
    common = dict(
        survivor_id="self_x",
        org_id="",
        created_by="u",
        created_role="survivor",
        full_name="田中",
        age=78,
        household_size=2,
        stage="evacuation",
        location_type="home",
    )
    row = survivors.build_survivor_row(**common, registered_via=survivors.VIA_SELF)
    assert row["org_id"] == ""
    with pytest.raises(survivors.SurvivorValidationError):
        survivors.build_survivor_row(**common, registered_via=survivors.VIA_PROFESSIONAL)


def test_組織につなぐと版が1つ増え組織が入る(monkeypatch) -> None:
    inserted: list = []
    monkeypatch.setattr(survivors.bq, "insert", lambda table, row, **kw: inserted.append((table, row)))
    current = survivor_row(org_id="", version=1, address="河井町1-1", municipality_code="172049")
    row = survivors.connect_organization(current=current, org_id="org_wajima", user_id=SELF_UID)
    assert row["org_id"] == "org_wajima"
    assert row["version"] == 2
    assert row["address"] == "河井町1-1"
    assert inserted[0][0] == "raw_survivors"


def test_組織がある記録はつなぎ直さない(monkeypatch) -> None:
    monkeypatch.setattr(survivors.bq, "insert", lambda *a, **k: pytest.fail("書いてはいけません"))
    with pytest.raises(survivors.SurvivorValidationError):
        survivors.connect_organization(
            current=survivor_row(org_id="org_wajima"), org_id="org_suzu", user_id=SELF_UID
        )
