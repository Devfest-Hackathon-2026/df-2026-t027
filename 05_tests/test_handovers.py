"""引き継ぎシート（F3）の組み立てと値の検査のテスト（開発手順書 Step 9-3）。

Google Cloud には接続しない。確かめるのは次の5つ。

- `raw_handovers` の列と、作る行の列が合っていること
- **移る先と要約のない引き継ぎシートは保存できない**（どこへ向けて作ったかを残すため。要件定義書F3）
- 載せた健康・介護の情報の**版を残す**こと（要件定義書7-3-1）
- 保存したときの版と、いまの版が違うときに**そのことが分かる**こと
  （`raw_handovers` は健康の項目そのものを持たないため、あとで開くと今の情報が出る）
- 印刷用に返す形に、F3の項目・要約・気をつけること・作った人と日時が入ること
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from backend.services import handovers

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_SQL = (REPO_ROOT / "03_sql" / "02_raw_tables.sql").read_text(encoding="utf-8")

ORG = "org_wajima"
SURVIVOR = "sv_0001"
PRO_UID = "uid-pro-1"
HANDOVER = "ho_0001"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)

DESTINATION = "輪島中央 福祉避難所"
SUMMARY = "11月に入って食事の量が減り、血圧の薬が切れた日があった。"
CAUTIONS = "夜間にトイレで何度も起きるため、入口に近い部屋が望ましい。"


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


def build(**overrides) -> dict:
    kwargs = {
        "survivor_id": SURVIVOR,
        "org_id": ORG,
        "created_by": PRO_UID,
        "created_role": "professional",
        "destination": DESTINATION,
        "summary": SUMMARY,
        "cautions": CAUTIONS,
        "health_version": 2,
        "ai_used": True,
    }
    kwargs.update(overrides)
    return handovers.build_row(**kwargs)


def stg_row(**overrides) -> dict:
    """`stg_handovers` の1行と同じ形。"""
    row = {
        "handover_id": HANDOVER,
        "org_id": ORG,
        "survivor_id": SURVIVOR,
        "destination": DESTINATION,
        "health_version": 2,
        "summary": SUMMARY,
        "cautions": CAUTIONS,
        "ai_used": True,
        "created_at": NOW,
        "created_by": PRO_UID,
        "created_role": "professional",
    }
    row.update(overrides)
    return row


# --- 列がそろっているか ---


def test_引き継ぎシートの行の列がraw_handoversと合っている():
    assert set(build()) == set(columns_of("raw_handovers"))


def test_引き継ぎシートのIDが毎回ちがう():
    assert build()["handover_id"] != build()["handover_id"]


# --- 値の検査 ---


def test_移る先のない引き継ぎシートは保存できない():
    # 「どこへ向けて作ったか」を残すため（要件定義書F3）
    with pytest.raises(handovers.HandoverValidationError):
        build(destination="   ")


def test_要約のない引き継ぎシートは保存できない():
    with pytest.raises(handovers.HandoverValidationError):
        build(summary="")


def test_気をつけることは空でもよい():
    row = build(cautions="")
    assert row["cautions"] is None


def test_長すぎる要約は断る():
    with pytest.raises(handovers.HandoverValidationError):
        build(summary="あ" * (handovers.SUMMARY_MAX_LENGTH + 1))


def test_健康の情報の版は数字で残す():
    assert build(health_version=3)["health_version"] == 3


def test_健康の情報がまだ無いときは版を空にする():
    assert build(health_version=None)["health_version"] is None


def test_版が数字でなければ断る():
    with pytest.raises(handovers.HandoverValidationError):
        build(health_version="2")


def test_記録が決まっていなければ断る():
    with pytest.raises(handovers.HandoverValidationError):
        build(survivor_id="")


# --- 画面に返す形 ---


def test_画面に返す形に要約と気をつけることと作った人が入る():
    item = handovers.payload(stg_row(), creator_names={PRO_UID: "保健師 田中"})
    assert item["handover_id"] == HANDOVER
    assert item["destination"] == DESTINATION
    assert item["summary"] == SUMMARY
    assert item["cautions"] == CAUTIONS
    assert item["created_by_name"] == "保健師 田中"
    assert item["created_at"] == NOW
    assert item["ai_used"] is True


def test_保存した版といまの版が同じなら知らせを出さない():
    item = handovers.payload(stg_row(health_version=2), health_version_now=2)
    assert item["health_version_changed"] is False
    assert item["health_version_notice"] is None


def test_保存した版といまの版が違えばそのことを知らせる():
    # raw_handovers は健康の項目そのものを持たないため、あとで開くと「いまの情報」が出る
    item = handovers.payload(stg_row(health_version=2), health_version_now=4)
    assert item["health_version_changed"] is True
    assert handovers.HEALTH_VERSION_NOTICE in item["health_version_notice"]


def test_版が分からないときは違うと決めつけない():
    item = handovers.payload(stg_row(health_version=None), health_version_now=4)
    assert item["health_version_changed"] is False


def test_AIを使わずに書いたシートはそのことが分かる():
    item = handovers.payload(stg_row(ai_used=False))
    assert item["ai_used"] is False
