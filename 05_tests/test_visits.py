"""訪問・見守りの記録（F2）の組み立てと値の検査のテスト（要件定義書F2・7-3-1、開発手順書 Step 6）。

Google Cloud には接続しない。確かめるのは次の3つ。

- **文章に書かれていない項目が「記載なし」になる**（要件定義書F2・10-6。推測で埋めない）
- 更新の案が、画面に出せない値のときに落ちること（要件定義書F1の11種類に無い居場所など）
- `raw_visits`・`raw_update_applies` の列と、作る行の列が合っていること
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from backend.services import health, visits

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_SQL = (REPO_ROOT / "03_sql" / "02_raw_tables.sql").read_text(encoding="utf-8")

ORG = "org_wajima"
SURVIVOR = "sv_0001"
PRO_UID = "uid-pro-1"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)


def columns_of(table: str) -> list[str]:
    """03_sql/02_raw_tables.sql から、その表の列の名前を取り出す。"""
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS `\{\{PROJECT\}\}\.gdgoc_raw\."
        + re.escape(table)
        + r"` \((.*?)\n\)",
        RAW_SQL,
        re.DOTALL,
    )
    assert match, f"{table} の定義が 02_raw_tables.sql に見つかりません"
    return re.findall(r"^\s{2}(\w+) ", match.group(1), re.MULTILINE)


def filled_items() -> dict[str, str]:
    return visits.clean_items({"condition": "熱が37.8度", "meal": "朝だけ食べた"})


def build_row(**overrides):
    row = {
        "survivor_id": SURVIVOR,
        "org_id": ORG,
        "created_by": PRO_UID,
        "created_role": "professional",
        "visited_at": NOW,
        "raw_text": "熱が37.8度あって、朝だけ食べた。",
        "items": filled_items(),
    }
    row.update(overrides)
    return visits.build_visit_row(**row)


# --- 9つの項目（要件定義書F2） ---


def test_項目は要件定義書の9つ() -> None:
    assert list(visits.VISIT_FIELDS) == [
        "location",
        "condition",
        "meal",
        "sleep",
        "medication",
        "environment",
        "concerns",
        "change",
        "next_action",
    ]


def test_書かれていない項目は記載なしになる() -> None:
    items = visits.clean_items({"condition": "熱が37.8度"})

    assert items["condition"] == "熱が37.8度"
    assert items["meal"] == visits.NOT_RECORDED
    assert all(value for value in items.values())


@pytest.mark.parametrize("value", [None, "", "   ", "\n"])
def test_空の項目は記載なしになる(value) -> None:
    assert visits.clean_items({"sleep": value})["sleep"] == visits.NOT_RECORDED


def test_知らない項目は無視する() -> None:
    items = visits.clean_items({"condition": "元気", "risk_level": "high"})

    assert set(items) == set(visits.VISIT_FIELDS)


def test_項目の前後の空白は落とす() -> None:
    assert visits.clean_items({"meal": "  3食  "})["meal"] == "3食"


def test_長すぎる項目は断る() -> None:
    with pytest.raises(visits.VisitValidationError):
        visits.clean_items({"concerns": "あ" * (visits.ITEM_MAX_LENGTH + 1)})


def test_項目に文章以外を入れたら断る() -> None:
    with pytest.raises(visits.VisitValidationError):
        visits.clean_items({"condition": 37})


def test_すべて記載なしの記録は保存できない() -> None:
    # 押し間違いで中身のない記録が残るのを防ぐ
    with pytest.raises(visits.VisitValidationError):
        build_row(items=visits.clean_items({}))


# --- 入力した文章 ---


def test_文章は必須にできる() -> None:
    with pytest.raises(visits.VisitValidationError):
        visits.clean_raw_text("  ")


def test_手入力のときは文章が空でもよい() -> None:
    assert visits.clean_raw_text("", required=False) is None


def test_長すぎる文章は断る() -> None:
    with pytest.raises(visits.VisitValidationError):
        visits.clean_raw_text("あ" * (visits.RAW_TEXT_MAX_LENGTH + 1))


# --- 訪問した日時 ---


def test_訪問した日時は既定で今になる() -> None:
    assert visits.clean_visited_at(None, now=NOW) == NOW


def test_時間帯のない日時はUTCとして読む() -> None:
    naive = dt.datetime(2026, 11, 8, 9, 0)

    assert visits.clean_visited_at(naive, now=NOW).tzinfo == dt.timezone.utc


def test_未来の日時は断る() -> None:
    future = NOW + dt.timedelta(days=2)

    with pytest.raises(visits.VisitValidationError):
        visits.clean_visited_at(future, now=NOW)


def test_端末の時計の少しのずれは通す() -> None:
    # 発行されたばかりのトークンと同じで、端末の時計は数秒ずれる（Step 5 で踏んだ）
    slightly_ahead = NOW + dt.timedelta(minutes=5)

    assert visits.clean_visited_at(slightly_ahead, now=NOW) == slightly_ahead


# --- 居場所の更新の案 ---


def test_居場所の案は変化があるときだけ返す() -> None:
    assert visits.normalize_location_proposal(
        {"change_suggested": False, "location_type": "home"}
    ) is None


def test_居場所の案は11種類にない値を落とす() -> None:
    # 画面に出せない値を案として残すと、反映のときに何を書くか決められない
    assert visits.normalize_location_proposal(
        {"change_suggested": True, "location_type": "tent"}
    ) is None


def test_居場所の案には画面に出す名前を付ける() -> None:
    proposal = visits.normalize_location_proposal(
        {
            "change_suggested": True,
            "location_type": "temporary_built",
            "location_detail": " 仮設第1団地 ",
            "reason": "仮設に移ったと話している",
        }
    )

    assert proposal == {
        "change_suggested": True,
        "location_type": "temporary_built",
        "location_type_label": "仮設住宅（建設型）",
        "location_detail": "仮設第1団地",
        "reason": "仮設に移ったと話している",
    }


def test_居場所の案に案の形でないものを渡したらNone() -> None:
    assert visits.normalize_location_proposal(None) is None
    assert visits.normalize_location_proposal("home") is None


# --- 健康・介護の更新の案 ---


def test_健康の案は中身がないときは落ちる() -> None:
    assert visits.normalize_health_proposal({"change_suggested": True, "fields": {}}) is None


def test_健康の案は項目をそろえて返す() -> None:
    proposal = visits.normalize_health_proposal(
        {
            "change_suggested": True,
            "reason": "新しい薬が増えたと話している",
            "fields": {"medications": ["ワーファリン", "", "ワーファリン"], "care_level": "要介護2"},
        }
    )

    assert proposal is not None
    assert proposal["fields"]["medications"] == ["ワーファリン"]
    assert proposal["fields"]["care_level"] == "要介護2"
    assert proposal["reason"] == "新しい薬が増えたと話している"


def test_健康の案は平らな形でも受け取る() -> None:
    # Gemini の返事は fields を持たない平らな形で返る
    proposal = visits.normalize_health_proposal(
        {"change_suggested": True, "allergies": "そば"}
    )

    assert proposal is not None
    assert proposal["fields"]["allergies"] == "そば"


def test_健康の案の要介護度が決まった値でなければ断る() -> None:
    with pytest.raises(visits.VisitValidationError):
        visits.normalize_health_proposal(
            {"change_suggested": True, "care_level": "とても重い"}
        )


# --- 更新の案の状況（要件定義書7-3-2） ---


def test_案がなければ状況はnone() -> None:
    assert visits.proposal_status(location_proposal=None, health_proposal=None) == visits.STATUS_NONE


def test_案があれば状況はpending() -> None:
    # 反映したことは raw_update_applies に書く（proposal_status は書き換えない）
    status = visits.proposal_status(
        location_proposal={"change_suggested": True}, health_proposal=None
    )

    assert status == visits.STATUS_PENDING


def test_一般の支援者が書いても専門職が書いても確認待ちで保存する() -> None:
    proposal = {"change_suggested": True, "location_type": "home"}
    for role in ("professional", "volunteer"):
        row = build_row(
            created_role=role,
            location_proposal=visits.normalize_location_proposal(proposal),
        )
        assert row["proposal_status"] == visits.STATUS_PENDING


# --- raw_visits に入れる行 ---


def test_記録の行はraw_visitsの列と合う() -> None:
    row = build_row()

    assert set(row) == set(columns_of("raw_visits"))


def test_記録の行は版1から始まる() -> None:
    assert build_row()["version"] == 1


def test_記録のIDは毎回ちがう() -> None:
    assert build_row()["visit_id"] != build_row()["visit_id"]


def test_記録の出来事のIDは毎回ちがう() -> None:
    assert build_row()["event_id"] != build_row()["event_id"]


def test_版の番号が正しくなければ断る() -> None:
    for version in (0, -1, "1", True):
        with pytest.raises(visits.VisitValidationError):
            build_row(version=version)


def test_組織が決まっていなければ断る() -> None:
    with pytest.raises(visits.VisitValidationError):
        build_row(org_id="")


def test_手入力の記録はai_usedがfalse() -> None:
    assert build_row(ai_used=False)["ai_used"] is False


# --- 反映の記録（raw_update_applies） ---


def test_反映の記録はraw_update_appliesの列と合う(monkeypatch) -> None:
    written: dict = {}
    monkeypatch.setattr(
        visits.bq, "insert", lambda table, row, **kwargs: written.update({"table": table, "row": row})
    )

    visits.record_apply(
        survivor_id=SURVIVOR,
        org_id=ORG,
        visit_id="vs_1",
        created_by=PRO_UID,
        created_role="professional",
        applied_items=[visits.ITEM_LOCATION],
        result=visits.RESULT_APPLIED,
    )

    assert written["table"] == "raw_update_applies"
    assert set(written["row"]) == set(columns_of("raw_update_applies"))
    assert written["row"]["applied_items"] == ["location"]


def test_知らない反映の内容は落とす(monkeypatch) -> None:
    written: dict = {}
    monkeypatch.setattr(visits.bq, "insert", lambda table, row, **kwargs: written.update(row))

    visits.record_apply(
        survivor_id=SURVIVOR,
        org_id=ORG,
        visit_id="vs_1",
        created_by=PRO_UID,
        created_role="professional",
        applied_items=["location", "risk_level"],
        result=visits.RESULT_APPLIED,
    )

    assert written["applied_items"] == ["location"]


def test_反映するのに内容が選ばれていなければ断る() -> None:
    with pytest.raises(visits.VisitValidationError):
        visits.record_apply(
            survivor_id=SURVIVOR,
            org_id=ORG,
            visit_id="vs_1",
            created_by=PRO_UID,
            created_role="professional",
            applied_items=[],
            result=visits.RESULT_APPLIED,
        )


def test_反映しないときは内容が空でよい(monkeypatch) -> None:
    written: dict = {}
    monkeypatch.setattr(visits.bq, "insert", lambda table, row, **kwargs: written.update(row))

    visits.record_apply(
        survivor_id=SURVIVOR,
        org_id=ORG,
        visit_id="vs_1",
        created_by=PRO_UID,
        created_role="professional",
        applied_items=[],
        result=visits.RESULT_REJECTED,
    )

    assert written["result"] == "rejected"
    assert written["applied_items"] == []


def test_決まっていない反映の結果は断る() -> None:
    with pytest.raises(visits.VisitValidationError):
        visits.record_apply(
            survivor_id=SURVIVOR,
            org_id=ORG,
            visit_id="vs_1",
            created_by=PRO_UID,
            created_role="professional",
            applied_items=["location"],
            result="maybe",
        )


# --- 読んだ行を画面に返す形にする ---


def test_JSONの列が文字列で返っても辞書にする() -> None:
    # BigQuery のクライアントの版によって、JSON の列は文字列で返ることがある
    payload = visits.visit_payload(
        {
            "visit_id": "vs_1",
            "location_proposal": '{"change_suggested": true, "location_type": "home"}',
            "health_proposal": None,
            "is_pending": True,
            **visits.clean_items({"condition": "元気"}),
        }
    )

    assert payload["location_proposal"]["location_type"] == "home"
    assert payload["health_proposal"] is None
    assert payload["items"]["condition"] == "元気"
    assert payload["is_pending"] is True


def test_記録した人の名前を付ける() -> None:
    payload = visits.visit_payload(
        {"visit_id": "vs_1", "recorded_by": "uid-vol-1", **visits.clean_items({"meal": "3食"})},
        supporter_names={"uid-vol-1": "山本さん"},
    )

    assert payload["recorded_by_name"] == "山本さん"


def test_Geminiに渡す健康の情報は記載なしで埋める() -> None:
    text = health.summary_for_ai({"chronic_diseases": ["高血圧"], "medications": []})

    assert "持病：高血圧" in text
    assert "飲んでいる薬：記載なし" in text


def test_Geminiに渡す直近の記録は件数を絞る() -> None:
    rows = [{"visited_at": NOW, **visits.clean_items({"condition": f"{index}日目"})} for index in range(5)]

    text = visits.recent_items_for_ai(rows, limit=2)

    assert text.count("体調：") == 2
