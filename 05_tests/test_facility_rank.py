"""受け入れ先の候補をルールで並べる処理のテスト（画面の改修の計画.md 7章）。

AI は使わない。施設と健康の情報は架空のもの。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "02_app"))

from backend.services import facilities  # noqa: E402

NOW = dt.datetime(2026, 11, 10, 9, 0, tzinfo=dt.timezone.utc)


def row(facility_id: str, **overrides) -> dict:
    base = {
        "facility_id": facility_id,
        "org_id": "org_test",
        "version": 1,
        "facility_name": f"架空の施設 {facility_id}",
        "facility_type": "welfare_shelter",
        "address": "架空市1-1",
        "vacancies": 2,
        "capabilities": [],
        "water_outage": False,
        "power_outage": False,
        "updated_at": NOW,
        "is_stale": False,
    }
    base.update(overrides)
    return base


def ids(result: list[dict]) -> list[str]:
    return [item["facility_id"] for item in result]


def rank(rows: list[dict], *, medical_needs=None, health_row=None) -> list[dict]:
    return facilities.rank_candidates(rows, medical_needs=medical_needs or [], health_row=health_row)


def test_対応できるものが多い施設を先にする() -> None:
    health_row = {"assistance_mobility": "車いすを使う", "dementia_notes": "軽い認知症がある"}
    rows = [
        row("fc_a", vacancies=9),
        row("fc_b", capabilities=["wheelchair"]),
        row("fc_c", capabilities=["wheelchair", "dementia"]),
    ]
    assert ids(rank(rows, health_row=health_row)) == ["fc_c", "fc_b", "fc_a"]


def test_同じなら空きの数が多い施設を先にする() -> None:
    rows = [row("fc_a", vacancies=1), row("fc_b", vacancies=5), row("fc_c", vacancies=3)]
    assert ids(rank(rows)) == ["fc_b", "fc_c", "fc_a"]


def test_同じなら断水と停電が少ない施設を先にする() -> None:
    rows = [
        row("fc_a", water_outage=True, power_outage=True),
        row("fc_b", water_outage=True),
        row("fc_c"),
    ]
    assert ids(rank(rows)) == ["fc_c", "fc_b", "fc_a"]


def test_同じなら情報が新しい施設を先にする() -> None:
    rows = [
        row("fc_a", is_stale=True, updated_at=NOW - dt.timedelta(hours=30)),
        row("fc_b", updated_at=NOW - dt.timedelta(hours=5)),
        row("fc_c", updated_at=NOW - dt.timedelta(hours=1)),
    ]
    assert ids(rank(rows)) == ["fc_c", "fc_b", "fc_a"]


def test_すべて同じなら施設のIDの順で毎回同じにする() -> None:
    rows = [row("fc_c"), row("fc_a"), row("fc_b")]
    assert ids(rank(rows)) == ["fc_a", "fc_b", "fc_c"]
    assert ids(rank(list(reversed(rows)))) == ["fc_a", "fc_b", "fc_c"]


def test_候補は3件まで() -> None:
    rows = [row(f"fc_{index}") for index in range(5)]
    assert len(rank(rows)) == facilities.MAX_CANDIDATES


def test_なしと書いた欄は介護の必要と読まない() -> None:
    assert facilities.care_needs({"dementia_notes": "認知症なし"}) == []
    assert facilities.care_needs({"assistance_mobility": "車いすは使わない"}) == []
    assert facilities.care_needs({"assistance_mobility": "寝たきり"}) == ["bedridden"]
    assert facilities.care_needs(None) == []


def test_理由は空き_対応できるもの_断水と停電_情報の新しさ() -> None:
    result = rank(
        [row("fc_a", vacancies=3, capabilities=["suction"], power_outage=True, is_stale=True)],
        medical_needs=["たんの吸引"],
    )
    assert result[0]["reason"] == (
        "空き3。対応できるもの：たんの吸引。停電あり。情報は24時間以上前の更新（電話で確かめる）。"
    )
    assert result[0]["stale_notice"] == facilities.STALE_NOTICE
