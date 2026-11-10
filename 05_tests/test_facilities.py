"""受け入れ先の施設と移る先の決定（F5）の組み立てと値の検査のテスト（開発手順書 Step 9）。

Google Cloud には接続しない。確かめるのは次の6つ。

- `raw_facilities`・`raw_placements` の列と、作る行の列が合っていること
- 施設は**上書きせず版を足す**。空き・断水・停電だけを変えても、名前と対応できる医療は引き継ぐ
- **空きがない施設と、必要な医療に対応できない施設は、Gemini に渡す前に外す**（要件定義書F5）
- **対応できるか判断できない必要な医療では外さない**（分からないものを「対応できない」と
  決めてしまうと、候補が理由なく消える）
- 最終更新から24時間以上たった施設は、そのことが分かる形で返る（要件定義書F5）
- 移る先の決定は、**候補から選んだかどうか**を自分で数え、理由のない決定を受け取らない
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from backend.services import facilities

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_SQL = (REPO_ROOT / "03_sql" / "02_raw_tables.sql").read_text(encoding="utf-8")

ORG = "org_wajima"
SURVIVOR = "sv_0001"
PRO_UID = "uid-pro-1"
FACILITY = "fc_0001"
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


def facility_row(**overrides) -> dict:
    """`stg_facilities_latest` の1行と同じ形。"""
    row = {
        "facility_id": FACILITY,
        "org_id": ORG,
        "version": 1,
        "facility_name": "輪島中央 福祉避難所",
        "facility_type": "welfare_shelter",
        "address": "輪島市中央町1-1",
        "vacancies": 3,
        "capabilities": ["dementia", "wheelchair"],
        "water_outage": False,
        "power_outage": False,
        "updated_at": NOW,
        "updated_by": PRO_UID,
        "is_stale": False,
    }
    row.update(overrides)
    return row


# --- 値の正本 ---


def test_施設の種類に福祉避難所と介護施設と病院がある():
    labels = list(facilities.FACILITY_TYPES.values())
    for label in ("福祉避難所", "介護施設", "病院"):
        assert label in labels


def test_対応できる医療に透析とたんの吸引と認知症と車いすがある():
    labels = list(facilities.CAPABILITIES.values())
    for word in ("透析", "たんの吸引", "認知症", "車いす"):
        assert any(word in label for label in labels), f"{word} の項目がありません"


def test_古いと見なす時間は24時間():
    # 要件定義書F5「最終更新から24時間以上など」。ビュー（stg_facilities_latest）と同じ値にする
    assert facilities.STALE_HOURS == 24


# --- 列がそろっているか ---


def test_施設の行の列がraw_facilitiesと合っている():
    row = facilities.build_row(
        org_id=ORG,
        created_by=PRO_UID,
        created_role="professional",
        fields={
            "facility_name": "輪島中央 福祉避難所",
            "facility_type": "welfare_shelter",
            "address": "輪島市中央町1-1",
            "vacancies": 3,
            "capabilities": ["dementia"],
            "water_outage": False,
            "power_outage": False,
        },
        version=1,
    )
    assert set(row) == set(columns_of("raw_facilities"))


def test_移る先の行の列がraw_placementsと合っている():
    row = facilities.build_placement_row(
        survivor_id=SURVIVOR,
        org_id=ORG,
        created_by=PRO_UID,
        created_role="professional",
        candidates=[{"facility_id": FACILITY, "reason": "認知症に対応できる"}],
        chosen_facility_id=FACILITY,
        chosen_reason="空きがあり、認知症の対応ができるため",
    )
    assert set(row) == set(columns_of("raw_placements"))


# --- 施設の登録と更新 ---


def test_名前のない施設は登録できない():
    with pytest.raises(facilities.FacilityValidationError):
        facilities.clean_fields({"facility_name": "  ", "facility_type": "hospital", "vacancies": 1})


def test_知らない種類の施設は登録できない():
    with pytest.raises(facilities.FacilityValidationError):
        facilities.clean_fields(
            {"facility_name": "どこか", "facility_type": "unknown_type", "vacancies": 1}
        )


def test_知らない対応できる医療は受け取らない():
    # 画面の選択肢（/api/public-config）にない値を受け取ると、候補を外す判断が狂う
    with pytest.raises(facilities.FacilityValidationError):
        facilities.clean_fields(
            {
                "facility_name": "どこか",
                "facility_type": "hospital",
                "vacancies": 1,
                "capabilities": ["透析できます"],
            }
        )


def test_空きの数に負の数は入れられない():
    with pytest.raises(facilities.FacilityValidationError):
        facilities.clean_fields(
            {"facility_name": "どこか", "facility_type": "hospital", "vacancies": -1}
        )


def test_空きと断水だけを変えても名前と対応できる医療は引き継ぐ():
    current = facility_row()
    row = facilities.build_update_row(
        current=current,
        created_by=PRO_UID,
        created_role="professional",
        vacancies=0,
        water_outage=True,
        power_outage=None,
    )
    assert row["version"] == 2
    assert row["facility_name"] == current["facility_name"]
    assert row["facility_type"] == current["facility_type"]
    assert row["address"] == current["address"]
    assert row["capabilities"] == current["capabilities"]
    assert row["vacancies"] == 0
    assert row["water_outage"] is True
    # 渡されなかった項目は、前の版のままにする（False で上書きしない）
    assert row["power_outage"] is False


def test_版が読み取れない施設は更新できない():
    with pytest.raises(facilities.FacilityValidationError):
        facilities.build_update_row(
            current=facility_row(version=None),
            created_by=PRO_UID,
            created_role="professional",
            vacancies=1,
            water_outage=None,
            power_outage=None,
        )


# --- Gemini に渡す前に外す（要件定義書F5） ---


def test_必要な医療を対応できる医療のコードに読み替える():
    matched, unmatched = facilities.required_capabilities(["人工透析", "たんの吸引"])
    assert matched == ["dialysis", "suction"]
    assert unmatched == []


def test_読み替えられない必要な医療は分からないものとして残す():
    matched, unmatched = facilities.required_capabilities(["週3回の通院の付き添い"])
    assert matched == []
    assert unmatched == ["週3回の通院の付き添い"]


def test_空きがない施設は候補に渡さない():
    rows = [facility_row(), facility_row(facility_id="fc_0002", vacancies=0)]
    eligible, excluded = facilities.filter_eligible(rows, medical_needs=[])
    assert [item["facility_id"] for item in eligible] == [FACILITY]
    assert excluded[0]["facility_id"] == "fc_0002"
    assert excluded[0]["reason"] == facilities.EXCLUDED_NO_VACANCY


def test_空きの数が未記入の施設は候補に渡さない():
    rows = [facility_row(vacancies=None)]
    eligible, excluded = facilities.filter_eligible(rows, medical_needs=[])
    assert eligible == []
    assert excluded[0]["reason"] == facilities.EXCLUDED_NO_VACANCY


def test_必要な医療に対応できない施設は候補に渡さない():
    rows = [
        facility_row(capabilities=["dialysis", "wheelchair"]),
        facility_row(facility_id="fc_0002", capabilities=["wheelchair"]),
    ]
    eligible, excluded = facilities.filter_eligible(rows, medical_needs=["透析"])
    assert [item["facility_id"] for item in eligible] == [FACILITY]
    assert excluded[0]["facility_id"] == "fc_0002"
    assert excluded[0]["reason"] == facilities.EXCLUDED_NO_CAPABILITY


def test_判断できない必要な医療では施設を外さない():
    # 「対応できるか分からない」を「対応できない」にすると、候補が理由なく消える
    rows = [facility_row(capabilities=[])]
    eligible, excluded = facilities.filter_eligible(
        rows, medical_needs=["週3回の通院の付き添い"]
    )
    assert [item["facility_id"] for item in eligible] == [FACILITY]
    assert excluded == []


def test_断水や停電でも候補からは外さない():
    # 要件定義書F5が外すと決めているのは「空きがない」「必要な医療に対応できない」の2つだけ。
    # 断水・停電は候補に出したうえで、画面と理由で分かるようにする
    rows = [facility_row(water_outage=True, power_outage=True)]
    eligible, _ = facilities.filter_eligible(rows, medical_needs=[])
    assert len(eligible) == 1


# --- 画面に返す形 ---


def test_古い施設はそのことが分かる形で返る():
    item = facilities.payload(facility_row(is_stale=True))
    assert item["is_stale"] is True
    assert facilities.STALE_NOTICE in item["stale_notice"]


def test_新しい施設には古いという知らせを付けない():
    item = facilities.payload(facility_row(is_stale=False))
    assert item["is_stale"] is False
    assert item["stale_notice"] is None


def test_画面に返す形に種類と対応できる医療の名前が入る():
    item = facilities.payload(facility_row())
    assert item["facility_type_label"] == facilities.FACILITY_TYPES["welfare_shelter"]
    assert item["capability_labels"] == [
        facilities.CAPABILITIES["dementia"],
        facilities.CAPABILITIES["wheelchair"],
    ]


# --- 移る先の決定 ---


def test_候補から選んだときは候補から選んだと残る():
    row = facilities.build_placement_row(
        survivor_id=SURVIVOR,
        org_id=ORG,
        created_by=PRO_UID,
        created_role="professional",
        candidates=[{"facility_id": FACILITY, "reason": "空きがある"}],
        chosen_facility_id=FACILITY,
        chosen_reason="近くて家族が通いやすい",
    )
    assert row["chosen_from_candidates"] is True
    assert row["candidates"] == {"items": [{"facility_id": FACILITY, "reason": "空きがある"}]}


def test_候補にない施設を選んでも決められる():
    # 要件定義書F5「候補にない施設も選べる」
    row = facilities.build_placement_row(
        survivor_id=SURVIVOR,
        org_id=ORG,
        created_by=PRO_UID,
        created_role="professional",
        candidates=[{"facility_id": FACILITY, "reason": "空きがある"}],
        chosen_facility_id="fc_9999",
        chosen_reason="本人の希望",
    )
    assert row["chosen_from_candidates"] is False


def test_理由のない決定は受け取らない():
    with pytest.raises(facilities.FacilityValidationError):
        facilities.build_placement_row(
            survivor_id=SURVIVOR,
            org_id=ORG,
            created_by=PRO_UID,
            created_role="professional",
            candidates=[],
            chosen_facility_id=FACILITY,
            chosen_reason="   ",
        )


def test_施設を選んでいない決定は受け取らない():
    with pytest.raises(facilities.FacilityValidationError):
        facilities.build_placement_row(
            survivor_id=SURVIVOR,
            org_id=ORG,
            created_by=PRO_UID,
            created_role="professional",
            candidates=[],
            chosen_facility_id="",
            chosen_reason="理由はある",
        )


def test_候補は3件までにする():
    many = [{"facility_id": f"fc_{i:04d}", "reason": "理由"} for i in range(5)]
    row = facilities.build_placement_row(
        survivor_id=SURVIVOR,
        org_id=ORG,
        created_by=PRO_UID,
        created_role="professional",
        candidates=many,
        chosen_facility_id="fc_0000",
        chosen_reason="理由はある",
    )
    assert len(row["candidates"]["items"]) == facilities.MAX_CANDIDATES
