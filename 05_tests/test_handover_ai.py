"""引き継ぎシートの要約と気をつけることを Gemini に作らせる処理（F3。開発手順書 Step 9-3）。

Google Cloud にも Gemini にも接続しない。確かめるのは次の5つ。

- **渡すものが要件定義書10-6どおり**（健康・介護の情報、直近の記録と相談、移る先）
- 要約と気をつけることが、**作った人が直せる案として**返ること（保存はしない）
- Gemini を呼べないときは**空の案を返す**（手で書けるようにする。例外を外に出さない）
- 長すぎる案は、保存できる長さに切ること（表に入らない案を画面に出さない）
- 「考える時間」を 0 で呼ぶこと（要件定義書11-1の10秒以内のため）
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend.config import get_settings
from backend.services import gemini, handover_ai, handovers

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
SURVIVOR = "sv_0001"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)

SURVIVOR_ROW = {
    "survivor_id": SURVIVOR,
    "org_id": ORG,
    "full_name": "佐藤 太郎",
    "age": 82,
    "stage": "evacuation",
    "location_type": "shelter",
    "location_detail": "市民体育館",
}

HEALTH_ROW = {
    "version": 2,
    "source": "professional",
    "chronic_diseases": ["高血圧"],
    "medications": ["降圧剤"],
    "allergies": None,
    "primary_clinic": "輪島中央クリニック",
    "medical_needs": ["透析"],
    "care_level": "要介護2",
    "assistance_mobility": "車いすを使う",
    "assistance_meal": None,
    "assistance_toilet": "一部介助",
    "dementia_notes": None,
    "family_contact": "長女（金沢市）",
}

VISIT_ROWS = [
    {
        "visit_id": "vs_0001",
        "visited_at": NOW,
        "location": "市民体育館",
        "condition": "熱っぽい",
        "meal": "朝だけ",
        "sleep": "夜に何度も起きる",
        "medication": "血圧の薬が切れた",
        "environment": "体育館が寒い",
        "concerns": "薬をどこでもらえるか分からない",
        "change": "前回より食事が減った",
        "next_action": "受診をすすめる",
    }
]

CONSULTATION_ROWS = [
    {
        "consultation_id": "cs_0001",
        "consulted_at": NOW,
        "category": "体調",
        "urgency": "high",
        "body": "胸が苦しい／血圧の薬が切れた",
    }
]

DESTINATION = "輪島中央 福祉避難所"


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_gemini(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("テストから Gemini を呼んではいけません")

    monkeypatch.setattr(gemini, "generate_structured", fail)


def answer(**overrides) -> handover_ai.HandoverDraft:
    payload = {
        "summary": "11月に入って食事の量が減り、血圧の薬が切れた日があった。",
        "cautions": "夜間にトイレで何度も起きるため、入口に近い部屋が望ましい。",
    }
    payload.update(overrides)
    return handover_ai.HandoverDraft(**payload)


def draft(**overrides):
    kwargs = {
        "survivor": SURVIVOR_ROW,
        "health_row": HEALTH_ROW,
        "recent_visits": VISIT_ROWS,
        "recent_consultations": CONSULTATION_ROWS,
        "destination": DESTINATION,
    }
    kwargs.update(overrides)
    return handover_ai.draft(**kwargs)


# --- 渡す本文 ---


def test_渡す本文に健康の情報と直近の記録と移る先が入る():
    contents = handover_ai.build_contents(
        survivor=SURVIVOR_ROW,
        health_row=HEALTH_ROW,
        recent_visits=VISIT_ROWS,
        recent_consultations=CONSULTATION_ROWS,
        destination=DESTINATION,
    )
    assert "透析" in contents
    assert "血圧の薬が切れた" in contents
    assert "胸が苦しい" in contents
    assert DESTINATION in contents


def test_記録も相談も無くても本文を作れる():
    contents = handover_ai.build_contents(
        survivor=SURVIVOR_ROW,
        health_row=None,
        recent_visits=[],
        recent_consultations=[],
        destination=DESTINATION,
    )
    assert DESTINATION in contents


# --- 返ったものの扱い ---


def test_要約と気をつけることの案が返る(monkeypatch):
    monkeypatch.setattr(gemini, "generate_structured", lambda **kwargs: answer())
    result = draft()
    assert "食事の量が減り" in result["summary"]
    assert "入口に近い部屋" in result["cautions"]
    assert result["ai_used"] is True
    assert result["notice"] is None


def test_長すぎる案は保存できる長さに切る(monkeypatch):
    monkeypatch.setattr(
        gemini,
        "generate_structured",
        lambda **kwargs: answer(summary="あ" * (handovers.SUMMARY_MAX_LENGTH + 50)),
    )
    result = draft()
    assert len(result["summary"]) == handovers.SUMMARY_MAX_LENGTH


def test_Geminiを呼べないときは空の案を返す(monkeypatch):
    def unavailable(**kwargs):
        raise gemini.GeminiUnavailable("呼べません")

    monkeypatch.setattr(gemini, "generate_structured", unavailable)
    result = draft()
    assert result["summary"] == ""
    assert result["cautions"] == ""
    assert result["ai_used"] is False
    assert result["notice"]


def test_考える時間を0で呼ぶ(monkeypatch):
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return answer()

    monkeypatch.setattr(gemini, "generate_structured", capture)
    draft()
    assert captured["thinking_budget"] == 0
