"""健康・介護の情報（F3）のテスト（要件定義書F3・7-3-1、開発手順書 Step 6-2）。

Google Cloud には接続しない。確かめるのは次の4つ。

- 要件定義書F3の項目がそろっていること
- **上書きにならないこと**（版が1つ増える）
- **本人が書いたものが「本人の申告」として残り**、一般の支援者は更新できないこと（要件定義書F3）
- F2 の更新の案を反映するとき、**案に無い項目を空で消さないこと**
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.services import health

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_SQL = (REPO_ROOT / "03_sql" / "02_raw_tables.sql").read_text(encoding="utf-8")

ORG = "org_wajima"
SURVIVOR = "sv_0001"
PRO_UID = "uid-pro-1"
SELF_UID = "uid-self-1"


def definition_of(table: str) -> str:
    """03_sql/02_raw_tables.sql から、その表の定義（列の並び）だけを取り出す。"""
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS `\{\{PROJECT\}\}\.gdgoc_raw\."
        + re.escape(table)
        + r"` \((.*?)\n\)",
        RAW_SQL,
        re.DOTALL,
    )
    assert match, f"{table} の定義が 02_raw_tables.sql に見つかりません"
    return match.group(1)


def columns_of(table: str) -> list[str]:
    """その表の列の名前。"""
    return re.findall(r"^\s{2}(\w+) ", definition_of(table), re.MULTILINE)


def current_row(**overrides) -> dict:
    row = {
        "survivor_id": SURVIVOR,
        "org_id": ORG,
        "version": 2,
        "chronic_diseases": ["高血圧", "糖尿病"],
        "medications": ["アムロジピン"],
        "allergies": "そば",
        "primary_clinic": "輪島中央クリニック",
        "medical_needs": ["在宅酸素"],
        "care_level": "要介護2",
        "assistance_mobility": "杖で歩ける",
        "assistance_meal": None,
        "assistance_toilet": None,
        "dementia_notes": None,
        "family_contact": "長女（金沢市）",
        "source": health.SOURCE_PROFESSIONAL,
    }
    row.update(overrides)
    return row


# --- 項目（要件定義書F3） ---


def test_項目は要件定義書F3の11項目() -> None:
    assert sorted(health.FIELD_ORDER) == sorted(
        [
            "chronic_diseases",
            "medications",
            "allergies",
            "primary_clinic",
            "medical_needs",
            "care_level",
            "assistance_mobility",
            "assistance_meal",
            "assistance_toilet",
            "dementia_notes",
            "family_contact",
        ]
    )


def test_項目の並びと名前の表は同じ組() -> None:
    assert set(health.FIELD_ORDER) == set(health.FIELD_LABELS)


def test_複数書く項目は配列の列と同じ() -> None:
    # raw_health_versions で ARRAY<STRING> になっている列と合っていないと保存で落ちる
    array_columns = set(
        re.findall(
            r"^\s{2}(\w+) ARRAY<STRING>", definition_of("raw_health_versions"), re.MULTILINE
        )
    )

    assert set(health.ARRAY_FIELDS) == array_columns


# --- 値をそろえる ---


def test_複数の項目は改行で分ける() -> None:
    fields = health.clean_fields({"medications": "アムロジピン\nワーファリン"})

    assert fields["medications"] == ["アムロジピン", "ワーファリン"]


def test_複数の項目は読点でも分ける() -> None:
    # スマートフォンで「、」区切りで書く人がいる（訪問先での入力）
    fields = health.clean_fields({"chronic_diseases": "高血圧、糖尿病"})

    assert fields["chronic_diseases"] == ["高血圧", "糖尿病"]


def test_複数の項目は空と重複を落とす() -> None:
    fields = health.clean_fields({"medical_needs": ["透析", "", "透析", "  "]})

    assert fields["medical_needs"] == ["透析"]


def test_文章の項目は前後の空白を落とす() -> None:
    assert health.clean_fields({"allergies": "  そば  "})["allergies"] == "そば"


def test_空の文章はNoneにする() -> None:
    assert health.clean_fields({"allergies": "   "})["allergies"] is None


def test_知らない項目は無視する() -> None:
    fields = health.clean_fields({"allergies": "そば", "risk_level": "high"})

    assert set(fields) == set(health.FIELD_ORDER)


def test_長すぎる文章は断る() -> None:
    with pytest.raises(health.HealthValidationError):
        health.clean_fields({"dementia_notes": "あ" * (health.TEXT_MAX_LENGTH + 1)})


def test_多すぎる件数は断る() -> None:
    with pytest.raises(health.HealthValidationError):
        health.clean_fields({"medications": [f"薬{index}" for index in range(health.ARRAY_MAX_ITEMS + 1)]})


@pytest.mark.parametrize("value", ["要介護2", "要支援1", "なし", "不明"])
def test_要介護度は決まった値を通す(value) -> None:
    assert health.validate_care_level(value) == value


def test_要介護度は決まっていない値を断る() -> None:
    with pytest.raises(health.HealthValidationError):
        health.validate_care_level("要介護6")


def test_要介護度は空でよい() -> None:
    assert health.validate_care_level("") is None


def test_どの項目も空なら空と判定する() -> None:
    assert health.is_empty(health.clean_fields({}))
    assert not health.is_empty(health.clean_fields({"allergies": "そば"}))


# --- 立場（要件定義書F3） ---


def test_本人が書いたものは本人の申告として残る() -> None:
    assert health.source_for_role("survivor") == health.SOURCE_SELF
    assert health.SOURCE_LABELS[health.SOURCE_SELF] == "ご本人の申告"


def test_専門職が書いたものは専門職の記入になる() -> None:
    assert health.source_for_role("professional") == health.SOURCE_PROFESSIONAL


def test_一般の支援者は直接更新できない() -> None:
    # 更新は F2 の更新の案を通す（要件定義書F3）
    with pytest.raises(health.HealthValidationError):
        health.source_for_role("volunteer")


# --- 版（要件定義書F3「版として残す」） ---


def test_まだ無ければ版1から始まる() -> None:
    assert health.next_version(None) == 1


def test_版は1つずつ増える() -> None:
    assert health.next_version({"version": 3}) == 4


def test_版の番号が読めなければ断る() -> None:
    for version in (0, "2", None, True):
        with pytest.raises(health.HealthValidationError):
            health.next_version({"version": version})


def test_保存は上書きではなく版を足す(monkeypatch) -> None:
    written: dict = {}
    monkeypatch.setattr(
        health.bq, "insert", lambda table, row, **kwargs: written.update({"table": table, "row": row})
    )

    health.save(
        survivor_id=SURVIVOR,
        org_id=ORG,
        created_by=PRO_UID,
        created_role="professional",
        fields={"allergies": "そば"},
        source=health.SOURCE_PROFESSIONAL,
        current=current_row(),
    )

    assert written["table"] == "raw_health_versions"
    assert written["row"]["version"] == 3


def test_保存する行はraw_health_versionsの列と合う() -> None:
    row = health.build_row(
        survivor_id=SURVIVOR,
        org_id=ORG,
        created_by=PRO_UID,
        created_role="professional",
        fields={"allergies": "そば"},
        source=health.SOURCE_SELF,
        version=1,
    )

    assert set(row) == set(columns_of("raw_health_versions"))


def test_出どころが決まっていなければ断る() -> None:
    with pytest.raises(health.HealthValidationError):
        health.build_row(
            survivor_id=SURVIVOR,
            org_id=ORG,
            created_by=PRO_UID,
            created_role="professional",
            fields={},
            source="guess",
            version=1,
        )


def test_記録が決まっていなければ断る() -> None:
    with pytest.raises(health.HealthValidationError):
        health.build_row(
            survivor_id="",
            org_id=ORG,
            created_by=PRO_UID,
            created_role="professional",
            fields={},
            source=health.SOURCE_SELF,
            version=1,
        )


# --- F2 の更新の案を重ねる ---


def test_案に無い項目は消さない() -> None:
    merged = health.merge(current_row(), {"medications": ["ワーファリン"]})

    assert merged["medications"] == ["ワーファリン"]
    # 案に入っていない項目は、前の版のまま残る
    assert merged["chronic_diseases"] == ["高血圧", "糖尿病"]
    assert merged["allergies"] == "そば"
    assert merged["care_level"] == "要介護2"


def test_空の案では上書きしない() -> None:
    merged = health.merge(current_row(), {"allergies": "", "medical_needs": []})

    assert merged["allergies"] == "そば"
    assert merged["medical_needs"] == ["在宅酸素"]


def test_まだ版が無くても案を重ねられる() -> None:
    merged = health.merge(None, {"care_level": "要介護1"})

    assert merged["care_level"] == "要介護1"
    assert merged["chronic_diseases"] == []


def test_重ねた結果の項目はそろっている() -> None:
    merged = health.merge(current_row(), {})

    assert set(merged) == set(health.FIELD_ORDER)


# --- 画面に返す形 ---


def test_画面に返す形は項目と誰が書いたかを持つ() -> None:
    payload = health.payload({**current_row(), "updated_at": None, "updated_role": "professional"})

    assert payload is not None
    assert payload["version"] == 2
    assert payload["source_label"] == "専門職が記入"
    assert list(payload["fields"]) == list(health.FIELD_ORDER)
    assert payload["fields"]["chronic_diseases"] == ["高血圧", "糖尿病"]


def test_まだ無ければ画面にはNoneを返す() -> None:
    assert health.payload(None) is None
