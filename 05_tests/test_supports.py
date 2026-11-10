"""F6（受けられる支援と申請の状況）の値・行づくり・画面に返す形のテスト（Step 10）。

Google Cloud にも Gemini にも接続しない。確かめるのは次の6つ。

- 選択肢の外の値（住宅の被害の程度・再建の意向・申請の状況）を**受け取らない**
- 支援の記録のIDは「被災者と制度の組み合わせ」で**必ず同じ値**になる（記録が二重にならない）
- 示したときは**未申請から始まり**、状況を変えても**前の行を消さない**（追記だけ）
- 期限は**読み取れない値・遠すぎる値を落とす**（AIが作り出した日付を保存しない）
- 期限が近い未申請だけ知らせる（30日・0日・過ぎたときの文言）
- 「目安であり、受けられるかは自治体が決める」は**必ず応答に入る**
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend.config import get_settings
from backend.services import supports

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
OTHER_ORG = "org_suzu"
SURVIVOR = "sv_0001"
PROGRAM = "org_wajima__seikatsu_saiken_kiso"
TODAY = dt.date(2026, 11, 8)


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def program_row(**overrides) -> dict:
    row = {
        "program_id": PROGRAM,
        "org_id": ORG,
        "version": 1,
        "program_name": "被災者生活再建支援金（基礎支援金）",
        "conditions": "住宅が全壊、または解体した世帯",
        "benefit": "100万円（複数世帯）",
        "apply_to": "輪島市 福祉課",
        "deadline_rule": "災害発生日から13月以内",
        "documents": "罹災証明書、住民票",
        "source_url": "https://example.lg.jp/saiken",
    }
    row.update(overrides)
    return row


def support_row(**overrides) -> dict:
    row = {
        "support_id": supports.support_id_for(
            org_id=ORG, survivor_id=SURVIVOR, program_id=PROGRAM
        ),
        "survivor_id": SURVIVOR,
        "org_id": ORG,
        "program_id": PROGRAM,
        "suggested_reason": "住宅が全壊のため対象になりそうです",
        "suggested_at": dt.datetime(2026, 11, 8, tzinfo=dt.timezone.utc),
        "status": supports.STATUS_NOT_APPLIED,
        "deadline": dt.date(2026, 12, 1),
        "days_to_deadline": 23,
        "housing_damage": "total",
        "rebuild_intent": "rebuild",
        "updated_at": dt.datetime(2026, 11, 8, tzinfo=dt.timezone.utc),
        "updated_by": "uid-pro-1",
    }
    row.update(program_row())
    row.update(overrides)
    return row


# --- 選択肢の検査 ---


def test_住宅の被害の程度は選択肢の中だけ受け取る():
    assert supports.validate_housing_damage("total") == "total"
    with pytest.raises(supports.SupportValidationError):
        supports.validate_housing_damage("ぜんかい")
    with pytest.raises(supports.SupportValidationError):
        supports.validate_housing_damage(None)


def test_再建の方法の意向は選択肢の中だけ受け取る():
    assert supports.validate_rebuild_intent("repair") == "repair"
    with pytest.raises(supports.SupportValidationError):
        supports.validate_rebuild_intent("建設")


def test_申請の状況は4つだけ受け取る():
    for value in ("not_applied", "applying", "received", "not_eligible"):
        assert supports.validate_status(value) == value
    with pytest.raises(supports.SupportValidationError):
        supports.validate_status("done")


def test_理由は空なら入れず長すぎれば切る():
    assert supports.clean_reason("  ") is None
    assert supports.clean_reason(None) is None
    long_reason = "あ" * (supports.REASON_MAX_LENGTH + 50)
    assert len(supports.clean_reason(long_reason)) == supports.REASON_MAX_LENGTH
    with pytest.raises(supports.SupportValidationError):
        supports.clean_reason(123)


# --- 期限（AIが作り出した日付を保存しない） ---


def test_期限は日付にできるときだけ受け取る():
    assert supports.clean_deadline("2026-12-01", today=TODAY) == dt.date(2026, 12, 1)
    assert supports.clean_deadline(dt.date(2026, 12, 1), today=TODAY) == dt.date(2026, 12, 1)
    assert supports.clean_deadline(
        dt.datetime(2026, 12, 1, 9, tzinfo=dt.timezone.utc), today=TODAY
    ) == dt.date(2026, 12, 1)
    assert supports.clean_deadline("", today=TODAY) is None
    assert supports.clean_deadline(None, today=TODAY) is None


def test_読み取れない期限と遠すぎる期限は落とす():
    assert supports.clean_deadline("令和8年12月1日", today=TODAY) is None
    assert supports.clean_deadline("2026-13-45", today=TODAY) is None
    assert supports.clean_deadline(12345, today=TODAY) is None
    # 10年より先・1年より前は落とす（AIが桁を間違えた日付を保存しない）
    assert supports.clean_deadline("2099-01-01", today=TODAY) is None
    assert supports.clean_deadline("2020-01-01", today=TODAY) is None


# --- 支援の記録のID ---


def test_同じ被災者と制度の組み合わせなら記録のIDは同じ():
    first = supports.support_id_for(org_id=ORG, survivor_id=SURVIVOR, program_id=PROGRAM)
    second = supports.support_id_for(org_id=ORG, survivor_id=SURVIVOR, program_id=PROGRAM)
    assert first == second
    assert first.startswith("sp_")


def test_被災者や制度や組織が違えば記録のIDは変わる():
    base = supports.support_id_for(org_id=ORG, survivor_id=SURVIVOR, program_id=PROGRAM)
    assert base != supports.support_id_for(
        org_id=ORG, survivor_id="sv_0002", program_id=PROGRAM
    )
    assert base != supports.support_id_for(
        org_id=ORG, survivor_id=SURVIVOR, program_id="org_wajima__kasan"
    )
    assert base != supports.support_id_for(
        org_id=OTHER_ORG, survivor_id=SURVIVOR, program_id=PROGRAM
    )


def test_記録のIDを決められないときは断る():
    with pytest.raises(supports.SupportValidationError):
        supports.support_id_for(org_id=ORG, survivor_id="", program_id=PROGRAM)
    with pytest.raises(supports.SupportValidationError):
        supports.support_id_for(org_id=ORG, survivor_id=SURVIVOR, program_id="")


def test_全国共通の制度は組織に関係なく同じ記録のIDになる():
    # 組織がない本人が示した記録と、のちに組織につないでから示した記録が1本になる（追加修正②）
    without_org = build_suggested(org_id="", program_org_id="")
    with_org = build_suggested(org_id=ORG, program_org_id="")
    assert without_org["support_id"] == with_org["support_id"]
    assert without_org["org_id"] == ""
    assert with_org["org_id"] == ORG
    # 組織の制度は今までどおりのID（program_org_id を渡さなければ org_id で決める）
    assert build_suggested()["support_id"] == supports.support_id_for(
        org_id=ORG, survivor_id=SURVIVOR, program_id=PROGRAM
    )


# --- 行づくり（示したとき） ---


def build_suggested(**overrides) -> dict:
    kwargs = {
        "org_id": ORG,
        "survivor_id": SURVIVOR,
        "created_by": "uid-pro-1",
        "created_role": "professional",
        "program_id": PROGRAM,
        "reason": "住宅が全壊のため",
        "deadline": "2026-12-01",
        "housing_damage": "total",
        "rebuild_intent": "rebuild",
    }
    kwargs.update(overrides)
    return supports.build_suggested_row(**kwargs)


def test_示した支援は未申請から始まる():
    row = build_suggested()
    assert row["action"] == supports.ACTION_SUGGESTED
    assert row["status"] == supports.STATUS_NOT_APPLIED
    assert row["deadline"] == dt.date(2026, 12, 1)
    assert row["housing_damage"] == "total"
    assert row["rebuild_intent"] == "rebuild"
    assert row["support_id"] == supports.support_id_for(
        org_id=ORG, survivor_id=SURVIVOR, program_id=PROGRAM
    )
    assert row["created_role"] == "professional"
    assert row["created_at"].tzinfo is not None


def test_示した支援の行は毎回ちがうevent_idになる():
    assert build_suggested()["event_id"] != build_suggested()["event_id"]


def test_制度や選択肢がそろわない示し方は断る():
    with pytest.raises(supports.SupportValidationError):
        build_suggested(program_id="  ")
    with pytest.raises(supports.SupportValidationError):
        build_suggested(housing_damage="")
    with pytest.raises(supports.SupportValidationError):
        build_suggested(rebuild_intent="いえをなおす")
    with pytest.raises(supports.SupportValidationError):
        build_suggested(survivor_id="")


def test_読み取れない期限は入れずに保存する():
    row = build_suggested(deadline="きょねんの冬")
    assert row["deadline"] is None


# --- 行づくり（状況を変えたとき） ---


def test_状況の変更は追記で前の行を消さない():
    row = supports.build_status_row(
        current=support_row(),
        created_by="uid-self-1",
        created_role="survivor",
        status="applying",
    )
    assert row["action"] == supports.ACTION_STATUS_CHANGED
    assert row["status"] == "applying"
    # 期限を渡さなければ、いまの期限をそのまま引き継ぐ（None で消さない）
    assert row["deadline"] == dt.date(2026, 12, 1)
    # 理由は示したときだけの列（状況の変更では入れない）
    assert row["reason"] is None
    assert row["support_id"] == support_row()["support_id"]
    assert row["created_role"] == "survivor"


def test_状況の変更で期限を入れ直せる():
    row = supports.build_status_row(
        current=support_row(),
        created_by="uid-pro-1",
        created_role="professional",
        status="received",
        deadline="2027-01-31",
    )
    assert row["deadline"] == dt.date(2027, 1, 31)


def test_状況の変更は記録が無いときと選択肢の外のときに断る():
    with pytest.raises(supports.SupportValidationError):
        supports.build_status_row(
            current={}, created_by="u", created_role="professional", status="applying"
        )
    with pytest.raises(supports.SupportValidationError):
        supports.build_status_row(
            current=support_row(support_id=""),
            created_by="u",
            created_role="professional",
            status="applying",
        )
    with pytest.raises(supports.SupportValidationError):
        supports.build_status_row(
            current=support_row(),
            created_by="u",
            created_role="professional",
            status="うけとった",
        )


# --- 期限の知らせ ---


@pytest.mark.parametrize(
    "days,expected",
    [
        (31, False),
        (30, True),
        (1, True),
        (0, True),
        (-1, True),
        (None, False),
    ],
)
def test_期限が近い未申請だけ知らせる(days, expected):
    assert supports.is_due_soon(supports.STATUS_NOT_APPLIED, days) is expected


def test_申請中や受給済みには期限の知らせを出さない():
    assert supports.is_due_soon("applying", 3) is False
    assert supports.deadline_notice("received", 3) is None
    assert supports.deadline_notice("not_eligible", 0) is None


def test_期限の知らせの文言():
    assert supports.deadline_notice(supports.STATUS_NOT_APPLIED, 5) == "申請の期限まであと5日です"
    assert supports.deadline_notice(supports.STATUS_NOT_APPLIED, 0) == "申請の期限は今日です"
    assert (
        supports.deadline_notice(supports.STATUS_NOT_APPLIED, -3)
        == supports.DEADLINE_PASSED_NOTICE
    )
    assert supports.deadline_notice(supports.STATUS_NOT_APPLIED, 90) is None


def test_期限までの日数はビューの値を使い無ければ数える():
    assert supports.days_to_deadline(support_row(days_to_deadline=23)) == 23
    counted = supports.days_to_deadline(
        support_row(days_to_deadline=None, deadline=dt.date(2026, 11, 18)), today=TODAY
    )
    assert counted == 10
    assert (
        supports.days_to_deadline(support_row(days_to_deadline=None, deadline=None)) is None
    )


# --- 画面に返す形 ---


def test_支援1件の応答に制度の情報と次にすることが入る():
    item = supports.payload(support_row(), role="professional")
    assert item["program_name"] == "被災者生活再建支援金（基礎支援金）"
    assert item["apply_to"] == "輪島市 福祉課"
    assert item["documents"] == "罹災証明書、住民票"
    assert item["source_url"] == "https://example.lg.jp/saiken"
    assert item["status_label"] == "未申請"
    assert item["next_action"] == supports.NEXT_ACTIONS[supports.STATUS_NOT_APPLIED]
    assert item["housing_damage_label"] == "全壊"
    assert item["rebuild_intent_label"] == "建設・購入"
    assert item["due_soon"] is True
    assert item["deadline_notice"] == "申請の期限まであと23日です"


def test_目安であることは必ず応答に入る():
    assert supports.payload(support_row())["disclaimer"] == supports.DISCLAIMER
    assert supports.alerts([])["disclaimer"] == supports.DISCLAIMER


def test_誰が変えたかは専門職にだけ返す():
    assert "updated_by" not in supports.payload(support_row(), role="survivor")
    assert supports.payload(support_row(), role="professional")["updated_by"] == "uid-pro-1"


def test_制度1件の応答は登録された情報だけを返す():
    item = supports.program_payload(program_row())
    assert item["program_id"] == PROGRAM
    assert item["deadline_rule"] == "災害発生日から13月以内"
    assert "reason" not in item


# --- ホームと一覧の知らせ ---


def list_row(**overrides) -> dict:
    row = {
        "survivor_id": SURVIVOR,
        "full_name": "佐藤 太郎",
        "unapplied_count": 2,
        "nearest_unapplied_deadline": dt.date(2026, 11, 20),
        "days_to_nearest_deadline": 12,
    }
    row.update(overrides)
    return row


def test_期限の近い未申請がある人だけホームに出す():
    rows = [
        list_row(),
        list_row(survivor_id="sv_0002", unapplied_count=0, days_to_nearest_deadline=3),
        list_row(survivor_id="sv_0003", days_to_nearest_deadline=90),
        list_row(survivor_id="sv_0004", days_to_nearest_deadline=None),
    ]
    result = supports.alerts(rows)
    assert result["count"] == 1
    assert result["survivors"][0]["survivor_id"] == SURVIVOR
    assert result["survivors"][0]["notice"] == "申請の期限まであと12日です"


def test_ホームの知らせは期限の近い順に並ぶ():
    rows = [
        list_row(survivor_id="sv_a", days_to_nearest_deadline=20),
        list_row(survivor_id="sv_b", days_to_nearest_deadline=-2),
        list_row(survivor_id="sv_c", days_to_nearest_deadline=5),
    ]
    order = [item["survivor_id"] for item in supports.alerts(rows)["survivors"]]
    assert order == ["sv_b", "sv_c", "sv_a"]


def test_すべての絞り込みのために30日より先の未申請も返す():
    """使いやすさの改修の計画.md 11章。期日で絞るのは画面の中で行う。"""
    rows = [
        list_row(survivor_id="sv_far", days_to_nearest_deadline=90),
        list_row(survivor_id="sv_near", days_to_nearest_deadline=3),
        list_row(survivor_id="sv_none", days_to_nearest_deadline=None),
    ]
    result = supports.alerts(rows)
    assert result["count"] == 1
    assert result["all_count"] == 2
    assert [item["survivor_id"] for item in result["all_survivors"]] == ["sv_near", "sv_far"]
    assert result["soon_days"] == supports.DEADLINE_SOON_DAYS


# --- Gemini に渡す文章と読みのSQL ---


def test_AIに渡す制度の文章にIDが必ず入る():
    text = supports.summary_for_ai(program_row())
    assert PROGRAM in text
    assert "申請先：輪島市 福祉課" in text
    assert "記載なし" in supports.summary_for_ai(program_row(documents=None))


def test_支援の読みは被災者で絞る(monkeypatch):
    captured = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(supports.bq, "query", fake_query)
    supports.list_for_survivor(survivor_id=SURVIVOR, org_id=ORG)
    assert "s.survivor_id = @survivor_id" in captured["sql"]
    # 組織では絞らない（組織がないときに示した支援も、組織を変える前の支援も出す。追加修正②⑦）
    assert "s.org_id = @org_id" not in captured["sql"]
    assert "org_id" not in captured["params"]
    # 制度の情報は同じ読みでつなぐ（詳細の読みを増やさない）。全国共通の制度もつなぐ
    assert "stg_programs" in captured["sql"]
    assert "IFNULL(p.org_id, '') = ''" in captured["sql"]


def test_被災者が決まらないときは読まない(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("読んではいけません")

    monkeypatch.setattr(supports.bq, "query", fail)
    monkeypatch.setattr(supports.bq, "query_one", fail)
    assert supports.list_for_survivor(survivor_id="", org_id=ORG) == []
    assert supports.fetch_support(support_id="sp_x", survivor_id="", org_id=ORG) is None
    assert supports.fetch_support(support_id="", survivor_id=SURVIVOR, org_id=ORG) is None


def test_組織がない本人には全国共通の制度を出す(monkeypatch):
    captured = {}

    def fake_query(sql, params=None):
        captured.update(sql=sql, params=params)
        return [program_row(program_id="nationwide__kiso", org_id="")]

    monkeypatch.setattr(supports.bq, "query", fake_query)
    rows = supports.list_programs(org_id="")
    assert [row["program_id"] for row in rows] == ["nationwide__kiso"]
    # 組織のIDが空の行（全国共通）と、その組織の行を読む
    assert "IFNULL(org_id, '') = ''" in captured["sql"]
    assert captured["params"] == {"org_id": ""}
    assert supports.program_payload(rows[0])["nationwide"] is True


def test_組織に入っている本人には組織の制度と全国共通の制度を出す(monkeypatch):
    monkeypatch.setattr(
        supports.bq,
        "query",
        lambda sql, params=None: [
            program_row(program_id="org_wajima__kiso", program_name="基礎支援金"),
            program_row(program_id="nationwide__kiso", org_id="", program_name="基礎支援金"),
            program_row(program_id="nationwide__other", org_id="", program_name="全国の別の制度"),
        ],
    )
    rows = supports.list_programs(org_id=ORG)
    # 同じ名前の制度は組織のものを残す（2つ並べない）。名前の違う全国共通の制度は出す
    assert [row["program_id"] for row in rows] == ["org_wajima__kiso", "nationwide__other"]
    assert supports.program_payload(rows[0])["nationwide"] is False
